"""Spectator event bus behind the SSE v2 endpoint (docs/v0.12-contracts.md §8).

Every :class:`~app.world.types.UiEvent` published here gets a monotonically increasing id and is kept
in a ring buffer, so a browser that reconnects with ``Last-Event-ID`` is *resumed* instead of being
sent a whole snapshot.  Each subscriber owns a bounded queue; a slow subscriber never blocks the game:
when its queue is full the oldest **ordinary** event (high-frequency, self-superseding ones such as
``agent.status``) is dropped first, only if the queue holds nothing but important events the oldest
event overall goes - and the queue is flagged (``lost_important``) so the endpoint can resync the
client with a fresh snapshot.

Everything is synchronous and runs on the event loop thread.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Collection
from typing import Any

from app.world.types import UiEvent

#: Event types that are safe to drop under back-pressure: each one is superseded by the next of its kind
#: (``llm.call``, v0.13.1: one line of the error log - a client that missed some asks ``GET /api/llm/calls?after=``).
ORDINARY_TYPES = frozenset({"agent.status", "agent.metrics", "request.progress", "budget.updated", "llm.call"})

#: Event types only a *trusted* client may be sent (v0.13.1).  An ``llm.call`` line says which model answered a call, how
#: many tokens it took and what an error said - what ``GET /api/llm/calls`` gives to trusted clients only
#: (``require_trusted_client``: a token, or a loopback peer).  The SSE endpoint subscribes every other client with
#: ``exclude=TRUSTED_ONLY_TYPES``: it gets no such event, neither live nor in a replay.
TRUSTED_ONLY_TYPES = frozenset({"llm.call"})

# v0.12.1: every message wakes every agent, so ``agent.status`` / ``agent.metrics`` events are far more
# frequent than before - the ring holds more of them so a short disconnect can still be resumed.
DEFAULT_RING = 5000
DEFAULT_QUEUE_SIZE = 1024


class BusEvent:
    """One published event: ``{"id", "type", "data"}`` plus its lazily built wire encodings."""

    __slots__ = ("id", "type", "data", "_json")

    def __init__(self, id: int, type: str, data: dict[str, Any]) -> None:  # noqa: A002
        self.id = id
        self.type = type
        self.data = data
        self._json: str | None = None

    def json(self) -> str:
        """The compact JSON body ``{"id":N,"type":"...","data":{...}}`` (encoded once)."""
        if self._json is None:
            self._json = json.dumps(
                {"id": self.id, "type": self.type, "data": self.data},
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        return self._json

    def sse(self) -> str:
        """The event as one Server-Sent-Events message (no ``event:`` field, see contracts §8.2)."""
        return f"id: {self.id}\ndata: {self.json()}\n\n"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"BusEvent(id={self.id}, type={self.type!r})"


class SubscriberQueue(asyncio.Queue):
    """A bounded per-subscriber queue that sheds the least valuable event when it is full.

    ``exclude`` = event types this subscriber is never told about: they are not queued at all (so they cannot crowd
    anything out of the queue either)."""

    def __init__(self, maxsize: int = DEFAULT_QUEUE_SIZE, exclude: Collection[str] = ()) -> None:
        super().__init__(maxsize=max(1, int(maxsize)))
        self.exclude: frozenset[str] = frozenset(exclude)
        self.dropped = 0  # events shed because the subscriber was too slow
        self.lost_important = False  # an event that is *not* self-superseding was shed

    def offer(self, event: BusEvent) -> None:
        if event.type in self.exclude:
            return
        if self.full():
            self._evict_one()
        self.put_nowait(event)

    def _evict_one(self) -> None:
        items: deque[BusEvent] = self._queue  # type: ignore[attr-defined]  # CPython's deque
        for index, item in enumerate(items):
            if item.type in ORDINARY_TYPES:
                del items[index]
                self.dropped += 1
                return
        if items:
            items.popleft()
            self.dropped += 1
            self.lost_important = True


class EventBus:
    """Ring buffer + fan-out; implements :class:`app.runtime.ports.EventSink`."""

    def __init__(self, ring: int = DEFAULT_RING, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self.ring_size = max(1, int(ring))
        self.queue_size = max(1, int(queue_size))
        self._ring: deque[BusEvent] = deque(maxlen=self.ring_size)
        self._last_id = 0
        self._subscribers: set[SubscriberQueue] = set()

    # ------------------------------------------------------------------ publishing --
    @property
    def last_id(self) -> int:
        """Id of the newest published event (0 before the first one)."""
        return self._last_id

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: UiEvent) -> int:
        """Store and fan out one event; returns its id.  Never raises, never blocks."""
        self._last_id += 1
        published = BusEvent(self._last_id, event.type, event.data)
        self._ring.append(published)
        for queue in tuple(self._subscribers):
            queue.offer(published)
        return published.id

    # ------------------------------------------------------------------ subscribing --
    def subscribe(
        self, last_event_id: int | str | None = None, *, exclude: Collection[str] = ()
    ) -> tuple[list[BusEvent] | None, SubscriberQueue]:
        """Register a subscriber.

        Returns ``(replay, queue)``.  ``replay`` is the list of events after ``last_event_id`` (possibly
        empty) when the ring still holds all of them - the client is resumed seamlessly - or ``None``
        when it cannot be resumed (first connection, or the gap is too large, or the id is unknown):
        the endpoint must then send a full snapshot first.  Events published from now on arrive on
        ``queue``; no event is both in ``replay`` and in ``queue`` (this method does not await).

        ``exclude``: event types this subscriber must not get (:data:`TRUSTED_ONLY_TYPES` for a client that is not
        trusted) - neither in the replay nor on the queue.  The ring still holds them for everybody else; the ids the
        subscriber sees simply skip them, which a resume with ``Last-Event-ID`` does not mind.
        """
        blocked = frozenset(exclude)
        queue = SubscriberQueue(self.queue_size, exclude=blocked)
        self._subscribers.add(queue)
        return self._replay_after(last_event_id, blocked), queue

    def unsubscribe(self, queue: SubscriberQueue) -> None:
        self._subscribers.discard(queue)

    def _replay_after(
        self, last_event_id: int | str | None, exclude: frozenset[str] = frozenset()
    ) -> list[BusEvent] | None:
        if last_event_id is None:
            return None
        try:
            last = int(last_event_id)
        except (TypeError, ValueError):
            return None
        if last == self._last_id:
            return []
        if last < 0 or last > self._last_id:
            return None  # an id from a previous server run
        oldest = self._ring[0].id if self._ring else self._last_id + 1
        if last + 1 < oldest:
            return None  # the ring no longer holds the whole gap
        return [event for event in self._ring if event.id > last and event.type not in exclude]
