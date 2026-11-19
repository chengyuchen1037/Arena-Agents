"""What the user can look at afterwards: the call / error log and the thinking record (v0.13.1, G21-G26, G32-G36).

Mixed into :class:`~app.game.orchestrator.GameOrchestrator`.  Contract: docs/v0.12-contracts.md sections 12.3, 12.4.

**The error log** (``GET /api/llm/calls``, the ``llm.call`` event).  :class:`~app.runtime.call_log.CallLog` is a ring of
the last 2000 lines of the live game.  It belongs to a game (``session_id``) and is made lazily: the first time anybody asks
for it while the live game is another one than the log's - a new game, a game loaded from the history, an import, a restart -
a new log is made and filled from that game's trace log (``logs/<id>/trace-*.jsonl``, read from its end).  The runtime hands it
to the model port (:meth:`GameOrchestrator._build_runtime`), the agents' trace sink (:meth:`_observe_trace`) and the one-shot
calls (review, "complete it for me": ``_oneshot_port``); an ``error`` event the orchestrator publishes lands in it too
(:meth:`_log_error_event`).  Lines are announced as ``llm.call`` events through ``_publish`` (an ordinary, droppable event).

**The thinking record** (``GET /api/agents/{id}/thoughts``): the ``thought`` / ``content`` events of one agent in the same
trace, newest page first (:func:`app.runtime.trace_log.read_thoughts`).  While somebody plays a seat the thoughts of the
others are closed (409) until the game is over (G25); thoughts never go through the event bus (G26).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.runtime.call_log import CallLog, LoggingTrace
from app.runtime.trace_log import TraceLog, read_thoughts, trace_dir
from app.world.types import MODERATOR_ID, UiEvent

logger = logging.getLogger(__name__)

#: The actor of a model call that is nobody's turn: "complete it for me" (the usage book uses the same id).
ROSTER_ACTOR_ID = "roster"
ROSTER_ACTOR_NAME = "名册"


class ObserveError(RuntimeError):
    """A refusal of the observe endpoints; ``status`` is the HTTP status the API answers with."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def oneshot_label(agent_id: str) -> tuple[str, str]:
    """``(purpose, actor_id)`` of a one-shot model call for ``agent_id``: the moderator's model completing a text is a
    ``refine`` (nobody's turn: the actor is the roster), a player's own model writing the review of a game is a ``review``."""
    if agent_id == MODERATOR_ID:
        return "refine", ROSTER_ACTOR_ID
    return "review", agent_id


