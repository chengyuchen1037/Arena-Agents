"""Game-side world module: players, dealing, requests, abilities, challenges, randomness, game flow,
private memory (contracts §2.3 "游戏类", §2.5 ledger catalogue, §2.6 deliveries, §8.2 UI events).

``app.world.world.World`` assembles it::

    self.game = GameModule(self)
    self.add_module(self.game)

and ``new_game`` calls ``self.game.deal()`` once the players and their system DMs exist.  The
memory helpers (``memory`` / ``write_note`` / ``read_notes_text`` / ``compact``) and ``board`` are what
the ``WorldPort`` methods of the same name delegate to.

Everything is kept in ``world.state`` (requests, tickets, abilities, counters, rng, memory), so
``dump()`` / ``load()`` have nothing of their own to persist.

Conventions used throughout (they are part of the contract the runtime / agents build on):

* a handler that rejects an intent changes nothing (no counter, no ledger entry, no delivery);
* sealed request answers are committed with ``visible_to_ids = []`` and only reach the moderator
  inside the ``request.closed`` entry;
* ``request.closed`` (the entry data and the moderator's envelope payload alike) carries, for ``choice``
  requests, ``tally`` (votes per option id, defaulted answers included, every option listed in option
  order) and ``leaders`` (the options with the most votes; none when nobody voted);
* ``mod.evaluate`` binds ``args`` as variables of the check expression (see :func:`check_eval_args`);
  a name the expression needs but ``args`` lacks is answered with ``INVALID_PARAMS`` naming it;
* payload keys of deliveries follow §2.6 exactly (``_deliver`` passes them as ``deliver(payload=...)``
  because a payload key such as ``kind`` cannot be given as a keyword argument).
"""

from __future__ import annotations

import copy
import json
import keyword
import logging
import math
import re
from collections.abc import Iterable
from typing import Any

from app.engine.expressions import ExpressionError
from app.world import intents as pl
from app.world.core import EVERYONE, Handler, WorldCore, WorldModule
from app.world.projection import request_view, seat_sorted, ticket_view
from app.world.render import player_label, render_text, validate_params
from app.world.state import AbilityState, RequestAnswer, RequestState, TicketState
from app.world.types import ENGINE_ID, MODERATOR_ID, AgentMemory, Code, Intent, Receipt

logger = logging.getLogger(__name__)

NO_NOTES_TEXT = "（当前你的私人笔记本为空，可调用 write_note 记录关键推理与信息）"
DEFAULT_NOTE_TITLE = "默认笔记"
PREVIEW_CHARS = 60  # length of a ticket preview
MAX_KEEP_RECENT = 1000

# ``mod.evaluate`` arguments are bound as plain variables of the check expression.
MAX_EVAL_ARGS_CHARS = 4000  # serialized size of all arguments together
MAX_EVAL_ARGS_DEPTH = 6  # nesting of lists / dicts inside one argument value
#: Names an evaluate argument may not take: the functions of ``WorldCore.eval_expr``, the ``players``
#: variable and the literals of the expression language.  (``test_game_evaluate`` keeps it in sync
#: with the function table of ``eval_expr``.)
RESERVED_EXPR_NAMES = frozenset(
    {
        "alive", "by_role", "by_faction", "by_tag", "ids", "first", "count", "len", "sum", "min", "max",
        "get", "tally", "leaders", "seat_order", "players", "true", "false", "null",
    }
)
_ARG_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_UNKNOWN_NAME = re.compile(r"Unknown name: ([A-Za-z_][A-Za-z0-9_]*)")


# --------------------------------------------------------------------------- helpers


def _unique(items: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(str(i) for i in items))


def _secs(value: float) -> float:
    return round(max(0.0, float(value)), 1)


def _preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _type_matches(entry_type: str, patterns: list[str]) -> bool:
    """Exact match, ``prefix*`` or ``prefix.`` (e.g. ``request.`` selects every request entry)."""
    for pattern in patterns:
        if entry_type == pattern:
            return True
        if pattern.endswith("*") and entry_type.startswith(pattern[:-1]):
            return True
        if pattern.endswith(".") and entry_type.startswith(pattern):
            return True
    return False


def _cut(text: str, limit: int) -> tuple[str, dict[str, int] | None]:
    """Truncate ``text`` to ``limit`` characters (D19); the second item is the receipt ``truncated``."""
    if limit > 0 and len(text) > limit:
        return text[:limit], {"dropped_chars": len(text) - limit, "kept_chars": limit}
    return text, None


