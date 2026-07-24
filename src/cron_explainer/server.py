"""The MCP server: eight tools over stdio.

Every tool answers in one text block. The first paragraph is the short human
readable answer, then a pretty printed JSON object carries the structured data.
Nothing here touches the network, the filesystem or the environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from cron_explainer import __version__
from cron_explainer.describe import describe, human_duration
from cron_explainer.dst import analyze_dst
from cron_explainer.errors import CronError
from cron_explainer.parser import (
    DIALECTS,
    MACRO_MEANINGS,
    MACROS,
    parse,
)
from cron_explainer.schedule import (
    MAX_RESULTS,
    CronSchedule,
    Occurrence,
    build,
    compute_stats,
    first_common_run,
    get_timezone,
    is_subset,
    next_runs,
    parse_instant,
    previous_runs,
)

SERVER_NAME = "cron-explainer"


# --------------------------------------------------------------------------
# Argument helpers
# --------------------------------------------------------------------------


def _text_arg(
    arguments: dict[str, Any], name: str, *, required: bool = True, default: str = ""
) -> str:
    value = arguments.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise CronError(
                f'The "{name}" argument is required.',
                field=name,
                expected="a non empty string",
            )
        return default
    if not isinstance(value, str):
        raise CronError(
            f'The "{name}" argument must be a string, not {type(value).__name__}.',
            field=name,
            expected="a string",
        )
    return value


def _count_arg(arguments: dict[str, Any], default: int) -> int:
    value = arguments.get("count", default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CronError(
            'The "count" argument must be a whole number.',
            field="count",
            expected=f"1 to {MAX_RESULTS}",
        )
    number = int(value)
    if number < 1:
        raise CronError(
            'The "count" argument must be at least 1.',
            field="count",
            expected=f"1 to {MAX_RESULTS}",
            suggestion=f"Use count={default} for the usual answer.",
        )
    return min(number, MAX_RESULTS)


def _dialect_arg(arguments: dict[str, Any]) -> str:
    return _text_arg(arguments, "dialect", required=False, default="auto") or "auto"


def _timezone_arg(arguments: dict[str, Any]) -> str:
    return _text_arg(arguments, "timezone", required=False, default="UTC") or "UTC"


def _year_arg(arguments: dict[str, Any], reference: datetime) -> int:
    value = arguments.get("year")
    if value is None:
        return reference.year
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise CronError(
            'The "year" argument must be a four digit year.',
            field="year",
            expected="a year such as 2026",
        )
    try:
        year = int(value)
    except (TypeError, ValueError):
        raise CronError(
            f'"{value}" is not a year.',
            field="year",
            expected="a year such as 2026",
        ) from None
    if not 1900 <= year <= 2200:
        raise CronError(
            f"Year {year} is outside the range this tool will analyze.",
            field="year",
            expected="1900 to 2200",
        )
    return year


def _dialect_details(name: str) -> dict[str, Any]:
    spec = DIALECTS[name]
    return {"name": name, "description": spec.description}


def _render(summary: str, payload: dict[str, Any]) -> str:
    return f"{summary}\n\n{json.dumps(payload, indent=2, ensure_ascii=False)}"


def _occurrence_rows(
    occurrences: Sequence[Occurrence], reference: datetime
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in occurrences:
        row = item.to_dict()
        delta = (item.local - reference).total_seconds()
        row["relative"] = (
            f"in {human_duration(delta)}" if delta >= 0 else f"{human_duration(delta)} ago"
        )
        rows.append(row)
    return rows


def _never_runs_payload(expression: str, dialect: str, schedule: CronSchedule) -> dict[str, Any]:
    return {
        "ok": True,
        "expression": expression,
        "dialect": dialect,
        "never_runs": True,
        "reason": schedule.impossible_reason(),
        "runs": [],
    }


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------


def tool_explain_cron(arguments: dict[str, Any]) -> str:
    expression = _text_arg(arguments, "expression")
    dialect = _dialect_arg(arguments)
    zone_name = _timezone_arg(arguments)
    zone = get_timezone(zone_name)

    parsed, schedule = build(expression, dialect)
    description = describe(parsed)
    now = datetime.now(tz=zone)

    upcoming = next_runs(schedule, zone, now, 3)
    never = schedule.never_runs(now.date())

    payload: dict[str, Any] = {
        "ok": True,
        "expression": parsed.expression,
        "normalized": parsed.normalized,
        "dialect": _dialect_details(parsed.dialect),
        "timezone": zone_name,
        "macro": parsed.macro,
        "notes": list(parsed.notes),
        "description": description.to_dict(),
        "never_runs": never,
        "next_runs": _occurrence_rows(upcoming.occurrences, now),
    }
    if never:
        payload["reason"] = schedule.impossible_reason()

    summary = f"{description.summary}. Dialect: {parsed.dialect}. Timezone: {zone_name}."
    if parsed.day_rule == "or":
        summary += " Note the day-of-month OR day-of-week rule below."
    if never:
        summary = f"This expression never runs. {schedule.impossible_reason()}"
    return _render(summary, payload)


def tool_next_runs(arguments: dict[str, Any]) -> str:
    expression = _text_arg(arguments, "expression")
    dialect = _dialect_arg(arguments)
    zone_name = _timezone_arg(arguments)
    zone = get_timezone(zone_name)
    count = _count_arg(arguments, 5)
    after = parse_instant(arguments.get("after"), zone, "after")

    parsed, schedule = build(expression, dialect)
    result = next_runs(schedule, zone, after, count)

    if not result.occurrences and schedule.never_runs(after.date()):
        payload = _never_runs_payload(parsed.expression, parsed.dialect, schedule)
        payload["timezone"] = zone_name
        return _render(f"This expression never runs. {schedule.impossible_reason()}", payload)

    rows = _occurrence_rows(result.occurrences, after)
    payload = {
        "ok": True,
        "expression": parsed.expression,
        "dialect": _dialect_details(parsed.dialect),
        "timezone": zone_name,
        "after": after.isoformat(),
        "description": describe(parsed).summary,
        "count": len(rows),
        "runs": rows,
        "skipped_by_dst": [moment.isoformat() for moment in result.skipped],
        "horizon_exhausted": result.exhausted,
    }

    if rows:
        first = rows[0]
        summary = (
            f"Next run {first['local']} ({first['relative']}). "
            f"Showing {len(rows)} run(s) in {zone_name}, dialect {parsed.dialect}."
        )
    else:
        summary = f"No run found within the search horizon for {parsed.expression} in {zone_name}."
    if result.skipped:
        summary += (
            f" {len(result.skipped)} run(s) were skipped because the local clock "
            "jumps over them at a DST change."
        )
    return _render(summary, payload)


def tool_previous_runs(arguments: dict[str, Any]) -> str:
    expression = _text_arg(arguments, "expression")
    dialect = _dialect_arg(arguments)
    zone_name = _timezone_arg(arguments)
    zone = get_timezone(zone_name)
    count = _count_arg(arguments, 5)
    before = parse_instant(arguments.get("before"), zone, "before")

    parsed, schedule = build(expression, dialect)
    result = previous_runs(schedule, zone, before, count)
    rows = _occurrence_rows(result.occurrences, before)

    payload = {
        "ok": True,
        "expression": parsed.expression,
        "dialect": _dialect_details(parsed.dialect),
        "timezone": zone_name,
        "before": before.isoformat(),
        "description": describe(parsed).summary,
        "count": len(rows),
        "runs": rows,
        "skipped_by_dst": [moment.isoformat() for moment in result.skipped],
        "horizon_exhausted": result.exhausted,
    }
    if rows:
        first = rows[0]
        summary = (
            f"Most recent run {first['local']} ({first['relative']}). "
            f"Showing {len(rows)} run(s) in {zone_name}, dialect {parsed.dialect}."
        )
    else:
        summary = f"No earlier run found for {parsed.expression} in {zone_name}."
    return _render(summary, payload)


def tool_validate_cron(arguments: dict[str, Any]) -> str:
    expression = _text_arg(arguments, "expression")
    dialect = _dialect_arg(arguments)
    try:
        parsed = parse(expression, dialect)
    except CronError as error:
        payload = {
            "ok": False,
            "valid": False,
            "expression": expression,
            "requested_dialect": dialect,
            "error": error.to_dict(),
        }
        summary = f"Not valid. {error.message}"
        if error.suggestion:
            summary += f" {error.suggestion}"
        return _render(summary, payload)

    schedule = CronSchedule(parsed)
    never = schedule.never_runs()
    payload = {
        "ok": True,
        "valid": True,
        "expression": parsed.expression,
        "normalized": parsed.normalized,
        "dialect": _dialect_details(parsed.dialect),
        "notes": list(parsed.notes),
        "day_rule": parsed.day_rule,
        "description": describe(parsed).summary,
        "never_runs": never,
        "warnings": [schedule.impossible_reason()] if never else [],
    }
    summary = f"Valid {parsed.dialect} expression. {describe(parsed).summary}."
    if never:
        summary += f" Warning: it never actually fires. {schedule.impossible_reason()}"
    return _render(summary, payload)


def tool_describe_schedule_stats(arguments: dict[str, Any]) -> str:
    expression = _text_arg(arguments, "expression")
    dialect = _dialect_arg(arguments)
    zone_name = _timezone_arg(arguments)
    zone = get_timezone(zone_name)

    parsed, schedule = build(expression, dialect)
    now = datetime.now(tz=zone)
    stats = compute_stats(schedule, zone, now)

    payload: dict[str, Any] = {
        "ok": True,
        "expression": parsed.expression,
        "dialect": _dialect_details(parsed.dialect),
        "timezone": zone_name,
        "description": describe(parsed).summary,
        "never_runs": stats.never_runs,
        "reason": stats.reason,
        "frequency": {
            "runs_per_hour": round(stats.runs_per_hour, 4),
            "runs_per_day": round(stats.runs_per_day, 4),
            "runs_per_week": round(stats.runs_per_week, 4),
            "runs_per_month": round(stats.runs_per_month, 4),
            "runs_per_year": round(stats.runs_per_year, 4),
            "times_per_matching_day": stats.times_per_matching_day,
            "matching_days_per_year": round(stats.matching_days_per_year, 3),
        },
        "gaps": {
            "shortest_seconds": stats.shortest_gap_seconds,
            "shortest_human": (
                human_duration(stats.shortest_gap_seconds)
                if stats.shortest_gap_seconds is not None
                else None
            ),
            "longest_seconds": stats.longest_gap_seconds,
            "longest_human": (
                human_duration(stats.longest_gap_seconds)
                if stats.longest_gap_seconds is not None
                else None
            ),
            "sample_size": stats.sample_size,
            "sample_truncated": stats.sample_truncated,
        },
        "flags": list(stats.flags),
        "flag_notes": [_FLAG_NOTES[flag] for flag in stats.flags if flag in _FLAG_NOTES],
    }

    if stats.never_runs:
        summary = f"This expression never runs. {stats.reason}"
    else:
        summary = (
            f"About {_round_text(stats.runs_per_day)} runs per day, "
            f"{_round_text(stats.runs_per_week)} per week, "
            f"{_round_text(stats.runs_per_year)} per year."
        )
        if stats.shortest_gap_seconds is not None and stats.longest_gap_seconds is not None:
            summary += (
                f" Gaps between runs range from {human_duration(stats.shortest_gap_seconds)} "
                f"to {human_duration(stats.longest_gap_seconds)}."
            )
        if stats.flags:
            summary += " Flags: " + ", ".join(stats.flags) + "."
    return _render(summary, payload)


_FLAG_NOTES: dict[str, str] = {
    "fires-every-minute": (
        "This runs every minute of every day, 525,600 times a year. Make sure that "
        "is intended and that the job finishes in under a minute."
    ),
    "fires-every-second": (
        "This runs every second. Cron is rarely the right tool at that frequency."
    ),
    "fires-less-than-once-a-year": (
        "This fires less than once a year, so a mistake in it could go unnoticed "
        "for a very long time."
    ),
    "high-frequency": "This runs more than 60 times a day.",
    "never-runs": "This expression can never fire.",
}


def _round_text(value: float) -> str:
    if value >= 100:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{value:.4f}".rstrip("0").rstrip(".")


def tool_check_dst_safety(arguments: dict[str, Any]) -> str:
    expression = _text_arg(arguments, "expression")
    zone_name = _text_arg(arguments, "timezone")
    dialect = _dialect_arg(arguments)
    zone = get_timezone(zone_name)
    year = _year_arg(arguments, datetime.now(tz=zone))

    parsed, schedule = build(expression, dialect)
    report = analyze_dst(schedule, zone, zone_name, year)

    payload: dict[str, Any] = {
        "ok": True,
        "expression": parsed.expression,
        "dialect": _dialect_details(parsed.dialect),
        "description": describe(parsed).summary,
        **report.to_dict(),
    }
    summary = f"{report.summary} {report.recommendation}"
    return _render(summary, payload)


def tool_compare_crons(arguments: dict[str, Any]) -> str:
    expression_a = _text_arg(arguments, "expression_a")
    expression_b = _text_arg(arguments, "expression_b")
    dialect = _dialect_arg(arguments)
    zone_name = _timezone_arg(arguments)
    zone = get_timezone(zone_name)
    count = _count_arg(arguments, 10)

    parsed_a, schedule_a = build(expression_a, dialect)
    parsed_b, schedule_b = build(expression_b, dialect)
    now = datetime.now(tz=zone)

    runs_a = next_runs(schedule_a, zone, now, count)
    runs_b = next_runs(schedule_b, zone, now, count)
    rows_a = _occurrence_rows(runs_a.occurrences, now)
    rows_b = _occurrence_rows(runs_b.occurrences, now)

    instants_a = {item.local for item in runs_a.occurrences}
    instants_b = {item.local for item in runs_b.occurrences}
    shared = sorted(instants_a & instants_b)

    common_beyond = None
    if not shared:
        found = first_common_run(schedule_a, schedule_b, zone, now)
        common_beyond = found.isoformat() if found else None

    description_a = describe(parsed_a)
    description_b = describe(parsed_b)
    stats_a = compute_stats(schedule_a, zone, now)
    stats_b = compute_stats(schedule_b, zone, now)

    a_in_b = is_subset(schedule_a, schedule_b, zone, now)
    b_in_a = is_subset(schedule_b, schedule_a, zone, now)

    differences = _difference_notes(
        description_a.summary,
        description_b.summary,
        stats_a.runs_per_year,
        stats_b.runs_per_year,
        a_in_b,
        b_in_a,
        bool(shared) or common_beyond is not None,
    )

    payload: dict[str, Any] = {
        "ok": True,
        "timezone": zone_name,
        "reference_time": now.isoformat(),
        "a": {
            "expression": parsed_a.expression,
            "dialect": _dialect_details(parsed_a.dialect),
            "description": description_a.summary,
            "runs_per_year": round(stats_a.runs_per_year, 3),
            "next_runs": rows_a,
        },
        "b": {
            "expression": parsed_b.expression,
            "dialect": _dialect_details(parsed_b.dialect),
            "description": description_b.summary,
            "runs_per_year": round(stats_b.runs_per_year, 3),
            "next_runs": rows_b,
        },
        "identical_schedules": a_in_b and b_in_a,
        "a_is_subset_of_b": a_in_b,
        "b_is_subset_of_a": b_in_a,
        "coinciding_runs": [moment.isoformat() for moment in shared],
        "first_common_run_beyond_window": common_beyond,
        "summary": differences,
    }

    if shared:
        verb = "run happens" if len(shared) == 1 else "runs happen"
        headline = (
            f"Yes, they collide. {len(shared)} of the next {count} {verb} at the "
            f"same instant, starting {shared[0].isoformat()}."
        )
    elif common_beyond:
        headline = (
            f"They do not collide in the next {count} runs, but they do meet at {common_beyond}."
        )
    else:
        headline = "They never fire at the same instant within the search window."
    return _render(f"{headline} {differences}", payload)


def _difference_notes(
    summary_a: str,
    summary_b: str,
    per_year_a: float,
    per_year_b: float,
    a_in_b: bool,
    b_in_a: bool,
    overlaps: bool,
) -> str:
    if a_in_b and b_in_a:
        return f"The two expressions describe the same schedule: {summary_a}."
    parts = [f"A is {summary_a.lower()}", f"B is {summary_b.lower()}"]
    text = ". ".join(parts) + "."
    if a_in_b:
        text += " Every run of A is also a run of B, so B is the broader schedule."
    elif b_in_a:
        text += " Every run of B is also a run of A, so A is the broader schedule."
    elif not overlaps:
        text += " They have no run times in common."
    if per_year_a and per_year_b:
        ratio = per_year_a / per_year_b
        if ratio >= 1.5 or ratio <= 0.67:
            busier, quieter = ("A", "B") if ratio > 1 else ("B", "A")
            factor = ratio if ratio > 1 else 1 / ratio
            text += f" {busier} fires about {factor:.1f} times as often as {quieter}."
    return text


def tool_explain_special(arguments: dict[str, Any]) -> str:
    token = _text_arg(arguments, "token").strip().lower()
    zone_name = _timezone_arg(arguments)
    zone = get_timezone(zone_name)
    if not token.startswith("@"):
        token = "@" + token

    if token not in MACROS:
        raise CronError(
            f'"{token}" is not a cron macro.',
            field="token",
            expected=", ".join(sorted(MACROS)),
            suggestion='Try "@daily".',
        )

    equivalent = MACROS[token]
    payload: dict[str, Any] = {
        "ok": True,
        "token": token,
        "meaning": MACRO_MEANINGS[token],
        "equivalent_posix_expression": equivalent,
        "scheduler_dependent": equivalent is None,
        "timezone": zone_name,
        "all_macros": [
            {
                "token": name,
                "equivalent": MACROS[name],
                "meaning": MACRO_MEANINGS[name],
            }
            for name in (
                "@yearly",
                "@annually",
                "@monthly",
                "@weekly",
                "@daily",
                "@midnight",
                "@hourly",
                "@reboot",
            )
        ],
    }

    if equivalent is None:
        payload["next_runs"] = []
        payload["note"] = (
            "@reboot is handled entirely by the scheduler. Vixie cron and cronie run "
            "it once at daemon start, systemd has no direct equivalent, and many "
            "hosted cron services ignore it completely. There is no next run time to "
            "compute, and a machine that never reboots never runs it."
        )
        summary = f"{token}: {MACRO_MEANINGS[token]}"
        return _render(summary, payload)

    parsed, schedule = build(equivalent, "posix")
    now = datetime.now(tz=zone)
    upcoming = next_runs(schedule, zone, now, 3)
    payload["description"] = describe(parsed).summary
    payload["next_runs"] = _occurrence_rows(upcoming.occurrences, now)

    summary = f'{token}: {MACRO_MEANINGS[token]} It is exactly the same as "{equivalent}".'
    return _render(summary, payload)


# --------------------------------------------------------------------------
# Tool registry
# --------------------------------------------------------------------------

_EXPRESSION_SCHEMA = {
    "type": "string",
    "description": 'The cron expression, for example "30 2 * * 1-5" or "@daily".',
}
_DIALECT_SCHEMA = {
    "type": "string",
    "enum": ["auto", "posix", "quartz", "aws"],
    "default": "auto",
    "description": (
        'Which cron flavour to read the expression as. "auto" decides from the '
        "field count: 5 fields is POSIX, 7 is Quartz, and 6 is Quartz unless the "
        "last field looks like a year, which makes it AWS EventBridge."
    ),
}
_TIMEZONE_SCHEMA = {
    "type": "string",
    "default": "UTC",
    "description": (
        "IANA timezone name the schedule is interpreted in, for example "
        '"America/New_York". Defaults to UTC.'
    ),
}

TOOLS: list[Tool] = [
    Tool(
        name="explain_cron",
        description=(
            "Turn a cron expression into plain English, with a per field breakdown. "
            "Use this whenever someone pastes a crontab line, asks what a schedule "
            "means, or is unsure how an expression will be interpreted. It states "
            "the detected dialect and calls out the day-of-month / day-of-week rule, "
            "which is the single most misunderstood part of cron."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "expression": _EXPRESSION_SCHEMA,
                "dialect": _DIALECT_SCHEMA,
                "timezone": _TIMEZONE_SCHEMA,
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="next_runs",
        description=(
            "Compute the next N times a cron expression will fire, as ISO 8601 "
            "timestamps with UTC offsets, plus a human readable delta. Use this for "
            '"when does this run next", for checking that a deploy window is clear, '
            "or for confirming that an expression fires when someone expects. "
            "Handles leap days and impossible schedules such as 30 February."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "expression": _EXPRESSION_SCHEMA,
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_RESULTS,
                    "default": 5,
                    "description": f"How many runs to return, 1 to {MAX_RESULTS}.",
                },
                "dialect": _DIALECT_SCHEMA,
                "timezone": _TIMEZONE_SCHEMA,
                "after": {
                    "type": "string",
                    "description": (
                        "ISO 8601 timestamp to start from. Defaults to now. A naive "
                        "timestamp is read in the given timezone."
                    ),
                },
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="previous_runs",
        description=(
            "Compute the most recent N times a cron expression fired, working "
            'backwards. Use this to answer "did my job run last night", to line a '
            "schedule up against log timestamps, or to work out which run a failure "
            "belongs to."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "expression": _EXPRESSION_SCHEMA,
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_RESULTS,
                    "default": 5,
                    "description": f"How many runs to return, 1 to {MAX_RESULTS}.",
                },
                "dialect": _DIALECT_SCHEMA,
                "timezone": _TIMEZONE_SCHEMA,
                "before": {
                    "type": "string",
                    "description": ("ISO 8601 timestamp to work backwards from. Defaults to now."),
                },
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="validate_cron",
        description=(
            "Check whether a cron expression is valid, and if it is not, say exactly "
            "what is wrong: the offending field, the character position, what was "
            "expected there, and a corrected suggestion when one is obvious. Use this "
            "before writing a crontab entry, or when a scheduler has rejected an "
            "expression and the message was unhelpful. Invalid input is a normal "
            "result here, not an error."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "expression": _EXPRESSION_SCHEMA,
                "dialect": _DIALECT_SCHEMA,
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="describe_schedule_stats",
        description=(
            "Frequency analysis for a schedule: runs per hour, day, week, month and "
            "year, the shortest and longest gap between consecutive runs, and flags "
            "for pathological schedules such as ones that fire every minute or less "
            "than once a year. Use this for capacity questions, for reviewing whether "
            "a schedule is more aggressive than intended, or for spotting a job that "
            "is so rare a mistake in it would go unnoticed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "expression": _EXPRESSION_SCHEMA,
                "dialect": _DIALECT_SCHEMA,
                "timezone": _TIMEZONE_SCHEMA,
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="check_dst_safety",
        description=(
            "Find the runs that daylight saving time will skip or duplicate. In a "
            "zone that changes its clocks, a job scheduled inside the spring forward "
            "gap never runs at all, and one inside the fall back window can run "
            "twice. Use this for any job scheduled between roughly midnight and 04:00 "
            "in a local timezone, and whenever someone reports a job that ran twice "
            "or silently did not run. Returns the exact affected instants for the "
            "year and a recommendation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "expression": _EXPRESSION_SCHEMA,
                "timezone": {
                    "type": "string",
                    "description": (
                        "IANA timezone name to analyze, for example "
                        '"America/New_York". Required, because this question only '
                        "means something in a specific zone."
                    ),
                },
                "year": {
                    "type": "integer",
                    "description": "Calendar year to analyze. Defaults to the current year.",
                },
                "dialect": _DIALECT_SCHEMA,
            },
            "required": ["expression", "timezone"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="compare_crons",
        description=(
            "Compare two cron expressions: their next runs side by side, any instants "
            "where they fire together, whether one is a subset of the other, and a "
            "plain English account of how they differ. Use this to check whether two "
            "jobs will collide on the same box, whether a rewritten expression really "
            "is equivalent to the old one, or how a proposed schedule change alters "
            "the cadence."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "expression_a": {"type": "string", "description": "The first cron expression."},
                "expression_b": {"type": "string", "description": "The second cron expression."},
                "dialect": _DIALECT_SCHEMA,
                "timezone": _TIMEZONE_SCHEMA,
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_RESULTS,
                    "default": 10,
                    "description": "How many runs of each to compare.",
                },
            },
            "required": ["expression_a", "expression_b"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="explain_special",
        description=(
            "Explain a cron macro: @yearly, @annually, @monthly, @weekly, @daily, "
            "@midnight, @hourly or @reboot. Returns the equivalent five field "
            "expression and the next few runs. Use this when a crontab uses a macro "
            "rather than five fields. @reboot is special: it is scheduler dependent "
            "and has no computable next run, and this tool says so plainly."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": 'The macro, with or without the @, for example "@daily".',
                },
                "timezone": _TIMEZONE_SCHEMA,
            },
            "required": ["token"],
            "additionalProperties": False,
        },
    ),
]

HANDLERS = {
    "explain_cron": tool_explain_cron,
    "next_runs": tool_next_runs,
    "previous_runs": tool_previous_runs,
    "validate_cron": tool_validate_cron,
    "describe_schedule_stats": tool_describe_schedule_stats,
    "check_dst_safety": tool_check_dst_safety,
    "compare_crons": tool_compare_crons,
    "explain_special": tool_explain_special,
}


def dispatch(name: str, arguments: dict[str, Any] | None) -> str:
    """Run one tool and return its text block. Never raises anything but ValueError."""
    handler = HANDLERS.get(name)
    if handler is None:
        raise ValueError(f'Unknown tool "{name}". Available tools: {", ".join(sorted(HANDLERS))}.')
    try:
        return handler(arguments or {})
    except CronError as error:
        detail = json.dumps({"ok": False, "error": error.to_dict()}, indent=2)
        raise ValueError(f"{error.format()}\n\n{detail}") from None
    except RecursionError:  # pragma: no cover - defensive
        raise ValueError("The expression was too complex to evaluate.") from None
    except Exception as error:
        raise ValueError(
            f"Cron Explainer could not complete {name}: {type(error).__name__}: {error}. "
            "This is a bug, please report it with the expression that caused it."
        ) from None


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

_INSTRUCTIONS = (
    "Cron Explainer turns cron expressions into plain English and into real "
    "timestamps. It understands POSIX, Quartz and AWS EventBridge syntax, and it "
    "is careful about the three things people get wrong: which dialect an "
    "expression is written in, the day-of-month / day-of-week rule (cron ORs "
    "them when both are restricted), and daylight saving time. Everything is "
    "computed locally. It makes no network calls."
)

server: Server = Server(SERVER_NAME, version=__version__, instructions=_INSTRUCTIONS)


@server.list_tools()  # type: ignore[no-untyped-call, misc]
async def handle_list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()  # type: ignore[no-untyped-call, misc]
async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
    return [TextContent(type="text", text=dispatch(name, arguments))]


async def serve() -> None:
    """Run the stdio server until the client disconnects."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


_USAGE = f"""cron-explainer {__version__}

An MCP server that explains cron expressions and computes their run times.
It speaks MCP over stdio, so it is normally started by an MCP client rather
than by hand.

  cron-explainer            start the stdio server
  cron-explainer --version  print the version
  cron-explainer --help     print this message

No configuration, no API keys, no network access.
"""


def main() -> None:
    """Console script entry point."""
    argv = sys.argv[1:]
    if argv:
        if argv[0] in ("-h", "--help"):
            sys.stdout.write(_USAGE)
            return
        if argv[0] in ("-V", "--version"):
            sys.stdout.write(f"{__version__}\n")
            return
        sys.stderr.write(f"Unknown option: {argv[0]}\n\n{_USAGE}")
        raise SystemExit(2)
    # A client disconnecting is a normal way for a stdio server to end.
    with contextlib.suppress(KeyboardInterrupt, BrokenPipeError):
        asyncio.run(serve())


if __name__ == "__main__":  # pragma: no cover
    main()
