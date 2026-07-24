"""The occurrence engine.

Matching is done on *dates* first and clock times second. That is what keeps a
query such as ``0 0 29 2 *`` fast: the engine walks candidate years and the
months named by the month field, so a leap day four years out costs a few
hundred comparisons rather than the two million minutes between here and there.

Everything below the :class:`Occurrence` layer works on naive local datetimes.
Timezones are applied at the edge, which lets :mod:`cron_explainer.dst` reuse
the same matcher to enumerate wall clock times that a DST transition skips or
repeats.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import MAXYEAR, MINYEAR, UTC, date, datetime, timedelta, tzinfo
from itertools import pairwise
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cron_explainer.errors import CronError
from cron_explainer.parser import (
    MONTH_FULL,
    DomExtras,
    DowExtras,
    ParsedExpression,
    ParsedField,
    parse,
)

#: How far the date walker will look before declaring that nothing matches.
MAX_SCAN_YEARS: Final[int] = 100

#: Hard cap on how many results any single tool call will return.
MAX_RESULTS: Final[int] = 100

#: Safety valve so no call can spin, even on a pathological expression.
MAX_CANDIDATES: Final[int] = 500_000

_SATURDAY: Final[int] = 6
_SUNDAY: Final[int] = 0


# --------------------------------------------------------------------------
# Calendar helpers
# --------------------------------------------------------------------------


def canonical_weekday(day: date) -> int:
    """0 = Sunday through 6 = Saturday, matching the parser's canonical scale."""
    return day.isoweekday() % 7


def last_weekday_of_month(year: int, month: int) -> int:
    """Day number of the last Monday-to-Friday day in the month (Quartz ``LW``)."""
    day = monthrange(year, month)[1]
    while canonical_weekday(date(year, month, day)) in (_SATURDAY, _SUNDAY):
        day -= 1
    return day


def nearest_weekday(year: int, month: int, target: int) -> int:
    """Quartz ``nW``: the weekday nearest ``target``, never crossing months."""
    length = monthrange(year, month)[1]
    day = min(target, length)
    weekday = canonical_weekday(date(year, month, day))
    if weekday not in (_SATURDAY, _SUNDAY):
        return day
    if weekday == _SATURDAY:
        return day - 1 if day > 1 else day + 2
    return day + 1 if day < length else day - 2


def dom_matches(field: ParsedField, year: int, month: int, day: int) -> bool:
    """Does this day-of-month field match, given the month it lands in?"""
    if day in field.values:
        return True
    extras: DomExtras = field.dom_extras
    if not extras:
        return False
    length = monthrange(year, month)[1]
    for offset in extras.last_offsets:
        if day == length - offset:
            return True
    if extras.last_weekday and day == last_weekday_of_month(year, month):
        return True
    return any(day == nearest_weekday(year, month, target) for target in extras.nearest_weekday)


def dow_matches(field: ParsedField, year: int, month: int, day: int) -> bool:
    """Does this day-of-week field match, including ``L`` and ``#`` forms?"""
    weekday = canonical_weekday(date(year, month, day))
    if weekday in field.values:
        return True
    extras: DowExtras = field.dow_extras
    if not extras:
        return False
    if weekday in extras.last and day + 7 > monthrange(year, month)[1]:
        return True
    occurrence = (day - 1) // 7 + 1
    return any(weekday == target and occurrence == nth for target, nth in extras.nth)


# --------------------------------------------------------------------------
# The schedule
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Occurrence:
    """One firing, resolved against a timezone."""

    local: datetime  # aware, fold=0
    status: str  # "normal" or "ambiguous"
    repeat: datetime | None = None  # the second instant of an ambiguous wall time

    @property
    def utc(self) -> datetime:
        return self.local.astimezone(UTC)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "local": self.local.isoformat(),
            "utc": self.utc.isoformat().replace("+00:00", "Z"),
            "offset": _offset_label(self.local),
            "weekday": self.local.strftime("%A"),
        }
        if self.status == "ambiguous" and self.repeat is not None:
            payload["dst"] = "ambiguous"
            payload["dst_note"] = (
                "This wall clock time happens twice on this date because the clocks "
                "go back. Many schedulers fire the job twice."
            )
            payload["second_occurrence"] = self.repeat.isoformat()
        return payload


