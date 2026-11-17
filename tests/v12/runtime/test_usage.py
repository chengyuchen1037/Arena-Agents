from __future__ import annotations

import json

import pytest

from app.llm.types import LLMResponse
from app.runtime.ports import UsageSink
from app.runtime.usage import ActorUsage, ModelUsage, UsageBook, hit_rate


def priced(
    prompt: int,
    completion: int,
    *,
    cache_read: int = 0,
    cache_write: int = 0,
    provider="prov",
    model="m1",
    in_price=2.0,
    out_price=8.0,
    cr_price: float | None = None,
    cw_price: float | None = None,
) -> LLMResponse:
    """A response with a full price snapshot (an empty cache price follows the input price, as the LLM layer does)."""
    return LLMResponse(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        provider_id=provider,
        model_id=model,
        input_price_per_m=in_price,
        output_price_per_m=out_price,
        cache_read_price_per_m=in_price if cr_price is None else cr_price,
        cache_write_price_per_m=in_price if cw_price is None else cw_price,
    )


def test_usage_book_satisfies_the_usage_sink_protocol():
    book: UsageSink = UsageBook()
    book.record_llm("a1", priced(1, 1))
    book.count("a1", "tool_calls")
    book.count("a1", "activations")


# ------------------------------------------------------------------------ the four kinds of tokens


def test_cost_follows_the_four_price_formula():
    book = UsageBook()
    # 1M x $2 (miss) + 2M x $0.2 (cache read) + 0.5M x $2.5 (cache write) + 0.25M x $8 (output)
    cost = book.record_usage(
        "a1",
        prompt_tokens=1_000_000,
        cache_read_tokens=2_000_000,
        cache_write_tokens=500_000,
        completion_tokens=250_000,
        provider_id="prov",
        model_id="m1",
        input_price_per_m=2.0,
        output_price_per_m=8.0,
        cache_read_price_per_m=0.2,
        cache_write_price_per_m=2.5,
    )
    expected = 2.0 + 0.4 + 1.25 + 2.0
    assert cost == pytest.approx(expected) and book.total_cost_usd == pytest.approx(expected)
    assert book.by_actor["a1"].cost_usd == pytest.approx(expected)
    assert book.by_model["prov::m1"].cost_usd == pytest.approx(expected)


def test_record_llm_reads_the_cache_tokens_and_all_four_prices_of_the_snapshot():
    book = UsageBook()
    book.record_llm(
        "a1", priced(1000, 200, cache_read=9000, cache_write=500, in_price=3.0, out_price=15.0, cr_price=0.3, cw_price=3.75)
    )
    assert (book.prompt_tokens, book.cache_read_tokens, book.cache_write_tokens, book.completion_tokens) == (
        1000, 9000, 500, 200,
    )
    assert book.total_cost_usd == pytest.approx((1000 * 3.0 + 9000 * 0.3 + 500 * 3.75 + 200 * 15.0) / 1e6)
    model = book.by_model["prov::m1"]
    assert (model.input_price_per_m, model.output_price_per_m) == (3.0, 15.0)
    assert (model.cache_read_price_per_m, model.cache_write_price_per_m) == (0.3, 3.75)


def test_total_tokens_is_the_sum_of_the_four_kinds():
    book = UsageBook()
    book.record_llm("a1", priced(10, 20, cache_read=30, cache_write=40))
    book.record_llm("a2", priced(1, 2, cache_read=3, cache_write=4))
    assert book.total_tokens == 10 + 20 + 30 + 40 + 1 + 2 + 3 + 4 == 110
    assert book.by_actor["a1"].total_tokens == 100


def test_cache_hit_rate_is_read_over_all_input_per_agent_and_overall():
    book = UsageBook()
    assert book.cache_hit_rate == 0.0  # no input yet
    book.record_llm("a1", priced(100, 5, cache_read=300, cache_write=100))  # 300 / 500
    book.record_llm("a2", priced(100, 5))  # nothing cached: 0 / 100
    assert book.by_actor["a1"].cache_hit_rate == pytest.approx(0.6)
    assert book.by_actor["a2"].cache_hit_rate == 0.0
    assert book.cache_hit_rate == pytest.approx(300 / 600)
    assert hit_rate(0, 0, 0) == 0.0 and hit_rate(1, 3, 0) == pytest.approx(0.75)


def test_a_price_snapshot_is_per_call_and_later_prices_never_touch_history():
    book = UsageBook()
    book.record_llm("a1", priced(1_000_000, 500_000, in_price=2.0, out_price=8.0))
    book.record_llm("a1", priced(1_000_000, 0, in_price=4.0, out_price=16.0))
    # first call: 1M x $2 + 0.5M x $8 = $6;  second call: 1M x $4 = $4
    assert book.total_cost_usd == pytest.approx(10.0)
    assert book.by_actor["a1"].calls == 2 and book.by_actor["a1"].cost_usd == pytest.approx(10.0)
    model = book.by_model["prov::m1"]
    assert model.calls == 2 and model.provider_id == "prov" and model.model_id == "m1"
    assert (model.input_price_per_m, model.output_price_per_m) == (4.0, 16.0)  # the most recent snapshot


