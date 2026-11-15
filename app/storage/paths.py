from __future__ import annotations

import os
import platform
from pathlib import Path

_APP_DIR_NAME = "Arena-Agent"


def _get_app_dir() -> Path:
    """Return the v0.10 application data directory without creating it."""
    override = os.environ.get("ARENA_DATA_DIR")
    if override and override.strip():
        return Path(override).expanduser().resolve()

    system = platform.system()
    if system == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        root = (
            Path(local_app_data).expanduser()
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
    elif system == "Darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        xdg_data_home = os.environ.get("XDG_DATA_HOME")
        root = (
            Path(xdg_data_home).expanduser()
            if xdg_data_home
            else Path.home() / ".local" / "share"
        )
    return (root / _APP_DIR_NAME).resolve()


def get_data_dir() -> Path:
    """Return active workspace data, or the legacy app directory in compat mode."""
    from app.storage.workspace import (
        get_active_workspace_data_dir,
        workspace_enabled,
    )

    if workspace_enabled():
        active_data_dir = get_active_workspace_data_dir()
        if active_data_dir is not None:
            return active_data_dir
    return _get_app_dir()
