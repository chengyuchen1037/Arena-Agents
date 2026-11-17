"""World kernel: ledger, commit gate, mailbox deliveries, spectator events and the module registry.

``WorldCore`` is the *shared base*; behaviour lives in :class:`WorldModule` subclasses
(``app.world.chat`` = channels / floors / free discussion / messages / templates,
``app.world.game`` = players / dealing / requests / abilities / challenges / randomness / game flow)
that register intent handlers here.  ``app.world.world.World`` composes the two.

Rules every module must follow (see docs/v0.12-contracts.md):

* the world is single-threaded and **synchronous**: no ``await`` and no blocking I/O inside a handler;
* all state changes append to the ledger through :meth:`WorldCore.commit` (history is never edited);
* side effects toward agents go through :meth:`deliver`, toward spectators through :meth:`emit`;
* no game-specific vocabulary — rules come from the :class:`~app.world.spec.ScenarioSpec`.
"""

from __future__ import annotations

import datetime
import logging
import random
from collections.abc import Callable, Iterable
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ValidationError

from app.engine.expressions import (
    ExpressionError,
    ParticipantView,
    SafeExpression,
    leaders,
    seat_order,
    tally,
)
from app.world.intents import INTENT_MODELS, STALE_CHECKED_INTENTS, is_moderator_intent
from app.world.port import Clock
from app.world.spec import ScenarioSpec
from app.world.state import ChannelState, PlayerState, WorldState
from app.world.types import (
    ENGINE_ID,
    MODERATOR_ID,
    Code,
    Delivery,
    Envelope,
    Intent,
    LedgerEntry,
    Receipt,
    UiEvent,
)

logger = logging.getLogger(__name__)

EVERYONE = "*"  # ``visible_to`` shorthand: every player and the moderator
MODERATOR_NAME = "主持人"
ENGINE_NAME = "系统"

Handler = Callable[[Intent, BaseModel], Receipt]


class WorldModule:
    """A slice of world behaviour.  Subclasses override what they need."""

    name = "module"

    def __init__(self, world: "WorldCore") -> None:
        self.w = world

    def handlers(self) -> dict[str, Handler]:
        """``intent type -> handler(intent, validated payload) -> Receipt``."""
        return {}

    def tick(self) -> None:
        """Process timers that expired at ``clock.now()`` (must be idempotent)."""

    def next_deadline(self) -> float | None:
        """Earliest pending timer (game seconds) owned by this module, if any."""
        return None

    def redeliver(self) -> None:
        """Queue deliveries for work that is still open (used when a game is resumed / restored)."""

    def dump(self) -> dict[str, Any]:
        """Extra module state not covered by ``WorldState`` (JSON-serializable)."""
        return {}

    def load(self, data: dict[str, Any]) -> None:
        """Restore what :meth:`dump` produced."""


