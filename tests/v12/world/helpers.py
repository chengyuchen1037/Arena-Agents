"""Shared helpers for the world tests.

``ChatWorld`` is ``WorldCore`` + :class:`~app.world.chat.ChatModule` only (no game module), built on a
hand-made scenario with tiny limits so that truncation / quotas are easy to hit.  The world is
synchronous, so every test is synchronous too.
"""

from __future__ import annotations

from typing import Any

from app.world.chat import ChatModule
from app.world.core import WorldCore
from app.world.spec import ScenarioSpec
from app.world.state import PlayerState, WorldState
from app.world.types import MODERATOR_ID, Delivery, Envelope, Intent, LedgerEntry, Receipt, UiEvent

PLAYERS = ["p01", "p02", "p03", "p04"]

# tiny limits: 2 messages x 20 chars (50 in total) per token, 60 s per token
BASE_SPEC: dict[str, Any] = {
    "id": "chat_test",
    "version": "1",
    "defaults": {
        "floor": {"duration_s": 60, "max_messages": 2, "max_chars_per_message": 20, "max_chars_total": 50},
        "free": {
            "duration_s": 100,
            "max_messages_per_member": 3,
            "max_chars_per_message": 30,
            "max_chars_total": 100,
            "debounce_s": 1.5,
            "silence_s": 40,
        },
        "moderator": {"max_chars_per_message": 40},
        "dm": {"max_chars_per_message": 25},
    },
    "templates": {
        "notice": {"text": "【公告】{{text}}", "params": {"text": "text(30)"}, "users": "moderator", "sensitive": True},
        "vote": {
            "text": "我投 {{target}}。{{reason}}",
            "params": {"target": "player_id", "reason": "text(10)?"},
            "users": "players",
        },
        "shout": {"text": "{{who}} 说：{{word}}", "params": {"who": "player_ids", "word": "enum(yes,no)"}, "users": "any"},
        "count": {"text": "数到 {{n}}", "params": {"n": "int(1,9)"}, "users": "any"},
        "long": {"text": "这是一条很长很长很长很长很长很长很长很长很长的模板消息，超过了单条上限。", "users": "any"},
    },
}


def make_spec(**overrides: Any) -> ScenarioSpec:
    """The base scenario with top-level keys replaced; ``defaults`` is merged section by section."""
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in BASE_SPEC.items()}
    defaults = overrides.pop("defaults", None)
    if defaults:
        merged = {k: dict(v) for k, v in BASE_SPEC["defaults"].items()}
        for section, values in defaults.items():
            merged.setdefault(section, {}).update(values)
        data["defaults"] = merged
    data.update(overrides)
    return ScenarioSpec.model_validate(data)


