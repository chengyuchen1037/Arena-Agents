"""Frozen v0.12 contracts: the data that crosses the boundary between the *world* (physical
layer), the async *runtime* (actors / workers) and the *agents* (tools / prompts).

Nothing in this module may know about a concrete game.  Semantics are specified in
``docs/v0.12-contracts.md``; this file only defines the shapes.

Time: every deadline in the world is expressed in **game seconds** (``Clock.now()``), which
stops while the game is paused (decision D18).
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MODERATOR_ID = "moderator"
ENGINE_ID = "engine"  # sender of engine-generated notices / the target of engine notifications

# v0.12.1 (E1): *every* delivered envelope wakes its target at once - there is no ``ambient`` level and no
# debounce.  ``urgent`` only marks envelopes the agent is expected to act on: it is a label (it goes to the
# trace) and changes neither when nor in which order an agent is woken.  ``PRIORITY_RANK`` has no user.
Priority = Literal["urgent", "normal"]
PRIORITY_RANK: dict[str, int] = {"urgent": 0, "normal": 1}

EnvelopeKind = Literal["message", "floor", "request", "challenge", "ability", "notice"]


class Code:
    """Rejection codes returned in :class:`Receipt` (plain strings so they serialize as-is)."""

    NO_FLOOR = "NO_FLOOR"  # turn/queue channel without a (valid) token, a frozen channel, read-only member
    NOT_MEMBER = "NOT_MEMBER"
    CHANNEL_CLOSED = "CHANNEL_CLOSED"  # channel mode is ``closed`` / ``listen`` (incl. a punished DM)
    STALE = "STALE"  # the world changed materially since ``based_on_seq``
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    INVALID_TARGET = "INVALID_TARGET"
    INVALID_PARAMS = "INVALID_PARAMS"
    REQUEST_CLOSED = "REQUEST_CLOSED"
    UNKNOWN_CHANNEL = "UNKNOWN_CHANNEL"
    UNKNOWN_TEMPLATE = "UNKNOWN_TEMPLATE"
    UNKNOWN_ABILITY = "UNKNOWN_ABILITY"
    UNKNOWN_REQUEST = "UNKNOWN_REQUEST"
    UNKNOWN_TICKET = "UNKNOWN_TICKET"
    UNKNOWN_PLAYER = "UNKNOWN_PLAYER"
    ABILITY_DISABLED = "ABILITY_DISABLED"
    FORBIDDEN = "FORBIDDEN"  # this actor may not perform this intent / use this template
    CONSTRAINT = "CONSTRAINT"  # violates a scenario-declared constraint
    GAME_NOT_RUNNING = "GAME_NOT_RUNNING"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # v0.14 (H15, H16): a speech that is too long is sent back, never cut; a bot's speech that reads like a report
    # (lists, headings, its own seat id as a prefix) is sent back once.  Both are refusals: nothing is written, the floor is kept.
    TOO_LONG = "TOO_LONG"
    FORMAT = "FORMAT"

    ALL = frozenset(
        {
            "NO_FLOOR", "NOT_MEMBER", "CHANNEL_CLOSED", "STALE", "QUOTA_EXCEEDED", "INVALID_TARGET",
            "INVALID_PARAMS", "REQUEST_CLOSED", "UNKNOWN_CHANNEL",
            "UNKNOWN_TEMPLATE", "UNKNOWN_ABILITY", "UNKNOWN_REQUEST", "UNKNOWN_TICKET",
            "UNKNOWN_PLAYER", "ABILITY_DISABLED", "FORBIDDEN", "CONSTRAINT", "GAME_NOT_RUNNING",
            "ALREADY_EXISTS", "INTERNAL_ERROR", "TOO_LONG", "FORMAT",
        }
    )


class Envelope(BaseModel):
    """One notification delivered to an agent's mailbox (the *input* of an Actor).

    ``payload["event"]`` is the sub-type (see the contracts document for every ``kind``).
    Envelopes are lightweight notifications: the authoritative history is the Ledger, which an
    agent re-reads (``WorldPort.entries_visible``) whenever its worker is (re)started.
    """

    model_config = ConfigDict(extra="forbid")

    seq: int = 0  # ledger seq this notification refers to (0 = none)
    kind: EnvelopeKind
    priority: Priority = "normal"
    # True = work in flight that concerns ``channel_id`` / ``payload["request_id"]`` is now invalid
    # (floor revoked, request cancelled): the actor cancels a matching running worker.
    interrupt: bool = False
    channel_id: str | None = None
    sender: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class Delivery(BaseModel):
    """``envelope`` must be put into the mailbox of ``target`` (``ENGINE_ID`` = the orchestrator)."""

    model_config = ConfigDict(extra="forbid")

    target: str
    envelope: Envelope


class Intent(BaseModel):
    """What an agent's tool call asks the world to do (the *only* way to change the world)."""

    model_config = ConfigDict(extra="forbid")

    intent_id: str = ""
    actor_id: str
    gen_id: int = 0  # worker generation that produced it (diagnostics / trace only)
    based_on_seq: int = 0  # last ledger seq the actor had seen when it decided
    type: str  # see app.world.intents.INTENT_MODELS
    payload: dict[str, Any] = Field(default_factory=dict)