@dataclass(frozen=True)
class RunList:
    """The result of a next/previous query."""

    occurrences: tuple[Occurrence, ...]
    skipped: tuple[datetime, ...] = ()  # naive local times that do not exist
    exhausted: bool = False  # the search horizon ran out before ``count`` was reached


class CronSchedule:
    """A parsed expression turned into something that can be iterated."""

    def __init__(self, parsed: ParsedExpression) -> None:
        self.parsed = parsed
        self.seconds: tuple[int, ...] = (
            tuple(sorted(parsed.field("second").values)) if parsed.has_seconds else (0,)
        )
        self.minutes: tuple[int, ...] = tuple(sorted(parsed.field("minute").values))
        self.hours: tuple[int, ...] = tuple(sorted(parsed.field("hour").values))
        self.months: tuple[int, ...] = tuple(sorted(parsed.field("month").values))
        self.dom: ParsedField = parsed.field("dom")
        self.dow: ParsedField = parsed.field("dow")
        self.years: frozenset[int] | None = parsed.field("year").values if parsed.has_year else None

    # -- introspection ----------------------------------------------------

    @property
    def day_rule(self) -> str:
        """``"or"`` when both day fields are restricted, otherwise ``"and"``."""
        return self.parsed.day_rule

    @property
    def times_per_day(self) -> int:
        """How many firings a matching day contains."""
        return len(self.seconds) * len(self.minutes) * len(self.hours)

    def day_matches(self, year: int, month: int, day: int) -> bool:
        """Apply the day-of-month / day-of-week rule for one date."""
        by_dom = dom_matches(self.dom, year, month, day)
        by_dow = dow_matches(self.dow, year, month, day)
        if self.day_rule == "and":
            return by_dom and by_dow
        return by_dom or by_dow

    def matches(self, moment: datetime) -> bool:
        """Would the schedule fire at this naive local datetime?"""
        return (
            moment.second in self.seconds
            and moment.minute in self.minutes
            and moment.hour in self.hours
            and moment.month in self.months
            and (self.years is None or moment.year in self.years)
            and self.day_matches(moment.year, moment.month, moment.day)
        )

    # -- iteration --------------------------------------------------------

    def iter_dates(self, start: date, *, forward: bool = True) -> Iterator[date]:
        """Yield matching dates from ``start`` (inclusive), jumping over the rest."""
        step = 1 if forward else -1
        months = self.months if forward else tuple(reversed(self.months))
        anchor = (start.year, start.month, start.day)
        year = start.year

        for _ in range(MAX_SCAN_YEARS + 1):
            if not MINYEAR <= year <= MAXYEAR:
                return
            if self.years is None or year in self.years:
                for month in months:
                    if forward and (year, month) < anchor[:2]:
                        continue
                    if not forward and (year, month) > anchor[:2]:
                        continue
                    length = monthrange(year, month)[1]
                    days = range(1, length + 1) if forward else range(length, 0, -1)
                    for day in days:
                        if forward and (year, month, day) < anchor:
                            continue
                        if not forward and (year, month, day) > anchor:
                            continue
                        if self.day_matches(year, month, day):
                            yield date(year, month, day)
            year += step
            if self.years is not None:
                if forward and year > max(self.years):
                    return
                if not forward and year < min(self.years):
                    return

    def iter_naive(
        self,
        start: datetime,
        *,
        forward: bool = True,
        inclusive: bool = False,
    ) -> Iterator[datetime]:
        """Yield matching naive local datetimes, in order, starting at ``start``."""
        hours = self.hours if forward else tuple(reversed(self.hours))
        minutes = self.minutes if forward else tuple(reversed(self.minutes))
        seconds = self.seconds if forward else tuple(reversed(self.seconds))
        first_date = start.date()

        for day in self.iter_dates(first_date, forward=forward):
            boundary = day == first_date
            for hour in hours:
                for minute in minutes:
                    for second in seconds:
                        moment = datetime(day.year, day.month, day.day, hour, minute, second)
                        if boundary:
                            if forward and (moment < start or (moment == start and not inclusive)):
                                continue
                            if not forward and (
                                moment > start or (moment == start and not inclusive)
                            ):
                                continue
                        yield moment

    def iter_naive_between(self, start: datetime, end: datetime) -> Iterator[datetime]:
        """Yield matching naive datetimes in the half open window ``[start, end)``."""
        for moment in self.iter_naive(start, forward=True, inclusive=True):
            if moment >= end:
                return
            yield moment

    # -- feasibility ------------------------------------------------------

    def first_date_on_or_after(self, start: date) -> date | None:
        for day in self.iter_dates(start, forward=True):
            return day
        return None

    def never_runs(self, reference: date | None = None) -> bool:
        anchor = reference or date(2000, 1, 1)
        return self.first_date_on_or_after(anchor) is None

    def impossible_reason(self) -> str:
        """Explain, as specifically as possible, why nothing ever matches."""
        # fmt: off
        max_length = {
            1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
            7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31,
        }
        # fmt: on
        if self.day_rule == "and" and self.dom.restricted and not self.dom.dom_extras:
            smallest = min(self.dom.values) if self.dom.values else 32
            impossible = [month for month in self.months if smallest > max_length[month]]
            if len(impossible) == len(self.months) and self.months:
                names = ", ".join(MONTH_FULL[month - 1] for month in self.months)
                return f"Day {smallest} never occurs in {names}, so this expression can never fire."
        if self.years is not None and not self.years:
            return "The year field excludes every year."
        return (
            f"No matching date exists within the next {MAX_SCAN_YEARS} years, "
            "so this expression is treated as one that never fires."
        )


