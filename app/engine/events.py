"""SSE payload encoding and the per-subscriber queue used by ``broadcast_state``.

The wire format is unchanged: ``{"event": <name>, "session": {...public session...}}``.
What changed is the cost of producing it:

* a broadcast is encoded exactly once (pydantic's Rust JSON serializer, no intermediate
  ``model_dump`` dict) and the same string is handed to every subscriber;
* nothing is serialized when nobody is subscribed;
* every payload is a *full* snapshot, so when a slow subscriber's queue is full the oldest
  ordinary snapshot is dropped instead of letting snapshots pile up — but events that mark
  a state transition the UI must not miss (``game_ended``, ``error``, ...) are never evicted
  in favour of an ordinary snapshot.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

# Fields that must never leave the server (hidden information / bulky internals).
PUBLIC_SESSION_EXCLUDE = frozenset({"moderator_messages", "prompt_cache", "scenario_snapshot"})

# Events carrying a transition the client should always see, even on a congested queue.
IMPORTANT_EVENTS = frozenset(
    {
        "game_ended",
        "error",
        "new_game",
        "session_loaded",
        "identities_shuffled",
        "status_change",
        "config_updated",
        "execution_depth_updated",
        "bindings_updated",
        "autosave_error",
        "workspace_changed",
        "initial",
    }
)

SUBSCRIBER_QUEUE_SIZE = 32


class EventPayload(str):
    """A serialized SSE data string that remembers whether it is a must-deliver event."""

    important: bool = False


def encode_event(event: str, session: Any) -> EventPayload:
    """Serialize ``{"event": event, "session": <public session>}`` once."""
    body = session.model_dump_json(exclude=set(PUBLIC_SESSION_EXCLUDE))
    payload = EventPayload(
        '{"event":' + json.dumps(event, ensure_ascii=False) + ',"session":' + body + "}"
    )
    payload.important = event in IMPORTANT_EVENTS
    return payload


class SubscriberQueue(asyncio.Queue):
    """Bounded queue that sheds superseded state snapshots when it is full."""

    def offer(self, payload: EventPayload) -> None:
        if self.full():
            self._evict_one()
        self.put_nowait(payload)

    def _evict_one(self) -> None:
        """Drop the oldest ordinary snapshot; only if all are important, the oldest overall."""
        items = self._queue  # type: ignore[attr-defined]  # collections.deque
        for index, item in enumerate(items):
            if not getattr(item, "important", False):
                del items[index]
                return
        if items:
            items.popleft()


def offer_to(queue: asyncio.Queue, payload: EventPayload) -> None:
    """Deliver to any asyncio queue (plain queues keep the old drop-oldest behaviour)."""
    offer = getattr(queue, "offer", None)
    if offer is not None:
        offer(payload)
        return
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(payload)
