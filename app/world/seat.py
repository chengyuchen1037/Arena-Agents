"""What the person who plays a seat may do right now (v0.13, decision F12).

``seat_status(world, seat)`` is the human's counterpart of :func:`app.world.board.build_board`: for every
channel the seat belongs to, whether it may write (answered by the very function that decides at commit
time, :func:`app.world.chat.speech_gate`), why not, how long one message may be, what is left of its
token; the open requests addressed to it; the abilities it may use now; and ``attention``, the channels
that wait for the player.  The UI draws exactly this and never re-derives a speaking rule.

Pure: nothing here changes the world.  Engine-neutral: no game vocabulary, no fixed participant ids.
``tests/v12/world/test_seat.py`` locks the numbers to ``speech_gate``, the commit gate and the board.
"""

from __future__ import annotations

from typing import Any

from app.world.chat import FLOOR_MODES, speech_gate, token_untimed
from app.world.game import ability_holders
from app.world.spec import ParamSpec
from app.world.state import ChannelState, RequestState, Token
from app.world.types import Code, Receipt

__all__ = ["plain_names", "seat_status"]


def _r1(value: float | None) -> float | None:
    return None if value is None else round(max(0.0, float(value)), 1)


def _r3(value: float | None) -> float | None:
    return None if value is None else round(float(value), 3)


def plain_names(world: Any, text: str) -> str:
    """Drop the internal id from the ``<id> <name>`` labels the engine writes into its messages: a person
    reads nicknames ("the nickname is what people say"), the ids are for models."""
    players = sorted(world.state.players, key=lambda p: len(p.id), reverse=True)
    for player in players:
        if player.name and player.name != player.id:
            text = text.replace(f"{player.id} {player.name}", player.name)
    return text


_plain_names = plain_names


def _active_token(channel: ChannelState, seat: str) -> Token | None:
    group = channel.group if channel.mode in FLOOR_MODES else None
    if group is None:
        return None
    return next((t for t in group.tokens if t.holder == seat and t.status == "active"), None)


def _token_clock(world: Any, channel: ChannelState, token: Token) -> tuple[float | None, float | None]:
    """``(seconds_left, deadline_clock)`` of an active token: a paused / frozen group has no deadline, and neither
    has the token of a seat that is played in a public channel (v0.13.1, G10: ``untimed``, both are ``None``)."""
    if token_untimed(world, channel, token):
        return None, None
    group = channel.group
    if group is not None and group.paused:
        return _r1(token.remaining), None
    if token.deadline is None:
        return None, None
    return _r1(token.deadline - float(world.clock.now())), _r3(token.deadline)


def _write_state(world: Any, channel: ChannelState, seat: str) -> tuple[bool, str | None, str | None, int | None]:
    """``(can_write, why_not, code, max_chars)`` of ``seat`` in ``channel`` - what a ``speak`` would meet."""
    gate = speech_gate(world, channel, seat)
    if isinstance(gate, Receipt):
        return False, _plain_names(world, gate.message), gate.code, None
    token = gate.token
    if token is not None and (
        token.messages_used >= token.max_messages or token.chars_used >= token.max_chars_total
    ):
        # (unreachable through the intents: a token that used everything ends by itself)
        return False, "你本次的发言额度已用完。", Code.NO_FLOOR, None
    if gate.max_chars is not None and gate.max_chars <= 0:
        return False, "你本次发言权的字数已经用尽。", Code.NO_FLOOR, None
    return True, None, None, gate.max_chars


def _floor_state(world: Any, channel: ChannelState, seat: str) -> tuple[dict[str, Any] | None, bool]:
    """``(floor, can_pass)``: the seat's token (turn / queue), or its quota in a free discussion."""
    if channel.mode in FLOOR_MODES and channel.group is not None:
        group = channel.group
        mine = _active_token(channel, seat)
        floor: dict[str, Any] = {
            "kind": group.mode,
            "mine": mine is not None,
            "paused": bool(group.paused),
            "messages_left": None,
            "chars_left": None,
            "seconds_left": None,
            "deadline_clock": None,
            "untimed": False,
        }
        if mine is not None:
            floor["messages_left"] = max(0, mine.max_messages - mine.messages_used)
            floor["chars_left"] = max(0, mine.max_chars_total - mine.chars_used)
            floor["seconds_left"], floor["deadline_clock"] = _token_clock(world, channel, mine)
            floor["untimed"] = token_untimed(world, channel, mine)
        # ``pass`` ends an active token of a channel that is neither frozen nor paused (``ChatModule.on_pass``)
        return floor, mine is not None and not channel.frozen and not group.paused
    free = channel.free
    if channel.mode == "free" and free is not None and not channel.system:
        floor = {
            "kind": "free",
            "mine": False,
            "paused": False,
            "messages_left": max(0, free.max_messages_per_member - int(free.counts.get(seat, 0))),
            "chars_left": None,
            "seconds_left": None,
            "deadline_clock": None,
            "untimed": False,
        }
        return floor, False
    return None, False


def _channel_state(world: Any, channel: ChannelState, seat: str) -> dict[str, Any]:
    can_write, why_not, code, max_chars = _write_state(world, channel, seat)
    floor, can_pass = _floor_state(world, channel, seat)
    return {
        "can_write": can_write,
        "why_not": why_not,
        "code": code,
        "max_chars": max_chars,
        "floor": floor,
        "can_pass": can_pass,
    }


# --------------------------------------------------------------------------- requests


