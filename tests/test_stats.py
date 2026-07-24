"""Frequency analysis and gap measurement."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cron_explainer.schedule import ScheduleStats, build, compute_stats

UTC = ZoneInfo("UTC")
REFERENCE = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def stats(expression: str) -> ScheduleStats:
    _, schedule = build(expression)
    return compute_stats(schedule, UTC, REFERENCE)


def test_every_minute() -> None:
    result = stats("* * * * *")
    assert result.times_per_matching_day == 1440
    assert result.matching_days_per_year == pytest.approx(365.2425, abs=0.01)
    assert result.runs_per_day == pytest.approx(1440, abs=1)
    assert result.runs_per_hour == pytest.approx(60, abs=0.1)
    assert result.shortest_gap_seconds == 60
    assert result.longest_gap_seconds == 60
    assert "fires-every-minute" in result.flags


def test_hourly() -> None:
    result = stats("0 * * * *")
    assert result.times_per_matching_day == 24
    assert result.runs_per_day == pytest.approx(24, abs=0.1)
    assert result.runs_per_week == pytest.approx(168, abs=1)
    assert result.shortest_gap_seconds == 3600
    assert result.longest_gap_seconds == 3600


def test_daily() -> None:
    result = stats("0 2 * * *")
    assert result.times_per_matching_day == 1
    assert result.runs_per_day == pytest.approx(1, abs=0.01)
    assert result.runs_per_month == pytest.approx(30.44, abs=0.1)
    assert result.runs_per_year == pytest.approx(365.24, abs=0.5)
    assert result.shortest_gap_seconds == 86400


def test_weekdays_have_uneven_gaps() -> None:
    result = stats("30 2 * * 1-5")
    assert result.runs_per_week == pytest.approx(5, abs=0.05)
    assert result.runs_per_year == pytest.approx(261, abs=1)
    assert result.shortest_gap_seconds == 86400  # Monday to Tuesday
    assert result.longest_gap_seconds == 3 * 86400  # Friday to Monday


def test_yearly_is_flagged_as_rare() -> None:
    result = stats("0 0 1 1 *")
    assert result.runs_per_year == pytest.approx(1, abs=0.01)
    assert result.times_per_matching_day == 1
    assert "fires-less-than-once-a-year" not in result.flags


def test_leap_day_fires_less_than_once_a_year() -> None:
    result = stats("0 0 29 2 *")
    assert result.runs_per_year == pytest.approx(0.25, abs=0.02)
    assert "fires-less-than-once-a-year" in result.flags


def test_impossible_schedule() -> None:
    result = stats("0 0 30 2 *")
    assert result.never_runs
    assert result.runs_per_year == 0
    assert "never-runs" in result.flags
    assert result.reason is not None and "February" in result.reason


def test_high_frequency_flag() -> None:
    result = stats("*/10 * * * *")
    assert result.runs_per_day == pytest.approx(144, abs=1)
    assert "high-frequency" in result.flags
    assert "fires-every-minute" not in result.flags


def test_quartz_seconds_are_counted() -> None:
    result = stats("*/30 * * * * ?")
    assert result.times_per_matching_day == 2880
    assert result.shortest_gap_seconds == 30


def test_business_hours_gap_spans_the_night() -> None:
    result = stats("0 9-17 * * *")
    assert result.times_per_matching_day == 9
    assert result.shortest_gap_seconds == 3600
    assert result.longest_gap_seconds == 16 * 3600  # 17:00 to 09:00 the next day


def test_monthly_gaps_vary_with_month_length() -> None:
    result = stats("0 0 1 * *")
    assert result.shortest_gap_seconds == 28 * 86400  # February in a common year
    assert result.longest_gap_seconds == 31 * 86400


def test_sample_is_bounded() -> None:
    result = stats("* * * * *")
    assert result.sample_truncated
    assert result.sample_size <= 4000
