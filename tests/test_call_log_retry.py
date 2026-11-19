"""The retry hook of the model client (v0.13.1, G34; contract docs/v0.12-contracts.md 12.3): ``run_with_retry(on_retry=...)`` tells
about every attempt that failed and is repeated, ``LLMClient.chat_stream`` hands the hook through (with the provider's key
scrubbed from what it hears), and the gate turns what it hears into lines of the error log.  Nothing here touches a network."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.engine.llm import LLMClient, LLMResponse
from app.llm.common import RetryPolicy, StreamProgress, run_with_retry
from app.llm.openai_chat import OpenAIChatAdapter
from app.runtime.call_log import CallLog
from app.runtime.llm_gate import GatedEngineLlm, LlmGate
from game_fakes import async_test
from test_llm_support import base_cfg, connection_error, status_error

POLICY = RetryPolicy(max_attempts=4, base_delay=0.5, max_delay=8.0, jitter=0.0)


def hearing() -> tuple[list[tuple[str, int, float]], Any]:
    heard: list[tuple[str, int, float]] = []

    def on_retry(exc: BaseException, attempt: int, delay: float) -> None:
        heard.append((type(exc).__name__, attempt, delay))

    return heard, on_retry


def script(*steps: Any) -> Any:
    """An ``attempt`` callable that raises / returns the steps in turn."""
    remaining = list(steps)

    async def attempt(progress: StreamProgress) -> LLMResponse:
        step = remaining.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    return attempt


# ------------------------------------------------------------------------------------ run_with_retry
@async_test
async def test_the_hook_hears_about_every_attempt_that_is_repeated_with_its_number_and_the_wait():
    heard, on_retry = hearing()
    sleep = AsyncMock()
    done = LLMResponse(content="done")
    with patch("app.llm.common.asyncio.sleep", new=sleep):
        result = await run_with_retry(
            script(connection_error(), status_error(429), status_error(503), done), policy=POLICY, on_retry=on_retry
        )
    assert result is done
    assert heard == [("APIConnectionError", 1, 0.5), ("APIStatusError", 2, 1.0), ("APIStatusError", 3, 2.0)]
    assert [c.args[0] for c in sleep.await_args_list] == [0.5, 1.0, 2.0]  # (the wait is unchanged by the hook)


@async_test
async def test_the_hook_is_called_before_the_wait_not_after():
    order: list[str] = []

    async def sleep(delay: float) -> None:
        order.append(f"sleep {delay}")

    with patch("app.llm.common.asyncio.sleep", new=sleep):
        await run_with_retry(
            script(connection_error(), LLMResponse()), policy=POLICY, on_retry=lambda exc, n, d: order.append(f"heard {n}")
        )
    assert order == ["heard 1", "sleep 0.5"]


@async_test
async def test_the_failure_that_ends_the_loop_is_raised_and_not_reported():
    heard, on_retry = hearing()
    last = status_error(500, "last")
    with patch("app.llm.common.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(type(last)) as raised:
            await run_with_retry(
                script(connection_error(), connection_error(), last),
                policy=RetryPolicy(max_attempts=3, base_delay=0.5, max_delay=8.0, jitter=0.0),
                on_retry=on_retry,
            )
    assert raised.value is last
    assert [(name, n) for name, n, _ in heard] == [("APIConnectionError", 1), ("APIConnectionError", 2)]  # two repeated, the third raised


@async_test
async def test_an_error_that_is_not_retried_is_raised_at_once_without_a_report():
    heard, on_retry = hearing()
    with pytest.raises(Exception, match="bad request"):
        await run_with_retry(script(status_error(400, "bad request")), policy=POLICY, on_retry=on_retry)
    assert heard == []

    async def emitted(progress: StreamProgress) -> LLMResponse:
        progress.emitted = True  # something was already shown to the user: a repeat would show it twice
        raise connection_error()

    with pytest.raises(Exception):
        await run_with_retry(emitted, policy=POLICY, on_retry=on_retry)
    assert heard == []


@async_test
async def test_a_downgrade_is_not_reported_but_it_counts_as_an_attempt():
    # an upstream that rejects an optional parameter does so on EVERY request: that must not fill the error log
    heard, on_retry = hearing()
    rejected = status_error(400, "Unsupported parameter: reasoning_effort")
    seen: list[BaseException] = []

    def downgrade(exc: BaseException) -> bool:
        seen.append(exc)
        return exc is rejected

    with patch("app.llm.common.asyncio.sleep", new=AsyncMock()):
        await run_with_retry(
            script(rejected, connection_error(), LLMResponse()), policy=POLICY, downgrade=downgrade, on_retry=on_retry
        )
    assert heard == [("APIConnectionError", 2, 0.5)]  # the retry is the second attempt; the downgrade was the first (silent)
    assert seen[0] is rejected


@async_test
async def test_an_observer_that_raises_never_breaks_the_retry():
    sleep = AsyncMock()

    def explode(exc: BaseException, attempt: int, delay: float) -> None:
        raise RuntimeError("observer bug")

    done = LLMResponse(content="done")
    with patch("app.llm.common.asyncio.sleep", new=sleep):
        assert await run_with_retry(script(connection_error(), done), policy=POLICY, on_retry=explode) is done
    assert sleep.await_count == 1


@async_test
async def test_without_a_hook_nothing_is_different():
    done = LLMResponse(content="done")
    with patch("app.llm.common.asyncio.sleep", new=AsyncMock()):
        assert await run_with_retry(script(connection_error(), done), policy=POLICY) is done


# ------------------------------------------------------------------------------------- the real client
def client_with(key: str = "test-key-secret-123") -> tuple[LLMClient, AsyncMock]:
    llm = LLMClient(base_cfg(api_key=key))
    fake = AsyncMock()
    patcher = patch.object(llm, "_create_client", return_value=fake)
    patcher.start()
    llm._test_patcher = patcher  # type: ignore[attr-defined]
    return llm, fake


@async_test
async def test_chat_stream_hands_the_hook_through_and_the_key_is_scrubbed_from_what_it_hears():
    llm, _fake = client_with("test-key-secret-123")
    try:
        heard: list[tuple[str, int, float]] = []
        texts: list[str] = []

        def on_retry(exc: BaseException, attempt: int, delay: float) -> None:
            heard.append((type(exc).__name__, attempt, delay))
            texts.append(f"{exc} | {getattr(exc, 'message', '')} | {getattr(exc, 'body', '')}")

        expected = LLMResponse(content="ok")
        consume = AsyncMock(
            side_effect=[status_error(429, "slow down, key test-key-secret-123 is rate limited"), connection_error(), expected]
        )
        with patch.object(OpenAIChatAdapter, "consume", new=consume), patch("app.llm.common.asyncio.sleep", new=AsyncMock()):
            result = await llm.chat_stream([{"role": "user", "content": "hi"}], on_retry=on_retry)
        assert result is expected and consume.await_count == 3
        assert [(name, n) for name, n, _ in heard] == [("APIStatusError", 1), ("APIConnectionError", 2)]
        assert "slow down" in texts[0] and "test-key-secret-123" not in " ".join(texts)  # the key is gone, the rest is there
        assert "***" in texts[0]
    finally:
        llm._test_patcher.stop()  # type: ignore[attr-defined]
        await llm.aclose()


@async_test
async def test_the_final_failure_is_still_scrubbed_and_raised_whether_or_not_there_is_a_hook():
    llm, _fake = client_with("test-key-secret-123")
    try:
        final = status_error(401, "invalid key test-key-secret-123")
        for hook in (None, lambda exc, n, d: None):
            consume = AsyncMock(side_effect=[final])
            with patch.object(OpenAIChatAdapter, "consume", new=consume):
                with pytest.raises(type(final)) as raised:
                    await llm.chat_stream([{"role": "user", "content": "hi"}], on_retry=hook)
            assert "test-key-secret-123" not in str(raised.value)
    finally:
        llm._test_patcher.stop()  # type: ignore[attr-defined]
        await llm.aclose()


@async_test
async def test_the_gate_with_the_real_client_logs_each_repeated_attempt_and_the_end_of_the_request():
    llm, _fake = client_with("test-key-secret-123")
    try:
        log = CallLog("s1", clock=lambda: 3.0)
        gated = GatedEngineLlm(llm, lambda agent_id: None, LlmGate(), 5, call_log=log)
        answer = LLMResponse(content="private words", prompt_tokens=10, completion_tokens=4, finish_reason="stop")
        consume = AsyncMock(
            side_effect=[connection_error(), status_error(429, "slow down for test-key-secret-123"), answer]
        )
        with patch.object(OpenAIChatAdapter, "consume", new=consume), patch("app.llm.common.asyncio.sleep", new=AsyncMock()):
            resp = await gated.complete("a1", [{"role": "user", "content": "private prompt"}], None)
        assert resp.content == "private words"
        lines = log.entries()
        assert [(c["status"], c["attempt"]) for c in lines] == [("retry", 1), ("retry", 2), ("ok", 3)]
        assert (lines[0]["error_class"], lines[0]["http_status"]) == ("APIConnectionError", None)
        assert (lines[1]["error_class"], lines[1]["http_status"]) == ("APIStatusError", 429)
        assert (lines[2]["prompt_tokens"], lines[2]["completion_tokens"], lines[2]["finish_reason"]) == (10, 4, "stop")
        text = json.dumps(lines, ensure_ascii=False)
        assert "test-key-secret-123" not in text and "private" not in text  # not the key, the prompt or the answer
        assert (lines[2]["provider_id"], lines[2]["model_id"]) == ("legacy", "test-model")  # what answered (the stamped identity)

        # a request that ends in an error: the retries, then the last attempt as an error line
        log2 = CallLog("s2")
        gated2 = GatedEngineLlm(llm, lambda agent_id: None, LlmGate(), 5, call_log=log2)
        consume2 = AsyncMock(side_effect=[connection_error(), connection_error(), connection_error(), connection_error()])
        with patch.object(OpenAIChatAdapter, "consume", new=consume2), patch("app.llm.common.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(Exception):
                await gated2.complete("a1", [{"role": "user", "content": "x"}], None)
        assert [(c["status"], c["attempt"]) for c in log2.entries()] == [("retry", 1), ("retry", 2), ("error", 3)]  # 3 attempts by default
    finally:
        llm._test_patcher.stop()  # type: ignore[attr-defined]
        await llm.aclose()
