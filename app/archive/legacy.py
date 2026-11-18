"""The "old data found" notice (decision F24).

A v0.12.1 workspace lives in ``~/Arena-Agents/Default`` (Windows, macOS) or ``~/.local/share/Arena-Agent/workspace/default``
(Linux).  v0.13 never touches it: when the new data directory is still empty and such a workspace exists, the page shows
"old data found - run the conversion script, then import" with a "dismiss" button that is remembered in the settings.
"""

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

WORKSPACE_MARKER = ".arena-workspace.json"
WORKSPACE_DATA_DIRNAME = ".arena"
DISMISS_PREF = "legacy_data_dismissed"


def _home() -> Path:
    return Path.home()


def default_legacy_path() -> Path:
    """Where v0.12.1 put its default workspace on this platform (the folder need not exist)."""
    if platform.system() in ("Windows", "Darwin"):
        return _home() / "Arena-Agents" / "Default"
    configured = os.environ.get("XDG_DATA_HOME", "").strip()
    root = Path(configured).expanduser() if configured else _home() / ".local" / "share"
    return root / "Arena-Agent" / "workspace" / "default"


def detect_legacy_workspace() -> Path | None:
    """The default v0.12.1 workspace when it exists (marker file and ``.arena`` folder present), else ``None``."""
    try:
        path = default_legacy_path()
        marker = path / WORKSPACE_MARKER
        data = path / WORKSPACE_DATA_DIRNAME
        if path.is_symlink() or marker.is_symlink() or data.is_symlink():
            return None
        if marker.is_file() and data.is_dir():
            return path
    except (OSError, RuntimeError):  # no home directory, unreadable folder ...
        logger.debug("Could not look for a v0.12.1 workspace", exc_info=True)
    return None


def data_dir_is_empty(data_dir: Path) -> bool:
    """No ``providers.json`` and no game under ``games/``: the new data directory holds nothing of the user's yet."""
    if (data_dir / "providers.json").exists():
        return False
    games = data_dir / "games"
    try:
        return not games.exists() or not any(games.iterdir())
    except OSError:
        return False


def is_dismissed() -> bool:
    from app.storage.settings import get_ui_pref

    try:
        return bool(get_ui_pref(DISMISS_PREF, False))
    except Exception:  # noqa: BLE001 - an unreadable settings file must not break /api/state
        return False


def dismiss() -> None:
    """Remember that the user does not want the notice (stored in ``settings.json`` under ``ui``)."""
    from app.storage.settings import set_ui_pref

    set_ui_pref(DISMISS_PREF, True)


def legacy_data_state(request: Any = None, *, include_path: bool | None = None) -> dict[str, Any]:
    """``{"found": bool, "dismissed": bool, "path"?: str}`` for ``GET /api/state``.

    ``found`` only while the data directory is empty; ``path`` (where the old workspace is) only for a request from this
    machine.  Never raises.
    """
    from app.storage.paths import get_data_dir

    state: dict[str, Any] = {"found": False, "dismissed": is_dismissed()}
    try:
        if not data_dir_is_empty(get_data_dir()):
            return state
        found = detect_legacy_workspace()
    except Exception:  # noqa: BLE001
        logger.debug("Legacy data check failed", exc_info=True)
        return state
    if found is None:
        return state
    state["found"] = True
    if include_path is None:
        if request is None:
            include_path = True
        else:
            from app.api.security import is_local_request

            include_path = is_local_request(request)
    if include_path:
        state["path"] = str(found)
    return state
