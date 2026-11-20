"""The per-agent *state board* (contracts §2.7): what one agent knows about the world right now.

``build_board(world, agent_id)`` reads ``world.state`` directly (it does not depend on the chat
module's implementation).  A player sees only public tags of the others and only what concerns
themselves; the moderator sees everything.  ``can_write`` / ``why_not`` of every channel follow the
same order of checks as the ``speak`` commit gate (contracts §2.3):

1. ``closed`` / ``listen`` mode            -> ``CHANNEL_CLOSED``
2. frozen channel                         -> ``NO_FLOOR``
3. ``turn`` / ``queue``: needs an *active, un-paused* token -> ``NO_FLOOR``
4. ``free``: read-only / not in ``writers`` -> ``NO_FLOOR``; per-member message limit -> ``QUOTA_EXCEEDED``
   (system DMs have no limit and need no token)

``why_not`` is the string ``"<CODE>：<one-sentence reason>"`` (``None`` when the agent can write).

``max_chars`` (v0.13) is the length cap of ONE free-text message the agent could write there right now
(longer text is sent back by the gate with ``TOO_LONG``, v0.14 H16 - it used to be cut, D19): the token's
per-message cap clipped to what is left of its total (turn / queue), ``max_chars_per_message`` of the
discussion (free), the ``dm`` default (system DM).  A free discussion's ``floor.free`` also carries
``max_chars_per_message`` (v0.14, H17), so a model can read the cap of one message next to the other numbers.
``None`` when the agent cannot write or the moderator writes (no cap).  ``seat_status`` (``seat.py``) and
``speech_gate`` (``chat.py``) say the same; ``tests/v12/world`` locks the three together.

The board never reveals that a person plays a seat (v0.13.1, G15): a floor the person holds in a public channel has
no deadline, yet the board shows the nominal length of the turn for it like for any other holder - there is no
"untimed" anywhere in it.

``facts`` (v0.14, H10) is the *hard-fact desk*: the messages of the ledger the scenario marked with ``record`` (a template's
``fact`` / ``claim``) and the public declarations of abilities, as the agent can read them (see :func:`_facts_block`).
It lets an agent tell what happened by the rules from what somebody merely said.
"""

from __future__ import annotations

import copy
from typing import Any

from app.world.chat import token_untimed
from app.world.game import ability_holders
from app.world.projection import seat_sorted
from app.world.spec import ParamSpec
from app.world.state import ChannelState
from app.world.types import ENGINE_ID, MODERATOR_ID, Code

MAX_RESOLVED_TICKETS = 20  # resolved tickets kept on the board (open ones are always listed)
MAX_FACTS = 300  # recorded messages kept on the board's hard-fact desk (the newest ones)


def _secs(value: float | None) -> float | None:
    return None if value is None else round(max(0.0, float(value)), 1)


def param_brief(spec: ParamSpec) -> str:
    """One-line description of a template / ability parameter type for the agent."""
    if spec.type == "enum":
        text = f"enum({'|'.join(spec.values)})"
    elif spec.type == "int":
        low = "" if spec.min is None else str(spec.min)
        high = "" if spec.max is None else str(spec.max)
        text = f"int({low}..{high})" if (low or high) else "int"
    elif spec.type == "text":
        text = f"text(最多{spec.max_chars}字)"
    else:
        text = spec.type
    return f"{text}?" if spec.optional else text


# --------------------------------------------------------------------------- channels


def _token_seconds(world: Any, channel: ChannelState, group: Any, token: Any, now: float) -> float | None:
    if token_untimed(world, channel, token):
        # G15 (v0.13.1): the seat a person plays holds this floor without a deadline - but the board must not say so.
        # It shows the nominal length of the turn, as it would for any other holder, and never a word about "untimed".
        return _secs(token.duration_s)
    if group.paused:
        return _secs(token.remaining)
    if token.deadline is not None:
        return _secs(token.deadline - now)
    return None


