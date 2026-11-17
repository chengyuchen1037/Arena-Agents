"""Frozen v0.12 contract: the serializable state of the world (everything except the ledger).

All times are **game-clock seconds** (absolute, ``Clock.now()``).  These models are shared by every
world module, so field names/semantics must not change without updating docs/v0.12-contracts.md.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.world.types import AgentMemory

ChannelMode = Literal["closed", "turn", "queue", "free", "listen"]
ChannelType = Literal["public", "group", "dm"]


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlayerState(_S):
    id: str
    seat: str
    name: str
    persona: str = ""
    role: str = ""  # set by dealing (opaque to the engine)
    faction: str = ""  # set by dealing (opaque to the engine)
    tags: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class Token(_S):
    """One player's permission to speak in a ``turn`` / ``queue`` floor."""

    holder: str
    max_messages: int
    max_chars_per_message: int
    max_chars_total: int
    messages_used: int = 0
    chars_used: int = 0
    # pending: waiting in a queue; active: may speak now; done: finished (see ``ended_reason``)
    status: Literal["pending", "active", "done"] = "active"
    ended_reason: str = ""  # used | pass | timeout | revoked
    duration_s: float = 120.0
    deadline: float | None = None  # absolute game seconds while active and not paused
    remaining: float | None = None  # seconds left, only while the group is paused / frozen
    started_gt: float | None = None


class FloorGroup(_S):
    """A set of tokens handed out by one ``open_floor(turn)`` or ``set_floor_queue`` call."""

    id: str
    channel_id: str
    mode: Literal["turn", "queue"]
    tokens: list[Token]
    paused: bool = False  # queue paused by the moderator or frozen by an ability
    completed: bool = False
    created_gt: float = 0.0


class FreeState(_S):
    """Parameters and counters of a free discussion (advisories only notify the moderator, D20)."""

    writers: list[str] | None = None  # None = every non-readonly member
    max_messages_per_member: int
    max_chars_per_message: int
    max_chars_total: int
    duration_s: float
    debounce_s: float
    silence_s: float
    started_gt: float = 0.0
    counts: dict[str, int] = Field(default_factory=dict)
    chars_used: int = 0
    last_message_gt: float = 0.0
    notified: dict[str, bool] = Field(default_factory=dict)  # keys: time | chars | silence


class ChannelState(_S):
    id: str
    name: str
    type: ChannelType = "group"
    mode: ChannelMode = "closed"
    members: list[str] = Field(default_factory=list)  # the moderator is always included
    readonly: list[str] = Field(default_factory=list)
    frozen: bool = False  # set by an ability (D25); cleared by the moderator re-opening / closing
    system: bool = False  # engine-created per-player DM
    owner: str | None = None  # the player a system DM belongs to
    group: FloorGroup | None = None
    free: FreeState | None = None


class RequestAnswer(_S):
    value: str
    reason: str = ""
    defaulted: bool = False
    seq: int = 0  # ledger seq of the ``request.answered`` entry (0 for defaulted answers)


class RequestState(_S):
    id: str
    to: list[str]
    kind: Literal["choice", "text"]
    question: str
    options: list[dict[str, str]] = Field(default_factory=list)  # [{"id","label"}]
    sealed: bool = False
    default: str | None = None
    deadline: float | None = None
    status: Literal["open", "closed", "cancelled"] = "open"
    answers: dict[str, RequestAnswer] = Field(default_factory=dict)
    opened_seq: int = 0
    reopens: str | None = None
    created_gt: float = 0.0


class TicketState(_S):
    id: str
    player_id: str
    status: Literal["open", "resolved"] = "open"
    verdict: Literal["upheld", "rejected"] | None = None
    opened_seq: int = 0
    ref_seq: int | None = None
    preview: str = ""


class AbilityState(_S):
    id: str
    enabled: bool = False
    allowed: list[str] | None = None  # None = every holder allowed by the scenario expression
    uses: dict[str, int] = Field(default_factory=dict)  # player id -> times used


class DashboardState(_S):
    factions: list[dict[str, Any]] = Field(default_factory=list)
    tag_stats: list[dict[str, Any]] = Field(default_factory=list)


class RngState(_S):
    seed: int = 0
    state: list[Any] | None = None  # ``random.Random.getstate()`` as JSON-friendly nested lists


class WorldState(_S):
    players: list[PlayerState] = Field(default_factory=list)
    channels: list[ChannelState] = Field(default_factory=list)
    requests: dict[str, RequestState] = Field(default_factory=dict)
    tickets: dict[str, TicketState] = Field(default_factory=dict)
    abilities: dict[str, AbilityState] = Field(default_factory=dict)
    dashboard: DashboardState = Field(default_factory=DashboardState)
    phase: str = ""
    counters: dict[str, int] = Field(default_factory=dict)  # request / ticket / group id counters
    rng: RngState = Field(default_factory=RngState)
    memory: dict[str, AgentMemory] = Field(default_factory=dict)
    status: Literal["idle", "running", "paused", "finished"] = "idle"
    winner: str | None = None
    summary: str = ""
