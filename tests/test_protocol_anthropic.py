"""Anthropic Messages adapter: request shape, thinking budget and streaming (SDK mocked)."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx
from anthropic import BadRequestError

from app.engine.llm import LLMClient
from app.llm.anthropic_messages import (
    THINKING_BUDGETS,
    AnthropicMessagesAdapter,
    normalize_base_url,
)
from app.llm.common import UpstreamStreamError, is_retryable_error
from test_llm_support import (
    ANTHROPIC_SDK,
    FakeStore,
    SharedScriptFactory,
    base_cfg,
    ev,
    make_binding,
    make_provider,
    make_request,
    ns,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "speak",
            "description": "发言",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
        },
    }
]


def _client(base_url: str = "https://api.anthropic.example", **cfg) -> tuple[LLMClient, SharedScriptFactory]:
    provider = make_provider("anthropic_messages", base_url=base_url)
    return LLMClient(base_cfg(**cfg), provider_store=FakeStore(provider)), SharedScriptFactory()


def _empty_stream() -> list:
    return [
        ev("message_start", message=ns(usage=ns(input_tokens=5, output_tokens=0))),
        ev("message_delta", delta=ns(stop_reason="end_turn"), usage=ns(output_tokens=1)),
        ev("message_stop"),
    ]


def _thinking_tool_stream() -> list:
    return [
        ev(
            "message_start",
            message=ns(usage=ns(input_tokens=100, cache_creation_input_tokens=10, cache_read_input_tokens=5, output_tokens=1)),
        ),
        ev("content_block_start", index=0, content_block=ns(type="thinking", thinking="")),
        ev("content_block_delta", index=0, delta=ns(type="thinking_delta", thinking="先")),
        ev("content_block_delta", index=0, delta=ns(type="thinking_delta", thinking="想")),
        ev("content_block_delta", index=0, delta=ns(type="signature_delta", signature="SIG")),
        ev("content_block_stop", index=0),
        ev("content_block_start", index=1, content_block=ns(type="redacted_thinking", data="RED")),
        ev("content_block_stop", index=1),
        ev("content_block_start", index=2, content_block=ns(type="text", text="")),
        ev("content_block_delta", index=2, delta=ns(type="text_delta", text="好")),
        ev("content_block_delta", index=2, delta=ns(type="text_delta", text="的")),
        ev("content_block_stop", index=2),
        ev("content_block_start", index=3, content_block=ns(type="tool_use", id="toolu_1", name="speak", input={})),
        ev("content_block_delta", index=3, delta=ns(type="input_json_delta", partial_json='{"text":')),
        ev("content_block_delta", index=3, delta=ns(type="input_json_delta", partial_json='"hi"}')),
        ev("content_block_stop", index=3),
        ev("ping"),
        ev("message_delta", delta=ns(stop_reason="tool_use"), usage=ns(output_tokens=33)),
        ev("message_stop"),
    ]


class AnthropicRequestTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, depth="high", messages=None, tools=TOOLS, base_url="https://api.anthropic.example", **kw):
        llm, factory = _client(base_url)
        factory.script[:] = [_empty_stream()]
        with patch(ANTHROPIC_SDK, new=factory):
            await llm.chat_stream(
                messages or [{"role": "system", "content": "规则"}, {"role": "user", "content": "hi"}],
                tools=tools,
                binding=make_binding(depth=depth),
                **kw,
            )
        return factory.instances[0].calls[0], factory

    async def test_request_shape(self) -> None:
        request, factory = await self._call(max_tokens_override=9000)
        self.assertEqual(request["model"], "model-a")
        # v0.12.1: prompt-cache breakpoints on the system prompt, the last tool and the last message block
        cache = {"type": "ephemeral"}
        self.assertEqual(request["system"], [{"type": "text", "text": "规则", "cache_control": cache}])
        self.assertEqual(
            request["messages"], [{"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": cache}]}]
        )
        self.assertTrue(request["stream"])
        self.assertEqual(request["tool_choice"], {"type": "auto"})
        self.assertEqual(
            request["tools"],
            [{"name": "speak", "description": "发言", "input_schema": TOOLS[0]["function"]["parameters"],
              "cache_control": cache}],
        )
        self.assertEqual(request["max_tokens"], 9000)
        self.assertEqual(factory.instances[0].init_kwargs["max_retries"], 0)

    async def test_thinking_budget_maps_depth(self) -> None:
        self.assertEqual(THINKING_BUDGETS, {"low": 1024, "medium": 2048, "high": 4096, "xhigh": 8192})
        for depth, budget in THINKING_BUDGETS.items():
            with self.subTest(depth=depth):
                request, _ = await self._call(depth=depth, max_tokens_override=20000)
                self.assertEqual(request["thinking"], {"type": "enabled", "budget_tokens": budget})
                self.assertNotIn("temperature", request)  # must be left at 1 with thinking

    async def test_max_tokens_always_exceeds_the_budget(self) -> None:
        for depth, budget in THINKING_BUDGETS.items():
            for asked in (256, 1024, budget, budget + 1, 8192, 100000):
                with self.subTest(depth=depth, asked=asked):
                    request, _ = await self._call(depth=depth, max_tokens_override=asked)
                    self.assertGreater(request["max_tokens"], budget)
                    self.assertGreaterEqual(request["max_tokens"], min(asked, request["max_tokens"]))
        request, _ = await self._call(depth="low", max_tokens_override=100000)
        self.assertEqual(request["max_tokens"], 100000)  # a large ask is not reduced

    async def test_thinking_disabled_uses_temperature_and_no_thinking(self) -> None:
        llm, factory = _client(thinking={"enabled": False}, temperature=0.4)
        factory.script[:] = [_empty_stream()]
        with patch(ANTHROPIC_SDK, new=factory):
            await llm.chat_stream([{"role": "user", "content": "hi"}], binding=make_binding())
        request = factory.instances[0].calls[0]
        self.assertNotIn("thinking", request)
        self.assertEqual(request["temperature"], 0.4)

    async def test_base_url_is_normalised_for_the_sdk(self) -> None:
        for given, expected in (
            ("https://api.anthropic.com", "https://api.anthropic.com"),
            ("https://api.anthropic.com/", "https://api.anthropic.com"),
            ("https://api.anthropic.com/v1", "https://api.anthropic.com"),
            ("https://gw.example/anthropic/v1/", "https://gw.example/anthropic"),
        ):
            self.assertEqual(normalize_base_url(given), expected)
        _, factory = await self._call(base_url="https://gw.example/v1")
        self.assertEqual(factory.instances[0].init_kwargs["base_url"], "https://gw.example")

    async def test_thinking_is_turned_off_when_a_tool_loop_has_no_signed_thinking(self) -> None:
        # The tool-using turn was produced by another provider: nothing to hand back.
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "x",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "speak", "arguments": "{}"}}],
                "_provider_private": {"provider_id": "other", "protocol": "openai_chat", "model_id": "z", "blocks": []},
            },
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        request, _ = await self._call(messages=messages)
        self.assertNotIn("thinking", request)
        self.assertNotIn("_provider_private", repr(request))

    async def test_signed_thinking_is_replayed_and_keeps_thinking_on(self) -> None:
        blocks = [{"type": "thinking", "thinking": "hm", "signature": "SIG"}]
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "x",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "speak", "arguments": "{}"}}],
                "_provider_private": {
                    "provider_id": "prov1",
                    "protocol": "anthropic_messages",
                    "model_id": "model-a",
                    "blocks": blocks,
                },
            },
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        request, _ = await self._call(messages=messages)
        self.assertIn("thinking", request)
        self.assertEqual(request["messages"][1]["content"][0], blocks[0])
        self.assertEqual(request["messages"][2]["content"][0]["type"], "tool_result")

    async def test_plain_history_keeps_thinking_on_without_blocks(self) -> None:
        # Thinking blocks of *finished* turns are not required.
        messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]
        request, _ = await self._call(messages=messages)
        self.assertIn("thinking", request)

    async def test_rejected_thinking_is_downgraded_without_losing_the_request(self) -> None:
        llm, factory = _client()
        msg = "thinking.budget_tokens: not supported for this model"
        err = BadRequestError(msg, response=httpx.Response(400, request=make_request()), body={"error": {"message": msg}})
        factory.script[:] = [err, _empty_stream()]
        with patch(ANTHROPIC_SDK, new=factory):
            await llm.chat_stream([{"role": "user", "content": "hi"}], binding=make_binding())
        first, second = factory.instances[0].calls
        self.assertIn("thinking", first)
        self.assertNotIn("thinking", second)
        self.assertEqual(second["messages"][0]["role"], "user")

    async def test_adapter_downgrade_ignores_unrelated_errors(self) -> None:
        adapter = AnthropicMessagesAdapter()
        request = {"thinking": {"type": "enabled"}, "messages": [{"role": "user", "content": [{"type": "text", "text": "x"}]}]}
        err = BadRequestError(
            "messages: roles must alternate", response=httpx.Response(400, request=make_request()), body=None
        )
        self.assertFalse(adapter.downgrade(err, request))
        self.assertIn("thinking", request)


class AnthropicStreamTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, events, on_chunk=None):
        llm, factory = _client()
        factory.script[:] = [events]
        with patch(ANTHROPIC_SDK, new=factory):
            return await llm.chat_stream(
                [{"role": "user", "content": "hi"}], tools=TOOLS, binding=make_binding(), on_chunk=on_chunk
            )

    async def test_thinking_text_tool_use_usage_and_private_blocks(self) -> None:
        seen: list[tuple[str, str]] = []

        async def on_chunk(thought: str, content: str) -> None:
            seen.append((thought, content))

        resp = await self._run(_thinking_tool_stream(), on_chunk)
        self.assertEqual(resp.thought, "先想")
        self.assertEqual(resp.content, "好的")
        self.assertEqual([(c.id, c.name, c.arguments_json) for c in resp.tool_calls],
                         [("toolu_1", "speak", '{"text":"hi"}')])
        self.assertEqual(resp.finish_reason, "tool_calls")
        # v0.12.1: the three input classes are reported separately (fresh input excludes the cache counters)
        self.assertEqual((resp.prompt_tokens, resp.completion_tokens), (100, 33))
        self.assertEqual((resp.cache_read_tokens, resp.cache_write_tokens), (5, 10))
        self.assertEqual(resp.provider_private["blocks"], [
            {"type": "thinking", "thinking": "先想", "signature": "SIG"},
            {"type": "redacted_thinking", "data": "RED"},
        ])
        self.assertEqual(
            (resp.provider_private["provider_id"], resp.provider_private["protocol"], resp.provider_private["model_id"]),
            ("prov1", "anthropic_messages", "model-a"),
        )
        self.assertEqual(seen[0], ("先", ""))
        self.assertEqual(seen[-1], ("先想", "好的"))

    async def test_unsigned_thinking_is_not_kept_for_replay(self) -> None:
        events = [
            ev("content_block_start", index=0, content_block=ns(type="thinking", thinking="")),
            ev("content_block_delta", index=0, delta=ns(type="thinking_delta", thinking="x")),
            ev("content_block_start", index=1, content_block=ns(type="text", text="")),
            ev("content_block_delta", index=1, delta=ns(type="text_delta", text="hi")),
            ev("message_delta", delta=ns(stop_reason="end_turn"), usage=ns(output_tokens=2)),
        ]
        resp = await self._run(events)
        self.assertEqual(resp.provider_private, {})
        self.assertEqual(resp.finish_reason, "stop")

    async def test_stop_reason_mapping_and_estimated_usage(self) -> None:
        events = [
            ev("content_block_start", index=0, content_block=ns(type="text", text="abc")),
            ev("message_delta", delta=ns(stop_reason="max_tokens"), usage=None),
        ]
        resp = await self._run(events)
        self.assertEqual(resp.content, "abc")
        self.assertEqual(resp.finish_reason, "length")
        self.assertGreater(resp.prompt_tokens, 0)
        self.assertGreater(resp.completion_tokens, 0)

    async def test_multiple_thinking_blocks_are_separated(self) -> None:
        events = [
            ev("content_block_start", index=0, content_block=ns(type="thinking", thinking="")),
            ev("content_block_delta", index=0, delta=ns(type="thinking_delta", thinking="one")),
            ev("content_block_start", index=1, content_block=ns(type="thinking", thinking="")),
            ev("content_block_delta", index=1, delta=ns(type="thinking_delta", thinking="two")),
        ]
        resp = await self._run(events)
        self.assertEqual(resp.thought, "one\n\ntwo")

    async def test_overloaded_error_event_is_retried_before_output_only(self) -> None:
        overloaded = ev("error", error=ns(type="overloaded_error", message="secret detail"))
        good = [ev("content_block_start", index=0, content_block=ns(type="text", text="ok"))]
        llm, factory = _client()
        factory.script[:] = [[overloaded], good]
        with patch(ANTHROPIC_SDK, new=factory), patch("app.llm.common.asyncio.sleep", new=AsyncMock()):
            resp = await llm.chat_stream([{"role": "user", "content": "hi"}], binding=make_binding())
        self.assertEqual(resp.content, "ok")

        llm, factory = _client()
        partial = [ev("content_block_start", index=0, content_block=ns(type="text", text="par")), overloaded]
        factory.script[:] = [partial, good]
        with patch(ANTHROPIC_SDK, new=factory), patch("app.llm.common.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(UpstreamStreamError) as raised:
                await llm.chat_stream([{"role": "user", "content": "hi"}], binding=make_binding())
        self.assertNotIn("secret detail", str(raised.exception))
        self.assertEqual(len(factory.instances[0].calls), 1)

    async def test_invalid_request_error_event_is_not_retried(self) -> None:
        self.assertFalse(is_retryable_error(UpstreamStreamError("x", retryable=False)))
        llm, factory = _client()
        factory.script[:] = [[ev("error", error=ns(type="invalid_request_error", message="m"))], []]
        with patch(ANTHROPIC_SDK, new=factory), patch("app.llm.common.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(UpstreamStreamError):
                await llm.chat_stream([{"role": "user", "content": "hi"}], binding=make_binding())
        self.assertEqual(len(factory.instances[0].calls), 1)

    async def test_anthropic_sdk_errors_are_classified_like_openai_ones(self) -> None:
        import anthropic

        req = make_request()
        self.assertTrue(is_retryable_error(anthropic.APIConnectionError(request=req)))
        self.assertTrue(is_retryable_error(anthropic.APITimeoutError(request=req)))
        overloaded = anthropic.APIStatusError(
            "overloaded", response=httpx.Response(529, request=req), body=None
        )
        self.assertTrue(is_retryable_error(overloaded))
        rate = anthropic.APIStatusError(
            "slow down", response=httpx.Response(429, request=req, headers={"retry-after": "3"}), body=None
        )
        self.assertTrue(is_retryable_error(rate))
        bad = anthropic.APIStatusError("bad", response=httpx.Response(400, request=req), body=None)
        self.assertFalse(is_retryable_error(bad))

    async def test_429_retry_after_is_honoured_for_anthropic(self) -> None:
        import anthropic

        rate = anthropic.APIStatusError(
            "slow", response=httpx.Response(429, request=make_request(), headers={"retry-after": "4"}), body=None
        )
        llm, factory = _client()
        factory.script[:] = [rate, _empty_stream()]
        sleep = AsyncMock()
        with patch(ANTHROPIC_SDK, new=factory), patch("app.llm.common.asyncio.sleep", new=sleep):
            await llm.chat_stream([{"role": "user", "content": "hi"}], binding=make_binding())
        sleep.assert_awaited_once_with(4.0)

    async def test_tool_loop_roundtrip(self) -> None:
        llm, factory = _client()
        factory.script[:] = [_thinking_tool_stream(), _empty_stream()]
        messages = [{"role": "user", "content": "hi"}]
        with patch(ANTHROPIC_SDK, new=factory):
            first = await llm.chat_stream(messages, tools=TOOLS, binding=make_binding())
            messages.append(
                {
                    "role": "assistant",
                    "content": first.content,
                    "tool_calls": [
                        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments_json}}
                        for c in first.tool_calls
                    ],
                    "_provider_private": first.provider_private,
                }
            )
            messages.append({"role": "tool", "tool_call_id": "toolu_1", "content": "sent"})
            await llm.chat_stream(messages, tools=TOOLS, binding=make_binding())
        second = factory.instances[0].calls[1]
        assistant = second["messages"][1]
        self.assertEqual([b["type"] for b in assistant["content"]],
                         ["thinking", "redacted_thinking", "text", "tool_use"])
        self.assertEqual(assistant["content"][0]["signature"], "SIG")
        self.assertEqual(assistant["content"][3]["input"], {"text": "hi"})
        self.assertEqual(second["messages"][2]["content"],
                         [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "sent",
                           "cache_control": {"type": "ephemeral"}}])
        self.assertIn("thinking", second)


if __name__ == "__main__":
    unittest.main()
