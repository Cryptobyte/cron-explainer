"""The occurrence engine: the day rule, leap years, extensions and performance."""

from __future__ import annotations

import calendar
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cron_explainer.schedule import (
    build,
    first_common_run,
    get_timezone,
    is_subset,
    last_weekday_of_month,
    nearest_weekday,
    next_runs,
    parse_instant,
    previous_runs,
)

UTC = ZoneInfo("UTC")
NEW_YORK = ZoneInfo("America/New_York")


def runs(
    expression: str, after: datetime, count: int = 5, zone: ZoneInfo = UTC, dialect: str = "auto"
) -> list[str]:
    _, schedule = build(expression, dialect)
    return [
        item.local.isoformat()[:19] for item in next_runs(schedule, zone, after, count).occurrences
    ]


ANCHOR = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Basic forward and backward walking
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("*/15 * * * *", ["2026-01-01T00:15:00", "2026-01-01T00:30:00", "2026-01-01T00:45:00"]),
        ("30 2 * * 1-5", ["2026-01-01T02:30:00", "2026-01-02T02:30:00", "2026-01-05T02:30:00"]),
        ("0 0 1 1 *", ["2027-01-01T00:00:00", "2028-01-01T00:00:00", "2029-01-01T00:00:00"]),
        ("0 0 29 2 *", ["2028-02-29T00:00:00", "2032-02-29T00:00:00", "2036-02-29T00:00:00"]),
        ("0 12 * * SUN", ["2026-01-04T12:00:00", "2026-01-11T12:00:00", "2026-01-18T12:00:00"]),
        ("0 0 0 L * ?", ["2026-01-31T00:00:00", "2026-02-28T00:00:00", "2026-03-31T00:00:00"]),
    ],
)
def test_next_runs_are_correct(expression: str, expected: list[str]) -> None:
    assert runs(expression, ANCHOR, len(expected)) == expected


def test_next_runs_are_strictly_after_the_anchor() -> None:
    exactly_on_a_run = datetime(2026, 1, 1, 2, 30, tzinfo=UTC)
    assert runs("30 2 * * *", exactly_on_a_run, 1) == ["2026-01-02T02:30:00"]


def test_previous_runs_walk_backwards() -> None:
    _, schedule = build("30 2 * * 1-5")
    result = previous_runs(schedule, UTC, datetime(2026, 7, 24, 12, 0, tzinfo=UTC), 3)
    assert [item.local.isoformat()[:19] for item in result.occurrences] == [
        "2026-07-24T02:30:00",
        "2026-07-23T02:30:00",
        "2026-07-22T02:30:00",
    ]


def test_previous_runs_are_strictly_before_the_anchor() -> None:
    _, schedule = build("30 2 * * *")
    on_a_run = datetime(2026, 1, 5, 2, 30, tzinfo=UTC)
    result = previous_runs(schedule, UTC, on_a_run, 1)
    assert result.occurrences[0].local.isoformat()[:19] == "2026-01-04T02:30:00"


def test_next_and_previous_are_consistent() -> None:
    _, schedule = build("*/17 3 * * *")
    forward = next_runs(schedule, UTC, ANCHOR, 5).occurrences
    backward = previous_runs(schedule, UTC, forward[-1].local, 4).occurrences
    assert [item.local for item in backward] == [item.local for item in reversed(forward[:-1])]


# --------------------------------------------------------------------------
# The day-of-month / day-of-week rule
# --------------------------------------------------------------------------


def matching_days(expression: str, year: int, month: int) -> list[int]:
    _, schedule = build(expression)
    length = calendar.monthrange(year, month)[1]
    return [day for day in range(1, length + 1) if schedule.day_matches(year, month, day)]


def test_both_day_fields_restricted_means_or_not_and() -> None:
    # August 2026: the 13th is a Thursday, and the Fridays are 7, 14, 21 and 28.
    # "0 0 13 * FRI" is NOT "Friday the 13th", it is "the 13th OR any Friday".
    assert matching_days("0 0 13 * FRI", 2026, 8) == [7, 13, 14, 21, 28]


def test_a_star_day_of_week_leaves_day_of_month_alone() -> None:
    assert matching_days("0 0 13 * *", 2026, 8) == [13]


def test_a_star_day_of_month_leaves_day_of_week_alone() -> None:
    assert matching_days("0 0 * * FRI", 2026, 8) == [7, 14, 21, 28]


def test_friday_the_thirteenth_needs_the_or_rule_worked_around() -> None:
    # The only honest way to say "Friday the 13th" in POSIX cron is to filter in
    # the job itself. Here we show the OR rule really does fire on both.
    _, schedule = build("0 0 13 * FRI")
    assert schedule.day_matches(2026, 11, 13)  # a Friday that is also the 13th
    assert schedule.day_matches(2026, 11, 6)  # a Friday that is not the 13th
    assert schedule.day_matches(2026, 8, 13)  # the 13th that is not a Friday


def test_leading_star_step_switches_the_rule_to_and() -> None:
    # Vixie cron sets its "star" flag from the first character of the field.
    assert matching_days("0 0 */2 * FRI", 2026, 8) == [7, 21]


