"""Every model request leaves lines in the error log (v0.13.1, G33, G34; contract 12.3): ``GatedEngineLlm`` records each
attempt the client repeats and the end of the request (ok / timeout / error / cancelled); ``LoggedLlm`` does the same, one
line per request, around any other port.  The log never changes what a call does."""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from app.engine.llm import LLMClient
from app.llm.types import LLMResponse
from app.runtime.call_log import CallLog
from app.runtime.llm_gate import GatedEngineLlm, LlmGate, LoggedLlm, turn_label
from app.runtime.ports import LlmPort

from .fakes import async_test, wait_until


class Client:
    """A stand-in for ``LLMClient`` that takes ``on_retry`` like the real one and repeats attempts the way
    ``run_with_retry`` reports them."""

    def __init__(
        self,
        *,
        retries: list[BaseException] | None = None,
        final: BaseException | None = None,
        delay: float = 0.0,
        delays: list[float] | None = None,
    ) -> None:
        self.retries = list(retries or [])
        self.final = final
        self.delay = delay
        self.delays = list(delays or [])  # one per call, in the order the calls reach the client (then ``delay``)
        self.calls: list[dict[str, Any]] = []

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        is_moderator: bool = False,
        tools: list[dict[str, Any]] | None = None,
        on_chunk: Any = None,
        max_tokens_override: int | None = None,
        binding: Any = None,
        on_retry: Any = None,
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "binding": binding, "on_retry": on_retry, "is_moderator": is_moderator})
        delay = self.delays.pop(0) if self.delays else self.delay
        for number, exc in enumerate(self.retries, start=1):
            if on_retry is not None:
                on_retry(exc, number, 0.01)
        if delay:
            await asyncio.sleep(delay)
        if self.final is not None:
            raise self.final
        return LLMResponse(
            content="PRIVATE ANSWER", prompt_tokens=120, completion_tokens=30, cache_read_tokens=40, cache_write_tokens=0,
            finish_reason="stop", provider_id="prov", model_id="m-answered",
        )


