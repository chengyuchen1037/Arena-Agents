"""The user's spending limit of the live game (v0.12.1, E10 / E22), as a mixin of the orchestrator.

Mixed into :class:`~app.game.orchestrator.GameOrchestrator`.  The limit is checked after every accounted LLM call and may pause the
game; it is kept in the session (``session.budget``) and remembered as the default of the next games.  (Until v0.13.1 this lived
in ``roster_runtime`` together with the roster, which v0.14 replaced by the bots.)
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.game import budget as budget_mod
from app.world.types import UiEvent

logger = logging.getLogger(__name__)

BUDGET_PUSH_INTERVAL_S = 1.0


class BudgetRuntimeMixin:
    # Class-level defaults so partially initialized instances (tests) stay valid.
    _budget_watch: budget_mod.BudgetWatch | None = None
    _budget_pushed_at: float = 0.0

    def budget_settings(self) -> budget_mod.BudgetSettings:
        return self.session.budget  # type: ignore[attr-defined]

    def budget_payload(self) -> dict[str, Any]:
        usage = self.usage  # type: ignore[attr-defined]
        return budget_mod.public_view(self.budget_settings(), usage.total_tokens, usage.total_cost_usd)

    def _load_budget_defaults(self) -> budget_mod.BudgetSettings:
        try:
            raw = self._settings().get_budget_defaults()  # type: ignore[attr-defined]
            return budget_mod.BudgetSettings.model_validate(raw or {})
        except Exception:  # noqa: BLE001 - a bad default must not block a new game
            logger.warning("Could not load the default budget", exc_info=True)
            return budget_mod.BudgetSettings()

    async def set_budget(self, values: dict[str, Any], *, save_default: bool = True) -> dict[str, Any]:
        """Replace the limit of this game (and remember it as the default of the next ones)."""
        settings = budget_mod.BudgetSettings.model_validate(values)
        self.session.budget = settings  # type: ignore[attr-defined]
        if save_default:
            try:
                self._settings().save_budget_defaults(settings.model_dump(mode="json"))  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - the limit itself applies even when the default could not be saved
                logger.warning("Could not save the default budget", exc_info=True)
        self._budget_watch = budget_mod.BudgetWatch()
        self._budget_pushed_at = 0.0
        self._on_budget_changed(force=True)
        await self.broadcast_state("budget_updated")  # type: ignore[attr-defined]
        return self.budget_payload()

    def _refuse_when_over_budget(self) -> None:
        """Starting a game that already used up a *stopping* limit would pause it again at once: say why."""
        settings = self.budget_settings()
        if not settings.force_stop:
            return
        usage = self.usage  # type: ignore[attr-defined]
        if budget_mod.state_of(settings, usage.total_tokens, usage.total_cost_usd) == budget_mod.REACHED:
            raise RuntimeError(
                "本局已达到你设置的预算上限，不能继续：请提高上限、清除上限，或取消“达到上限后强制停止”后再继续。"
            )

    def _attach_budget(self) -> None:
        """Hook the (new) usage book of the live game into the limit check."""
        self._budget_watch = budget_mod.BudgetWatch()
        self._budget_pushed_at = 0.0
        self.usage.on_record = self._on_usage_recorded  # type: ignore[attr-defined]

    def _on_usage_recorded(self, agent_id: str, resp: Any) -> None:
        self._on_budget_changed(force=False)
        supervisor = getattr(self, "_supervisor", None)
        if supervisor is None or agent_id not in supervisor.agents:
            # Nobody publishes this actor's numbers (the post-game review, "complete it with the moderator's
            # model", or any call while no game is running): tell the clients now, so that what they add up
            # from the events stays equal to the session's own totals.
            try:
                self.bus.publish(UiEvent(type="agent.metrics", data=self.usage.metrics_for(agent_id)))  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - the numbers reach the clients with the next snapshot anyway
                logger.debug("could not publish the metrics of %s", agent_id, exc_info=True)

    def _on_budget_changed(self, *, force: bool) -> None:
        if self._budget_watch is None:
            self._budget_watch = budget_mod.BudgetWatch()
        settings = self.budget_settings()
        usage = self.usage  # type: ignore[attr-defined]
        announced = self._budget_watch.update(settings, usage.total_tokens, usage.total_cost_usd)
        now = time.monotonic()
        payload = {"budget": self.budget_payload()}
        if announced is not None:
            self.bus.publish(UiEvent(type=f"budget.{'reached' if announced == budget_mod.REACHED else 'warning'}", data=payload))  # type: ignore[attr-defined]
            if announced == budget_mod.REACHED and settings.force_stop and self.session.status == "running":  # type: ignore[attr-defined]
                self._spawn(  # type: ignore[attr-defined]
                    self.pause("budget", hold_reason="已达到你设置的预算上限；提高上限或取消“强制停止”后可以继续。")  # type: ignore[attr-defined]
                )
        if force or announced is not None or now - self._budget_pushed_at >= BUDGET_PUSH_INTERVAL_S:
            self._budget_pushed_at = now
            self.bus.publish(UiEvent(type="budget.updated", data=payload))  # type: ignore[attr-defined]
