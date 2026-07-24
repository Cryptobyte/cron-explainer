"""Fields to English.

The goal is a sentence a person would actually write. "At 02:30, Monday through
Friday" beats "minute=30 hour=2 dow=1-5", and the day-of-month / day-of-week
rule gets said out loud because almost nobody expects it.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Final

from cron_explainer.parser import (
    DOW_FULL,
    MONTH_FULL,
    ParsedExpression,
    ParsedField,
)

_NTH_WORDS: Final[tuple[str, ...]] = ("", "first", "second", "third", "fourth", "fifth")


# --------------------------------------------------------------------------
# Small text helpers
# --------------------------------------------------------------------------


def ordinal(number: int) -> str:
    """1 -> "1st", 22 -> "22nd"."""
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def join_words(items: list[str], conjunction: str = "and") -> str:
    """["a", "b", "c"] -> "a, b and c"."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} {conjunction} {items[1]}"
    return f"{', '.join(items[:-1])} {conjunction} {items[-1]}"


def runs_of(values: list[int]) -> list[tuple[int, int]]:
    """Collapse a sorted list into contiguous [low, high] runs."""
    result: list[tuple[int, int]] = []
    for value in values:
        if result and value == result[-1][1] + 1:
            result[-1] = (result[-1][0], value)
        else:
            result.append((value, value))
    return result


def _full_step(field: ParsedField) -> int | None:
    """Detect ``*/n``: an even step that spans the field's whole domain.

    A step must actually have been written. "JAN,JUL" covers the same months as
    "*/6", but "in January and July" is what the author meant and what they want
    to read back.
    """
    if "/" not in field.raw:
        return None
    values = sorted(field.values)
    if len(values) < 2:
        return None
    step = values[1] - values[0]
    if step <= 1:
        return None
    if values != list(range(field.lo, field.hi + 1, step)):
        return None
    return step


def _progression(field: ParsedField) -> tuple[int, int, int] | None:
    """Detect an evenly spaced field, returning (start, end, step) when step > 1."""
    values = sorted(field.values)
    if len(values) < 3:
        return None
    step = values[1] - values[0]
    if step <= 1:
        return None
    if any(later - earlier != step for earlier, later in pairwise(values)):
        return None
    return values[0], values[-1], step


def _is_contiguous(field: ParsedField) -> tuple[int, int] | None:
    values = sorted(field.values)
    if len(values) < 2:
        return None
    if values[-1] - values[0] + 1 != len(values):
        return None
    return values[0], values[-1]


def _clock(hour: int, minute: int, second: int | None = None) -> str:
    if second:
        return f"{hour:02d}:{minute:02d}:{second:02d}"
    return f"{hour:02d}:{minute:02d}"


def human_duration(total_seconds: float) -> str:
    """Render a span as "3 hours 12 minutes", using the two largest useful units."""
    seconds = int(abs(total_seconds))
    if seconds == 0:
        return "0 seconds"
    units = (
        ("year", 31_536_000),
        ("day", 86_400),
        ("hour", 3_600),
        ("minute", 60),
        ("second", 1),
    )
    parts: list[str] = []
    for name, size in units:
        if seconds >= size:
            count, seconds = divmod(seconds, size)
            parts.append(f"{count} {name}" + ("s" if count != 1 else ""))
        if len(parts) == 2:
            break
    return " ".join(parts)


# --------------------------------------------------------------------------
# Value rendering
# --------------------------------------------------------------------------


def _value_label(field: ParsedField, value: int) -> str:
    if field.key == "dow":
        return DOW_FULL[value]
    if field.key == "month":
        return MONTH_FULL[value - 1]
    return str(value)