def _write_status(world: Any, channel: ChannelState, agent_id: str, is_moderator: bool) -> tuple[bool, str | None]:
    """Could ``agent_id`` ``speak`` in ``channel`` right now?  ``(can_write, why_not)``."""
    if is_moderator:
        return True, None
    if agent_id not in channel.members:
        return False, f"{Code.NOT_MEMBER}：你不是该频道的成员。"
    mode = channel.mode
    if mode in ("closed", "listen"):
        if channel.system:
            return False, f"{Code.CHANNEL_CLOSED}：你与主持人的私聊已被关闭。"
        if mode == "listen":
            return False, f"{Code.CHANNEL_CLOSED}：该频道当前只供收听，不能发言。"
        return False, f"{Code.CHANNEL_CLOSED}：该频道当前已关闭，不能发言。"
    if channel.frozen:
        return False, f"{Code.NO_FLOOR}：频道已被冻结，等待主持人裁决。"
    if mode in ("turn", "queue"):
        group = channel.group
        tokens = [] if group is None else list(group.tokens)
        active_now = next((t for t in tokens if t.status == "active"), None)
        mine_active = next((t for t in tokens if t.holder == agent_id and t.status == "active"), None)
        if mine_active is not None:
            if group is not None and group.paused:
                return False, f"{Code.NO_FLOOR}：发言权已暂停，等待主持人恢复。"
            if (
                mine_active.messages_used >= mine_active.max_messages
                or mine_active.chars_used >= mine_active.max_chars_total
            ):
                return False, f"{Code.NO_FLOOR}：你本次的发言额度已用完。"
            return True, None
        speaker = f"（当前发言人：{active_now.holder}）" if active_now is not None else ""
        mine_pending = next((t for t in tokens if t.holder == agent_id and t.status == "pending"), None)
        if mine_pending is not None:
            ahead = 0
            for token in tokens:
                if token is mine_pending:
                    break
                if token.status in ("active", "pending"):
                    ahead += 1
            return False, f"{Code.NO_FLOOR}：还没轮到你发言（排在第 {ahead + 1} 位）{speaker}。"
        if any(t.holder == agent_id for t in tokens):
            return False, f"{Code.NO_FLOOR}：你本轮的发言权已经结束。"
        return False, f"{Code.NO_FLOOR}：你现在没有发言权{speaker}。"
    if mode == "free":
        if agent_id in channel.readonly:
            return False, f"{Code.NO_FLOOR}：你在该频道是只读成员，不能发言。"
        free = channel.free
        if free is not None and free.writers is not None and agent_id not in free.writers:
            return False, f"{Code.NO_FLOOR}：该讨论只允许指定成员发言。"
        if free is not None and not channel.system:
            used = int(free.counts.get(agent_id, 0))
            if used >= free.max_messages_per_member:
                return False, (
                    f"{Code.QUOTA_EXCEEDED}：你在该频道的发言条数已达上限（{free.max_messages_per_member} 条）。"
                )
        return True, None
    return False, f"{Code.NO_FLOOR}：现在不能在该频道发言。"


def _max_chars(
    world: Any, channel: ChannelState, agent_id: str, is_moderator: bool, can_write: bool
) -> int | None:
    """The cap of one message of ``agent_id`` in ``channel`` (same numbers as ``speech_gate(...).max_chars``)."""
    if is_moderator or not can_write:
        return None
    mode = channel.mode
    if mode in ("turn", "queue"):
        group = channel.group
        tokens = [] if group is None else group.tokens
        mine = next((t for t in tokens if t.holder == agent_id and t.status == "active"), None)
        if mine is None:
            return None
        return max(0, min(mine.max_chars_per_message, mine.max_chars_total - mine.chars_used))
    if mode == "free":
        if channel.free is not None:
            return channel.free.max_chars_per_message
        defaults = world.spec.defaults
        return defaults.dm.max_chars_per_message if channel.system else defaults.free.max_chars_per_message
    return None


def _floor_block(world: Any, channel: ChannelState, agent_id: str, is_moderator: bool) -> dict[str, Any]:
    now = float(world.clock.now())
    mode = channel.mode
    floor: dict[str, Any] = {
        "kind": mode if mode in ("turn", "queue", "free") else "none",
        "active": None,
        "paused": False,
        "seconds_left": None,
        "queue": [],
        "mine": None,
        "free": None,
    }
    group = channel.group
    if mode in ("turn", "queue") and group is not None:
        floor["paused"] = bool(group.paused)
        floor["queue"] = [
            {
                "holder": t.holder,
                "state": t.status,
                "messages_used": t.messages_used,
                "max_messages": t.max_messages,
            }
            for t in group.tokens
        ]
        active = next((t for t in group.tokens if t.status == "active"), None)
        if active is not None:
            floor["active"] = active.holder
            floor["seconds_left"] = _token_seconds(world, channel, group, active, now)
        if not is_moderator:
            mine = next((t for t in group.tokens if t.holder == agent_id and t.status == "active"), None)
            if mine is not None:
                floor["mine"] = {
                    "messages_left": max(0, mine.max_messages - mine.messages_used),
                    "chars_per_message": mine.max_chars_per_message,
                    "chars_left": max(0, mine.max_chars_total - mine.chars_used),
                    "seconds_left": _token_seconds(world, channel, group, mine, now),
                }
    free = channel.free
    if mode == "free" and free is not None and not channel.system:
        floor["free"] = {
            "my_messages_left": (
                None
                if is_moderator
                else max(0, free.max_messages_per_member - int(free.counts.get(agent_id, 0)))
            ),
            "max_messages_per_member": free.max_messages_per_member,
            "max_chars_per_message": free.max_chars_per_message,  # v0.14 (H17): the cap of ONE message (longer: TOO_LONG)
            "chars_used": free.chars_used,
            "max_chars_total": free.max_chars_total,
            "elapsed_s": _secs(now - free.started_gt),
            "duration_s": free.duration_s,
            "silent_s": _secs(now - max(free.last_message_gt, free.started_gt)),
            "writers": None if free.writers is None else list(free.writers),
        }
    return floor


