"""The session container (save format v6, v0.14; v5 games are still read).

It holds what the *application* persists next to the world: bindings, flags shown to the user, the
spending limit, the table of the game (the bots in their seats: names, avatars and - once the game has started - the
snapshot of what each bot carried into it), the serialized world / usage, and (v0.13) who - if anybody - plays a
seat instead of its agent.  The live game objects (``World``, ``Supervisor`` ...) are owned by the orchestrator; ``world`` /
``usage_stats`` here are refreshed right before every save.

v4 -> v5 (docs/v0.13-plan.md F10, F22): ``takeover``, ``human_spans``, ``created_at`` / ``started_at`` /
``finished_at``.  Games of v0.12.1 (v4) are converted with ``tools/migrate_v0121_to_archive.py``.

v5 -> v6 (docs/v0.14-plan.md H3, H11, H52, H58; contract section 13.3): a seat is *where a bot sits in this game*.
``Participant.bot_id`` names the bot, ``Participant.memory_snapshot`` is what it carried into the game
(``BotMind.to_dict()``, taken when the game started, so a resumed game builds the very same prompts), ``GameSession.lineup``
is the bot ids in seat order and ``GameSession.contract_snapshot`` the official contract of the game.  Identities are dealt
when the game starts, so an idle v6 game has none.  A v5 game keeps ``schema_version == 5`` while it lives: its seats have
no bot (``bot_id == ""``), its ``persona`` / ``memory`` texts are used as they were saved, it plays on with the v0.13.1
brain and cannot be reviewed (:attr:`GameSession.legacy`).
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

SESSION_SCHEMA_VERSION = 6
#: The save formats this engine can open: v6, and v5 (v0.13) - which stays a v5 game while it lives (H58).
SUPPORTED_SCHEMA_VERSIONS = (5, 6)
APP_ENGINE_VERSION = "v0.14.0"
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

__all__ = [
    "APP_ENGINE_VERSION",
    "MODERATOR_ID",
    "SESSION_ID_PATTERN",
    "SESSION_SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "BindingIssue",
    "GameSession",
    "HumanSpan",
    "IncompatibleSessionError",
    "Participant",
    "Takeover",
    "utc_now_iso",
]


def utc_now_iso() -> str:
    """The current time as ISO-8601 UTC text (``created_at`` / ``started_at`` / ``finished_at`` of a game)."""
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
    """A seat of this game: ``config.yml`` lists the seats, the bot that sits there (v0.14) gives the name / avatar.
    Runtime state (tags, roles) lives in the world.

    v6: ``bot_id`` is the bot in the seat (the table is :attr:`GameSession.lineup`), ``memory_snapshot`` what it carries into
    the game - empty until the game starts.  v5: ``bot_id == ""``; ``persona`` / ``memory`` hold the texts the seat played
    with (in a v6 game they stay empty: the bot's own layers are in ``memory_snapshot``)."""

    id: str
    seat: str
    name: str
    persona: str = ""
    memory: str = ""  # v5: long-term memory as of the start of this game (empty when memory is off)
    avatar: dict[str, Any] | None = None
    bot_id: str = ""  # v6 (H3)
    memory_snapshot: dict[str, Any] = Field(default_factory=dict)  # v6 (H11): ``BotMind.to_dict()`` + the hashes of the contract / scenario


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
    created_at: str | None = Field(default_factory=utc_now_iso)  # ISO-8601 UTC; when the game was created
    started_at: str | None = None  # when it was first started
    finished_at: str | None = None  # when it last ended (cleared when it is continued)

    # v0.14 (H3, H7; contract 13.3)
    lineup: list[str] = Field(default_factory=list)  # the bot ids in seat order
    contract_snapshot: dict[str, Any] = Field(default_factory=dict)  # {"version", "sha256", "markdown"}: the contract the game started with

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
        if value not in SUPPORTED_SCHEMA_VERSIONS:
            raise IncompatibleSessionError(
                f"该存档来自不同版本的引擎（存档格式 v{value}，当前为 v{SESSION_SCHEMA_VERSION}），"
                "v0.14 只能打开 v5（v0.13）和 v6 的存档；v0.12.1 的对局请用 tools/migrate_v0121_to_archive.py 转换后导入。"
            )
        return value

    @property
    def legacy(self) -> bool:
        """A v5 game (H58): its seats have no bot - it plays on with the persona / memory texts it was saved with and cannot be
        reviewed.  (A v6 game always has bots.)"""
        return int(self.schema_version) < 6

    def get_model_binding(self, actor_id: str) -> ModelBinding | None:
        """The binding of a participant (or of ``"moderator"``), if one is set."""
        return self.model_bindings.get(actor_id)
