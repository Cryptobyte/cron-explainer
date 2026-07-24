# Cron Explainer

An MCP server that turns a cron expression into plain English and into real timestamps. It reads POSIX, Quartz and AWS EventBridge syntax, states which dialect it used, and is deliberately rigorous about the three things people get wrong: the day-of-month / day-of-week rule (cron ORs those two fields, it does not AND them), daylight saving time (jobs that silently never run, and jobs that quietly run twice), and impossible schedules such as 30 February. It is pure computation with no dependencies beyond the MCP SDK itself, and it never opens a network connection.

---

## Tools

| Tool | What it does |
| --- | --- |
| `explain_cron` | Describes an expression in plain English, with a per field breakdown and the detected dialect. |
| `next_runs` | The next N run times as ISO 8601 with offsets, plus a human delta ("in 3 hours 12 minutes"). |
| `previous_runs` | The same in reverse, for "did my job run last night". |
| `validate_cron` | Valid or not, and on failure the offending field, the character position, what was expected, and a fix. |
| `describe_schedule_stats` | Runs per hour, day, week, month and year, shortest and longest gap, and flags for pathological schedules. |
| `check_dst_safety` | The runs a clock change skips or duplicates in a given year, with the exact instants and a recommendation. |
| `compare_crons` | Two expressions side by side: coinciding runs, subset relationships, and how they differ in English. |
| `explain_special` | The macros: `@yearly`, `@annually`, `@monthly`, `@weekly`, `@daily`, `@midnight`, `@hourly` and `@reboot`. |

Every tool answers with a short readable sentence first, then a pretty printed JSON object with the structured data.

---

## Requirements

- Python 3.11 or newer
- No API keys
- No configuration
- No network access
- No environment variables

The only runtime dependency is the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk). Everything else is the standard library: `zoneinfo`, `datetime`, `calendar`, `re` and `dataclasses`. The cron engine is implemented here rather than pulled from `croniter` or `dateutil`, which keeps the supply chain empty and the behaviour inspectable.

On Windows, `zoneinfo` has no system timezone database, so install `tzdata` alongside it (`pip install tzdata`). Linux and macOS already have one.

---

## Install and run

Pick whichever suits you. `uvx` needs no install step at all:

```bash
uvx cron-explainer
```

```bash
pipx install cron-explainer
cron-explainer
```

```bash
git clone https://github.com/cryptobyte/cron-explainer
cd cron-explainer
pip install -e .
cron-explainer
```

The server speaks MCP over stdio, so running it by hand just leaves it waiting for a client on stdin. That is expected. Use `cron-explainer --version` to check the install.

---

