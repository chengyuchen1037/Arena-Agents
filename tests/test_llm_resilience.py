from __future__ import annotations

import asyncio
import unittest
from copy import deepcopy
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, call, patch

import httpx
from openai import APIConnectionError, APITimeoutError, BadRequestError

from app.engine.llm import LLMClient, LLMResponse, is_retryable_error, retry_after_seconds
from app.llm.openai_chat import OpenAIChatAdapter
from test_llm_support import (
    CHAT_SDK,
    SharedScriptFactory,
    base_cfg,
    connection_error,
    make_chunk,
    make_request,
    status_error,
)


def _bad_request(message: str) -> BadRequestError:
    response = httpx.Response(400, request=make_request())
    body = {"error": {"message": message, "type": "invalid_request_error"}}
    return BadRequestError(message, response=response, body=body)


class _FakeClient:
    def __init__(self) -> None:
        self.close = AsyncMock()


class LLMClientResilienceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        cfg = base_cfg(
            thinking={
                "enabled": True,
                "reasoning_effort": "high",
                "extra_body": {"custom": "value"},
            }
        )
        self.llm = LLMClient(cfg)
        self.fake_client = _FakeClient()
        create_client_patch = patch.object(
            self.llm,
            "_create_client",
            return_value=self.fake_client,
        )
        create_client_patch.start()
        self.addCleanup(create_client_patch.stop)
        self.messages = [{"role": "user", "content": "hello"}]

    async def test_connection_error_retries_until_third_attempt_succeeds(self) -> None:
        expected = LLMResponse(content="connected")
        consume = AsyncMock(
            side_effect=[connection_error(), connection_error(), expected]
        )
        sleep = AsyncMock()

        with (
            patch.object(OpenAIChatAdapter, "consume", new=consume),
            patch("app.llm.common.asyncio.sleep", new=sleep),
        ):
            result = await self.llm.chat_stream(self.messages)

        self.assertIs(result, expected)
        self.assertEqual(consume.await_count, 3)
        # exponential backoff: 0.5 * 2^(n-1)
        self.assertEqual(sleep.await_args_list, [call(0.5), call(1.0)])
        # The shared client is NOT closed after every call any more.
        self.fake_client.close.assert_not_awaited()
        await self.llm.aclose()
        self.fake_client.close.assert_awaited_once_with()

    async def test_tool_message_400_is_not_retried_or_downgraded(self) -> None:
        error = _bad_request("assistant tool_calls insufficient tool messages")
        captured_kwargs: list[dict] = []

        async def fail_once(_client, kwargs, on_chunk=None, progress=None):
            captured_kwargs.append(deepcopy(kwargs))
            raise error

        consume = AsyncMock(side_effect=fail_once)
        with patch.object(OpenAIChatAdapter, "consume", new=consume):
            with self.assertRaises(BadRequestError) as raised:
                await self.llm.chat_stream(self.messages)

        self.assertIs(raised.exception, error)
        self.assertEqual(consume.await_count, 1)
        self.assertEqual(len(captured_kwargs), 1)
        self.assertIn("reasoning_effort", captured_kwargs[0])
        self.assertIn("extra_body", captured_kwargs[0])
        self.assertIn("stream_options", captured_kwargs[0])
        self.fake_client.close.assert_not_awaited()

    async def test_reasoning_effort_400_retries_without_extended_parameters(self) -> None:
        expected = LLMResponse(content="standard request succeeded")
        error = _bad_request("Unsupported parameter: reasoning_effort")
        captured_kwargs: list[dict] = []

        async def fail_then_succeed(_client, kwargs, on_chunk=None, progress=None):
            captured_kwargs.append(deepcopy(kwargs))
            if len(captured_kwargs) == 1:
                raise error
            return expected

        consume = AsyncMock(side_effect=fail_then_succeed)
        with patch.object(OpenAIChatAdapter, "consume", new=consume):
            result = await self.llm.chat_stream(self.messages)

        self.assertIs(result, expected)
        self.assertEqual(consume.await_count, 2)
        self.assertEqual(len(captured_kwargs), 2)
        for parameter in ("reasoning_effort", "extra_body", "stream_options"):
            self.assertIn(parameter, captured_kwargs[0])
            self.assertNotIn(parameter, captured_kwargs[1])
        self.fake_client.close.assert_not_awaited()

    async def test_downgrade_does_not_consume_the_retry_budget(self) -> None:
        expected = LLMResponse(content="ok")
        script = [
            _bad_request("Unsupported parameter: reasoning_effort"),
            connection_error(),
            connection_error(),
            expected,
        ]
        consume = AsyncMock(side_effect=script)
        with (
            patch.object(OpenAIChatAdapter, "consume", new=consume),
            patch("app.llm.common.asyncio.sleep", new=AsyncMock()),
        ):
            result = await self.llm.chat_stream(self.messages)
        self.assertIs(result, expected)
        self.assertEqual(consume.await_count, 4)