class OldClient:
    """A client written before ``on_retry`` existed (the fakes of the older tests): it must be called as before."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_stream(self, messages, *, is_moderator=False, tools=None, on_chunk=None, max_tokens_override=None, binding=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        return LLMResponse(content="x", prompt_tokens=1, completion_tokens=1, finish_reason="stop")


def binding(provider: str = "prov", model: str = "m-bound") -> SimpleNamespace:
    return SimpleNamespace(provider_id=provider, model_id=model)


def llm_with_log(client: Any, *, timeout_s: float | None = 5.0, **kwargs: Any) -> tuple[GatedEngineLlm, CallLog]:
    events: list[Any] = []
    log = CallLog("s1", clock=lambda: 42.0, names=lambda actor: f"<{actor}>", publish=events.append)
    log.events = events  # type: ignore[attr-defined]
    llm = GatedEngineLlm(client, lambda agent_id: binding(), LlmGate(), timeout_s, call_log=log, **kwargs)
    return llm, log


def msgs(text: str = "PRIVATE PROMPT") -> list[dict[str, Any]]:
    return [{"role": "system", "content": text}]


def rate_limit() -> openai.RateLimitError:
    request = httpx.Request("POST", "https://api.example.invalid/v1/chat/completions")
    return openai.RateLimitError("Rate limit reached for sk-abcdef1234567890", response=httpx.Response(429, request=request), body=None)


# ---------------------------------------------------------------------------------------------- success
@async_test()
async def test_a_successful_request_is_one_ok_line_with_counts_and_no_content():
    llm, log = llm_with_log(Client())
    resp = await llm.complete("a1", msgs(), None)
    assert resp.content == "PRIVATE ANSWER"
    (line,) = log.entries()
    assert (line["kind"], line["status"], line["purpose"], line["actor_id"], line["actor_name"]) == ("call", "ok", "turn", "a1", "<a1>")
    assert (line["provider_id"], line["model_id"]) == ("prov", "m-answered")  # what answered, not what was bound
    assert (line["prompt_tokens"], line["completion_tokens"], line["cache_read_tokens"], line["cache_write_tokens"]) == (120, 30, 40, 0)
    assert (line["finish_reason"], line["attempt"], line["gt"]) == ("stop", 1, 42.0)
    assert isinstance(line["elapsed_ms"], int) and line["elapsed_ms"] >= 0
    text = json.dumps(log.entries(), ensure_ascii=False)
    assert "PRIVATE" not in text  # neither the prompt nor the answer is in the log
    assert [e.type for e in log.events] == ["llm.call"]  # type: ignore[attr-defined]


@async_test()
async def test_the_arguments_the_client_gets_are_unchanged_and_it_is_given_the_retry_hook():
    client = Client()
    llm, _log = llm_with_log(client)
    tools = [{"type": "function", "function": {"name": "speak"}}]
    await llm.complete("moderator", msgs(), tools, max_tokens=77)
    call = client.calls[0]
    assert call["is_moderator"] is True and callable(call["on_retry"])


@async_test()
async def test_the_failed_attempts_a_client_repeats_are_retry_lines_before_the_final_one():
    client = Client(retries=[rate_limit(), openai.APIConnectionError(request=httpx.Request("POST", "https://x.invalid"))])
    llm, log = llm_with_log(client)
    await llm.complete("a1", msgs(), None)
    lines = log.entries()
    assert [(c["status"], c["attempt"]) for c in lines] == [("retry", 1), ("retry", 2), ("ok", 3)]
    first = lines[0]
    assert (first["http_status"], first["error_class"]) == (429, "RateLimitError")
    assert "sk-abcdef" not in json.dumps(lines)  # the text of a failure is scrubbed
    assert lines[1]["error_class"] == "APIConnectionError" and lines[1]["http_status"] is None
    assert (log.errors, log.retries) == (0, 2)


@async_test()
async def test_a_client_without_the_retry_hook_is_called_exactly_as_before():
    client = OldClient()
    llm, log = llm_with_log(client)
    assert (await llm.complete("a1", msgs(), None)).content == "x"
    assert client.calls == 1 and [c["status"] for c in log.entries()] == ["ok"]  # one line per request: the attempts are not visible


@async_test()
async def test_without_a_log_nothing_changes_and_no_hook_is_passed():
    client = Client()
    llm = GatedEngineLlm(client, lambda a: None, LlmGate(), 5)
    port: LlmPort = llm
    await port.complete("a1", msgs(), None)
    assert client.calls[0]["on_retry"] is None and llm.call_log is None


def test_the_real_client_takes_the_retry_hook():
    # (the gate decides by the signature: the real LLMClient must qualify, or no retry would ever be logged)
    assert "on_retry" in inspect.signature(LLMClient.chat_stream).parameters
    assert GatedEngineLlm(SimpleNamespace(chat_stream=LLMClient.chat_stream), lambda a: None, call_log=CallLog("s"))._report_retries is True


# ---------------------------------------------------------------------------------------------- failure
@async_test()
async def test_a_failed_request_is_an_error_line_and_the_exception_still_propagates():
    llm, log = llm_with_log(Client(retries=[rate_limit()], final=RuntimeError("upstream exploded sk-secret-1234567")))
    with pytest.raises(RuntimeError, match="upstream exploded"):
        await llm.complete("a1", msgs(), None)
    retry, error = log.entries()
    assert (retry["status"], retry["attempt"]) == ("retry", 1)
    assert (error["status"], error["attempt"], error["error_class"], error["http_status"]) == ("error", 2, "RuntimeError", None)
    assert "sk-secret" not in error["message"] and "upstream exploded" in error["message"]
    assert (error["provider_id"], error["model_id"]) == ("prov", "m-bound")  # nothing answered: the binding is what is known
    assert error["prompt_tokens"] is None and (log.errors, log.retries) == (1, 1)


@async_test()
async def test_an_http_error_status_is_carried_over():
    llm, log = llm_with_log(Client(final=rate_limit()))
    with pytest.raises(openai.RateLimitError):
        await llm.complete("a1", msgs(), None)
    (line,) = log.entries()
    assert (line["status"], line["http_status"], line["error_class"]) == ("error", 429, "RateLimitError")


@async_test()
async def test_the_gates_own_time_limit_is_a_timeout_line_with_a_sentence_of_its_own():
    llm, log = llm_with_log(Client(delay=5.0), timeout_s=0.05)
    with pytest.raises(TimeoutError):
        await llm.complete("a1", msgs(), None)
    (line,) = log.entries()
    assert (line["status"], line["error_class"]) == ("timeout", "TimeoutError")
    assert "0.05 秒" in line["message"]
    assert 30 <= line["elapsed_ms"] < 2000 and llm.gate.in_flight == 0
    assert (log.errors, log.retries) == (1, 0)


@async_test()
async def test_a_timeout_after_a_retry_counts_the_attempt_it_died_in():
    llm, log = llm_with_log(Client(retries=[rate_limit()], delay=5.0), timeout_s=0.05)
    with pytest.raises(TimeoutError):
        await llm.complete("a1", msgs(), None)
    assert [(c["status"], c["attempt"]) for c in log.entries()] == [("retry", 1), ("timeout", 2)]


@async_test()
async def test_a_sdk_timeout_is_a_timeout_too():
    llm, log = llm_with_log(Client(final=openai.APITimeoutError(request=httpx.Request("POST", "https://x.invalid"))))
    with pytest.raises(openai.APITimeoutError):
        await llm.complete("a1", msgs(), None)
    assert log.entries()[0]["status"] == "timeout"


@async_test()
async def test_a_cancelled_request_is_a_cancelled_line_and_the_cancellation_goes_on():
    client = Client(delay=5.0)
    llm, log = llm_with_log(client, timeout_s=30)
    running = asyncio.create_task(llm.complete("a1", msgs(), None))
    await wait_until(lambda: llm.gate.in_flight == 1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    (line,) = log.entries()
    assert line["status"] == "cancelled" and line["actor_id"] == "a1" and "取消" in line["message"]
    assert (log.errors, log.retries) == (0, 0) and llm.gate.in_flight == 0  # not an error, and the slot is free


@async_test()
async def test_the_wait_for_a_slot_is_not_part_of_the_time_of_the_call():
    client = Client(delays=[0.4, 0.05])
    gate = LlmGate(max_concurrent=1, per_provider=None)
    log = CallLog("s1")
    llm = GatedEngineLlm(client, lambda a: binding(), gate, 5, call_log=log)
    first = asyncio.create_task(llm.complete("a1", msgs(), None))
    await wait_until(lambda: gate.in_flight == 1)
    second = await llm.complete("a2", msgs(), None)  # waits ~0.4 s for the slot, then needs 0.05 s itself
    await first
    assert second.content
    by_actor = {c["actor_id"]: c["elapsed_ms"] for c in log.entries()}
    assert by_actor["a2"] < 300  # the call took ~50 ms; the ~400 ms in the queue are not counted
    assert by_actor["a1"] >= 300


# ------------------------------------------------------------------------------------------- what for
@async_test()
async def test_the_label_says_whose_line_it_is_and_what_the_call_is_for():
    def label(agent_id: str) -> tuple[str, str]:
        return ("refine", "roster") if agent_id == "moderator" else ("review", agent_id)

    llm, log = llm_with_log(Client(), label=label)
    await llm.complete("moderator", msgs(), None)
    await llm.complete("a2", msgs(), None)
    assert [(c["purpose"], c["actor_id"]) for c in log.entries()] == [("refine", "roster"), ("review", "a2")]
    assert turn_label("a1") == ("turn", "a1")


# --------------------------------------------------------------------------------------------- safety
@async_test()
async def test_a_broken_log_never_changes_what_a_call_returns_or_raises():
    llm, log = llm_with_log(Client(retries=[rate_limit()]))

    def explode(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("log broken")

    log.add = explode  # type: ignore[method-assign]
    assert (await llm.complete("a1", msgs(), None)).content == "PRIVATE ANSWER"
    failing, log2 = llm_with_log(Client(final=RuntimeError("real failure")))
    log2.add = explode  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="real failure"):
        await failing.complete("a1", msgs(), None)


@async_test()
async def test_a_label_that_raises_means_no_line_not_a_failed_call():
    def label(agent_id: str) -> tuple[str, str]:
        raise RuntimeError("no label")

    llm, log = llm_with_log(Client(), label=label)
    assert (await llm.complete("a1", msgs(), None)).content == "PRIVATE ANSWER" and len(log) == 0


@async_test()
async def test_a_binding_lookup_that_raises_still_surfaces_to_the_caller():
    llm = GatedEngineLlm(Client(), lambda a: (_ for _ in ()).throw(RuntimeError("no model bound")), LlmGate(), call_log=CallLog("s"))
    with pytest.raises(RuntimeError, match="no model bound"):
        await llm.complete("a1", msgs(), None)


# ----------------------------------------------------------------------------------------- LoggedLlm
class Port:
    """Any other :class:`LlmPort`: scripted bots, fakes."""

    def __init__(self, *, error: BaseException | None = None, delay: float = 0.0) -> None:
        self.error, self.delay, self.calls, self.marker = error, delay, 0, "port-attribute"

    async def complete(self, agent_id: str, messages: list[dict[str, Any]], tools: Any, *, max_tokens: int | None = None) -> LLMResponse:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return LLMResponse(content="PRIVATE", prompt_tokens=11, completion_tokens=4, finish_reason="tool_calls", provider_id="bot", model_id="scripted")


@async_test()
async def test_a_logged_port_records_one_line_per_request_and_passes_everything_through():
    log = CallLog("s1", clock=lambda: 7.0)
    port = Port()
    logged: LlmPort = LoggedLlm(port, log, lambda agent_id: binding("demo", "bound-model"))
    resp = await logged.complete("a1", msgs(), None, max_tokens=5)
    assert resp.content == "PRIVATE" and port.calls == 1
    (line,) = log.entries()
    assert (line["status"], line["actor_id"], line["purpose"], line["attempt"], line["gt"]) == ("ok", "a1", "turn", 1, 7.0)
    assert (line["provider_id"], line["model_id"], line["prompt_tokens"], line["finish_reason"]) == ("bot", "scripted", 11, "tool_calls")
    assert logged.marker == "port-attribute" and logged.calls == 1  # type: ignore[attr-defined]  # the port's own attributes stay reachable
    assert "PRIVATE" not in json.dumps(log.entries())


@async_test()
async def test_a_logged_port_records_failures_timeouts_and_cancellations():
    log = CallLog("s1")
    bound = lambda agent_id: binding("demo", "bound-model")  # noqa: E731
    failing = LoggedLlm(Port(error=RuntimeError("scripted failure sk-abcdef1234567890")), log, bound)
    with pytest.raises(RuntimeError, match="scripted failure"):
        await failing.complete("a1", msgs(), None)
    timing_out = LoggedLlm(Port(error=TimeoutError()), log, bound)
    with pytest.raises(TimeoutError):
        await timing_out.complete("a2", msgs(), None)
    slow = LoggedLlm(Port(delay=5.0), log, bound)
    task = asyncio.create_task(slow.complete("a1", msgs(), None))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    error, timeout, cancelled = log.entries()
    assert (error["status"], error["error_class"]) == ("error", "RuntimeError") and "sk-abcdef" not in error["message"]
    assert (error["provider_id"], error["model_id"]) == ("demo", "bound-model")
    assert (timeout["status"], timeout["actor_id"]) == ("timeout", "a2")
    assert cancelled["status"] == "cancelled"


@async_test()
async def test_a_logged_port_survives_a_binding_lookup_that_raises():
    log = CallLog("s1")
    logged = LoggedLlm(Port(), log, lambda agent_id: (_ for _ in ()).throw(RuntimeError("no binding")))
    assert (await logged.complete("a1", msgs(), None)).content == "PRIVATE"
    assert log.entries()[0]["provider_id"] == "bot"  # taken from the answer instead
    bare = LoggedLlm(Port(), log)  # no binding source at all
    await bare.complete("a1", msgs(), None)
    assert len(log) == 2


@async_test()
async def test_a_logged_port_with_a_label_of_its_own():
    log = CallLog("s1")
    logged = LoggedLlm(Port(), log, None, lambda agent_id: ("review", agent_id))
    await logged.complete("a3", msgs(), None)
    assert (log.entries()[0]["purpose"], log.entries()[0]["actor_id"]) == ("review", "a3")


def test_a_logged_port_does_not_recurse_on_a_missing_attribute():
    logged = LoggedLlm(Port(), CallLog("s"))
    with pytest.raises(AttributeError):
        logged.not_there  # type: ignore[attr-defined]
    bare = LoggedLlm.__new__(LoggedLlm)  # an object whose ``__init__`` never ran (a copy, an unpickle)
    with pytest.raises(AttributeError):
        bare.complete_nothing  # type: ignore[attr-defined]


def test_a_logged_port_hides_nothing_the_wrapped_port_has_under_a_public_name():
    """Review: the wrapper's own ``inner`` / ``call_log`` / ``binding_for`` / ``label`` shadowed a wrapped port's attributes of
    the same names.  Its state is private now: whatever the wrapped port offers is what the wrapper offers."""

    class Rich(Port):
        def __init__(self) -> None:
            super().__init__()
            self.inner, self.call_log, self.binding_for, self.label = "its inner", "its log", "its binding", "its label"
            self.gate, self.soft_gate, self.timeout_s = "its gate", "its soft gate", 7.5

    port = Rich()
    logged = LoggedLlm(port, CallLog("s"), lambda agent_id: binding(), lambda agent_id: ("review", agent_id))
    assert (logged.inner, logged.call_log, logged.binding_for, logged.label) == ("its inner", "its log", "its binding", "its label")
    assert (logged.gate, logged.soft_gate, logged.timeout_s, logged.marker) == ("its gate", "its soft gate", 7.5, "port-attribute")
    # ... and the wrapper still does its own work with ITS binding source and label
    import asyncio as _asyncio

    _asyncio.run(logged.complete("a3", msgs(), None))
    line = logged._call_log.entries()[0]
    assert (line["purpose"], line["actor_id"], line["provider_id"]) == ("review", "a3", "bot")


# ----------------------------------------------------- the log must not change what a model call does (review, v0.13.1)
# The gate records a failure from inside its ``except`` block: whatever the recording raises there would REPLACE the
# exception of the model call - the agent would retry (or give up) because of the log, not because of the model.
class BadStr(Exception):
    """An exception whose own text cannot be made."""

    def __str__(self) -> str:
        raise ValueError("__str__ exploded")


class BadAttrs(Exception):
    """An exception whose HTTP attributes raise when they are looked at."""

    @property
    def status_code(self) -> int:
        raise RuntimeError("status_code exploded")

    @property
    def response(self) -> Any:
        raise RuntimeError("response exploded")


class HostileAnswer:
    """An 'answer' of a model whose every attribute raises."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"no {name}")


