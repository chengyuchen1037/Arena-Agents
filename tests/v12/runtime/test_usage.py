from __future__ import annotations

import json

import pytest

from app.llm.types import LLMResponse
from app.runtime.ports import UsageSink
from app.runtime.usage import ActorUsage, ModelUsage, UsageBook


def priced(prompt: int, completion: int, *, provider="prov", model="m1", in_price=2.0, out_price=8.0) -> LLMResponse:
    return LLMResponse(
        prompt_tokens=prompt,
        completion_tokens=completion,
        provider_id=provider,
        model_id=model,
        input_price_per_m=in_price,
        output_price_per_m=out_price,
    )


def test_usage_book_satisfies_the_usage_sink_protocol():
    book: UsageSink = UsageBook()
    book.record_llm("a1", priced(1, 1))
    book.count("a1", "tool_calls")


def test_record_llm_uses_the_price_snapshot_of_each_call():
    book = UsageBook()
    book.record_llm("a1", priced(1_000_000, 500_000, in_price=2.0, out_price=8.0))
    # a later price change on the model must not touch history: the next call has its own snapshot
    book.record_llm("a1", priced(1_000_000, 0, in_price=4.0, out_price=16.0))
    assert book.prompt_tokens == 2_000_000
    assert book.completion_tokens == 500_000
    assert book.total_tokens == 2_500_000
    # first call: 1M x $2 + 0.5M x $8 = $6;  second call: 1M x $4 = $4
    assert book.total_cost_usd == pytest.approx(10.0)
    actor = book.by_actor["a1"]
    assert actor.calls == 2 and actor.cost_usd == pytest.approx(10.0)
    model = book.by_model["prov::m1"]
    assert model.calls == 2 and model.provider_id == "prov" and model.model_id == "m1"
    assert (model.input_price_per_m, model.output_price_per_m) == (4.0, 16.0)  # most recent snapshot


def test_a_free_model_snapshot_of_zero_is_honoured_not_replaced_by_the_fallback():
    book = UsageBook()
    book.record_llm("a1", priced(1_000_000, 1_000_000, in_price=0.0, out_price=0.0))
    assert book.total_cost_usd == 0.0
    assert book.by_actor["a1"].prompt_tokens == 1_000_000


def test_a_response_without_a_snapshot_is_priced_with_the_fallback_prices():
    book = UsageBook(input_price_per_m=1.0, output_price_per_m=3.0)
    book.record_llm("a1", LLMResponse(prompt_tokens=1_000_000, completion_tokens=1_000_000))
    assert book.total_cost_usd == pytest.approx(4.0)
    assert book.by_model == {}  # nothing names a model


def test_negative_or_missing_token_counts_are_clamped():
    book = UsageBook()
    cost = book.record_usage("a1", prompt_tokens=-5, completion_tokens=None)  # type: ignore[arg-type]
    assert cost == 0.0 and book.total_tokens == 0 and book.by_actor["a1"].calls == 1


def test_counters_accumulate_per_agent():
    book = UsageBook()
    book.count("a1", "tool_calls")
    book.count("a1", "tool_calls", 2)
    book.count("a1", "thinking_rounds")
    book.count("a2", "rejections", 4)
    assert book.by_actor["a1"].tool_calls == 3
    assert book.by_actor["a1"].thinking_rounds == 1
    assert book.by_actor["a1"].rejections == 0
    assert book.by_actor["a2"].rejections == 4
    assert book.by_actor["a1"].calls == 0  # counters do not invent LLM calls


def test_unknown_counter_is_a_programming_error():
    with pytest.raises(ValueError):
        UsageBook().count("a1", "bogus")


def test_metrics_for_has_the_event_shape_and_zeroes_for_unknown_agents():
    book = UsageBook()
    book.record_llm("a1", priced(1000, 200, in_price=1.0, out_price=1.0))
    book.count("a1", "tool_calls", 3)
    metrics = book.metrics_for("a1")
    assert set(metrics) == {
        "id", "calls", "prompt_tokens", "completion_tokens", "cost_usd", "tool_calls", "thinking_rounds", "rejections",
    }
    assert metrics["id"] == "a1" and metrics["calls"] == 1 and metrics["tool_calls"] == 3
    assert metrics["cost_usd"] == pytest.approx(0.0012)
    zero = book.metrics_for("nobody")
    assert zero["calls"] == 0 and zero["cost_usd"] == 0.0
    assert "nobody" not in book.by_actor  # reading never creates records


def test_round_trip_through_model_dump_and_json_restores_everything():
    book = UsageBook()
    book.record_llm("a1", priced(1234, 56, in_price=0.5, out_price=1.5))
    book.record_llm("moderator", priced(10, 20, provider="p2", model="m2"))
    book.count("a1", "tool_calls", 5)
    book.count("a1", "thinking_rounds", 2)
    book.count("moderator", "rejections")

    restored = UsageBook.model_validate(book.model_dump())
    assert restored == book
    again = UsageBook.model_validate(json.loads(json.dumps(book.snapshot())))
    assert again == book
    assert isinstance(again.by_actor["a1"], ActorUsage) and isinstance(again.by_model["prov::m1"], ModelUsage)
    # the restored book keeps accounting where the old one stopped
    again.record_llm("a1", priced(1, 1))
    assert again.by_actor["a1"].calls == 2


def test_snapshot_uses_the_v011_field_names():
    book = UsageBook()
    book.record_llm("a1", priced(10, 5))
    snap = book.snapshot()
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "total_cost_usd", "by_model", "by_actor"):
        assert key in snap
    assert set(snap["by_actor"]["a1"]) == {
        "calls", "prompt_tokens", "completion_tokens", "cost_usd", "tool_calls", "thinking_rounds", "rejections",
    }
    json.dumps(snap)  # JSON-serializable


def test_restore_ignores_unknown_fields_of_future_saves():
    data = UsageBook().model_dump()
    data["something_new"] = 1
    assert UsageBook.model_validate(data).total_tokens == 0
