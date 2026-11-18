"""Where the application keeps its data (v0.13, decisions F20 / F37).

There is exactly one data directory and no "workspace" to choose.  The resolution order is:

1. ``ARENA_DATA_DIR`` (non-empty): tests, development instances, Docker volumes that live elsewhere;
2. demo mode (``ARENA_DEMO``): ``<program dir>/.demo-data/demo``, so scripted demo games never mix into the
   real history;
3. ``<program dir>/data``.

The *program directory* is ``app.config.BASE_DIR`` (the folder that holds ``app/`` and ``config.yml``); it does
not depend on the current working directory.  Reading the location never creates anything: see
:func:`app.storage.datadir.ensure_data_dir` for that.
"""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIRNAME = "data"
DEMO_DATA_DIRNAME = ".demo-data"
DEMO_DATA_SUBDIR = "demo"

_FALSE_VALUES = frozenset({"", "0", "false", "no"})


def demo_mode_enabled() -> bool:
    """True when ``ARENA_DEMO`` asks for the scripted-bot demo (anything but empty / 0 / false / no)."""
    return os.environ.get("ARENA_DEMO", "").strip() not in _FALSE_VALUES


def get_base_dir() -> Path:
    """The program directory.  ``app.config`` is read at call time: tests point ``BASE_DIR`` at a temporary project."""
    import app.config

    return Path(app.config.BASE_DIR)


def get_data_dir() -> Path:
    """The data directory of this process (resolved, not created)."""
    override = os.environ.get("ARENA_DATA_DIR")
    if override and override.strip():
        return Path(override).expanduser().resolve()
    base = get_base_dir()
    if demo_mode_enabled():
        return (base / DEMO_DATA_DIRNAME / DEMO_DATA_SUBDIR).resolve()
    return (base / DATA_DIRNAME).resolve()