def _default_channel(world: Any, channels: list[ChannelState]) -> ChannelState | None:
    """The channel a request that fits nowhere is shown in (the front end's default channel)."""
    groups = [c for c in channels if c.type != "dm" and not c.system]
    everyone = {p.id for p in world.state.players}
    for channel in groups:
        if channel.type == "public":
            return channel
    for channel in groups:
        if everyone <= set(channel.members):
            return channel
    return groups[0] if groups else None


def _request_channel(world: Any, seat: str, request: RequestState) -> str | None:
    """Where the request card shows up for ``seat``: the smallest channel of the seat that holds every
    recipient (same rule as the front end's ``requestChannelId``), else the default channel."""
    mine = [c for c in world.state.channels if seat in c.members]
    fits = [c for c in mine if request.to and all(t in c.members for t in request.to)]
    fits.sort(key=lambda c: len(c.members))  # stable: ties keep the creation order
    if fits:
        return fits[0].id
    fallback = _default_channel(world, mine)
    return fallback.id if fallback is not None else None


def _answer_label(request: RequestState, value: str) -> str:
    for option in request.options:
        if option.get("id") == value:
            return str(option.get("label") or value)
    return value


def _request_states(world: Any, seat: str) -> list[dict[str, Any]]:
    now = float(world.clock.now())
    rows: list[dict[str, Any]] = []
    for request in world.state.requests.values():
        if request.status != "open" or seat not in request.to:
            continue
        mine = request.answers.get(seat)
        rows.append(
            {
                "id": request.id,
                "kind": request.kind,
                # a text answer is cut to this (``None`` for a choice)
                "max_chars": int(world.spec.defaults.request.max_text_chars) if request.kind == "text" else None,
                "answered": mine is not None,
                "my_answer": (
                    None
                    if mine is None
                    else {"value": mine.value, "label": _answer_label(request, mine.value), "reason": mine.reason}
                ),
                "seconds_left": None if request.deadline is None else _r1(request.deadline - now),
                "deadline_clock": _r3(request.deadline),
                "channel_id": _request_channel(world, seat, request),
            }
        )
    return rows


# --------------------------------------------------------------------------- abilities


def _param_view(world: Any, name: str, spec: ParamSpec) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "type": spec.type, "optional": bool(spec.optional)}
    if spec.type == "text":
        out["max_chars"] = int(spec.max_chars)
    elif spec.type == "enum":
        out["values"] = list(spec.values)
    elif spec.type == "int":
        out["min"] = spec.min
        out["max"] = spec.max
    elif spec.type in ("player_id", "player_ids"):
        out["candidates"] = [p.id for p in world.state.players if not world.is_out(p)]
    return out


def _ability_states(world: Any, seat: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ability_id, spec in world.spec.abilities.items():
        state = world.state.abilities.get(ability_id)
        if state is None or not state.enabled or (state.allowed is not None and seat not in state.allowed):
            continue
        if seat not in ability_holders(world, ability_id):
            continue
        quota_left = max(0, int(spec.quota) - int(state.uses.get(seat, 0)))
        if quota_left <= 0:
            continue
        rows.append(
            {
                "id": ability_id,
                "description": spec.description,
                "params": [_param_view(world, name, p) for name, p in spec.params.items()],
                "quota_left": quota_left,
            }
        )
    return rows


# --------------------------------------------------------------------------- the status


def seat_status(world: Any, seat: str) -> dict[str, Any]:
    """The state of ``seat`` as its player needs it (JSON-serializable; ``KeyError`` for an unknown seat)::

        {"seat": id, "seq": ledger seq,
         "channels": {channel_id: {"can_write", "why_not", "code", "max_chars",
                                   "floor": {"kind", "mine", "paused", "messages_left", "chars_left",
                                             "seconds_left", "deadline_clock", "untimed"} | None,
                                   "can_pass"}},
         "requests": [{"id", "kind", "max_chars", "answered", "my_answer", "seconds_left",
                       "deadline_clock", "channel_id"}],
         "abilities": [{"id", "description", "params": [{"name", "type", ...}], "quota_left"}],
         "attention": [channel ids that wait for the player]}

    ``can_write`` / ``why_not`` / ``max_chars`` are what a ``speak`` would meet at the commit gate (the
    running state of the game is not part of it: the UI says "paused" itself).  ``seq`` is the ledger
    position the numbers were computed at: a draft the player starts now is ``based_on_seq`` it.
    ``deadline_clock`` is an absolute game-clock second, so a client can count down without further
    events (``None`` while the clock of a paused floor is frozen).  ``floor.untimed`` (v0.13.1, G10) is true when
    the seat holds a floor in a public channel that has no deadline - the seat is the one a person plays - and
    then ``seconds_left`` and ``deadline_clock`` are ``None``.  ``attention`` lists the channels in
    which the seat holds the floor and may write, and the channels that show an open request the seat has
    not answered yet.
    """
    if world.player(seat) is None:
        raise KeyError(f"unknown seat: {seat}")
    channels: dict[str, dict[str, Any]] = {}
    for channel in world.state.channels:
        if seat in channel.members:
            channels[channel.id] = _channel_state(world, channel, seat)
    requests = _request_states(world, seat)
    waiting = {
        cid for cid, state in channels.items() if state["can_write"] and (state["floor"] or {}).get("mine")
    }
    waiting.update(r["channel_id"] for r in requests if not r["answered"] and r["channel_id"])
    return {
        "seat": seat,
        "seq": int(world.seq),
        "channels": channels,
        "requests": requests,
        "abilities": _ability_states(world, seat),
        "attention": [cid for cid in channels if cid in waiting],
    }
