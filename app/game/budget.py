"""The user's spending limit of one game (v0.12.1, E10 / E22).

The engine itself never limits anything: agents think as much as they want.  The *user* may set a token
limit and / or a cost limit for the current game and choose whether reaching it must stop the game.
Everything here is pure: the orchestrator feeds it the usage book after every accounted LLM call and
acts on the transition it reports.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

WARNING_AT = 0.8  # 80 %: the front end turns the counter yellow
MAX_TOKENS_LIMIT = 10**12
MAX_COST_LIMIT = 10**6

OK, WARNING, REACHED = "ok", "warning", "reached"


class BudgetSettings(BaseModel):
    """What the user configured.  ``None`` = no limit of that kind."""

    model_config = ConfigDict(extra="forbid")

    max_tokens: int | None = Field(default=None, gt=0, le=MAX_TOKENS_LIMIT)
    max_cost_usd: float | None = Field(default=None, gt=0, le=MAX_COST_LIMIT, allow_inf_nan=False)
    force_stop: bool = False  # reaching a limit pauses the game (otherwise it only warns)

    @field_validator("max_tokens", "max_cost_usd", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()) or value == 0:
            return None
        return value


def progress(settings: BudgetSettings, total_tokens: int, total_cost_usd: float) -> float:
    """The larger of the two used fractions (0.0 when no limit is set)."""
    fractions = []
    if settings.max_tokens:
        fractions.append(total_tokens / settings.max_tokens)
    if settings.max_cost_usd:
        fractions.append(total_cost_usd / settings.max_cost_usd)
    return max(fractions) if fractions else 0.0


def state_of(settings: BudgetSettings, total_tokens: int, total_cost_usd: float) -> str:
    used = progress(settings, total_tokens, total_cost_usd)
    if used >= 1.0:
        return REACHED
    if used >= WARNING_AT:
        return WARNING
    return OK


def public_view(settings: BudgetSettings, total_tokens: int, total_cost_usd: float) -> dict[str, Any]:
    """The ``budget`` object of the public session (contracts section 10.6)."""
    return {
        "max_tokens": settings.max_tokens,
        "max_cost_usd": settings.max_cost_usd,
        "force_stop": settings.force_stop,
        "used_tokens": int(total_tokens),
        "used_cost_usd": round(float(total_cost_usd), 6),
        "progress": round(progress(settings, total_tokens, total_cost_usd), 4),
        "state": state_of(settings, total_tokens, total_cost_usd),
    }


class BudgetWatch:
    """Remembers which thresholds were already announced so each fires once (per limit configuration)."""

    def __init__(self) -> None:
        self._announced: str = OK

    def reset(self) -> None:
        self._announced = OK

    def update(self, settings: BudgetSettings, total_tokens: int, total_cost_usd: float) -> str | None:
        """The state to announce *now* (``"warning"`` / ``"reached"``), or ``None``.

        Raising a limit (or removing it) re-arms the announcements: a user who raised the limit after the
        game stopped wants to hear about it again when the new limit is reached."""
        current = state_of(settings, total_tokens, total_cost_usd)
        if current == OK:
            self._announced = OK
            return None
        rank = {OK: 0, WARNING: 1, REACHED: 2}
        if rank[current] > rank[self._announced]:
            self._announced = current
            return current
        if rank[current] < rank[self._announced]:
            self._announced = current  # the limit was raised: down from "reached" to "warning"
        return None
