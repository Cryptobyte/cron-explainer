"""Cron Explainer: turn cron expressions into plain English and real timestamps.

Pure computation, standard library only. Nothing here opens a socket, reads a
configuration file, or writes to disk.
"""

from __future__ import annotations

from cron_explainer.describe import describe, human_duration
from cron_explainer.dst import DstReport, analyze_dst
from cron_explainer.errors import CronError
from cron_explainer.parser import (
    DIALECT_NAMES,
    MACROS,
    ParsedExpression,
    ParsedField,
    detect_dialect,
    parse,
)
from cron_explainer.schedule import (
    CronSchedule,
    Occurrence,
    RunList,
    ScheduleStats,
    build,
    compute_stats,
    next_runs,
    previous_runs,
)

__version__ = "1.0.0"

__all__ = [
    "DIALECT_NAMES",
    "MACROS",
    "CronError",
    "CronSchedule",
    "DstReport",
    "Occurrence",
    "ParsedExpression",
    "ParsedField",
    "RunList",
    "ScheduleStats",
    "__version__",
    "analyze_dst",
    "build",
    "compute_stats",
    "describe",
    "detect_dialect",
    "human_duration",
    "next_runs",
    "parse",
    "previous_runs",
]