def _is_plain_json(value: Any, depth: int = 0) -> bool:
    """JSON primitives, lists and dicts with string keys (finite numbers only), nested at most
    ``MAX_EVAL_ARGS_DEPTH`` levels."""
    if depth > MAX_EVAL_ARGS_DEPTH:
        return False
    if value is None or isinstance(value, (bool, str, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_plain_json(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_plain_json(v, depth + 1) for k, v in value.items())
    return False


def check_eval_args(args: dict[str, Any]) -> str | None:
    """Why ``args`` cannot be bound as expression variables (a one-sentence reason), or ``None``.

    Keys must be identifiers that do not shadow a built-in name; values must be JSON primitives, lists
    or dicts; the compact JSON of everything together may not exceed ``MAX_EVAL_ARGS_CHARS``.
    """
    for name in args:
        if not _ARG_NAME.fullmatch(name) or keyword.iskeyword(name):
            return f"参数名 {name!r} 不合法：只能由字母、数字、下划线组成，不能以数字开头，也不能是 Python 关键字。"
        if name in RESERVED_EXPR_NAMES:
            return f"参数名 {name!r} 与表达式的内置名字冲突，请换一个名字（内置名字：{'、'.join(sorted(RESERVED_EXPR_NAMES))}）。"
    for name, value in args.items():
        if not _is_plain_json(value):
            return (
                f"参数 {name!r} 的值不合法：只能是字符串、数字、布尔、null、列表或字典"
                f"（字典的键必须是字符串，嵌套不超过 {MAX_EVAL_ARGS_DEPTH} 层，数字必须是有限值）。"
            )
    size = len(json.dumps(args, ensure_ascii=False, separators=(",", ":")))
    if size > MAX_EVAL_ARGS_CHARS:
        return f"args 序列化后有 {size} 个字符，超过上限 {MAX_EVAL_ARGS_CHARS}。"
    return None


def ability_holders(world: WorldCore, ability_id: str) -> list[str]:
    """Player ids the scenario expression of ``ability_id`` selects *right now* (evaluated at call
    time).  Ignores ``enabled`` / ``allowed`` / quota.  A broken expression yields no holders."""
    spec = world.spec.abilities.get(ability_id)
    if spec is None:
        return []
    try:
        plain = WorldCore.to_plain(world.eval_expr(spec.holders))
    except Exception:  # noqa: BLE001 - a broken scenario expression must not crash the world
        logger.exception("ability %s: holders expression failed: %s", ability_id, spec.holders)
        return []
    if isinstance(plain, str):
        return [plain]
    if isinstance(plain, (list, tuple, set)):
        return _unique(i for i in plain if isinstance(i, (str, int)) and not isinstance(i, bool))
    return []


class GameModule(WorldModule):
    name = "game"

    def __init__(self, world: WorldCore) -> None:
        super().__init__(world)
        self._ensure_abilities()

    # ------------------------------------------------------------------ module protocol --
    def handlers(self) -> dict[str, Handler]:
        return {
            "respond": self.on_respond,  # type: ignore[dict-item]
            "use_ability": self.on_use_ability,  # type: ignore[dict-item]
            "challenge": self.on_challenge,  # type: ignore[dict-item]
            "mod.open_request": self.on_open_request,  # type: ignore[dict-item]
            "mod.cancel_request": self.on_cancel_request,  # type: ignore[dict-item]
            "mod.reopen_request": self.on_reopen_request,  # type: ignore[dict-item]
            "mod.update_player": self.on_update_player,  # type: ignore[dict-item]
            "mod.set_phase": self.on_set_phase,  # type: ignore[dict-item]
            "mod.update_dashboard": self.on_update_dashboard,  # type: ignore[dict-item]
            "mod.random": self.on_random,  # type: ignore[dict-item]
            "mod.evaluate": self.on_evaluate,  # type: ignore[dict-item]
            "mod.get_ledger": self.on_get_ledger,  # type: ignore[dict-item]
            "mod.resolve_challenge": self.on_resolve_challenge,  # type: ignore[dict-item]
            "mod.hold_game": self.on_hold_game,  # type: ignore[dict-item]
            "mod.end_game": self.on_end_game,  # type: ignore[dict-item]
            "mod.enable_ability": self.on_enable_ability,  # type: ignore[dict-item]
            "mod.disable_ability": self.on_disable_ability,  # type: ignore[dict-item]
        }

    def tick(self) -> None:
        """Close the open requests whose deadline has passed (idempotent)."""
        now = float(self.w.clock.now())
        for request in list(self.w.state.requests.values()):
            if request.status == "open" and request.deadline is not None and request.deadline <= now:
                self._close_request(request, "deadline")

    def next_deadline(self) -> float | None:
        deadlines = [
            r.deadline
            for r in self.w.state.requests.values()
            if r.status == "open" and r.deadline is not None
        ]
        return min(deadlines) if deadlines else None

    def redeliver(self) -> None:
        """Re-deliver ``request.opened`` for open requests (only to targets who have not answered yet;
        requests already past their deadline are left to the next tick) and ``challenge.opened`` for
        every unresolved ticket (to the moderator)."""
        w = self.w
        now = float(w.clock.now())
        for request in w.state.requests.values():
            if request.status != "open":
                continue
            if request.deadline is not None and request.deadline <= now:
                continue
            seconds = None if request.deadline is None else _secs(request.deadline - now)
            for pid in request.to:
                if pid not in request.answers:
                    self._deliver_request_opened(pid, request, seconds, request.opened_seq)
        for ticket in w.state.tickets.values():
            if ticket.status != "open":
                continue
            dm = self._system_dm(ticket.player_id)
            self._deliver(
                MODERATOR_ID,
                "challenge",
                "challenge.opened",
                {"message_kind": "challenge", "ticket_id": ticket.id},
                priority="urgent",
                channel_id=dm.id if dm else None,
                sender=ticket.player_id,
                seq=ticket.opened_seq,
            )

    def dump(self) -> dict[str, Any]:
        """Nothing beyond ``WorldState`` (requests, tickets, abilities, counters, rng and memory all
        live there); the dict only carries a format marker."""
        return {"format": 1}

    def load(self, data: dict[str, Any]) -> None:
        self._ensure_abilities()

    # ------------------------------------------------------------------------- plumbing --
    def _ensure_abilities(self) -> None:
        for ability_id, spec in self.w.spec.abilities.items():
            if ability_id not in self.w.state.abilities:
                self.w.state.abilities[ability_id] = AbilityState(id=ability_id, enabled=bool(spec.enabled))

    def _ability_state(self, ability_id: str) -> AbilityState:
        state = self.w.state.abilities.get(ability_id)
        if state is None:
            spec = self.w.spec.abilities[ability_id]
            state = AbilityState(id=ability_id, enabled=bool(spec.enabled))
            self.w.state.abilities[ability_id] = state
        return state

    def _deliver(
        self,
        target: str,
        kind: str,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        priority: str = "ambient",
        channel_id: str | None = None,
        sender: str | None = None,
        seq: int = 0,
        interrupt: bool = False,
    ) -> None:
        """Queue one envelope.  ``payload`` is passed as a ready-made dict because some §2.6 payload keys
        (``kind``) collide with the parameter names of ``WorldCore.deliver``."""
        self.w.deliver(
            target,
            kind,
            event,
            priority=priority,
            channel_id=channel_id,
            sender=sender,
            seq=seq,
            interrupt=interrupt,
            payload=payload,
        )

    def _system_dm(self, player_id: str) -> Any:
        """The engine-created DM of ``player_id`` (``dm-<id>``), or ``None``."""
        for channel in self.w.state.channels:
            if channel.system and channel.owner == player_id:
                return channel
        return self.w.channel(f"dm-{player_id}")

    def _players_except(self, *excluded: str) -> list[str]:
        return [p.id for p in self.w.state.players if p.id not in excluded]

    # -------------------------------------------------------------------------- dealing --
    def deal(self) -> list[dict[str, Any]]:
        """Deal ``setup.identity_pool`` to ``state.players`` (already created, in play order).

        Shuffles with ``world.rng`` when ``setup.shuffle``, then assigns in player order.  Each player
        gets ``role`` / ``faction`` / ``tags`` / ``attributes`` of its role, and one ``player.updated``
        entry is written (``actor=engine``, visible to the player and the moderator,
        ``reason="deal"``).  Raises ``ValueError`` when the number of players differs from the size
        of the pool; a scenario without any identity pool deals nothing.
        Returns ``[{"player_id", "role", "faction"}]`` in player order.
        """
        w = self.w
        setup = w.spec.setup
        players = list(w.state.players)
        pool: list[str] = []
        for item in setup.identity_pool:
            pool.extend([item.role] * item.count)
        if not pool:
            return []
        if len(pool) != len(players):
            raise ValueError(f"身份池共有 {len(pool)} 个身份，但对局有 {len(players)} 名玩家，无法发牌。")
        if setup.shuffle:
            w.rng.shuffle(pool)
        dealt: list[dict[str, Any]] = []
        for player, role_id in zip(players, pool):
            role = setup.roles[role_id]
            player.role = role_id
            player.faction = role.faction
            player.tags = _unique(role.tags)
            player.attributes = copy.deepcopy(role.attributes)
            w.commit(
                "player.updated",
                ENGINE_ID,
                {
                    "player_id": player.id,
                    "add_tags": list(player.tags),
                    "remove_tags": [],
                    "set_attributes": copy.deepcopy(player.attributes),
                    "tags_after": list(player.tags),
                    "role": player.role,
                    "faction": player.faction,
                    "reason": "deal",
                },
                visible_to=[player.id, MODERATOR_ID],
            )
            dealt.append({"player_id": player.id, "role": role_id, "faction": role.faction})
        return dealt

    # ---------------------------------------------------------------------- requests ----
    def _deliver_request_opened(
        self, target: str, request: RequestState, seconds: float | None, seq: int
    ) -> None:
        self._deliver(
            target,
            "request",
            "request.opened",
            {
                "request_id": request.id,
                "kind": request.kind,
                "question": request.question,
                "options": copy.deepcopy(request.options),
                "seconds": seconds,
                "sealed": request.sealed,
            },
            priority="urgent",
            seq=seq,
        )

    def _open_request(
        self,
        *,
        to: list[str],
        kind: str,
        question: str,
        options: list[dict[str, str]],
        duration: float,
        sealed: bool,
        default: str | None,
        reopens: str | None = None,
    ) -> RequestState:
        w = self.w
        request_id = f"r{w.next_counter('request')}"
        now = float(w.clock.now())
        request = RequestState(
            id=request_id,
            to=list(to),
            kind=kind,  # type: ignore[arg-type]
            question=question,
            options=copy.deepcopy(options),
            sealed=sealed,
            default=default,
            deadline=now + duration,
            status="open",
            reopens=reopens,
            created_gt=now,
        )
        data: dict[str, Any] = {
            "request_id": request_id,
            "kind": kind,
            "question": question,
            "options": copy.deepcopy(options),
            "to": list(to),
            "duration_s": duration,
            "sealed": sealed,
        }
        if reopens:
            data["reopens"] = reopens
        entry = w.commit("request.opened", MODERATOR_ID, data, visible_to=[MODERATOR_ID, *to])
        request.opened_seq = entry.seq
        w.state.requests[request_id] = request
        for pid in to:
            self._deliver_request_opened(pid, request, duration, entry.seq)
        w.emit("request.opened", request=request_view(w, request))
        return request

    def _check_targets(self, targets: list[str]) -> Receipt | None:
        unknown = [t for t in targets if self.w.player(t) is None]
        if unknown:
            return Receipt.reject(Code.UNKNOWN_PLAYER, f"未知的玩家 id：{'、'.join(unknown)}")
        return None

    def on_open_request(self, intent: Intent, p: pl.ModOpenRequest) -> Receipt:
        w = self.w
        targets = _unique(p.to)
        rejected = self._check_targets(targets)
        if rejected is not None:
            return rejected
        options: list[dict[str, str]] = []
        default = p.default
        if p.kind == "choice":
            options = [{"id": o.id, "label": o.label or o.id} for o in p.options]
        elif default is not None:
            default = default[: w.spec.defaults.request.max_text_chars]
        duration = float(p.duration_s) if p.duration_s else float(w.spec.defaults.request.duration_s)
        request = self._open_request(
            to=targets,
            kind=p.kind,
            question=p.question,
            options=options,
            duration=duration,
            sealed=p.sealed,
            default=default,
        )
        return Receipt.accept(
            seq=request.opened_seq,
            message=(
                f"已向 {len(targets)} 名玩家发出请求 {request.id}（{duration:g} 秒内作答）。"
                "无需等待：全员作答或到期后，你会收到汇总。"
            ),
            request_id=request.id,
            to=targets,
            duration_s=duration,
        )

    def on_respond(self, intent: Intent, p: pl.Respond) -> Receipt:
        w = self.w
        actor = intent.actor_id
        request = w.state.requests.get(p.request_id)
        if request is None:
            return Receipt.reject(Code.UNKNOWN_REQUEST, f"请求 {p.request_id} 不存在。")
        if request.status != "open":
            state = "已被取消" if request.status == "cancelled" else "已经关闭"
            return Receipt.reject(Code.REQUEST_CLOSED, f"请求 {request.id} {state}，不能再作答。")
        if request.deadline is not None and request.deadline <= float(w.clock.now()):
            return Receipt.reject(Code.REQUEST_CLOSED, f"请求 {request.id} 已到期，不能再作答。")
        if actor not in request.to:
            return Receipt.reject(Code.FORBIDDEN, f"请求 {request.id} 不是发给你的。")

        truncated: dict[str, int] | None = None
        if request.kind == "choice":
            value = p.value.strip()
            valid = [o["id"] for o in request.options]
            if value not in valid:
                return Receipt.reject(
                    Code.INVALID_TARGET,
                    f"「{p.value}」不是有效的选项。合法的选项 id：{'、'.join(valid)}。",
                    options=valid,
                )
        else:
            value, truncated = _cut(p.value.strip(), w.spec.defaults.request.max_text_chars)
            if not value:
                return Receipt.reject(Code.INVALID_PARAMS, "回答内容不能为空。")

        replaced = actor in request.answers
        entry = w.commit(
            "request.answered",
            actor,
            {
                "request_id": request.id,
                "player_id": actor,
                "value": value,
                "reason": p.reason,
                "replaced": replaced,
            },
            visible_to=[] if request.sealed else [MODERATOR_ID, actor],
        )
        request.answers[actor] = RequestAnswer(value=value, reason=p.reason, defaulted=False, seq=entry.seq)
        w.emit(
            "request.progress",
            request_id=request.id,
            answered_by=seat_sorted(w, request.answers),
            total=len(request.to),
        )
        closed = False
        if all(target in request.answers for target in request.to):
            self._close_request(request, "all_answered")
            closed = True
        note = "（已覆盖你之前的回答）" if replaced else ""
        tail = "请求已收齐并关闭。" if closed else "在请求关闭前你可以再次作答来修改。"
        return Receipt.accept(
            seq=entry.seq,
            message=f"已记录你对 {request.id} 的回答{note}。{tail}",
            truncated=truncated,
            request_id=request.id,
            value=value,
            replaced=replaced,
            closed=closed,
        )

    @staticmethod
    def _tally(request: RequestState) -> tuple[dict[str, int], list[str]]:
        """Votes per option id of a ``choice`` request (defaulted answers included; every option is
        listed, in option order) and the options with the most votes (several on a tie, none when nobody
        voted)."""
        counts = {option["id"]: 0 for option in request.options}
        for answer in request.answers.values():
            if answer.value in counts:
                counts[answer.value] += 1
        top = max(counts.values(), default=0)
        return counts, ([option_id for option_id, n in counts.items() if n == top] if top > 0 else [])

    def _close_request(self, request: RequestState, reason: str) -> None:
        """Close an open request (``reason``: ``all_answered`` | ``deadline``).

        Unanswered targets get their ``default`` (marked ``defaulted``) when there is one; the ledger
        entry ``request.closed`` (moderator only) carries every answer in seat order and, for ``choice``
        requests, ``tally`` / ``leaders``; the moderator is woken up with the same payload; targets who
        had not answered are told the request expired.
        """
        w = self.w
        request.status = "closed"
        ordered = seat_sorted(w, request.to)
        defaulted: list[str] = []
        missing: list[str] = []
        for pid in ordered:
            if pid in request.answers:
                continue
            if request.default is not None:
                request.answers[pid] = RequestAnswer(value=request.default, defaulted=True, seq=0)
                defaulted.append(pid)
            else:
                missing.append(pid)
        answers = [
            {
                "player_id": pid,
                "value": request.answers[pid].value,
                "reason": request.answers[pid].reason,
                "defaulted": request.answers[pid].defaulted,
            }
            for pid in ordered
            if pid in request.answers
        ]
        summary: dict[str, Any] = {
            "request_id": request.id,
            "reason": reason,
            "answers": answers,
            "defaulted": defaulted,
            "missing": missing,
        }
        if request.kind == "choice":
            summary["tally"], summary["leaders"] = self._tally(request)
        entry = w.commit("request.closed", ENGINE_ID, copy.deepcopy(summary), visible_to=[MODERATOR_ID])
        self._deliver(
            MODERATOR_ID, "request", "request.closed", copy.deepcopy(summary), priority="urgent", seq=entry.seq
        )
        for pid in ordered:
            if pid in defaulted or pid in missing:
                self._deliver(
                    pid,
                    "request",
                    "request.cancelled",
                    {"request_id": request.id, "reason": "expired"},
                    priority="ambient",
                    interrupt=True,
                    seq=entry.seq,
                )
        w.emit(
            "request.closed",
            request_id=request.id,
            reason=reason,
            answered_by=[pid for pid in ordered if pid in request.answers and not request.answers[pid].defaulted],
            defaulted=list(defaulted),
        )

    def on_cancel_request(self, intent: Intent, p: pl.ModRequestRef) -> Receipt:
        w = self.w
        request = w.state.requests.get(p.request_id)
        if request is None:
            return Receipt.reject(Code.UNKNOWN_REQUEST, f"请求 {p.request_id} 不存在。")
        if request.status != "open":
            state = "已被取消" if request.status == "cancelled" else "已经关闭"
            return Receipt.reject(Code.REQUEST_CLOSED, f"请求 {request.id} {state}，无需再取消。")
        request.status = "cancelled"
        entry = w.commit(
            "request.cancelled",
            MODERATOR_ID,
            {"request_id": request.id, "reason": "cancelled"},
            visible_to=[MODERATOR_ID, *request.to],
        )
        for pid in request.to:
            self._deliver(
                pid,
                "request",
                "request.cancelled",
                {"request_id": request.id, "reason": "cancelled"},
                priority="ambient",
                interrupt=True,
                seq=entry.seq,
            )
        w.emit(
            "request.closed",
            request_id=request.id,
            reason="cancelled",
            answered_by=seat_sorted(w, [pid for pid, a in request.answers.items() if not a.defaulted]),
            defaulted=[],
        )
        return Receipt.accept(seq=entry.seq, message=f"请求 {request.id} 已取消。", request_id=request.id)

    def on_reopen_request(self, intent: Intent, p: pl.ModReopenRequest) -> Receipt:
        w = self.w
        old = w.state.requests.get(p.request_id)
        if old is None:
            return Receipt.reject(Code.UNKNOWN_REQUEST, f"请求 {p.request_id} 不存在。")
        if old.status == "open":
            return Receipt.reject(
                Code.INVALID_TARGET, f"请求 {old.id} 仍在进行中；只能重开已关闭或已取消的请求。"
            )
        targets = _unique(p.to) if p.to else list(old.to)
        rejected = self._check_targets(targets)
        if rejected is not None:
            return rejected
        if p.duration_s:
            duration = float(p.duration_s)
        elif old.deadline is not None and old.deadline > old.created_gt:
            duration = float(old.deadline - old.created_gt)  # same window as the original
        else:
            duration = float(w.spec.defaults.request.duration_s)
        request = self._open_request(
            to=targets,
            kind=old.kind,
            question=old.question,
            options=old.options,
            duration=duration,
            sealed=old.sealed,
            default=old.default,
            reopens=old.id,
        )
        return Receipt.accept(
            seq=request.opened_seq,
            message=f"已重开请求 {old.id}，新请求 {request.id}（{duration:g} 秒内作答）。",
            request_id=request.id,
            reopens=old.id,
            to=targets,
            duration_s=duration,
        )

    # ---------------------------------------------------------------------- abilities ---
    def on_use_ability(self, intent: Intent, p: pl.UseAbility) -> Receipt:
        w = self.w
        actor = intent.actor_id
        spec = w.spec.abilities.get(p.ability_id)
        if spec is None:
            known = "、".join(w.spec.abilities) or "（无）"
            return Receipt.reject(Code.UNKNOWN_ABILITY, f"没有名为 {p.ability_id} 的能力。已知能力：{known}")
        state = self._ability_state(p.ability_id)
        if not state.enabled or (state.allowed is not None and actor not in state.allowed):
            return Receipt.reject(Code.ABILITY_DISABLED, f"能力 {p.ability_id} 当前不可用（未启用）。")
        if actor not in ability_holders(w, p.ability_id):
            return Receipt.reject(Code.FORBIDDEN, f"你不是能力 {p.ability_id} 的持有者。")
        used = int(state.uses.get(actor, 0))
        if used >= spec.quota:
            return Receipt.reject(Code.QUOTA_EXCEEDED, f"能力 {p.ability_id} 的使用次数已用完（共 {spec.quota} 次）。")
        clean, rejected = validate_params(spec.params, p.params, w)
        if rejected is not None:
            return rejected
        assert clean is not None

        state.uses[actor] = used + 1
        entry = w.commit(
            "ability.used",
            actor,
            {"ability_id": p.ability_id, "actor_id": actor, "params": copy.deepcopy(clean)},
            visible_to=[actor, MODERATOR_ID],
        )
        reveal = spec.reveal
        if reveal is not None and w.channel(reveal.channel_id) is not None:
            text = render_text(
                reveal.text, clean, spec.params, w, extra={"actor": player_label(w, actor)}
            )
            w.post_message(reveal.channel_id, actor, text, kind="ability")
        chat = getattr(w, "chat", None)
        if chat is not None:
            for channel_id in spec.freeze_channels:
                if w.channel(channel_id) is not None:
                    chat.freeze_channel(channel_id, f"ability:{p.ability_id}")
        self._deliver(
            MODERATOR_ID,
            "ability",
            "ability.used",
            {"ability_id": p.ability_id, "actor_id": actor, "params": copy.deepcopy(clean)},
            priority="urgent",
            sender=actor,
            seq=entry.seq,
        )
        w.emit("ability.used", ability_id=p.ability_id, actor_id=actor)
        uses_left = max(0, spec.quota - (used + 1))
        return Receipt.accept(
            seq=entry.seq,
            message=f"已发动能力 {p.ability_id}（剩余 {uses_left} 次）。后续裁决由主持人完成。",
            ability_id=p.ability_id,
            uses_left=uses_left,
        )

    def on_enable_ability(self, intent: Intent, p: pl.ModAbilityToggle) -> Receipt:
        w = self.w
        if p.ability_id not in w.spec.abilities:
            known = "、".join(w.spec.abilities) or "（无）"
            return Receipt.reject(Code.UNKNOWN_ABILITY, f"没有名为 {p.ability_id} 的能力。已知能力：{known}")
        data: dict[str, Any] = {"ability_id": p.ability_id}
        allowed: list[str] | None = None
        if p.holders is not None:
            allowed = _unique(p.holders)
            rejected = self._check_targets(allowed)
            if rejected is not None:
                return rejected
            data["holders"] = list(allowed)
        state = self._ability_state(p.ability_id)
        state.enabled = True
        state.allowed = allowed
        entry = w.commit("ability.enabled", MODERATOR_ID, data, visible_to=[MODERATOR_ID])
        scope = f"（限 {'、'.join(allowed)}）" if allowed is not None else "（所有持有者）"
        return Receipt.accept(seq=entry.seq, message=f"能力 {p.ability_id} 已启用{scope}。", ability_id=p.ability_id)

    def on_disable_ability(self, intent: Intent, p: pl.ModAbilityToggle) -> Receipt:
        w = self.w
        if p.ability_id not in w.spec.abilities:
            known = "、".join(w.spec.abilities) or "（无）"
            return Receipt.reject(Code.UNKNOWN_ABILITY, f"没有名为 {p.ability_id} 的能力。已知能力：{known}")
        if p.holders is not None:
            return Receipt.reject(
                Code.INVALID_PARAMS,
                "disable_ability 不接受 holders（它会对所有持有者关闭该能力）；"
                "若只想让部分玩家可用，请用 enable_ability 并指定 holders。",
            )
        self._ability_state(p.ability_id).enabled = False
        entry = w.commit(
            "ability.disabled", MODERATOR_ID, {"ability_id": p.ability_id}, visible_to=[MODERATOR_ID]
        )
        return Receipt.accept(seq=entry.seq, message=f"能力 {p.ability_id} 已关闭。", ability_id=p.ability_id)

    # ---------------------------------------------------------------------- challenges --
    def on_challenge(self, intent: Intent, p: pl.Challenge) -> Receipt:
        w = self.w
        actor = intent.actor_id
        dm = self._system_dm(actor)
        if dm is None or dm.mode in ("closed", "listen"):
            return Receipt.reject(Code.CHANNEL_CLOSED, "你与主持人的私聊已被关闭，无法提出质询。")
        content, truncated = _cut(p.content.strip(), w.spec.defaults.dm.max_chars_per_message)
        if not content:
            return Receipt.reject(Code.INVALID_PARAMS, "质询内容不能为空。")
        ref_seq = p.ref_seq if p.ref_seq is not None and 1 <= p.ref_seq <= w.seq else None
        ticket_id = f"t{w.next_counter('ticket')}"
        extra: dict[str, Any] = {"ticket_id": ticket_id}
        if ref_seq is not None:
            extra["ref_seq"] = ref_seq
        entry = w.post_message(dm.id, actor, content, kind="challenge", truncated=truncated, extra=extra)
        ticket = TicketState(
            id=ticket_id,
            player_id=actor,
            status="open",
            opened_seq=entry.seq,
            ref_seq=ref_seq,
            preview=_preview(content),
        )
        w.state.tickets[ticket_id] = ticket
        w.emit("challenge.opened", ticket=ticket_view(ticket))
        return Receipt.accept(
            seq=entry.seq,
            message=f"质询已提交（工单 {ticket_id}），主持人会回复你。",
            truncated=truncated,
            ticket_id=ticket_id,
        )

    def on_resolve_challenge(self, intent: Intent, p: pl.ModResolveChallenge) -> Receipt:
        w = self.w
        ticket = w.state.tickets.get(p.ticket_id)
        if ticket is None:
            return Receipt.reject(Code.UNKNOWN_TICKET, f"工单 {p.ticket_id} 不存在。")
        if ticket.status != "open":
            return Receipt.reject(
                Code.INVALID_TARGET, f"工单 {ticket.id} 已经处理过（结论：{ticket.verdict}）。"
            )
        dm = self._system_dm(ticket.player_id)
        if dm is None:
            return Receipt.reject(Code.UNKNOWN_CHANNEL, f"找不到玩家 {ticket.player_id} 的系统私聊。")
        content, truncated = _cut(p.reply.strip(), w.spec.defaults.moderator.max_chars_per_message)
        if not content:
            return Receipt.reject(Code.INVALID_PARAMS, "回复内容不能为空。")
        entry = w.post_message(
            dm.id,
            MODERATOR_ID,
            content,
            kind="reply",
            truncated=truncated,
            extra={"ticket_id": ticket.id, "verdict": p.verdict},
        )
        ticket.status = "resolved"
        ticket.verdict = p.verdict
        w.emit("challenge.resolved", ticket_id=ticket.id, verdict=p.verdict)
        return Receipt.accept(
            seq=entry.seq,
            message=f"工单 {ticket.id} 已处理（{p.verdict}），回复已发给玩家。",
            truncated=truncated,
            ticket_id=ticket.id,
        )

    # --------------------------------------------------------------------------- players --
    def on_update_player(self, intent: Intent, p: pl.ModUpdatePlayer) -> Receipt:
        w = self.w
        player = w.player(p.player_id)
        if player is None:
            return Receipt.reject(Code.UNKNOWN_PLAYER, f"未知的玩家 id：{p.player_id}")

        before = list(player.tags)
        removing = set(p.remove_tags)
        after = [t for t in before if t not in removing]
        for tag in _unique(p.add_tags):
            if tag not in after:
                after.append(tag)
        added = [t for t in after if t not in before]
        removed = [t for t in before if t not in after]

        attributes: dict[str, Any] = {}
        new_role = new_faction = None
        for key, value in p.set_attributes.items():
            if key == "role":
                if str(value) != player.role:
                    new_role = str(value)
            elif key == "faction":
                if str(value) != player.faction:
                    new_faction = str(value)
            elif key not in player.attributes or player.attributes[key] != value:
                attributes[key] = copy.deepcopy(value)

        if not (added or removed or attributes or new_role is not None or new_faction is not None):
            return Receipt.accept(
                message="没有变化：该玩家已经是这个状态。",
                player_id=player.id,
                changed=False,
                tags=list(player.tags),
            )

        player.tags = after
        player.attributes.update(attributes)
        data: dict[str, Any] = {
            "player_id": player.id,
            "add_tags": added,
            "remove_tags": removed,
            "set_attributes": copy.deepcopy(attributes),
            "tags_after": list(player.tags),
        }
        if new_role is not None:
            player.role = new_role
            data["role"] = new_role
        if new_faction is not None:
            player.faction = new_faction
            data["faction"] = new_faction
        entry = w.commit("player.updated", MODERATOR_ID, data, visible_to=[player.id, MODERATOR_ID])
        self._deliver(
            player.id,
            "notice",
            "player.updated",
            {"player_id": player.id, "add_tags": added, "remove_tags": removed},
            seq=entry.seq,
        )

        public = set(w.spec.setup.public_tags)
        pub_added = [t for t in added if t in public]
        pub_removed = [t for t in removed if t in public]
        if pub_added or pub_removed:
            audience = [MODERATOR_ID, *self._players_except(player.id)]
            public_entry = w.commit(
                "player.public_changed",
                MODERATOR_ID,
                {"player_id": player.id, "added": pub_added, "removed": pub_removed},
                visible_to=audience,
            )
            for other in self._players_except(player.id):
                self._deliver(
                    other,
                    "notice",
                    "player.updated",
                    {"player_id": player.id, "added": pub_added, "removed": pub_removed},
                    seq=public_entry.seq,
                )
        w.emit("player.updated", player_id=player.id, tags=list(player.tags), out=bool(w.is_out(player)))
        return Receipt.accept(
            seq=entry.seq,
            message=f"已更新玩家 {player.id}：现有标签 {list(player.tags)}。",
            player_id=player.id,
            changed=True,
            tags=list(player.tags),
            attributes=copy.deepcopy(player.attributes),
        )

    # ---------------------------------------------------------------------- game status --
    def on_set_phase(self, intent: Intent, p: pl.ModSetPhase) -> Receipt:
        w = self.w
        text = p.text.strip()
        if not text:
            return Receipt.reject(Code.INVALID_PARAMS, "阶段文字不能为空。")
        w.state.phase = text
        entry = w.commit("phase.changed", MODERATOR_ID, {"text": text}, visible_to=EVERYONE)
        for pid in self._players_except():
            self._deliver(pid, "notice", "phase.changed", {"text": text}, seq=entry.seq)
        w.emit("phase.changed", text=text)
        return Receipt.accept(seq=entry.seq, message=f"阶段已更新为：{text}")

    def on_update_dashboard(self, intent: Intent, p: pl.ModUpdateDashboard) -> Receipt:
        w = self.w
        dashboard = {
            "factions": [f.model_dump() for f in p.factions],
            "tag_stats": [t.model_dump() for t in p.tag_stats],
        }
        w.state.dashboard.factions = copy.deepcopy(dashboard["factions"])
        w.state.dashboard.tag_stats = copy.deepcopy(dashboard["tag_stats"])
        entry = w.commit("dashboard.updated", MODERATOR_ID, copy.deepcopy(dashboard), visible_to=EVERYONE)
        w.emit("dashboard.updated", dashboard=copy.deepcopy(dashboard))
        return Receipt.accept(seq=entry.seq, message="看板已更新。")

    def on_hold_game(self, intent: Intent, p: pl.ModHoldGame) -> Receipt:
        w = self.w
        reason = p.reason.strip()
        w.set_running(False, f"hold:{reason}" if reason else "hold")
        self._deliver(ENGINE_ID, "notice", "hold_game", {"reason": reason})
        return Receipt.accept(seq=w.seq, message="对局已暂停，等待人工决定何时继续。")

    def on_end_game(self, intent: Intent, p: pl.ModEndGame) -> Receipt:
        w = self.w
        state = w.state
        state.status = "finished"
        state.winner = p.winner
        state.summary = p.summary
        entry = w.commit(
            "game.status",
            MODERATOR_ID,
            {"status": "finished", "reason": "end_game", "winner": p.winner, "summary": p.summary},
            visible_to=EVERYONE,
        )
        w.emit("game.status", status="finished", reason="end_game", winner=p.winner, summary=p.summary)
        w.set_running(False, "end_game")  # status is already "finished": this only closes the gate
        self._deliver(ENGINE_ID, "notice", "end_game", {"winner": p.winner, "summary": p.summary})
        return Receipt.accept(seq=entry.seq, message=f"对局已结束：{p.winner} 获胜。", winner=p.winner)

    # --------------------------------------------------------------------- randomness ---
    def on_random(self, intent: Intent, p: pl.ModRandom) -> Receipt:
        w = self.w
        if p.kind == "int":
            assert p.lo is not None and p.hi is not None
            result: Any = w.rng.randint(p.lo, p.hi)
            args: dict[str, Any] = {"lo": p.lo, "hi": p.hi}
        elif p.kind == "choice":
            result = w.rng.sample(list(p.items), p.k)
            args = {"items": copy.deepcopy(p.items), "k": p.k}
        else:
            result = list(p.items)
            w.rng.shuffle(result)
            args = {"items": copy.deepcopy(p.items)}
        entry = w.commit(
            "random.drawn",
            MODERATOR_ID,
            {"kind": p.kind, "args": args, "result": copy.deepcopy(result)},
            visible_to=[MODERATOR_ID],
        )
        return Receipt.accept(seq=entry.seq, message=f"随机结果：{result}", result=result, kind=p.kind)

    # ----------------------------------------------------------------------- read-only --
    def on_evaluate(self, intent: Intent, p: pl.ModEvaluate) -> Receipt:
        """Run ``spec.checks[check_id]``.  ``args`` are bound as variables of the expression (their keys
        are the variable names); see :func:`check_eval_args` for what is accepted."""
        w = self.w
        expression = w.spec.checks.get(p.check_id)
        if expression is None:
            known = "、".join(w.spec.checks) or "（无）"
            return Receipt.reject(Code.INVALID_TARGET, f"没有名为 {p.check_id} 的检查项。可用检查项：{known}")
        problem = check_eval_args(p.args)
        if problem is not None:
            return Receipt.reject(Code.INVALID_PARAMS, problem)
        try:
            value = WorldCore.to_plain(w.eval_expr(expression, extra=p.args))
        except ExpressionError as exc:
            missing = _UNKNOWN_NAME.search(str(exc))
            if missing is not None:
                name = missing.group(1)
                given = "、".join(p.args) or "（没有传入任何 args）"
                return Receipt.reject(
                    Code.INVALID_PARAMS,
                    f"检查项 {p.check_id} 的表达式用到了没有传入的名字：{name}。请在 args 里提供它"
                    f"（args 的键名就是表达式里的变量名）。已传入：{given}。",
                    missing=name,
                )
            logger.exception("check %s failed", p.check_id)
            return Receipt.reject(Code.INTERNAL_ERROR, f"检查项 {p.check_id} 执行失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - scenario expression error
            logger.exception("check %s failed", p.check_id)
            return Receipt.reject(Code.INTERNAL_ERROR, f"检查项 {p.check_id} 执行失败：{exc}")
        return Receipt.accept(message=f"{p.check_id} = {value}", check_id=p.check_id, value=value)

    def on_get_ledger(self, intent: Intent, p: pl.ModGetLedger) -> Receipt:
        w = self.w
        entries = w.entries_visible(MODERATOR_ID, after_seq=p.after_seq)
        if p.channel_id:
            entries = [e for e in entries if e.channel_id == p.channel_id]
        if p.types:
            entries = [e for e in entries if _type_matches(e.type, p.types)]
        total = len(entries)
        entries = entries[-p.limit :]
        rows = [
            {
                "seq": e.seq,
                "type": e.type,
                "channel_id": e.channel_id,
                "actor_id": e.actor_id,
                "data": copy.deepcopy(e.data),
            }
            for e in entries
        ]
        return Receipt.accept(
            message=f"共 {total} 条匹配，返回最近的 {len(rows)} 条。",
            entries=rows,
            count=len(rows),
            omitted_older=total - len(rows),
            latest_seq=w.seq,
        )

    # ------------------------------------------------------------------ board (WorldPort) --
    def board(self, agent_id: str) -> dict[str, Any]:
        from app.world.board import build_board  # local import: board.py imports this module

        return build_board(self.w, agent_id)

    # ------------------------------------------------------------- memory (WorldPort) ----
    def memory(self, agent_id: str) -> AgentMemory:
        return self.w.state.memory.setdefault(agent_id, AgentMemory())

    def write_note(self, agent_id: str, title: str, content: str, mode: str = "overwrite") -> str:
        """Create / replace (``overwrite``) or extend (``append``) the note ``title``; returns the stored
        text."""
        notes = self.memory(agent_id).notes
        key = (title or "").strip() or DEFAULT_NOTE_TITLE
        text = (content or "").strip()
        if mode == "append" and notes.get(key):
            notes[key] = f"{notes[key]}\n{text}"
        else:
            notes[key] = text
        return notes[key]

    def read_notes_text(self, agent_id: str) -> str:
        notes = self.memory(agent_id).notes
        if not notes:
            return NO_NOTES_TEXT
        return "\n\n".join(f"#### 📌 {title}\n{text}" for title, text in notes.items())

    def compact(self, agent_id: str, summary: str, keep_recent_count: int = 10) -> dict[str, Any]:
        """Store ``summary`` and move ``summary_upto_seq`` so that only the newest ``keep_recent_count``
        ledger entries visible to the agent stay un-summarized.  ``summary_upto_seq`` never moves back."""
        text = (summary or "").strip()
        if not text:
            return {"ok": False, "error": "摘要不能为空，未执行压缩。"}
        keep = 10 if keep_recent_count is None else max(0, min(int(keep_recent_count), MAX_KEEP_RECENT))
        visible = [e.seq for e in self.w.ledger if agent_id in e.visible_to_ids]
        memory = self.memory(agent_id)
        upto = visible[len(visible) - keep - 1] if len(visible) > keep else 0
        memory.summary = text
        memory.summary_upto_seq = max(memory.summary_upto_seq, upto)
        memory.compactions += 1
        return {
            "ok": True,
            "summary_upto_seq": memory.summary_upto_seq,
            "keep_recent_count": keep,
            "compactions": memory.compactions,
            "kept_entries": sum(1 for seq in visible if seq > memory.summary_upto_seq),
        }
