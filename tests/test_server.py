"""The MCP tool surface.

These tests call the handlers directly rather than over a socket, because the
server has no socket. They need the MCP SDK for the Tool type, so they skip
cleanly if it is not installed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp", reason="the MCP SDK is needed for the tool definitions")

from cron_explainer.server import HANDLERS, TOOLS, dispatch

DECLARED_TOOLS = {
    "explain_cron",
    "next_runs",
    "previous_runs",
    "validate_cron",
    "describe_schedule_stats",
    "check_dst_safety",
    "compare_crons",
    "explain_special",
}


def call(name: str, **arguments: Any) -> tuple[str, dict[str, Any]]:
    """Run a tool and split its answer into the human half and the JSON half."""
    text = dispatch(name, arguments)
    summary, _, payload = text.partition("\n\n")
    return summary, json.loads(payload)


# --------------------------------------------------------------------------
# The surface itself
# --------------------------------------------------------------------------


def test_all_eight_tools_are_registered() -> None:
    assert {tool.name for tool in TOOLS} == DECLARED_TOOLS
    assert set(HANDLERS) == DECLARED_TOOLS


def test_every_tool_has_a_description_that_says_when_to_use_it() -> None:
    for tool in TOOLS:
        assert tool.description is not None
        assert len(tool.description) > 120, tool.name
        assert "Use this" in tool.description or "Use it" in tool.description, tool.name


def test_every_tool_has_a_strict_schema() -> None:
    for tool in TOOLS:
        schema = tool.inputSchema
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False, tool.name
        assert schema["required"], tool.name
        for name in schema["required"]:
            assert name in schema["properties"], (tool.name, name)


def test_no_em_dashes_anywhere_in_the_tool_surface() -> None:
    for tool in TOOLS:
        assert "—" not in (tool.description or "")
        assert "—" not in json.dumps(tool.inputSchema)


def test_the_published_listing_matches_the_code() -> None:
    """The listing declares a tool surface, and a mismatch gets flagged on review."""
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/publish-listing.yml"
    declared = set(re.findall(r'"name":\s*"(\w+)"', workflow.read_text()))
    assert declared == DECLARED_TOOLS


# --------------------------------------------------------------------------
# Response shape
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("explain_cron", {"expression": "30 2 * * 1-5"}),
        ("next_runs", {"expression": "30 2 * * 1-5"}),
        ("previous_runs", {"expression": "30 2 * * 1-5"}),
        ("validate_cron", {"expression": "30 2 * * 1-5"}),
        ("describe_schedule_stats", {"expression": "30 2 * * 1-5"}),
        ("check_dst_safety", {"expression": "30 2 * * *", "timezone": "America/New_York"}),
        ("compare_crons", {"expression_a": "0 * * * *", "expression_b": "0 0 * * *"}),
        ("explain_special", {"token": "@daily"}),
    ],
)
def test_every_tool_answers_with_prose_then_json(name: str, arguments: dict[str, Any]) -> None:
    summary, payload = call(name, **arguments)
    assert summary and not summary.startswith("{")
    assert "—" not in summary
    assert payload["ok"] is True


# --------------------------------------------------------------------------
# Individual tools
# --------------------------------------------------------------------------


def test_explain_cron_states_the_dialect_and_the_or_rule() -> None:
    summary, payload = call("explain_cron", expression="0 0 13 * FRI")
    assert "posix" in summary
    assert payload["dialect"]["name"] == "posix"
    assert payload["description"]["day_rule"] == "or"
    assert "13th" in payload["description"]["summary"]
    assert len(payload["description"]["fields"]) == 5


def test_next_runs_returns_offsets_and_deltas() -> None:
    _, payload = call(
        "next_runs",
        expression="30 2 * * *",
        timezone="America/New_York",
        after="2025-06-01T00:00:00Z",
        count=2,
    )
    assert payload["count"] == 2
    first = payload["runs"][0]
    assert first["local"] == "2025-06-01T02:30:00-04:00"
    assert first["utc"] == "2025-06-01T06:30:00Z"
    assert first["offset"] == "-04:00"
    assert first["weekday"] == "Sunday"
    assert first["relative"].startswith("in ")


def test_next_runs_caps_the_count() -> None:
    _, payload = call("next_runs", expression="* * * * *", count=5000)
    assert payload["count"] == 100


def test_previous_runs_look_backwards() -> None:
    _, payload = call(
        "previous_runs", expression="0 0 * * *", before="2026-03-05T12:00:00Z", count=3
    )
    assert [row["local"][:10] for row in payload["runs"]] == [
        "2026-03-05",
        "2026-03-04",
        "2026-03-03",
    ]
    assert payload["runs"][0]["relative"].endswith("ago")


def test_validate_cron_accepts_a_good_expression() -> None:
    summary, payload = call("validate_cron", expression="*/5 9-17 * * MON-FRI")
    assert payload["valid"] is True
    assert "Valid posix expression" in summary


def test_validate_cron_explains_a_bad_one_without_raising() -> None:
    summary, payload = call("validate_cron", expression="0 0 32 * *")
    assert payload["ok"] is False
    assert payload["valid"] is False
    error = payload["error"]
    assert error["field"] == "day-of-month"
    assert error["position"] == 4
    assert error["expected"] == "1-31"
    assert "^" in error["pointer"]
    assert summary.startswith("Not valid")


def test_validate_cron_suggests_a_fix_when_one_is_obvious() -> None:
    _, payload = call("validate_cron", expression="0 0 * * SUNDAY")
    assert "SUN" in payload["error"]["suggestion"]


def test_validate_cron_warns_about_a_valid_expression_that_never_fires() -> None:
    summary, payload = call("validate_cron", expression="0 0 30 2 *")
    assert payload["valid"] is True
    assert payload["never_runs"] is True
    assert payload["warnings"]
    assert "never" in summary


def test_stats_reports_frequency_and_gaps() -> None:
    _, payload = call("describe_schedule_stats", expression="30 2 * * 1-5")
    assert payload["frequency"]["runs_per_week"] == pytest.approx(5, abs=0.05)
    assert payload["gaps"]["shortest_human"] == "1 day"
    assert payload["gaps"]["longest_human"] == "3 days"


def test_stats_flags_a_pathological_schedule() -> None:
    _, payload = call("describe_schedule_stats", expression="* * * * *")
    assert "fires-every-minute" in payload["flags"]
    assert payload["flag_notes"]


def test_dst_reports_a_skipped_run() -> None:
    summary, payload = call(
        "check_dst_safety",
        expression="30 2 * * *",
        timezone="America/New_York",
        year=2025,
    )
    assert payload["safe"] is False
    assert payload["skipped_runs"][0]["local_time"] == "2025-03-09T02:30:00"
    assert payload["repeated_runs"] == []
    assert "skipped" in summary
    assert len(payload["transitions"]) == 2


def test_dst_reports_a_repeated_run_with_both_instants() -> None:
    _, payload = call(
        "check_dst_safety",
        expression="30 1 * * *",
        timezone="America/New_York",
        year=2025,
    )
    repeated = payload["repeated_runs"][0]
    assert repeated["local_time"] == "2025-11-02T01:30:00"
    assert repeated["utc_instants"] == ["2025-11-02T05:30:00Z", "2025-11-02T06:30:00Z"]


def test_dst_says_utc_is_immune() -> None:
    summary, payload = call("check_dst_safety", expression="30 2 * * *", timezone="UTC")
    assert payload["observes_dst"] is False
    assert payload["safe"] is True
    assert "UTC" in summary


def test_dst_requires_a_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        dispatch("check_dst_safety", {"expression": "30 2 * * *"})


def test_compare_finds_collisions() -> None:
    summary, payload = call(
        "compare_crons", expression_a="0 * * * *", expression_b="0 0 * * *", count=24
    )
    assert payload["coinciding_runs"]
    assert payload["b_is_subset_of_a"] is True
    assert payload["a_is_subset_of_b"] is False
    assert payload["identical_schedules"] is False
    assert "collide" in summary


def test_compare_spots_identical_schedules() -> None:
    _, payload = call("compare_crons", expression_a="0 0 * * *", expression_b="@daily")
    assert payload["identical_schedules"] is True
    assert "same schedule" in payload["summary"]


def test_compare_reports_schedules_that_never_meet() -> None:
    _, payload = call("compare_crons", expression_a="0 1-23/2 * * *", expression_b="0 0-22/2 * * *")
    assert payload["coinciding_runs"] == []
    assert payload["first_common_run_beyond_window"] is None


def test_explain_special_covers_every_macro() -> None:
    for token in ["@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly"]:
        summary, payload = call("explain_special", token=token)
        assert payload["equivalent_posix_expression"]
        assert payload["next_runs"]
        assert payload["scheduler_dependent"] is False
        assert token in summary


def test_explain_special_accepts_a_bare_token() -> None:
    _, payload = call("explain_special", token="daily")
    assert payload["token"] == "@daily"


def test_explain_special_is_honest_about_reboot() -> None:
    summary, payload = call("explain_special", token="@reboot")
    assert payload["scheduler_dependent"] is True
    assert payload["equivalent_posix_expression"] is None
    assert payload["next_runs"] == []
    assert "scheduler" in payload["note"]
    assert "no next run time" in summary


def test_explain_special_rejects_a_non_macro() -> None:
    with pytest.raises(ValueError, match="not a cron macro"):
        dispatch("explain_special", {"token": "@sometimes"})


# --------------------------------------------------------------------------
# Errors never escape as tracebacks
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        {"expression": "0 0 32 * *"},
        {"expression": "not a cron expression at all"},
        {"expression": "* * * *"},
        {"expression": "@reboot"},
        {"expression": "30 2 * * 1-5", "timezone": "Mars/Olympus_Mons"},
        {"expression": "30 2 * * 1-5", "after": "yesterday"},
        {"expression": "30 2 * * 1-5", "count": 0},
        {"expression": "30 2 * * 1-5", "dialect": "vixie"},
        {"expression": 42},
        {},
    ],
)
def test_bad_input_produces_a_message_not_a_traceback(arguments: dict[str, Any]) -> None:
    with pytest.raises(ValueError) as caught:
        dispatch("next_runs", arguments)
    message = str(caught.value)
    assert "Traceback" not in message
    assert message.strip()
    assert not message.startswith("Cron Explainer could not complete")


def test_an_unknown_tool_is_reported_clearly() -> None:
    with pytest.raises(ValueError, match="Unknown tool"):
        dispatch("delete_all_crontabs", {})


def test_errors_carry_structured_detail() -> None:
    with pytest.raises(ValueError) as caught:
        dispatch("next_runs", {"expression": "0 0 * * 9"})
    _, _, payload = str(caught.value).partition("\n\n")
    body = json.loads(payload)
    assert body["ok"] is False
    assert body["error"]["field"] == "day-of-week"
    assert body["error"]["position"] == 8


def test_a_schedule_that_never_runs_is_not_an_error() -> None:
    summary, payload = call("next_runs", expression="0 0 30 2 *")
    assert payload["never_runs"] is True
    assert payload["runs"] == []
    assert "never runs" in summary