def build(expression: str, dialect: str = "auto") -> tuple[ParsedExpression, CronSchedule]:
    """Parse and compile in one step."""
    parsed = parse(expression, dialect)
    return parsed, CronSchedule(parsed)


# --------------------------------------------------------------------------
# Timezone layer
# --------------------------------------------------------------------------


def get_timezone(name: str) -> ZoneInfo:
    """Look up an IANA zone, with an error a human can act on."""
    if not isinstance(name, str) or not name.strip():
        raise CronError(
            "A timezone name is required.",
            expected='an IANA name such as "UTC", "America/New_York" or "Europe/Berlin"',
        )
    try:
        return ZoneInfo(name.strip())
    except ZoneInfoNotFoundError:
        raise CronError(
            f'Unknown timezone "{name}".',
            expected='an IANA name such as "UTC", "America/New_York" or "Europe/Berlin"',
            suggestion=(
                "Names are case sensitive and use the Region/City form. "
                "On Windows the tzdata package supplies the database."
            ),
        ) from None
    except (ValueError, OSError):
        raise CronError(
            f'"{name}" is not a usable timezone name.',
            expected='an IANA name such as "UTC" or "America/New_York"',
        ) from None


def classify_local(moment: datetime, zone: tzinfo) -> tuple[str, datetime | None]:
    """Classify a naive wall clock time against a zone.

    Returns ``("normal", None)``, ``("nonexistent", None)`` for a time inside a
    spring forward gap, or ``("ambiguous", second_instant)`` for a time that the
    clocks visit twice.
    """
    first = moment.replace(tzinfo=zone, fold=0)
    if first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != moment:
        return "nonexistent", None
    second = moment.replace(tzinfo=zone, fold=1)
    if second.utcoffset() != first.utcoffset():
        return "ambiguous", second
    return "normal", None


