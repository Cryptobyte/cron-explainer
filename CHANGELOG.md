# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-07-24

First release.

### Added

- Eight MCP tools over stdio: `explain_cron`, `next_runs`, `previous_runs`,
  `validate_cron`, `describe_schedule_stats`, `check_dst_safety`,
  `compare_crons` and `explain_special`.
- Three dialects with auto detection: POSIX (5 fields), Quartz (6 or 7 fields
  with seconds first and an optional year) and AWS EventBridge (6 fields ending
  in a year).
- The Vixie day-of-month / day-of-week rule, stated explicitly in the English
  output whenever the OR case applies.
- Quartz extensions: `?`, `L`, `L-n`, `LW`, `nW` and `#`.
- Daylight saving analysis: the exact local times a spring forward skips and a
  fall back repeats, for any IANA zone and any year, including half hour shifts.
- A date jumping occurrence engine, so a leap day four years out resolves in
  under a millisecond rather than scanning two million minutes.
- Detection of schedules that can never fire, such as `0 0 30 2 *`, by bounded
  search rather than an unbounded loop.
- Errors carrying the field, the character position, what was expected, and a
  suggested fix.
