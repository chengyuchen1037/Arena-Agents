from __future__ import annotations

import json

from app.llm.types import PROVIDER_PRIVATE_KEY, LLMResponse, ToolCallData
from app.runtime.messages import (
    args_error_text,
    assistant_text_message,
    assistant_tool_message,
    ensure_call_ids,
    parse_call,
    system_message,
    tool_failure_text,
    tool_message,
    user_message,
)


def test_basic_message_shapes():
    assert system_message("s") == {"role": "system", "content": "s"}
    assert user_message("u") == {"role": "user", "content": "u"}
    assert tool_message("c1", "r") == {"role": "tool", "tool_call_id": "c1", "content": "r"}


def test_assistant_tool_message_matches_the_v011_shape_and_keeps_private_blocks():
    resp = LLMResponse(
        content="",
        tool_calls=[ToolCallData(id="c1", name="speak", arguments_json='{"a":1}'), ToolCallData(id="c2", name="wait")],
        provider_private={"provider_id": "p", "blocks": [{"type": "thinking"}]},
    )
    message = assistant_tool_message(resp)
    assert message["role"] == "assistant"
    assert message["content"] is None  # empty text is sent as null, like the old engine
    assert message["tool_calls"][0] == {
        "id": "c1",
        "type": "function",
        "function": {"name": "speak", "arguments": '{"a":1}'},
    }
    assert message["tool_calls"][1]["function"]["arguments"] == "{}"  # empty arguments become an object
    assert message[PROVIDER_PRIVATE_KEY] == resp.provider_private
    plain = assistant_tool_message(LLMResponse(content="hi", tool_calls=[ToolCallData(id="x", name="wait")]))
    assert plain["content"] == "hi" and PROVIDER_PRIVATE_KEY not in plain


def test_assistant_text_message_is_none_for_silence():
    assert assistant_text_message(LLMResponse(content="  \n")) is None
    assert assistant_text_message(LLMResponse(content=" hello ")) == {"role": "assistant", "content": "hello"}


def test_ensure_call_ids_fills_blanks_and_resolves_duplicates():
    resp = LLMResponse(
        tool_calls=[
            ToolCallData(id="", name="a"),
            ToolCallData(id="dup", name="b"),
            ToolCallData(id="dup", name="c"),
            ToolCallData(id="ok", name="d"),
        ]
    )
    ensure_call_ids(resp, prefix="call_7")
    ids = [c.id for c in resp.tool_calls]
    assert len(set(ids)) == 4 and all(ids)
    assert ids[1] == "dup" and ids[3] == "ok"


def test_parse_call_returns_args_or_an_error_never_raises():
    ok, error = parse_call(ToolCallData(id="1", name="speak", arguments_json='{"channel_id":"c"}'))
    assert ok == {"channel_id": "c"} and error is None
    assert parse_call(ToolCallData(id="2", name="wait", arguments_json="")) == ({}, None)
    args, error = parse_call(ToolCallData(id="3", name="speak", arguments_json="{not json"))
    assert args == {} and error and "speak" in error
    args, error = parse_call(ToolCallData(id="4", name="speak", arguments_json="[1,2]"))
    assert args == {} and error

    class Broken:
        id = "5"
        name = "x"
        arguments_json = ""

        def parse_args_checked(self):
            raise RuntimeError("boom")

    args, error = parse_call(Broken())  # type: ignore[arg-type]
    assert args == {} and "boom" in (error or "")


def test_error_texts_are_json_with_chinese_text():
    parsed = json.loads(args_error_text("speak", "bad json"))
    assert parsed["ok"] is False and "speak" in parsed["error"] and "bad json" in parsed["error"]
    parsed = json.loads(tool_failure_text("speak", ValueError("nope")))
    assert parsed["ok"] is False and "ValueError" in parsed["error"] and "nope" in parsed["error"]
