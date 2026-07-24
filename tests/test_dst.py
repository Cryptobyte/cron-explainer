"""Daylight saving: the runs that vanish and the runs that happen twice.

The reference facts these tests pin, all from the IANA database:

* America/New_York 2025: forward 9 March (02:00 to 03:00), back 2 November
  (02:00 to 01:00, so 01:00-01:59 repeats).
* Europe/London 2025: forward 30 March (01:00 to 02:00), back 26 October.
* Australia/Sydney 2025: back 6 April, forward 5 October. Southern hemisphere,
  so the order of the two is reversed.
* Australia/Lord_Howe: shifts by 30 minutes rather than a full hour.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cron_explainer.dst import DstReport, analyze_dst, find_transitions
from cron_explainer.schedule import build, classify_local, next_runs

NEW_YORK = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
SYDNEY = ZoneInfo("Australia/Sydney")
LORD_HOWE = ZoneInfo("Australia/Lord_Howe")
UTC = ZoneInfo("UTC")
KOLKATA = ZoneInfo("Asia/Kolkata")


def report(expression: str, zone: ZoneInfo, name: str, year: int = 2025) -> DstReport:
    _, schedule = build(expression)
    return analyze_dst(schedule, zone, name, year)


# --------------------------------------------------------------------------
# Finding the transitions
# --------------------------------------------------------------------------


def test_new_york_2025_transitions() -> None:
    transitions = find_transitions(NEW_YORK, 2025)
    assert len(transitions) == 2
    forward, backward = transitions
    assert forward.kind == "forward"
    assert forward.window_start.isoformat() == "2025-03-09T02:00:00"
    assert forward.window_end.isoformat() == "2025-03-09T03:00:00"
    assert backward.kind == "backward"
    assert backward.window_start.isoformat() == "2025-11-02T01:00:00"
    assert backward.window_end.isoformat() == "2025-11-02T02:00:00"


def test_london_2025_transitions() -> None:
    forward, backward = find_transitions(LONDON, 2025)
    assert forward.window_start.isoformat() == "2025-03-30T01:00:00"
    assert backward.window_start.isoformat() == "2025-10-26T01:00:00"


def test_southern_hemisphere_order_is_reversed() -> None:
    first, second = find_transitions(SYDNEY, 2025)
    assert first.kind == "backward"
    assert first.window_start.isoformat() == "2025-04-06T02:00:00"
    assert second.kind == "forward"
    assert second.window_start.isoformat() == "2025-10-05T02:00:00"


def test_half_hour_shifts_are_handled() -> None:
    transitions = find_transitions(LORD_HOWE, 2025)
    assert transitions
    assert all(abs(item.shift.total_seconds()) == 1800 for item in transitions)


def test_zones_without_dst_have_no_transitions() -> None:
    assert find_transitions(UTC, 2025) == []
    assert find_transitions(KOLKATA, 2025) == []


# --------------------------------------------------------------------------
# Skipped runs, spring forward
# --------------------------------------------------------------------------


def test_a_run_inside_the_gap_is_skipped() -> None:
    result = report("30 2 * * *", NEW_YORK, "America/New_York")
    assert [item.local.isoformat() for item in result.skipped] == ["2025-03-09T02:30:00"]
    assert not result.repeated
    assert not result.safe
    assert "skipped" in result.summary


def test_every_run_in_the_gap_is_reported() -> None:
    result = report("*/30 * * * *", NEW_YORK, "America/New_York")
    assert [item.local.isoformat() for item in result.skipped] == [
        "2025-03-09T02:00:00",
        "2025-03-09T02:30:00",
    ]


def test_a_run_outside_the_gap_is_safe() -> None:
    result = report("0 5 * * *", NEW_YORK, "America/New_York")
    assert result.safe
    assert "Safe" in result.summary


def test_the_boundaries_of_the_gap_are_not_skipped() -> None:
    # 03:00 is the first instant that exists again after the jump.
    assert report("0 3 * * *", NEW_YORK, "America/New_York").safe
    # 01:59 survives the March jump, but it is inside the November repeat
    # window, which is exactly the sort of thing this tool exists to point out.
    boundary = report("59 1 * * *", NEW_YORK, "America/New_York")
    assert not boundary.skipped
    assert [item.local.isoformat() for item in boundary.repeated] == ["2025-11-02T01:59:00"]


# --------------------------------------------------------------------------
# Repeated runs, fall back
# --------------------------------------------------------------------------


def test_a_run_inside_the_repeat_happens_twice() -> None:
    result = report("30 1 * * *", NEW_YORK, "America/New_York")
    assert not result.skipped
    assert len(result.repeated) == 1
    event = result.repeated[0]
    assert event.local.isoformat() == "2025-11-02T01:30:00"
    first, second = event.instants_utc
    assert first.isoformat() == "2025-11-02T05:30:00+00:00"
    assert second.isoformat() == "2025-11-02T06:30:00+00:00"
    assert (second - first).total_seconds() == 3600


def test_repeat_recommendation_mentions_idempotency() -> None:
    result = report("30 1 * * *", NEW_YORK, "America/New_York")
    assert "idempotent" in result.recommendation


def test_sydney_gets_both_kinds() -> None:
    result = report("30 2 * * *", SYDNEY, "Australia/Sydney")
    assert [item.local.isoformat() for item in result.skipped] == ["2025-10-05T02:30:00"]
    assert [item.local.isoformat() for item in result.repeated] == ["2025-04-06T02:30:00"]


# --------------------------------------------------------------------------
# UTC is immune
# --------------------------------------------------------------------------


def test_utc_is_immune() -> None:
    result = report("30 2 * * *", UTC, "UTC")
    assert result.safe
    assert not result.observes_dst
    assert "no clock changes" in result.summary
    assert "immune" in result.recommendation or "never change offset" in result.recommendation


def test_a_fixed_offset_zone_is_immune() -> None:
    result = report("30 2 * * *", KOLKATA, "Asia/Kolkata")
    assert result.safe
    assert not result.observes_dst


# --------------------------------------------------------------------------
# Classification of individual local times
# --------------------------------------------------------------------------


def test_classify_local_times() -> None:
    assert classify_local(datetime(2025, 3, 9, 2, 30), NEW_YORK)[0] == "nonexistent"
    assert classify_local(datetime(2025, 11, 2, 1, 30), NEW_YORK)[0] == "ambiguous"
    assert classify_local(datetime(2025, 6, 1, 12, 0), NEW_YORK)[0] == "normal"
    assert classify_local(datetime(2025, 3, 9, 2, 30), UTC)[0] == "normal"


# --------------------------------------------------------------------------
# How next_runs behaves across a transition
# --------------------------------------------------------------------------


def test_next_runs_skips_a_nonexistent_time_and_says_so() -> None:
    _, schedule = build("30 2 * * *")
    result = next_runs(schedule, NEW_YORK, datetime(2025, 3, 8, 12, 0, tzinfo=NEW_YORK), 2)
    # 9 March 02:30 does not exist, so it is reported separately rather than
    # being quietly rounded to a time the job would not really have run at.
    assert [item.local.isoformat()[:19] for item in result.occurrences] == [
        "2025-03-10T02:30:00",
        "2025-03-11T02:30:00",
    ]
    assert [item.isoformat() for item in result.skipped] == ["2025-03-09T02:30:00"]


def test_next_runs_flags_an_ambiguous_time() -> None:
    _, schedule = build("30 1 * * *")
    result = next_runs(schedule, NEW_YORK, datetime(2025, 11, 1, 12, 0, tzinfo=NEW_YORK), 1)
    occurrence = result.occurrences[0]
    assert occurrence.status == "ambiguous"
    assert occurrence.repeat is not None
    payload = occurrence.to_dict()
    assert payload["dst"] == "ambiguous"
    assert "twice" in payload["dst_note"]


def test_run_times_stay_ordered_across_a_transition() -> None:
    _, schedule = build("0 * * * *")
    result = next_runs(schedule, NEW_YORK, datetime(2025, 10, 31, 20, 0, tzinfo=NEW_YORK), 60)
    instants = [item.utc for item in result.occurrences]
    assert instants == sorted(instants)
    assert len(set(instants)) == len(instants)


@pytest.mark.parametrize("year", [2024, 2025, 2026, 2027])
def test_the_analysis_works_for_any_year(year: int) -> None:
    result = report("30 2 * * *", NEW_YORK, "America/New_York", year)
    assert result.observes_dst
    assert len(result.skipped) == 1
    assert result.year == year
