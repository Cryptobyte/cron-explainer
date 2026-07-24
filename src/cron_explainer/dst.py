"""Daylight saving analysis.

Two things go wrong when a cron job lives in a zone that observes DST:

* **Spring forward.** The clocks jump, say, from 02:00 to 03:00. A job scheduled
  for 02:30 has no 02:30 to run at that day. It is silently skipped.
* **Fall back.** The clocks repeat, say, 01:00 to 02:00. A job scheduled for
  01:30 has two 01:30s. Many schedulers run it twice.

Transitions are found by walking UTC a day at a time watching the offset, then
bisecting to the exact second. That works for every zone in the IANA database,
including the ones with 30 and 45 minute shifts, without hardcoding any rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from cron_explainer.schedule import CronSchedule, classify_local

_DAY: int = 86_400


@dataclass(frozen=True)
class Transition:
    """One offset change in a zone."""

    instant_utc: datetime
    kind: str  # "forward" (clocks jump ahead) or "backward" (clocks repeat)
    offset_before: timedelta
    offset_after: timedelta
    window_start: datetime  # naive local, inclusive
    window_end: datetime  # naive local, exclusive

    @property
    def shift(self) -> timedelta:
        return self.offset_after - self.offset_before

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "instant_utc": self.instant_utc.isoformat().replace("+00:00", "Z"),
            "offset_before": _offset_text(self.offset_before),
            "offset_after": _offset_text(self.offset_after),
            "shift_minutes": int(self.shift.total_seconds() // 60),
            "affected_local_window": {
                "start": self.window_start.isoformat(),
                "end": self.window_end.isoformat(),
                "note": (
                    "These wall clock times never happen."
                    if self.kind == "forward"
                    else "These wall clock times happen twice."
                ),
            },
        }


def _offset_text(offset: timedelta) -> str:
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def _offset_at(stamp: float, zone: tzinfo) -> timedelta:
    moment = datetime.fromtimestamp(stamp, tz=UTC).astimezone(zone)
    return moment.utcoffset() or timedelta(0)


def find_transitions(zone: tzinfo, year: int) -> list[Transition]:
    """Every offset change whose local date falls in ``year``."""
    scan_start = datetime(year - 1, 12, 30, tzinfo=UTC).timestamp()
    scan_end = datetime(year + 1, 1, 2, tzinfo=UTC).timestamp()

    transitions: list[Transition] = []
    previous_stamp = scan_start
    previous_offset = _offset_at(previous_stamp, zone)

    stamp = scan_start + _DAY
    while stamp <= scan_end:
        offset = _offset_at(stamp, zone)
        if offset != previous_offset:
            exact = _bisect(previous_stamp, stamp, previous_offset, zone)
            transition = _build(exact, previous_offset, offset, zone)
            if transition.window_start.year == year:
                transitions.append(transition)
            previous_offset = offset
        previous_stamp = stamp
        stamp += _DAY

    return transitions


def _bisect(low: float, high: float, offset_before: timedelta, zone: tzinfo) -> float:
    """First second at which the offset differs from ``offset_before``."""
    low_i, high_i = int(low), int(high)
    while low_i + 1 < high_i:
        middle = (low_i + high_i) // 2
        if _offset_at(middle, zone) == offset_before:
            low_i = middle
        else:
            high_i = middle
    return float(high_i)


def _build(
    stamp: float, offset_before: timedelta, offset_after: timedelta, zone: tzinfo
) -> Transition:
    instant = datetime.fromtimestamp(stamp, tz=UTC)
    local_before = (instant + offset_before).replace(tzinfo=None)
    local_after = (instant + offset_after).replace(tzinfo=None)
    if offset_after > offset_before:
        # Clocks jump ahead: [local_before, local_after) never happens.
        return Transition(
            instant_utc=instant,
            kind="forward",
            offset_before=offset_before,
            offset_after=offset_after,
            window_start=local_before,
            window_end=local_after,
        )
    # Clocks go back: [local_after, local_before) happens twice.
    return Transition(
        instant_utc=instant,
        kind="backward",
        offset_before=offset_before,
        offset_after=offset_after,
        window_start=local_after,
        window_end=local_before,
    )


@dataclass(frozen=True)
class DstEvent:
    """One scheduled firing caught by a transition."""

    local: datetime  # naive wall clock time the crontab asks for
    kind: str  # "skipped" or "repeated"
    instants_utc: tuple[datetime, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "local_time": self.local.isoformat(),
            "kind": self.kind,
            "utc_instants": [
                moment.isoformat().replace("+00:00", "Z") for moment in self.instants_utc
            ],
            "explanation": (
                "This local time does not exist on this date, so the job never runs."
                if self.kind == "skipped"
                else "This local time happens twice on this date, so the job may run twice."
            ),
        }


@dataclass(frozen=True)
class DstReport:
    """The full answer for one expression, one zone and one year."""

    timezone: str
    year: int
    observes_dst: bool
    transitions: tuple[Transition, ...]
    skipped: tuple[DstEvent, ...]
    repeated: tuple[DstEvent, ...]
    summary: str
    recommendation: str

    @property
    def safe(self) -> bool:
        return not self.skipped and not self.repeated

    def to_dict(self) -> dict[str, Any]:
        return {
            "timezone": self.timezone,
            "year": self.year,
            "observes_dst": self.observes_dst,
            "safe": self.safe,
            "summary": self.summary,
            "recommendation": self.recommendation,
            "transitions": [item.to_dict() for item in self.transitions],
            "skipped_runs": [item.to_dict() for item in self.skipped],
            "repeated_runs": [item.to_dict() for item in self.repeated],
        }


_UTC_NOTE = (
    "UTC and other fixed offset zones never change offset, so a schedule "
    "expressed in UTC can never be skipped or repeated. Running a job in UTC is "
    "the simplest way to make this class of bug impossible."
)


def analyze_dst(
    schedule: CronSchedule,
    zone: tzinfo,
    zone_name: str,
    year: int,
) -> DstReport:
    """Report every run this schedule loses or duplicates across DST in ``year``."""
    transitions = tuple(find_transitions(zone, year))

    if not transitions:
        return DstReport(
            timezone=zone_name,
            year=year,
            observes_dst=False,
            transitions=(),
            skipped=(),
            repeated=(),
            summary=(
                f"{zone_name} has no clock changes in {year}, so this schedule "
                "cannot be skipped or repeated."
            ),
            recommendation=_UTC_NOTE,
        )

    skipped: list[DstEvent] = []
    repeated: list[DstEvent] = []

    for transition in transitions:
        for moment in schedule.iter_naive_between(transition.window_start, transition.window_end):
            if transition.kind == "forward":
                skipped.append(DstEvent(local=moment, kind="skipped", instants_utc=()))
            else:
                first = moment.replace(tzinfo=zone, fold=0).astimezone(UTC)
                second = moment.replace(tzinfo=zone, fold=1).astimezone(UTC)
                repeated.append(
                    DstEvent(local=moment, kind="repeated", instants_utc=(first, second))
                )

    summary = _summarize(zone_name, year, skipped, repeated)
    recommendation = _recommend(schedule, zone, transitions, skipped, repeated)
    return DstReport(
        timezone=zone_name,
        year=year,
        observes_dst=True,
        transitions=transitions,
        skipped=tuple(skipped),
        repeated=tuple(repeated),
        summary=summary,
        recommendation=recommendation,
    )


def _summarize(zone_name: str, year: int, skipped: list[DstEvent], repeated: list[DstEvent]) -> str:
    if not skipped and not repeated:
        return (
            f"Safe. In {year} this schedule never lands inside a clock change in "
            f"{zone_name}, so no run is lost and none is duplicated."
        )
    pieces: list[str] = []
    if skipped:
        times = ", ".join(item.local.isoformat(sep=" ") for item in skipped[:5])
        subject = "1 run is" if len(skipped) == 1 else f"{len(skipped)} runs are"
        pieces.append(f"{subject} skipped when the clocks go forward ({times})")
    if repeated:
        times = ", ".join(item.local.isoformat(sep=" ") for item in repeated[:5])
        subject = "1 run happens" if len(repeated) == 1 else f"{len(repeated)} runs happen"
        pieces.append(f"{subject} twice when the clocks go back ({times})")
    return f"In {zone_name} during {year}, " + " and ".join(pieces) + "."


def _recommend(
    schedule: CronSchedule,
    zone: tzinfo,
    transitions: tuple[Transition, ...],
    skipped: list[DstEvent],
    repeated: list[DstEvent],
) -> str:
    if not skipped and not repeated:
        return (
            "No change needed. If you want this guaranteed for every future year, "
            "run the job in UTC. " + _UTC_NOTE
        )
    advice: list[str] = []
    if skipped:
        safe_hour = _suggest_safe_hour(schedule, zone, transitions)
        advice.append(
            "The skipped runs never happen at all. Move the job outside the "
            f"transition window{f' (for example to {safe_hour} local time)' if safe_hour else ''}, "
            "or schedule it in UTC."
        )
    if repeated:
        advice.append(
            "The repeated runs may fire twice. Make the job idempotent, add a lock "
            "or a run marker, or move it outside the transition window."
        )
    advice.append(_UTC_NOTE)
    return " ".join(advice)


def _suggest_safe_hour(
    schedule: CronSchedule, zone: tzinfo, transitions: tuple[Transition, ...]
) -> str | None:
    """Offer a nearby hour that no transition window touches."""
    windows = [(item.window_start, item.window_end) for item in transitions]
    minutes = sorted(schedule.minutes)[0] if schedule.minutes else 0
    for hour in (5, 6, 4, 7, 8, 3, 9, 10, 11, 12):
        candidate_ok = True
        for start, end in windows:
            probe = start.replace(hour=hour, minute=minutes, second=0)
            if start <= probe < end:
                candidate_ok = False
                break
            status, _ = classify_local(probe, zone)
            if status != "normal":
                candidate_ok = False
                break
        if candidate_ok:
            return f"{hour:02d}:{minutes:02d}"
    return None  # pragma: no cover - no zone shifts that much
