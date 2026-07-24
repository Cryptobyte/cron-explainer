"""Cron expression -> parsed fields.

Three dialects are supported, selected explicitly or detected from the field
count:

===========  ========  ==========================================================
dialect      fields    layout
===========  ========  ==========================================================
``posix``    5         minute hour day-of-month month day-of-week
``quartz``   6 or 7    second minute hour day-of-month month day-of-week [year]
``aws``      6         minute hour day-of-month month day-of-week year
===========  ========  ==========================================================

Day-of-week numbering differs between them: POSIX counts Sunday as 0 (and
accepts 7 as a second spelling of Sunday), while Quartz and AWS EventBridge
count Sunday as 1. Everything is normalised on the way in to a single canonical
scale, 0 = Sunday through 6 = Saturday, so the schedule engine never has to care
which dialect produced it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Final

from cron_explainer.errors import CronError

# --------------------------------------------------------------------------
# Names and constants
# --------------------------------------------------------------------------

# fmt: off
MONTH_NAMES: Final[tuple[str, ...]] = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)
MONTH_FULL: Final[tuple[str, ...]] = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
DOW_NAMES: Final[tuple[str, ...]] = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")
DOW_FULL: Final[tuple[str, ...]] = (
    "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
)
# fmt: on

DIALECT_NAMES: Final[tuple[str, ...]] = ("posix", "quartz", "aws")

#: Macro -> equivalent five field POSIX expression. ``@reboot`` has no schedule.
MACROS: Final[Mapping[str, str | None]] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
    "@reboot": None,
}

MACRO_MEANINGS: Final[Mapping[str, str]] = {
    "@yearly": "Once a year, at midnight on the 1st of January.",
    "@annually": "Once a year, at midnight on the 1st of January. Same as @yearly.",
    "@monthly": "Once a month, at midnight on the 1st.",
    "@weekly": "Once a week, at midnight on Sunday.",
    "@daily": "Once a day, at midnight.",
    "@midnight": "Once a day, at midnight. Same as @daily.",
    "@hourly": "Once an hour, at the top of the hour.",
    "@reboot": (
        "Once, when the scheduler itself starts. It is not a clock schedule at all, "
        "so it has no next run time that can be computed."
    ),
}


# --------------------------------------------------------------------------
# Field definitions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldDef:
    """Static description of one field position within a dialect."""

    key: str
    label: str
    lo: int
    hi: int
    names: Mapping[str, int] = dc_field(default_factory=dict)
    kind: str = "plain"  # "plain" | "dom" | "dow"
    allow_question: bool = False
    allow_last: bool = False
    allow_nearest_weekday: bool = False
    allow_nth: bool = False

    def syntax_hint(self) -> str:
        """What this field accepts, phrased for an error message."""
        bits = [f"{self.lo}-{self.hi}"]
        if self.names:
            if self.kind == "dow":
                bits.append("SUN-SAT")
            else:
                bits.append("JAN-DEC")
        bits.append("or the operators * , - /")
        extras = []
        if self.allow_question:
            extras.append("?")
        if self.allow_last:
            extras.append("L")
        if self.allow_nearest_weekday:
            extras.append("W")
        if self.allow_nth:
            extras.append("#")
        if extras:
            bits.append("plus " + " ".join(extras))
        return ", ".join(bits)


def _month_names() -> dict[str, int]:
    return {name: index + 1 for index, name in enumerate(MONTH_NAMES)}


def _dow_names(sunday: int) -> dict[str, int]:
    return {name: index + sunday for index, name in enumerate(DOW_NAMES)}


_SECOND = FieldDef("second", "second", 0, 59)
_MINUTE = FieldDef("minute", "minute", 0, 59)
_HOUR = FieldDef("hour", "hour", 0, 23)
_MONTH = FieldDef("month", "month", 1, 12, names=_month_names())

_POSIX_DOM = FieldDef("dom", "day-of-month", 1, 31, kind="dom")
_POSIX_DOW = FieldDef("dow", "day-of-week", 0, 7, names=_dow_names(0), kind="dow")

_EXT_DOM = FieldDef(
    "dom",
    "day-of-month",
    1,
    31,
    kind="dom",
    allow_question=True,
    allow_last=True,
    allow_nearest_weekday=True,
)
_EXT_DOW = FieldDef(
    "dow",
    "day-of-week",
    1,
    7,
    names=_dow_names(1),
    kind="dow",
    allow_question=True,
    allow_last=True,
    allow_nth=True,
)

_QUARTZ_YEAR = FieldDef("year", "year", 1970, 2099)
_AWS_YEAR = FieldDef("year", "year", 1970, 2199)


@dataclass(frozen=True)
class DialectSpec:
    """A dialect: its field layout and its cross-field rules."""

    name: str
    layouts: Mapping[int, tuple[FieldDef, ...]]
    sunday_is: int
    require_question: bool
    description: str


POSIX = DialectSpec(
    name="posix",
    layouts={5: (_MINUTE, _HOUR, _POSIX_DOM, _MONTH, _POSIX_DOW)},
    sunday_is=0,
    require_question=False,
    description="POSIX / Vixie cron, 5 fields, Sunday is 0 (7 also accepted)",
)

QUARTZ = DialectSpec(
    name="quartz",
    layouts={
        6: (_SECOND, _MINUTE, _HOUR, _EXT_DOM, _MONTH, _EXT_DOW),
        7: (_SECOND, _MINUTE, _HOUR, _EXT_DOM, _MONTH, _EXT_DOW, _QUARTZ_YEAR),
    },
    sunday_is=1,
    require_question=True,
    description="Quartz, 6 or 7 fields with seconds first and an optional year, Sunday is 1",
)

AWS = DialectSpec(
    name="aws",
    layouts={6: (_MINUTE, _HOUR, _EXT_DOM, _MONTH, _EXT_DOW, _AWS_YEAR)},
    sunday_is=1,
    require_question=True,
    description="AWS EventBridge, 6 fields ending in a year, Sunday is 1",
)

DIALECTS: Final[Mapping[str, DialectSpec]] = {"posix": POSIX, "quartz": QUARTZ, "aws": AWS}


# --------------------------------------------------------------------------
# Parsed representation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DomExtras:
    """Day-of-month constructs that depend on the month being evaluated."""

    last_offsets: frozenset[int] = frozenset()  # 0 means L, n means "L-n"
    last_weekday: bool = False  # LW
    nearest_weekday: frozenset[int] = frozenset()  # nW

    def __bool__(self) -> bool:
        return bool(self.last_offsets or self.last_weekday or self.nearest_weekday)


@dataclass(frozen=True)
class DowExtras:
    """Day-of-week constructs that depend on the month being evaluated."""

    last: frozenset[int] = frozenset()  # canonical dow, "last <dow> of the month"
    nth: frozenset[tuple[int, int]] = frozenset()  # (canonical dow, nth occurrence)

    def __bool__(self) -> bool:
        return bool(self.last or self.nth)


@dataclass(frozen=True)
class ParsedField:
    """One field after expansion.

    ``values`` is always on the canonical scale: for day-of-week that is
    0 = Sunday through 6 = Saturday, whatever the source dialect wrote.
    """

    key: str
    label: str
    raw: str
    position: int
    lo: int
    hi: int
    values: frozenset[int]
    star: bool
    question: bool = False
    dom_extras: DomExtras = DomExtras()
    dow_extras: DowExtras = DowExtras()

    @property
    def restricted(self) -> bool:
        """True when the field narrows the schedule at all."""
        return not self.star

    @property
    def covers_all(self) -> bool:
        return len(self.values) == (self.hi - self.lo + 1)


@dataclass(frozen=True)
class ParsedExpression:
    """A fully validated expression."""

    expression: str
    normalized: str
    dialect: str
    fields: tuple[ParsedField, ...]
    macro: str | None = None
    notes: tuple[str, ...] = ()

    def field(self, key: str) -> ParsedField:
        for item in self.fields:
            if item.key == key:
                return item
        raise KeyError(key)

    def has(self, key: str) -> bool:
        return any(item.key == key for item in self.fields)

    @property
    def has_seconds(self) -> bool:
        return self.has("second")

    @property
    def has_year(self) -> bool:
        return self.has("year")

    @property
    def day_rule(self) -> str:
        """``"and"`` or ``"or"``: how day-of-month and day-of-week combine.

        This follows Vixie cron, which is the behaviour every POSIX crontab
        inherits: if either day field is a plain ``*`` (or begins with ``*``,
        or is ``?``), the two are ANDed. Otherwise they are ORed, which is the
        rule that surprises people.
        """
        dom = self.field("dom")
        dow = self.field("dow")
        return "and" if (dom.star or dow.star) else "or"


# --------------------------------------------------------------------------
# Tokenising
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\S+")
_INT_RE = re.compile(r"^\d+$")


def _tokens(text: str) -> list[tuple[str, int]]:
    return [(match.group(0), match.start()) for match in _TOKEN_RE.finditer(text)]


def detect_dialect(expression: str) -> tuple[str, str | None]:
    """Guess a dialect from the shape of ``expression``.

    Returns the dialect name and an optional note explaining an ambiguous call.
    Six field expressions are ambiguous between Quartz (seconds first) and AWS
    (year last), so the final field decides: a four digit year can only be AWS.
    """
    stripped = expression.strip()
    if stripped.startswith("@"):
        return "posix", None
    count = len(_tokens(stripped))
    if count == 5:
        return "posix", None
    if count == 7:
        return "quartz", None
    if count == 6:
        parts = [token for token, _ in _tokens(stripped)]
        if _looks_like_year(parts[-1]):
            return "aws", (
                "6 fields with a year in the last position, read as AWS EventBridge. "
                'Pass dialect="quartz" if the first field was meant to be seconds.'
            )
        # "?" only ever sits in a day field, and the day fields are in different
        # places in the two 6 field layouts, so it settles the question.
        aws_day_positions = any(parts[index] == "?" for index in (2, 4))
        quartz_day_positions = any(parts[index] == "?" for index in (3, 5))
        if aws_day_positions and not quartz_day_positions:
            return "aws", ('6 fields with "?" in an AWS day position, read as AWS EventBridge.')
        if quartz_day_positions and not aws_day_positions:
            return "quartz", None
        return "quartz", (
            "6 fields read as Quartz, so the first field is seconds. "
            'Pass dialect="aws" if the last field was meant to be a year.'
        )
    return "posix", None


def _looks_like_year(token: str) -> bool:
    for chunk in re.split(r"[,\-/]", token):
        if re.fullmatch(r"\d{4}", chunk) and 1970 <= int(chunk) <= 2199:
            return True
    return False


# --------------------------------------------------------------------------
# Field parsing
# --------------------------------------------------------------------------


class _Accumulator:
    """Mutable scratch space while a single field is being expanded."""

    def __init__(self) -> None:
        self.values: set[int] = set()
        self.last_offsets: set[int] = set()
        self.last_weekday = False
        self.nearest_weekday: set[int] = set()
        self.dow_last: set[int] = set()
        self.dow_nth: set[tuple[int, int]] = set()


def _canonical_dow(value: int, spec: DialectSpec) -> int:
    """Map a dialect day-of-week number onto 0 = Sunday .. 6 = Saturday."""
    if spec.sunday_is == 0:
        return value % 7
    return value - 1


def _suggest_for_value(fdef: FieldDef, value: int) -> str | None:
    if fdef.key == "minute" and value == 60:
        return 'Minutes run 0-59. The top of the hour is "0".'
    if fdef.key == "hour" and value == 24:
        return 'Hours run 0-23. Midnight is "0".'
    if fdef.key == "dow" and value in (8, 9):
        return f'Days of the week run {fdef.lo}-{fdef.hi}. Sunday is "{fdef.lo}".'
    if fdef.key == "month" and value == 0:
        return 'Months run 1-12. January is "1" or "JAN".'
    if fdef.key == "dom" and value == 0:
        return 'Days of the month run 1-31. The first is "1".'
    return None


def _parse_number(
    fdef: FieldDef,
    token: str,
    position: int,
    expression: str,
) -> int:
    """Resolve one bare value, numeric or three letter name, with bounds checks."""
    upper = token.upper()
    if fdef.names and upper in fdef.names:
        return fdef.names[upper]
    if _INT_RE.match(token):
        value = int(token)
        if value < fdef.lo or value > fdef.hi:
            raise CronError(
                f'Value "{token}" is out of range for the {fdef.label} field.',
                field=fdef.label,
                position=position,
                expected=f"{fdef.lo}-{fdef.hi}",
                suggestion=_suggest_for_value(fdef, value),
                expression=expression,
            )
        return value
    if fdef.names:
        # "SUNDAY" or "sept": accept the abbreviation the user was reaching for.
        prefix = upper[:3]
        if prefix in fdef.names and len(upper) > 3:
            raise CronError(
                f'Unrecognized name "{token}" in the {fdef.label} field.',
                field=fdef.label,
                position=position,
                expected=fdef.syntax_hint(),
                suggestion=f'Use the three letter abbreviation "{prefix}".',
                expression=expression,
            )
    raise CronError(
        f'Unrecognized value "{token}" in the {fdef.label} field.',
        field=fdef.label,
        position=position,
        expected=fdef.syntax_hint(),
        suggestion=None,
        expression=expression,
    )


def _parse_step(
    fdef: FieldDef,
    token: str,
    position: int,
    expression: str,
) -> int:
    if not _INT_RE.match(token):
        raise CronError(
            f'Step "{token}" in the {fdef.label} field is not a number.',
            field=fdef.label,
            position=position,
            expected="a positive whole number after /",
            expression=expression,
        )
    step = int(token)
    if step == 0:
        raise CronError(
            f"A step of 0 in the {fdef.label} field would never advance.",
            field=fdef.label,
            position=position,
            expected="a step of 1 or more",
            suggestion='Use "1" to mean every value.',
            expression=expression,
        )
    span = fdef.hi - fdef.lo + 1
    if step > span:
        raise CronError(
            f'Step "{step}" is larger than the {fdef.label} field range, '
            "so only the first value would ever match.",
            field=fdef.label,
            position=position,
            expected=f"a step of 1-{span}",
            expression=expression,
        )
    return step


def _split_step(
    fdef: FieldDef,
    item: str,
    position: int,
    expression: str,
) -> tuple[str, int | None]:
    if "/" not in item:
        return item, None
    head, _, tail = item.partition("/")
    if "/" in tail:
        raise CronError(
            f'"{item}" in the {fdef.label} field has more than one step.',
            field=fdef.label,
            position=position,
            expected="at most one / per item",
            expression=expression,
        )
    step = _parse_step(fdef, tail, position + len(head) + 1, expression)
    return head, step


def _apply_range(
    acc: _Accumulator,
    fdef: FieldDef,
    spec: DialectSpec,
    start: int,
    end: int,
    step: int,
) -> None:
    for value in range(start, end + 1, step):
        acc.values.add(_canonical_dow(value, spec) if fdef.kind == "dow" else value)


def _parse_item(
    acc: _Accumulator,
    fdef: FieldDef,
    spec: DialectSpec,
    item: str,
    position: int,
    expression: str,
) -> bool:
    """Expand a single comma separated item. Returns True if it was a ``*`` form."""
    if not item:
        raise CronError(
            f"Empty value in the {fdef.label} field.",
            field=fdef.label,
            position=position,
            expected=fdef.syntax_hint(),
            suggestion="Remove the stray comma.",
            expression=expression,
        )

    upper = item.upper()

    # ---- day-of-month specials -------------------------------------------
    if fdef.kind == "dom" and fdef.allow_last:
        if upper == "L":
            acc.last_offsets.add(0)
            return False
        if upper == "LW":
            acc.last_weekday = True
            return False
        match = re.fullmatch(r"L-(\d+)", upper)
        if match:
            offset = int(match.group(1))
            if offset > 30:
                raise CronError(
                    f'"{item}" would step past the start of every month.',
                    field=fdef.label,
                    position=position,
                    expected="L-0 through L-30",
                    expression=expression,
                )
            acc.last_offsets.add(offset)
            return False
    if fdef.kind == "dom" and fdef.allow_nearest_weekday:
        match = re.fullmatch(r"(\d+)W", upper)
        if match:
            day = _parse_number(fdef, match.group(1), position, expression)
            acc.nearest_weekday.add(day)
            return False

    # ---- day-of-week specials --------------------------------------------
    if fdef.kind == "dow" and fdef.allow_last:
        if upper == "L":
            # Quartz: a bare L in day-of-week means Saturday, the last day of the week.
            acc.values.add(6)
            return False
        match = re.fullmatch(r"([A-Z0-9]+)L", upper)
        if match:
            value = _parse_number(fdef, match.group(1), position, expression)
            acc.dow_last.add(_canonical_dow(value, spec))
            return False
    if fdef.kind == "dow" and fdef.allow_nth and "#" in upper:
        head, _, tail = upper.partition("#")
        value = _parse_number(fdef, head, position, expression)
        if not _INT_RE.match(tail) or not 1 <= int(tail) <= 5:
            raise CronError(
                f'"{item}" must select the 1st through 5th occurrence of a weekday.',
                field=fdef.label,
                position=position + len(head) + 1,
                expected="#1 through #5",
                suggestion=f'Write "{head}#1" for the first {head} of the month.',
                expression=expression,
            )
        acc.dow_nth.add((_canonical_dow(value, spec), int(tail)))
        return False

    # ---- ordinary forms ---------------------------------------------------
    try:
        return _parse_plain_item(acc, fdef, spec, item, position, expression)
    except CronError:
        # Only now, once the ordinary grammar has failed, is it safe to blame
        # L / W / #. Blaming them earlier would reject the name "WED".
        if fdef.kind in ("dom", "dow") and _bad_special(upper, fdef):
            raise CronError(
                f'"{item}" is not valid in the {fdef.label} field for {spec.name} cron.',
                field=fdef.label,
                position=position,
                expected=fdef.syntax_hint(),
                suggestion=(
                    "L, W and # are Quartz and AWS extensions. "
                    'Pass dialect="quartz" if that is what you meant.'
                ),
                expression=expression,
            ) from None
        raise


def _parse_plain_item(
    acc: _Accumulator,
    fdef: FieldDef,
    spec: DialectSpec,
    item: str,
    position: int,
    expression: str,
) -> bool:
    """Expand ``*``, a value, a list member, a range, or any of those with a step."""
    head, step = _split_step(fdef, item, position, expression)

    if head == "*":
        _apply_range(acc, fdef, spec, fdef.lo, fdef.hi, step or 1)
        return True

    if "-" in head and not head.startswith("-"):
        low_text, _, high_text = head.partition("-")
        start = _parse_number(fdef, low_text, position, expression)
        end = _parse_number(fdef, high_text, position + len(low_text) + 1, expression)
        if start > end:
            raise CronError(
                f'Range "{head}" runs backwards in the {fdef.label} field.',
                field=fdef.label,
                position=position,
                expected="a low value, then a high value",
                suggestion=_reversed_range_suggestion(fdef, low_text, high_text, start, end),
                expression=expression,
            )
        _apply_range(acc, fdef, spec, start, end, step or 1)
        return False

    value = _parse_number(fdef, head, position, expression)
    if step is not None:
        # The common "0/15" extension: from this value to the top of the range.
        _apply_range(acc, fdef, spec, value, fdef.hi, step)
    else:
        acc.values.add(_canonical_dow(value, spec) if fdef.kind == "dow" else value)
    return False


_EXTENSION_RE = re.compile(r"L|LW|L-\d+|\d+W|[A-Z]{3}L|\d+L|[A-Z]{3}#\d+|\d+#\d+")


def _bad_special(upper: str, fdef: FieldDef) -> bool:
    """True when an item is clearly an L/W/# construct the field does not support.

    Deliberately narrow: a mistyped weekday such as "WEDS" must keep its own
    error message rather than being blamed on the W operator.
    """
    if not _EXTENSION_RE.fullmatch(upper):
        return False
    if "#" in upper and not fdef.allow_nth:
        return True
    if upper.endswith("W") and not fdef.allow_nearest_weekday:
        return True
    return "L" in upper and not fdef.allow_last


def _reversed_range_suggestion(
    fdef: FieldDef, low_text: str, high_text: str, start: int, end: int
) -> str:
    swapped = f"{high_text}-{low_text}"
    if fdef.kind == "dow" or fdef.kind == "month":
        wrap = f"{low_text}-{fdef.hi},{fdef.lo}-{high_text}"
        return (
            f'Write "{swapped}" for {end} through {start}, '
            f'or "{wrap}" to wrap around the end of the range.'
        )
    return f'Write "{swapped}".'


def _parse_field(
    fdef: FieldDef,
    spec: DialectSpec,
    raw: str,
    position: int,
    expression: str,
) -> ParsedField:
    # Day-of-week values are stored on the canonical 0 = Sunday .. 6 = Saturday
    # scale, so the bounds recorded here are the canonical ones rather than the
    # dialect's (POSIX writes 0-7, with 7 a second spelling of Sunday).
    lo, hi = (0, 6) if fdef.kind == "dow" else (fdef.lo, fdef.hi)

    if raw == "?":
        if not fdef.allow_question:
            raise CronError(
                f'"?" is not valid in the {fdef.label} field for {spec.name} cron.',
                field=fdef.label,
                position=position,
                expected=fdef.syntax_hint(),
                suggestion='POSIX cron has no "?". Use "*" instead.',
                expression=expression,
            )
        return ParsedField(
            key=fdef.key,
            label=fdef.label,
            raw=raw,
            position=position,
            lo=lo,
            hi=hi,
            values=frozenset(range(lo, hi + 1)),
            star=True,
            question=True,
        )

    acc = _Accumulator()
    offset = 0
    for chunk in raw.split(","):
        _parse_item(acc, fdef, spec, chunk.strip(), position + offset, expression)
        offset += len(chunk) + 1

    if fdef.kind == "dow" and spec.sunday_is == 0 and 7 in acc.values:
        acc.values.discard(7)
        acc.values.add(0)

    if not acc.values and not (
        acc.last_offsets or acc.last_weekday or acc.nearest_weekday or acc.dow_last or acc.dow_nth
    ):
        raise CronError(
            f"The {fdef.label} field matches nothing.",
            field=fdef.label,
            position=position,
            expected=fdef.syntax_hint(),
            expression=expression,
        )

    # Vixie sets its "star" flag from the first character of the field, so
    # "*/2" counts as unrestricted for the day-of-month / day-of-week rule.
    star = raw.startswith("*")

    return ParsedField(
        key=fdef.key,
        label=fdef.label,
        raw=raw,
        position=position,
        lo=lo,
        hi=hi,
        values=frozenset(acc.values),
        star=star,
        question=False,
        dom_extras=DomExtras(
            last_offsets=frozenset(acc.last_offsets),
            last_weekday=acc.last_weekday,
            nearest_weekday=frozenset(acc.nearest_weekday),
        ),
        dow_extras=DowExtras(last=frozenset(acc.dow_last), nth=frozenset(acc.dow_nth)),
    )


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def _field_count_error(expression: str, count: int, spec: DialectSpec, explicit: bool) -> CronError:
    accepted = sorted(spec.layouts)
    shapes = " or ".join(str(number) for number in accepted)
    layout = " ".join(item.label for item in spec.layouts[accepted[0]])
    hint = ""
    if not explicit:
        hint = (
            ' Pass dialect="quartz" for a seconds field, '
            'or dialect="aws" for an EventBridge rule with a year.'
        )
    return CronError(
        f"Expected {shapes} fields for {spec.name} cron but found {count}.",
        field=None,
        position=len(expression.rstrip()) if count < accepted[0] else 0,
        expected=f"{shapes} fields: {layout}",
        suggestion=(
            f"A {accepted[0]} field {spec.name} expression looks like "
            f'"{_EXAMPLE_BY_DIALECT[spec.name]}".{hint}'
        ),
        expression=expression,
    )


_EXAMPLE_BY_DIALECT: Final[Mapping[str, str]] = {
    "posix": "30 2 * * 1-5",
    "quartz": "0 30 2 ? * MON-FRI",
    "aws": "30 2 ? * MON-FRI *",
}


def _check_day_fields(
    spec: DialectSpec, dom: ParsedField, dow: ParsedField, expression: str
) -> None:
    """Quartz and AWS both insist that exactly one day field is ``?``."""
    if not spec.require_question:
        return
    if dom.question and dow.question:
        raise CronError(
            f'{spec.name} cron cannot have "?" in both day fields, '
            "because then no day is specified at all.",
            field="day-of-month",
            position=dom.position,
            expected='"?" in exactly one of day-of-month and day-of-week',
            suggestion='Use "*" for the field you do not care about.',
            expression=expression,
        )
    if not dom.question and not dow.question:
        specified, other = ("day-of-month", "day-of-week")
        if dom.star and not dow.star:
            specified, other = ("day-of-week", "day-of-month")
        raise CronError(
            f'{spec.name} cron needs "?" in one of the day fields.',
            field=other,
            position=(dow.position if other == "day-of-week" else dom.position),
            expected='"?" in day-of-month or day-of-week',
            suggestion=(f'You specified {specified}, so write "?" in the {other} field.'),
            expression=expression,
        )


def parse(expression: str, dialect: str = "auto") -> ParsedExpression:
    """Parse ``expression`` into validated, expanded fields.

    Raises :class:`CronError` (never anything else) when the expression is not
    usable. ``dialect`` is one of ``"auto"``, ``"posix"``, ``"quartz"`` or
    ``"aws"``.
    """
    if not isinstance(expression, str):  # pragma: no cover - defensive
        raise CronError("The expression must be a string.", expected="a cron expression")

    original = expression
    text = " ".join(expression.split())
    if not text:
        raise CronError(
            "The expression is empty.",
            expected='5 fields such as "30 2 * * 1-5", or a macro such as "@daily"',
            expression=original,
        )

    requested = (dialect or "auto").strip().lower()
    if requested not in ("auto", *DIALECT_NAMES):
        raise CronError(
            f'Unknown dialect "{dialect}".',
            expected="auto, posix, quartz or aws",
            expression=original,
        )

    notes: list[str] = []
    macro: str | None = None

    if text.startswith("@"):
        token = text.lower()
        if token not in MACROS:
            close = _closest_macro(token)
            raise CronError(
                f'Unknown macro "{text}".',
                position=0,
                expected="@yearly, @annually, @monthly, @weekly, @daily, @midnight, "
                "@hourly or @reboot",
                suggestion=f'Did you mean "{close}"?' if close else None,
                expression=original,
            )
        if MACROS[token] is None:
            raise CronError(
                "@reboot runs when the scheduler starts, not on a clock, "
                "so it has no schedule to compute.",
                position=0,
                expected="a macro with a clock schedule, or a full expression",
                suggestion="Use explain_special to read about @reboot.",
                expression=original,
            )
        macro = token
        expanded = MACROS[token]
        assert expanded is not None
        notes.append(f'{token} expands to the POSIX expression "{expanded}".')
        text = expanded
        requested = "posix"

    if requested == "auto":
        detected, note = detect_dialect(text)
        if note:
            notes.append(note)
    else:
        detected = requested

    spec = DIALECTS[detected]
    tokens = _tokens(text)
    if len(tokens) not in spec.layouts:
        raise _field_count_error(text, len(tokens), spec, requested != "auto")

    layout = spec.layouts[len(tokens)]
    fields = tuple(
        _parse_field(fdef, spec, raw, position, text)
        for fdef, (raw, position) in zip(layout, tokens, strict=True)
    )

    parsed = ParsedExpression(
        expression=original.strip(),
        normalized=text,
        dialect=detected,
        fields=fields,
        macro=macro,
        notes=tuple(notes),
    )
    _check_day_fields(spec, parsed.field("dom"), parsed.field("dow"), text)
    return parsed


def _closest_macro(token: str) -> str | None:
    body = token.lstrip("@")
    for name in MACROS:
        if name.lstrip("@").startswith(body[:3]) and len(body) >= 3:
            return name
    return None
