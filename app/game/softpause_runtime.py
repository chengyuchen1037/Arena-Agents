"""Soft pause: let the work in flight finish, then hold every agent (v0.13.1, docs/v0.13.1-plan.md G1-G9).

Mixed into :class:`~app.game.orchestrator.GameOrchestrator`.  Contract: docs/v0.12-contracts.md section 12.2.

While a person plays a seat they may want to think.  A *soft* pause lets them - without cancelling a single call:

* the game stays ``running`` and the commit gate stays open: the person can still read, type, speak, pass, answer and
  use abilities (G4) - what they do is delivered to the agents as usual, and the agents merge it into ONE message
  when they are let go;
* the *gate* of the runtime (:class:`~app.runtime.soft_gate.SoftGate`) is closed: no agent - the moderator included -
  starts a new model call.  A call in flight finishes, its answer is written and its tools are carried out (G2); the
  agent then waits in front of its next call (agent state ``held``);
* the *game clock* stops at the moment of the click (G2): the person's deadlines, every other deadline and every
  ``sleep`` alarm stand still, so nobody times out meanwhile;
* the watchdog stays quiet (G8) and nobody is told (G9, F6): the agents only find that their next thought comes late,
  and since the clock stood still they cannot even tell how late.

It is a volatile flag of the live runtime, never saved and never a game status (G6): a hard pause, the end of the game,
the budget limit, ``hold_game``, a new / loaded / imported game and a shutdown all clear it (they all go through
``GameOrchestrator._halt``), and a restart comes back hard-paused like every restart does.

Two phases are shown (``PublicSession.soft_pause``): ``draining`` - ``in_flight`` answers are still on their way - and
``held`` - everybody has stopped.  ``clock_running`` says whether the game clock runs (the page stops its local
countdown on it, because ``status`` stays ``running``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.game.session import GameSession
from app.runtime.soft_gate import SoftGate

logger = logging.getLogger(__name__)


class SoftPauseError(RuntimeError):
    """The soft pause (or its release) is refused; ``status`` is the HTTP status the API answers with (409)."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


class SoftPauseRuntimeMixin:
    """The soft-pause state of the orchestrator (a volatile flag, never saved: a restart comes back hard-paused)."""

    # Class-level default keeps partially initialized instances (tests) valid.
    _soft_gate: SoftGate | None = None

    # ------------------------------------------------------------------------ the state ---
    def soft_pause_payload(self) -> dict[str, Any] | None:
        """``PublicSession.soft_pause``: ``None`` or ``{"state": "draining" | "held", "in_flight": n}`` (§12.2)."""
        gate = self._soft_gate
        if gate is None or not gate.closed:
            return None
        return gate.payload()

    def clock_running(self) -> bool:
        """``PublicSession.clock_running``: the game clock runs - the game is ``running`` and not soft-paused (§12.2).
        (``status`` stays ``running`` during a soft pause; the page counts down only while this is true.)"""
        return self.session.status == "running" and self.soft_pause_payload() is None  # type: ignore[attr-defined]

    # ------------------------------------------------------------------- the runtime's gate ---
    def _new_soft_gate(self) -> SoftGate:
        """The gate of the runtime that is being built (``_build_runtime``): open, one per start / resume."""
        gate = SoftGate(on_change=self._on_soft_gate_change)
        self._soft_gate = gate
        return gate

    def _on_soft_gate_change(self, gate: SoftGate) -> None:
        """Answers came back (``draining`` -> fewer in flight -> ``held``): tell the clients.  The gate calls this one
        loop turn after the cause; going through a task adds one more, so the messages and events the last answer
        produced are published before "everybody is held"."""
        if gate is not self._soft_gate or not gate.closed:
            return  # (the runtime this gate belonged to is gone)
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # not on an event loop: nobody is listening
            return
        self._spawn(self.broadcast_state("soft_pause"))  # type: ignore[attr-defined]

    def _clear_soft_pause(self) -> None:
        """Forget the soft pause: the runtime it belonged to is gone (a hard pause, the end of the game, the budget,
        ``hold_game``, a new / loaded / imported game, a shutdown - everything that stops the agents).  The clock is
        the caller's business (``_halt`` freezes it anyway)."""
        gate, self._soft_gate = self._soft_gate, None
        if gate is not None:
            gate.dispose()

    # ------------------------------------------------------------------------ the control ---
    # Atomicity of the two controls below: neither ever *yields to the event loop* between its checks and its effects -
    # ``broadcast_state`` is a coroutine only by declaration (it publishes and returns, it awaits nothing), so ``await``-ing
    # it does not suspend.  That is why they need no ``_control()`` lock (the one that serialises start / resume, the re-deal
    # and taking a seat over / giving it back): nothing can slip in between "the game runs and a seat is played" and "the
    # gate is closed", and a hard pause that is under way has already dropped ``_supervisor`` (so the checks refuse).  If
    # ``broadcast_state`` ever gets a real ``await`` this must be revisited (take ``_control()``, re-check after it).
    async def soft_pause(self) -> GameSession:
        """Soft-pause the running game (G1-G3).  Only while the game runs *and* a seat is taken over - without a person
        there is nobody to give time to.  Already soft-paused: nothing happens.  Raises :class:`SoftPauseError`."""
        session = self.session  # type: ignore[attr-defined]
        gate = self._soft_gate
        if session.status != "running" or self._supervisor is None or gate is None:  # type: ignore[attr-defined]
            raise SoftPauseError("对局没有在运行。")
        if self.takeover_seat is None:  # type: ignore[attr-defined]
            raise SoftPauseError("只有接管座位时才能软暂停。")
        if gate.closed:
            return session
        self.clock.pause()  # type: ignore[attr-defined]  # every deadline and alarm stands still from this moment on
        gate.close()
        logger.info("Soft pause: %d model call(s) in flight", gate.in_flight)
        await self.broadcast_state("soft_pause")  # type: ignore[attr-defined]  # carries the frozen clock
        return session

    async def soft_resume(self) -> GameSession:
        """Let the agents go on (G5): the clock runs again, the gate opens, and every held agent first merges what
        arrived meanwhile into one message and then calls its model.  Also cancels a soft pause that is still
        draining.  Raises :class:`SoftPauseError` when there is none."""
        gate = self._soft_gate
        if gate is None or not gate.closed or self._supervisor is None:  # type: ignore[attr-defined]
            raise SoftPauseError("没有处于软暂停。")
        self.clock.resume()  # type: ignore[attr-defined]  # (before the gate: the first thing an agent sees is a running clock)
        gate.open()
        logger.info("Soft pause ended")
        await self.broadcast_state("soft_resume")  # type: ignore[attr-defined]
        return self.session  # type: ignore[attr-defined]
