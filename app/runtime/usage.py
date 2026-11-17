"""Per-session accounting: four kinds of tokens, cost and behaviour counters per agent and per model.

``UsageBook`` implements :class:`app.runtime.ports.UsageSink` and is a plain pydantic model, so the
orchestrator stores it in the save file with ``model_dump()`` and restores it with
``UsageBook.model_validate(...)``.

Tokens (v0.12.1, docs/v0.12-contracts.md section 10.3)::

    prompt_tokens        input that MISSED the provider's cache
    cache_read_tokens    input served from the cache
    cache_write_tokens   input written to the cache (only some providers bill it)
    completion_tokens    output (reasoning included)
    total_tokens         = the sum of the four
    cache_hit_rate       = cache_read / (prompt + cache_read + cache_write)

Cost is the sum of *per-call* costs, each computed with the price snapshot carried by that call's
response (``LLMResponse.*_price_per_m``, USD per million tokens; an empty cache price has already been
resolved to the input price by the LLM layer)::

    cost = (prompt * input + cache_read * cache_read_price + cache_write * cache_write_price
            + completion * output) / 1e6

Editing a model's price later never recomputes history.  A response without a snapshot (a client that is
not routed through a binding) is priced with the book's fallback prices.

``on_record`` is called synchronously after every :meth:`UsageBook.record_llm` (the orchestrator checks the
user's token / cost limit in it); a failing hook is logged and never breaks the call that was accounted.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

logger = logging.getLogger(__name__)

COUNTERS = ("tool_calls", "thinking_rounds", "rejections", "activations")


def hit_rate(prompt: int, cache_read: int, cache_write: int) -> float:
    """Share of the input that was served from the cache (0.0 when there was no input at all)."""
    total = prompt + cache_read + cache_write
    return round(cache_read / total, 6) if total > 0 else 0.0


class _Totals(BaseModel):
    calls: int = 0
    prompt_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cache_hit_rate(self) -> float:
        return hit_rate(self.prompt_tokens, self.cache_read_tokens, self.cache_write_tokens)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.cache_read_tokens + self.cache_write_tokens + self.completion_tokens


class ActorUsage(_Totals):
    """Totals of one agent; ``cost_usd`` is the sum of per-call price snapshots."""

    activations: int = 0  # runs the actor started (every delivered envelope wakes it, merged when it is busy)
    tool_calls: int = 0  # tool calls the agent made (accepted or not)
    thinking_rounds: int = 0  # LLM rounds that produced reasoning text
    rejections: int = 0  # tool calls the commit gate rejected (receipt ``ok=False``)


class ModelUsage(_Totals):
    """Totals of one model; the prices are those of the most recent call (a snapshot)."""

    provider_id: str = ""
    model_id: str = ""
    input_price_per_m: float = 0.0
    output_price_per_m: float = 0.0
    cache_read_price_per_m: float = 0.0
    cache_write_price_per_m: float = 0.0


class UsageBook(BaseModel):
    model_config = ConfigDict(extra="ignore")

    calls: int = 0
    prompt_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    completion_tokens: int = 0
    total_cost_usd: float = 0.0
    # Fallback prices (USD per million tokens) for calls that carry no price snapshot; ``None`` cache prices
    # follow the input price.  ``context_window_tokens`` is what the API shows when no binding says otherwise.
    input_price_per_m: float = 0.27
    output_price_per_m: float = 1.10
    cache_read_price_per_m: float | None = None
    cache_write_price_per_m: float | None = None
    context_window_tokens: int = 1_000_000
    by_model: dict[str, ModelUsage] = Field(default_factory=dict)  # "<provider_id>::<model_id>"
    by_actor: dict[str, ActorUsage] = Field(default_factory=dict)  # agent id -> totals
    #: ``hook(agent_id, response)`` after every accounted LLM call (not saved, not compared).
    on_record: Any = Field(default=None, exclude=True, repr=False)

    # ------------------------------------------------------------------- derived --
    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.cache_read_tokens + self.cache_write_tokens + self.completion_tokens

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cache_hit_rate(self) -> float:
        return hit_rate(self.prompt_tokens, self.cache_read_tokens, self.cache_write_tokens)

    # ------------------------------------------------------------------ recording --
    def record_usage(
        self,
        agent_id: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        provider_id: str | None = None,
        model_id: str | None = None,
        input_price_per_m: float | None = None,
        output_price_per_m: float | None = None,
        cache_read_price_per_m: float | None = None,
        cache_write_price_per_m: float | None = None,
    ) -> float:
        """Account one LLM call and return its cost (USD).  ``None`` prices use the book's fallbacks."""
        p_tok = max(0, int(prompt_tokens or 0))
        c_tok = max(0, int(completion_tokens or 0))
        r_tok = max(0, int(cache_read_tokens or 0))
        w_tok = max(0, int(cache_write_tokens or 0))
        in_price = self.input_price_per_m if input_price_per_m is None else float(input_price_per_m)
        out_price = self.output_price_per_m if output_price_per_m is None else float(output_price_per_m)
        cr_price = _first(cache_read_price_per_m, self.cache_read_price_per_m, in_price)
        cw_price = _first(cache_write_price_per_m, self.cache_write_price_per_m, in_price)
        cost = (p_tok * in_price + r_tok * cr_price + w_tok * cw_price + c_tok * out_price) / 1_000_000.0

        self.calls += 1
        self.prompt_tokens += p_tok
        self.cache_read_tokens += r_tok
        self.cache_write_tokens += w_tok
        self.completion_tokens += c_tok
        self.total_cost_usd = round(self.total_cost_usd + cost, 10)

        actor = self.by_actor.setdefault(agent_id, ActorUsage())
        _add(actor, p_tok, r_tok, w_tok, c_tok, cost)

        if provider_id or model_id:
            key = f"{provider_id or ''}::{model_id or ''}"
            model = self.by_model.setdefault(
                key, ModelUsage(provider_id=provider_id or "", model_id=model_id or "")
            )
            _add(model, p_tok, r_tok, w_tok, c_tok, cost)
            model.input_price_per_m = in_price
            model.output_price_per_m = out_price
            model.cache_read_price_per_m = cr_price
            model.cache_write_price_per_m = cw_price
        return cost

    def record_llm(self, agent_id: str, resp: Any) -> None:
        """Account an ``LLMResponse`` (reads its tokens and its provider / model / price snapshot)."""
        provider_id = getattr(resp, "provider_id", None) or None
        model_id = getattr(resp, "model_id", None) or None
        # A response carries a price snapshot only when it was routed through a binding (it then names its
        # provider / model).  A snapshot of 0.0 is a legitimate free model / free cache.
        has_snapshot = bool(provider_id or model_id)

        def price(name: str) -> float | None:
            return getattr(resp, name, None) if has_snapshot else None

        self.record_usage(
            agent_id,
            prompt_tokens=getattr(resp, "prompt_tokens", 0),
            completion_tokens=getattr(resp, "completion_tokens", 0),
            cache_read_tokens=getattr(resp, "cache_read_tokens", 0),
            cache_write_tokens=getattr(resp, "cache_write_tokens", 0),
            provider_id=provider_id,
            model_id=model_id,
            input_price_per_m=price("input_price_per_m"),
            output_price_per_m=price("output_price_per_m"),
            cache_read_price_per_m=price("cache_read_price_per_m"),
            cache_write_price_per_m=price("cache_write_price_per_m"),
        )
        hook = self.on_record
        if hook is not None:
            try:
                hook(agent_id, resp)
            except Exception:  # noqa: BLE001 - a broken budget hook must not break the call that was accounted
                logger.exception("UsageBook.on_record failed for %s", agent_id)

    def count(self, agent_id: str, what: str, n: int = 1) -> None:
        """Bump a behaviour counter (``activations`` / ``tool_calls`` / ``thinking_rounds`` / ``rejections``)."""
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
            "activations": actor.activations,
            "calls": actor.calls,
            "prompt_tokens": actor.prompt_tokens,
            "cache_read_tokens": actor.cache_read_tokens,
            "cache_write_tokens": actor.cache_write_tokens,
            "completion_tokens": actor.completion_tokens,
            "cost_usd": actor.cost_usd,
            "tool_calls": actor.tool_calls,
            "thinking_rounds": actor.thinking_rounds,
            "rejections": actor.rejections,
            "cache_hit_rate": actor.cache_hit_rate,
        }

    def snapshot(self) -> dict[str, Any]:
        """JSON-friendly copy for the public session (``usage_stats``)."""
        return self.model_dump(mode="json")


def _first(*values: float | None) -> float:
    for value in values:
        if value is not None:
            return float(value)
    return 0.0  # pragma: no cover - the last argument is always a number


def _add(totals: _Totals, prompt: int, cache_read: int, cache_write: int, completion: int, cost: float) -> None:
    totals.calls += 1
    totals.prompt_tokens += prompt
    totals.cache_read_tokens += cache_read
    totals.cache_write_tokens += cache_write
    totals.completion_tokens += completion
    totals.cost_usd = round(totals.cost_usd + cost, 10)
