"""Parsing, dialects, field syntax and the quality of the error messages."""

from __future__ import annotations

import pytest

from cron_explainer.errors import CronError
from cron_explainer.parser import detect_dialect, parse

# --------------------------------------------------------------------------
# Dialect detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("* * * * *", "posix"),
        ("30 2 * * 1-5", "posix"),
        ("0 30 2 ? * MON-FRI", "quartz"),  # "?" sits in the Quartz day-of-month slot
        ("0 15 10 ? * MON-FRI 2026", "quartz"),  # 7 fields is always Quartz
        ("30 2 ? * MON-FRI *", "aws"),  # "?" sits in the AWS day-of-month slot
        ("0 10 * * ? *", "aws"),  # the canonical EventBridge shape
        ("0 0 1 1 ? 2027", "aws"),  # a four digit year in the last field
        ("@daily", "posix"),
    ],
)
def test_dialect_is_detected_from_shape(expression: str, expected: str) -> None:
    assert detect_dialect(expression)[0] == expected
    assert parse(expression).dialect == expected


def test_ambiguous_six_fields_default_to_quartz_and_say_so() -> None:
    # No year, no "?", so nothing distinguishes the two 6 field layouts.
    dialect, note = detect_dialect("0 0 12 1 1 1")
    assert dialect == "quartz"
    assert note is not None and "aws" in note


def test_explicit_dialect_changes_interpretation() -> None:
    # Read as AWS this is minute hour day-of-month month day-of-week year.
    assert parse("0 0 12 1 ? 2027").dialect == "aws"
    # Read as Quartz the same six fields put "?" in the month, which is invalid.
    with pytest.raises(CronError) as caught:
        parse("0 0 12 1 ? 2027", dialect="quartz")
    assert caught.value.field == "month"


def test_unknown_dialect_is_rejected() -> None:
    with pytest.raises(CronError) as caught:
        parse("* * * * *", dialect="vixie")
    assert "vixie" in caught.value.message


@pytest.mark.parametrize("expression", ["* * * *", "* * * * * * * *", ""])
def test_wrong_field_count_is_rejected(expression: str) -> None:
    with pytest.raises(CronError):
        parse(expression)


def test_field_count_error_names_the_layout() -> None:
    with pytest.raises(CronError) as caught:
        parse("* * * *")
    error = caught.value
    assert "found 4" in error.message
    assert "day-of-week" in (error.expected or "")
    assert error.suggestion is not None


# --------------------------------------------------------------------------
# Field syntax
# --------------------------------------------------------------------------


def values(expression: str, key: str, dialect: str = "auto") -> set[int]:
    return set(parse(expression, dialect).field(key).values)


def test_wildcard_expands_to_the_whole_domain() -> None:
    assert values("* * * * *", "minute") == set(range(60))
    assert values("* * * * *", "hour") == set(range(24))
    assert values("* * * * *", "dom") == set(range(1, 32))
    assert values("* * * * *", "month") == set(range(1, 13))
    assert values("* * * * *", "dow") == set(range(7))


def test_single_values_and_lists() -> None:
    assert values("5 * * * *", "minute") == {5}
    assert values("1,2,3 * * * *", "minute") == {1, 2, 3}
    assert values("0,15,30,45 * * * *", "minute") == {0, 15, 30, 45}


def test_ranges() -> None:
    assert values("1-5 * * * *", "minute") == {1, 2, 3, 4, 5}
    assert values("* 9-17 * * *", "hour") == set(range(9, 18))


def test_steps() -> None:
    assert values("*/5 * * * *", "minute") == set(range(0, 60, 5))
    assert values("1-30/2 * * * *", "minute") == set(range(1, 31, 2))
    assert values("* */6 * * *", "hour") == {0, 6, 12, 18}
    # The widely supported "start/step" extension.
    assert values("10/15 * * * *", "minute") == {10, 25, 40, 55}


def test_names_are_case_insensitive() -> None:
    assert values("0 0 1 JAN *", "month") == {1}
    assert values("0 0 1 jan *", "month") == {1}
    assert values("0 0 1 JaN *", "month") == {1}
    assert values("0 0 * * MON-FRI", "dow") == {1, 2, 3, 4, 5}
    assert values("0 0 * * mon,wed,fri", "dow") == {1, 3, 5}