# --------------------------------------------------------------------------
# Quartz and AWS extensions
# --------------------------------------------------------------------------


def test_last_day_of_month_handles_short_months() -> None:
    assert runs("0 0 0 L * ?", datetime(2028, 1, 1, tzinfo=UTC), 3) == [
        "2028-01-31T00:00:00",
        "2028-02-29T00:00:00",  # 2028 is a leap year
        "2028-03-31T00:00:00",
    ]


def test_offset_from_the_last_day() -> None:
    assert runs("0 0 0 L-2 * ?", datetime(2026, 3, 1, tzinfo=UTC), 2) == [
        "2026-03-29T00:00:00",
        "2026-04-28T00:00:00",
    ]


def test_nearest_weekday_stays_inside_the_month() -> None:
    # 1 August 2026 is a Saturday, so "1W" moves forward to Monday the 3rd
    # rather than back into July.
    assert nearest_weekday(2026, 8, 1) == 3
    # 31 May 2026 is a Sunday, so "31W" moves back to Friday the 29th.
    assert nearest_weekday(2026, 5, 31) == 29
    # A plain weekday is left alone.
    assert nearest_weekday(2026, 1, 15) == 15


def test_nearest_weekday_expression() -> None:
    # 15 February 2026 is a Sunday, so the job moves to Monday the 16th.
    assert runs("0 0 0 15W * ?", datetime(2026, 2, 1, tzinfo=UTC), 1) == ["2026-02-16T00:00:00"]


def test_last_weekday_of_month() -> None:
    # 31 May 2026 is a Sunday, so the last weekday is Friday the 29th.
    assert last_weekday_of_month(2026, 5) == 29
    assert runs("0 0 0 LW * ?", datetime(2026, 5, 1, tzinfo=UTC), 1) == ["2026-05-29T00:00:00"]


def test_nth_weekday_of_month() -> None:
    assert runs("0 0 0 ? * FRI#3", datetime(2026, 1, 1, tzinfo=UTC), 3) == [
        "2026-01-16T00:00:00",
        "2026-02-20T00:00:00",
        "2026-03-20T00:00:00",
    ]


def test_last_weekday_of_week_in_month() -> None:
    assert runs("0 0 0 ? * 6L", datetime(2026, 1, 1, tzinfo=UTC), 2) == [
        "2026-01-30T00:00:00",
        "2026-02-27T00:00:00",
    ]


def test_quartz_seconds_are_honoured() -> None:
    assert runs("*/20 * * * * ?", datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC), 4) == [
        "2026-01-01T00:00:20",
        "2026-01-01T00:00:40",
        "2026-01-01T00:01:00",
        "2026-01-01T00:01:20",
    ]


def test_quartz_year_field_bounds_the_schedule() -> None:
    _, schedule = build("0 0 12 ? * * 2027", dialect="quartz")
    result = next_runs(schedule, UTC, ANCHOR, 2)
    assert [item.local.isoformat()[:19] for item in result.occurrences] == [
        "2027-01-01T12:00:00",
        "2027-01-02T12:00:00",
    ]
    after_the_year = next_runs(schedule, UTC, datetime(2028, 1, 1, tzinfo=UTC), 1)
    assert after_the_year.occurrences == ()


def test_aws_expression_runs_on_sundays() -> None:
    # AWS counts Sunday as 1. A "?" day-of-week must not exclude it.
    assert runs("0 10 * * ? *", datetime(2026, 7, 24, 12, tzinfo=UTC), 3, dialect="aws") == [
        "2026-07-25T10:00:00",
        "2026-07-26T10:00:00",  # a Sunday
        "2026-07-27T10:00:00",
    ]


def test_aws_weekday_numbering() -> None:
    # "0 12 ? * 1 *" is Sunday in AWS, not Monday.
    assert runs("0 12 ? * 1 *", datetime(2026, 1, 1, tzinfo=UTC), 1, dialect="aws") == [
        "2026-01-04T12:00:00"
    ]


# --------------------------------------------------------------------------
# Impossible schedules
# --------------------------------------------------------------------------


def test_thirty_february_never_runs() -> None:
    _, schedule = build("0 0 30 2 *")
    assert schedule.never_runs()
    assert next_runs(schedule, UTC, ANCHOR, 1).occurrences == ()
    assert "February" in schedule.impossible_reason()


@pytest.mark.parametrize("expression", ["0 0 30 2 *", "0 0 31 2 *", "0 0 31 4 *", "0 0 31 2,4,6 *"])
def test_impossible_day_month_pairs(expression: str) -> None:
    _, schedule = build(expression)
    assert schedule.never_runs()


def test_impossible_schedules_return_quickly() -> None:
    started = time.perf_counter()
    _, schedule = build("0 0 30 2 *")
    assert next_runs(schedule, UTC, ANCHOR, 5).occurrences == ()
    assert time.perf_counter() - started < 1.0


def test_a_possible_but_rare_schedule_is_not_called_impossible() -> None:
    _, schedule = build("0 0 29 2 *")
    assert not schedule.never_runs()


