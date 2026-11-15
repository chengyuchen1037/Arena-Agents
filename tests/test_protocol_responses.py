"""OpenAI Responses adapter: request shape and streaming events (SDK mocked)."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx
from openai import BadRequestError

from app.engine.llm import LLMClient
from app.llm.common import UpstreamStreamError
from test_llm_support import (
    RESPONSES_SDK,
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


def _client(depth: str = "high", **cfg) -> tuple[LLMClient, SharedScriptFactory]:
    provider = make_provider("openai_responses")
    llm = LLMClient(base_cfg(**cfg), provider_store=FakeStore(provider))
    factory = SharedScriptFactory()
    return llm, factory


def _binding(depth: str = "high"):
    return make_binding(depth=depth)


def _tool_stream() -> list:
    final_items = [
        ns(type="reasoning", id="rs_1", summary=[ns(type="summary_text", text="想一想")], encrypted_content="ENC"),
        ns(type="function_call", id="fc_1", call_id="call_x", name="speak", arguments='{"text":"hi"}'),
    ]
    return [
        ev("response.reasoning_summary_part.added"),
        ev("response.reasoning_summary_text.delta", delta="想"),
        ev("response.reasoning_summary_text.delta", delta="一想"),
        ev("response.output_text.delta", delta="好"),
        ev("response.output_text.delta", delta="的"),
        ev(
            "response.output_item.added",
            output_index=2,
            item=ns(type="function_call", id="fc_1", call_id="call_x", name="speak", arguments=""),
        ),
        ev("response.function_call_arguments.delta", output_index=2, item_id="fc_1", delta='{"text"'),
        ev("response.function_call_arguments.delta", output_index=2, item_id="fc_1", delta=':"hi"}'),
        ev("response.function_call_arguments.done", output_index=2, arguments='{"text":"hi"}', name="speak"),
        ev("response.output_item.done", output_index=0, item=final_items[0]),
        ev("response.output_item.done", output_index=2, item=final_items[1]),
        ev(
            "response.completed",
            response=ns(output=final_items, usage=ns(input_tokens=42, output_tokens=17), status="completed"),
        ),
    ]


class ResponsesRequestTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, depth="high", tools=TOOLS, messages=None, **kw):
        llm, factory = _client()
        factory.script[:] = [[ev("response.completed", response=ns(output=[], usage=None))]]
        with patch(RESPONSES_SDK, new=factory):
            resp = await llm.chat_stream(
                messages or [{"role": "system", "content": "规则"}, {"role": "user", "content": "hi"}],
                tools=tools,
                binding=_binding(depth),
                **kw,
            )
        return factory.instances[0].calls[0], resp, factory

    async def test_request_shape(self) -> None:
        request, _, factory = await self._call(max_tokens_override=777)
        self.assertEqual(request["model"], "model-a")
        self.assertEqual(request["instructions"], "规则")
        self.assertEqual(request["input"], [{"role": "user", "content": "hi"}])
        self.assertTrue(request["stream"])
        self.assertIs(request["store"], False)
        self.assertEqual(request["max_output_tokens"], 777)
        self.assertEqual(request["tool_choice"], "auto")
        self.assertEqual(
            request["tools"],
            [
                {
                    "type": "function",
                    "name": "speak",
                    "description": "发言",
                    "parameters": TOOLS[0]["function"]["parameters"],
                }
            ],
        )
        self.assertNotIn("messages", request)
        self.assertEqual(factory.instances[0].init_kwargs["base_url"], "https://api.example.test/v1")

    async def test_reasoning_effort_follows_depth_and_drops_temperature(self) -> None:
        for depth in ("low", "medium", "high", "xhigh"):
            with self.subTest(depth=depth):
                request, _, _ = await self._call(depth=depth)
                self.assertEqual(request["reasoning"]["effort"], depth)
                self.assertEqual(request["include"], ["reasoning.encrypted_content"])
                self.assertNotIn("temperature", request)

    async def test_thinking_disabled_sends_temperature_instead(self) -> None:
        llm = LLMClient(
            base_cfg(thinking={"enabled": False}, temperature=0.4),
            provider_store=FakeStore(make_provider("openai_responses")),
        )
        factory = SharedScriptFactory([[ev("response.completed", response=ns(output=[], usage=None))]])
        with patch(RESPONSES_SDK, new=factory):
            await llm.chat_stream([{"role": "user", "content": "hi"}], binding=_binding())
        request = factory.instances[0].calls[0]
        self.assertNotIn("reasoning", request)
        self.assertNotIn("include", request)
        self.assertEqual(request["temperature"], 0.4)

    async def test_no_tools_means_no_tool_fields(self) -> None:
        request, _, _ = await self._call(tools=None)
        self.assertNotIn("tools", request)
        self.assertNotIn("tool_choice", request)

    async def test_private_keys_never_reach_the_request(self) -> None:
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "hello",
                "_provider_private": {"provider_id": "other", "items": [{"secret": 1}]},
            },
            {"role": "user", "content": "again"},
        ]
        request, _, _ = await self._call(messages=messages)
        self.assertNotIn("_provider_private", repr(request))
        self.assertNotIn("secret", repr(request))

    async def test_rejected_reasoning_params_are_downgraded(self) -> None:
        llm, factory = _client()
        err = BadRequestError(
            "Unsupported parameter: 'reasoning.effort'",
            response=httpx.Response(400, request=make_request()),
            body={"error": {"message": "Unsupported parameter: 'reasoning.effort'"}},
        )
        factory.script[:] = [err, [ev("response.completed", response=ns(output=[], usage=None))]]
        with patch(RESPONSES_SDK, new=factory):
            await llm.chat_stream([{"role": "user", "content": "hi"}], binding=_binding())
        first, second = factory.instances[0].calls
        self.assertIn("reasoning", first)
        self.assertNotIn("reasoning", second)
        self.assertNotIn("include", second)


class ResponsesStreamTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, events, on_chunk=None):
        llm, factory = _client()
        factory.script[:] = [events]
        with patch(RESPONSES_SDK, new=factory):
            return await llm.chat_stream(
                [{"role": "user", "content": "hi"}], tools=TOOLS, binding=_binding(), on_chunk=on_chunk
            )

    async def test_text_thought_tool_calls_and_usage(self) -> None:
        seen: list[tuple[str, str]] = []

        async def on_chunk(thought: str, content: str) -> None:
            seen.append((thought, content))

        resp = await self._run(_tool_stream(), on_chunk)
        self.assertEqual(resp.content, "好的")
        self.assertEqual(resp.thought, "想一想")
        self.assertEqual([(c.id, c.name, c.arguments_json) for c in resp.tool_calls],
                         [("call_x", "speak", '{"text":"hi"}')])
        self.assertEqual(resp.tool_calls[0].parse_args(), {"text": "hi"})
        self.assertEqual((resp.prompt_tokens, resp.completion_tokens), (42, 17))
        self.assertEqual(resp.finish_reason, "tool_calls")
        self.assertEqual(seen[0], ("想", ""))
        self.assertEqual(seen[-1], ("想一想", "好的"))
        self.assertEqual(len(seen), 4)  # one per text / reasoning delta

    async def test_reasoning_items_are_kept_as_provider_private(self) -> None:
        resp = await self._run(_tool_stream())
        private = resp.provider_private
        self.assertEqual(private["provider_id"], "prov1")
        self.assertEqual(private["protocol"], "openai_responses")
        self.assertEqual(private["model_id"], "model-a")
        self.assertEqual(private["call_ids"], ["call_x"])
        self.assertEqual([i["type"] for i in private["items"]], ["reasoning", "function_call"])
        self.assertEqual(private["items"][0]["encrypted_content"], "ENC")

    async def test_plain_answer_has_no_private_state(self) -> None:
        events = [
            ev("response.output_text.delta", delta="hello"),
            ev(
                "response.completed",
                response=ns(
                    output=[ns(type="message", content=[ns(type="output_text", text="hello")])],
                    usage=ns(input_tokens=3, output_tokens=2),
                ),
            ),
        ]
        resp = await self._run(events)
        self.assertEqual(resp.content, "hello")
        self.assertEqual(resp.provider_private, {})
        self.assertEqual(resp.finish_reason, "stop")
        self.assertEqual(resp.tool_calls, [])

    async def test_final_output_is_used_when_the_gateway_sends_no_deltas(self) -> None:
        events = [
            ev(
                "response.completed",
                response=ns(
                    output=[
                        ns(type="message", content=[ns(type="output_text", text="全文")]),
                        ns(type="function_call", id="fc", call_id="c1", name="speak", arguments='{"text":"a"}'),
                    ],
                    usage=ns(input_tokens=1, output_tokens=1),
                ),
            )
        ]
        resp = await self._run(events)
        self.assertEqual(resp.content, "全文")
        self.assertEqual([(c.id, c.name) for c in resp.tool_calls], [("c1", "speak")])
        self.assertEqual(resp.tool_calls[0].arguments_json, '{"text":"a"}')

    async def test_incomplete_max_tokens_maps_to_length(self) -> None:
        events = [
            ev("response.output_text.delta", delta="cut"),
            ev(
                "response.incomplete",
                response=ns(output=[], usage=None, incomplete_details=ns(reason="max_output_tokens")),
            ),
        ]
        resp = await self._run(events)
        self.assertEqual(resp.finish_reason, "length")
        self.assertGreater(resp.prompt_tokens, 0)  # estimated when usage is missing
        self.assertGreater(resp.completion_tokens, 0)

    async def test_failed_response_is_retried_only_before_output(self) -> None:
        llm, factory = _client()
        failed = ev("response.failed", response=ns(error=ns(code="server_error", message="sensitive text")))
        ok = [ev("response.output_text.delta", delta="ok"), ev("response.completed", response=ns(output=[], usage=None))]
        factory.script[:] = [[failed], ok]
        with patch(RESPONSES_SDK, new=factory), patch("app.llm.common.asyncio.sleep", new=AsyncMock()):
            resp = await llm.chat_stream([{"role": "user", "content": "hi"}], binding=_binding())
        self.assertEqual(resp.content, "ok")
        self.assertEqual(len(factory.instances[0].calls), 2)

        llm, factory = _client()
        factory.script[:] = [[ev("response.output_text.delta", delta="partial"), failed], ok]
        with patch(RESPONSES_SDK, new=factory), patch("app.llm.common.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(UpstreamStreamError) as raised:
                await llm.chat_stream([{"role": "user", "content": "hi"}], binding=_binding())
        self.assertNotIn("sensitive text", str(raised.exception))  # upstream text is not echoed
        self.assertEqual(len(factory.instances[0].calls), 1)

    async def test_non_retryable_error_event_is_raised_immediately(self) -> None:
        llm, factory = _client()
        factory.script[:] = [[ev("error", code="invalid_prompt", message="x")], []]
        with patch(RESPONSES_SDK, new=factory), patch("app.llm.common.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(UpstreamStreamError):
                await llm.chat_stream([{"role": "user", "content": "hi"}], binding=_binding())
        self.assertEqual(len(factory.instances[0].calls), 1)

    async def test_reasoning_roundtrip_replays_items_on_the_next_request(self) -> None:
        llm, factory = _client()
        factory.script[:] = [_tool_stream(), [ev("response.completed", response=ns(output=[], usage=None))]]
        messages = [{"role": "user", "content": "hi"}]
        with patch(RESPONSES_SDK, new=factory):
            first = await llm.chat_stream(messages, tools=TOOLS, binding=_binding())
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
            messages.append({"role": "tool", "tool_call_id": "call_x", "content": "sent"})
            await llm.chat_stream(messages, tools=TOOLS, binding=_binding())
        second_input = factory.instances[0].calls[1]["input"]
        self.assertEqual([i.get("type", i.get("role")) for i in second_input],
                         ["user", "reasoning", "function_call", "function_call_output"])
        self.assertEqual(second_input[1]["encrypted_content"], "ENC")
        self.assertEqual(second_input[2]["id"], "fc_1")
        self.assertEqual(second_input[3], {"type": "function_call_output", "call_id": "call_x", "output": "sent"})


if __name__ == "__main__":
    unittest.main()