def test_a_free_model_and_a_free_cache_are_honoured_not_replaced_by_the_fallback():
    book = UsageBook()
    book.record_llm("a1", priced(1_000_000, 1_000_000, in_price=0.0, out_price=0.0))
    assert book.total_cost_usd == 0.0
    book.record_llm("a1", priced(0, 0, cache_read=1_000_000, in_price=2.0, cr_price=0.0))  # a free cache read
    assert book.total_cost_usd == 0.0 and book.by_actor["a1"].cache_read_tokens == 1_000_000


def test_a_response_without_a_snapshot_is_priced_with_the_fallback_prices():
    book = UsageBook(input_price_per_m=1.0, output_price_per_m=3.0)
    book.record_llm("a1", LLMResponse(prompt_tokens=1_000_000, completion_tokens=1_000_000))
    assert book.total_cost_usd == pytest.approx(4.0)
    assert book.by_model == {}  # nothing names a model
    # cache tokens without a snapshot follow the input price (unless the book names cache fallbacks)
    book.record_llm("a1", LLMResponse(cache_read_tokens=1_000_000, cache_write_tokens=1_000_000))
    assert book.total_cost_usd == pytest.approx(4.0 + 1.0 + 1.0)
    book2 = UsageBook(input_price_per_m=1.0, cache_read_price_per_m=0.1, cache_write_price_per_m=1.25)
    book2.record_llm("a1", LLMResponse(cache_read_tokens=1_000_000, cache_write_tokens=1_000_000))
    assert book2.total_cost_usd == pytest.approx(0.1 + 1.25)


def test_negative_or_missing_token_counts_are_clamped():
    book = UsageBook()
    cost = book.record_usage("a1", prompt_tokens=-5, completion_tokens=None, cache_read_tokens=-1)  # type: ignore[arg-type]
    assert cost == 0.0 and book.total_tokens == 0 and book.by_actor["a1"].calls == 1 and book.calls == 1


def test_totals_by_model_and_by_actor_agree():
    book = UsageBook()
    for agent, model in (("a1", "m1"), ("a2", "m1"), ("a1", "m2")):
        book.record_llm(agent, priced(10, 5, cache_read=20, cache_write=1, model=model))
    assert book.calls == 3
    assert sum(a.calls for a in book.by_actor.values()) == 3 == sum(m.calls for m in book.by_model.values())
    assert sum(a.cost_usd for a in book.by_actor.values()) == pytest.approx(book.total_cost_usd)
    assert sum(m.cost_usd for m in book.by_model.values()) == pytest.approx(book.total_cost_usd)
    assert sum(a.cache_read_tokens for a in book.by_actor.values()) == book.cache_read_tokens == 60


# ------------------------------------------------------------------------------------ counters


def test_counters_accumulate_per_agent_including_activations():
    book = UsageBook()
    book.count("a1", "activations")
    book.count("a1", "activations", 2)
    book.count("a1", "tool_calls", 3)
    book.count("a1", "thinking_rounds")
    book.count("a2", "rejections", 4)
    a1 = book.by_actor["a1"]
    assert (a1.activations, a1.tool_calls, a1.thinking_rounds, a1.rejections) == (3, 3, 1, 0)
    assert book.by_actor["a2"].rejections == 4 and book.by_actor["a1"].calls == 0  # counters do not invent LLM calls


def test_unknown_counter_is_a_programming_error():
    with pytest.raises(ValueError):
        UsageBook().count("a1", "bogus")


# ------------------------------------------------------------------------------ the on_record hook


def test_on_record_runs_after_every_accounted_call_with_the_agent_and_the_response():
    book = UsageBook()
    seen: list[tuple[str, int, float]] = []

    def hook(agent_id: str, resp: LLMResponse) -> None:
        # the call is already accounted when the hook runs (the orchestrator checks the budget in it)
        seen.append((agent_id, book.total_tokens, book.total_cost_usd))

    book.on_record = hook
    first = priced(1_000_000, 0, in_price=2.0)
    book.record_llm("a1", first)
    book.record_llm("a2", priced(0, 1_000_000, out_price=8.0))
    assert seen == [("a1", 1_000_000, pytest.approx(2.0)), ("a2", 2_000_000, pytest.approx(10.0))]


def test_on_record_is_not_called_for_plain_counters_or_record_usage():
    book = UsageBook()
    calls: list[str] = []
    book.on_record = lambda agent_id, resp: calls.append(agent_id)
    book.count("a1", "tool_calls")
    book.record_usage("a1", prompt_tokens=5)
    assert calls == []


def test_a_failing_hook_never_breaks_the_accounting():
    book = UsageBook()

    def boom(agent_id: str, resp: LLMResponse) -> None:
        raise RuntimeError("budget hook bug")

    book.on_record = boom
    book.record_llm("a1", priced(10, 5))  # does not raise
    assert book.by_actor["a1"].calls == 1 and book.total_tokens == 15


