#!/usr/bin/env python3
"""Entrypoint shim.

The real server lives in ``src/cron_explainer/server.py``. This file exists so
that the package can be started with ``python server.py`` from a clone, and so
that tooling which auto-detects a Python entrypoint finds one at the repo root.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from cron_explainer.server import main
except ModuleNotFoundError:  # running from a clone without installing
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from cron_explainer.server import main


if __name__ == "__main__":
    main()
