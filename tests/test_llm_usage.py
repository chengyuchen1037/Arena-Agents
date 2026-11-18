"""v0.12.1: usage is parsed into four classes (fresh input / cache read / cache write / output) for every
protocol, Anthropic requests carry prompt-cache breakpoints, and the price snapshot has four prices."""

from __future__ import annotations

from types import SimpleNamespace as ns

import pytest

from app.llm.anthropic_messages import add_cache_breakpoints, strip_cache_control
from app.llm.common import anthropic_usage_parts, openai_usage_parts
from app.llm.types import LLMResponse
from app.providers.models import ModelEntry


# ------------------------------------------------------------------------------- OpenAI style
def test_openai_cached_tokens_are_split_out_of_the_prompt_tokens():
    usage = ns(prompt_tokens=1000, completion_tokens=50, prompt_tokens_details=ns(cached_tokens=800))
    assert openai_usage_parts(usage) == (200, 800)


def test_deepseek_reports_the_cache_hit_in_its_own_field():
    usage = ns(prompt_tokens=1000, prompt_cache_hit_tokens=640, prompt_cache_miss_tokens=360)
    assert openai_usage_parts(usage) == (360, 640)


def test_extra_fields_of_the_sdk_models_and_plain_dicts_are_understood():
    sdk = ns(prompt_tokens=500, model_extra={"prompt_cache_hit_tokens": 100})
    assert openai_usage_parts(sdk) == (400, 100)
    assert openai_usage_parts({"prompt_tokens": 90, "prompt_tokens_details": {"cached_tokens": 30}}) == (60, 30)


def test_the_responses_api_uses_input_tokens_details():
    usage = ns(input_tokens=700, output_tokens=20, input_tokens_details=ns(cached_tokens=512))
    assert openai_usage_parts(usage, details="input_tokens_details") == (188, 512)


@pytest.mark.parametrize("usage", [None, ns(), ns(prompt_tokens=None), ns(prompt_tokens="x"), {}])
def test_missing_or_garbled_usage_is_zero_not_an_error(usage):
    assert openai_usage_parts(usage) == (0, 0)


def test_a_cache_count_larger_than_the_prompt_is_clamped():
    assert openai_usage_parts(ns(prompt_tokens=100, prompt_tokens_details=ns(cached_tokens=999))) == (0, 100)


# ------------------------------------------------------------------------------- Anthropic
def test_anthropic_input_tokens_already_exclude_the_cache_counters():
    usage = ns(input_tokens=12, cache_read_input_tokens=3000, cache_creation_input_tokens=400, output_tokens=7)
    assert anthropic_usage_parts(usage) == (12, 3000, 400)
    assert anthropic_usage_parts(None) == (0, 0, 0)


def test_cache_breakpoints_go_on_system_tools_and_the_last_block_only():
    request = {
        "system": "规则",
        "tools": [{"name": "a"}, {"name": "b"}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "第一条"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "好"}, {"type": "tool_use", "id": "t", "name": "a", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]},
        ],
    }
    add_cache_breakpoints(request)
    mark = {"type": "ephemeral"}
    assert request["system"] == [{"type": "text", "text": "规则", "cache_control": mark}]
    assert "cache_control" not in request["tools"][0] and request["tools"][1]["cache_control"] == mark
    assert "cache_control" not in str(request["messages"][:2])
    assert request["messages"][2]["content"][0]["cache_control"] == mark
    assert str(request).count("cache_control") == 3  # three of the four allowed breakpoints


def test_a_trailing_thinking_block_is_never_marked_and_string_content_is_wrapped():
    request = {"messages": [{"role": "assistant", "content": [{"type": "thinking", "thinking": "x", "signature": "s"}]}]}
    add_cache_breakpoints(request)
    assert "cache_control" not in str(request)
    request = {"messages": [{"role": "user", "content": "你好"}]}
    add_cache_breakpoints(request)
    assert request["messages"][0]["content"] == [{"type": "text", "text": "你好", "cache_control": {"type": "ephemeral"}}]


def test_strip_cache_control_removes_every_marker_for_upstreams_that_reject_them():
    request = {"system": [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}], "tools": [{"name": "a", "cache_control": {}}]}
    assert strip_cache_control(request) is True
    assert "cache_control" not in str(request)
    assert strip_cache_control(request) is False


