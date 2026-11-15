"""Upgrade older saved-session payloads (v0.7 – v0.9, schema 1) to the current schema.

The migration only reshapes data: it never invents provider bindings (a v1 save has none, so the
game comes up as "needs rebinding" unless the user's saved default bindings cover it) and never
touches anything secret.
"""

from __future__ import annotations

import copy
from typing import Any

from app.engine.state import APP_ENGINE_VERSION, SESSION_SCHEMA_VERSION


class UnsupportedSessionVersion(ValueError):
    """The payload was written by a newer engine than this one."""


def migrate_session_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Return a schema-v2 copy of *data* (the input is never mutated)."""
    payload = copy.copy(data)  # top-level keys only; nested values are re-created below
    version = payload.get("schema_version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("schema_version 必须是整数")
    if version > SESSION_SCHEMA_VERSION:
        raise UnsupportedSessionVersion(
            f"存档的 schema_version={version} 高于本程序支持的 {SESSION_SCHEMA_VERSION}，请升级程序后再导入"
        )
    if version < 2:
        payload["schema_version"] = SESSION_SCHEMA_VERSION
        payload["version"] = APP_ENGINE_VERSION
        payload["engine_version"] = APP_ENGINE_VERSION
        # v1 saves had one global model: no per-actor bindings, no per-model accounting.
        payload.setdefault("model_bindings", {})
        usage = payload.get("usage_stats")
        if isinstance(usage, dict):
            usage = dict(usage)
            usage.setdefault("by_model", {})
            usage.setdefault("by_actor", {})
            payload["usage_stats"] = usage
    # Transient fields are recomputed for the loaded game, never trusted from the file.
    payload.pop("binding_issues", None)
    payload.pop("autosave_error", None)
    return payload