def test_month_names_map_to_numbers() -> None:
    assert values("0 0 1 JAN,JUL *", "month") == {1, 7}
    assert values("0 0 1 MAR-MAY *", "month") == {3, 4, 5}


def test_sunday_is_both_zero_and_seven_in_posix() -> None:
    assert values("0 0 * * 0", "dow") == {0}
    assert values("0 0 * * 7", "dow") == {0}
    assert values("0 0 * * SUN", "dow") == {0}
    assert values("0 0 * * 0-7", "dow") == set(range(7))


def test_quartz_counts_sunday_as_one() -> None:
    # Quartz FRI is 6, which is canonical 5.
    assert values("0 0 0 ? * 6", "dow", "quartz") == {5}
    assert values("0 0 0 ? * FRI", "dow", "quartz") == {5}
    assert values("0 0 0 ? * 1", "dow", "quartz") == {0}
    assert values("0 0 0 ? * MON-FRI", "dow", "quartz") == {1, 2, 3, 4, 5}


def test_aws_counts_sunday_as_one() -> None:
    assert values("0 0 ? * 1 *", "dow", "aws") == {0}
    assert values("0 0 ? * MON-FRI *", "dow", "aws") == {1, 2, 3, 4, 5}


def test_quartz_seconds_and_year() -> None:
    parsed = parse("30 0 12 ? * MON 2026-2028", dialect="quartz")
    assert parsed.field("second").values == frozenset({30})
    assert parsed.field("year").values == frozenset({2026, 2027, 2028})
    assert parsed.has_seconds and parsed.has_year


# --------------------------------------------------------------------------
# Quartz and AWS extensions
# --------------------------------------------------------------------------


def test_last_day_of_month() -> None:
    parsed = parse("0 0 0 L * ?", dialect="quartz")
    assert parsed.field("dom").dom_extras.last_offsets == frozenset({0})


def test_offset_from_last_day() -> None:
    parsed = parse("0 0 0 L-3 * ?", dialect="quartz")
    assert parsed.field("dom").dom_extras.last_offsets == frozenset({3})


def test_last_weekday_and_nearest_weekday() -> None:
    assert parse("0 0 0 LW * ?", dialect="quartz").field("dom").dom_extras.last_weekday
    nearest = parse("0 0 0 15W * ?", dialect="quartz").field("dom").dom_extras.nearest_weekday
    assert nearest == frozenset({15})


def test_nth_weekday_of_month() -> None:
    parsed = parse("0 0 0 ? * FRI#3", dialect="quartz")
    assert parsed.field("dow").dow_extras.nth == frozenset({(5, 3)})


def test_last_weekday_of_month() -> None:
    parsed = parse("0 0 0 ? * 6L", dialect="quartz")
    assert parsed.field("dow").dow_extras.last == frozenset({5})


def test_bare_l_in_day_of_week_is_saturday() -> None:
    assert parse("0 0 0 ? * L", dialect="quartz").field("dow").values == frozenset({6})


def test_question_mark_is_rejected_in_posix() -> None:
    with pytest.raises(CronError) as caught:
        parse("0 0 ? * *")
    assert caught.value.suggestion is not None
    assert '"*"' in caught.value.suggestion


def test_quartz_requires_a_question_mark_in_one_day_field() -> None:
    with pytest.raises(CronError) as caught:
        parse("0 0 0 15 * MON", dialect="quartz")
    assert "?" in (caught.value.expected or "")
    assert "day-of-week" in (caught.value.suggestion or "")


def test_quartz_rejects_question_marks_in_both_day_fields() -> None:
    with pytest.raises(CronError):
        parse("0 0 0 ? * ?", dialect="quartz")


def test_aws_requires_a_question_mark() -> None:
    with pytest.raises(CronError):
        parse("0 10 15 * MON *", dialect="aws")
    assert parse("0 10 15 * ? *", dialect="aws").field("dow").question


def test_extensions_are_rejected_in_posix_with_a_useful_hint() -> None:
    for expression in ["0 0 L * *", "0 0 15W * *", "0 0 * * FRI#3", "0 0 * * 6L"]:
        with pytest.raises(CronError) as caught:
            parse(expression)
        assert "quartz" in (caught.value.suggestion or "").lower()