class Receipt(BaseModel):
    """The commit gate's synchronous answer; it becomes the tool result the worker sees."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    seq: int | None = None  # ledger seq of the committed entry (if any)
    code: str | None = None  # a Code.* value when ``ok`` is false
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)  # intent-specific result
    truncated: dict[str, int] | None = None  # {"dropped_chars": n, "kept_chars": m}  (D19)
    floor_left: dict[str, float | int | None] | None = None  # {"messages", "chars", "seconds"}

    @classmethod
    def accept(
        cls,
        seq: int | None = None,
        message: str = "",
        *,
        truncated: dict[str, int] | None = None,
        floor_left: dict[str, float | int | None] | None = None,
        **data: Any,
    ) -> "Receipt":
        return cls(
            ok=True, seq=seq, message=message, data=data, truncated=truncated, floor_left=floor_left
        )

    @classmethod
    def reject(cls, code: str, message: str = "", **data: Any) -> "Receipt":
        if code not in Code.ALL:  # pragma: no cover - programming error
            raise ValueError(f"unknown rejection code {code!r}")
        return cls(ok=False, code=code, message=message, data=data)

    def tool_json(self) -> str:
        """Compact JSON used as the tool result text."""
        body = self.model_dump(exclude_none=True, exclude_defaults=False)
        if not body.get("data"):
            body.pop("data", None)
        if not body.get("message"):
            body.pop("message", None)
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


class LedgerEntry(BaseModel):
    """One append-only fact.  ``seq`` is global, gap-free and strictly increasing."""

    model_config = ConfigDict(extra="forbid")

    seq: int
    ts: str  # wall-clock ISO-8601 (display only; never used for logic)
    gt: float = 0.0  # game-clock seconds when committed
    type: str  # message | channel.created | channel.updated | floor.* | request.* | challenge.* | ability.* |
    # player.updated | phase.changed | dashboard.updated | random.drawn | game.status
    actor_id: str  # a player id, MODERATOR_ID or ENGINE_ID
    channel_id: str | None = None
    # Who may read this entry (membership frozen at commit time).  ``[]`` = nobody except the
    # god-view spectator (e.g. a sealed answer before its request closes).
    visible_to_ids: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class UiEvent(BaseModel):
    """A spectator-facing event produced by the world; the orchestrator publishes it on the SSE bus.

    Never carries thoughts, tool calls or private attributes.
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class AgentMemory(BaseModel):
    """Per-agent private memory persisted with the game (notes + compaction)."""

    model_config = ConfigDict(extra="forbid")

    notes: dict[str, str] = Field(default_factory=dict)
    summary: str = ""
    summary_upto_seq: int = 0  # ledger entries with seq <= this are replaced by ``summary``
    compactions: int = 0