def _offset_label(moment: datetime) -> str:
    offset = moment.utcoffset() or timedelta(0)
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def _collect(
    schedule: CronSchedule,
    zone: tzinfo,
    anchor: datetime,
    count: int,
    *,
    forward: bool,
) -> RunList:
    naive_anchor = anchor.astimezone(zone).replace(tzinfo=None)
    found: list[Occurrence] = []
    skipped: list[datetime] = []
    examined = 0
    exhausted = True

    for moment in schedule.iter_naive(naive_anchor, forward=forward):
        examined += 1
        if examined > MAX_CANDIDATES:  # pragma: no cover - safety valve
            break
        status, second = classify_local(moment, zone)
        if status == "nonexistent":
            skipped.append(moment)
            continue
        aware = moment.replace(tzinfo=zone, fold=0)
        if forward and aware <= anchor:
            continue
        if not forward and aware >= anchor:
            continue
        found.append(Occurrence(local=aware, status=status, repeat=second))
        if len(found) >= count:
            exhausted = False
            break

    return RunList(occurrences=tuple(found), skipped=tuple(skipped), exhausted=exhausted)


def next_runs(
    schedule: CronSchedule,
    zone: tzinfo,
    after: datetime,
    count: int = 5,
) -> RunList:
    """The next ``count`` firings strictly after ``after``."""
    return _collect(schedule, zone, after, count, forward=True)


def previous_runs(
    schedule: CronSchedule,
    zone: tzinfo,
    before: datetime,
    count: int = 5,
) -> RunList:
    """The most recent ``count`` firings strictly before ``before``."""
    return _collect(schedule, zone, before, count, forward=False)


# --------------------------------------------------------------------------
# Frequency analysis
# --------------------------------------------------------------------------

_DAYS_PER_YEAR: Final[float] = 365.2425
_SAMPLE_DAYS: Final[int] = 1461  # four years, so one leap day is always included
_GAP_SAMPLE_LIMIT: Final[int] = 4000
_GAP_WINDOW_DAYS: Final[int] = 366 * 5


@dataclass(frozen=True)
class ScheduleStats:
    """Frequency and gap analysis for one schedule."""

    never_runs: bool
    reason: str | None
    times_per_matching_day: int
    matching_days_per_year: float
    runs_per_hour: float
    runs_per_day: float
    runs_per_week: float
    runs_per_month: float
    runs_per_year: float
    shortest_gap_seconds: int | None
    longest_gap_seconds: int | None
    sample_size: int
    sample_truncated: bool
    flags: tuple[str, ...]


def compute_stats(
    schedule: CronSchedule,
    zone: tzinfo,
    reference: datetime,
) -> ScheduleStats:
    """Describe how often a schedule fires, and how evenly."""
    anchor = reference.astimezone(zone)
    start = anchor.date()

    if schedule.never_runs(start):
        return ScheduleStats(
            never_runs=True,
            reason=schedule.impossible_reason(),
            times_per_matching_day=schedule.times_per_day,
            matching_days_per_year=0.0,
            runs_per_hour=0.0,
            runs_per_day=0.0,
            runs_per_week=0.0,
            runs_per_month=0.0,
            runs_per_year=0.0,
            shortest_gap_seconds=None,
            longest_gap_seconds=None,
            sample_size=0,
            sample_truncated=False,
            flags=("never-runs",),
        )

    horizon = start + timedelta(days=_SAMPLE_DAYS)
    matching_days = 0
    for day in schedule.iter_dates(start, forward=True):
        if day >= horizon:
            break
        matching_days += 1
    days_per_year = matching_days / _SAMPLE_DAYS * _DAYS_PER_YEAR

    per_day = schedule.times_per_day
    runs_per_year = days_per_year * per_day
    runs_per_day = runs_per_year / _DAYS_PER_YEAR

    gap_end = anchor + timedelta(days=_GAP_WINDOW_DAYS)
    instants: list[datetime] = []
    truncated = False
    for moment in schedule.iter_naive(anchor.replace(tzinfo=None), forward=True):
        status, _ = classify_local(moment, zone)
        if status == "nonexistent":
            continue
        aware = moment.replace(tzinfo=zone, fold=0)
        if aware > gap_end:
            break
        instants.append(aware)
        if len(instants) >= _GAP_SAMPLE_LIMIT:
            truncated = True
            break

    shortest: int | None = None
    longest: int | None = None
    for earlier, later in pairwise(instants):
        delta = int((later - earlier).total_seconds())
        shortest = delta if shortest is None else min(shortest, delta)
        longest = delta if longest is None else max(longest, delta)

    flags: list[str] = []
    if per_day >= 86400 and days_per_year > 364:
        flags.append("fires-every-second")
    elif per_day >= 1440 and days_per_year > 364:
        flags.append("fires-every-minute")
    # A yearly schedule averages 0.99998 runs per year over a leap cycle, which
    # is not what "less than once a year" is meant to catch.
    if runs_per_year < 0.99:
        flags.append("fires-less-than-once-a-year")
    if runs_per_day > 60 and "fires-every-minute" not in flags:
        flags.append("high-frequency")

    return ScheduleStats(
        never_runs=False,
        reason=None,
        times_per_matching_day=per_day,
        matching_days_per_year=days_per_year,
        runs_per_hour=runs_per_day / 24,
        runs_per_day=runs_per_day,
        runs_per_week=runs_per_year / (_DAYS_PER_YEAR / 7),
        runs_per_month=runs_per_year / 12,
        runs_per_year=runs_per_year,
        shortest_gap_seconds=shortest,
        longest_gap_seconds=longest,
        sample_size=len(instants),
        sample_truncated=truncated,
        flags=tuple(flags),
    )


