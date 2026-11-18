"""Game clocks (decision D18: the game clock does not run while the game is paused).

The world measures every deadline in **game seconds** (``clock.now()``).  ``GameClock`` is the
production clock (based on :func:`time.monotonic`); ``ManualClock`` is the deterministic fake used
by tests, where time only moves when the test calls :meth:`ManualClock.advance`.

Both follow the same pause semantics, so a test can exercise "the timer does not run while paused"
without sleeping:

* a **paused** clock returns the same value from ``now()`` however much wall time passes;
* ``resume()`` continues from the frozen value, ``set(t)`` jumps to ``t`` (keeping the paused state).

A fresh :class:`GameClock` starts **paused** (a new / loaded game is frozen until the orchestrator
calls ``resume()`` when the game starts), a fresh :class:`ManualClock` starts running.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class GameClock:
    """Monotonic game clock in seconds that stops while paused."""

    def __init__(
        self,
        start: float = 0.0,
        *,
        paused: bool = True,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = time_source
        self._base = float(start)  # game seconds at ``_anchor`` (or the frozen value while paused)
        self._anchor = self._source()
        self._paused = bool(paused)

    # ------------------------------------------------------------------ reading --
    def now(self) -> float:
        """Current game time in seconds."""
        if self._paused:
            return self._base
        return self._base + max(0.0, self._source() - self._anchor)

    @property
    def paused(self) -> bool:
        return self._paused

    # ------------------------------------------------------------------ control --
    def pause(self) -> None:
        """Freeze the clock at its current value (idempotent)."""
        if self._paused:
            return
        self._base = self.now()
        self._paused = True

    def resume(self) -> None:
        """Let the clock run again from the frozen value (idempotent)."""
        if not self._paused:
            return
        self._anchor = self._source()
        self._paused = False

    def set(self, t: float) -> None:
        """Jump to ``t`` game seconds; the paused / running state is kept."""
        self._base = float(t)
        self._anchor = self._source()


class ManualClock(GameClock):
    """A clock that only moves when :meth:`advance` is called (tests).

    ``advance`` models *wall time passing*: while the clock is paused the game time stays frozen,
    exactly like the real :class:`GameClock`.  Use :meth:`set` to jump regardless.
    """

    def __init__(self, start: float = 0.0, *, paused: bool = False) -> None:
        self._wall = 0.0
        super().__init__(start, paused=paused, time_source=self._read_wall)

    def _read_wall(self) -> float:
        return self._wall

    def advance(self, dt: float) -> float:
        """Let ``dt`` seconds of wall time pass; returns the new game time."""
        if dt < 0:
            raise ValueError("a clock cannot go backwards")
        self._wall += float(dt)
        return self.now()