# --------------------------------------------------------------------------
# Performance
# --------------------------------------------------------------------------


def test_leap_day_resolves_in_milliseconds() -> None:
    # 29 February can be four years away, which is over two million minutes.
    # A minute by minute search would take seconds. This must not.
    _, schedule = build("0 0 29 2 *")
    started = time.perf_counter()
    result = next_runs(schedule, UTC, datetime(2029, 3, 1, tzinfo=UTC), 1)
    elapsed = time.perf_counter() - started
    assert result.occurrences[0].local.isoformat()[:19] == "2032-02-29T00:00:00"
    assert elapsed < 0.05, f"took {elapsed:.3f}s"


def test_many_runs_of_a_sparse_schedule_is_fast() -> None:
    _, schedule = build("0 0 29 2 *")
    started = time.perf_counter()
    result = next_runs(schedule, UTC, ANCHOR, 20)
    assert len(result.occurrences) == 20
    assert time.perf_counter() - started < 0.5


def test_leap_day_respects_the_century_rule() -> None:
    # 2100 is divisible by 4 but is not a leap year, so the series jumps 8 years.
    assert runs("0 0 29 2 *", datetime(2096, 3, 1, tzinfo=UTC), 2) == [
        "2104-02-29T00:00:00",
        "2108-02-29T00:00:00",
    ]
    assert runs("0 0 29 2 *", datetime(2096, 1, 1, tzinfo=UTC), 2) == [
        "2096-02-29T00:00:00",
        "2104-02-29T00:00:00",
    ]


def test_the_busiest_schedule_is_still_fast() -> None:
    _, schedule = build("* * * * *")
    started = time.perf_counter()
    result = next_runs(schedule, UTC, ANCHOR, 100)
    assert len(result.occurrences) == 100
    assert time.perf_counter() - started < 0.2


# --------------------------------------------------------------------------
# Matching, subsets and collisions
# --------------------------------------------------------------------------


def test_matches_agrees_with_iteration() -> None:
    _, schedule = build("*/10 9-17 * * MON-FRI")
    for item in next_runs(schedule, UTC, ANCHOR, 20).occurrences:
        assert schedule.matches(item.local.replace(tzinfo=None))


def test_subset_detection() -> None:
    _, hourly = build("0 * * * *")
    _, daily = build("0 0 * * *")
    assert is_subset(daily, hourly, UTC, ANCHOR)
    assert not is_subset(hourly, daily, UTC, ANCHOR)


def test_first_common_run() -> None:
    _, every_two = build("0 */2 * * *")
    _, every_three = build("0 */3 * * *")
    found = first_common_run(every_two, every_three, UTC, datetime(2026, 1, 1, 0, 30, tzinfo=UTC))
    assert found is not None
    assert found.isoformat()[:19] == "2026-01-01T06:00:00"


def test_schedules_that_never_coincide() -> None:
    _, odd = build("0 1-23/2 * * *")
    _, even = build("0 0-22/2 * * *")
    assert first_common_run(odd, even, UTC, ANCHOR, max_steps=500) is None


# --------------------------------------------------------------------------
# Timezones and instants
# --------------------------------------------------------------------------


def test_output_carries_the_utc_offset() -> None:
    _, schedule = build("0 12 * * *")
    winter = next_runs(schedule, NEW_YORK, datetime(2026, 1, 1, tzinfo=UTC), 1).occurrences[0]
    summer = next_runs(schedule, NEW_YORK, datetime(2026, 7, 1, tzinfo=UTC), 1).occurrences[0]
    assert winter.local.isoformat().endswith("-05:00")
    assert summer.local.isoformat().endswith("-04:00")


def test_arithmetic_happens_in_the_requested_zone() -> None:
    _, schedule = build("0 0 * * *")
    result = next_runs(schedule, NEW_YORK, datetime(2026, 6, 15, 12, 0, tzinfo=UTC), 1)
    occurrence = result.occurrences[0]
    assert occurrence.local.isoformat()[:19] == "2026-06-16T00:00:00"
    assert occurrence.utc.isoformat()[:19] == "2026-06-16T04:00:00"


def test_unknown_timezone_is_reported_clearly() -> None:
    with pytest.raises(Exception) as caught:
        get_timezone("Mars/Olympus_Mons")
    assert "Mars/Olympus_Mons" in str(caught.value)


def test_parse_instant_accepts_offsets_and_z() -> None:
    assert parse_instant("2026-03-09T06:30:00Z", UTC, "after").isoformat()[:19] == (
        "2026-03-09T06:30:00"
    )
    naive = parse_instant("2026-03-09T01:30:00", NEW_YORK, "after")
    assert naive.tzinfo is not None


def test_parse_instant_rejects_nonsense() -> None:
    with pytest.raises(Exception) as caught:
        parse_instant("last tuesday", UTC, "after")
    assert "ISO 8601" in str(caught.value)


def test_parse_instant_defaults_to_now() -> None:
    before = datetime.now(tz=UTC) - timedelta(seconds=5)
    assert parse_instant(None, UTC, "after") > before
