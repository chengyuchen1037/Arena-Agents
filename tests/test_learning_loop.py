"""The loop of a learning run (v0.14, H20, H54; contract §13.6): ``app.learning.loop.run_learning``.

A fake brain and a scripted model: the loop's own rules are what is tested - the wall clock around the whole run (a call in flight included), the
safety net of rounds, one nudge for an answer without a tool call, every tool call answered and in order, a failing model or tool never raises,
nothing of a prompt reaches the trace.  Nothing here calls a model.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.learning.loop import CUT_OFF_NUDGE, MAX_ROUNDS, run_learning
from app.llm.types import LLMResponse, ToolCallData
from app.runtime.ports import ToolOutcome
from game_fakes import async_test
from learning_fakes import text_answer, tool_calls

SCHEMA = [{"type": "function", "function": {"name": "act", "description": "d", "parameters": {"type": "object", "properties": {}}}}]


class FakeBrain:
    """What the loop asks of a brain; every tool call is kept, ``finish`` ends the run, a text answer is nudged once."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.nudges: list[tuple[str, int]] = []
        self.written: tuple[str, ...] = ()
        self.explode = False

    def system_prompt(self) -> str:
        return "SYSTEM-PROMPT-TEXT"

    def first_message(self) -> str:
        return "FIRST-MESSAGE-TEXT"

    def tool_schemas(self) -> list[dict[str, Any]]:
        return SCHEMA

    def execute_tool(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        self.calls.append((name, args))
        if self.explode:
            raise RuntimeError("the tool is broken")
        if name == "write":
            self.written = (*self.written, str(args.get("file")))
        return ToolOutcome(text=f"done {name}", ends_run=(name == "finish"))

    def no_tool_nudge(self, content: str, round_idx: int) -> str | None:
        self.nudges.append((content, round_idx))
        return "NUDGE-TEXT" if len(self.nudges) == 1 else None


class Script:
    """A model that answers from a list (the last answer repeats); ``requests`` keeps every conversation it was asked to continue."""

    def __init__(self, *answers: LLMResponse | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[list[dict[str, Any]]] = []
        self.seen_tools: list[Any] = []
        self.max_tokens: list[int | None] = []

    async def complete(self, agent_id: str, messages: list[dict[str, Any]], tools: Any, *, max_tokens: int | None = None) -> LLMResponse:
        self.requests.append([dict(m) for m in messages])
        self.seen_tools.append(tools)
        self.max_tokens.append(max_tokens)
        answer = self.answers[min(len(self.requests) - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Sink:
    """A trace sink that keeps what it is given."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str, dict[str, Any]]] = []

    def write(self, event: str, agent_id: str, **fields: Any) -> None:
        self.lines.append((event, agent_id, fields))


# ------------------------------------------------------------------------------------------------------------------ the rounds
@async_test
async def test_every_tool_call_of_an_answer_is_carried_out_in_order_and_answered_before_the_model_is_asked_again() -> None:
    brain = FakeBrain()
    llm = Script(
        tool_calls([("read", {"source": "a"}), ("write", {"file": "x"}), ("read", {"source": "b"})]),
        tool_calls([("write", {"file": "y"}), ("finish", {})]),
    )
    result = await run_learning(brain, llm, "b-00000001", deadline_s=5, max_tokens=777)
    assert (result.status, result.rounds, result.error, result.capped) == ("finished", 2, None, False)
    assert [name for name, _ in brain.calls] == ["read", "write", "read", "write", "finish"]
    first, second = llm.requests
    assert [m["role"] for m in first] == ["system", "user"]
    assert first[0]["content"] == "SYSTEM-PROMPT-TEXT" and first[1]["content"] == "FIRST-MESSAGE-TEXT"
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool", "tool", "tool"]  # every call has its result
    assert [m["content"] for m in second[3:]] == ["done read", "done write", "done read"]
    call_ids = [c["id"] for c in second[2]["tool_calls"]]
    assert [m["tool_call_id"] for m in second[3:]] == call_ids and len(set(call_ids)) == 3
    assert llm.max_tokens == [777, 777] and all(tools == SCHEMA for tools in llm.seen_tools)


@async_test
async def test_an_answer_without_a_tool_call_is_nudged_once_and_the_second_one_ends_the_run() -> None:
    brain = FakeBrain()
    llm = Script(text_answer("我写好了。"), text_answer("真的写好了。"), tool_calls([("finish", {})]))
    result = await run_learning(brain, llm, "b-00000001", deadline_s=5)
    assert (result.status, result.rounds) == ("finished", 2)  # the third answer was never asked for
    assert brain.calls == [] and [content for content, _ in brain.nudges] == ["我写好了。", "真的写好了。"]
    second = llm.requests[1]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]
    assert second[2]["content"] == "我写好了。" and second[3]["content"] == "NUDGE-TEXT"  # what it said stays; the nudge is a user message


@async_test
async def test_an_answer_that_was_cut_off_by_the_length_limit_is_told_so_once_before_the_brains_own_nudge() -> None:
    brain = FakeBrain()
    llm = Script(text_answer("写到一半……", finish_reason="length"), tool_calls([("finish", {})]))
    result = await run_learning(brain, llm, "b-00000001", deadline_s=5)
    assert (result.status, result.rounds) == ("finished", 2) and brain.nudges == []
    assert llm.requests[1][-1] == {"role": "user", "content": CUT_OFF_NUDGE}
    assert "长度上限" in CUT_OFF_NUDGE


@async_test
async def test_the_safety_net_ends_a_run_that_never_finishes() -> None:
    assert MAX_ROUNDS == 60  # the contract's number
    brain = FakeBrain()
    llm = Script(tool_calls([("read", {})]))
    result = await run_learning(brain, llm, "b-00000001", deadline_s=30, max_rounds=5)
    assert (result.status, result.rounds, result.capped) == ("finished", 5, True) and len(brain.calls) == 5


@async_test
async def test_arguments_that_are_not_a_json_object_never_reach_the_brain_and_the_model_is_told() -> None:
    brain = FakeBrain()
    bad = LLMResponse(
        tool_calls=[ToolCallData(id="c1", name="write", arguments_json="{not json"), ToolCallData(id="c2", name="write", arguments_json="[1]")],
        finish_reason="tool_calls",
    )
    llm = Script(bad, tool_calls([("finish", {})]))
    result = await run_learning(brain, llm, "b-00000001", deadline_s=5)
    assert result.status == "finished" and [name for name, _ in brain.calls] == ["finish"]
    results = [m["content"] for m in llm.requests[1] if m["role"] == "tool"]
    assert len(results) == 2 and all("参数" in text and "write" in text for text in results)


@async_test
async def test_a_tool_that_raises_is_answered_and_the_run_goes_on() -> None:
    brain = FakeBrain()
    brain.explode = True
    llm = Script(tool_calls([("write", {"file": "x"})]), text_answer("好"), text_answer("好"))
    result = await run_learning(brain, llm, "b-00000001", deadline_s=5)
    assert result.status == "finished" and result.rounds == 3
    (answer,) = [m["content"] for m in llm.requests[1] if m["role"] == "tool"]
    assert "write" in answer and "RuntimeError" in answer and "the tool is broken" in answer  # (the call stays paired with a result)


@async_test
async def test_progress_and_every_answer_are_reported_after_each_round() -> None:
    brain = FakeBrain()
    llm = Script(tool_calls([("write", {"file": "arch_notes"})]), tool_calls([("write", {"file": "digest"}), ("finish", {})]))
    progress: list[dict[str, Any]] = []
    answers: list[LLMResponse] = []
    await run_learning(brain, llm, "b-00000001", deadline_s=5, on_progress=progress.append, on_response=answers.append)
    assert [(p["rounds"], p["written"]) for p in progress] == [(1, ["arch_notes"]), (2, ["arch_notes", "digest"])]
    assert all(isinstance(p["elapsed_s"], float) for p in progress) and len(answers) == 2
    broken = FakeBrain()

    def explode(_info: Any) -> None:
        raise RuntimeError("the page is gone")

    result = await run_learning(broken, Script(tool_calls([("finish", {})])), "b-00000001", deadline_s=5, on_progress=explode, on_response=explode)
    assert result.status == "finished"  # a callback never ends a run


# ------------------------------------------------------------------------------------------------------------------- the clock
@async_test
async def test_the_wall_clock_cancels_the_call_in_flight_and_ends_the_run() -> None:
    started = asyncio.Event()
    cancelled: list[bool] = []

    class Hung:
        async def complete(self, agent_id: str, messages: Any, tools: Any, *, max_tokens: int | None = None) -> LLMResponse:
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return text_answer("never")

    loop_time = asyncio.get_running_loop().time
    began = loop_time()
    result = await run_learning(FakeBrain(), Hung(), "b-00000001", deadline_s=0.2)
    assert result.status == "timeout" and result.rounds == 1 and "时间到了" in (result.error or "")
    assert started.is_set() and cancelled == [True]  # the clock covers the model call itself
    assert loop_time() - began < 3


@async_test
async def test_the_clock_covers_the_whole_run_not_one_call() -> None:
    class Slow:
        def __init__(self) -> None:
            self.n = 0

        async def complete(self, agent_id: str, messages: Any, tools: Any, *, max_tokens: int | None = None) -> LLMResponse:
            self.n += 1
            await asyncio.sleep(0.08)  # every call is quick; there are just always more of them
            return tool_calls([("read", {})])

    brain = FakeBrain()
    result = await run_learning(brain, Slow(), "b-00000001", deadline_s=0.35)
    assert result.status == "timeout" and 1 <= result.rounds <= 8 and brain.calls  # what was done before the time ran out was done


@async_test
async def test_a_call_that_times_out_by_itself_is_a_failed_call_not_the_end_of_the_clock() -> None:
    llm = Script(TimeoutError("the gateway gave up"))
    result = await run_learning(FakeBrain(), llm, "b-00000001", deadline_s=30)
    assert result.status == "error" and "超时" in (result.error or "") and "the gateway gave up" in (result.error or "")


# --------------------------------------------------------------------------------------------------------------- failures
@async_test
async def test_a_model_that_cannot_be_reached_ends_the_run_with_its_reason_and_raises_nothing() -> None:
    sink = Sink()
    llm = Script(RuntimeError("401 unauthorized"))
    result = await run_learning(FakeBrain(), llm, "b-00000001", deadline_s=5, trace=sink)
    assert (result.status, result.rounds) == ("error", 1) and result.error == "RuntimeError：401 unauthorized"
    assert sink.lines[-1][0] == "end" and sink.lines[-1][2]["status"] == "error"


@async_test
async def test_cancelling_the_task_of_a_run_is_not_swallowed() -> None:
    gate = asyncio.Event()

    class Waits:
        async def complete(self, agent_id: str, messages: Any, tools: Any, *, max_tokens: int | None = None) -> LLMResponse:
            await gate.wait()
            return text_answer("x")

    task = asyncio.ensure_future(run_learning(FakeBrain(), Waits(), "b-00000001", deadline_s=30))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@async_test
async def test_the_trace_has_what_was_done_and_never_a_prompt_or_a_text_a_bot_wrote() -> None:
    sink = Sink()
    secret_text = "我写的很长的一段话" * 40
    brain = FakeBrain()
    llm = Script(tool_calls([("write", {"file": "digest", "content": secret_text})]), text_answer("不调用工具"), text_answer("还是不调用"))
    await run_learning(brain, llm, "b-00000001", deadline_s=5, trace=sink)
    events = [event for event, _, _ in sink.lines]
    assert events.count("llm_call") == 3 and "tool_call" in events and "nudge" in events and events[-1] == "end"
    dumped = json.dumps(sink.lines, ensure_ascii=False)
    assert "SYSTEM-PROMPT-TEXT" not in dumped and "FIRST-MESSAGE-TEXT" not in dumped and "NUDGE-TEXT" not in dumped
    assert secret_text not in dumped  # (a long text is cut: a log line is no place for a memory file)
    (tool_line,) = [fields for event, _, fields in sink.lines if event == "tool_call"]
    assert tool_line["name"] == "write" and tool_line["args"]["file"] == "digest" and tool_line["result_chars"] == len("done write")
    assert tool_line["args"]["content"] == f"（{len(secret_text)} 字）"  # what a bot writes is logged as its length
    assert all(agent == "b-00000001" for _, agent, _ in sink.lines)


@async_test
async def test_a_trace_that_fails_never_ends_a_run() -> None:
    class Broken:
        def write(self, *a: Any, **k: Any) -> None:
            raise OSError("disk full")

    result = await run_learning(FakeBrain(), Script(tool_calls([("finish", {})])), "b-00000001", deadline_s=5, trace=Broken())
    assert result.status == "finished"