def summarize_values(field: ParsedField) -> str:
    """Compact expansion of a field, for the per field breakdown."""
    if field.question:
        return "any (no value specified)"
    pieces: list[str] = []
    values = sorted(field.values)
    if values:
        if field.covers_all:
            if field.key == "dow":
                pieces.append("SUN-SAT (all)")
            elif field.key == "month":
                pieces.append("JAN-DEC (all)")
            else:
                pieces.append(f"{field.lo}-{field.hi} (all)")
        else:
            for low, high in runs_of(values):
                if field.key in ("dow", "month"):
                    low_name = _short(field, low)
                    high_name = _short(field, high)
                    pieces.append(low_name if low == high else f"{low_name}-{high_name}")
                else:
                    pieces.append(str(low) if low == high else f"{low}-{high}")
    for offset in sorted(field.dom_extras.last_offsets):
        pieces.append("last day" if offset == 0 else f"{offset} days before the last day")
    if field.dom_extras.last_weekday:
        pieces.append("last weekday")
    for target in sorted(field.dom_extras.nearest_weekday):
        pieces.append(f"weekday nearest the {ordinal(target)}")
    for day in sorted(field.dow_extras.last):
        pieces.append(f"last {DOW_FULL[day]}")
    for day, nth in sorted(field.dow_extras.nth):
        pieces.append(f"{_NTH_WORDS[nth]} {DOW_FULL[day]}")
    return ", ".join(pieces) if pieces else "(nothing)"


def _short(field: ParsedField, value: int) -> str:
    if field.key == "dow":
        return DOW_FULL[value][:3].upper()
    return MONTH_FULL[value - 1][:3].upper()


# --------------------------------------------------------------------------
# Per field phrases
# --------------------------------------------------------------------------


def _hour_bare(hour: ParsedField) -> str:
    """The hour part, phrased to follow the word "past"."""
    if hour.covers_all:
        return "every hour"
    step = _full_step(hour)
    if step:
        return f"every {ordinal(step)} hour"
    progression = _progression(hour)
    if progression:
        return (
            f"every {ordinal(progression[2])} hour from "
            f"{progression[0]:02d}:00 through {progression[1]:02d}:00"
        )
    values = sorted(hour.values)
    if len(values) == 1:
        return f"{values[0]:02d}:00"
    span = _is_contiguous(hour)
    if span:
        return f"every hour from {span[0]:02d}:00 through {span[1]:02d}:00"
    return "the hours " + join_words([f"{value:02d}:00" for value in values])


def _hour_window(hour: ParsedField) -> str:
    """The hour part, phrased as a window, to follow "every N minutes"."""
    if hour.covers_all:
        return ""
    span = _is_contiguous(hour)
    if span:
        return f"between {span[0]:02d}:00 and {span[1]:02d}:59"
    values = sorted(hour.values)
    if len(values) == 1:
        return f"between {values[0]:02d}:00 and {values[0]:02d}:59"
    return "past " + _hour_bare(hour)


def time_phrase(parsed: ParsedExpression) -> str:
    """The clock half of the sentence."""
    minute = parsed.field("minute")
    hour = parsed.field("hour")
    second = parsed.field("second") if parsed.has_seconds else None

    if second is not None and not second.covers_all and len(second.values) > 1:
        lead = _every_n(second, "second")
        rest = _minutes_and_hours(minute, hour, 0)
        return f"{lead}, {rest[0].lower() + rest[1:]}" if rest else lead
    if second is not None and second.covers_all:
        rest = _minutes_and_hours(minute, hour, 0)
        if minute.covers_all and hour.covers_all:
            return "Every second"
        return f"Every second, {rest[0].lower() + rest[1:]}"

    fixed_second = sorted(second.values)[0] if second is not None else 0
    return _minutes_and_hours(minute, hour, fixed_second)


def _every_n(field: ParsedField, unit: str) -> str:
    step = _full_step(field)
    if step:
        return f"Every {step} {unit}s"
    progression = _progression(field)
    if progression:
        return (
            f"Every {progression[2]} {unit}s from {unit} {progression[0]} through {progression[1]}"
        )
    values = sorted(field.values)
    label = unit if len(values) == 1 else f"{unit}s"
    return f"At {label} " + join_words([str(value) for value in values])


