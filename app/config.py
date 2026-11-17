from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

APP_VERSION = "0.12.1"

BASE_DIR = Path(__file__).resolve().parent.parent

_ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


def _interpolate_env(value: Any) -> Any:
    """Recursively interpolate ${VAR:-default} in YAML values."""
    if isinstance(value, str):
        def replacer(match: re.Match[str]) -> str:
            var_name = match.group(1)
            default_val = match.group(2) if match.group(2) is not None else ""
            return os.environ.get(var_name, default_val)

        return _ENV_PATTERN.sub(replacer, value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(item) for item in value]
    return value


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    """Load config.yml and interpolate environment variables."""
    target_path = config_path or (BASE_DIR / "config.yml")
    if not target_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {target_path}")
    with open(target_path, "r", encoding="utf-8") as f:
        raw_cfg = yaml.safe_load(f) or {}
    return _interpolate_env(raw_cfg)


def load_prompt_template(relative_path: str) -> str:
    """Read a markdown prompt template from the workspace."""
    full_path = BASE_DIR / relative_path
    if not full_path.exists():
        return ""
    return full_path.read_text(encoding="utf-8")