def _channel_block(world: Any, channel: ChannelState, agent_id: str, is_moderator: bool) -> dict[str, Any]:
    can_write, why_not = _write_status(world, channel, agent_id, is_moderator)
    return {
        "id": channel.id,
        "name": channel.name,
        "type": channel.type,
        "mode": channel.mode,
        "members": list(channel.members),
        "readonly": list(channel.readonly),
        "frozen": bool(channel.frozen),
        "system": bool(channel.system),
        "can_write": can_write,
        "why_not": why_not,
        "max_chars": _max_chars(world, channel, agent_id, is_moderator, can_write),
        "floor": _floor_block(world, channel, agent_id, is_moderator),
    }


# --------------------------------------------------------------------------- the board


def _answer_label(request: Any, value: str) -> str:
    for option in request.options:
        if option.get("id") == value:
            return str(option.get("label") or value)
    return value


def _requests_block(world: Any, agent_id: str, is_moderator: bool) -> list[dict[str, Any]]:
    now = float(world.clock.now())
    rows: list[dict[str, Any]] = []
    for request in world.state.requests.values():
        if request.status != "open":
            continue
        if not is_moderator and agent_id not in request.to:
            continue
        row: dict[str, Any] = {
            "id": request.id,
            "kind": request.kind,
            "question": request.question,
            "options": [{"id": o.get("id", ""), "label": o.get("label", "")} for o in request.options],
            "seconds_left": None if request.deadline is None else _secs(request.deadline - now),
            "sealed": request.sealed,
            "answered": False,
            "to": list(request.to),
        }
        row["share_answers"] = request.share_answers
        row["auto_close"] = request.auto_close
        if request.share_answers:
            # Everybody involved sees the current answers (they may still change them until it closes).
            row["answers"] = [
                {
                    "player_id": pid,
                    "value": request.answers[pid].value,
                    "label": _answer_label(request, request.answers[pid].value),
                    "reason": request.answers[pid].reason,
                }
                for pid in seat_sorted(world, [p for p, a in request.answers.items() if not a.defaulted])
            ]
        if is_moderator:
            row["answered_by"] = seat_sorted(
                world, [pid for pid, a in request.answers.items() if not a.defaulted]
            )
            row["default"] = request.default
        else:
            mine = request.answers.get(agent_id)
            row["answered"] = mine is not None
            if mine is not None:
                row["my_answer"] = {"value": mine.value, "reason": mine.reason}
        rows.append(row)
    return rows