def test_the_hook_is_neither_saved_nor_compared():
    book = UsageBook()
    book.on_record = lambda agent_id, resp: None
    book.record_llm("a1", priced(10, 5))
    dumped = book.model_dump()
    assert "on_record" not in dumped and "on_record" not in book.snapshot()
    json.dumps(book.snapshot())  # serializable although a hook is attached
    restored = UsageBook.model_validate(dumped)
    assert restored.on_record is None and restored == UsageBook.model_validate(dumped)


# ------------------------------------------------------------------------------------ reading


def test_metrics_for_has_the_event_shape_and_zeroes_for_unknown_agents():
    book = UsageBook()
    book.record_llm("a1", priced(1000, 200, cache_read=3000, in_price=1.0, out_price=1.0, cr_price=0.1))
    book.count("a1", "tool_calls", 3)
    book.count("a1", "activations", 2)
    metrics = book.metrics_for("a1")
    assert set(metrics) == {
        "id", "activations", "calls", "prompt_tokens", "cache_read_tokens", "cache_write_tokens", "completion_tokens",
        "cost_usd", "tool_calls", "thinking_rounds", "rejections", "cache_hit_rate",
    }
    assert metrics["id"] == "a1" and metrics["calls"] == 1 and metrics["tool_calls"] == 3 and metrics["activations"] == 2
    assert metrics["cache_read_tokens"] == 3000 and metrics["cache_hit_rate"] == pytest.approx(0.75)
    assert metrics["cost_usd"] == pytest.approx((1000 * 1.0 + 3000 * 0.1 + 200 * 1.0) / 1e6)
    zero = book.metrics_for("nobody")
    assert zero["calls"] == 0 and zero["cost_usd"] == 0.0 and zero["cache_hit_rate"] == 0.0
    assert "nobody" not in book.by_actor  # reading never creates records


def test_snapshot_has_the_public_usage_stats_shape():
    book = UsageBook()
    book.record_llm("a1", priced(10, 5, cache_read=20, cache_write=3))
    snap = book.snapshot()
    for key in (
        "prompt_tokens", "cache_read_tokens", "cache_write_tokens", "completion_tokens", "total_tokens",
        "total_cost_usd", "cache_hit_rate", "by_model", "by_actor",
    ):
        assert key in snap
    assert set(snap["by_actor"]["a1"]) == {
        "activations", "calls", "prompt_tokens", "cache_read_tokens", "cache_write_tokens", "completion_tokens",
        "cost_usd", "tool_calls", "thinking_rounds", "rejections", "cache_hit_rate",
    }
    assert snap["total_tokens"] == 38 and snap["by_actor"]["a1"]["cache_hit_rate"] == pytest.approx(20 / 33, abs=1e-5)
    assert {"calls", "cost_usd", "cache_hit_rate", "provider_id", "model_id"} <= set(snap["by_model"]["prov::m1"])
    json.dumps(snap)  # JSON-serializable


# --------------------------------------------------------------------------------- round trip


def test_round_trip_through_model_dump_and_json_restores_everything():
    book = UsageBook()
    book.record_llm("a1", priced(1234, 56, cache_read=777, cache_write=11, in_price=0.5, out_price=1.5, cr_price=0.05))
    book.record_llm("moderator", priced(10, 20, provider="p2", model="m2"))
    book.count("a1", "tool_calls", 5)
    book.count("a1", "thinking_rounds", 2)
    book.count("a1", "activations", 4)
    book.count("moderator", "rejections")

    restored = UsageBook.model_validate(book.model_dump())
    assert restored == book
    again = UsageBook.model_validate(json.loads(json.dumps(book.snapshot())))
    assert again == book
    assert isinstance(again.by_actor["a1"], ActorUsage) and isinstance(again.by_model["prov::m1"], ModelUsage)
    assert again.total_tokens == book.total_tokens and again.cache_hit_rate == book.cache_hit_rate
    # the restored book keeps accounting where the old one stopped
    again.record_llm("a1", priced(1, 1, cache_read=1))
    assert again.by_actor["a1"].calls == 2 and again.by_actor["a1"].cache_read_tokens == 778


def test_restore_ignores_unknown_and_derived_fields_and_old_saves_load():
    data = UsageBook().model_dump()
    data["something_new"] = 1
    assert UsageBook.model_validate(data).total_tokens == 0
    # a v0.12.0 save: no cache fields, ``total_tokens`` stored, by_actor without activations
    old = {
        "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "total_cost_usd": 0.5,
        "by_actor": {"p01": {"calls": 2, "prompt_tokens": 100, "completion_tokens": 50, "cost_usd": 0.5, "tool_calls": 4}},
    }
    restored = UsageBook.model_validate(old)
    assert restored.total_tokens == 150 and restored.by_actor["p01"].activations == 0
    assert restored.by_actor["p01"].cache_read_tokens == 0 and restored.cache_hit_rate == 0.0
