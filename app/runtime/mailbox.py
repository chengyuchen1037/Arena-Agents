"""The inbox of one agent's Actor.

``put`` never blocks (the supervisor's pump calls it synchronously) and ``get`` / ``wait`` are the
only suspension points of the Actor loop.  Envelopes are kept in three FIFO lanes; the *priority*
view (:meth:`get`, :meth:`drain`) hands out urgent envelopes before normal ones before ambient ones,
same-priority envelopes stay in arrival order.

The Actor itself consumes the inbox with :meth:`drain_arrival` (strict arrival order): a
``floor.revoked`` followed by a fresh ``floor.granted`` for the same channel must be seen in that
order, otherwise the revocation of the *old* floor would cancel the run started for the *new* one.
"""

from __future__ import annotations

import asyncio
from collections import deque

from app.world.types import PRIORITY_RANK, Envelope

_LANES = tuple(sorted(PRIORITY_RANK, key=PRIORITY_RANK.__getitem__))  # urgent, normal, ambient


class Mailbox:
    """Unbounded, priority-lane inbox with a non-blocking ``put``."""

    def __init__(self) -> None:
        self._lanes: dict[str, deque[tuple[int, Envelope]]] = {name: deque() for name in _LANES}
        self._counter = 0  # arrival number, to merge the lanes back into arrival order
        self._size = 0
        self._poked = False
        self._event = asyncio.Event()

    # ----------------------------------------------------------------- producing --
    def put(self, envelope: Envelope) -> None:
        """Add an envelope (never blocks, never raises for a valid envelope)."""
        self._counter += 1
        self._lanes[envelope.priority].append((self._counter, envelope))
        self._size += 1
        self._event.set()

    def poke(self) -> None:
        """Wake a pending :meth:`wait` / :meth:`get` caller without adding an envelope."""
        self._poked = True
        self._event.set()

    # ----------------------------------------------------------------- consuming --
    async def wait(self) -> None:
        """Return once there is at least one envelope (or :meth:`poke` was called); consumes nothing."""
        while self._size == 0 and not self._poked:
            self._event.clear()
            await self._event.wait()
        self._poked = False

    async def get(self) -> Envelope:
        """The most urgent envelope (oldest first within one priority); waits while empty."""
        while self._size == 0:
            self._event.clear()
            await self._event.wait()
        self._poked = False
        envelope = self.get_nowait()
        assert envelope is not None
        return envelope

    def get_nowait(self) -> Envelope | None:
        for name in _LANES:
            lane = self._lanes[name]
            if lane:
                _, envelope = lane.popleft()
                self._size -= 1
                return envelope
        return None

    def drain(self) -> list[Envelope]:
        """Everything pending, urgent first (arrival order within one priority); empties the inbox."""
        out: list[Envelope] = []
        for name in _LANES:
            lane = self._lanes[name]
            out.extend(envelope for _, envelope in lane)
            lane.clear()
        self._size = 0
        return out

    def drain_arrival(self) -> list[Envelope]:
        """Everything pending in strict arrival order; empties the inbox."""
        merged = sorted((item for lane in self._lanes.values() for item in lane), key=lambda item: item[0])
        for lane in self._lanes.values():
            lane.clear()
        self._size = 0
        return [envelope for _, envelope in merged]

    def clear(self) -> int:
        """Drop everything pending (and a pending :meth:`poke`); returns how many envelopes were dropped."""
        dropped = self._size
        for lane in self._lanes.values():
            lane.clear()
        self._size = 0
        self._poked = False
        return dropped

    # ---------------------------------------------------------------- inspecting --
    def __len__(self) -> int:
        return self._size

    def empty(self) -> bool:
        return self._size == 0

    def peek_all(self) -> list[Envelope]:
        """A copy of everything pending in arrival order (does not consume)."""
        merged = sorted((item for lane in self._lanes.values() for item in lane), key=lambda item: item[0])
        return [envelope for _, envelope in merged]
