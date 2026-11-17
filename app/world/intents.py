"""Frozen v0.12 contract: the payload of every :class:`~app.world.types.Intent`.

``Intent.type`` selects a model from :data:`INTENT_MODELS`; the commit gate validates
``Intent.payload`` against it (failure -> ``INVALID_PARAMS``).  ``app.agents`` derives the LLM tool
JSON schemas from these models (``Model.model_json_schema()``), so **field names here are the tool
parameter names**.

Player intents are plain names; moderator intents are prefixed ``mod.`` and are refused with
``FORBIDDEN`` for any other actor.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------- shared pieces


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FloorLimits(_Payload):
    """Limits of one floor token.  ``None`` = take the scenario default (``defaults.floor``)."""

    max_messages: int | None = Field(None, ge=1, le=50)
    max_chars_per_message: int | None = Field(None, ge=1, le=5000)
    max_chars_total: int | None = Field(None, ge=1, le=50000)
    duration_s: float | None = Field(None, gt=0, le=3600)


class FreeLimits(_Payload):
    """Limits of a free discussion.  ``None`` = scenario default (``defaults.free``)."""

    duration_s: float | None = Field(None, gt=0, le=7200)  # advisory (D20)
    max_messages_per_member: int | None = Field(None, ge=1, le=200)  # hard
    max_chars_per_message: int | None = Field(None, ge=1, le=5000)  # hard, truncating
    max_chars_total: int | None = Field(None, ge=1, le=200000)  # advisory (D20)
    silence_s: float | None = Field(None, gt=0, le=3600)  # advisory


#: Keys of :class:`OpenLimits` that apply to each floor mode (v0.12.1, E-tools).  The world validates the
#: keys against the mode (then the values with FloorLimits / FreeLimits) and answers ``INVALID_PARAMS``
#: naming the allowed keys when a key does not apply.
TOKEN_LIMIT_KEYS = ("max_messages", "max_chars_per_message", "max_chars_total", "duration_s")
FREE_LIMIT_KEYS = ("duration_s", "max_messages_per_member", "max_chars_per_message", "max_chars_total", "silence_s")


class OpenLimits(_Payload):
    """ONE ``limits`` object for ``open_floor`` / ``update_channel`` (replaces ``limits`` + ``free_limits``).

    mode=turn takes: max_messages, max_chars_per_message, max_chars_total, duration_s (per token).
    mode=free takes: duration_s, max_messages_per_member, max_chars_per_message, max_chars_total, silence_s
    (free discussions: duration_s / max_chars_total / silence_s are advisories, the others are hard).
    """

    # per-token (turn / queue)
    max_messages: int | None = Field(None, ge=1, le=50)
    # free discussion
    max_messages_per_member: int | None = Field(None, ge=1, le=200)
    silence_s: float | None = Field(None, gt=0, le=3600)
    # both
    max_chars_per_message: int | None = Field(None, ge=1, le=5000)
    max_chars_total: int | None = Field(None, ge=1, le=200000)
    duration_s: float | None = Field(None, gt=0, le=7200)


def _legacy_free_limits(data: Any) -> Any:
    """``free_limits`` (v0.12.0) is a deprecated alias of ``limits``; models only ever see ``limits``."""
    if isinstance(data, dict) and "free_limits" in data:
        data = dict(data)
        legacy = data.pop("free_limits")
        if legacy is not None and data.get("limits") is None:
            data["limits"] = legacy
    return data


class Option(_Payload):
    id: str = Field(min_length=1, max_length=64)
    label: str = Field("", max_length=120)


# --------------------------------------------------------------------------- player intents


class Speak(_Payload):
    channel_id: str
    content: str = Field(min_length=1, max_length=20000)  # over-long text is *truncated* by the gate (D19)


class SpeakTemplate(_Payload):
    channel_id: str
    template_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class Pass(_Payload):
    channel_id: str


class Respond(_Payload):
    request_id: str
    value: str = Field(min_length=1, max_length=2000)  # option id, or free text for ``text`` requests
    reason: str = Field("", max_length=1000)


class UseAbility(_Payload):
    ability_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class Challenge(_Payload):
    content: str = Field(min_length=1, max_length=20000)  # truncated to ``defaults.dm.max_chars_per_message``
    ref_seq: int | None = None


# --------------------------------------------------------------------------- moderator intents


class ModAnnounce(_Payload):
    channel_id: str
    template_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class ModSendMessage(_Payload):
    channel_id: str
    content: str = Field(min_length=1, max_length=20000)  # truncated to ``defaults.moderator``


class ModCreateChannel(_Payload):
    channel_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,48}$")
    name: str = Field(min_length=1, max_length=60)
    members: list[str] = Field(default_factory=list)  # the moderator is always added
    type: Literal["public", "group", "dm"] = "group"
    mode: Literal["closed", "free", "listen"] = "closed"  # turn/queue are entered via open_floor / set_floor_queue


class ModUpdateChannel(_Payload):
    channel_id: str
    name: str | None = None
    add_members: list[str] = Field(default_factory=list)
    remove_members: list[str] = Field(default_factory=list)  # the moderator can never be removed
    readonly_members: list[str] | None = None  # replaces the set of members who may read but not write
    mode: Literal["closed", "free", "listen"] | None = None  # also used to close / re-open a (punished) DM
    limits: OpenLimits | None = None  # (re)configure / extend a running free discussion (free keys only)

    @model_validator(mode="before")
    @classmethod
    def _alias(cls, data: Any) -> Any:
        return _legacy_free_limits(data)


class ModOpenFloor(_Payload):
    channel_id: str
    mode: Literal["turn", "free"] = "turn"
    holders: list[str] = Field(default_factory=list)  # turn: required; free: optional subset allowed to write
    limits: OpenLimits | None = None  # which keys apply depends on ``mode`` (see OpenLimits)

    @model_validator(mode="before")
    @classmethod
    def _alias(cls, data: Any) -> Any:
        return _legacy_free_limits(data)

    @model_validator(mode="after")
    def _holders_for_turn(self) -> "ModOpenFloor":
        if self.mode == "turn" and not self.holders:
            raise ValueError("mode=turn needs at least one holder")
        return self


class ModSetFloorQueue(_Payload):
    channel_id: str
    order: list[str] = Field(min_length=1)
    limits: FloorLimits | None = None  # one token per entry, same limits for everybody


class ModChannelRef(_Payload):
    """Used by ``mod.pause_queue``, ``mod.resume_queue`` and ``mod.close_floor``."""

    channel_id: str


class ModOpenRequest(_Payload):
    to: list[str] = Field(min_length=1)
    kind: Literal["choice", "text"] = "choice"
    question: str = Field(min_length=1, max_length=2000)
    options: list[Option] = Field(default_factory=list)  # required for kind=choice
    duration_s: float | None = Field(None, gt=0, le=3600)
    sealed: bool = False  # answers hidden from everyone (incl. the moderator) until the request closes
    # v0.12.1 (E19): every recipient (and the moderator) sees each answer as it is given and may change
    # their own answer until the request closes - e.g. a group of players agreeing on one target.
    # Mutually exclusive with ``sealed``.
    share_answers: bool = False
    # When the request may close *before* its deadline: all_answered (default) = once everybody answered;
    # unanimous = once everybody answered with the same option (otherwise it runs to the deadline, so the
    # group can keep talking and changing answers); deadline = never early.
    auto_close: Literal["all_answered", "unanimous", "deadline"] = "all_answered"
    default: str | None = None  # applied to non-responders at the deadline (option id / text)
    # ``dm`` is where targets are told about the request; defaults to each target's system DM.

    @model_validator(mode="after")
    def _options_for_choice(self) -> "ModOpenRequest":
        if self.sealed and self.share_answers:
            raise ValueError("sealed and share_answers cannot both be true")
        if self.auto_close == "unanimous" and self.kind != "choice":
            raise ValueError("auto_close=unanimous needs kind=choice")
        if self.kind == "choice" and len(self.options) < 2:
            raise ValueError("kind=choice needs at least two options")
        ids = [o.id for o in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate option id")
        if self.default is not None and self.kind == "choice" and self.default not in ids:
            raise ValueError("default must be one of the option ids")
        return self


class ModRequestRef(_Payload):
    """Used by ``mod.cancel_request``."""

    request_id: str


class ModReopenRequest(_Payload):
    """Clone a *closed or cancelled* request (new id, ``reopens`` links back) for ``to`` (default: the
    original targets)."""

    request_id: str
    to: list[str] | None = None
    duration_s: float | None = Field(None, gt=0, le=3600)


class ModUpdatePlayer(_Payload):
    player_id: str
    add_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)
    set_attributes: dict[str, Any] = Field(default_factory=dict)  # merged into ``attributes``


class ModSetPhase(_Payload):
    text: str = Field(min_length=1, max_length=120)


class FactionStat(_Payload):
    name: str
    alive: int = 0
    total: int = 0
    variant: str = "neutral"


class TagStat(_Payload):
    name: str
    count: int = 0
    variant: str = "neutral"


class ModUpdateDashboard(_Payload):
    factions: list[FactionStat] = Field(default_factory=list)
    tag_stats: list[TagStat] = Field(default_factory=list)


class ModRandom(_Payload):
    kind: Literal["int", "choice", "shuffle"]
    lo: int | None = None  # int: inclusive
    hi: int | None = None  # int: inclusive
    items: list[Any] = Field(default_factory=list)  # choice / shuffle
    k: int = Field(1, ge=1, le=100)  # choice: how many distinct items

    @model_validator(mode="after")
    def _check(self) -> "ModRandom":
        if self.kind == "int" and (self.lo is None or self.hi is None or self.lo > self.hi):
            raise ValueError("kind=int needs lo <= hi")
        if self.kind in ("choice", "shuffle") and not self.items:
            raise ValueError("items required")
        if self.kind == "choice" and self.k > len(self.items):
            raise ValueError("k larger than the number of items")
        return self


class ModEvaluate(_Payload):
    check_id: str
    # Names bound for this evaluation (identifier keys, JSON values), e.g. {"start": "<player id>", "direction": "left"}.
    args: dict[str, Any] = Field(default_factory=dict)


class ModGetLedger(_Payload):
    channel_id: str | None = None
    after_seq: int = 0
    limit: int = Field(50, ge=1, le=300)
    types: list[str] = Field(default_factory=list)


class ModResolveChallenge(_Payload):
    ticket_id: str
    verdict: Literal["upheld", "rejected"]
    reply: str = Field(min_length=1, max_length=20000)


class ModHoldGame(_Payload):
    reason: str = Field("", max_length=500)


class ModEndGame(_Payload):
    winner: str = Field(min_length=1, max_length=60)
    summary: str = Field("", max_length=2000)


class ModAbilityToggle(_Payload):
    """``mod.enable_ability`` / ``mod.disable_ability``.

    ``holders`` omitted = every holder allowed by the scenario expression.  enable + holders: only those
    players may use it.  disable + holders: those players lose it, the others keep what they had (all of
    them are disabled when nothing is left).  An empty list is an error (use ``null`` / omit it)."""

    ability_id: str
    holders: list[str] | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> "ModAbilityToggle":
        if self.holders is not None and not self.holders:
            raise ValueError("holders must not be an empty list: omit it to mean every holder")
        return self


# --------------------------------------------------------------------------- registry

PLAYER_INTENTS: dict[str, type[BaseModel]] = {
    "speak": Speak,
    "speak_template": SpeakTemplate,
    "pass": Pass,
    "respond": Respond,
    "use_ability": UseAbility,
    "challenge": Challenge,
}

MODERATOR_INTENTS: dict[str, type[BaseModel]] = {
    "mod.announce": ModAnnounce,
    "mod.send_message": ModSendMessage,
    "mod.create_channel": ModCreateChannel,
    "mod.update_channel": ModUpdateChannel,
    "mod.open_floor": ModOpenFloor,
    "mod.set_floor_queue": ModSetFloorQueue,
    "mod.pause_queue": ModChannelRef,
    "mod.resume_queue": ModChannelRef,
    "mod.close_floor": ModChannelRef,
    "mod.open_request": ModOpenRequest,
    "mod.cancel_request": ModRequestRef,
    "mod.reopen_request": ModReopenRequest,
    "mod.update_player": ModUpdatePlayer,
    "mod.set_phase": ModSetPhase,
    "mod.update_dashboard": ModUpdateDashboard,
    "mod.random": ModRandom,
    "mod.evaluate": ModEvaluate,  # read-only
    "mod.get_ledger": ModGetLedger,  # read-only
    "mod.resolve_challenge": ModResolveChallenge,
    "mod.hold_game": ModHoldGame,
    "mod.end_game": ModEndGame,
    "mod.enable_ability": ModAbilityToggle,
    "mod.disable_ability": ModAbilityToggle,
}

INTENT_MODELS: dict[str, type[BaseModel]] = {**PLAYER_INTENTS, **MODERATOR_INTENTS}

#: Intents that never change the world (they bypass the STALE check and never write the ledger).
READ_ONLY_INTENTS = frozenset({"mod.evaluate", "mod.get_ledger"})

#: Intents subject to the STALE check (a draft written before an invalidating entry is dropped).
STALE_CHECKED_INTENTS = frozenset({"speak", "speak_template"})


def is_moderator_intent(intent_type: str) -> bool:
    return intent_type.startswith("mod.")
