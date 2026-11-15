"""Binding routing: provider/model resolution, adapters, per-provider client cache."""

from __future__ import annotations

import asyncio
import logging
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from openai import APIStatusError

from app.engine.llm import LLMClient
from app.providers.models import ModelNotFound, ProviderNotFound
from test_llm_support import (
    ANTHROPIC_SDK,
    CHAT_SDK,
    RESPONSES_SDK,
    FakeStore,
    SharedScriptFactory,
    base_cfg,
    ev,
    make_binding,
    make_chunk,
    make_provider,
    make_request,
    ns,
    status_error,
)

MESSAGES = [{"role": "user", "content": "hi"}]


def chat_ok(text: str = "ok") -> list:
    return [make_chunk(text, finish="stop", usage=None), make_chunk(no_choices=True, usage=(11, 7))]


def responses_ok(text: str = "ok") -> list:
    return [
        ev("response.output_text.delta", delta=text),
        ev("response.completed", response=ns(output=[], usage=ns(input_tokens=11, output_tokens=7))),
    ]


def anthropic_ok(text: str = "ok") -> list:
    return [
        ev("message_start", message=ns(usage=ns(input_tokens=11, output_tokens=0))),
        ev("content_block_start", index=0, content_block=ns(type="text", text=text)),
        ev("message_delta", delta=ns(stop_reason="end_turn"), usage=ns(output_tokens=7)),
    ]


