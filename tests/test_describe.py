"""The English renderer, including a table of real world expressions."""

from __future__ import annotations

import pytest

from cron_explainer.describe import describe, human_duration, join_words, ordinal
from cron_explainer.parser import parse

# --------------------------------------------------------------------------
# A table of expressions people actually write
# --------------------------------------------------------------------------

REAL_WORLD = [
    ("* * * * *", "Every minute"),
    ("*/5 * * * *", "Every 5 minutes"),
    ("*/30 * * * *", "Every 30 minutes"),
    ("0 * * * *", "Every hour, on the hour"),
    ("15 * * * *", "At 15 minutes past every hour"),
    ("0 2 * * *", "At 02:00, every day"),
    ("30 2 * * 1-5", "At 02:30, Monday through Friday"),
    ("0 0,12 * * *", "At 00:00 and 12:00, every day"),
    ("0 22 * * 1-5", "At 22:00, Monday through Friday"),
    ("0 0 * * 0", "At 00:00, on Sunday"),
    ("0 0 * * SAT,SUN", "At 00:00, on Saturday and Sunday"),
    ("0 0 1 * *", "At 00:00, on the 1st of the month"),
    ("0 0 1 1 *", "At 00:00, on the 1st of the month, in January"),
    ("30 2 * JAN,JUL 1-5", "At 02:30, Monday through Friday, in January and July"),
    ("0 4 8-14 * *", "At 04:00, on days 8 through 14 of the month"),
    ("0 0 1 */3 *", "At 00:00, on the 1st of the month, in every 3rd month"),
    ("*/15 9-17 * * 1-5", "Every 15 minutes between 09:00 and 17:59, Monday through Friday"),
    ("0 9-17 * * *", "Every hour on the hour, from 09:00 through 17:00"),
    ("5 0 * 8 *", "At 00:05, every day, in August"),
    ("1-30/2 * * * *", "Every 2 minutes from minute 1 through 29"),
    ("@daily", "At 00:00, every day"),
    ("@weekly", "At 00:00, on Sunday"),
    ("@yearly", "At 00:00, on the 1st of the month, in January"),
    # Quartz
    ("0 0 0 L * ?", "At 00:00, on the last day of the month"),
    ("0 0 0 LW * ?", "At 00:00, on the last weekday of the month"),
    ("0 0 0 15W * ?", "At 00:00, on the weekday nearest the 15th"),
    ("0 0 0 ? * FRI#3", "At 00:00, on the third Friday of the month"),
    ("0 0 0 ? * 6L", "At 00:00, on the last Friday of the month"),
    ("0 15 10 ? * MON-FRI 2026", "At 10:15, Monday through Friday, in 2026"),
    # AWS EventBridge
    ("0 10 * * ? *", "At 10:00, every day"),
    ("*/10 * ? * MON-FRI *", "Every 10 minutes, Monday through Friday"),
]


@pytest.mark.parametrize(("expression", "expected"), REAL_WORLD)
def test_real_world_expressions_read_like_english(expression: str, expected: str) -> None:
    assert describe(parse(expression)).summary == expected


@pytest.mark.parametrize(("expression", "_expected"), REAL_WORLD)
def test_no_em_dashes_in_user_facing_text(expression: str, _expected: str) -> None:
    description = describe(parse(expression))
    assert "—" not in description.summary
    assert "—" not in description.day_rule_note
    for row in description.fields:
        assert "—" not in row["meaning"]


# --------------------------------------------------------------------------
# The day rule is spelled out
# --------------------------------------------------------------------------


def test_the_or_rule_is_stated_in_the_sentence() -> None:
    description = describe(parse("0 0 13 * FRI"))
    assert description.summary == "At 00:00, on the 13th of the month or on Friday"
    assert description.day_rule == "or"
    assert "EITHER" in description.day_rule_note


def test_the_and_case_does_not_claim_an_or() -> None:
    description = describe(parse("0 0 13 * *"))
    assert description.summary == "At 00:00, on the 13th of the month"
    assert description.day_rule == "and"
    assert "only day-of-month" in description.day_rule_note


def test_day_of_week_only() -> None:
    description = describe(parse("0 0 * * FRI"))
    assert "only day-of-week" in description.day_rule_note


# --------------------------------------------------------------------------
# The field breakdown
# --------------------------------------------------------------------------


def test_field_breakdown_covers_every_field() -> None:
    description = describe(parse("30 2 * JAN,JUL 1-5"))
    labels = [row["field"] for row in description.fields]
    assert labels == ["minute", "hour", "day-of-month", "month", "day-of-week"]
    by_label = {row["field"]: row for row in description.fields}
    assert by_label["minute"]["raw"] == "30"
    assert by_label["month"]["expanded"] == "JAN, JUL"
    assert by_label["day-of-week"]["expanded"] == "MON-FRI"
    assert by_label["day-of-month"]["expanded"] == "1-31 (all)"
    assert by_label["day-of-week"]["count"] == 5


def test_quartz_breakdown_includes_seconds_and_year() -> None:
    description = describe(parse("0 15 10 ? * MON-FRI 2026"))
    labels = [row["field"] for row in description.fields]
    assert labels == ["second", "minute", "hour", "day-of-month", "month", "day-of-week", "year"]
    by_label = {row["field"]: row for row in description.fields}
    assert by_label["day-of-month"]["expanded"] == "any (no value specified)"
    assert by_label["day-of-month"]["count"] == 0


def test_extensions_are_described_in_the_breakdown() -> None:
    description = describe(parse("0 0 0 L,15W * ?", dialect="quartz"))
    by_label = {row["field"]: row for row in description.fields}
    expanded = by_label["day-of-month"]["expanded"]
    assert "last day" in expanded
    assert "weekday nearest the 15th" in expanded


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (1, "1st"),
        (2, "2nd"),
        (3, "3rd"),
        (4, "4th"),
        (11, "11th"),
        (12, "12th"),
        (13, "13th"),
        (21, "21st"),
        (22, "22nd"),
        (23, "23rd"),
        (31, "31st"),
        (101, "101st"),
    ],
)
def test_ordinal(number: int, expected: str) -> None:
    assert ordinal(number) == expected


def test_join_words() -> None:
    assert join_words([]) == ""
    assert join_words(["a"]) == "a"
    assert join_words(["a", "b"]) == "a and b"
    assert join_words(["a", "b", "c"]) == "a, b and c"
    assert join_words(["a", "b"], "or") == "a or b"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0 seconds"),
        (1, "1 second"),
        (60, "1 minute"),
        (90, "1 minute 30 seconds"),
        (3600, "1 hour"),
        (11520, "3 hours 12 minutes"),
        (86400, "1 day"),
        (90000, "1 day 1 hour"),
        (31536000, "1 year"),
        (-3600, "1 hour"),
    ],
)
def test_human_duration(seconds: int, expected: str) -> None:
    assert human_duration(seconds) == expected