@async_test()
async def test_an_exception_with_a_broken_text_reaches_the_caller_unchanged_with_or_without_a_log():
    original = BadStr("original")
    logged_llm, log = llm_with_log(Client(final=original))
    with pytest.raises(BadStr) as with_log:
        await logged_llm.complete("a1", msgs(), None)
    assert with_log.value is original  # the very same exception - not a ValueError the log made
    plain = GatedEngineLlm(Client(final=original), lambda agent_id: binding(), LlmGate(), 5)
    with pytest.raises(BadStr) as without_log:
        await plain.complete("a1", msgs(), None)
    assert without_log.value is original
    (line,) = log.entries()  # and the failure is in the log all the same, told by its class
    assert (line["status"], line["error_class"], line["message"], line["http_status"]) == ("error", "BadStr", "BadStr", None)
    assert llm_gate_in_flight(logged_llm) == 0


def llm_gate_in_flight(llm: GatedEngineLlm) -> int:
    return llm.gate.in_flight


@async_test()
async def test_an_exception_whose_attributes_raise_reaches_the_caller_unchanged_too():
    original = BadAttrs("original")
    logged_llm, log = llm_with_log(Client(final=original, retries=[BadAttrs("first")]))
    with pytest.raises(BadAttrs) as raised:
        await logged_llm.complete("a1", msgs(), None)
    assert raised.value is original
    retry, error = log.entries()
    assert (retry["status"], error["status"], error["http_status"], error["message"]) == ("retry", "error", None, "original")