## Client configuration

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "cron-explainer": {
      "command": "uvx",
      "args": ["cron-explainer"]
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add cron-explainer -- uvx cron-explainer
```

**Cursor** (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "cron-explainer": {
      "command": "uvx",
      "args": ["cron-explainer"]
    }
  }
}
```

**VS Code** (`.vscode/mcp.json`):

```json
{
  "servers": {
    "cron-explainer": {
      "type": "stdio",
      "command": "uvx",
      "args": ["cron-explainer"]
    }
  }
}
```

If you installed with `pipx` or `pip` instead of using `uvx`, replace the command with `cron-explainer` and drop the `args`.

---

## What it talks to

Nothing.

There are no outbound network calls, no telemetry, no analytics, no crash reporting, no credentials, no API keys, and no config files. The server reads from stdin and writes to stdout. It never touches the filesystem. Its declared egress allowlist is empty because there is genuinely nothing on it, and CI has a job that fails the build if a networking import ever appears under `src/`.

Given a cron string and a timezone name, the answer is arithmetic. There is nothing to phone home about.

---

## Worked examples

### The day-of-month rule, which is the one that bites

`0 0 13 * FRI` does **not** mean "midnight on Friday the 13th". When both the day-of-month and day-of-week fields are restricted, cron ORs them.

Ask `explain_cron` about `0 0 13 * FRI`:

```
At 00:00, on the 13th of the month or on Friday. Dialect: posix. Timezone: UTC.
Note the day-of-month OR day-of-week rule below.
```

```json
{
  "ok": true,
  "expression": "0 0 13 * FRI",
  "dialect": { "name": "posix", "description": "POSIX / Vixie cron, 5 fields, Sunday is 0 (7 also accepted)" },
  "description": {
    "summary": "At 00:00, on the 13th of the month or on Friday",
    "day_rule": "or",
    "day_rule_note": "Day-of-month and day-of-week are both restricted, so cron fires when EITHER matches. This is a union, not an intersection: the two conditions are ORed. That is the rule that surprises people.",
    "fields": [
      { "field": "minute",       "raw": "0",   "expanded": "0",             "meaning": "at minute 0" },
      { "field": "hour",         "raw": "0",   "expanded": "0",             "meaning": "00:00" },
      { "field": "day-of-month", "raw": "13",  "expanded": "13",            "meaning": "on the 13th of the month" },
      { "field": "month",        "raw": "*",   "expanded": "JAN-DEC (all)", "meaning": "every month" },
      { "field": "day-of-week",  "raw": "FRI", "expanded": "FRI",           "meaning": "on Friday" }
    ]
  }
}
```

In August 2026 that expression fires on the 7th, 13th, 14th, 21st and 28th: every Friday, plus the 13th, which is a Thursday. Five runs, not one. If you actually want Friday the 13th, cron cannot express it and you have to test the date inside the job.

When one of the two day fields is `*`, the other simply applies, and the tool says so instead.

### Daylight saving, the runs you never notice missing

Ask `check_dst_safety` about `30 2 * * *` in `America/New_York` for 2025:

```
In America/New_York during 2025, 1 run is skipped when the clocks go forward
(2025-03-09 02:30:00). The skipped runs never happen at all. Move the job outside
the transition window (for example to 05:30 local time), or schedule it in UTC.
```

```json
{
  "safe": false,
  "observes_dst": true,
  "transitions": [
    {
      "kind": "forward",
      "instant_utc": "2025-03-09T07:00:00Z",
      "offset_before": "-05:00",
      "offset_after": "-04:00",
      "shift_minutes": 60,
      "affected_local_window": {
        "start": "2025-03-09T02:00:00",
        "end": "2025-03-09T03:00:00",
        "note": "These wall clock times never happen."
      }
    }
  ],
  "skipped_runs": [
    {
      "local_time": "2025-03-09T02:30:00",
      "kind": "skipped",
      "explanation": "This local time does not exist on this date, so the job never runs."
    }
  ]
}
```

The mirror image is `30 1 * * *`, which on 2 November 2025 has two 01:30s, one at `05:30Z` and one at `06:30Z`, so the job may run twice. The tool returns both instants and suggests making the job idempotent.

A schedule expressed in UTC is immune to both, because UTC never changes offset. The tool says so every time.

### Leap days and impossible schedules

`0 0 29 2 *` is over two million minutes away at its furthest. It resolves in under a millisecond, because the engine walks candidate dates rather than candidate minutes:

```json
{ "runs": [{ "local": "2028-02-29T00:00:00+00:00" }, { "local": "2032-02-29T00:00:00+00:00" }] }
```

It also gets the century rule right: after 2096 the next one is 2104, because 2100 is not a leap year.

`0 0 30 2 *` is a valid expression that can never fire. Rather than searching forever, the tool says so:

```json
{ "never_runs": true, "reason": "Day 30 never occurs in February, so this expression can never fire." }
```

### Errors that teach

`validate_cron` on `0 0 32 * *`:

```
Not valid. Value "32" is out of range for the day-of-month field.
```

```json
{
  "valid": false,
  "error": {
    "message": "Value \"32\" is out of range for the day-of-month field.",
    "field": "day-of-month",
    "position": 4,
    "expected": "1-31",
    "pointer": "0 0 32 * *\n    ^"
  }
}
```

A reversed range like `5-1` is rejected with the corrected form. `SUNDAY` suggests `SUN`. Using `L` or `?` in a POSIX expression explains that those are Quartz extensions. Invalid input is a normal result from `validate_cron`, not an error, so an assistant can show it to you directly.

---

## Dialects

| Dialect | Fields | Layout | Sunday |
| --- | --- | --- | --- |
| `posix` | 5 | minute hour day-of-month month day-of-week | `0` (and `7`) |
| `quartz` | 6 or 7 | second minute hour day-of-month month day-of-week [year] | `1` |
| `aws` | 6 | minute hour day-of-month month day-of-week year | `1` |

`dialect` defaults to `auto`, which decides from the field count. Six fields are ambiguous between Quartz and AWS, so the tool looks at the last field for a four digit year and at which position holds a `?`, then reports what it concluded in every response.

Supported everywhere: `*`, single values, lists (`1,2,3`), ranges (`1-5`), steps (`*/5`, `1-30/2`, `0/15`), and case-insensitive three letter names for months (`JAN`-`DEC`) and weekdays (`SUN`-`SAT`).

Quartz and AWS additionally support `?` (no specific value, and exactly one of the two day fields must have it), `L` (last day of the month, or `6L` for the last Friday), `L-3` (three days before the end of the month), `LW` (last weekday), `15W` (the weekday nearest the 15th, never crossing a month boundary) and `FRI#3` (the third Friday).

One documented behaviour worth knowing: the day rule follows Vixie cron exactly. Vixie sets its "unrestricted" flag from the *first character* of the field, so `*/2` counts as unrestricted and switches the two day fields from OR back to AND. `1-31`, which covers the same days written out longhand, does not.

---

## Development

```bash
git clone https://github.com/cryptobyte/cron-explainer
cd cron-explainer
pip install -e ".[dev]"
pytest
ruff check .
mypy
```

The parser, the schedule engine and the English renderer are independent and do no I/O, so they are testable on their own. The suite covers every field syntax, all three dialects, the OR rule, leap years and the century rule, the Quartz extensions, impossible schedules, DST skips and repeats in both hemispheres and across a 30 minute shift, and the performance guarantee.

## License

MIT. See [LICENSE](LICENSE).
