"""The session container (save format v5, v0.13).

It holds what the *application* persists next to the world: bindings, flags shown to the user, the
spending limit, the roster snapshot of the game (names, avatars, persona / memory as they were when it
started), the serialized world / usage, and (v0.13) who - if anybody - plays a seat instead of its agent.
The live game objects (``World``, ``Supervisor`` ...) are owned by the orchestrator; ``world`` /
``usage_stats`` here are refreshed right before every save.

v4 -> v5 (docs/v0.13-plan.md F10, F22): ``takeover``, ``human_spans``, ``created_at`` / ``started_at`` /
``finished_at``.  Games of v0.12.1 (v4) are converted with ``tools/migrate_v0121_to_archive.py``.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.game.budget import BudgetSettings
from app.providers.models import ModelBinding
from app.world.types import MODERATOR_ID

SESSION_SCHEMA_VERSION = 5
APP_ENGINE_VERSION = "v0.12.1"
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

__all__ = [
    "APP_ENGINE_VERSION",
    "MODERATOR_ID",
    "SESSION_ID_PATTERN",
    "SESSION_SCHEMA_VERSION",
    "BindingIssue",
    "GameSession",
    "HumanSpan",
    "IncompatibleSessionError",
    "Participant",
    "Takeover",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Takeover(BaseModel):
    """Who plays a seat instead of its agent (F2, F10): ``seat`` is ``None`` while nobody does."""

    seat: str | None = None


class HumanSpan(BaseModel):
    """A stretch of the ledger during which a person played ``seat`` (F22): ``from_seq`` is the world's
    ``seq`` when the takeover began, ``to_seq`` the ``seq`` when it ended (``None`` = still taken over)."""

    seat: str
    from_seq: int = 0
    to_seq: int | None = None


class IncompatibleSessionError(ValueError):
    """A stored game was written by an older (or newer) engine and cannot be opened (D3)."""


class Participant(BaseModel):
    """A seat of this game: ``config.yml`` lists the seats, the roster fills in name / avatar / persona /
    memory when the game is created.  Runtime state (tags, roles) lives in the world."""

    id: str
    seat: str
    name: str
    persona: str = ""
    memory: str = ""  # long-term memory as of the start of this game (empty when memory is off)
    avatar: dict[str, Any] | None = None


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
    pause_reason: str = ""  # user | hold | budget | "" (error / stall are accepted and have UI texts, but nothing sets them)
    hold_reason: str = ""

    participants: list[Participant] = Field(default_factory=list)
    model_bindings: dict[str, ModelBinding] = Field(default_factory=dict)
    binding_issues: list[BindingIssue] = Field(default_factory=list)

    restored_from_save: bool = False
    restore_notice: str = ""
    autosave_error: str = ""

    # v0.12.1
    budget: BudgetSettings = Field(default_factory=BudgetSettings)  # the user's limit for THIS game (E22)
    memory_enabled: bool = True  # whether the agents were given their memory at the start of this game
    moderator_avatar: dict[str, Any] | None = None  # the moderator's random pixel avatar, fixed for this game
    review_done: list[str] = Field(default_factory=list)  # seats whose post-game review has been written (E21)

    # v0.13 (F10, F22)
    takeover: Takeover = Field(default_factory=Takeover)  # the seat a person plays right now, if any
    human_spans: list[HumanSpan] = Field(default_factory=list)  # every stretch a person played a seat
    created_at: str | None = Field(default_factory=_utc_now_iso)  # ISO-8601 UTC; when the game was created
    started_at: str | None = None  # when it was first started
    finished_at: str | None = None  # when it last ended (cleared when it is continued)

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
                "v0.13 不能直接打开；v0.12.1 的对局请用 tools/migrate_v0121_to_archive.py 转换后导入。"
            )
        return value

    def get_model_binding(self, actor_id: str) -> ModelBinding | None:
        """The binding of a participant (or of ``"moderator"``), if one is set."""
        return self.model_bindings.get(actor_id)
