"""Client reuse, request parameters and defaults of ``LLMClient`` (no network)."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import patch

from app.config import BASE_DIR, load_config
from app.engine.llm import (
    DEFAULT_BASE_URL,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    LLMClient,
)
from test_llm_support import CHAT_SDK, SharedScriptFactory, base_cfg, make_chunk

MESSAGES = [{"role": "user", "content": "hi"}]


def _ok() -> list[Any]:
    return [make_chunk("ok", finish="stop")]


class _FactoryCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.factory = SharedScriptFactory()
        patcher = patch(CHAT_SDK, new=self.factory)
        patcher.start()
        self.addCleanup(patcher.stop)


class ClientReuseTests(_FactoryCase):
    async def test_sequential_calls_share_one_client(self) -> None:
        llm = LLMClient(base_cfg())
        self.factory.script[:] = [_ok(), _ok(), _ok()]
        for _ in range(3):
            await llm.chat_stream(MESSAGES)
        self.assertEqual(len(self.factory.instances), 1)
        self.assertEqual(self.factory.instances[0].closed, 0)
        self.assertEqual(len(self.factory.instances[0].calls), 3)

    async def test_concurrent_calls_share_one_client(self) -> None:
        llm = LLMClient(base_cfg())
        self.factory.script[:] = [_ok() for _ in range(8)]
        results = await asyncio.gather(*(llm.chat_stream(MESSAGES) for _ in range(8)))
        self.assertTrue(all(r.content == "ok" for r in results))
        self.assertEqual(len(self.factory.instances), 1)
        self.assertEqual(self.factory.instances[0].closed, 0)

    async def test_client_is_created_with_configured_endpoint(self) -> None:
        llm = LLMClient(
            base_cfg(base_url="https://gateway.example/api", timeout_seconds=33)
        )
        self.factory.script[:] = [_ok()]
        await llm.chat_stream(MESSAGES)
        kwargs = self.factory.instances[0].init_kwargs
        self.assertEqual(kwargs["base_url"], "https://gateway.example/api")
        self.assertEqual(kwargs["api_key"], "test-key")
        self.assertEqual(kwargs["timeout"], 33.0)
        self.assertEqual(kwargs["max_retries"], 0)

    async def test_client_is_rebuilt_and_old_closed_when_api_key_changes(self) -> None:
        llm = LLMClient(base_cfg())
        self.factory.script[:] = [_ok(), _ok()]
        await llm.chat_stream(MESSAGES)
        llm.api_key = "another-key"
        await llm.chat_stream(MESSAGES)
        first, second = self.factory.instances
        self.assertEqual(first.closed, 1)
        self.assertEqual(second.closed, 0)
        self.assertEqual(second.init_kwargs["api_key"], "another-key")

    async def test_client_is_rebuilt_when_base_url_changes(self) -> None:
        llm = LLMClient(base_cfg())
        self.factory.script[:] = [_ok(), _ok()]
        await llm.chat_stream(MESSAGES)
        llm.base_url = "https://other.example"
        await llm.chat_stream(MESSAGES)
        self.assertEqual(len(self.factory.instances), 2)
        self.assertEqual(self.factory.instances[0].closed, 1)
        self.assertEqual(self.factory.instances[1].init_kwargs["base_url"], "https://other.example")

    async def test_unrelated_changes_do_not_rebuild_the_client(self) -> None:
        llm = LLMClient(base_cfg())
        self.factory.script[:] = [_ok(), _ok()]
        await llm.chat_stream(MESSAGES)
        llm.default_model = "m2"
        llm.reasoning_effort = "low"
        llm.temperature = 0.1
        llm.input_price_per_m = 9.0
        llm.context_window_tokens = 5000
        await llm.chat_stream(MESSAGES)
        self.assertEqual(len(self.factory.instances), 1)

    async def test_reconfigure_with_same_endpoint_keeps_client(self) -> None:
        llm = LLMClient(base_cfg())
        self.factory.script[:] = [_ok(), _ok()]
        await llm.chat_stream(MESSAGES)
        llm.reconfigure(base_cfg(temperature=0.2, default_model="m3"))
        self.assertEqual(llm.default_model, "m3")
        await llm.chat_stream(MESSAGES)
        self.assertEqual(len(self.factory.instances), 1)
        self.assertEqual(self.factory.instances[0].calls[-1]["model"], "m3")

    async def test_reconfigure_with_new_key_rebuilds_and_closes_old(self) -> None:
        llm = LLMClient(base_cfg())
        self.factory.script[:] = [_ok(), _ok()]
        await llm.chat_stream(MESSAGES)
        llm.reconfigure(base_cfg(api_key="rotated"))
        await llm.chat_stream(MESSAGES)
        self.assertEqual(len(self.factory.instances), 2)
        self.assertEqual(self.factory.instances[0].closed, 1)

    async def test_old_client_is_closed_only_after_inflight_requests_finish(self) -> None:
        llm = LLMClient(base_cfg())
        self.factory.gate = asyncio.Event()
        self.factory.script[:] = [_ok(), _ok()]
        first_call = asyncio.ensure_future(llm.chat_stream(MESSAGES))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(len(self.factory.instances), 1)  # first request is blocked in create()

        llm.api_key = "rotated"
        second_call = asyncio.ensure_future(llm.chat_stream(MESSAGES))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(len(self.factory.instances), 2)
        self.assertEqual(self.factory.instances[0].closed, 0)  # still serving request #1

        self.factory.gate.set()
        await asyncio.gather(first_call, second_call)
        self.assertEqual(self.factory.instances[0].closed, 1)
        self.assertEqual(self.factory.instances[1].closed, 0)

    async def test_aclose_closes_and_next_call_recreates(self) -> None:
        llm = LLMClient(base_cfg())
        self.factory.script[:] = [_ok(), _ok()]
        await llm.chat_stream(MESSAGES)
        await llm.aclose()
        await llm.aclose()  # idempotent
        self.assertEqual(self.factory.instances[0].closed, 1)
        await llm.chat_stream(MESSAGES)
        self.assertEqual(len(self.factory.instances), 2)

    async def test_failed_request_does_not_leak_the_client_handle(self) -> None:
        llm = LLMClient(base_cfg(retry={"max_attempts": 1}))
        self.factory.script[:] = [RuntimeError("boom"), _ok()]
        with self.assertRaises(RuntimeError):
            await llm.chat_stream(MESSAGES)
        llm.api_key = "rotated"
        await llm.chat_stream(MESSAGES)
        # in-flight counter was released, so the old client could be closed
        self.assertEqual(self.factory.instances[0].closed, 1)

    async def test_close_error_of_retired_client_is_swallowed(self) -> None:
        llm = LLMClient(base_cfg())
        self.factory.script[:] = [_ok(), _ok()]
        await llm.chat_stream(MESSAGES)

        async def bad_close() -> None:
            raise RuntimeError("loop closed")

        self.factory.instances[0].close = bad_close  # type: ignore[method-assign]
        llm.api_key = "rotated"
        result = await llm.chat_stream(MESSAGES)
        self.assertEqual(result.content, "ok")

    async def test_client_is_recreated_when_the_event_loop_changes(self) -> None:
        llm = LLMClient(base_cfg())
        self.factory.script[:] = [_ok(), _ok()]
        await llm.chat_stream(MESSAGES)
        # pretend the cached client belongs to a different (dead) loop
        other_loop = asyncio.new_event_loop()
        self.addCleanup(other_loop.close)
        llm._cache.get("legacy").loop = other_loop  # type: ignore[union-attr]
        await llm.chat_stream(MESSAGES)
        self.assertEqual(len(self.factory.instances), 2)
        self.assertEqual(self.factory.instances[0].closed, 1)


class ReasoningEffortTests(_FactoryCase):
    async def _first_call(self, llm: LLMClient, **kw: Any) -> dict[str, Any]:
        self.factory.script[:] = [_ok()]
        await llm.chat_stream(MESSAGES, **kw)
        return self.factory.instances[-1].calls[-1]

    async def test_effort_is_sent_once_at_top_level_only(self) -> None:
        cfg = base_cfg(
            thinking={
                "enabled": True,
                "reasoning_effort": "medium",
                "extra_body": {"reasoning_effort": "high", "thinking": {"type": "enabled"}},
            }
        )
        call = await self._first_call(LLMClient(cfg))
        self.assertEqual(call["reasoning_effort"], "medium")
        self.assertEqual(call["extra_body"], {"thinking": {"type": "enabled"}})

    async def test_extra_body_omitted_when_only_effort_was_in_it(self) -> None:
        cfg = base_cfg(
            thinking={
                "enabled": True,
                "reasoning_effort": "low",
                "extra_body": {"reasoning_effort": "high"},
            }
        )
        call = await self._first_call(LLMClient(cfg))
        self.assertEqual(call["reasoning_effort"], "low")
        self.assertNotIn("extra_body", call)

    async def test_runtime_effort_reaches_the_request(self) -> None:
        llm = LLMClient(base_cfg())
        llm.reasoning_effort = "xhigh"
        call = await self._first_call(llm)
        self.assertEqual(call["reasoning_effort"], "xhigh")
        self.assertNotIn("reasoning_effort", call.get("extra_body", {}))

    async def test_thinking_disabled_sends_no_extended_parameters(self) -> None:
        cfg = base_cfg(thinking={"enabled": False, "extra_body": {"thinking": {"type": "enabled"}}})
        call = await self._first_call(LLMClient(cfg))
        self.assertNotIn("reasoning_effort", call)
        self.assertNotIn("extra_body", call)

    async def test_empty_effort_is_not_sent(self) -> None:
        cfg = base_cfg(thinking={"enabled": True, "reasoning_effort": ""})
        call = await self._first_call(LLMClient(cfg))
        self.assertNotIn("reasoning_effort", call)

    async def test_moderator_follows_ui_effort_over_config_override(self) -> None:
        cfg = base_cfg(moderator_override={"model": "mod-model", "temperature": 0.3, "reasoning_effort": "high"})
        llm = LLMClient(cfg)
        llm.reasoning_effort = "low"  # user picked "low" in the UI
        call = await self._first_call(llm, is_moderator=True)
        self.assertEqual(call["reasoning_effort"], "low")
        self.assertEqual(call["model"], "mod-model")
        self.assertEqual(call["temperature"], 0.3)

    async def test_explicit_config_override_applies_until_ui_sets_a_value(self) -> None:
        cfg = base_cfg(moderator_override={"reasoning_effort": "max"})
        llm = LLMClient(cfg)
        call = await self._first_call(llm, is_moderator=True)
        self.assertEqual(call["reasoning_effort"], "max")
        call = await self._first_call(llm, is_moderator=False)
        self.assertEqual(call["reasoning_effort"], "high")
        llm.reasoning_effort = "low"
        call = await self._first_call(llm, is_moderator=True)
        self.assertEqual(call["reasoning_effort"], "low")

    async def test_moderator_without_override_uses_session_effort(self) -> None:
        llm = LLMClient(base_cfg(moderator_override={"temperature": 0.1}))
        llm.reasoning_effort = "medium"
        call = await self._first_call(llm, is_moderator=True)
        self.assertEqual(call["reasoning_effort"], "medium")

    async def test_reconfigure_keeps_a_ui_chosen_effort(self) -> None:
        llm = LLMClient(base_cfg())
        llm.reasoning_effort = "low"
        llm.reconfigure(base_cfg(thinking={"enabled": True, "reasoning_effort": "high"}))
        self.assertEqual(llm.reasoning_effort, "low")

    async def test_reconfigure_picks_up_config_effort_when_ui_never_set_one(self) -> None:
        llm = LLMClient(base_cfg())
        llm.reconfigure(base_cfg(thinking={"enabled": True, "reasoning_effort": "medium"}))
        self.assertEqual(llm.reasoning_effort, "medium")

    async def test_moderator_and_player_models_and_tools(self) -> None:
        cfg = base_cfg(moderator_override={"model": "mod", "temperature": 0.1})
        llm = LLMClient(cfg)
        tools = [{"type": "function", "function": {"name": "f"}}]
        call = await self._first_call(llm, is_moderator=False, tools=tools, max_tokens_override=77)
        self.assertEqual(call["model"], "test-model")
        self.assertEqual(call["temperature"], 0.7)
        self.assertEqual(call["max_tokens"], 77)
        self.assertEqual(call["tools"], tools)
        self.assertEqual(call["tool_choice"], "auto")
        self.assertTrue(call["stream"])
        call = await self._first_call(llm, is_moderator=True)
        self.assertEqual(call["model"], "mod")
        self.assertEqual(call["temperature"], 0.1)
        self.assertNotIn("tools", call)


class DefaultsTests(unittest.TestCase):
    def test_defaults_match_config_yml(self) -> None:
        cfg = load_config(BASE_DIR / "config.yml")
        llm_cfg = cfg["llm"]
        empty = LLMClient({})
        self.assertEqual(empty.base_url, DEFAULT_BASE_URL)
        self.assertEqual(DEFAULT_BASE_URL, "https://api.deepseek.com")
        self.assertFalse(empty.base_url.rstrip("/").endswith("/v1"))
        self.assertEqual(empty.context_window_tokens, DEFAULT_CONTEXT_WINDOW_TOKENS)
        self.assertEqual(empty.context_window_tokens, int(llm_cfg["context_window_tokens"]))

    def test_context_window_default_matches_binding_default(self) -> None:
        from app.game.bindings import context_window_for
        from app.game.session import GameSession

        self.assertEqual(DEFAULT_CONTEXT_WINDOW_TOKENS, context_window_for(GameSession(), None))

    def test_blank_env_style_values_fall_back_to_defaults(self) -> None:
        llm = LLMClient({"llm": {"base_url": "", "default_model": "", "api_key": ""}})
        self.assertEqual(llm.base_url, DEFAULT_BASE_URL)
        self.assertEqual(llm.default_model, "deepseek-flash")
        self.assertFalse(llm.is_configured())

    def test_garbage_numbers_do_not_crash_construction(self) -> None:
        llm = LLMClient(
            {
                "llm": {
                    "temperature": "hot",
                    "max_tokens": None,
                    "timeout_seconds": "",
                    "context_window_tokens": "many",
                    "pricing": {"input_price_per_m": "x"},
                }
            }
        )
        self.assertEqual(llm.temperature, 0.7)
        self.assertEqual(llm.max_tokens, 4096)
        self.assertEqual(llm.timeout, 120.0)
        self.assertEqual(llm.context_window_tokens, DEFAULT_CONTEXT_WINDOW_TOKENS)
        self.assertEqual(llm.input_price_per_m, 0.27)


if __name__ == "__main__":
    unittest.main()
