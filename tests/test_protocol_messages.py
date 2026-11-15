"""Canonical message helpers and per-protocol message conversion (pure, no SDK)."""

from __future__ import annotations

import json

from app.llm.anthropic_messages import AnthropicMessagesAdapter
from app.llm.messages import (
    INTERRUPTED_TOOL_RESULT,
    arguments_to_dict,
    repair_tool_pairs,
    safe_tool_id,
    strip_private,
    text_of,
)
from app.llm.openai_responses import OpenAIResponsesAdapter
from app.llm.types import Target


def call(call_id: str, name: str = "speak", args: str = '{"text": "hi"}') -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


def assistant(content=None, calls=None, private=None) -> dict:
    msg: dict = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = calls
    if private is not None:
        msg["_provider_private"] = private
    return msg


def tool(call_id: str, content: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def target(protocol: str, **kw) -> Target:
    defaults = dict(provider_id="p1", protocol=protocol, base_url="https://x.test", api_key="k", model_id="m1")
    defaults.update(kw)
    return Target(**defaults)


# ------------------------------------------------------------------ helpers
def test_text_of_handles_all_content_shapes() -> None:
    assert text_of(None) == ""
    assert text_of("abc") == "abc"
    assert text_of([{"type": "text", "text": "a"}, {"type": "image"}, "b"]) == "a\nb"
    assert text_of(12) == "12"


def test_strip_private_removes_underscore_keys_without_mutating_input() -> None:
    original = [
        {"role": "system", "content": "s"},
        assistant("x", [call("c1")], private={"blocks": [1]}),
    ]
    cleaned = strip_private(original)
    assert "_provider_private" not in cleaned[1]
    assert cleaned[1]["tool_calls"] == original[1]["tool_calls"]
    assert "_provider_private" in original[1]
    assert cleaned[0] is original[0]  # untouched messages are not copied


def test_repair_adds_missing_results_and_drops_orphans() -> None:
    msgs = [
        {"role": "user", "content": "go"},
        tool("ghost", "orphan"),  # no preceding call
        assistant(None, [call("a"), call("b")]),
        tool("b", "result-b"),  # out of order; "a" missing
        {"role": "user", "content": "next"},
    ]
    fixed = repair_tool_pairs(msgs)
    roles = [m["role"] for m in fixed]
    assert roles == ["user", "assistant", "tool", "tool", "user"]
    assert [m["tool_call_id"] for m in fixed[2:4]] == ["a", "b"]
    assert fixed[2]["content"] == INTERRUPTED_TOOL_RESULT
    assert fixed[3]["content"] == "result-b"


def test_repair_drops_duplicate_results_and_keeps_valid_history_untouched() -> None:
    valid = [
        {"role": "user", "content": "go"},
        assistant("t", [call("a")]),
        tool("a"),
        assistant("done"),
    ]
    assert repair_tool_pairs(valid) == valid
    dup = [valid[0], valid[1], tool("a", "first"), tool("a", "second")]
    fixed = repair_tool_pairs(dup)
    assert [m["content"] for m in fixed if m["role"] == "tool"] == ["first"]


def test_safe_tool_id_and_arguments() -> None:
    assert safe_tool_id("call_1-x") == "call_1-x"
    assert safe_tool_id("fc:ab/c.d") == "fc_ab_c_d"
    assert len(safe_tool_id("x" * 200)) == 64
    assert safe_tool_id("") == "call_0"
    assert arguments_to_dict('{"a": 1}') == {"a": 1}
    assert arguments_to_dict("") == {}
    assert arguments_to_dict("[1]") == {}
    assert arguments_to_dict("{bad") == {}


# ---------------------------------------------------------------- Anthropic
def test_anthropic_system_tool_use_and_tool_result_conversion() -> None:
    adapter = AnthropicMessagesAdapter()
    msgs = [
        {"role": "system", "content": "规则 A"},
        {"role": "system", "content": "规则 B"},
        {"role": "user", "content": "开始"},
        assistant("我来发言", [call("call_1", "speak", '{"text": "你好"}'), call("call_2", "note", "{}")]),
        tool("call_1", "已发送"),
        tool("call_2", "已记录"),
        {"role": "user", "content": "继续"},
    ]
    system, out = adapter.convert_messages(target("anthropic_messages"), msgs, with_thinking=True)
    assert system == "规则 A\n\n规则 B"
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[0]["content"] == [{"type": "text", "text": "开始"}]
    assert out[1]["content"] == [
        {"type": "text", "text": "我来发言"},
        {"type": "tool_use", "id": "call_1", "name": "speak", "input": {"text": "你好"}},
        {"type": "tool_use", "id": "call_2", "name": "note", "input": {}},
    ]
    # both results and the follow-up user text share one user turn, results first
    assert [b["type"] for b in out[2]["content"]] == ["tool_result", "tool_result", "text"]
    assert out[2]["content"][0] == {"type": "tool_result", "tool_use_id": "call_1", "content": "已发送"}


def test_anthropic_repairs_interrupted_calls_and_ends_with_user() -> None:
    adapter = AnthropicMessagesAdapter()
    msgs = [
        {"role": "user", "content": "go"},
        assistant("先想想", [call("a")]),
    ]
    _, out = adapter.convert_messages(target("anthropic_messages"), msgs, with_thinking=False)
    assert out[-1]["role"] == "user"
    assert out[-1]["content"][0]["type"] == "tool_result"
    assert out[-1]["content"][0]["content"] == INTERRUPTED_TOOL_RESULT


def test_anthropic_leading_assistant_gets_a_user_turn_and_empty_texts_are_replaced() -> None:
    adapter = AnthropicMessagesAdapter()
    msgs = [assistant("hello"), {"role": "user", "content": ""}]
    _, out = adapter.convert_messages(target("anthropic_messages"), msgs, with_thinking=False)
    assert out[0]["role"] == "user"
    assert out[1]["role"] == "assistant"
    assert out[2]["content"][0]["text"]  # never an empty text block


def test_anthropic_sanitizes_tool_ids_consistently() -> None:
    adapter = AnthropicMessagesAdapter()
    msgs = [
        {"role": "user", "content": "go"},
        assistant(None, [call("fc:1/x")]),
        tool("fc:1/x", "r"),
    ]
    _, out = adapter.convert_messages(target("anthropic_messages"), msgs, with_thinking=False)
    use = out[1]["content"][0]
    result = out[2]["content"][0]
    assert use["id"] == result["tool_use_id"] == "fc_1_x"


def test_anthropic_replays_private_thinking_only_for_the_same_provider() -> None:
    adapter = AnthropicMessagesAdapter()
    blocks = [{"type": "thinking", "thinking": "hmm", "signature": "SIG"}]
    private = {"provider_id": "p1", "protocol": "anthropic_messages", "model_id": "m1", "blocks": blocks}
    msgs = [
        {"role": "user", "content": "go"},
        assistant("x", [call("a")], private=private),
        tool("a"),
    ]
    _, same = adapter.convert_messages(target("anthropic_messages"), msgs, with_thinking=True)
    assert same[1]["content"][0] == blocks[0]
    _, other_provider = adapter.convert_messages(
        target("anthropic_messages", provider_id="p2"), msgs, with_thinking=True
    )
    assert other_provider[1]["content"][0]["type"] == "text"
    _, other_model = adapter.convert_messages(
        target("anthropic_messages", model_id="m2"), msgs, with_thinking=True
    )
    assert other_model[1]["content"][0]["type"] == "text"
    _, no_thinking = adapter.convert_messages(target("anthropic_messages"), msgs, with_thinking=False)
    assert no_thinking[1]["content"][0]["type"] == "text"
    # the stored private blocks are copied, not shared
    same[1]["content"][0]["thinking"] = "changed"
    assert blocks[0]["thinking"] == "hmm"


# ---------------------------------------------------------------- Responses
def test_responses_conversion_of_system_user_calls_and_outputs() -> None:
    adapter = OpenAIResponsesAdapter()
    msgs = [
        {"role": "system", "content": "规则"},
        {"role": "user", "content": "开始"},
        assistant("我来", [call("call_1", "speak", '{"text": "hi"}')]),
        tool("call_1", "done"),
        {"role": "system", "content": "中途提醒"},
        {"role": "user", "content": "继续"},
    ]
    instructions, items = adapter.convert_messages(target("openai_responses"), msgs)
    assert instructions == "规则"
    assert items == [
        {"role": "user", "content": "开始"},
        {"role": "assistant", "content": "我来"},
        {"type": "function_call", "call_id": "call_1", "name": "speak", "arguments": '{"text": "hi"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "done"},
        {"role": "system", "content": "中途提醒"},
        {"role": "user", "content": "继续"},
    ]


def test_responses_repairs_missing_outputs() -> None:
    adapter = OpenAIResponsesAdapter()
    msgs = [{"role": "user", "content": "go"}, assistant(None, [call("a")])]
    _, items = adapter.convert_messages(target("openai_responses"), msgs)
    assert items[-1] == {
        "type": "function_call_output",
        "call_id": "a",
        "output": INTERRUPTED_TOOL_RESULT,
    }


def test_responses_replays_stored_items_for_the_same_provider_only() -> None:
    adapter = OpenAIResponsesAdapter()
    stored = [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ENC"},
        {"type": "function_call", "id": "fc_1", "call_id": "a", "name": "speak", "arguments": "{}"},
    ]
    private = {
        "provider_id": "p1",
        "protocol": "openai_responses",
        "model_id": "m1",
        "items": stored,
        "call_ids": ["a"],
    }
    msgs = [{"role": "user", "content": "go"}, assistant(None, [call("a")], private=private), tool("a")]
    _, items = adapter.convert_messages(target("openai_responses"), msgs)
    assert items[1:3] == stored
    # different provider -> plain reconstruction, no reasoning item
    _, items = adapter.convert_messages(target("openai_responses", provider_id="other"), msgs)
    assert all(i.get("type") != "reasoning" for i in items)
    assert items[1]["type"] == "function_call" and "id" not in items[1]
    # the engine rewrote the call ids (e.g. after a provider switch) -> do not replay stale items
    msgs2 = [{"role": "user", "content": "go"}, assistant(None, [call("renamed")], private=private), tool("renamed")]
    _, items = adapter.convert_messages(target("openai_responses"), msgs2)
    assert all(i.get("type") != "reasoning" for i in items)
    json.dumps(items)  # JSON-serialisable
