"""Chat module: channels, floors (turn / queue / free), speech and message templates.

Contracts: docs/v0.12-contracts.md §2.3 (semantics), §2.4 (freezing), §2.5 (ledger catalog), §2.6
(deliveries), §8.1 / §8.2 (views and UI events).  This module owns *who may write where and how much*;
it knows nothing about any concrete game.

Where the contract is silent the following (additive) choices were made:

* ledger entries produced by the engine on its own (token advancement, timeouts, completion, freezes)
  carry ``actor_id = engine``; entries caused by a moderator intent carry ``moderator``;
* ``floor.token_ended`` is written for every token that ends while *active* (with ``data.cause`` when it
  was revoked); when a whole group is revoked (replaced / closed / mode change) its still-pending tokens
  leave no entry, and only the active holders get ``floor.revoked``;
* a queue may contain the same holder several times (one token per position); a ``turn`` group holds
  each holder once;
* moderator messages never count against any quota, but they do count as activity in a free discussion
  (they reset the silence timer);
* ``open_floor`` / ``update_channel`` take ONE ``limits`` object; the keys are checked against the mode
  (``turn``: TOKEN_LIMIT_KEYS, ``free``: FREE_LIMIT_KEYS) before the values are validated (v0.12.1);
  ``free_limits`` is only a legacy alias that ``intents.py`` maps to ``limits``;
* every limit of a free discussion is an *absolute* cap over the whole discussion (``duration_s`` is
  measured from the start, counters never restart; values already exceeded are refused);
  ``open_floor(mode=free)`` is what restarts a discussion.  Setting ``limits`` re-arms every
  advisory and restarts the silence countdown;
* delivery (contracts §10.1): every envelope wakes its recipient (no ambient level, no debounce); a
  message reaches everybody who can read the channel except its author; envelopes addressed to one
  person (``floor.*`` of a token holder, anything in a system DM) are ``direct`` and also reach a player
  who is out, broadcasts do not (E23);
* system DMs never use tokens or free-discussion limits: their only limits are ``defaults.dm`` and the
  moderator-controlled ``mode``; they cannot be given a floor (``open_floor`` / ``set_floor_queue``);
* (v0.14, H16) a player's ``speak`` longer than the cap of the moment (``speech_gate(...).max_chars``: the token's
  per-message cap clipped to what is left of its total / the free discussion's / the system DM's) is **sent back**:
  ``TOO_LONG`` with ``chars`` and ``limit``, nothing written, nothing booked, the floor untouched; it is never cut.
  Only the moderator's ``send_message`` (2000) is still cut, with ``truncated`` in its receipt;
* ``resume_queue`` on a channel without a group but frozen (a free discussion) just lifts the freeze;
* (v0.13.1, G10 / G14) a token held by one of ``world.untimed_holders`` (the seat a person plays) in a channel of
  ``type == "public"`` has **no deadline**: ``deadline`` stays ``None`` and ``remaining`` holds its nominal time
  (what it resumes with when the seat is given back); its ``floor.granted`` entry carries ``untimed: true`` and
  ``FloorView.untimed`` tells the client.  Pausing / resuming the group and a change of the set
  (:meth:`ChatModule.untimed_changed`) respect it.  Message and character limits are unchanged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.world.core import Handler, WorldCore, WorldModule
from pydantic import ValidationError

from app.world.intents import (
    FREE_LIMIT_KEYS,
    TOKEN_LIMIT_KEYS,
    FloorLimits,
    FreeLimits,
    ModAnnounce,
    ModChannelRef,
    ModCreateChannel,
    ModOpenFloor,
    ModSendMessage,
    ModSetFloorQueue,
    ModUpdateChannel,
    OpenLimits,
    Pass,
    Speak,
    SpeakTemplate,
)
from app.world.render import render_text, validate_params
from app.world.state import ChannelState, FloorGroup, FreeState, Token
from app.world.types import ENGINE_ID, MODERATOR_ID, Code, Intent, LedgerEntry, Receipt

logger = logging.getLogger(__name__)

FLOOR_MODES = ("turn", "queue")  # channel modes that are driven by a FloorGroup
_FREE_LIMIT_FIELDS = FREE_LIMIT_KEYS  # the settings of a free discussion (FreeState carries exactly these)


# --------------------------------------------------------------------------- small helpers


def system_dm_id(player_id: str) -> str:
    """Id of the engine-created DM between ``player_id`` and the moderator (D23)."""
    return f"dm-{player_id}"


def _now(world: WorldCore) -> float:
    return float(world.clock.now())


def _r1(value: float | None) -> float | None:
    return None if value is None else round(float(value), 1)


def _dedupe(items: Any) -> list[str]:
    return list(dict.fromkeys(str(i) for i in items))


def _label(world: WorldCore, agent_id: str) -> str:
    name = world.display_name(agent_id)
    return agent_id if name == agent_id else f"{agent_id} {name}"


def _clip(text: str, limit: int | None) -> tuple[str, dict[str, int] | None]:
    """Cut ``text`` to ``limit`` characters (D19).  Returns ``(kept, truncated | None)``.

    Only the moderator's free text (``mod.send_message``) is cut this way.  A *player's* speech is never cut (v0.14,
    H16): over its limit it is refused with ``TOO_LONG`` and the player rewrites it."""
    if limit is None or len(text) <= limit:
        return text, None
    return text[:limit], {"dropped_chars": len(text) - limit, "kept_chars": limit}


def token_seconds_left(world: WorldCore, token: Token) -> float | None:
    """Seconds an *active* token has left (the frozen value while its group is paused)."""
    if token.status != "active":
        return None
    if token.deadline is not None:
        return _r1(max(0.0, token.deadline - _now(world)))
    if token.remaining is not None:
        return _r1(max(0.0, token.remaining))
    return None


def token_untimed(world: WorldCore, channel: ChannelState, token: Token) -> bool:
    """The floor of ``token`` has no deadline (v0.13.1, G10 / G14): its holder is one of the world's
    ``untimed_holders`` - the seat a person plays - and the channel is a *public* one (by ``type``, never by id).
    Messages and characters stay limited, only the clock is off; every other channel and every request keeps its
    deadline.  While untimed, an active token has ``deadline is None`` and keeps the nominal time it has left in
    ``remaining`` (what it resumes with when the seat is given back)."""
    return channel.type == "public" and token.holder in world.untimed_holders


def _active_tokens(group: FloorGroup | None) -> list[Token]:
    return [t for t in group.tokens if t.status == "active"] if group is not None else []


def _free_writers(channel: ChannelState) -> list[str]:
    """Players who may currently write into a free discussion (the moderator is not listed)."""
    free = channel.free
    out: list[str] = []
    for member in channel.members:
        if member == MODERATOR_ID or member in channel.readonly:
            continue
        if free is not None and free.writers is not None and member not in free.writers:
            continue
        out.append(member)
    return out


def _limits_dict(token: Token) -> dict[str, Any]:
    return {
        "max_messages": token.max_messages,
        "max_chars_per_message": token.max_chars_per_message,
        "max_chars_total": token.max_chars_total,
        "duration_s": token.duration_s,
    }


# --------------------------------------------------------------------------- views


def free_view(world: WorldCore, channel: ChannelState) -> dict[str, Any] | None:
    """Progress of a free discussion (``None`` when the channel has none)."""
    free = channel.free
    if free is None or channel.mode != "free":
        return None
    now = _now(world)
    return {
        "chars_used": free.chars_used,
        "max_chars_total": free.max_chars_total,
        "elapsed_s": _r1(max(0.0, now - free.started_gt)),
        "duration_s": _r1(free.duration_s),
        "silent_s": _r1(max(0.0, now - max(free.started_gt, free.last_message_gt))),
        "message_counts": dict(free.counts),
        # extras beyond the contract shape (useful to the UI and the state board)
        "max_messages_per_member": free.max_messages_per_member,
        "max_chars_per_message": free.max_chars_per_message,
        "writers": list(free.writers) if free.writers is not None else None,
    }


def floor_view(world: WorldCore, channel: ChannelState) -> dict[str, Any]:
    """``FloorView`` (contracts §8.1).

    ``active`` is the holder of the first active token (a ``turn`` group may have several active
    tokens: read ``queue[].state`` for the full picture); ``seconds_left`` is the smallest remaining
    time among the active tokens that have a deadline.  ``untimed`` (v0.13.1, G14) is true when there are
    active tokens and none of them has a deadline (the person who plays a seat holds a public floor): then
    ``seconds_left`` is ``None`` and the client shows "不限时" instead of a countdown.
    """
    group = channel.group if channel.mode in FLOOR_MODES else None
    if group is not None:
        active = _active_tokens(group)
        timed = [t for t in active if not token_untimed(world, channel, t)]
        lefts = [s for s in (token_seconds_left(world, t) for t in timed) if s is not None]
        return {
            "kind": group.mode,
            "active": active[0].holder if active else None,
            "paused": bool(group.paused),
            "seconds_left": min(lefts) if lefts else None,
            "untimed": bool(active) and not timed,
            "queue": [
                {
                    "holder": t.holder,
                    "state": t.status,
                    "messages_used": t.messages_used,
                    "max_messages": t.max_messages,
                }
                for t in group.tokens
            ],
            "free": None,
        }
    free = free_view(world, channel)
    if free is not None:
        return {
            "kind": "free", "active": None, "paused": False, "seconds_left": None, "untimed": False, "queue": [], "free": free,
        }
    return {"kind": "none", "active": None, "paused": False, "seconds_left": None, "untimed": False, "queue": [], "free": None}


def channel_view(world: WorldCore, channel: ChannelState) -> dict[str, Any]:
    """``ChannelView`` (contracts §8.1), used by the snapshot, ``channel.*`` ledger entries and UI events."""
    return {
        "id": channel.id,
        "name": channel.name,
        "type": channel.type,
        "mode": channel.mode,
        "members": list(channel.members),
        "readonly": list(channel.readonly),
        "frozen": bool(channel.frozen),
        "system": bool(channel.system),
        "owner": channel.owner,
        "floor": floor_view(world, channel),
    }


# --------------------------------------------------------------------------- who may write?


@dataclass
class _Gate:
    """What a writer may do in a channel right now (the outcome of :func:`speech_gate`)."""

    kind: str  # moderator | dm | token | free
    max_chars: int | None  # cap for one free-text message (``None`` = unlimited)
    group: FloorGroup | None = None
    token: Token | None = None
    free: FreeState | None = None


def _why_no_floor(world: WorldCore, group: FloorGroup, actor: str) -> str:
    tokens = group.tokens
    if any(t.holder == actor and t.status == "pending" for t in tokens):
        text = "还没轮到你发言"
    elif any(t.holder == actor and t.status == "done" for t in tokens):
        text = "你在本轮的发言权已经结束"
    else:
        text = "你没有本轮发言权"
    holders = _dedupe(t.holder for t in _active_tokens(group))
    if holders:
        text += "，当前可以发言的是：" + "、".join(_label(world, h) for h in holders)
    return text + "。"


def speech_gate(world: WorldCore, channel: ChannelState, actor: str) -> _Gate | Receipt:
    """Decide whether ``actor`` may write into ``channel`` now (contracts §2.3 ``speak`` steps ②–③).

    Pure: never changes any state.  (The state board re-implements the same rules on its own;
    ``tests/v12/world`` locks the two together.)
    """
    if actor not in channel.members:
        return Receipt.reject(Code.NOT_MEMBER, f"你不是频道「{channel.name}」（{channel.id}）的成员，不能在其中发言。")
    if actor == MODERATOR_ID:
        return _Gate("moderator", None)
    mode = channel.mode
    if mode in ("closed", "listen"):
        if channel.system:
            return Receipt.reject(Code.CHANNEL_CLOSED, "你与主持人的私聊已被关闭，暂时无法在其中发言。")
        if mode == "listen":
            return Receipt.reject(Code.CHANNEL_CLOSED, f"频道「{channel.name}」是只听频道，成员不能在其中发言。")
        return Receipt.reject(Code.CHANNEL_CLOSED, f"频道「{channel.name}」当前已关闭，只有主持人可以发言。")
    if channel.frozen:
        return Receipt.reject(Code.NO_FLOOR, "频道已被冻结，等待主持人裁决。")
    if mode in FLOOR_MODES:
        group = channel.group
        if group is None or group.completed:
            return Receipt.reject(Code.NO_FLOOR, "该频道当前没有进行中的发言权。", holders=[], current=None)
        holders = _dedupe(t.holder for t in _active_tokens(group))
        current = holders[0] if group.mode == "queue" and holders else None
        mine = next((t for t in _active_tokens(group) if t.holder == actor), None)
        if mine is None:
            return Receipt.reject(Code.NO_FLOOR, _why_no_floor(world, group, actor), holders=holders, current=current)
        if group.paused:
            return Receipt.reject(Code.NO_FLOOR, "发言权已暂停，请等待主持人恢复。", holders=holders, current=current)
        cap = min(mine.max_chars_per_message, mine.max_chars_total - mine.chars_used)
        return _Gate("token", cap, group=group, token=mine)
    if mode == "free":
        if actor in channel.readonly:
            return Receipt.reject(Code.NO_FLOOR, "你在该频道是只读成员，只能阅读，不能发言。")
        free = channel.free
        if free is None:
            if channel.system:
                return _Gate("dm", world.spec.defaults.dm.max_chars_per_message)
            return _Gate("free", world.spec.defaults.free.max_chars_per_message)
        if free.writers is not None and actor not in free.writers:
            return Receipt.reject(Code.NO_FLOOR, "本次自由讨论只有指定的成员可以发言。", writers=list(free.writers))
        used = free.counts.get(actor, 0)
        if used >= free.max_messages_per_member:
            return Receipt.reject(
                Code.QUOTA_EXCEEDED,
                f"你在本次自由讨论中的发言条数已用完（上限 {free.max_messages_per_member} 条）。",
                used=used,
                max_messages=free.max_messages_per_member,
            )
        return _Gate("free", free.max_chars_per_message, free=free)
    return Receipt.reject(Code.CHANNEL_CLOSED, f"频道「{channel.name}」当前不可发言。")


# --------------------------------------------------------------------------- the module


class ChatModule(WorldModule):
    name = "chat"

    def handlers(self) -> dict[str, Handler]:
        return {
            "speak": self.on_speak,
            "speak_template": self.on_speak_template,
            "pass": self.on_pass,
            "mod.announce": self.on_announce,
            "mod.send_message": self.on_send_message,
            "mod.create_channel": self.on_create_channel,
            "mod.update_channel": self.on_update_channel,
            "mod.open_floor": self.on_open_floor,
            "mod.set_floor_queue": self.on_set_floor_queue,
            "mod.pause_queue": self.on_pause_queue,
            "mod.resume_queue": self.on_resume_queue,
            "mod.close_floor": self.on_close_floor,
        }

    # ------------------------------------------------------------------ plumbing --
    def _now(self) -> float:
        return _now(self.w)

    def _unknown_channel(self, channel_id: str, actor: str) -> Receipt:
        ids = [c.id for c in self.w.state.channels if actor in c.members]
        return Receipt.reject(
            Code.UNKNOWN_CHANNEL,
            f"频道 {channel_id!r} 不存在。你可以使用的频道：{', '.join(ids) or '（无）'}。",
            channels=ids,
        )

    def _emit_channel(self, channel: ChannelState, *, created: bool = False) -> None:
        self.w.emit("channel.created" if created else "channel.updated", channel=channel_view(self.w, channel))

    def _emit_floor(self, channel: ChannelState) -> None:
        self.w.emit("floor.changed", channel_id=channel.id, floor=floor_view(self.w, channel))

    def _notice(self, targets: list[str], event: str, channel: ChannelState, **payload: Any) -> None:
        """Tell the members of a channel about a change of the channel (the operator, the moderator, is not
        told; a player who is out is spared the broadcast)."""
        for target in targets:
            if target != MODERATOR_ID:
                self.w.deliver(
                    target, "notice", event, priority="normal", channel_id=channel.id, sender=MODERATOR_ID, **payload
                )

    # ------------------------------------------------------------------ speech ---
    def on_speak(self, intent: Intent, p: Speak) -> Receipt:
        actor = intent.actor_id
        channel = self.w.channel(p.channel_id)
        if channel is None:
            return self._unknown_channel(p.channel_id, actor)
        gate = speech_gate(self.w, channel, actor)
        if isinstance(gate, Receipt):
            return gate
        text = p.content.strip()
        if not text:
            return Receipt.reject(Code.INVALID_PARAMS, "发言内容不能为空。")
        if gate.max_chars is not None and gate.max_chars <= 0:
            return Receipt.reject(Code.NO_FLOOR, "你本次发言权的字数已经用尽。")
        if gate.max_chars is not None and len(text) > gate.max_chars:
            # v0.14 (H16): a player's speech is never cut - too long, it is sent back whole and the writer rewrites it.
            # Nothing is written, nothing is booked, the floor stays exactly as it was (the clock keeps running).
            return Receipt.reject(
                Code.TOO_LONG,
                f"这条发言 {len(text)} 个字，超过了单条上限 {gate.max_chars} 字，没有发出，发言权还在。请删减后重发。",
                chars=len(text),
                limit=gate.max_chars,
            )
        entry = self.w.post_message(channel.id, actor, text, kind="speech")
        return self._receipt_for_message(channel, gate, actor, entry, len(text), None)

    def on_speak_template(self, intent: Intent, p: SpeakTemplate) -> Receipt:
        actor = intent.actor_id
        channel = self.w.channel(p.channel_id)
        if channel is None:
            return self._unknown_channel(p.channel_id, actor)
        gate = speech_gate(self.w, channel, actor)
        if isinstance(gate, Receipt):
            return gate
        rendered = self._render_template(p.template_id, p.params, actor)
        if isinstance(rendered, Receipt):
            return rendered
        text, clean = rendered
        entry = self.w.post_message(
            channel.id, actor, text, kind="template", template_id=p.template_id, params=clean
        )
        return self._receipt_for_message(channel, gate, actor, entry, len(text), None)

    def on_announce(self, intent: Intent, p: ModAnnounce) -> Receipt:
        channel = self.w.channel(p.channel_id)
        if channel is None:
            return self._unknown_channel(p.channel_id, MODERATOR_ID)
        gate = speech_gate(self.w, channel, MODERATOR_ID)
        if isinstance(gate, Receipt):
            return gate
        rendered = self._render_template(p.template_id, p.params, MODERATOR_ID)
        if isinstance(rendered, Receipt):
            return rendered
        text, clean = rendered
        entry = self.w.post_message(
            channel.id, MODERATOR_ID, text, kind="announce", template_id=p.template_id, params=clean
        )
        return self._receipt_for_message(channel, gate, MODERATOR_ID, entry, len(text), None)

    def on_send_message(self, intent: Intent, p: ModSendMessage) -> Receipt:
        channel = self.w.channel(p.channel_id)
        if channel is None:
            return self._unknown_channel(p.channel_id, MODERATOR_ID)
        gate = speech_gate(self.w, channel, MODERATOR_ID)
        if isinstance(gate, Receipt):
            return gate
        text = p.content.strip()
        if not text:
            return Receipt.reject(Code.INVALID_PARAMS, "消息内容不能为空。")
        kept, truncated = _clip(text, self.w.spec.defaults.moderator.max_chars_per_message)
        entry = self.w.post_message(channel.id, MODERATOR_ID, kept, kind="speech", truncated=truncated)
        return self._receipt_for_message(channel, gate, MODERATOR_ID, entry, len(kept), truncated)

    def _render_template(
        self, template_id: str, params: dict[str, Any], actor: str
    ) -> tuple[str, dict[str, Any]] | Receipt:
        templates = self.w.spec.templates
        template = templates.get(template_id)
        if template is None:
            usable = [
                tid
                for tid, t in templates.items()
                if t.users == "any" or (t.users == "moderator") == (actor == MODERATOR_ID)
            ]
            return Receipt.reject(
                Code.UNKNOWN_TEMPLATE,
                f"模板 {template_id!r} 不存在。你可以使用的模板：{', '.join(usable) or '（无）'}。",
                templates=usable,
            )
        if actor == MODERATOR_ID and template.users == "players":
            return Receipt.reject(Code.FORBIDDEN, f"模板 {template_id} 只供玩家使用，主持人请改用 send_message 或其他模板。")
        if actor != MODERATOR_ID and template.users == "moderator":
            return Receipt.reject(Code.FORBIDDEN, f"模板 {template_id} 只有主持人可以使用。")
        clean, rejected = validate_params(template.params, dict(params), self.w)
        if rejected is not None:
            return rejected
        assert clean is not None
        return render_text(template.text, clean, template.params, self.w), clean

    def _receipt_for_message(
        self,
        channel: ChannelState,
        gate: _Gate,
        actor: str,
        entry: LedgerEntry,
        chars: int,
        truncated: dict[str, int] | None,
    ) -> Receipt:
        floor_left = self._account(channel, gate, actor, chars)
        message = "已发送。"
        if truncated:
            message = f"已发送，但超出字数上限：末尾 {truncated['dropped_chars']} 个字被截掉（保留了 {truncated['kept_chars']} 个字）。"
        if gate.kind == "token" and gate.token is not None and gate.token.status == "done":
            message += "你的发言权已用尽。"
        return Receipt.accept(entry.seq, message, truncated=truncated, floor_left=floor_left)

    def _account(self, channel: ChannelState, gate: _Gate, actor: str, chars: int) -> dict[str, float | int | None] | None:
        """Book one committed message against the token / free discussion; returns ``floor_left``."""
        if gate.kind == "token":
            group, token = gate.group, gate.token
            assert group is not None and token is not None
            token.messages_used += 1
            token.chars_used += chars
            left: dict[str, float | int | None] = {
                "messages": max(0, token.max_messages - token.messages_used),
                "chars": max(0, token.max_chars_total - token.chars_used),
                "seconds": token_seconds_left(self.w, token),
            }
            if token.messages_used >= token.max_messages or token.chars_used >= token.max_chars_total:
                self._end_token(channel, group, token, "used")
            else:
                self._emit_floor(channel)
            return left
        free = channel.free
        if channel.mode == "free" and free is not None:
            now = self._now()
            free.last_message_gt = now
            free.notified.pop("silence", None)
            if actor != MODERATOR_ID:
                free.counts[actor] = free.counts.get(actor, 0) + 1
                free.chars_used += chars
            self._emit_floor(channel)
            if gate.kind == "free":
                return {
                    "messages": max(0, free.max_messages_per_member - free.counts.get(actor, 0)),
                    "chars": max(0, free.max_chars_total - free.chars_used),
                    "seconds": _r1(max(0.0, free.duration_s - (now - free.started_gt))),
                }
        return None

    def on_pass(self, intent: Intent, p: Pass) -> Receipt:
        actor = intent.actor_id
        channel = self.w.channel(p.channel_id)
        if channel is None:
            return self._unknown_channel(p.channel_id, actor)
        if actor not in channel.members:
            return Receipt.reject(Code.NOT_MEMBER, f"你不是频道「{channel.name}」（{channel.id}）的成员。")
        if channel.mode == "free":
            return Receipt.accept(message="自由讨论中没有发言权令牌，无需放弃；不想说话什么都不做即可。")
        group = channel.group if channel.mode in FLOOR_MODES else None
        if group is not None and not channel.frozen and not group.paused:
            token = next((t for t in _active_tokens(group) if t.holder == actor), None)
            if token is not None:
                entry = self._end_token(channel, group, token, "pass")
                return Receipt.accept(entry.seq, "已放弃发言权。")
        return Receipt.reject(Code.NO_FLOOR, "你当前没有可以放弃的发言权。")

    # --------------------------------------------------------- channel management --
    def _new_free(self, limits: FreeLimits | None, writers: list[str] | None) -> FreeState:
        d = self.w.spec.defaults.free
        override = limits.model_dump(exclude_none=True) if limits is not None else {}
        now = self._now()
        values = {f: override.get(f, getattr(d, f)) for f in _FREE_LIMIT_FIELDS}
        return FreeState(writers=writers, started_gt=now, last_message_gt=now, **values)

    def _check_players(self, ids: list[str]) -> Receipt | None:
        unknown = [i for i in ids if not self.w.is_player(i)]
        if unknown:
            valid = [p.id for p in self.w.state.players]
            return Receipt.reject(
                Code.UNKNOWN_PLAYER,
                f"未知的玩家：{', '.join(unknown)}。有效的玩家 id：{', '.join(valid)}。",
                unknown=unknown,
                players=valid,
            )
        return None

    def on_create_channel(self, intent: Intent, p: ModCreateChannel) -> Receipt:
        if self.w.channel(p.channel_id) is not None:
            return Receipt.reject(Code.ALREADY_EXISTS, f"频道 {p.channel_id} 已存在。")
        members = [m for m in _dedupe(p.members) if m != MODERATOR_ID]
        bad = self._check_players(members)
        if bad is not None:
            return bad
        channel = ChannelState(
            id=p.channel_id,
            name=p.name.strip() or p.channel_id,
            type=p.type,
            mode=p.mode,
            members=[MODERATOR_ID, *members],
        )
        if channel.mode == "free":
            channel.free = self._new_free(None, None)
        self.w.state.channels.append(channel)
        entry = self.w.commit("channel.created", MODERATOR_ID, channel_view(self.w, channel), channel_id=channel.id)
        self._notice(members, "channel.created", channel, name=channel.name, mode=channel.mode, type=channel.type)
        self._emit_channel(channel, created=True)
        return Receipt.accept(
            entry.seq, f"频道「{channel.name}」已创建。", channel_id=channel.id, mode=channel.mode, members=list(channel.members)
        )

    def create_system_dms(self) -> list[ChannelState]:
        """Create the per-player system DM ``dm-<id>`` (members: the moderator and the player), D23.

        Idempotent.  Writes ``channel.created`` entries and UI events but delivers nothing.
        """
        created: list[ChannelState] = []
        for player in self.w.state.players:
            cid = system_dm_id(player.id)
            if self.w.channel(cid) is not None:
                continue
            channel = ChannelState(
                id=cid,
                name=f"私聊 · {player.name}",
                type="dm",
                mode="free",
                members=[MODERATOR_ID, player.id],
                system=True,
                owner=player.id,
            )
            self.w.state.channels.append(channel)
            self.w.commit("channel.created", ENGINE_ID, channel_view(self.w, channel), channel_id=cid)
            self._emit_channel(channel, created=True)
            created.append(channel)
        return created

    def on_update_channel(self, intent: Intent, p: ModUpdateChannel) -> Receipt:
        channel = self.w.channel(p.channel_id)
        if channel is None:
            return self._unknown_channel(p.channel_id, MODERATOR_ID)
        add = [m for m in _dedupe(p.add_members) if m != MODERATOR_ID]
        remove = _dedupe(p.remove_members)
        # -- validation: nothing below may change state --------------------------------------
        if MODERATOR_ID in remove:
            return Receipt.reject(Code.FORBIDDEN, "主持人不能被移出频道。")
        if channel.system and (add or remove or p.readonly_members is not None):
            return Receipt.reject(Code.FORBIDDEN, "系统私聊的成员不能更改（可以用 mode 关闭或重新打开它）。")
        free_limits: FreeLimits | None = None
        if p.limits is not None:
            if channel.system:
                return Receipt.reject(Code.INVALID_PARAMS, "系统私聊不使用自由讨论限额，不能设置 limits。")
            parsed, rejected = self._limits_for("free", p.limits, tool="update_channel")
            if rejected is not None:
                return rejected
            free_limits = parsed  # type: ignore[assignment]
        bad = self._check_players([*add, *remove])
        if bad is not None:
            return bad
        both = sorted(set(add) & set(remove))
        if both:
            return Receipt.reject(Code.INVALID_PARAMS, f"同一个玩家不能同时被加入和移出：{', '.join(both)}。")
        absent = [m for m in remove if m not in channel.members]
        if absent:
            return Receipt.reject(Code.NOT_MEMBER, f"{', '.join(absent)} 不是该频道的成员，无法移出。", not_members=absent)
        members_after = [m for m in channel.members if m not in remove] + [m for m in add if m not in channel.members]
        if p.readonly_members is not None:
            readonly_after = _dedupe(p.readonly_members)
            if MODERATOR_ID in readonly_after:
                return Receipt.reject(Code.FORBIDDEN, "主持人不能被设为只读成员。")
            bad = self._check_players(readonly_after)
            if bad is not None:
                return bad
            outside = [m for m in readonly_after if m not in members_after]
            if outside:
                return Receipt.reject(Code.NOT_MEMBER, f"只读成员必须是频道成员：{', '.join(outside)} 不在频道中。", not_members=outside)
        else:
            readonly_after = [m for m in channel.readonly if m in members_after]
        final_mode = p.mode or channel.mode
        if free_limits is not None:
            if final_mode != "free":
                return Receipt.reject(
                    Code.INVALID_PARAMS,
                    "只有处于自由讨论（mode=free）的频道才能设置 limits；这个频道现在不是自由讨论。"
                    "要开始自由讨论请用 open_floor(mode=free, limits=…)，或同时传 mode=free。",
                    channel_mode=channel.mode,
                )
            checked = self._check_free_limits(channel, free_limits) if channel.mode == "free" else None
            if checked is not None:
                return checked

        # -- apply -----------------------------------------------------------------------------
        old_members = list(channel.members)
        old_mode = channel.mode
        writers_before = set(_free_writers(channel)) if old_mode == "free" and channel.free is not None else set()
        changes: dict[str, Any] = {}
        floor_touched = False

        # 1. the mode: leaving a floor revokes it while the old membership is still in place
        if p.mode is not None:
            was_frozen = channel.frozen
            if old_mode in FLOOR_MODES:
                self._revoke_group(channel, "mode_changed")
                floor_touched = True
            elif old_mode == "free" and p.mode != "free":
                self._end_free(channel, "mode_changed")
                floor_touched = True
            channel.frozen = False
            if p.mode != old_mode:
                channel.mode = p.mode
                changes["mode"] = {"from": old_mode, "to": p.mode}
                if p.mode == "free" and not channel.system:
                    channel.free = self._new_free(free_limits, None)
                    changes["limits"] = {f: getattr(channel.free, f) for f in _FREE_LIMIT_FIELDS}
                    floor_touched = True
            if was_frozen:
                changes["unfrozen"] = True
                floor_touched = True

        # 2. name, members, read-only members
        name = p.name.strip() if p.name is not None else ""
        if name and name != channel.name:
            channel.name = name
            changes["name"] = name
        added = [m for m in add if m not in old_members]
        removed = list(remove)
        if added or removed:
            channel.members = members_after
            if added:
                changes["added"] = added
            if removed:
                changes["removed"] = removed
        if sorted(readonly_after) != sorted(channel.readonly):
            changes["readonly"] = readonly_after
        channel.readonly = readonly_after

        # 3. new limits for a free discussion that keeps running (this re-arms its advisories)
        if free_limits is not None and channel.mode == "free" and channel.free is not None and "limits" not in changes:
            changes["limits"] = self._apply_free_limits(channel.free, free_limits)
            floor_touched = True

        # 4. removed members lose their tokens (the queue moves on) / their right to write
        if removed:
            group = channel.group
            if group is not None and channel.mode in FLOOR_MODES:
                for token in list(group.tokens):
                    if channel.group is not group:
                        break
                    if token.holder in removed and token.status != "done":
                        self._end_token(channel, group, token, "revoked", cause="removed")
                        floor_touched = True
            free = channel.free
            if free is not None and channel.mode == "free":
                if free.writers is not None:
                    free.writers = [w for w in free.writers if w not in removed]
                for member in removed:
                    if member in writers_before:
                        self.w.deliver(
                            member, "floor", "floor.revoked", priority="normal", interrupt=True,
                            channel_id=channel.id, sender=MODERATOR_ID, reason="removed",
                        )
                floor_touched = True

        if not changes:
            return Receipt.accept(message="频道没有需要更新的内容。", channel_id=channel.id)
        entry = self.w.commit(
            "channel.updated",
            MODERATOR_ID,
            {"changes": changes},
            channel_id=channel.id,
            visible_to=_dedupe([*old_members, *channel.members]),
        )
        self._notice(
            _dedupe([*channel.members, *removed]),
            "channel.updated",
            channel,
            name=channel.name,
            mode=channel.mode,
            added=added,
            removed=removed,
        )
        self._emit_channel(channel)
        if floor_touched:
            self._emit_floor(channel)
        return Receipt.accept(
            entry.seq,
            "频道已更新。",
            channel_id=channel.id,
            changes=changes,
            mode=channel.mode,
            members=list(channel.members),
            readonly=list(channel.readonly),
        )

    #: A correct ``limits`` per mode, quoted in the rejection of a key that does not apply.
    _LIMITS_EXAMPLE: dict[str, dict[str, Any]] = {
        "turn": {"max_messages": 2, "max_chars_per_message": 300, "duration_s": 90},
        "free": {"duration_s": 180, "max_messages_per_member": 3, "silence_s": 30},
    }
    #: What to say about a key of the *other* mode (the two modes name the per-person quota differently).
    _OTHER_MODE_HINT: dict[str, str] = {
        "max_messages": "max_messages（每个令牌的条数）属于 mode=turn，自由讨论里对应的键是 max_messages_per_member",
        "max_messages_per_member": "max_messages_per_member（每人的条数）属于 mode=free，mode=turn 里对应的键是 max_messages",
        "silence_s": "silence_s（冷场提醒）只用于 mode=free",
    }

    @staticmethod
    def _value_problem(err: dict[str, Any]) -> str:
        """One pydantic error of a ``limits`` value as a short Chinese sentence."""
        name = ".".join(str(part) for part in err["loc"])
        ctx = err.get("ctx") or {}
        kind = err.get("type", "")
        if kind == "less_than_equal":
            return f"{name} 不能大于 {ctx.get('le')}"
        if kind == "less_than":
            return f"{name} 必须小于 {ctx.get('lt')}"
        if kind == "greater_than_equal":
            return f"{name} 不能小于 {ctx.get('ge')}"
        if kind == "greater_than":
            return f"{name} 必须大于 {ctx.get('gt')}"
        if kind in ("int_parsing", "int_from_float", "float_parsing"):
            return f"{name} 必须是数字（整数或小数，视键而定）"
        return f"{name}：{err.get('msg', '无效')}"

    def _limits_for(
        self, mode: str, limits: OpenLimits | None, *, tool: str
    ) -> tuple[FloorLimits | FreeLimits | None, Receipt | None]:
        """Contracts §10.1.5: check the keys of the one ``limits`` object against ``mode`` (``turn`` takes
        ``TOKEN_LIMIT_KEYS``, ``free`` takes ``FREE_LIMIT_KEYS``), then its values with ``FloorLimits`` /
        ``FreeLimits``.  Returns ``(typed limits | None, rejection | None)``; an empty object is ``None``.

        A rejection names the mode, lists every key the mode accepts and quotes a correct example, so that
        a model can fix the call at once."""
        given = limits.model_dump(exclude_none=True) if limits is not None else {}
        if not given:
            return None, None
        allowed = TOKEN_LIMIT_KEYS if mode == "turn" else FREE_LIMIT_KEYS
        example = self._LIMITS_EXAMPLE[mode]
        tail = f"mode={mode} 只接受：{', '.join(allowed)}。"
        sample = f"正确示例：limits={json.dumps(example, ensure_ascii=False)}"
        wrong = [key for key in given if key not in allowed]
        if wrong:
            hints = "；".join(self._OTHER_MODE_HINT[key] for key in wrong if key in self._OTHER_MODE_HINT)
            if tool == "update_channel":
                head = f"{tool}：limits 只用于自由讨论（mode=free），其中的 {', '.join(wrong)} 不适用。"
            else:
                head = f"{tool}：limits 里的 {', '.join(wrong)} 不适用于 mode={mode}。"
            return None, Receipt.reject(
                Code.INVALID_PARAMS,
                head + tail + (f"{hints}。" if hints else "") + sample,
                mode=mode,
                allowed=list(allowed),
                rejected=wrong,
                example=dict(example),
            )
        model = FloorLimits if mode == "turn" else FreeLimits
        try:
            return model(**given), None
        except ValidationError as exc:
            problems = "；".join(
                self._value_problem(err) for err in exc.errors(include_input=False, include_url=False)[:4]
            )
            return None, Receipt.reject(
                Code.INVALID_PARAMS,
                f"{tool}：limits 的值不合要求（mode={mode}）：{problems}。{tail}{sample}",
                mode=mode,
                allowed=list(allowed),
                example=dict(example),
            )

    def _check_free_limits(self, channel: ChannelState, limits: FreeLimits) -> Receipt | None:
        """``limits`` of a free discussion are absolute caps over the whole discussion; refuse values already exceeded."""
        free = channel.free
        if free is None:
            return None
        now = self._now()
        elapsed = max(0.0, now - free.started_gt)
        if limits.duration_s is not None and limits.duration_s <= elapsed:
            return Receipt.reject(
                Code.INVALID_PARAMS,
                f"duration_s 是整场自由讨论的总时长（从开始计），必须大于已进行的 {elapsed:.0f} 秒。",
                elapsed_s=_r1(elapsed),
            )
        if limits.max_chars_total is not None and limits.max_chars_total <= free.chars_used:
            return Receipt.reject(
                Code.INVALID_PARAMS,
                f"max_chars_total 是整场自由讨论的总字数上限，必须大于已用的 {free.chars_used} 字。",
                chars_used=free.chars_used,
            )
        return None

    def _apply_free_limits(self, free: FreeState, limits: FreeLimits) -> dict[str, Any]:
        applied = limits.model_dump(exclude_none=True)
        for field, value in applied.items():
            setattr(free, field, value)
        free.notified = {}  # re-arm every advisory ...
        free.last_message_gt = self._now()  # ... and let the silence countdown start afresh (the moderator just acted)
        return applied

    # ------------------------------------------------------------- floor opening --
    def _token_limits(self, limits: FloorLimits | None) -> dict[str, Any]:
        d = self.w.spec.defaults.floor
        o = limits.model_dump(exclude_none=True) if limits is not None else {}
        return {
            "max_messages": o.get("max_messages", d.max_messages),
            "max_chars_per_message": o.get("max_chars_per_message", d.max_chars_per_message),
            "max_chars_total": o.get("max_chars_total", d.max_chars_total),
            "duration_s": o.get("duration_s", d.duration_s),
        }

    def _check_holders(self, channel: ChannelState, holders: list[str]) -> Receipt | None:
        if MODERATOR_ID in holders:
            return Receipt.reject(Code.INVALID_TARGET, "主持人不需要发言权令牌，可以直接在频道里发言。")
        bad = self._check_players(holders)
        if bad is not None:
            return bad
        outside = [h for h in holders if h not in channel.members]
        if outside:
            return Receipt.reject(
                Code.NOT_MEMBER,
                f"{', '.join(outside)} 不是频道「{channel.name}」的成员，请先用 update_channel 把他们加入频道。",
                not_members=outside,
            )
        return None

    def on_open_floor(self, intent: Intent, p: ModOpenFloor) -> Receipt:
        channel = self.w.channel(p.channel_id)
        if channel is None:
            return self._unknown_channel(p.channel_id, MODERATOR_ID)
        if channel.system:
            return Receipt.reject(Code.INVALID_TARGET, "系统私聊不使用发言权令牌；请用 update_channel 的 mode 来关闭或重新打开它。")
        holders = _dedupe(p.holders)
        bad = self._check_holders(channel, holders)
        if bad is not None:
            return bad
        limits, rejected = self._limits_for(p.mode, p.limits, tool="open_floor")
        if rejected is not None:
            return rejected
        if p.mode == "turn":
            token_limits = limits if isinstance(limits, FloorLimits) else None
            group, opened = self._open_group(channel, "turn", holders, token_limits, MODERATOR_ID)
            return Receipt.accept(
                opened.seq, f"已向 {len(holders)} 名成员授予发言权。", channel_id=channel.id, group_id=group.id,
                holders=holders, limits=self._token_limits(token_limits),
            )
        self._end_activity(channel, "replaced", keep_free=True)
        channel.mode = "free"
        channel.frozen = False
        channel.free = self._new_free(limits if isinstance(limits, FreeLimits) else None, holders or None)
        limits = {f: getattr(channel.free, f) for f in _FREE_LIMIT_FIELDS}
        entry = self.w.commit(
            "floor.opened", MODERATOR_ID,
            {"group_id": None, "mode": "free", "holders": holders, "limits": limits},
            channel_id=channel.id,
        )
        self._emit_floor(channel)
        self._emit_channel(channel)
        return Receipt.accept(
            entry.seq, "自由讨论已开始。", channel_id=channel.id, mode="free", writers=holders or None, limits=limits
        )

    def on_set_floor_queue(self, intent: Intent, p: ModSetFloorQueue) -> Receipt:
        channel = self.w.channel(p.channel_id)
        if channel is None:
            return self._unknown_channel(p.channel_id, MODERATOR_ID)
        if channel.system:
            return Receipt.reject(Code.INVALID_TARGET, "系统私聊不使用发言权令牌；请用 update_channel 的 mode 来关闭或重新打开它。")
        order = [str(h) for h in p.order]
        bad = self._check_holders(channel, order)
        if bad is not None:
            return bad
        group, opened = self._open_group(channel, "queue", order, p.limits, MODERATOR_ID)
        return Receipt.accept(
            opened.seq, f"已建立发言队列，共 {len(order)} 个位置，{_label(self.w, order[0])} 先发言。",
            channel_id=channel.id, group_id=group.id, order=order, limits=self._token_limits(p.limits),
        )

    def _open_group(
        self, channel: ChannelState, mode: str, holders: list[str], limits: FloorLimits | None, actor: str
    ) -> tuple[FloorGroup, LedgerEntry]:
        """Replace whatever floor the channel had by a new group; returns it with its ``floor.opened`` entry."""
        now = self._now()
        lim = self._token_limits(limits)
        self._end_activity(channel, "replaced")
        tokens = [Token(holder=h, status="pending", **lim) for h in holders]
        group = FloorGroup(
            id=f"g{self.w.next_counter('group')}", channel_id=channel.id, mode=mode, tokens=tokens, created_gt=now  # type: ignore[arg-type]
        )
        channel.group = group
        channel.mode = mode  # type: ignore[assignment]
        channel.frozen = False
        channel.free = None
        opened = self.w.commit(
            "floor.opened", actor,
            {"group_id": group.id, "mode": mode, "holders" if mode == "turn" else "order": list(holders), "limits": lim},
            channel_id=channel.id,
        )
        if mode == "turn":
            for token in tokens:
                self._activate(channel, group, token, actor)
        else:
            self._activate(channel, group, tokens[0], actor)
        self._emit_floor(channel)
        self._emit_channel(channel)
        return group, opened

    def _activate(self, channel: ChannelState, group: FloorGroup, token: Token, actor: str) -> None:
        """Hand the floor to ``token`` (it starts its timer unless the group is paused, or the holder is the seat a
        person plays and the channel is a public one: then it has no deadline at all, v0.13.1 G10 / G14)."""
        now = self._now()
        untimed = token_untimed(self.w, channel, token)
        token.status = "active"
        token.started_gt = now
        if group.paused or untimed:
            token.deadline = None
            token.remaining = float(token.duration_s)  # (untimed: the nominal time a release of the seat gives back)
        else:
            token.deadline = now + float(token.duration_s)
            token.remaining = None
        data: dict[str, Any] = {"group_id": group.id, "holder": token.holder, "limits": _limits_dict(token)}
        if untimed:
            data["untimed"] = True  # (an old entry has no such key: false) the replay shows no countdown for it
        position = next((i + 1 for i, t in enumerate(group.tokens) if t is token), None)
        if group.mode == "queue":
            data["position"] = position
            data["total"] = len(group.tokens)
        entry = self.w.commit("floor.granted", actor, data, channel_id=channel.id)
        if not group.paused:
            self._deliver_granted(channel, group, token, entry.seq, actor)

    def _deliver_granted(self, channel: ChannelState, group: FloorGroup, token: Token, seq: int, sender: str) -> None:
        payload: dict[str, Any] = {
            "mode": group.mode,
            "max_messages": token.max_messages,
            "max_chars_per_message": token.max_chars_per_message,
            "max_chars_total": token.max_chars_total,
            "seconds": token_seconds_left(self.w, token),
        }
        if group.mode == "queue":
            payload["position"] = next((i + 1 for i, t in enumerate(group.tokens) if t is token), None)
            payload["total"] = len(group.tokens)
        self.w.deliver(
            token.holder, "floor", "floor.granted", priority="urgent", direct=True, channel_id=channel.id, sender=sender, seq=seq,
            **payload,
        )

    # --------------------------------------------------------- ending floors ------
    def _revoke_group(self, channel: ChannelState, cause: str) -> None:
        """Drop the running group: every active token ends as ``revoked`` and its holder is told."""
        group = channel.group
        if group is None:
            return
        for token in group.tokens:
            if token.status == "done":
                continue
            was_active = token.status == "active"
            token.status = "done"
            token.ended_reason = "revoked"
            token.deadline = None
            token.remaining = None
            if not was_active:
                continue
            entry = self.w.commit(
                "floor.token_ended", ENGINE_ID,
                {
                    "group_id": group.id, "holder": token.holder, "reason": "revoked", "cause": cause,
                    "messages_used": token.messages_used, "chars_used": token.chars_used,
                },
                channel_id=channel.id,
            )
            self.w.deliver(
                token.holder, "floor", "floor.revoked", priority="normal", interrupt=True, direct=True,
                channel_id=channel.id, sender=ENGINE_ID, seq=entry.seq, reason=cause,
            )
        group.completed = True
        channel.group = None

    def _end_free(self, channel: ChannelState, cause: str) -> None:
        """Finish a free discussion (state dropped); the members who could write are told (a member who is
        out is spared: he has no token, nothing of his is in flight)."""
        writers = _free_writers(channel) if channel.free is not None else []
        channel.free = None
        for member in writers:
            self.w.deliver(
                member, "floor", "floor.revoked", priority="normal", interrupt=True,
                channel_id=channel.id, sender=ENGINE_ID, reason=cause,
            )

    def _end_activity(self, channel: ChannelState, cause: str, *, keep_free: bool = False) -> None:
        """Revoke the running group / end the free discussion before the channel is given another mode."""
        if channel.group is not None:
            self._revoke_group(channel, cause)
        if channel.free is not None and not keep_free:
            self._end_free(channel, cause)

    def _end_token(
        self, channel: ChannelState, group: FloorGroup, token: Token, reason: str, *, cause: str | None = None
    ) -> LedgerEntry:
        """Contracts §2.3 ``_end_token``: finish one token, advance a queue, complete the group."""
        was_active = token.status == "active"
        token.status = "done"
        token.ended_reason = reason
        token.deadline = None
        token.remaining = None
        data: dict[str, Any] = {
            "group_id": group.id, "holder": token.holder, "reason": reason,
            "messages_used": token.messages_used, "chars_used": token.chars_used,
        }
        if cause:
            data["cause"] = cause
        entry = self.w.commit("floor.token_ended", ENGINE_ID, data, channel_id=channel.id)
        if was_active and reason in ("timeout", "revoked"):
            self.w.deliver(
                token.holder, "floor", "floor.revoked", priority="normal", interrupt=True, direct=True,
                channel_id=channel.id, sender=ENGINE_ID, seq=entry.seq, reason=cause or reason,
            )
        if group.mode == "queue" and not any(t.status == "active" for t in group.tokens):
            following = next((t for t in group.tokens if t.status == "pending"), None)
            if following is not None:
                self._activate(channel, group, following, ENGINE_ID)
        completed = all(t.status == "done" for t in group.tokens)
        if completed:
            self._complete_group(channel, group)
        self._emit_floor(channel)
        if completed:
            self._emit_channel(channel)
        return entry

    def _complete_group(self, channel: ChannelState, group: FloorGroup) -> None:
        results = [
            {"holder": t.holder, "reason": t.ended_reason, "messages_used": t.messages_used, "chars_used": t.chars_used}
            for t in group.tokens
        ]
        group.completed = True
        channel.group = None
        channel.mode = "closed"
        entry = self.w.commit(
            "floor.completed", ENGINE_ID,
            {"group_id": group.id, "channel_id": channel.id, "mode": group.mode, "reason": "completed", "results": results},
            channel_id=channel.id,
        )
        self.w.deliver(
            MODERATOR_ID, "floor", "floor.completed", priority="urgent", channel_id=channel.id, sender=ENGINE_ID,
            seq=entry.seq, group_id=group.id, mode=group.mode, results=results,
        )

    # ------------------------------------------------- pause / resume / close ------
    def _suspend_tokens(self, group: FloorGroup) -> list[str]:
        """Freeze the clocks of the active tokens; returns their holders.  (An untimed token has no deadline and
        already holds its nominal remaining time: it is kept as it is.)"""
        now = self._now()
        holders: list[str] = []
        for token in _active_tokens(group):
            if token.deadline is not None:
                token.remaining = max(0.0, token.deadline - now)
                token.deadline = None
            elif token.remaining is None:
                token.remaining = float(token.duration_s)
            holders.append(token.holder)
        group.paused = True
        return holders

    def on_pause_queue(self, intent: Intent, p: ModChannelRef) -> Receipt:
        channel = self.w.channel(p.channel_id)
        if channel is None:
            return self._unknown_channel(p.channel_id, MODERATOR_ID)
        group = channel.group if channel.mode in FLOOR_MODES else None
        if group is None:
            return Receipt.reject(Code.INVALID_TARGET, f"频道「{channel.name}」当前没有进行中的发言队列，无法暂停。")
        if group.paused:
            return Receipt.accept(message="发言队列已经处于暂停状态。", channel_id=channel.id)
        holders = self._suspend_tokens(group)
        entry = self.w.commit(
            "floor.paused", MODERATOR_ID, {"group_id": group.id, "channel_id": channel.id, "reason": "paused"},
            channel_id=channel.id,
        )
        for holder in holders:
            self.w.deliver(
                holder, "floor", "floor.paused", priority="normal", interrupt=True, direct=True,
                channel_id=channel.id, sender=MODERATOR_ID, seq=entry.seq, reason="paused",
            )
        self._emit_floor(channel)
        return Receipt.accept(entry.seq, "发言队列已暂停，计时已冻结。", channel_id=channel.id, group_id=group.id)

    def on_resume_queue(self, intent: Intent, p: ModChannelRef) -> Receipt:
        channel = self.w.channel(p.channel_id)
        if channel is None:
            return self._unknown_channel(p.channel_id, MODERATOR_ID)
        group = channel.group if channel.mode in FLOOR_MODES else None
        if group is None:
            if channel.frozen:  # a frozen free discussion: resuming just lifts the freeze
                channel.frozen = False
                entry = self.w.commit(
                    "floor.resumed", MODERATOR_ID, {"group_id": None, "channel_id": channel.id, "reason": "resumed"},
                    channel_id=channel.id,
                )
                self._emit_channel(channel)
                self._emit_floor(channel)
                return Receipt.accept(entry.seq, "频道已解除冻结。", channel_id=channel.id)
            return Receipt.reject(Code.INVALID_TARGET, f"频道「{channel.name}」当前没有进行中的发言队列，无法恢复。")
        if not group.paused and not channel.frozen:
            return Receipt.accept(message="发言队列没有处于暂停状态，无需恢复。", channel_id=channel.id)
        was_frozen = channel.frozen
        channel.frozen = False
        group.paused = False
        now = self._now()
        resumed: list[Token] = []
        for token in _active_tokens(group):
            if token_untimed(self.w, channel, token):
                resumed.append(token)  # no deadline to restart: it keeps its nominal remaining time (G14)
                continue
            remaining = token.remaining if token.remaining is not None else float(token.duration_s)
            token.deadline = now + remaining
            token.remaining = None
            resumed.append(token)
        entry = self.w.commit(
            "floor.resumed", MODERATOR_ID, {"group_id": group.id, "channel_id": channel.id, "reason": "resumed"},
            channel_id=channel.id,
        )
        for token in resumed:
            self._deliver_granted(channel, group, token, entry.seq, MODERATOR_ID)
        self._emit_floor(channel)
        if was_frozen:
            self._emit_channel(channel)
        return Receipt.accept(entry.seq, "发言队列已恢复，计时继续。", channel_id=channel.id, group_id=group.id)

    def on_close_floor(self, intent: Intent, p: ModChannelRef) -> Receipt:
        channel = self.w.channel(p.channel_id)
        if channel is None:
            return self._unknown_channel(p.channel_id, MODERATOR_ID)
        if channel.mode not in (*FLOOR_MODES, "free") and not channel.frozen:
            return Receipt.accept(message="该频道当前没有进行中的发言权，无需关闭。", channel_id=channel.id, mode=channel.mode)
        group_id = channel.group.id if channel.group is not None else None
        self._end_activity(channel, "closed")
        channel.frozen = False
        channel.mode = "closed"
        entry = self.w.commit(
            "floor.closed", MODERATOR_ID, {"group_id": group_id, "channel_id": channel.id, "reason": "closed"},
            channel_id=channel.id,
        )
        self._emit_floor(channel)
        self._emit_channel(channel)
        return Receipt.accept(entry.seq, "已收回发言权，频道现在是关闭状态。", channel_id=channel.id, mode="closed")

    # ------------------------------------------------------------------- freezing --
    def freeze_channel(self, channel_id: str, reason: str = "") -> bool:
        """Contracts §2.4 (D25): freeze the floor of ``channel_id``.  Returns ``True`` if anything changed."""
        channel = self.w.channel(channel_id)
        if channel is None or channel.frozen:
            return False
        group = channel.group if channel.mode in FLOOR_MODES else None
        if group is not None and not group.paused:
            writers = self._suspend_tokens(group)
        elif group is None and channel.mode == "free":
            writers = _free_writers(channel)
        else:
            writers = []
        channel.frozen = True
        entry = self.w.commit(
            "floor.frozen", ENGINE_ID,
            {"group_id": group.id if group is not None else None, "channel_id": channel.id, "reason": reason},
            channel_id=channel.id,
        )
        for member in writers:
            self.w.deliver(  # token holders are addressed personally; free-discussion writers are not (E23)
                member, "floor", "floor.frozen", priority="normal", interrupt=True, direct=group is not None,
                channel_id=channel.id, sender=ENGINE_ID, seq=entry.seq, reason=reason,
            )
        self._emit_channel(channel)
        self._emit_floor(channel)
        return True

    # ------------------------------------------------------------ timers & restore --
    def tick(self) -> None:
        now = self._now()
        for channel in list(self.w.state.channels):
            group = channel.group
            if group is not None and channel.mode in FLOOR_MODES and not group.paused and not group.completed:
                for token in list(group.tokens):
                    if channel.group is not group:
                        break
                    if token.status == "active" and token.deadline is not None and now >= token.deadline:
                        self._end_token(channel, group, token, "timeout")
            self._tick_free(channel, now)

    def _tick_free(self, channel: ChannelState, now: float) -> None:
        free = channel.free
        if free is None or channel.mode != "free" or channel.frozen:
            return
        if not free.notified.get("time") and now - free.started_gt >= free.duration_s:
            self._advise(channel, "time")
        if not free.notified.get("chars") and free.chars_used >= free.max_chars_total:
            self._advise(channel, "chars")
        if not free.notified.get("silence") and now - max(free.started_gt, free.last_message_gt) >= free.silence_s:
            self._advise(channel, "silence")

    def _advise(self, channel: ChannelState, reason: str) -> None:
        """D20: a free discussion never closes by itself; the moderator is told once per reason."""
        free = channel.free
        assert free is not None
        free.notified[reason] = True
        stats = free_view(self.w, channel) or {}
        self.w.deliver(
            MODERATOR_ID, "floor", "free.advisory", priority="urgent", channel_id=channel.id, sender=ENGINE_ID,
            reason=reason, **stats,
        )

    def next_deadline(self) -> float | None:
        times: list[float] = []
        now = self._now()
        for channel in self.w.state.channels:
            group = channel.group
            if group is not None and channel.mode in FLOOR_MODES and not group.paused and not group.completed:
                times.extend(t.deadline for t in group.tokens if t.status == "active" and t.deadline is not None)
            free = channel.free
            if free is not None and channel.mode == "free" and not channel.frozen:
                if not free.notified.get("time"):
                    times.append(free.started_gt + free.duration_s)
                if not free.notified.get("silence"):
                    times.append(max(free.started_gt, free.last_message_gt) + free.silence_s)
                if not free.notified.get("chars") and free.chars_used >= free.max_chars_total:
                    times.append(now)
        return min(times) if times else None

    def untimed_changed(self, before: frozenset[str]) -> None:
        """The world's ``untimed_holders`` changed (a seat was taken over or given back, v0.13.1, G14): the floors
        those holders already hold in public channels are re-judged.  A token that becomes untimed freezes the
        time it had left in ``remaining`` and drops its deadline; one that is timed again resumes with exactly that
        time left (a floor in a paused group is frozen either way - only what the views say changes).  Whoever
        draws the picture is told through ``floor.changed``; the ledger gets nothing (nobody is told, F6)."""
        now = self._now()
        for channel in self.w.state.channels:
            group = channel.group
            if group is None or channel.mode not in FLOOR_MODES or group.completed:
                continue
            touched = False
            for token in _active_tokens(group):
                untimed = token_untimed(self.w, channel, token)
                if untimed == (channel.type == "public" and token.holder in before):
                    continue  # this holder's judgement did not change
                touched = True
                if group.paused:
                    continue
                if untimed and token.deadline is not None:
                    token.remaining = max(0.0, token.deadline - now)
                    token.deadline = None
                elif not untimed and token.deadline is None:
                    remaining = token.remaining if token.remaining is not None else float(token.duration_s)
                    token.deadline = now + remaining
                    token.remaining = None
            if touched:
                self._emit_floor(channel)

    def untimed_floor_open(self) -> bool:
        """A person holds a floor that has no deadline and may speak (the group runs: not paused, not frozen) - the
        game is waiting for them, so the watchdog must not take it for a stall (v0.13.1, G13)."""
        for channel in self.w.state.channels:
            group = channel.group
            if group is None or channel.mode not in FLOOR_MODES or group.completed or group.paused or channel.frozen:
                continue
            if any(token_untimed(self.w, channel, t) for t in _active_tokens(group)):
                return True
        return False

    def redeliver(self) -> None:
        """After a resume / restore: tell the holders of running floors again (paused ones wait for the
        moderator's ``resume_queue``)."""
        for channel in self.w.state.channels:
            group = channel.group
            if group is None or channel.mode not in FLOOR_MODES or group.paused or group.completed:
                continue
            for token in _active_tokens(group):
                self._deliver_granted(channel, group, token, self._granted_seq(channel, group, token), ENGINE_ID)

    def _granted_seq(self, channel: ChannelState, group: FloorGroup, token: Token) -> int:
        """Seq of the ``floor.granted`` entry that handed ``token`` the floor (0 if it is not in the ledger)."""
        for entry in reversed(self.w.ledger):
            if (
                entry.type == "floor.granted"
                and entry.channel_id == channel.id
                and entry.data.get("group_id") == group.id
                and entry.data.get("holder") == token.holder
            ):
                return entry.seq
        return 0

    def dump(self) -> dict[str, Any]:
        return {}  # all chat state lives in ``WorldState.channels``

    def load(self, data: dict[str, Any]) -> None:
        return None


__all__ = [
    "FLOOR_MODES",
    "ChatModule",
    "channel_view",
    "floor_view",
    "free_view",
    "speech_gate",
    "system_dm_id",
    "token_seconds_left",
    "token_untimed",
]