def test_a_mistyped_weekday_is_not_blamed_on_the_w_operator() -> None:
    with pytest.raises(CronError) as caught:
        parse("0 0 * * WEDS")
    assert "WED" in (caught.value.suggestion or "")


# --------------------------------------------------------------------------
# Range validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "field", "position"),
    [
        ("60 * * * *", "minute", 0),
        ("* 24 * * *", "hour", 2),
        ("0 0 32 * *", "day-of-month", 4),
        ("0 0 0 * *", "day-of-month", 4),
        ("0 0 1 13 *", "month", 6),
        ("0 0 1 0 *", "month", 6),
        ("0 0 * * 8", "day-of-week", 8),
    ],
)
def test_out_of_range_values_name_the_field_and_position(
    expression: str, field: str, position: int
) -> None:
    with pytest.raises(CronError) as caught:
        parse(expression)
    error = caught.value
    assert error.field == field
    assert error.position == position
    assert error.expected is not None
    assert error.caret_line() is not None


def test_reversed_ranges_are_rejected_with_a_fix() -> None:
    with pytest.raises(CronError) as caught:
        parse("5-1 * * * *")
    error = caught.value
    assert "backwards" in error.message
    assert error.suggestion is not None and "1-5" in error.suggestion


def test_reversed_weekday_range_offers_a_wrapping_form() -> None:
    with pytest.raises(CronError) as caught:
        parse("0 0 * * FRI-MON")
    assert "," in (caught.value.suggestion or "")


def test_zero_step_is_rejected() -> None:
    with pytest.raises(CronError) as caught:
        parse("*/0 * * * *")
    assert "never advance" in caught.value.message


def test_oversized_step_is_rejected() -> None:
    with pytest.raises(CronError):
        parse("*/61 * * * *")


def test_garbage_is_rejected() -> None:
    with pytest.raises(CronError) as caught:
        parse("banana * * * *")
    assert "banana" in caught.value.message


def test_empty_list_item_is_rejected() -> None:
    with pytest.raises(CronError) as caught:
        parse("1,,2 * * * *")
    assert "comma" in (caught.value.suggestion or "")


def test_year_bounds_differ_between_quartz_and_aws() -> None:
    assert parse("0 0 0 1 1 ? 2099", dialect="quartz").field("year").values == frozenset({2099})
    with pytest.raises(CronError):
        parse("0 0 0 1 1 ? 2150", dialect="quartz")
    assert parse("0 0 1 1 ? 2150", dialect="aws").field("year").values == frozenset({2150})


# --------------------------------------------------------------------------
# Macros
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("macro", "equivalent"),
    [
        ("@yearly", "0 0 1 1 *"),
        ("@annually", "0 0 1 1 *"),
        ("@monthly", "0 0 1 * *"),
        ("@weekly", "0 0 * * 0"),
        ("@daily", "0 0 * * *"),
        ("@midnight", "0 0 * * *"),
        ("@hourly", "0 * * * *"),
    ],
)
def test_macros_expand(macro: str, equivalent: str) -> None:
    parsed = parse(macro)
    assert parsed.normalized == equivalent
    assert parsed.macro == macro
    assert parsed.notes


def test_reboot_has_no_schedule() -> None:
    with pytest.raises(CronError) as caught:
        parse("@reboot")
    assert "scheduler starts" in caught.value.message


def test_unknown_macro_suggests_a_real_one() -> None:
    with pytest.raises(CronError) as caught:
        parse("@dail")
    assert "@daily" in (caught.value.suggestion or "")


# --------------------------------------------------------------------------
# The day rule flag
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "rule"),
    [
        ("0 0 13 * FRI", "or"),  # both restricted: cron ORs them
        ("0 0 13 * *", "and"),  # day-of-week open
        ("0 0 * * FRI", "and"),  # day-of-month open
        ("0 0 * * *", "and"),  # both open
        ("0 0 */2 * FRI", "and"),  # a leading * counts as open, as in Vixie cron
        ("0 0 1-31 * FRI", "or"),  # a full range written out is still "restricted"
    ],
)
def test_day_rule_flag(expression: str, rule: str) -> None:
    assert parse(expression).day_rule == rule


def test_whitespace_is_normalized() -> None:
    parsed = parse("  30   2  *  *   1-5 ")
    assert parsed.normalized == "30 2 * * 1-5"