class ObserveRuntimeMixin:
    """The call log of the current game (in memory; rebuilt from the game's trace log when a game is loaded)."""

    # Class-level defaults so partially initialized instances (tests) stay valid.
    _call_log_obj: CallLog | None = None
    _call_log_generation: int = -1

    # ------------------------------------------------------------------------------ the error log ---
    def call_log(self) -> CallLog:
        """The call log of the live game.

        Made on first use, and made again - from that game's trace log - as soon as the live game is another one than
        the log's (``session_id``): that is how a new game starts with an empty log and a loaded one with its own.  A game
        that is installed again under the same id (an import that replaced its files) counts as another one: the
        orchestrator bumps ``_session_generation`` whenever it installs a game."""
        session_id = self.session.session_id  # type: ignore[attr-defined]
        generation = int(getattr(self, "_session_generation", 0))
        log = self._call_log_obj
        if log is None or log.session_id != session_id or generation != self._call_log_generation:
            log = self._make_call_log(session_id)
            self._call_log_obj = log
            self._call_log_generation = generation
        return log

    def _make_call_log(self, session_id: str) -> CallLog:
        log = CallLog(
            session_id,
            clock=self._call_log_clock,
            names=self._call_log_name,
            make_trace=lambda: self._make_call_trace(session_id),
        )
        log.publish = lambda event, log=log: self._publish_call(log, event)
        try:
            found = log.load_from_trace(trace_dir(self._logs_root(), session_id))  # type: ignore[attr-defined]
            if found:
                logger.info("Rebuilt the call log of game %s from its trace: %d lines", session_id, found)
        except Exception:  # noqa: BLE001 - a log that cannot be rebuilt starts empty: never a reason to fail a request
            logger.warning("Could not rebuild the call log of game %s from its trace", session_id, exc_info=True)
        return log

    def _call_log_clock(self) -> float | None:
        return float(self.clock.now())  # type: ignore[attr-defined]

    def _call_log_name(self, actor_id: str) -> str:
        if actor_id == ROSTER_ACTOR_ID:
            return ROSTER_ACTOR_NAME
        return str(self.world.display_name(actor_id))  # type: ignore[attr-defined]

    def _make_call_trace(self, session_id: str) -> TraceLog:
        """A trace log of ``session_id`` for the lines the log writes while no game runs (a review, "complete it for
        me"): the same files, and the same promise that no API key reaches them."""
        trace = TraceLog(self._logs_root(), session_id)  # type: ignore[attr-defined]
        self._register_trace_secrets(trace)  # type: ignore[attr-defined]
        return trace

    def _publish_call(self, log: CallLog, event: UiEvent) -> None:
        """Announce a line of ``log`` (``llm.call``) - unless the log is the one of a game that is not live any more."""
        if log is self._call_log_obj:
            self._publish(event)  # type: ignore[attr-defined]

    def _observe_trace(self, trace: TraceLog) -> LoggingTrace:
        """The sink the agents of a starting game write their trace to: ``trace`` itself, and every ``error`` event
        also becomes a line of the call log (a failed call is written into ``trace`` by the log itself)."""
        log = self.call_log()
        log.attach_trace(trace)
        return LoggingTrace(trace, log)

    def _log_error_event(self, event: UiEvent) -> None:
        """An ``error`` event the orchestrator publishes (``_publish``).  One an agent raised (``agent_id``: it gave its
        action up) is a line already - its trace says so; the rest (a timer pass of the world that failed) is added."""
        data = event.data or {}
        if data.get("agent_id"):
            return
        try:
            self.call_log().note_runtime_error(str(data.get("message") or "运行出错。"))
        except Exception:  # noqa: BLE001 - the log must never break the event loop
            logger.debug("could not log an error event", exc_info=True)

    def call_log_payload(self, *, after: int = 0, limit: int = 500, errors_only: bool = False) -> dict[str, Any]:
        """``GET /api/llm/calls`` (contract 12.3)."""
        return self.call_log().view(after=after, limit=limit, errors_only=errors_only)

    # ------------------------------------------------------------------------- the thinking record ---
    def _thoughts_guard(self, agent_id: str) -> str:
        """The name of ``agent_id`` when its thinking record may be read now; :class:`ObserveError` otherwise (404 for an
        id that is not an agent of this game; 409 while somebody plays another seat and the game is not over, G25)."""
        session = self.session  # type: ignore[attr-defined]
        names = {p.id: p.name for p in session.participants}
        if agent_id == MODERATOR_ID:
            name = "主持人"
        elif agent_id in names:
            name = names[agent_id]
        else:
            raise ObserveError("没有这个玩家。", 404)
        seat = self.takeover_seat  # type: ignore[attr-defined]
        if seat is not None and session.status != "finished" and agent_id != seat:
            raise ObserveError("接管期间不能查看其他玩家的思考记录，对局结束后开放。", 409)
        return name

    async def agent_thoughts(self, agent_id: str, before: str | None = None, limit: int = 50) -> dict[str, Any]:
        """``GET /api/agents/{id}/thoughts`` (contract 12.4): one page of what the agent thought, newest page first.

        The file reading runs in a worker thread (a trace can be 20 MB per file); nothing is cached."""
        name = self._thoughts_guard(agent_id)
        directory = trace_dir(self._logs_root(), self.session.session_id)  # type: ignore[attr-defined]
        try:
            page = await asyncio.to_thread(read_thoughts, directory, agent_id, before=before, limit=limit)
        except ValueError as exc:
            raise ObserveError("游标无效，请重新加载思考记录。", 400) from exc
        return {"agent_id": agent_id, "name": name, **page}
