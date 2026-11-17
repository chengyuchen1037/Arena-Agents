"""Spectator-facing projections (contracts §2.8 / §8.1): the god-view snapshot and the small view
builders shared by the snapshot and the UI events.

* :func:`message_view`, :func:`request_view`, :func:`ticket_view` build the ``MessageView`` /
  ``RequestView`` / ``TicketView`` dictionaries of §8.1 (the same shapes are used by the SSE events).
* :func:`world_snapshot` is the god-view snapshot embedded in ``PublicSession``; channel views come
  from ``app.world.chat.channel_view`` (imported lazily, so this module never depends on chat at
  import time).

Nothing here ever includes request *answers*, thoughts or tool calls.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from typing import Any

from app.engine.expressions import seat_order
from app.world.state import RequestState, TicketState
from app.world.types import LedgerEntry


def seat_sorted(world: Any, ids: Iterable[str]) -> list[str]:
    """``ids`` (a subset of the players) ordered by seat; unknown ids are dropped."""
    wanted = set(ids)
    return [p.id for p in seat_order(list(world.state.players)) if p.id in wanted]


def _seconds(value: float | None) -> float | None:
    return None if value is None else round(max(0.0, float(value)), 1)


def message_view(entry: LedgerEntry) -> dict[str, Any]:
    """``MessageView`` of a ``message`` ledger entry (god view; ``visible_to_ids`` is kept so the
    front end can filter by spectator viewpoint).  Same shape as the ``message.committed`` event."""
    data = entry.data
    return {
        "seq": entry.seq,
        "ts": entry.ts,
        "channel_id": entry.channel_id,
        "from": entry.actor_id,
        "from_name": data.get("sender_name", entry.actor_id),
        "content": data.get("content", ""),
        "kind": data.get("kind", "speech"),
        "template_id": data.get("template_id"),
        "truncated": data.get("truncated"),
        "ticket_id": data.get("ticket_id"),
        "visible_to_ids": list(entry.visible_to_ids),
    }


def request_view(world: Any, request: RequestState) -> dict[str, Any]:
    """``RequestView`` (never contains answers; ``answered_by`` lists real answers in seat order)."""
    seconds_left = None
    if request.status == "open" and request.deadline is not None:
        seconds_left = _seconds(request.deadline - float(world.clock.now()))
    answered = [pid for pid, answer in request.answers.items() if not answer.defaulted]
    return {
        "id": request.id,
        "kind": request.kind,
        "question": request.question,
        "options": [{"id": o.get("id", ""), "label": o.get("label", "")} for o in request.options],
        "to": list(request.to),
        "answered_by": seat_sorted(world, answered),
        "status": request.status,
        "sealed": request.sealed,
        "seconds_left": seconds_left,
    }


def ticket_view(ticket: TicketState) -> dict[str, Any]:
    """``TicketView``."""
    return {
        "id": ticket.id,
        "player_id": ticket.player_id,
        "status": ticket.status,
        "verdict": ticket.verdict,
        "preview": ticket.preview,
        "opened_seq": ticket.opened_seq,
    }


def player_view_dict(world: Any, player: Any) -> dict[str, Any]:
    """God-view tile of one player: every tag (the front end filters by viewpoint)."""
    return {
        "id": player.id,
        "seat": player.seat,
        "name": player.name,
        "tags": list(player.tags),
        "out": bool(world.is_out(player)),
    }


def world_snapshot(world: Any, *, max_messages: int = 2000) -> dict[str, Any]:
    """God-view snapshot: ``{players, channels, messages, requests, tickets, dashboard, phase, status,
    winner, summary, seq, now, public_tags, ui}`` (contracts §2.8).

    ``messages`` holds the newest ``max_messages`` committed messages (oldest first).
    """
    chat = importlib.import_module("app.world.chat")
    state = world.state

    picked: list[LedgerEntry] = []
    if max_messages > 0:
        for entry in reversed(world.ledger):
            if entry.type == "message":
                picked.append(entry)
                if len(picked) >= max_messages:
                    break
        picked.reverse()

    return {
        "players": [player_view_dict(world, p) for p in state.players],
        "channels": [chat.channel_view(world, channel) for channel in state.channels],
        "messages": [message_view(e) for e in picked],
        "requests": [request_view(world, r) for r in state.requests.values()],
        "tickets": [ticket_view(t) for t in state.tickets.values()],
        "dashboard": {
            "factions": [dict(f) for f in state.dashboard.factions],
            "tag_stats": [dict(t) for t in state.dashboard.tag_stats],
        },
        "phase": state.phase,
        "status": state.status,
        "winner": state.winner,
        "summary": state.summary,
        "seq": world.seq,
        "now": round(float(world.clock.now()), 3),
        "public_tags": list(world.spec.setup.public_tags),
        "ui": world.spec.ui.model_dump(),
    }