def _minutes_and_hours(minute: ParsedField, hour: ParsedField, second: int) -> str:
    minutes = sorted(minute.values)
    hours = sorted(hour.values)

    if len(minutes) == 1 and len(hours) == 1:
        return f"At {_clock(hours[0], minutes[0], second)}"

    if not minute.covers_all and not hour.covers_all and len(minutes) * len(hours) <= 6:
        stamps = [_clock(h, m, second) for h in hours for m in minutes]
        return "At " + join_words(sorted(stamps))

    if minute.covers_all:
        window = _hour_window(hour)
        return "Every minute" if not window else f"Every minute {window}"

    step = _full_step(minute)
    if step:
        window = _hour_window(hour)
        base = f"Every {step} minutes"
        return base if not window else f"{base} {window}"
    progression = _progression(minute)
    if progression:
        base = (
            f"Every {progression[2]} minutes from minute {progression[0]} through {progression[1]}"
        )
        window = _hour_window(hour)
        return base if not window else f"{base}, {window}"

    if minutes == [0]:
        if hour.covers_all:
            return "Every hour, on the hour"
        span = _is_contiguous(hour)
        if span:
            return f"Every hour on the hour, from {span[0]:02d}:00 through {span[1]:02d}:00"
        return "Every hour on the hour, past " + _hour_bare(hour)

    if len(minutes) == 1:
        return f"At {minutes[0]} minutes past {_hour_bare(hour)}"

    listed = join_words([str(value) for value in minutes])
    return f"At minutes {listed} past {_hour_bare(hour)}"


def dom_phrase(field: ParsedField) -> str | None:
    """The day-of-month half, or None when it does not narrow anything."""
    if field.question:
        return None
    parts: list[str] = []
    values = sorted(field.values)
    if values and not field.covers_all:
        step = _full_step(field)
        span = _is_contiguous(field)
        if step:
            parts.append(f"on every {ordinal(step)} day of the month")
        elif span and len(values) > 2:
            parts.append(f"on days {span[0]} through {span[1]} of the month")
        else:
            parts.append(
                "on the " + join_words([ordinal(value) for value in values]) + " of the month"
            )
    for offset in sorted(field.dom_extras.last_offsets):
        if offset == 0:
            parts.append("on the last day of the month")
        else:
            parts.append(f"on the {ordinal(offset + 1)} to last day of the month")
    if field.dom_extras.last_weekday:
        parts.append("on the last weekday of the month")
    for target in sorted(field.dom_extras.nearest_weekday):
        parts.append(f"on the weekday nearest the {ordinal(target)}")
    if not parts:
        return None
    return join_words(parts, "or")


def dow_phrase(field: ParsedField) -> str | None:
    """The day-of-week half, or None when it does not narrow anything."""
    if field.question:
        return None
    parts: list[str] = []
    values = sorted(field.values)
    if values and not field.covers_all:
        if values == [0, 6]:
            parts.append("on Saturday and Sunday")
        else:
            span = _is_contiguous(field)
            if span and len(values) > 2:
                parts.append(f"{DOW_FULL[span[0]]} through {DOW_FULL[span[1]]}")
            else:
                parts.append("on " + join_words([DOW_FULL[value] for value in values]))
    for day in sorted(field.dow_extras.last):
        parts.append(f"on the last {DOW_FULL[day]} of the month")
    for day, nth in sorted(field.dow_extras.nth):
        parts.append(f"on the {_NTH_WORDS[nth]} {DOW_FULL[day]} of the month")
    if not parts:
        return None
    return join_words(parts, "or")


def month_phrase(field: ParsedField) -> str | None:
    if field.covers_all:
        return None
    values = sorted(field.values)
    step = _full_step(field)
    span = _is_contiguous(field)
    if step:
        return f"in every {ordinal(step)} month"
    if span and len(values) > 2:
        return f"from {MONTH_FULL[span[0] - 1]} through {MONTH_FULL[span[1] - 1]}"
    return "in " + join_words([MONTH_FULL[value - 1] for value in values])


def year_phrase(field: ParsedField) -> str | None:
    if field.covers_all:
        return None
    values = sorted(field.values)
    span = _is_contiguous(field)
    progression = _progression(field)
    if progression:
        return (
            f"every {ordinal(progression[2])} year from {progression[0]} through {progression[1]}"
        )
    if span and len(values) > 2:
        return f"in the years {span[0]} through {span[1]}"
    return "in " + join_words([str(value) for value in values])


