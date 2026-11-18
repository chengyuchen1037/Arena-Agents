"""Replay of a saved game (docs/v0.13-plan.md F36, contract section 11.8).

:func:`build_replay` turns the ledger of a saved game (a ``GameSession`` v5 as a plain dict) into the
spectator events the live stream carries - ``message.committed``, ``channel.created|updated``,
``floor.changed``, ``request.*``, ``player.updated``, ``phase.changed`` ... - so the front end can feed
them to its ordinary client ``Store``.  Every event also carries the ledger ``seq`` and the game-clock
second ``gt`` it happened at, which is what a player needs to pace the replay (speed, pause, seek).

The ledger is the whole truth about what spectators saw: thoughts, tool calls and agent states are not in
it, so a replay shows exactly the spectator view the live game showed (D5).  The ledger of a game saved by
v0.12.1 is the same, so converted games replay too.

How it works: the ledger records *facts* (``floor.granted``, ``request.answered`` ...), while the client
wants *views* (a whole channel with its floor, a whole request).  :class:`_Replayer` therefore keeps a
small model of the channels (members, mode, floor tokens or free-discussion counters), the requests, the
tickets and the players, updates it entry by entry - the same rules as ``app.world.chat`` /
``app.world.game`` apply them - and emits the views after each change.  ``tests/test_replay.py`` checks it
against the real world: after every step the replayed channel views equal ``chat.channel_view``, and a
whole demo game replayed through the real client store ends in the state the server reports.
"""

from __future__ import annotations

import copy
from typing import Any

from app.world.game import PREVIEW_CHARS, _preview
from app.world.projection import message_view
from app.world.types import MODERATOR_ID, LedgerEntry

__all__ = ["REPLAY_VERSION", "build_replay"]

REPLAY_VERSION = 1
_FLOOR_MODES = ("turn", "queue")
_NO_FLOOR = {"kind": "none", "active": None, "paused": False, "seconds_left": None, "queue": [], "free": None}


def _r1(value: float | None) -> float | None:
    return None if value is None else round(max(0.0, float(value)), 1)