class ManualClock:
    """Game clock driven by the test."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = float(start)

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt

    def set(self, t: float) -> None:
        self.t = float(t)


class ChatWorld(WorldCore):
    """``WorldCore`` + ``ChatModule``."""

    chat: ChatModule

    def __init__(
        self,
        spec: ScenarioSpec,
        clock: Any,
        *,
        state: WorldState | None = None,
        ledger: list[LedgerEntry] | None = None,
        seed: int | None = None,
    ) -> None:
        super().__init__(spec, clock, state=state, ledger=ledger, seed=seed)
        self.chat = ChatModule(self)
        self.add_module(self.chat)

    @classmethod
    def restore(cls, data: dict[str, Any], spec: ScenarioSpec, clock: Any) -> "ChatWorld":
        state = WorldState.model_validate(data["state"])
        if state.status == "running":
            state.status = "paused"
        ledger = [LedgerEntry.model_validate(e) for e in data["ledger"]]
        world = cls(spec, clock, state=state, ledger=ledger)
        for module in world.modules:
            module.load(data.get("modules", {}).get(module.name, {}))
        return world


def make_players(n: int = 4) -> list[PlayerState]:
    return [PlayerState(id=f"p{i:02d}", seat=str(i), name=f"玩家{i}") for i in range(1, n + 1)]


def make_world(
    n: int = 4,
    *,
    spec: ScenarioSpec | None = None,
    clock: ManualClock | None = None,
    running: bool = True,
) -> ChatWorld:
    """A world with ``n`` players, their system DMs and (by default) a running game; queues are empty."""
    world = ChatWorld(spec or make_spec(), clock or ManualClock(), state=WorldState(players=make_players(n)))
    world.chat.create_system_dms()
    if running:
        world.set_running(True, "start")
    flush(world)
    return world


def flush(world: WorldCore) -> tuple[list[Delivery], list[UiEvent]]:
    """Drop (and return) everything queued so far so the next assertions only see what follows."""
    return world.drain_outbox(), world.drain_events()


# ------------------------------------------------------------------------------ intents


def submit(
    world: WorldCore, actor: str, type_: str, payload: dict[str, Any] | None = None, *, based_on: int | None = None
) -> Receipt:
    intent = Intent(
        intent_id="t",
        actor_id=actor,
        gen_id=1,
        based_on_seq=world.seq if based_on is None else based_on,
        type=type_,
        payload=payload or {},
    )
    return world.submit(intent)


def speak(world: WorldCore, actor: str, channel_id: str, content: str, **kw: Any) -> Receipt:
    return submit(world, actor, "speak", {"channel_id": channel_id, "content": content}, **kw)


def speak_template(world: WorldCore, actor: str, channel_id: str, template_id: str, **params: Any) -> Receipt:
    return submit(world, actor, "speak_template", {"channel_id": channel_id, "template_id": template_id, "params": params})


def do_pass(world: WorldCore, actor: str, channel_id: str) -> Receipt:
    return submit(world, actor, "pass", {"channel_id": channel_id})


def mod(world: WorldCore, action: str, /, **payload: Any) -> Receipt:
    """Moderator intent ``mod.<action>``."""
    return submit(world, MODERATOR_ID, f"mod.{action}", payload)


def ok(receipt: Receipt) -> Receipt:
    assert receipt.ok, f"expected ok, got {receipt.code}: {receipt.message}"
    return receipt


def new_channel(
    world: WorldCore,
    channel_id: str = "c1",
    members: list[str] | None = None,
    mode: str = "closed",
    *,
    name: str | None = None,
    type_: str = "group",
    keep_queues: bool = False,
) -> Receipt:
    receipt = ok(
        mod(
            world,
            "create_channel",
            channel_id=channel_id,
            name=name or f"频道{channel_id}",
            members=list(PLAYERS if members is None else members),
            mode=mode,
            type=type_,
        )
    )
    if not keep_queues:
        flush(world)
    return receipt


def open_turn(world: WorldCore, channel_id: str, holders: list[str], *, keep_queues: bool = False, **limits: Any) -> Receipt:
    payload: dict[str, Any] = {"channel_id": channel_id, "mode": "turn", "holders": holders}
    if limits:
        payload["limits"] = limits
    receipt = ok(mod(world, "open_floor", **payload))
    if not keep_queues:
        flush(world)
    return receipt


def open_queue(world: WorldCore, channel_id: str, order: list[str], *, keep_queues: bool = False, **limits: Any) -> Receipt:
    payload: dict[str, Any] = {"channel_id": channel_id, "order": order}
    if limits:
        payload["limits"] = limits
    receipt = ok(mod(world, "set_floor_queue", **payload))
    if not keep_queues:
        flush(world)
    return receipt


def open_free(world: WorldCore, channel_id: str, *, holders: list[str] | None = None, keep_queues: bool = False, **free_limits: Any) -> Receipt:
    payload: dict[str, Any] = {"channel_id": channel_id, "mode": "free", "holders": holders or []}
    if free_limits:
        payload["free_limits"] = free_limits
    receipt = ok(mod(world, "open_floor", **payload))
    if not keep_queues:
        flush(world)
    return receipt


# ------------------------------------------------------------------------------ inspection


def entries(
    world: WorldCore,
    type_: str | None = None,
    *,
    channel: str | None = None,
    after: int = 0,
    prefix: bool = False,
) -> list[LedgerEntry]:
    out = []
    for entry in world.ledger:
        if entry.seq <= after:
            continue
        if type_ is not None and not (entry.type.startswith(type_) if prefix else entry.type == type_):
            continue
        if channel is not None and entry.channel_id != channel:
            continue
        out.append(entry)
    return out


def msgs(world: WorldCore, channel: str | None = None, after: int = 0) -> list[LedgerEntry]:
    return entries(world, "message", channel=channel, after=after)


def envelopes(
    deliveries: list[Delivery], target: str | None = None, event: str | None = None, kind: str | None = None
) -> list[Envelope]:
    out = []
    for delivery in deliveries:
        if target is not None and delivery.target != target:
            continue
        env = delivery.envelope
        if event is not None and env.payload.get("event") != event:
            continue
        if kind is not None and env.kind != kind:
            continue
        out.append(env)
    return out


def targets(deliveries: list[Delivery], event: str) -> list[str]:
    return [d.target for d in deliveries if d.envelope.payload.get("event") == event]


def ui(events: list[UiEvent], type_: str) -> list[dict[str, Any]]:
    return [e.data for e in events if e.type == type_]


def channel(world: WorldCore, channel_id: str) -> Any:
    found = world.channel(channel_id)
    assert found is not None, channel_id
    return found
