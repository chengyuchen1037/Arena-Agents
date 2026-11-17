"""The v0.12 session container (save format v3).

It holds what the *application* persists next to the world: bindings, flags shown to the user and the
serialized world / usage.  The live game objects (``World``, ``Supervisor`` ...) are owned by the
orchestrator; ``world`` / ``usage_stats`` here are refreshed right before every save.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.providers.models import ModelBinding
from app.world.types import MODERATOR_ID

SESSION_SCHEMA_VERSION = 3
APP_ENGINE_VERSION = "v0.12"
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

__all__ = [
    "APP_ENGINE_VERSION",
    "MODERATOR_ID",
    "SESSION_ID_PATTERN",
    "SESSION_SCHEMA_VERSION",
    "BindingIssue",
    "GameSession",
    "IncompatibleSessionError",
    "Participant",
]


class IncompatibleSessionError(ValueError):
    """A stored game was written by an older (or newer) engine and cannot be opened (D3)."""


class Participant(BaseModel):
    """A seat as configured in ``config.yml``.  Runtime state (tags, roles) lives in the world."""

    id: str
    seat: str
    name: str
    persona: str = ""


class BindingIssue(BaseModel):
    actor_id: str
    message: str


class GameSession(BaseModel):
    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    schema_version: int = SESSION_SCHEMA_VERSION
    version: str = APP_ENGINE_VERSION
    engine_version: str = APP_ENGINE_VERSION
    title: str = "Arena-Agents"
    scenario_name: str = ""
    scenario_title: str = ""
    status: Literal["idle", "running", "paused", "finished"] = "idle"
    pause_reason: str = ""  # user | hold | error | stall | ""
    hold_reason: str = ""
    execution_depth: Literal["low", "medium", "high", "xhigh"] = "medium"

    participants: list[Participant] = Field(default_factory=list)
    model_bindings: dict[str, ModelBinding] = Field(default_factory=dict)
    binding_issues: list[BindingIssue] = Field(default_factory=list)

    restored_from_save: bool = False
    restore_notice: str = ""
    autosave_error: str = ""

    scenario_spec: dict[str, Any] = Field(default_factory=dict)
    world: dict[str, Any] = Field(default_factory=dict)
    usage_stats: dict[str, Any] = Field(default_factory=dict)
    tag_styles: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        # session_id is embedded in save-file names: never let it carry separators or dots.
        if not isinstance(value, str) or not SESSION_ID_PATTERN.fullmatch(value):
            raise ValueError("session_id must match ^[A-Za-z0-9_-]{1,64}$")
        return value

    @field_validator("schema_version")
    @classmethod
    def _validate_schema(cls, value: int) -> int:
        if value != SESSION_SCHEMA_VERSION:
            raise IncompatibleSessionError(
                f"该存档来自不同版本的引擎（存档格式 v{value}，当前为 v{SESSION_SCHEMA_VERSION}），"
                "v0.12 不兼容旧版本存档。"
            )
        return value

    def get_model_binding(self, actor_id: str) -> ModelBinding | None:
        """The binding of a participant (or of ``"moderator"``), if one is set."""
        return self.model_bindings.get(actor_id)
