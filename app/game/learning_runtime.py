"""Initialization and the review after a game (v0.14, H19-H24, H31-H34), as a mixin of the orchestrator.

Mixed into :class:`~app.game.orchestrator.GameOrchestrator`.  The runs themselves are in :mod:`app.learning` (the loop, the ports over the bot
store, the runners, the job hub :class:`~app.learning.jobs.LearningHub`); this mixin is the thin layer between them and the game: the hub
(made on first use), the model port of a run, what the clients are shown (``review`` of the PublicSession, ``BotsPayload.init_job`` /
``learning_usage``) and what must not happen while a run is going on (a game is not replaced, saved over or continued under a review).
"""

from __future__ import annotations

import logging
from typing import Any

from app.bots.types import BOT_ID_RE, REVIEW_TIMEOUT_S
from app.learning.jobs import LearningHub, LearningRefused

logger = logging.getLogger(__name__)

__all__ = ["LearningBusyError", "LearningRefused", "LearningRuntimeMixin"]


class LearningBusyError(LearningRefused):
    """A learning run is in the way (a review is being written: the game it is about must not be replaced or continued meanwhile)."""

    status = 409
    code = "learning_busy"


class LearningRuntimeMixin:
    # Class-level defaults so partially initialized instances (tests) stay valid.
    _learning_hub: LearningHub | None = None

    # ------------------------------------------------------------------------------------------------ the hub
    def learning(self) -> LearningHub:
        """The learning jobs of this orchestrator (made on first use)."""
        hub = self._learning_hub
        if hub is None:
            hub = self._learning_hub = LearningHub(self)
        return hub

    async def start_init(self, ids: list[str] | None = None, rebuild: bool = False) -> dict[str, Any]:
        """``POST /api/bots/init``: see :meth:`LearningHub.start_init` (:class:`LearningRefused` says why it cannot be started)."""
        return await self.learning().start_init(ids, rebuild)

    async def cancel_init(self) -> bool:
        """``POST /api/bots/init:cancel``: ``False`` when no initialization was running."""
        hub = self._learning_hub
        return await hub.cancel_init() if hub is not None else False

    async def start_review(self, ids: list[str] | None = None, learn_style: dict[str, bool] | None = None) -> dict[str, Any]:
        """``POST /api/game/review``: see :meth:`LearningHub.start_review`."""
        return await self.learning().start_review(ids, learn_style)

    async def _learning_shutdown(self) -> None:
        hub = self._learning_hub
        if hub is not None:
            await hub.shutdown()

    def _learning_data_dir_replaced(self) -> None:
        hub = self._learning_hub
        if hub is not None:
            hub.forget()

    # ------------------------------------------------------------------------------------------------ the model of a run
    def _learning_binding_for(self, agent_id: str) -> Any:
        """The model binding of a learning run: an initialization is asked as the bot (its own default binding, H20), a review as the seat (the
        binding of the seat in the game that was reviewed)."""
        if BOT_ID_RE.match(agent_id or ""):
            return self._bot_binding(agent_id)  # type: ignore[attr-defined]
        return self.session.get_model_binding(agent_id)  # type: ignore[attr-defined]

    def learning_llm(self) -> Any:
        """The model port of one learning job: the demo's scripted bots (or a test's fake) when a ``llm_port_factory`` is set, else the engine's
        client behind the same per-actor bindings the game uses - under the concurrency gate of ``config.yml``.  Every call is a line of the error
        log (``purpose`` ``init`` for an initialization, ``review`` for a review)."""
        from app.game.observe_runtime import oneshot_label
        from app.runtime.llm_gate import GatedEngineLlm, LlmGate, LoggedLlm

        call_log = self.call_log()  # type: ignore[attr-defined]
        factory = self.llm_port_factory  # type: ignore[attr-defined]
        if factory is not None:
            return LoggedLlm(factory(self), call_log, self._learning_binding_for, oneshot_label)
        rt = self.cfg.get("runtime") or {}  # type: ignore[attr-defined]
        return GatedEngineLlm(
            self.llm,  # type: ignore[attr-defined]
            self._learning_binding_for,
            LlmGate(int(rt.get("llm_concurrency", 0)), int(rt.get("llm_concurrency_per_provider", 0))),
            float(rt.get("llm_timeout_seconds", 120)),
            call_log=call_log,
            label=oneshot_label,
        )

    # ------------------------------------------------------------------------------------------------ what is running
    def init_running_ids(self) -> set[str]:
        """The bots whose initialization is being run now."""
        hub = self._learning_hub
        return hub.init_running_ids() if hub is not None else set()

    def init_busy_ids(self) -> set[str]:
        """The bots that must not be changed right now (the API of the bots refuses to): the ones being initialized."""
        return self.init_running_ids()

    def review_running_ids(self) -> set[str]:
        """The seats whose review is being run now."""
        hub = self._learning_hub
        return hub.review_running_ids() if hub is not None else set()

    def init_job_view(self) -> dict[str, Any] | None:
        """``BotsPayload.init_job``: ``{"job_id", "state", "ids", "started_at", "deadline_s"}``, or ``None``."""
        hub = self._learning_hub
        return hub.init_job_view() if hub is not None else None

    def learning_usage(self) -> dict[str, Any]:
        """``BotsPayload.learning_usage``: what the initializations have cost so far (``<data>/bots/usage.json``, H24)."""
        if not self._table_io_allowed():  # type: ignore[attr-defined]
            return {"calls": 0, "tokens": 0, "cost_usd": 0.0}
        return self.learning().usage_view()

    # ------------------------------------------------------------------------------------------------ the review of the live game
    def review_view(self) -> dict[str, Any]:
        """``PublicSession.review`` (§13.8): ``{"available", "done", "running", "supported", "deadline_s", "seconds_left"}``.

        ``available``: the game is over and the review can be asked for; ``supported``: the game has bots (a v5 game cannot be reviewed,
        H58); ``done``: the seats that were reviewed (once per seat and game); ``seconds_left``: the longest time a running review has
        left, else ``None``."""
        session = self.session  # type: ignore[attr-defined]
        hub = self._learning_hub
        running = self.review_running_ids()
        return {
            "available": session.status == "finished",
            "done": list(session.review_done),
            "running": bool(running),
            "supported": not session.legacy,
            "deadline_s": REVIEW_TIMEOUT_S,
            "seconds_left": hub.review_seconds_left() if hub is not None and running else None,
        }

    def _refuse_while_learning(self, what: str) -> None:
        """A review is being written (at most a minute): the game it is about is not to be replaced, saved over or continued meanwhile."""
        if self.review_running_ids():
            raise LearningBusyError(f"赛后复盘正在进行（最多 {REVIEW_TIMEOUT_S} 秒），请等它结束后再{what}。")
