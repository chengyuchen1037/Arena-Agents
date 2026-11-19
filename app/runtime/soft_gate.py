"""The soft-pause gate: lets the work in flight finish, then holds every agent (v0.13.1, G2-G5).

A *hard* pause stops the supervisor and cancels the calls in flight (their answers are thrown away).  A *soft* pause
cancels nothing: it only closes this gate.  Every agent asks the gate before it starts a model call (the top of a
round, see :meth:`app.runtime.agent.Agent._run_rounds`); while it is closed the agent waits there - state ``held`` -
and a call that is already in flight is allowed to finish: its answer is written, its batch of tool calls is carried
out (that part is synchronous, it can never be half done) and the agent then stops at the gate for its next round.

Two phases, as the user sees them:

* ``draining`` - the gate is closed and ``in_flight`` model calls have not come back yet;
* ``held``     - the gate is closed and no call is in flight any more: nothing new will start until it opens.

The gate knows nothing about games, seats or the clock: freezing the game clock and telling the clients is the
orchestrator's job (:mod:`app.game.softpause_runtime`).  ``on_change(gate)`` is called (on the event loop, one loop turn
after the cause, at most once per turn) when ``in_flight`` or the phase changed while the gate is closed - never for the
closing and opening themselves, whose caller announces them.  The one-turn delay is on purpose: it lets whatever the
last answer produced (messages, events) reach the clients before they are told that everybody is held.

Waiting at the gate is an ordinary ``await``: cancelling the task (``Agent.stop()``) ends the wait at once.  (An interrupt
does *not* cancel anything while the gate is closed: the agent keeps its run and the interrupt is merged after the pause.)

Who counts a call as *in flight*: an agent counts it from just before it awaits its model (:meth:`SoftGate.call_started`) -
unless the model port counts for itself, which is what a port with a concurrency limit does
(:class:`app.runtime.llm_gate.GatedEngineLlm`, given this gate).  Such a port counts a call only from the moment its slot is
granted and the request is really about to be sent; a call that is merely *queued* for a slot is not in flight, and when the
slot finally comes while the gate is closed nothing is sent: the port raises :class:`GateClosed`, the agent goes back to
wait at the gate (state ``held``) and asks again after the pause.  So a soft pause never lets a request leave after the
click, however many calls were queued.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["GateClosed", "SoftGate"]


class GateClosed(Exception):
    """A model port that queues calls (a concurrency limit) found the soft-pause gate closed when its call finally got a
    slot: nothing was sent and the slot is free again.  The agent that made the call goes back to wait at the gate."""


class SoftGate:
    """One gate per running game (the orchestrator builds a new one every time the agents are started)."""

    def __init__(self, on_change: Callable[["SoftGate"], None] | None = None) -> None:
        self._on_change = on_change
        self._closed = False
        self._in_flight = 0  # model calls that were started and have not come back (or were dropped) yet
        self._held = 0  # agents waiting at the gate right now
        self._open_event = asyncio.Event()
        self._open_event.set()
        self._flush_handle: asyncio.Handle | None = None
        self._announced: dict[str, Any] | None = None  # what ``on_change`` last heard (or the closing announced)

    # ------------------------------------------------------------------ reading --
    @property
    def closed(self) -> bool:
        """The soft pause is on: no agent starts a model call."""
        return self._closed

    @property
    def in_flight(self) -> int:
        """How many model calls have not come back yet."""
        return self._in_flight

    @property
    def held(self) -> int:
        """How many agents wait at the gate right now."""
        return self._held

    @property
    def state(self) -> str | None:
        """``None`` (open), ``"draining"`` (closed, calls still in flight) or ``"held"`` (closed, nothing in flight)."""
        if not self._closed:
            return None
        return "draining" if self._in_flight > 0 else "held"

    def payload(self) -> dict[str, Any] | None:
        """``PublicSession.soft_pause``: ``None`` or ``{"state": "draining" | "held", "in_flight": n}``."""
        state = self.state
        return None if state is None else {"state": state, "in_flight": self._in_flight}

    # ------------------------------------------------------------------ control --
    def close(self) -> None:
        """Hold every agent before its next model call (idempotent)."""
        if self._closed:
            return
        self._closed = True
        self._open_event.clear()
        self._announced = self.payload()  # the caller announces the closing; only what follows is a "change"

    def open(self) -> None:
        """Let every held agent go (idempotent)."""
        if not self._closed:
            return
        self._closed = False
        self._open_event.set()
        self._announced = None
        self._cancel_flush()

    def dispose(self) -> None:
        """The game the gate belonged to is over: let everybody go and stop talking (idempotent)."""
        self._on_change = None
        self.open()
        self._cancel_flush()

    # ------------------------------------------------------------- the agents' side --
    def call_started(self) -> None:
        """A model call begins: an agent is about to await its model - or, for a port with a concurrency limit, the slot
        was granted and the request goes out now.  (A closed gate lets nothing start: nothing to announce.)"""
        self._in_flight += 1

    def call_finished(self) -> None:
        """A model call came back, failed, or was dropped (cancelled run)."""
        if self._in_flight > 0:
            self._in_flight -= 1
        self._schedule_flush()

    async def wait_open(self) -> None:
        """Wait while the gate is closed (returns at once when it is open)."""
        self._held += 1
        try:
            while self._closed:
                await self._open_event.wait()
        finally:
            self._held -= 1

    # --------------------------------------------------------------------- change --
    def _schedule_flush(self) -> None:
        if not self._closed or self._on_change is None or self._flush_handle is not None:
            return
        try:
            self._flush_handle = asyncio.get_running_loop().call_soon(self._flush)
        except RuntimeError:  # no running loop (the loop is closing): nobody is listening any more
            self._flush_handle = None

    def _cancel_flush(self) -> None:
        handle, self._flush_handle = self._flush_handle, None
        if handle is not None:
            handle.cancel()

    def _flush(self) -> None:
        self._flush_handle = None
        callback = self._on_change
        if not self._closed or callback is None:
            return
        view = self.payload()
        if view == self._announced:
            return
        self._announced = view
        try:
            callback(self)
        except Exception:  # noqa: BLE001 - a broken listener must not break an agent
            logger.exception("soft-pause change listener failed")