# --------------------------------------------------------------------------
# Comparing two schedules
# --------------------------------------------------------------------------


def first_common_run(
    left: CronSchedule,
    right: CronSchedule,
    zone: tzinfo,
    after: datetime,
    max_steps: int = 20_000,
) -> datetime | None:
    """Walk both schedules together until they land on the same instant."""
    naive_anchor = after.astimezone(zone).replace(tzinfo=None)
    left_iter = left.iter_naive(naive_anchor, forward=True)
    right_iter = right.iter_naive(naive_anchor, forward=True)
    left_value = next(left_iter, None)
    right_value = next(right_iter, None)
    steps = 0

    while left_value is not None and right_value is not None and steps < max_steps:
        steps += 1
        if left_value == right_value:
            status, _ = classify_local(left_value, zone)
            if status == "nonexistent":
                left_value = next(left_iter, None)
                right_value = next(right_iter, None)
                continue
            return left_value.replace(tzinfo=zone, fold=0)
        if left_value < right_value:
            left_value = next(left_iter, None)
        else:
            right_value = next(right_iter, None)
    return None


def is_subset(
    inner: CronSchedule,
    outer: CronSchedule,
    zone: tzinfo,
    after: datetime,
    sample: int = 60,
) -> bool:
    """True when every sampled run of ``inner`` is also a run of ``outer``."""
    naive_anchor = after.astimezone(zone).replace(tzinfo=None)
    seen = 0
    for moment in inner.iter_naive(naive_anchor, forward=True):
        if not outer.matches(moment):
            return False
        seen += 1
        if seen >= sample:
            break
    return seen > 0


def parse_instant(text: str | None, zone: tzinfo, label: str) -> datetime:
    """Read an ISO 8601 timestamp, defaulting to now in ``zone``."""
    if text is None or (isinstance(text, str) and not text.strip()):
        return datetime.now(tz=zone)
    if not isinstance(text, str):
        raise CronError(f"{label} must be an ISO 8601 string.", field=label)
    candidate = text.strip()
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        raise CronError(
            f'{label} is not a valid ISO 8601 timestamp: "{text}".',
            field=label,
            expected='a timestamp such as "2025-03-09T01:30:00" or "2025-03-09T06:30:00Z"',
            suggestion="Leave it out to use the current time.",
        ) from None
    if moment.tzinfo is None:
        status, _ = classify_local(moment, zone)
        if status == "nonexistent":
            raise CronError(
                f"{label} ({candidate}) does not exist in this timezone, "
                "because the clocks jump over it.",
                field=label,
                expected="a local time that exists in the given timezone",
                suggestion="Give the time with an explicit UTC offset instead.",
            )
        return moment.replace(tzinfo=zone)
    return moment
