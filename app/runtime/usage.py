"""Per-session accounting: tokens, cost and behaviour counters per agent and per model.

``UsageBook`` implements :class:`app.runtime.ports.UsageSink` and is a plain pydantic model, so the
orchestrator stores it in the save file with ``model_dump()`` and restores it with
``UsageBook.model_validate(...)``.  Field names follow the v0.11 ``usage_stats`` (``prompt_tokens``,
``completion_tokens``, ``total_tokens``, ``total_cost_usd``, ``by_model``, ``by_actor``) and add
``tool_calls`` / ``thinking_rounds`` / ``rejections`` per agent.

Cost is the sum of *per-call* costs, each computed with the price snapshot carried by that call's
response: editing a model's price later never recomputes history.  A response without a snapshot
(a client that is not routed through a binding) is priced with the book's legacy fallback prices.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

COUNTERS = ("tool_calls", "thinking_rounds", "rejections")


class _Totals(BaseModel):
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


class ActorUsage(_Totals):
    """Totals of one agent; ``cost_usd`` is the sum of per-call price snapshots."""

    tool_calls: int = 0  # tool calls the agent made (accepted or not)
    thinking_rounds: int = 0  # LLM rounds that produced reasoning text
    rejections: int = 0  # tool calls the commit gate rejected (receipt ``ok=False``)


class ModelUsage(_Totals):
    """Totals of one model; the prices are those of the most recent call (a snapshot)."""

    provider_id: str = ""
    model_id: str = ""
    input_price_per_m: float = 0.0
    output_price_per_m: float = 0.0


class UsageBook(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    # Fallback prices (USD per million tokens) for calls that carry no price snapshot, and the
    # context window shown by the API when no per-agent binding says otherwise.
    input_price_per_m: float = 0.27
    output_price_per_m: float = 1.10
    context_window_tokens: int = 1_000_000
    by_model: dict[str, ModelUsage] = Field(default_factory=dict)  # "<provider_id>::<model_id>"
    by_actor: dict[str, ActorUsage] = Field(default_factory=dict)  # agent id -> totals

    # ------------------------------------------------------------------ recording --
    def record_usage(
        self,
        agent_id: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        provider_id: str | None = None,
        model_id: str | None = None,
        input_price_per_m: float | None = None,
        output_price_per_m: float | None = None,
    ) -> float:
        """Account one LLM call and return its cost (USD)."""
        p_tok = max(0, int(prompt_tokens or 0))
        c_tok = max(0, int(completion_tokens or 0))
        in_price = self.input_price_per_m if input_price_per_m is None else float(input_price_per_m)
        out_price = self.output_price_per_m if output_price_per_m is None else float(output_price_per_m)
        cost = p_tok / 1_000_000.0 * in_price + c_tok / 1_000_000.0 * out_price

        self.prompt_tokens += p_tok
        self.completion_tokens += c_tok
        self.total_tokens = self.prompt_tokens + self.completion_tokens
        self.total_cost_usd = round(self.total_cost_usd + cost, 10)

        actor = self.by_actor.setdefault(agent_id, ActorUsage())
        actor.calls += 1
        actor.prompt_tokens += p_tok
        actor.completion_tokens += c_tok
        actor.cost_usd = round(actor.cost_usd + cost, 10)

        if provider_id or model_id:
            key = f"{provider_id or ''}::{model_id or ''}"
            model = self.by_model.setdefault(
                key, ModelUsage(provider_id=provider_id or "", model_id=model_id or "")
            )
            model.calls += 1
            model.prompt_tokens += p_tok
            model.completion_tokens += c_tok
            model.cost_usd = round(model.cost_usd + cost, 10)
            model.input_price_per_m = in_price
            model.output_price_per_m = out_price
        return cost

    def record_llm(self, agent_id: str, resp: Any) -> None:
        """Account an ``LLMResponse`` (reads its provider / model / price snapshot fields)."""
        provider_id = getattr(resp, "provider_id", None) or None
        model_id = getattr(resp, "model_id", None) or None
        # A response carries a price snapshot only when it was routed through a binding (it then
        # names its provider / model).  A snapshot of 0.0 is a legitimate free model.
        has_snapshot = bool(provider_id or model_id)
        self.record_usage(
            agent_id,
            prompt_tokens=getattr(resp, "prompt_tokens", 0),
            completion_tokens=getattr(resp, "completion_tokens", 0),
            provider_id=provider_id,
            model_id=model_id,
            input_price_per_m=getattr(resp, "input_price_per_m", None) if has_snapshot else None,
            output_price_per_m=getattr(resp, "output_price_per_m", None) if has_snapshot else None,
        )

    def count(self, agent_id: str, what: str, n: int = 1) -> None:
        """Bump one of the behaviour counters (``tool_calls`` / ``thinking_rounds`` / ``rejections``)."""
        if what not in COUNTERS:
            raise ValueError(f"unknown usage counter {what!r}; expected one of {COUNTERS}")
        actor = self.by_actor.setdefault(agent_id, ActorUsage())
        setattr(actor, what, getattr(actor, what) + int(n))

    # ------------------------------------------------------------------- reading --
    def actor(self, agent_id: str) -> ActorUsage:
        """The agent's totals (a zeroed record when it has not been accounted yet)."""
        return self.by_actor.get(agent_id) or ActorUsage()

    def metrics_for(self, agent_id: str) -> dict[str, Any]:
        """Payload of the ``agent.metrics`` spectator event for one agent."""
        actor = self.actor(agent_id)
        return {
            "id": agent_id,
            "calls": actor.calls,
            "prompt_tokens": actor.prompt_tokens,
            "completion_tokens": actor.completion_tokens,
            "cost_usd": actor.cost_usd,
            "tool_calls": actor.tool_calls,
            "thinking_rounds": actor.thinking_rounds,
            "rejections": actor.rejections,
        }

    def snapshot(self) -> dict[str, Any]:
        """JSON-friendly copy for the public session (``usage_stats``)."""
        return self.model_dump(mode="json")
