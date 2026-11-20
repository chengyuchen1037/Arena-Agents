"""Frozen v0.12 contract: the validated, game-agnostic scenario specification (schema v2).

The Markdown loader (``app.scenario``) produces a :class:`ScenarioSpec`; the world consumes it.
The engine never interprets game rules — it only needs the declarative pieces below:
identity dealing, public tags, default limits, message templates, abilities, check expressions
(advisory), optional constraints and a few UI hints.  The *flow* of the game is not described
here: it is carried out by the moderator agent following ``manual_text``.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHORTHAND = re.compile(r"^\s*([a-z_]+)\s*(?:\((.*)\))?\s*(\?)?\s*$")


class ScenarioError(ValueError):
    """A scenario document is malformed (raised by ``app.world.loader``)."""


class ParamSpec(BaseModel):
    """Type of one template / ability parameter.

    YAML shorthand (parsed before validation): ``player_id``, ``player_ids``, ``int``,
    ``int(0,9)``, ``enum(a,b,c)``, ``text(60)``; a trailing ``?`` marks it optional.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["player_id", "player_ids", "enum", "int", "text"]
    values: list[str] = Field(default_factory=list)  # enum
    max_chars: int = 200  # text
    min: int | None = None  # int
    max: int | None = None  # int
    optional: bool = False

    @model_validator(mode="before")
    @classmethod
    def _from_shorthand(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        match = _SHORTHAND.match(value)
        if not match:
            raise ValueError(f"invalid parameter type {value!r}")
        kind, arg, optional = match.group(1), match.group(2), bool(match.group(3))
        out: dict[str, Any] = {"type": kind, "optional": optional}
        if kind == "enum":
            vals = [v.strip() for v in (arg or "").split(",") if v.strip()]
            if not vals:
                raise ValueError("enum needs at least one value")
            out["values"] = vals
        elif kind == "text" and arg:
            out["max_chars"] = int(arg)
        elif kind == "int" and arg:
            lo, _, hi = arg.partition(",")
            out["min"] = int(lo) if lo.strip() else None
            out["max"] = int(hi) if hi.strip() else None
        return out


class RoleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    faction: str = ""
    tags: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    brief: str = ""  # shown only to the holder (and the moderator) on their state board


class PoolItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    count: int = Field(1, ge=0)


class SetupSpec(BaseModel):
    """Engine-side dealing.  Each dealt player gets ``tags`` and ``attributes`` of the role, plus the
    engine-set attributes ``role`` and ``faction`` (so expressions can use ``p.data.role``)."""

    model_config = ConfigDict(extra="forbid")

    identity_pool: list[PoolItem] = Field(default_factory=list)
    roles: dict[str, RoleSpec] = Field(default_factory=dict)
    shuffle: bool = True
    # Tags every spectator/player may see on *other* players; every other tag is visible only to
    # its owner and the moderator.
    public_tags: list[str] = Field(default_factory=list)
    # Optional display names of the factions (``faction id -> "名称"``).  The moderator ends the game with
    # a free-text winner; the application matches it against these names (and the ids) to tell who won
    # when it records a finished game in the players' memories.  Without it the outcome stays unknown.
    faction_names: dict[str, str] = Field(default_factory=dict)


class FloorDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_s: float = 120.0
    max_messages: int = 3
    max_chars_per_message: int = 300
    max_chars_total: int = 600


class FreeDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_s: float = 300.0  # advisory: reaching it only notifies the moderator (D20)
    max_messages_per_member: int = 6  # hard
    max_chars_per_message: int = 300  # hard: a longer player's message is sent back (TOO_LONG, v0.14), never cut
    max_chars_total: int = 2000  # advisory
    silence_s: float = 30.0  # advisory: no message for this long -> notify the moderator

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_debounce(cls, data: Any) -> Any:
        # v0.12.1 (E1): there is no debounce any more; scenario files of v0.12.0 may still carry the key.
        if isinstance(data, dict) and "debounce_s" in data:
            data = {k: v for k, v in data.items() if k != "debounce_s"}
        return data


class RequestDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_s: float = 90.0
    max_text_chars: int = 300  # answers to ``text`` requests are truncated to this


class LlmDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Per LLM call, a safety net next to the character limits.  Reasoning models count their thinking
    # against it, so it must be generous (a real-model smoke test truncated every call at 1024).
    max_tokens: int = 8192


class ModeratorDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_chars_per_message: int = 2000  # free-text ``send_message`` is truncated to this


class DmDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_chars_per_message: int = 600  # a player's message / challenge in a system DM (never limited in count)


class DefaultsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    floor: FloorDefaults = Field(default_factory=FloorDefaults)
    free: FreeDefaults = Field(default_factory=FreeDefaults)
    request: RequestDefaults = Field(default_factory=RequestDefaults)
    llm: LlmDefaults = Field(default_factory=LlmDefaults)
    moderator: ModeratorDefaults = Field(default_factory=ModeratorDefaults)
    dm: DmDefaults = Field(default_factory=DmDefaults)


class TemplateSpec(BaseModel):
    """A structured message.  ``text`` uses ``{{param}}`` placeholders; a ``player_id`` renders as
    ``<name>（<id>）``, ``player_ids`` as those joined by ``、``.

    ``record`` (v0.14, H10) marks the messages of this template for the *hard-fact desk* of the state board
    (``world.board(agent)["facts"]``): ``fact`` = something the rules produced (an announcement of the moderator:
    what happened, who voted for whom ...), ``claim`` = something a player publicly *said* (true or false, the
    player's word only).  ``None`` = not recorded.  The engine does not know what the facts are about; the scenario
    marks its templates."""

    model_config = ConfigDict(extra="forbid")

    text: str
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    users: Literal["moderator", "players", "any"] = "moderator"
    sensitive: bool = False  # documentation + prompt hint; enforcement is ``users``
    description: str = ""
    record: Literal["fact", "claim"] | None = None

    @model_validator(mode="after")
    def _record_fits_its_users(self) -> "TemplateSpec":
        # What a player says is only ever his claim - never a fact; what the moderator announces is never a claim.
        if self.record == "fact" and self.users == "players":
            raise ValueError("record: fact 的模板不能是玩家专用的（users: players）：玩家说的话只能是 claim")
        if self.record == "claim" and self.users == "moderator":
            raise ValueError("record: claim 的模板不能是主持人专用的（users: moderator）：主持人的公告是 fact")
        return self


_SITUATION_TAG_FORBIDDEN = re.compile(r"[\s#|\[\]]")
_SITUATION_EVENT = re.compile(r"^[a-z][a-z_]*(\.[a-z_]+)*$")


class SituationRule(BaseModel):
    """One rule of ``ScenarioSpec.situations`` (v0.14, H14): in the situation described by the conditions the agent's
    memory is searched for entries carrying ``tag``.

    Every condition is "empty = any", all non-empty ones must hold: ``phase_contains`` is a substring of the phase text
    of the state board, ``channel_type`` / ``channel_id`` say which channel the wake-up is about, ``events`` are the
    ``payload["event"]`` names of the wake-up (any one of them), ``request_kind`` is the kind of the request that
    woke the agent.  The engine only parses and validates the rules; matching them is the agent layer's job.  Several
    rules may carry the same tag (then any of them is enough)."""

    model_config = ConfigDict(extra="forbid")

    tag: str
    phase_contains: str = ""
    channel_type: Literal["", "public", "group", "dm"] = ""
    channel_id: str = ""
    events: list[str] = Field(default_factory=list)
    request_kind: Literal["", "choice", "text"] = ""

    @field_validator("tag")
    @classmethod
    def _tag_is_usable(cls, value: str) -> str:
        tag = value.strip()
        if not tag:
            raise ValueError("情境标签（tag）不能为空")
        if _SITUATION_TAG_FORBIDDEN.search(tag):
            raise ValueError(f"情境标签「{tag}」不能含空白或 # | [ ]（它要写进记忆文件的打法条目里）")
        return tag

    @field_validator("events")
    @classmethod
    def _events_are_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not isinstance(name, str) or not _SITUATION_EVENT.match(name):
                raise ValueError(f"events 里的事件名「{name}」不合法（形如 floor.granted、request.opened）")
        return value


class RevealSpec(BaseModel):
    """Public side effect of an ability: a normal message is committed in ``channel_id`` *on behalf of
    the actor*, bypassing the floor (the ability is the permission).  ``text`` supports
    ``{{actor}}`` (``<name>（<id>）``) and ``{{<param>}}`` placeholders."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    text: str


class AbilitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    holders: str  # expression ``${...}`` -> list of players (evaluated at use time) ; see exprs
    quota: int = Field(1, ge=1)  # uses per holder for the whole game
    enabled: bool = False  # the moderator enables / disables abilities by phase
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    freeze_channels: list[str] = Field(default_factory=list)  # D25: engine freezes these on use
    reveal: RevealSpec | None = None
    description: str = ""  # shown to holders in their tool help


class ConstraintSpec(BaseModel):
    """Optional hard rule.  ``expr`` is true when the intent is allowed; false -> ``CONSTRAINT``.
    Names available: ``actor`` (player view), ``channel`` (dict), ``players()`` helpers."""

    model_config = ConfigDict(extra="forbid")

    applies_to: list[str]  # intent types, e.g. ["speak", "speak_template"]
    expr: str
    message: str = ""


class UiSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status_tags: list[str] = Field(default_factory=list)  # shown as status, not as the tile's main tag
    out_tags: list[str] = Field(default_factory=list)  # a player carrying any of these is "out" (greyed)
    highlight_tags: list[str] = Field(default_factory=list)  # tags worth a badge on the tile


class ScenarioSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    id: str
    version: str
    title: str = ""
    setup: SetupSpec = Field(default_factory=SetupSpec)
    defaults: DefaultsSpec = Field(default_factory=DefaultsSpec)
    templates: dict[str, TemplateSpec] = Field(default_factory=dict)
    abilities: dict[str, AbilitySpec] = Field(default_factory=dict)
    checks: dict[str, str] = Field(default_factory=dict)  # advisory expressions for ``mod.evaluate``
    constraints: list[ConstraintSpec] = Field(default_factory=list)
    invalidates_drafts: list[str] = Field(default_factory=list)  # ledger entry types that make drafts STALE
    # v0.14 (H14): situation tags - which phase / channel / wake-up event / request kind is "a vote", "a night action" ...
    situations: list[SituationRule] = Field(default_factory=list)
    ui: UiSpec = Field(default_factory=UiSpec)
    # Markdown body split by the loader: everything players may read vs. the moderator-only manual.
    rules_text: str = ""
    manual_text: str = ""

    @field_validator("id", "version")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-empty")
        return value

    @model_validator(mode="after")
    def _cross_check(self) -> "ScenarioSpec":
        for item in self.setup.identity_pool:
            if item.role not in self.setup.roles:
                raise ValueError(f"identity_pool references undeclared role {item.role!r}")
        return self