def _abilities_block(world: Any, agent_id: str, is_moderator: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ability_id, spec in world.spec.abilities.items():
        state = world.state.abilities.get(ability_id)
        holders = ability_holders(world, ability_id)
        params = {name: param_brief(p) for name, p in spec.params.items()}
        if is_moderator:
            rows.append(
                {
                    "id": ability_id,
                    "description": spec.description,
                    "enabled": bool(state and state.enabled),
                    "allowed": None if state is None or state.allowed is None else list(state.allowed),
                    "uses": dict(state.uses) if state else {},
                    "quota": spec.quota,
                    "holders": holders,
                    "params": params,
                }
            )
            continue
        if agent_id not in holders:
            continue
        usable = bool(state and state.enabled and (state.allowed is None or agent_id in state.allowed))
        used = int(state.uses.get(agent_id, 0)) if state else 0
        rows.append(
            {
                "id": ability_id,
                "description": spec.description,
                "enabled": usable,
                "uses_left": max(0, spec.quota - used),
                "params": params,
            }
        )
    return rows


def _tickets_block(world: Any, agent_id: str, is_moderator: bool) -> list[dict[str, Any]]:
    tickets = [t for t in world.state.tickets.values() if is_moderator or t.player_id == agent_id]
    resolved = [t for t in tickets if t.status != "open"]
    dropped = {t.id for t in resolved[:-MAX_RESOLVED_TICKETS]} if len(resolved) > MAX_RESOLVED_TICKETS else set()
    return [
        {
            "id": t.id,
            "player_id": t.player_id,
            "status": t.status,
            "preview": t.preview,
            "verdict": t.verdict,
        }
        for t in tickets
        if t.id not in dropped
    ]


def _facts_block(world: Any, agent_id: str) -> list[dict[str, Any]]:
    """The hard-fact desk of ``agent_id`` (v0.14, H10): the messages of the ledger that carry a *record* mark, oldest first.

    What counts: a message built from a template whose ``record`` is ``fact`` / ``claim`` (the scenario marks them: what
    the rules announced vs. what a player said), and every public declaration of an ability (``kind == "ability"``), which
    is always a ``fact``.  Who sees what is what the agent could read in the ledger anyway (``entries_visible``): a fact
    announced into a private channel is on the board of its members only.  At most the newest ``MAX_FACTS``.

    One row: ``{"seq", "phase", "kind", "actor_id", "text", "template_id"}`` - ``phase`` is the phase text of the world when
    the message was committed (the ``phase.changed`` entry before it; ``""`` before the first one), ``actor_id`` is the
    player who said it and ``None`` for what the moderator announced, ``template_id`` is ``None`` for an ability
    declaration."""
    templates = world.spec.templates
    phase = ""
    rows: list[dict[str, Any]] = []
    for entry in world.ledger:
        if entry.type == "phase.changed":
            phase = str(entry.data.get("text") or "")
            continue
        if entry.type != "message" or agent_id not in entry.visible_to_ids:
            continue
        data = entry.data
        template_id = data.get("template_id") or None
        kind: str | None = None
        if data.get("kind") == "ability":
            kind, template_id = "fact", None
        elif template_id is not None:
            template = templates.get(str(template_id))
            kind = template.record if template is not None else None
        if kind is None:
            continue
        rows.append(
            {
                "seq": entry.seq,
                "phase": phase,
                "kind": kind,
                "actor_id": None if entry.actor_id in (MODERATOR_ID, ENGINE_ID) else entry.actor_id,
                "text": str(data.get("content") or ""),
                "template_id": None if template_id is None else str(template_id),
            }
        )
    return rows[-MAX_FACTS:]


def build_board(world: Any, agent_id: str) -> dict[str, Any]:
    """The state board of ``agent_id`` (a player id or ``moderator``); JSON-serializable.

    Raises ``KeyError`` for an unknown agent id.
    """
    state = world.state
    is_moderator = agent_id == MODERATOR_ID
    me_player = None if is_moderator else world.player(agent_id)
    if not is_moderator and me_player is None:
        raise KeyError(f"unknown agent: {agent_id}")

    if is_moderator:
        me: dict[str, Any] = {
            "id": MODERATOR_ID,
            "name": world.display_name(MODERATOR_ID),
            "seat": "",
            "tags": [],
            "attributes": {},
            "brief": "",
            "role": "",
            "faction": "",
            "is_moderator": True,
        }
    else:
        role_spec = world.spec.setup.roles.get(me_player.role)
        me = {
            "id": me_player.id,
            "name": me_player.name,
            "seat": me_player.seat,
            "tags": list(me_player.tags),
            "attributes": copy.deepcopy(me_player.attributes),
            "brief": role_spec.brief if role_spec else "",
            "role": me_player.role,
            "faction": me_player.faction,
            "is_moderator": False,
        }

    public = set(world.spec.setup.public_tags)
    players: list[dict[str, Any]] = []
    for p in state.players:
        row: dict[str, Any] = {"id": p.id, "seat": p.seat, "name": p.name, "out": bool(world.is_out(p))}
        if is_moderator:
            row["tags"] = list(p.tags)
            row["role"] = p.role
            row["faction"] = p.faction
            row["attributes"] = copy.deepcopy(p.attributes)
        elif p.id == agent_id:
            row["tags"] = list(p.tags)
        else:
            row["tags"] = [t for t in p.tags if t in public]
        players.append(row)

    return {
        "me": me,
        "phase": state.phase,
        "status": state.status,
        "now": round(float(world.clock.now()), 1),
        "players": players,
        "channels": [
            _channel_block(world, c, agent_id, is_moderator) for c in state.channels if agent_id in c.members
        ],
        "requests": _requests_block(world, agent_id, is_moderator),
        "abilities": _abilities_block(world, agent_id, is_moderator),
        "tickets": _tickets_block(world, agent_id, is_moderator),
        # v0.14 (H10): the hard-fact desk - the recorded messages (facts the rules announced, claims players made) this
        # agent can read, newest ``MAX_FACTS``, oldest first.
        "facts": _facts_block(world, agent_id),
        # Spectator panel: only the moderator sees it.  For a player it would leak hidden information
        # (faction statistics), so the key stays (stable shape) but is always empty.
        "dashboard": {
            "factions": copy.deepcopy(state.dashboard.factions) if is_moderator else [],
            "tag_stats": copy.deepcopy(state.dashboard.tag_stats) if is_moderator else [],
        },
    }