def field_phrase(field: ParsedField) -> str:
    """A short meaning for one row of the field breakdown."""
    if field.question:
        return "no value specified (the other day field decides)"
    if field.key == "second":
        if field.covers_all:
            return "every second"
        return _every_n(field, "second").lower()
    if field.key == "minute":
        if field.covers_all:
            return "every minute"
        return _every_n(field, "minute").lower()
    if field.key == "hour":
        return _hour_bare(field)
    if field.key == "dom":
        return dom_phrase(field) or "every day of the month"
    if field.key == "month":
        return month_phrase(field) or "every month"
    if field.key == "dow":
        return dow_phrase(field) or "every day of the week"
    if field.key == "year":
        return year_phrase(field) or "every year"
    return summarize_values(field)  # pragma: no cover - all keys are covered above


def field_summary(parsed: ParsedExpression) -> list[dict[str, Any]]:
    """Per field breakdown: raw text, expanded values, and a phrase."""
    return [
        {
            "field": item.label,
            "raw": item.raw,
            "expanded": summarize_values(item),
            "count": 0 if item.question else len(item.values),
            "meaning": field_phrase(item),
        }
        for item in parsed.fields
    ]


# --------------------------------------------------------------------------
# The whole sentence
# --------------------------------------------------------------------------

_DAY_RULE_OR: Final[str] = (
    "Day-of-month and day-of-week are both restricted, so cron fires when EITHER "
    "matches. This is a union, not an intersection: the two conditions are ORed. "
    "That is the rule that surprises people."
)
_DAY_RULE_AND_DOM: Final[str] = (
    "Day-of-week is unrestricted, so only day-of-month narrows the schedule."
)
_DAY_RULE_AND_DOW: Final[str] = (
    "Day-of-month is unrestricted, so only day-of-week narrows the schedule."
)
_DAY_RULE_AND_BOTH: Final[str] = (
    "Both day fields are unrestricted, so the schedule runs every day that the month field allows."
)
_DAY_RULE_AND_STAR_STEP: Final[str] = (
    "One day field begins with *, which cron treats as unrestricted for the "
    "purposes of this rule, so the two day fields are ANDed rather than ORed."
)


@dataclass(frozen=True)
class Description:
    """The rendered explanation of an expression."""

    summary: str
    day_rule: str
    day_rule_note: str
    fields: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "day_rule": self.day_rule,
            "day_rule_note": self.day_rule_note,
            "fields": self.fields,
        }


def _day_rule_note(parsed: ParsedExpression) -> str:
    dom = parsed.field("dom")
    dow = parsed.field("dow")
    if parsed.day_rule == "or":
        return _DAY_RULE_OR
    dom_open = dom.covers_all or dom.question
    dow_open = dow.covers_all or dow.question
    if dom_open and dow_open:
        return _DAY_RULE_AND_BOTH
    if dom_open:
        return _DAY_RULE_AND_DOW
    if dow_open:
        return _DAY_RULE_AND_DOM
    return _DAY_RULE_AND_STAR_STEP


def day_clause(parsed: ParsedExpression) -> str:
    """The day half of the sentence, with the OR rule spelled out when it applies."""
    dom = dom_phrase(parsed.field("dom"))
    dow = dow_phrase(parsed.field("dow"))
    if dom and dow:
        joiner = " or " if parsed.day_rule == "or" else " and "
        return f"{dom}{joiner}{dow}"
    return dom or dow or "every day"


def describe(parsed: ParsedExpression) -> Description:
    """Render an expression as one readable sentence plus a field breakdown."""
    clock = time_phrase(parsed)
    days = day_clause(parsed)
    # "Every 5 minutes, every day" says the same thing twice.
    redundant = days == "every day" and (
        clock.startswith("Every") or parsed.field("hour").covers_all
    )
    parts = [clock] if redundant else [clock, days]
    month = month_phrase(parsed.field("month"))
    if month:
        parts.append(month)
    if parsed.has_year:
        year = year_phrase(parsed.field("year"))
        if year:
            parts.append(year)
    return Description(
        summary=", ".join(part for part in parts if part),
        day_rule=parsed.day_rule,
        day_rule_note=_day_rule_note(parsed),
        fields=field_summary(parsed),
    )