class WorldCore:
    def __init__(
        self,
        spec: ScenarioSpec,
        clock: Clock,
        *,
        state: WorldState | None = None,
        ledger: list[LedgerEntry] | None = None,
        seed: int | None = None,
    ) -> None:
        self.spec = spec
        self.clock = clock
        self.state = state or WorldState()
        self.ledger: list[LedgerEntry] = ledger if ledger is not None else []
        self.modules: list[WorldModule] = []
        self._handlers: dict[str, Handler] = {}
        self._outbox: list[Delivery] = []
        self._events: list[UiEvent] = []
        self._notifier: Callable[[], None] | None = None
        self._running = False
        self._invalidating = set(spec.invalidates_drafts)
        self._last_invalidating_seq = 0
        for entry in self.ledger:
            if entry.type in self._invalidating:
                self._last_invalidating_seq = entry.seq
        if not self.state.rng.seed and seed is not None:
            self.state.rng.seed = int(seed)
        self.rng = random.Random(self.state.rng.seed)
        if self.state.rng.state:
            self.rng.setstate(_tuple_deep(self.state.rng.state))

    # ------------------------------------------------------------------ registry --
    def add_module(self, module: WorldModule) -> None:
        self.modules.append(module)
        for intent_type, handler in module.handlers().items():
            if intent_type in self._handlers:
                raise RuntimeError(f"intent {intent_type!r} registered twice")
            self._handlers[intent_type] = handler

    # ------------------------------------------------------------------- WorldPort --
    @property
    def seq(self) -> int:
        return self.ledger[-1].seq if self.ledger else 0

    @property
    def running(self) -> bool:
        return self._running

    def set_notifier(self, callback: Callable[[], None] | None) -> None:
        """``callback()`` is invoked (synchronously) after every ``submit`` / ``tick`` that queued
        deliveries or events, so the runtime can drain the queues without polling."""
        self._notifier = callback

    def _notify(self) -> None:
        if self._notifier is not None and (self._outbox or self._events):
            try:
                self._notifier()
            except Exception:  # noqa: BLE001 - a broken listener must never break the world
                logger.exception("world notifier failed")

    def drain_outbox(self) -> list[Delivery]:
        out, self._outbox = self._outbox, []
        return out

    def drain_events(self) -> list[UiEvent]:
        out, self._events = self._events, []
        return out

    def tick(self) -> None:
        if not self._running:
            return
        for module in self.modules:
            try:
                module.tick()
            except Exception:  # noqa: BLE001 - one module's bug must not stop the others' timers
                logger.exception("world module %s failed in tick()", module.name)
        self._notify()

    def next_deadline(self) -> float | None:
        deadlines = [d for d in (m.next_deadline() for m in self.modules) if d is not None]
        return min(deadlines) if deadlines else None

    def redeliver(self) -> None:
        for module in self.modules:
            module.redeliver()
        self._notify()

    # --------------------------------------------------------------------- ledger --
    def commit(
        self,
        type: str,  # noqa: A002 - mirrors LedgerEntry.type
        actor_id: str,
        data: dict[str, Any] | None = None,
        *,
        channel_id: str | None = None,
        visible_to: Iterable[str] | str | None = None,
    ) -> LedgerEntry:
        """Append one entry.  ``visible_to``: explicit ids, :data:`EVERYONE`, or ``None`` meaning "the
        current members of ``channel_id``".  ``[]`` means nobody (god-view only)."""
        if visible_to is None:
            if channel_id is None:
                raise ValueError("commit() needs visible_to or channel_id")
            audience = list(self.audience(channel_id))
        elif isinstance(visible_to, str):
            audience = self.everyone() if visible_to == EVERYONE else [visible_to]
        else:
            audience = _dedupe(visible_to)
        entry = LedgerEntry(
            seq=self.seq + 1,
            ts=datetime.datetime.now().astimezone().isoformat(timespec="milliseconds"),
            gt=round(float(self.clock.now()), 3),
            type=type,
            actor_id=actor_id,
            channel_id=channel_id,
            visible_to_ids=audience,
            data=dict(data or {}),
        )
        self.ledger.append(entry)
        if type in self._invalidating:
            self._last_invalidating_seq = entry.seq
        return entry

    def entries_visible(
        self, agent_id: str, *, after_seq: int = 0, limit: int | None = None
    ) -> list[LedgerEntry]:
        out = [e for e in self.ledger if e.seq > after_seq and agent_id in e.visible_to_ids]
        return out[-limit:] if limit else out

    # ------------------------------------------------------------------ deliveries --
    def deliver(
        self,
        target: str,
        kind: str,
        event: str,
        *,
        priority: str = "ambient",
        channel_id: str | None = None,
        sender: str | None = None,
        seq: int = 0,
        interrupt: bool = False,
        payload: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        """Queue one mailbox delivery.

        ``payload`` (a ready-made dict, may use any key such as ``kind``) and the keyword ``extra``
        are merged into ``Envelope.payload``; ``event`` and (when given) ``channel_id`` are always
        included in it, so a consumer can read either ``envelope.channel_id`` or ``payload["channel_id"]``.
        """
        body: dict[str, Any] = {"event": event}
        if channel_id is not None:
            body["channel_id"] = channel_id
        body.update(payload or {})
        body.update(extra)
        envelope = Envelope(
            seq=seq,
            kind=kind,  # type: ignore[arg-type]
            priority=priority,  # type: ignore[arg-type]
            interrupt=interrupt,
            channel_id=channel_id,
            sender=sender,
            payload=body,
        )
        self._outbox.append(Delivery(target=target, envelope=envelope))

    def emit(self, type: str, **data: Any) -> None:  # noqa: A002
        self._events.append(UiEvent(type=type, data=data))

    def message_priority(
        self, channel: ChannelState, sender: str, recipient: str, kind: str
    ) -> str:
        """Wake priority of a new channel message for ``recipient`` (see contracts §4.3)."""
        if recipient == MODERATOR_ID:
            if kind == "challenge":
                return "urgent"
            if channel.type == "dm" and channel.system and sender != MODERATOR_ID:
                return "normal"
            return "ambient"
        if sender == MODERATOR_ID and channel.type == "dm" and channel.system:
            return "urgent" if kind == "reply" else "normal"
        if channel.mode == "free" and not channel.frozen and recipient not in channel.readonly:
            free = channel.free
            if free is None:
                return "normal"
            # A member who may not write, or who has used up the quota, cannot answer: no need to wake it.
            if (free.writers is None or recipient in free.writers) and free.counts.get(
                recipient, 0
            ) < free.max_messages_per_member:
                return "normal"
        return "ambient"

    def post_message(
        self,
        channel_id: str,
        actor_id: str,
        content: str,
        *,
        kind: str,
        template_id: str | None = None,
        params: dict[str, Any] | None = None,
        truncated: dict[str, int] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> LedgerEntry:
        """Commit a ``message`` entry in ``channel_id``, fan it out to the members' mailboxes and emit
        ``message.committed``.  Permission checks are the caller's job."""
        channel = self.channel(channel_id)
        if channel is None:  # pragma: no cover - caller bug
            raise KeyError(channel_id)
        data: dict[str, Any] = {
            "content": content,
            "kind": kind,
            "sender_name": self.display_name(actor_id),
        }
        if template_id:
            data["template_id"] = template_id
            data["params"] = params or {}
        if truncated:
            data["truncated"] = truncated
        if extra:
            data.update(extra)
        entry = self.commit("message", actor_id, data, channel_id=channel_id)
        # A challenge (player -> moderator) and its reply travel as ``challenge`` envelopes.
        env_kind, env_event = {
            "challenge": ("challenge", "challenge.opened"),
            "reply": ("challenge", "challenge.resolved"),
        }.get(kind, ("message", "message.new"))
        for member in entry.visible_to_ids:
            if member == actor_id:
                continue
            priority = self.message_priority(channel, actor_id, member, kind)
            hint: dict[str, Any] = {}
            if priority == "normal" and channel.mode == "free" and channel.free is not None:
                hint["debounce_s"] = channel.free.debounce_s  # the runtime's NORMAL wake delay for this discussion
            self.deliver(
                member,
                env_kind,
                env_event,
                priority=priority,
                channel_id=channel_id,
                sender=actor_id,
                seq=entry.seq,
                message_kind=kind,
                ticket_id=(extra or {}).get("ticket_id"),
                **hint,
            )
        self.emit(
            "message.committed",
            seq=entry.seq,
            ts=entry.ts,
            channel_id=channel_id,
            **{
                "from": actor_id,
                "from_name": data["sender_name"],
                "content": content,
                "kind": kind,
                "template_id": template_id,
                "truncated": truncated,
                "ticket_id": (extra or {}).get("ticket_id"),
                "visible_to_ids": entry.visible_to_ids,
            },
        )
        return entry

    # ------------------------------------------------------------------- lookups ---
    def everyone(self) -> list[str]:
        return [MODERATOR_ID, *(p.id for p in self.state.players)]

    def player(self, player_id: str) -> PlayerState | None:
        for p in self.state.players:
            if p.id == player_id:
                return p
        return None

    def is_player(self, actor_id: str) -> bool:
        return self.player(actor_id) is not None

    def channel(self, channel_id: str) -> ChannelState | None:
        for c in self.state.channels:
            if c.id == channel_id:
                return c
        return None

    def audience(self, channel_id: str) -> list[str]:
        channel = self.channel(channel_id)
        return list(channel.members) if channel else []

    def display_name(self, agent_id: str) -> str:
        if agent_id == MODERATOR_ID:
            return MODERATOR_NAME
        if agent_id == ENGINE_ID:
            return ENGINE_NAME
        player = self.player(agent_id)
        return player.name if player else agent_id

    def next_counter(self, name: str) -> int:
        value = int(self.state.counters.get(name, 0)) + 1
        self.state.counters[name] = value
        return value

    # --------------------------------------------------------------- expressions ---
    def is_out(self, player: PlayerState) -> bool:
        out = set(self.spec.ui.out_tags)
        return any(tag in out for tag in player.tags)

    def player_view(self, player: PlayerState) -> ParticipantView:
        data = {**player.attributes, "role": player.role, "faction": player.faction}
        return ParticipantView(
            id=player.id,
            seat=player.seat,
            name=player.name,
            tags=tuple(player.tags),
            data=MappingProxyType(data),
        )

    def eval_expr(self, expression: str, extra: dict[str, Any] | None = None) -> Any:
        """Evaluate a scenario expression (``${...}`` wrapper optional) over the current players.

        Names: ``players`` (all views) plus ``extra``.  Functions: ``alive / by_role / by_faction /
        by_tag / ids / first / count / len / sum / min / max / get / tally / leaders / seat_order``.
        """
        text = expression.strip()
        if text.startswith("${") and text.endswith("}"):
            text = text[2:-1]
        views = [self.player_view(p) for p in self.state.players]
        out_ids = {p.id for p in self.state.players if self.is_out(p)}

        def selected(items: Any = None) -> list[Any]:
            return views if items is None else list(items)

        def ids(items: Any) -> list[str]:
            if items is None:
                return []
            if isinstance(items, (str, ParticipantView)):
                items = [items]
            return [i.id if isinstance(i, ParticipantView) else str(i) for i in items]

        functions: dict[str, Callable[..., Any]] = {
            "players": lambda: views,
            "alive": lambda items=None: [v for v in selected(items) if v.id not in out_ids],
            "by_role": lambda role, items=None: [v for v in selected(items) if v.data.get("role") == role],
            "by_faction": lambda f, items=None: [v for v in selected(items) if v.data.get("faction") == f],
            "by_tag": lambda tag, items=None: [v for v in selected(items) if tag in v.tags],
            "ids": ids,
            "first": lambda items, default=None: next(iter(items), default),
            "count": lambda items: len(items),
            "len": lambda items: len(items),
            "sum": lambda items: sum(items),
            "min": lambda items: min(items),
            "max": lambda items: max(items),
            "get": lambda value, key, default=None: (
                value.get(key, default) if isinstance(value, dict) else default
            ),
            "tally": tally,
            "leaders": leaders,
            "seat_order": seat_order,
        }
        names: dict[str, Any] = {"players": views, **(extra or {})}
        return SafeExpression(names, functions).evaluate(text)

    @staticmethod
    def to_plain(value: Any) -> Any:
        """Make an expression result JSON-friendly (player views -> ids)."""
        if isinstance(value, ParticipantView):
            return value.id
        if isinstance(value, dict):
            return {str(k): WorldCore.to_plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [WorldCore.to_plain(v) for v in value]
        return value

    # ------------------------------------------------------------------ the gate ---
    def set_running(self, running: bool, reason: str = "") -> None:
        """Open / close the commit gate.  Records a ``game.status`` entry when the status changes."""
        previous = self.state.status
        self._running = bool(running)
        if running:
            new = "running"
        elif previous in ("finished", "idle"):
            new = previous  # a game that never started stays ``idle``; a finished one stays ``finished``
        else:
            new = "paused"
        if new == previous:
            return
        if previous == "finished" and new == "running":  # D8: the moderator's verdict is withdrawn
            reason = "continued"
            self.state.winner = None
            self.state.summary = ""
        self.state.status = new
        self.commit("game.status", ENGINE_ID, {"status": new, "reason": reason}, visible_to=EVERYONE)
        self.emit("game.status", status=new, reason=reason, winner=self.state.winner)
        self._notify()

    def submit(self, intent: Intent) -> Receipt:
        """The commit gate (contracts §3).  Never raises: returns a rejected receipt instead."""
        try:
            receipt = self._submit(intent)
        except Exception:  # noqa: BLE001 - an engine bug must not crash the caller
            logger.exception("commit gate failed for %s", intent.type)
            receipt = Receipt.reject(Code.INTERNAL_ERROR, "引擎内部错误，请重试或联系主持人。")
        self._notify()
        return receipt

    def _submit(self, intent: Intent) -> Receipt:
        model = INTENT_MODELS.get(intent.type)
        if model is None:
            return Receipt.reject(Code.INVALID_PARAMS, f"未知的操作类型：{intent.type}")
        if not self._running:
            return Receipt.reject(Code.GAME_NOT_RUNNING, "对局当前未在运行，操作被拒绝。")
        actor = intent.actor_id
        if actor != MODERATOR_ID and not self.is_player(actor):
            return Receipt.reject(Code.UNKNOWN_PLAYER, f"未知的参与者：{actor}")
        if is_moderator_intent(intent.type) != (actor == MODERATOR_ID):
            who = "只有主持人" if is_moderator_intent(intent.type) else "只有玩家"
            return Receipt.reject(Code.FORBIDDEN, f"{who}可以执行 {intent.type}。")
        try:
            payload = model.model_validate(intent.payload)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or '参数'}: {err['msg']}"
                for err in exc.errors(include_input=False, include_url=False)[:4]
            )
            return Receipt.reject(Code.INVALID_PARAMS, f"参数无效：{problems}")
        if intent.type in STALE_CHECKED_INTENTS and self._last_invalidating_seq > intent.based_on_seq:
            return Receipt.reject(
                Code.STALE,
                "你思考期间世界发生了重要变化，请先阅读最新内容后再决定是否发言。",
                latest_seq=self.seq,
            )
        rejected = self._check_constraints(intent, payload)
        if rejected is not None:
            return rejected
        handler = self._handlers.get(intent.type)
        if handler is None:  # pragma: no cover - registry bug
            return Receipt.reject(Code.INTERNAL_ERROR, f"没有处理 {intent.type} 的模块。")
        return handler(intent, payload)

    def _check_constraints(self, intent: Intent, payload: BaseModel) -> Receipt | None:
        applicable = [c for c in self.spec.constraints if intent.type in c.applies_to]
        if not applicable:
            return None
        player = self.player(intent.actor_id)
        channel_id = getattr(payload, "channel_id", None)
        channel = self.channel(channel_id) if channel_id else None
        extra = {
            "actor": self.player_view(player) if player else None,
            "intent_type": intent.type,
            "channel": (
                {"id": channel.id, "type": channel.type, "mode": channel.mode, "members": list(channel.members)}
                if channel
                else {}
            ),
        }
        for constraint in applicable:
            try:
                allowed = bool(self.eval_expr(constraint.expr, extra))
            except ExpressionError:
                logger.exception("constraint expression failed: %s", constraint.expr)
                return Receipt.reject(Code.INTERNAL_ERROR, "剧本约束表达式执行失败。")
            if not allowed:
                return Receipt.reject(Code.CONSTRAINT, constraint.message or "违反了剧本约束。")
        return None

    # ---------------------------------------------------------------- persistence --
    def dump(self) -> dict[str, Any]:
        self.state.rng.state = _list_deep(self.rng.getstate())
        return {
            "format": 1,
            "clock_now": float(self.clock.now()),
            "state": self.state.model_dump(mode="json"),
            "ledger": [e.model_dump(mode="json") for e in self.ledger],
            "modules": {m.name: m.dump() for m in self.modules},
        }


# --------------------------------------------------------------------------- helpers


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(str(item), None)
    return list(seen)


def _list_deep(value: Any) -> Any:
    if isinstance(value, (tuple, list)):
        return [_list_deep(v) for v in value]
    return value


def _tuple_deep(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_tuple_deep(v) for v in value)
    return value
