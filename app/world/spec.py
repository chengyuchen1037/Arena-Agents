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
    max_chars_per_message: int = 300  # hard (truncating)
    max_chars_total: int = 2000  # advisory
    debounce_s: float = 1.5  # NORMAL wake debounce for members
    silence_s: float = 30.0  # advisory: no message for this long -> notify the moderator


class RequestDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_s: float = 90.0
    max_text_chars: int = 300  # answers to ``text`` requests are truncated to this


class LlmDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tokens: int = 2048  # per LLM call, a safety net next to the character limits


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
    ``<name>（<id>）``, ``player_ids`` as those joined by ``、``."""

    model_config = ConfigDict(extra="forbid")

    text: str
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    users: Literal["moderator", "players", "any"] = "moderator"
    sensitive: bool = False  # documentation + prompt hint; enforcement is ``users``
    description: str = ""


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