class _Replayer:
    def __init__(self, payload: dict[str, Any]) -> None:
        world = payload.get("world") or {}
        self.entries = [LedgerEntry.model_validate(e) for e in world.get("ledger") or []]
        spec = payload.get("scenario_spec") or {}
        self.out_tags = set((spec.get("ui") or {}).get("out_tags") or [])
        self.public_tags = list(((spec.get("setup") or {}).get("public_tags")) or [])
        self.ui = copy.deepcopy(spec.get("ui") or {})
        self.participants = [dict(p) for p in payload.get("participants") or []]
        self.order = {str(p.get("id")): i for i, p in enumerate(self.participants)}
        self.players: dict[str, dict[str, Any]] = {
            str(p["id"]): {"tags": [], "out": False} for p in self.participants if "id" in p
        }
        self.channels: dict[str, dict[str, Any]] = {}
        self.requests: dict[str, dict[str, Any]] = {}
        self.tickets: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.payload = payload

    # ------------------------------------------------------------------ helpers --
    def _sorted(self, ids: Any) -> list[str]:
        """``ids`` in seating order (unknown ids last, in their given order)."""
        return sorted((str(i) for i in ids), key=lambda i: self.order.get(i, len(self.order)))

    def _emit(self, entry: LedgerEntry, type_: str, **data: Any) -> None:
        self.events.append({"seq": entry.seq, "gt": entry.gt, "type": type_, "data": data})

    # ------------------------------------------------------------------ channels --
    def _new_free(self, now: float, limits: dict[str, Any] | None = None, writers: list[str] | None = None) -> dict[str, Any]:
        limits = limits or {}
        return {
            "started_gt": now,
            "last_message_gt": now,
            "counts": {},
            "chars_used": 0,
            "duration_s": limits.get("duration_s", 0.0),
            "max_messages_per_member": limits.get("max_messages_per_member", 0),
            "max_chars_per_message": limits.get("max_chars_per_message", 0),
            "max_chars_total": limits.get("max_chars_total", 0),
            "writers": list(writers) if writers is not None else None,
        }

    def _token_left(self, group: dict[str, Any], token: dict[str, Any], now: float) -> float | None:
        if token["state"] != "active":
            return None
        if group["paused"]:
            return _r1(token["remaining"])
        return _r1(token["duration_s"] - (now - token["started_gt"]))

    def _floor_view(self, channel: dict[str, Any], now: float) -> dict[str, Any]:
        group = channel["group"] if channel["mode"] in _FLOOR_MODES else None
        if group is not None:
            active = [t for t in group["tokens"] if t["state"] == "active"]
            lefts = [s for s in (self._token_left(group, t, now) for t in active) if s is not None]
            return {
                "kind": group["mode"],
                "active": active[0]["holder"] if active else None,
                "paused": bool(group["paused"]),
                "seconds_left": min(lefts) if lefts else None,
                "queue": [
                    {
                        "holder": t["holder"],
                        "state": t["state"],
                        "messages_used": t["messages_used"],
                        "max_messages": t["max_messages"],
                    }
                    for t in group["tokens"]
                ],
                "free": None,
            }
        free = channel["free"]
        if free is not None and channel["mode"] == "free":
            return {
                "kind": "free",
                "active": None,
                "paused": False,
                "seconds_left": None,
                "queue": [],
                "free": {
                    "chars_used": free["chars_used"],
                    "max_chars_total": free["max_chars_total"],
                    "elapsed_s": _r1(now - free["started_gt"]),
                    "duration_s": _r1(free["duration_s"]),
                    "silent_s": _r1(now - max(free["started_gt"], free["last_message_gt"])),
                    "message_counts": dict(free["counts"]),
                    "max_messages_per_member": free["max_messages_per_member"],
                    "max_chars_per_message": free["max_chars_per_message"],
                    "writers": list(free["writers"]) if free["writers"] is not None else None,
                },
            }
        return copy.deepcopy(_NO_FLOOR)

    def _channel_view(self, channel: dict[str, Any], now: float) -> dict[str, Any]:
        return {
            "id": channel["id"],
            "name": channel["name"],
            "type": channel["type"],
            "mode": channel["mode"],
            "members": list(channel["members"]),
            "readonly": list(channel["readonly"]),
            "frozen": bool(channel["frozen"]),
            "system": bool(channel["system"]),
            "owner": channel["owner"],
            "floor": self._floor_view(channel, now),
        }

    def _emit_channel(self, e: LedgerEntry, channel: dict[str, Any], event: str = "channel.updated") -> None:
        self._emit(e, event, channel=self._channel_view(channel, e.gt))

    def _emit_floor(self, e: LedgerEntry, channel: dict[str, Any]) -> None:
        self._emit(e, "floor.changed", channel_id=channel["id"], floor=self._floor_view(channel, e.gt))

    def _suspend(self, group: dict[str, Any], now: float) -> None:
        for token in group["tokens"]:
            if token["state"] == "active" and token["remaining"] is None:
                token["remaining"] = max(0.0, token["duration_s"] - (now - token["started_gt"]))
        group["paused"] = True

    def _on_channel_created(self, e: LedgerEntry) -> None:
        d = e.data
        channel = {
            "id": str(d.get("id") or e.channel_id),
            "name": str(d.get("name") or ""),
            "type": str(d.get("type") or ""),
            "mode": str(d.get("mode") or "closed"),
            "members": list(d.get("members") or []),
            "readonly": list(d.get("readonly") or []),
            "frozen": bool(d.get("frozen")),
            "system": bool(d.get("system")),
            "owner": d.get("owner"),
            "group": None,
            "free": None,
        }
        floor = d.get("floor") or {}
        if floor.get("kind") == "free" and isinstance(floor.get("free"), dict):
            f = floor["free"]
            channel["free"] = self._new_free(e.gt, f, f.get("writers"))
        self.channels[channel["id"]] = channel
        self._emit_channel(e, channel, "channel.created")

    def _on_channel_updated(self, e: LedgerEntry) -> None:
        channel = self.channels.get(str(e.channel_id))
        if channel is None:
            return
        changes = e.data.get("changes") or {}
        mode = changes.get("mode")
        if isinstance(mode, dict) and mode.get("to"):
            channel["mode"] = str(mode["to"])
            channel["group"] = None
            channel["free"] = None
            channel["frozen"] = False
            if channel["mode"] == "free" and not channel["system"]:
                channel["free"] = self._new_free(e.gt, changes.get("limits") or {})
        elif changes.get("unfrozen"):
            channel["frozen"] = False
        elif isinstance(changes.get("limits"), dict) and channel["free"] is not None:
            limits = changes["limits"]
            for key in ("duration_s", "max_messages_per_member", "max_chars_per_message", "max_chars_total"):
                if key in limits:
                    channel["free"][key] = limits[key]
        if "name" in changes:
            channel["name"] = str(changes["name"])
        for member in changes.get("added") or []:
            if member not in channel["members"]:
                channel["members"].append(member)
        removed = set(changes.get("removed") or [])
        if removed:
            channel["members"] = [m for m in channel["members"] if m not in removed]
            channel["readonly"] = [m for m in channel["readonly"] if m not in removed]
            if channel["free"] is not None and channel["free"]["writers"] is not None:
                channel["free"]["writers"] = [w for w in channel["free"]["writers"] if w not in removed]
        if "readonly" in changes:
            channel["readonly"] = list(changes["readonly"] or [])
        self._emit_channel(e, channel)
        self._emit_floor(e, channel)

    def _on_floor_opened(self, e: LedgerEntry) -> None:
        channel = self.channels.get(str(e.channel_id))
        if channel is None:
            return
        d = e.data
        mode = str(d.get("mode") or "")
        limits = d.get("limits") or {}
        channel["frozen"] = False
        if mode == "free":
            channel["mode"] = "free"
            channel["group"] = None
            channel["free"] = self._new_free(e.gt, limits, d.get("holders") or None)
        else:
            holders = list(d.get("holders") if mode == "turn" else d.get("order") or [])
            channel["mode"] = mode
            channel["free"] = None
            channel["group"] = {
                "id": d.get("group_id"),
                "mode": mode,
                "paused": False,
                "tokens": [
                    {
                        "holder": h,
                        "state": "pending",
                        "messages_used": 0,
                        "chars_used": 0,
                        "max_messages": limits.get("max_messages", 0),
                        "max_chars_per_message": limits.get("max_chars_per_message", 0),
                        "max_chars_total": limits.get("max_chars_total", 0),
                        "duration_s": float(limits.get("duration_s", 0.0)),
                        "started_gt": e.gt,
                        "remaining": None,
                    }
                    for h in holders
                ],
            }
        self._emit_floor(e, channel)
        self._emit_channel(e, channel)

    def _token_of(self, channel: dict[str, Any], holder: str, *states: str) -> dict[str, Any] | None:
        group = channel["group"]
        if group is None:
            return None
        return next((t for t in group["tokens"] if t["holder"] == holder and (not states or t["state"] in states)), None)

    def _on_floor_granted(self, e: LedgerEntry) -> None:
        channel = self.channels.get(str(e.channel_id))
        if channel is None:
            return
        token = self._token_of(channel, str(e.data.get("holder")), "pending")
        if token is None:
            return
        limits = e.data.get("limits") or {}
        token["state"] = "active"
        token["started_gt"] = e.gt
        for key in ("max_messages", "max_chars_per_message", "max_chars_total"):
            if key in limits:
                token[key] = limits[key]
        if "duration_s" in limits:
            token["duration_s"] = float(limits["duration_s"])
        group = channel["group"]
        token["remaining"] = token["duration_s"] if group is not None and group["paused"] else None
        self._emit_floor(e, channel)

    def _on_floor_token_ended(self, e: LedgerEntry) -> None:
        channel = self.channels.get(str(e.channel_id))
        if channel is None:
            return
        token = self._token_of(channel, str(e.data.get("holder")), "pending", "active")
        if token is None:
            return
        token["state"] = "done"
        token["remaining"] = None
        token["messages_used"] = int(e.data.get("messages_used", token["messages_used"]))
        token["chars_used"] = int(e.data.get("chars_used", token["chars_used"]))
        self._emit_floor(e, channel)

    def _on_floor_completed(self, e: LedgerEntry) -> None:
        channel = self.channels.get(str(e.channel_id))
        if channel is None:
            return
        channel["group"] = None
        channel["mode"] = "closed"
        self._emit_floor(e, channel)
        self._emit_channel(e, channel)

    def _on_floor_paused(self, e: LedgerEntry) -> None:
        channel = self.channels.get(str(e.channel_id))
        if channel is None or channel["group"] is None:
            return
        self._suspend(channel["group"], e.gt)
        self._emit_floor(e, channel)

    def _on_floor_resumed(self, e: LedgerEntry) -> None:
        channel = self.channels.get(str(e.channel_id))
        if channel is None:
            return
        was_frozen = channel["frozen"]
        channel["frozen"] = False
        group = channel["group"]
        if group is not None:
            group["paused"] = False
            for token in group["tokens"]:
                if token["state"] == "active":
                    remaining = token["remaining"] if token["remaining"] is not None else token["duration_s"]
                    token["started_gt"] = e.gt - (token["duration_s"] - remaining)
                    token["remaining"] = None
        self._emit_floor(e, channel)
        if was_frozen:
            self._emit_channel(e, channel)

    def _on_floor_frozen(self, e: LedgerEntry) -> None:
        channel = self.channels.get(str(e.channel_id))
        if channel is None:
            return
        group = channel["group"] if channel["mode"] in _FLOOR_MODES else None
        if group is not None and not group["paused"]:
            self._suspend(group, e.gt)
        channel["frozen"] = True
        self._emit_channel(e, channel)
        self._emit_floor(e, channel)

    def _on_floor_closed(self, e: LedgerEntry) -> None:
        channel = self.channels.get(str(e.channel_id))
        if channel is None:
            return
        channel["group"] = None
        channel["free"] = None
        channel["frozen"] = False
        channel["mode"] = "closed"
        self._emit_floor(e, channel)
        self._emit_channel(e, channel)

    # ------------------------------------------------------------------ messages --
    def _account(self, channel: dict[str, Any], e: LedgerEntry) -> bool:
        """Book a committed message against the speaker's token / the free discussion (as ``chat._account``)."""
        kind = e.data.get("kind")
        content = str(e.data.get("content") or "")
        actor = e.actor_id
        if channel["mode"] in _FLOOR_MODES and channel["group"] is not None:
            if actor == MODERATOR_ID or kind not in ("speech", "template"):
                return False
            token = self._token_of(channel, actor, "active")
            if token is None:
                return False
            token["messages_used"] += 1
            token["chars_used"] += len(content)
            return True
        free = channel["free"]
        if channel["mode"] == "free" and free is not None:
            if kind == "speech" or (kind == "template" and actor != MODERATOR_ID):
                free["last_message_gt"] = e.gt
                if actor != MODERATOR_ID:
                    free["counts"][actor] = free["counts"].get(actor, 0) + 1
                    free["chars_used"] += len(content)
                return True
        return False

    def _on_message(self, e: LedgerEntry) -> None:
        view = message_view(e)
        self._emit(e, "message.committed", **view)
        kind = e.data.get("kind")
        ticket_id = e.data.get("ticket_id")
        if kind == "challenge" and ticket_id:
            ticket = {
                "id": str(ticket_id),
                "player_id": e.actor_id,
                "status": "open",
                "verdict": None,
                "preview": _preview(str(e.data.get("content") or ""), PREVIEW_CHARS),
                "opened_seq": e.seq,
            }
            self.tickets[ticket["id"]] = ticket
            self._emit(e, "challenge.opened", ticket=dict(ticket))
        elif kind == "reply" and ticket_id and str(ticket_id) in self.tickets:
            ticket = self.tickets[str(ticket_id)]
            ticket["status"] = "resolved"
            ticket["verdict"] = e.data.get("verdict")
            self._emit(e, "challenge.resolved", ticket_id=ticket["id"], verdict=ticket["verdict"])
        channel = self.channels.get(str(e.channel_id))
        if channel is not None and self._account(channel, e):
            self._emit_floor(e, channel)

    # ------------------------------------------------------------------ requests --
    def _label(self, request: dict[str, Any], value: str) -> str:
        for option in request["options"]:
            if option["id"] == value:
                return str(option.get("label") or value)
        return value

    def _request_view(self, request: dict[str, Any], now: float) -> dict[str, Any]:
        answers = request["answers"]
        seconds_left = None
        if request["status"] == "open" and request["duration_s"] is not None:
            seconds_left = _r1(request["duration_s"] - (now - request["opened_gt"]))
        return {
            "id": request["id"],
            "kind": request["kind"],
            "question": request["question"],
            "options": [dict(o) for o in request["options"]],
            "to": list(request["to"]),
            "answered_by": self._sorted(pid for pid, a in answers.items() if not a["defaulted"]),
            "status": request["status"],
            "sealed": request["sealed"],
            "share_answers": request["share_answers"],
            "auto_close": request["auto_close"],
            "seconds_left": seconds_left,
            "opened_seq": request["opened_seq"],
            "answers": [
                {
                    "player_id": pid,
                    "value": answers[pid]["value"],
                    "label": self._label(request, answers[pid]["value"]),
                    "reason": answers[pid]["reason"],
                    "defaulted": answers[pid]["defaulted"],
                }
                for pid in self._sorted(answers)
            ],
            "result": copy.deepcopy(request["result"]),
            "answer_changes": dict(request["answer_changes"]),
        }

    def _on_request_opened(self, e: LedgerEntry) -> None:
        d = e.data
        request = {
            "id": str(d.get("request_id")),
            "kind": str(d.get("kind") or "choice"),
            "question": str(d.get("question") or ""),
            "options": [{"id": str(o.get("id", "")), "label": str(o.get("label", ""))} for o in d.get("options") or []],
            "to": [str(t) for t in d.get("to") or []],
            "status": "open",
            "sealed": bool(d.get("sealed")),
            "share_answers": bool(d.get("share_answers")),
            "auto_close": str(d.get("auto_close") or "all_answered"),
            "duration_s": d.get("duration_s"),
            "opened_gt": e.gt,
            "opened_seq": e.seq,
            "answers": {},
            "answer_changes": {},
            "result": None,
        }
        self.requests[request["id"]] = request
        self._emit(e, "request.opened", request=self._request_view(request, e.gt))

    def _on_request_answered(self, e: LedgerEntry) -> None:
        d = e.data
        request = self.requests.get(str(d.get("request_id")))
        if request is None:
            return
        pid = str(d.get("player_id"))
        value = str(d.get("value"))
        before = request["answers"].get(pid)
        if before is not None and before["value"] != value:
            request["answer_changes"][pid] = request["answer_changes"].get(pid, 0) + 1
        request["answers"][pid] = {"value": value, "reason": str(d.get("reason") or ""), "defaulted": False}
        self._emit(
            e,
            "request.progress",
            request_id=request["id"],
            answered_by=self._sorted(request["answers"]),
            total=len(request["to"]),
        )
        self._emit(
            e,
            "request.answer",
            request_id=request["id"],
            player_id=pid,
            value=value,
            label=self._label(request, value),
            reason=str(d.get("reason") or ""),
            visible_to_ids=list(e.visible_to_ids),
        )

    def _finish_request(self, e: LedgerEntry, request: dict[str, Any], reason: str, result: dict[str, Any]) -> None:
        request["status"] = "cancelled" if reason == "cancelled" else "closed"
        request["result"] = result
        answered = self._sorted(pid for pid, a in request["answers"].items() if not a["defaulted"])
        self._emit(
            e,
            "request.closed",
            request_id=request["id"],
            reason=reason,
            answered_by=answered,
            defaulted=list(result["defaulted"]),
            result=copy.deepcopy(result),
        )
        # a whole view too: the defaulted answers of a closed request are part of a fresh snapshot
        self._emit(e, "request.updated", request=self._request_view(request, e.gt))

    def _on_request_closed(self, e: LedgerEntry) -> None:
        d = e.data
        request = self.requests.get(str(d.get("request_id")))
        if request is None:
            return
        for answer in d.get("answers") or []:
            request["answers"][str(answer["player_id"])] = {
                "value": str(answer.get("value")),
                "reason": str(answer.get("reason") or ""),
                "defaulted": bool(answer.get("defaulted")),
            }
        result = {
            "reason": str(d.get("reason") or ""),
            "tally": copy.deepcopy(d.get("tally")),
            "leaders": copy.deepcopy(d.get("leaders")),
            "defaulted": list(d.get("defaulted") or []),
            "missing": list(d.get("missing") or []),
        }
        self._finish_request(e, request, result["reason"], result)

    def _on_request_cancelled(self, e: LedgerEntry) -> None:
        request = self.requests.get(str(e.data.get("request_id")))
        if request is None:
            return
        result = {"reason": "cancelled", "tally": None, "leaders": None, "defaulted": [], "missing": []}
        self._finish_request(e, request, "cancelled", result)

    # ------------------------------------------------------------------ the rest --
    def _on_player_updated(self, e: LedgerEntry) -> None:
        d = e.data
        pid = str(d.get("player_id"))
        player = self.players.get(pid)
        tags = d.get("tags_after")
        if player is None or tags is None:
            return
        player["tags"] = [str(t) for t in tags]
        player["out"] = any(t in self.out_tags for t in player["tags"])
        self._emit(e, "player.updated", player_id=pid, tags=list(player["tags"]), out=player["out"])

    def _on_phase_changed(self, e: LedgerEntry) -> None:
        self._emit(e, "phase.changed", text=str(e.data.get("text") or ""))

    def _on_dashboard_updated(self, e: LedgerEntry) -> None:
        self._emit(e, "dashboard.updated", dashboard=copy.deepcopy(e.data))

    def _on_game_status(self, e: LedgerEntry) -> None:
        d = e.data
        data: dict[str, Any] = {"status": d.get("status"), "reason": d.get("reason", "")}
        if d.get("status") == "finished" or "summary" in d:  # the moderator's verdict
            data["winner"] = d.get("winner")
            data["summary"] = d.get("summary", "")
        else:  # running / paused / continued: a verdict is never in force then
            data["winner"] = None
        self._emit(e, "game.status", **data)

    def _on_ability_used(self, e: LedgerEntry) -> None:
        self._emit(e, "ability.used", ability_id=e.data.get("ability_id"), actor_id=e.data.get("actor_id", e.actor_id))

    HANDLERS = {
        "message": _on_message,
        "channel.created": _on_channel_created,
        "channel.updated": _on_channel_updated,
        "floor.opened": _on_floor_opened,
        "floor.granted": _on_floor_granted,
        "floor.token_ended": _on_floor_token_ended,
        "floor.completed": _on_floor_completed,
        "floor.paused": _on_floor_paused,
        "floor.resumed": _on_floor_resumed,
        "floor.frozen": _on_floor_frozen,
        "floor.closed": _on_floor_closed,
        "request.opened": _on_request_opened,
        "request.answered": _on_request_answered,
        "request.closed": _on_request_closed,
        "request.cancelled": _on_request_cancelled,
        "player.updated": _on_player_updated,
        "phase.changed": _on_phase_changed,
        "dashboard.updated": _on_dashboard_updated,
        "game.status": _on_game_status,
        "ability.used": _on_ability_used,
    }

    # ------------------------------------------------------------------ output --
    def initial(self) -> dict[str, Any]:
        """The session before the first ledger entry: seated players without identities, no channels."""
        p = self.payload
        return {
            "session_id": p.get("session_id", ""),
            "schema_version": p.get("schema_version"),
            "version": p.get("version", ""),
            "title": p.get("title", ""),
            "scenario_name": p.get("scenario_name", ""),
            "scenario_title": p.get("scenario_title", ""),
            "status": "idle",
            "pause_reason": "",
            "hold_reason": "",
            "winner": None,
            "summary": "",
            "phase_text": "未开始 · 等待主持人开局",
            "active_status_text": "重放",
            "seq": 0,
            "event_id": 0,
            "clock": 0.0,
            "participants": [
                {
                    "id": str(part["id"]),
                    "seat": part.get("seat"),
                    "name": part.get("name", ""),
                    "tags": [],
                    "out": False,
                    "avatar": part.get("avatar"),
                    "state": "sleeping",
                    "sleep_until": None,
                }
                for part in self.participants
                if "id" in part
            ],
            "moderator": {
                "id": MODERATOR_ID,
                "name": "主持人",
                "avatar": p.get("moderator_avatar"),
                "state": "sleeping",
                "sleep_until": None,
            },
            "channels": [],
            "messages": [],
            "requests": [],
            "tickets": [],
            "dashboard": {"factions": [], "tag_stats": []},
            "public_tags": list(self.public_tags),
            "tag_styles": copy.deepcopy(p.get("tag_styles") or {}),
            "ui": copy.deepcopy(self.ui),
            "usage_stats": copy.deepcopy(p.get("usage_stats") or {}),
            "model_bindings": {},
            "binding_issues": [],
            "restored_from_save": False,
            "restore_notice": "",
            "autosave_error": "",
            "budget": {
                "max_tokens": None,
                "max_cost_usd": None,
                "force_stop": False,
                "used_tokens": 0,
                "used_cost_usd": 0.0,
                "state": "ok",
            },
            "memory_enabled": bool(p.get("memory_enabled", True)),
            "review": {"available": False, "done": [], "running": False},
            "stall_notice": "",
            "demo": False,
        }

    def run(self) -> dict[str, Any]:
        for entry in self.entries:
            handler = self.HANDLERS.get(entry.type)
            if handler is not None:
                handler(self, entry)
        p = self.payload
        last_gt = self.entries[-1].gt if self.entries else 0.0
        return {
            "version": REPLAY_VERSION,
            "game": {
                "session_id": p.get("session_id", ""),
                "title": p.get("title", ""),
                "scenario_title": p.get("scenario_title", ""),
                "status": p.get("status", ""),
                "winner": ((p.get("world") or {}).get("state") or {}).get("winner"),
                "created_at": p.get("created_at"),
                "finished_at": p.get("finished_at"),
                "human_involved": bool(p.get("human_spans")),
            },
            "initial": self.initial(),
            "events": self.events,
            "duration_gt": last_gt,
        }


def build_replay(payload: dict[str, Any]) -> dict[str, Any]:
    """The replay of a saved game: ``{"version", "game", "initial", "events", "duration_gt"}``.

    ``payload`` is a game of the current save format (what ``GameStore.load`` returns).  ``events`` are
    ``{"seq", "gt", "type", "data"}`` in ledger order; ``type`` / ``data`` are exactly what the live SSE
    stream sends (contracts section 8), ``gt`` is the game-clock second.  ``initial`` is the session a
    spectator has before the first entry (the first events seat the identities and open the channels).
    """
    return _Replayer(payload).run()