class RetryClassificationTests(unittest.IsolatedAsyncioTestCase):
    """Which errors are retried, how often, and with what delay."""

    def _make(self, **retry: object) -> tuple[LLMClient, SharedScriptFactory]:
        cfg = base_cfg(retry={"jitter": 0, **retry})
        llm = LLMClient(cfg)
        factory = SharedScriptFactory()
        patcher = patch(CHAT_SDK, new=factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        return llm, factory

    async def _run(self, llm: LLMClient, sleep: AsyncMock | None = None) -> LLMResponse:
        with patch("app.llm.common.asyncio.sleep", new=sleep or AsyncMock()):
            return await llm.chat_stream([{"role": "user", "content": "hi"}])

    async def test_transient_http_statuses_are_retried(self) -> None:
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                llm, factory = self._make()
                factory.script[:] = [
                    status_error(status),
                    [make_chunk("done", finish="stop")],
                ]
                sleep = AsyncMock()
                result = await self._run(llm, sleep)
                self.assertEqual(result.content, "done")
                self.assertEqual(sleep.await_count, 1)
                await llm.aclose()

    async def test_other_4xx_are_not_retried(self) -> None:
        for status in (400, 401, 403, 404, 409, 413, 422):
            with self.subTest(status=status):
                llm, factory = self._make()
                factory.script[:] = [status_error(status), [make_chunk("never")]]
                sleep = AsyncMock()
                with self.assertRaises(Exception) as raised:
                    await self._run(llm, sleep)
                self.assertEqual(raised.exception.status_code, status)  # type: ignore[attr-defined]
                sleep.assert_not_awaited()
                self.assertEqual(len(factory.instances[0].calls), 1)
                await llm.aclose()

    async def test_timeouts_and_transport_errors_are_retried(self) -> None:
        errors = [
            APITimeoutError(request=make_request()),
            httpx.ReadTimeout("slow", request=make_request()),
            httpx.RemoteProtocolError("peer closed", request=make_request()),
            connection_error(),
        ]
        for err in errors:
            with self.subTest(error=type(err).__name__):
                llm, factory = self._make()
                factory.script[:] = [err, [make_chunk("ok")]]
                result = await self._run(llm)
                self.assertEqual(result.content, "ok")
                await llm.aclose()

    async def test_gives_up_after_max_attempts_and_raises_last_error(self) -> None:
        llm, factory = self._make()
        last = status_error(503, "third")
        factory.script[:] = [status_error(503, "first"), status_error(503, "second"), last]
        sleep = AsyncMock()
        with self.assertRaises(Exception) as raised:
            await self._run(llm, sleep)
        self.assertIs(raised.exception, last)
        self.assertEqual(len(factory.instances[0].calls), 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_max_attempts_is_configurable(self) -> None:
        llm, factory = self._make(max_attempts=5)
        factory.script[:] = [connection_error() for _ in range(4)] + [[make_chunk("ok")]]
        result = await self._run(llm)
        self.assertEqual(result.content, "ok")
        self.assertEqual(len(factory.instances[0].calls), 5)

    async def test_max_attempts_one_disables_retry(self) -> None:
        llm, factory = self._make(max_attempts=1)
        factory.script[:] = [connection_error(), [make_chunk("ok")]]
        sleep = AsyncMock()
        with self.assertRaises(APIConnectionError):
            await self._run(llm, sleep)
        sleep.assert_not_awaited()

    async def test_invalid_retry_config_falls_back_to_defaults(self) -> None:
        llm = LLMClient(base_cfg(retry={"max_attempts": "abc", "base_delay_seconds": None}))
        self.assertEqual(llm.retry_max_attempts, 3)
        self.assertEqual(llm.retry_base_delay, 0.5)
        llm = LLMClient(base_cfg(retry={"max_attempts": 0}))
        self.assertEqual(llm.retry_max_attempts, 1)
        llm = LLMClient({"llm": {"api_key": "k"}})
        self.assertEqual(llm.retry_max_attempts, 3)

    async def test_backoff_is_exponential_and_capped(self) -> None:
        llm, _ = self._make(base_delay_seconds=1, max_delay_seconds=5)
        self.assertEqual(
            [llm._backoff_delay(n) for n in range(1, 6)], [1.0, 2.0, 4.0, 5.0, 5.0]
        )

    async def test_jitter_only_shortens_and_never_exceeds_cap(self) -> None:
        llm, _ = self._make(base_delay_seconds=1, max_delay_seconds=4, jitter=0.5)
        llm._random = lambda: 1.0  # maximum jitter
        self.assertEqual(llm._backoff_delay(1), 0.5)
        self.assertEqual(llm._backoff_delay(10), 2.0)
        llm._random = lambda: 0.0
        self.assertEqual(llm._backoff_delay(10), 4.0)
        llm._random = lambda: 0.37
        for n in range(1, 12):
            self.assertLessEqual(llm._backoff_delay(n), 4.0)

    async def test_retry_after_header_is_honoured(self) -> None:
        llm, factory = self._make()
        factory.script[:] = [
            status_error(429, headers={"retry-after": "7"}),
            [make_chunk("ok")],
        ]
        sleep = AsyncMock()
        await self._run(llm, sleep)
        self.assertEqual(sleep.await_args_list, [call(7.0)])

    async def test_retry_after_is_capped(self) -> None:
        llm, factory = self._make(max_retry_after_seconds=10)
        factory.script[:] = [
            status_error(429, headers={"retry-after": "3600"}),
            [make_chunk("ok")],
        ]
        sleep = AsyncMock()
        await self._run(llm, sleep)
        self.assertEqual(sleep.await_args_list, [call(10.0)])

    async def test_retry_after_never_shortens_the_backoff(self) -> None:
        llm, factory = self._make(base_delay_seconds=2)
        factory.script[:] = [
            status_error(429, headers={"retry-after": "0"}),
            [make_chunk("ok")],
        ]
        sleep = AsyncMock()
        await self._run(llm, sleep)
        self.assertEqual(sleep.await_args_list, [call(2.0)])

    def test_retry_after_parsing(self) -> None:
        self.assertEqual(retry_after_seconds(status_error(429, headers={"retry-after": "2.5"})), 2.5)
        self.assertEqual(
            retry_after_seconds(status_error(429, headers={"retry-after-ms": "1500"})), 1.5
        )
        self.assertIsNone(retry_after_seconds(status_error(429)))
        self.assertIsNone(retry_after_seconds(status_error(429, headers={"retry-after": "soon"})))
        self.assertIsNone(retry_after_seconds(connection_error()))
        self.assertIsNone(retry_after_seconds(status_error(429, headers={"retry-after": "nan"})))
        future = datetime.now(timezone.utc) + timedelta(seconds=30)
        value = retry_after_seconds(
            status_error(429, headers={"retry-after": format_datetime(future, usegmt=True)})
        )
        self.assertIsNotNone(value)
        self.assertTrue(20 <= value <= 31)  # type: ignore[operator]
        past = datetime.now(timezone.utc) - timedelta(seconds=30)
        self.assertEqual(
            retry_after_seconds(
                status_error(429, headers={"retry-after": format_datetime(past, usegmt=True)})
            ),
            0.0,
        )

    def test_is_retryable_error(self) -> None:
        self.assertTrue(is_retryable_error(connection_error()))
        self.assertTrue(is_retryable_error(status_error(429)))
        self.assertTrue(is_retryable_error(status_error(500)))
        self.assertFalse(is_retryable_error(status_error(400)))
        self.assertFalse(is_retryable_error(status_error(401)))
        self.assertFalse(is_retryable_error(ValueError("nope")))
        self.assertFalse(is_retryable_error(RuntimeError("nope")))

    async def test_sdk_level_retries_are_disabled(self) -> None:
        from app.llm.registry import get_adapter
        from app.llm.types import Target

        llm = LLMClient(base_cfg())
        for protocol in ("openai_chat", "openai_responses", "anthropic_messages"):
            with self.subTest(protocol=protocol):
                target = Target(
                    provider_id="p", protocol=protocol, base_url="https://x.test", api_key="k", model_id="m"
                )
                client = llm._create_client(get_adapter(protocol), target)
                try:
                    self.assertEqual(client.max_retries, 0)
                finally:
                    await client.close()

    async def test_unconfigured_key_raises_before_any_request(self) -> None:
        llm = LLMClient({"llm": {"api_key": "sk-your-deepseek-api-key-here"}})
        factory = SharedScriptFactory()
        with patch(CHAT_SDK, new=factory):
            with self.assertRaises(RuntimeError):
                await llm.chat_stream([{"role": "user", "content": "hi"}])
        self.assertEqual(factory.instances, [])

    async def test_cancellation_is_not_retried(self) -> None:
        llm, factory = self._make()
        factory.script[:] = [asyncio.CancelledError(), [make_chunk("ok")]]
        sleep = AsyncMock()
        with self.assertRaises(asyncio.CancelledError):
            await self._run(llm, sleep)
        sleep.assert_not_awaited()
        self.assertEqual(len(factory.instances[0].calls), 1)


class StreamingRetryTests(unittest.IsolatedAsyncioTestCase):
    """Streaming calls only retry while nothing has been produced yet."""

    def _make(self) -> tuple[LLMClient, SharedScriptFactory]:
        llm = LLMClient(base_cfg())
        factory = SharedScriptFactory()
        patcher = patch(CHAT_SDK, new=factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        return llm, factory

    async def _run(self, llm: LLMClient, on_chunk=None) -> LLMResponse:
        with patch("app.llm.common.asyncio.sleep", new=AsyncMock()):
            return await llm.chat_stream(
                [{"role": "user", "content": "hi"}], on_chunk=on_chunk
            )

    async def test_error_before_first_chunk_is_retried(self) -> None:
        llm, factory = self._make()
        factory.script[:] = [
            [connection_error()],  # stream opens, dies before any chunk
            [make_chunk("hello"), make_chunk(" world", finish="stop")],
        ]
        seen: list[str] = []

        async def on_chunk(_thought: str, content: str) -> None:
            seen.append(content)

        result = await self._run(llm, on_chunk)
        self.assertEqual(result.content, "hello world")
        self.assertEqual(seen, ["hello", "hello world"])
        self.assertEqual(len(factory.instances[0].calls), 2)

    async def test_usage_only_chunk_does_not_count_as_output(self) -> None:
        llm, factory = self._make()
        factory.script[:] = [
            [make_chunk(no_choices=True, usage=(5, 0)), httpx.ReadTimeout("t", request=make_request())],
            [make_chunk("ok")],
        ]
        result = await self._run(llm)
        self.assertEqual(result.content, "ok")

    async def test_error_after_content_chunk_is_not_retried(self) -> None:
        llm, factory = self._make()
        err = connection_error()
        factory.script[:] = [[make_chunk("partial"), err], [make_chunk("never used")]]
        seen: list[str] = []

        async def on_chunk(_thought: str, content: str) -> None:
            seen.append(content)

        with self.assertRaises(APIConnectionError) as raised:
            await self._run(llm, on_chunk)
        self.assertIs(raised.exception, err)
        self.assertEqual(seen, ["partial"])
        self.assertEqual(len(factory.instances[0].calls), 1)

    async def test_error_after_content_without_callback_is_not_retried(self) -> None:
        llm, factory = self._make()
        factory.script[:] = [[make_chunk("partial"), connection_error()], [make_chunk("x")]]
        with self.assertRaises(APIConnectionError):
            await self._run(llm)
        self.assertEqual(len(factory.instances[0].calls), 1)

    async def test_error_after_reasoning_chunk_is_not_retried(self) -> None:
        llm, factory = self._make()
        factory.script[:] = [
            [make_chunk(None, reasoning="thinking..."), httpx.ReadTimeout("t", request=make_request())],
            [make_chunk("x")],
        ]
        with self.assertRaises(httpx.ReadTimeout):
            await self._run(llm)
        self.assertEqual(len(factory.instances[0].calls), 1)

    async def test_error_after_tool_call_chunk_is_not_retried(self) -> None:
        from test_llm_support import tc_delta

        llm, factory = self._make()
        factory.script[:] = [
            [make_chunk(tool_calls=[tc_delta(0, id="c", name="f", args="{")]), connection_error()],
            [make_chunk("x")],
        ]
        with self.assertRaises(APIConnectionError):
            await self._run(llm)
        self.assertEqual(len(factory.instances[0].calls), 1)

    async def test_retry_state_is_fresh_for_every_attempt(self) -> None:
        # Partial data from a failed attempt must never leak into the retry.
        llm, factory = self._make()
        factory.script[:] = [
            [connection_error()],
            [make_chunk("a"), make_chunk("b", finish="stop")],
        ]
        result = await self._run(llm)
        self.assertEqual(result.content, "ab")
        self.assertEqual(result.finish_reason, "stop")


if __name__ == "__main__":
    unittest.main()