# ------------------------------------------------------------------------------- prices
def test_an_empty_cache_price_is_billed_like_an_input_token():
    model = ModelEntry(id="m", input_price_per_m=3.0, output_price_per_m=15.0)
    assert model.price_snapshot() == {
        "input_price_per_m": 3.0, "output_price_per_m": 15.0, "cache_read_price_per_m": 3.0, "cache_write_price_per_m": 3.0,
    }
    cheap = ModelEntry(id="m", input_price_per_m=3.0, output_price_per_m=15.0, cache_read_price_per_m=0.3, cache_write_price_per_m=0.0)
    snap = cheap.price_snapshot()
    assert snap["cache_read_price_per_m"] == 0.3 and snap["cache_write_price_per_m"] == 0.0  # zero is a real price


def test_old_registry_rows_without_the_new_fields_still_load():
    model = ModelEntry.model_validate({"id": "m", "input_price_per_m": 1, "output_price_per_m": 2, "context_window_tokens": 8000})
    assert model.cache_read_price_per_m is None and model.price_source == "manual"


@pytest.mark.parametrize("bad", [-1.0, float("inf"), float("nan")])
def test_cache_prices_must_be_finite_and_not_negative(bad):
    with pytest.raises(ValueError):
        ModelEntry(id="m", input_price_per_m=1, output_price_per_m=2, cache_read_price_per_m=bad)


def test_the_response_carries_the_cache_fields_with_zero_defaults():
    resp = LLMResponse()
    assert (resp.cache_read_tokens, resp.cache_write_tokens, resp.cache_read_price_per_m, resp.cache_write_price_per_m) == (0, 0, 0.0, 0.0)


# ------------------------------------------------------------------------------- through the adapters
import asyncio
from unittest.mock import patch

from app.engine.llm import LLMClient
from test_llm_support import CHAT_SDK, SharedScriptFactory, base_cfg, make_chunk


def _chat(usage_obj):
    chunks = [make_chunk("好"), make_chunk(finish="stop")]
    last = make_chunk(no_choices=True)
    last.usage = usage_obj
    return [*chunks, last]


def _run_chat(chunks):
    factory = SharedScriptFactory([chunks])
    llm = LLMClient(base_cfg())

    async def go():
        with patch(CHAT_SDK, new=factory):
            return await llm.chat_stream([{"role": "user", "content": "hi"}])

    return asyncio.run(go())


def test_the_chat_adapter_reports_openai_cache_hits_separately():
    resp = _run_chat(_chat(ns(prompt_tokens=2000, completion_tokens=40, prompt_tokens_details=ns(cached_tokens=1536))))
    assert (resp.prompt_tokens, resp.cache_read_tokens, resp.cache_write_tokens, resp.completion_tokens) == (464, 1536, 0, 40)


def test_the_chat_adapter_reports_deepseek_cache_hits_separately():
    resp = _run_chat(_chat(ns(prompt_tokens=900, completion_tokens=10, prompt_cache_hit_tokens=700, prompt_cache_miss_tokens=200)))
    assert (resp.prompt_tokens, resp.cache_read_tokens, resp.completion_tokens) == (200, 700, 10)


def test_a_fully_cached_prompt_is_not_replaced_by_an_estimate():
    resp = _run_chat(_chat(ns(prompt_tokens=800, completion_tokens=5, prompt_cache_hit_tokens=800)))
    assert (resp.prompt_tokens, resp.cache_read_tokens) == (0, 800)  # zero fresh input is a real number


def test_the_price_snapshot_of_a_response_has_four_prices():
    from app.engine.llm import LLMClient as Client
    from app.llm.types import Target

    target = Target(
        provider_id="p", protocol="openai_chat", base_url="https://x", api_key="k", model_id="m",
        input_price_per_m=0.3, output_price_per_m=1.2, cache_read_price_per_m=0.006, cache_write_price_per_m=0.3,
    )
    stamped = Client._stamp(LLMResponse(), target)
    assert (stamped.input_price_per_m, stamped.output_price_per_m) == (0.3, 1.2)
    assert (stamped.cache_read_price_per_m, stamped.cache_write_price_per_m) == (0.006, 0.3)


def test_build_target_resolves_empty_cache_prices_to_the_input_price():
    from app.engine.llm import _price_fields

    assert _price_fields(ModelEntry(id="m", input_price_per_m=2.0, output_price_per_m=8.0)) == {
        "input_price_per_m": 2.0, "output_price_per_m": 8.0, "cache_read_price_per_m": 2.0, "cache_write_price_per_m": 2.0,
    }
    assert _price_fields(ModelEntry(id="m", input_price_per_m=2.0, output_price_per_m=8.0, cache_read_price_per_m=0.2))["cache_read_price_per_m"] == 0.2