class ChatBindingTests(unittest.IsolatedAsyncioTestCase):
    def _setup(self, provider=None, **cfg):
        provider = provider or make_provider("openai_chat")
        store = FakeStore(provider)
        llm = LLMClient(base_cfg(**cfg), provider_store=store)
        factory = SharedScriptFactory()
        patcher = patch(CHAT_SDK, new=factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        return llm, factory, store, provider

    async def test_request_uses_the_bound_model_depth_and_provider_endpoint(self) -> None:
        llm, factory, _, _ = self._setup()
        factory.script[:] = [chat_ok()]
        await llm.chat_stream(MESSAGES, binding=make_binding(depth="xhigh"))
        inst = factory.instances[0]
        self.assertEqual(inst.init_kwargs["base_url"], "https://api.example.test/v1")
        self.assertEqual(inst.init_kwargs["api_key"], "sk-test-secret")
        call = inst.calls[0]
        self.assertEqual(call["model"], "model-a")
        self.assertEqual(call["reasoning_effort"], "xhigh")
        self.assertNotIn("extra_body", call)  # global extra_body is DeepSeek-only

    async def test_deepseek_endpoint_gets_the_configured_extra_body(self) -> None:
        provider = make_provider("openai_chat", base_url="https://api.deepseek.com")
        llm, factory, _, _ = self._setup(
            provider, thinking={"enabled": True, "extra_body": {"thinking": {"type": "enabled"}}}
        )
        factory.script[:] = [chat_ok()]
        await llm.chat_stream(MESSAGES, binding=make_binding())
        self.assertEqual(factory.instances[0].calls[0]["extra_body"], {"thinking": {"type": "enabled"}})

    async def test_response_carries_identity_price_snapshot_and_window(self) -> None:
        llm, factory, _, _ = self._setup(pricing={"input_price_per_m": 99, "output_price_per_m": 99})
        factory.script[:] = [chat_ok()]
        resp = await llm.chat_stream(MESSAGES, binding=make_binding(window=64_000))
        self.assertEqual((resp.provider_id, resp.model_id), ("prov1", "model-a"))
        # prices come from the ModelEntry, never from the global config
        self.assertEqual((resp.input_price_per_m, resp.output_price_per_m), (1.5, 6.0))
        # the smaller of binding window (64k) and model window (200k)
        self.assertEqual(resp.context_window_tokens, 64_000)
        self.assertEqual((resp.prompt_tokens, resp.completion_tokens), (11, 7))

    async def test_window_falls_back_to_the_model_entry_when_binding_is_larger(self) -> None:
        llm, factory, _, _ = self._setup()
        factory.script[:] = [chat_ok()]
        resp = await llm.chat_stream(MESSAGES, binding=make_binding(window=1_000_000))
        self.assertEqual(resp.context_window_tokens, 200_000)

    async def test_provider_private_and_underscore_keys_are_not_sent(self) -> None:
        llm, factory, _, _ = self._setup()
        factory.script[:] = [chat_ok()]
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "x",
                "tool_calls": [{"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
                "_provider_private": {"blocks": ["SECRET-BLOCK"]},
            },
            {"role": "tool", "tool_call_id": "c", "content": "r"},
        ]
        await llm.chat_stream(messages, binding=make_binding())
        sent = factory.instances[0].calls[0]["messages"]
        self.assertNotIn("_provider_private", repr(sent))
        self.assertNotIn("SECRET-BLOCK", repr(sent))
        self.assertIn("_provider_private", messages[1])  # caller's list is untouched

    async def test_moderator_uses_moderator_temperature_but_the_bound_model(self) -> None:
        llm, factory, _, _ = self._setup(
            temperature=0.7, moderator_override={"model": "ignored-model", "temperature": 0.2}
        )
        factory.script[:] = [chat_ok(), chat_ok()]
        await llm.chat_stream(MESSAGES, binding=make_binding(), is_moderator=True)
        await llm.chat_stream(MESSAGES, binding=make_binding(), is_moderator=False)
        first, second = factory.instances[0].calls
        self.assertEqual((first["model"], first["temperature"]), ("model-a", 0.2))
        self.assertEqual((second["model"], second["temperature"]), ("model-a", 0.7))

    async def test_session_reasoning_effort_does_not_override_a_binding(self) -> None:
        llm, factory, _, _ = self._setup()
        llm.reasoning_effort = "low"  # legacy global knob
        factory.script[:] = [chat_ok()]
        await llm.chat_stream(MESSAGES, binding=make_binding(depth="medium"))
        self.assertEqual(factory.instances[0].calls[0]["reasoning_effort"], "medium")

    async def test_unknown_provider_or_model_raises_the_store_error(self) -> None:
        llm, factory, _, _ = self._setup()
        with self.assertRaises(ProviderNotFound):
            await llm.chat_stream(MESSAGES, binding=make_binding(provider_id="nope"))
        with self.assertRaises(ModelNotFound):
            await llm.chat_stream(MESSAGES, binding=make_binding(model_id="nope"))
        self.assertEqual(factory.instances, [])

    async def test_provider_without_key_raises_a_clear_error(self) -> None:
        provider = make_provider("openai_chat", api_key="  ")
        llm, factory, _, _ = self._setup(provider)
        with self.assertRaises(RuntimeError) as raised:
            await llm.chat_stream(MESSAGES, binding=make_binding())
        self.assertIn("API Key", str(raised.exception))
        self.assertIn(provider.name, str(raised.exception))
        self.assertEqual(factory.instances, [])

    async def test_without_a_binding_the_cfg_fallback_is_used(self) -> None:
        llm, factory, _, _ = self._setup(base_url="https://legacy.example")
        factory.script[:] = [chat_ok()]
        resp = await llm.chat_stream(MESSAGES)
        self.assertEqual(factory.instances[0].init_kwargs["base_url"], "https://legacy.example")
        self.assertEqual(factory.instances[0].calls[0]["model"], "test-model")
        self.assertEqual(resp.provider_id, "legacy")
        self.assertEqual((resp.input_price_per_m, resp.output_price_per_m), (0.27, 1.10))

    async def test_default_store_is_the_process_wide_provider_store(self) -> None:
        provider = make_provider("openai_chat")
        llm = LLMClient(base_cfg())  # no provider_store injected
        factory = SharedScriptFactory([chat_ok()])
        with (
            patch(CHAT_SDK, new=factory),
            patch("app.providers.store.get_provider_store", return_value=FakeStore(provider)),
        ):
            resp = await llm.chat_stream(MESSAGES, binding=make_binding())
        self.assertEqual(resp.model_id, "model-a")


class ClientCachePerProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_provider_has_its_own_client_and_models_share_it(self) -> None:
        a = make_provider("openai_chat", id="A", models=(("m1", 1, 2, 1000), ("m2", 3, 4, 1000)))
        b = make_provider("openai_chat", id="B", base_url="https://other.example/v1", api_key="sk-other-key")
        llm = LLMClient(base_cfg(), provider_store=FakeStore(a, b))
        factory = SharedScriptFactory([chat_ok() for _ in range(5)])
        with patch(CHAT_SDK, new=factory):
            await llm.chat_stream(MESSAGES, binding=make_binding("A", "m1"))
            await llm.chat_stream(MESSAGES, binding=make_binding("A", "m2"))
            await llm.chat_stream(MESSAGES, binding=make_binding("B", "model-a"))
            await llm.chat_stream(MESSAGES, binding=make_binding("A", "m1"))
            await llm.chat_stream(MESSAGES, binding=make_binding("B", "model-a"))
        self.assertEqual(len(factory.instances), 2)
        self.assertEqual(
            sorted(i.init_kwargs["base_url"] for i in factory.instances),
            ["https://api.example.test/v1", "https://other.example/v1"],
        )
        self.assertTrue(all(i.closed == 0 for i in factory.instances))
        await llm.aclose()
        self.assertTrue(all(i.closed == 1 for i in factory.instances))

    async def test_key_rotation_rebuilds_only_that_provider(self) -> None:
        a = make_provider("openai_chat", id="A")
        b = make_provider("openai_chat", id="B", api_key="sk-b-key-123")
        store = FakeStore(a, b)
        llm = LLMClient(base_cfg(), provider_store=store)
        factory = SharedScriptFactory([chat_ok() for _ in range(4)])
        with patch(CHAT_SDK, new=factory):
            await llm.chat_stream(MESSAGES, binding=make_binding("A"))
            await llm.chat_stream(MESSAGES, binding=make_binding("B"))
            store.providers["A"] = a.model_copy(update={"api_key": "sk-rotated-key"})
            await llm.chat_stream(MESSAGES, binding=make_binding("A"))
            await llm.chat_stream(MESSAGES, binding=make_binding("B"))
        self.assertEqual(len(factory.instances), 3)
        first_a, first_b, new_a = factory.instances
        self.assertEqual(first_a.closed, 1)  # old A client retired
        self.assertEqual(first_b.closed, 0)
        self.assertEqual(new_a.init_kwargs["api_key"], "sk-rotated-key")

    async def test_protocol_change_on_the_same_provider_id_rebuilds(self) -> None:
        a = make_provider("openai_chat", id="A")
        store = FakeStore(a)
        llm = LLMClient(base_cfg(), provider_store=store)
        chat_factory = SharedScriptFactory([chat_ok()])
        resp_factory = SharedScriptFactory([responses_ok()])
        with patch(CHAT_SDK, new=chat_factory), patch(RESPONSES_SDK, new=resp_factory):
            await llm.chat_stream(MESSAGES, binding=make_binding("A"))
            store.providers["A"] = a.model_copy(update={"protocol": "openai_responses"})
            await llm.chat_stream(MESSAGES, binding=make_binding("A"))
        self.assertEqual(chat_factory.instances[0].closed, 1)
        self.assertEqual(len(resp_factory.instances), 1)

    async def test_three_protocols_run_concurrently(self) -> None:
        store = FakeStore(
            make_provider("openai_chat", id="C"),
            make_provider("openai_responses", id="R"),
            make_provider("anthropic_messages", id="N"),
        )
        llm = LLMClient(base_cfg(), provider_store=store)
        chat_f = SharedScriptFactory([chat_ok("from-chat")])
        resp_f = SharedScriptFactory([responses_ok("from-responses")])
        anth_f = SharedScriptFactory([anthropic_ok("from-anthropic")])
        with patch(CHAT_SDK, new=chat_f), patch(RESPONSES_SDK, new=resp_f), patch(ANTHROPIC_SDK, new=anth_f):
            results = await asyncio.gather(
                llm.chat_stream(MESSAGES, binding=make_binding("C")),
                llm.chat_stream(MESSAGES, binding=make_binding("R")),
                llm.chat_stream(MESSAGES, binding=make_binding("N")),
            )
        self.assertEqual([r.content for r in results], ["from-chat", "from-responses", "from-anthropic"])
        self.assertEqual([r.provider_id for r in results], ["C", "R", "N"])
        self.assertEqual([(r.prompt_tokens, r.completion_tokens) for r in results], [(11, 7)] * 3)
        self.assertEqual(len(chat_f.instances[0].calls), 1)
        self.assertIn("messages", chat_f.instances[0].calls[0])
        self.assertIn("input", resp_f.instances[0].calls[0])
        self.assertIn("max_tokens", anth_f.instances[0].calls[0])


class RetryAndRedactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_applies_to_every_protocol(self) -> None:
        cases = (
            ("openai_chat", CHAT_SDK, chat_ok),
            ("openai_responses", RESPONSES_SDK, responses_ok),
            ("anthropic_messages", ANTHROPIC_SDK, anthropic_ok),
        )
        for protocol, sdk, ok in cases:
            with self.subTest(protocol=protocol):
                store = FakeStore(make_provider(protocol))
                llm = LLMClient(base_cfg(), provider_store=store)
                factory = SharedScriptFactory([status_error(503), status_error(429), ok("done")])
                sleep = AsyncMock()
                with patch(sdk, new=factory), patch("app.llm.common.asyncio.sleep", new=sleep):
                    resp = await llm.chat_stream(MESSAGES, binding=make_binding())
                self.assertEqual(resp.content, "done")
                self.assertEqual(sleep.await_count, 2)

    async def test_non_retryable_status_is_not_retried_on_any_protocol(self) -> None:
        for protocol, sdk in (
            ("openai_chat", CHAT_SDK),
            ("openai_responses", RESPONSES_SDK),
            ("anthropic_messages", ANTHROPIC_SDK),
        ):
            with self.subTest(protocol=protocol):
                llm = LLMClient(base_cfg(), provider_store=FakeStore(make_provider(protocol)))
                factory = SharedScriptFactory([status_error(401), chat_ok()])
                with patch(sdk, new=factory), patch("app.llm.common.asyncio.sleep", new=AsyncMock()) as sleep:
                    with self.assertRaises(APIStatusError):
                        await llm.chat_stream(MESSAGES, binding=make_binding())
                sleep.assert_not_awaited()
                self.assertEqual(len(factory.instances[0].calls), 1)

    async def test_api_key_is_scrubbed_from_error_text(self) -> None:
        secret = "sk-test-secret"
        err = APIStatusError(
            f"Incorrect API key provided: {secret}",
            response=httpx.Response(401, request=make_request()),
            body={"error": {"message": f"bad key {secret}"}},
        )
        llm = LLMClient(base_cfg(), provider_store=FakeStore(make_provider("openai_chat", api_key=secret)))
        factory = SharedScriptFactory([err])
        with patch(CHAT_SDK, new=factory):
            with self.assertRaises(APIStatusError) as raised:
                await llm.chat_stream(MESSAGES, binding=make_binding())
        self.assertIs(raised.exception, err)
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, repr(raised.exception.body))
        self.assertIn("***", str(raised.exception))

    async def test_retry_logs_do_not_contain_keys_or_upstream_text(self) -> None:
        secret = "sk-test-secret"
        err = status_error(503, message=f"gateway says {secret}")
        llm = LLMClient(base_cfg(), provider_store=FakeStore(make_provider("openai_chat", api_key=secret)))
        factory = SharedScriptFactory([err, chat_ok()])
        with (
            patch(CHAT_SDK, new=factory),
            patch("app.llm.common.asyncio.sleep", new=AsyncMock()),
            self.assertLogs("app.llm.common", level=logging.WARNING) as logs,
        ):
            await llm.chat_stream(MESSAGES, binding=make_binding())
        joined = "\n".join(logs.output)
        self.assertNotIn(secret, joined)
        self.assertNotIn("gateway says", joined)
        self.assertIn("503", joined)


if __name__ == "__main__":
    unittest.main()