@async_test()
async def test_a_logged_port_keeps_the_exception_of_the_port_it_wraps_whatever_the_text():
    for original in (BadStr("original"), BadAttrs("original")):
        log = CallLog("s1")
        logged = LoggedLlm(Port(error=original), log, lambda agent_id: binding())
        with pytest.raises(type(original)) as raised:
            await logged.complete("a1", msgs(), None)
        assert raised.value is original and [c["status"] for c in log.entries()] == ["error"]


@async_test()
async def test_the_hook_of_a_retry_never_raises_into_the_client_whatever_the_exception():
    client = Client(retries=[BadStr("one"), BadAttrs("two"), rate_limit()])
    llm, log = llm_with_log(client)
    assert (await llm.complete("a1", msgs(), None)).content == "PRIVATE ANSWER"  # the client went on with its retries
    assert [(c["status"], c["attempt"], c["error_class"]) for c in log.entries()] == [
        ("retry", 1, "BadStr"), ("retry", 2, "BadAttrs"), ("retry", 3, "RateLimitError"), ("ok", 4, None),
    ]


@async_test()
async def test_an_answer_whose_attributes_raise_is_returned_as_it_is_and_still_logged():
    answer = HostileAnswer()

    class Hostile:
        async def chat_stream(self, messages: Any, **kwargs: Any) -> Any:
            return answer

    llm, log = llm_with_log(Hostile())
    assert await llm.complete("a1", msgs(), None) is answer
    (line,) = log.entries()
    assert (line["status"], line["provider_id"], line["model_id"], line["prompt_tokens"]) == ("ok", "prov", "m-bound", None)
    port_log = CallLog("s2")
    port = Port()
    port.complete = lambda *a, **k: _answer(answer)  # type: ignore[method-assign]
    assert await LoggedLlm(port, port_log, lambda agent_id: binding()).complete("a1", msgs(), None) is answer
    assert [c["status"] for c in port_log.entries()] == ["ok"]


async def _answer(value: Any) -> Any:
    return value


@async_test()
async def test_a_cancellation_stays_a_cancellation_even_when_the_log_cannot_record_it():
    llm, log = llm_with_log(Client(delay=5.0), timeout_s=30)

    def explode(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("log broken")

    log.add = explode  # type: ignore[method-assign]
    running = asyncio.create_task(llm.complete("a1", msgs(), None))
    await wait_until(lambda: llm.gate.in_flight == 1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):  # not the RuntimeError of the log
        await running
    assert llm.gate.in_flight == 0
    port = LoggedLlm(Port(delay=5.0), log, lambda agent_id: binding())
    slow = asyncio.create_task(port.complete("a1", msgs(), None))
    await asyncio.sleep(0.05)
    slow.cancel()
    with pytest.raises(asyncio.CancelledError):
        await slow
