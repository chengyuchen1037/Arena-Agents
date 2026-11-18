"""Worker behaviour (v0.12.1): the model / tool loop without a round limit, nudges, failures and retries on a
persistent conversation, accounting and tracing, the three states, stopping."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.llm.types import LLMResponse, ToolCallData
from app.runtime.agent import CUT_OFF_NUDGE, Agent, AgentConfig
from app.runtime.ports import Brain, ToolOutcome
from app.world.types import Receipt

from .fakes import (
    FakeBrain,
    FakeLlm,
    FakeWorld,
    RecordingSink,
    RecordingTrace,
    Rig,
    Step,
    async_test,
    fast_config,
    llm_resp,
    normal,
    running_agent,
    settle,
    sleep_resp,
    speak_resp,
    urgent,
    wait_until,
)

SPEAK = ("speak", {"channel_id": "pub", "content": "hello"})


def wake(agent: Agent, label: str = "m", **kw) -> None:
    kw.setdefault("channel_id", "pub")
    agent.deliver(normal("message.new", label=label, **kw))


def events(envelopes):
    return [e.payload["event"] for e in envelopes]


def labels(envelopes):
    return [e.payload.get("label") for e in envelopes]


def roles(messages):
    return [m["role"] for m in messages]


def test_fake_brain_satisfies_the_brain_protocol():
    brain: Brain = FakeBrain()
    assert brain.system_prompt("a1")


# ============================================================================== the loop


@async_test()
async def test_the_request_is_the_system_prompt_plus_the_conversation_and_the_fixed_tools():
    rig = Rig()
    rig.llm.script("a1", sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    first = rig.llm.calls[0]
    assert roles(first.messages) == ["system", "user"]
    assert first.messages[0]["content"] == "SYSTEM[a1]" and first.messages[1]["content"].startswith("START[a1]")
    assert first.tools == rig.brain.tools  # the schemas of the brain, unchanged
    assert first.max_tokens == rig.world.spec.defaults.llm.max_tokens


@async_test()
async def test_tool_results_flow_back_and_the_conversation_grows_round_by_round():
    rig = Rig()
    rig.llm.script("a1", llm_resp(SPEAK), llm_resp(SPEAK, SPEAK), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert [roles(c.messages) for c in rig.llm.calls] == [
        ["system", "user"],
        ["system", "user", "assistant", "tool"],
        ["system", "user", "assistant", "tool", "assistant", "tool", "tool"],
    ]
    last = rig.llm.calls[2].messages
    assert last[4]["tool_calls"][0]["id"] == last[5]["tool_call_id"]
    assert last[4]["tool_calls"][1]["id"] == last[6]["tool_call_id"]
    assert json.loads(last[3]["content"])["ok"] is True
    assert rig.brain.names() == ["speak", "speak", "speak", "sleep"]
    assert [i.type for i in rig.world.submitted] == ["speak", "speak", "speak"]  # world tools went through submit
    assert all(i.actor_id == "a1" and i.payload["channel_id"] == "pub" for i in rig.world.submitted)
    assert all(c.paired for c in rig.llm.calls)
    # the whole run is in the conversation, ready for the next activation
    assert roles(rig.conversation().messages) == ["user", "assistant", "tool", "assistant", "tool", "tool", "assistant", "tool"]


@async_test()
async def test_there_is_no_round_limit_fifty_rounds_of_tools_are_not_cut_short():
    rig = Rig()
    rig.llm.script("a1", *[speak_resp(f"line {i}") for i in range(60)], sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1, timeout=10)
    assert len(rig.llm.calls) == 61 and agent.stats["activations"] == 1  # one activation, 61 model calls
    assert rig.brain.names() == ["speak"] * 60 + ["sleep"]
    end = rig.trace.of("run_end", "a1")[0]
    assert end["reason"] == "ends_run" and end["rounds"] == 61
    assert agent.stats["failures"] == 0 and rig.usage.by_actor["a1"].tool_calls == 61
    assert all(c.paired for c in rig.llm.calls)


@async_test()
async def test_a_sleep_ends_the_run_after_the_whole_batch_has_been_executed():
    rig = Rig()
    rig.llm.script("a1", llm_resp(SPEAK, ("sleep", {}), SPEAK), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
        await asyncio.sleep(0.1)
    assert rig.brain.names() == ["speak", "sleep", "speak"]  # every call of the response got its result
    assert len(rig.llm.calls) == 1  # but no further round
    assert rig.trace.of("run_end", "a1")[0]["reason"] == "ends_run"
    assert roles(rig.conversation().messages) == ["user", "assistant", "tool", "tool", "tool"]


@async_test()
async def test_the_wait_alias_and_sleep_are_the_same_to_the_runtime():
    rig = Rig()
    rig.brain.tool_handlers["wait"] = lambda agent_id, args, *, gen_id, seen_seq: ToolOutcome(text="{}", ends_run=True)
    rig.llm.script("a1", llm_resp(("wait", {})))
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.llm.calls) == 1 and rig.trace.of("run_end", "a1")[0]["reason"] == "ends_run"


@async_test()
async def test_a_nothing_to_tell_activation_calls_no_model_and_goes_straight_back_to_sleep():
    rig = Rig()
    rig.llm.script("a1", sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        rig.brain.inject_hook = lambda ctx: None  # the brain has nothing worth telling
        before = list(rig.sink.statuses("a1"))
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 2)
        await settle()
    assert len(rig.llm.calls) == 1 and agent.stats["activations"] == 2  # an activation, but no model call
    assert rig.sink.statuses("a1")[: len(before)] == before and rig.sink.statuses("a1")[len(before):] == []  # no flicker
    assert rig.trace.of("run_end", "a1")[1]["reason"] == "nothing" and agent.status == "sleeping"
    assert roles(rig.conversation().messages) == ["user", "assistant", "tool"]  # nothing was appended


# ================================================================================ nudges


@async_test()
async def test_the_nudge_is_offered_once_per_run_even_if_the_brain_would_keep_nudging():
    rig = Rig()
    rig.brain.nudge_text = "NUDGE"
    rig.llm.script(
        "a1",
        llm_resp(content="hmm"),  # run 1: plain text -> nudged
        llm_resp(content="still hmm"),  # run 1: plain text again -> the run ends, no second nudge
        llm_resp(content="again"),  # run 2: nudged again (once per run)
        sleep_resp(),
    )
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        assert len(rig.llm.calls) == 2
        assert rig.brain.nudge_calls == [("a1", "hmm", 0)]
        second = rig.llm.calls[1].messages
        assert roles(second) == ["system", "user", "assistant", "user"]
        assert second[2]["content"] == "hmm" and second[3]["content"] == "NUDGE"
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert [c[1] for c in rig.brain.nudge_calls] == ["hmm", "again"]
    assert len(rig.llm.calls) == 4


@async_test()
async def test_no_nudge_means_the_run_ends_at_the_first_plain_answer():
    rig = Rig()
    rig.brain.nudge_text = None
    rig.llm.script("a1", llm_resp(content="I will just think about it"), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
        await asyncio.sleep(0.05)
    assert len(rig.llm.calls) == 1 and len(rig.brain.nudge_calls) == 1
    assert rig.trace.of("content", "a1")[0]["text"] == "I will just think about it"  # inner monologue: log only
    assert rig.world.submitted == []
    # what the model said stays in its own conversation (so the next request still extends the same prefix)
    assert rig.conversation().messages[-1] == {"role": "assistant", "content": "I will just think about it"}


@async_test()
async def test_an_empty_answer_adds_no_blank_assistant_message_to_the_conversation():
    rig = Rig()
    rig.brain.nudge_text = "NUDGE"
    rig.llm.script("a1", llm_resp(content=""), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert roles(rig.llm.calls[1].messages) == ["system", "user", "user"]


@async_test()
async def test_a_cut_off_answer_without_tools_is_told_once_to_think_less_and_act():
    rig = Rig()
    rig.brain.nudge_text = None
    rig.llm.script("a1", llm_resp(content="", finish_reason="length"), llm_resp(content="", finish_reason="length"), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.llm.calls) == 2  # one recovery attempt per run, then the usual rules end it
    second = rig.llm.calls[1].messages
    assert roles(second) == ["system", "user", "user"] and second[2]["content"] == CUT_OFF_NUDGE
    assert "sleep" in CUT_OFF_NUDGE and rig.trace.of("nudge", "a1")[0]["why"] == "length"


@async_test()
async def test_the_nudge_and_a_pending_delta_arrive_in_one_user_message():
    rig = Rig()
    rig.brain.nudge_text = "NUDGE"
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(content="hmm")), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: len(rig.llm.calls) == 1)
        wake(agent, "m2")
        await settle()
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
    last = rig.llm.calls[1].messages
    assert roles(last) == ["system", "user", "assistant", "user"]
    assert last[3]["content"].startswith("NUDGE\n\nDELTA[a1]") and "#m2" in last[3]["content"]


# ================================================================================ rebuild


@async_test()
async def test_compact_context_replaces_the_conversation_with_a_fresh_full_message_and_keeps_system_and_tools():
    rig = Rig()
    rig.llm.script("a1", speak_resp(), llm_resp(("compact_context", {})), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.brain.starts) == 2  # the opening message and the rebuilt one
    assert rig.brain.starts[1].kind == "start" and labels(rig.brain.starts[1].reasons) == ["m1"]  # what woke this run
    assert rig.brain.resets == ["a1"]  # the brain forgets its per-conversation baselines first
    third = rig.llm.calls[2].messages
    assert roles(third) == ["system", "user"] and third[1]["content"].startswith("START[a1]")  # no old assistant / tool
    assert third[0] == rig.llm.calls[0].messages[0] and rig.llm.calls[2].tools == rig.llm.calls[0].tools  # byte for byte
    assert rig.trace.of("rebuild", "a1")[0]["epoch"] == 2 and rig.conversation().epoch == 2


@async_test()
async def test_a_rebuild_includes_what_arrived_meanwhile_as_wake_reasons():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(("compact_context", {}))), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: len(rig.llm.calls) == 1)
        wake(agent, "m2")
        await settle()
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert labels(rig.brain.starts[1].reasons) == ["m1", "m2"]
    assert rig.brain.injects == [] and len(rig.llm.calls) == 2 and agent.pending == []


# ================================================================================ failures


@async_test()
async def test_an_llm_failure_is_retried_with_backoff_and_the_conversation_is_left_untouched():
    rig = Rig()
    rig.llm.script("a1", RuntimeError("boom1"), TimeoutError("boom2"), sleep_resp())
    agent = rig.agent(retry_backoff_s=0.03)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1, timeout=3)
    assert len(rig.llm.calls) == 3 and agent.stats["failures"] == 2 and agent.stats["gave_up"] == 0
    # the very same request three times: nothing was appended on the way (the opening is not duplicated)
    assert rig.llm.calls[0].lines == rig.llm.calls[1].lines == rig.llm.calls[2].lines
    assert len(rig.brain.starts) == 1 and rig.brain.injects == []
    assert agent.stats["activations"] == 1 and rig.usage.by_actor["a1"].activations == 1  # a retry is no new activation
    errors = rig.trace.of("error", "a1")
    assert [e["attempt"] for e in errors] == [1, 2] and "boom1" in errors[0]["message"] and "TimeoutError" in errors[1]["message"]
    assert [r["delay_s"] for r in rig.trace.of("retry", "a1")] == [0.03, 0.06]  # exponential
    assert rig.sink.statuses("a1") == ["sleeping", "thinking", "acting", "sleeping"]  # the user sees no storm of states
    assert rig.sink.of_type("error") == []  # recovered: nobody is bothered


@async_test()
async def test_a_retry_after_a_failure_merges_what_arrived_during_the_backoff():
    rig = Rig()
    rig.llm.script("a1", RuntimeError("blip"), sleep_resp())
    agent = rig.agent(retry_backoff_s=0.2)
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["failures"] == 1)
        assert not agent.is_quiet()  # a retry is pending
        wake(agent, "m2")  # arrives during the back-off: the back-off is respected, no immediate attempt
        await asyncio.sleep(0.05)
        assert len(rig.llm.calls) == 1
        await wait_until(lambda: agent.stats["runs"] == 1, timeout=3)
    assert len(rig.llm.calls) == 2 and agent.stats["activations"] == 1
    second = rig.llm.calls[1].messages
    assert roles(second) == ["system", "user", "user"]  # the opening, then ONE delta for what arrived meanwhile
    assert second[1]["content"].startswith("START[a1]") and "#m2" in second[2]["content"]
    assert labels(rig.brain.injects[0].reasons) == ["m2"]


@async_test()
async def test_a_failure_that_persists_is_given_up_reported_and_the_agent_stays_alive():
    rig = Rig()
    rig.llm.script("a1", RuntimeError("x1"), RuntimeError("x2"), RuntimeError("x3"), sleep_resp())
    rig.world.names["a1"] = "Alice"
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["gave_up"] == 1, timeout=3)
        await asyncio.sleep(0.1)
        assert len(rig.llm.calls) == 3  # 1 attempt + 2 retries, then it stops
        assert agent.status == "sleeping" and agent.is_quiet()
        (error,) = rig.sink.of_type("error")
        assert "Alice" in error.data["message"] and "a1" in error.data["message"] and "x3" in error.data["message"]
        assert error.data["agent_id"] == "a1"
        # still alive: the next message works and the failure count starts from zero
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.llm.calls) == 4
    # the conversation went on from where it was: the opening (unanswered), then the new delta
    assert roles(rig.llm.calls[3].messages) == ["system", "user", "user"]


@async_test()
async def test_an_agent_that_fails_does_not_disturb_the_others():
    rig = Rig()
    rig.llm.script("a1", RuntimeError("down"), RuntimeError("down"), RuntimeError("down"))
    rig.llm.script("a2", llm_resp(SPEAK), sleep_resp())
    broken, healthy = rig.agent("a1"), rig.agent("a2")
    async with running_agent(broken), running_agent(healthy):
        wake(broken)
        wake(healthy)
        await wait_until(lambda: healthy.stats["runs"] == 1 and broken.stats["gave_up"] == 1, timeout=3)
        assert healthy.stats["failures"] == 0 and healthy.status == "sleeping"
        assert [i.actor_id for i in rig.world.submitted] == ["a2"]
    assert len(rig.sink.of_type("error")) == 1


@async_test()
async def test_a_brain_bug_while_building_the_opening_is_retried_with_the_same_reasons():
    rig = Rig()
    rig.llm.script("a1", sleep_resp())
    original = rig.brain.start_message
    attempts = {"n": 0}

    def flaky(agent_id, ctx):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("context bug")
        return original(agent_id, ctx)

    rig.brain.start_message = flaky  # type: ignore[method-assign]
    agent = rig.agent(retry_backoff_s=0.1)
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["failures"] == 1)
        assert rig.llm.calls == [] and rig.conversation() is None  # nothing was told, nothing was created
        wake(agent, "m2")  # during the back-off
        await wait_until(lambda: agent.stats["runs"] == 1, timeout=3)
    # the retry's opening knows the reasons of the failed attempt AND the newcomer, in order
    # (the failed attempt raised before the fake could record it: ``starts`` holds the successful one)
    assert attempts["n"] == 2 and len(rig.brain.starts) == 1
    assert labels(rig.brain.starts[0].reasons) == ["m1", "m2"] and len(rig.llm.calls) == 1


@async_test()
async def test_a_brain_bug_that_never_goes_away_is_given_up_without_a_loop():
    rig = Rig()
    rig.brain.start_error = RuntimeError("context bug")
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["gave_up"] == 1, timeout=3)
        await asyncio.sleep(0.15)  # nothing keeps retrying or re-activating by itself
        assert rig.llm.calls == [] and agent.is_quiet() and len(rig.brain.starts) == 3
        rig.brain.start_error = None
        rig.llm.script("a1", sleep_resp())
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert labels(rig.brain.starts[-1].reasons) == ["m2"]  # the reasons of the given-up attempts are not resurrected


@async_test()
async def test_a_brain_bug_while_merging_loses_nothing_the_agent_is_told_with_the_next_activation():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=speak_resp()), sleep_resp(), sleep_resp())
    original = rig.brain.inject_message
    state = {"fail": True}

    def flaky(agent_id, ctx):
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("inject bug")
        return original(agent_id, ctx)

    rig.brain.inject_message = flaky  # type: ignore[method-assign]
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: len(rig.llm.calls) == 1)
        wake(agent, "m2")  # arrives during the call; the merge at the boundary blows up
        await settle()
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 2, timeout=3)
    assert agent.stats["failures"] == 0 and agent.stats["activations"] == 2  # the run went on, then the agent woke again
    assert any("inject bug" in e["message"] for e in rig.trace.of("error", "a1"))
    last = rig.llm.calls[-1].messages
    assert last[-1]["content"].startswith("DELTA[a1]") and "#m2" in last[-1]["content"]  # m2 reached the model after all
    assert labels(rig.brain.injects[-1].reasons) == ["m2"] and agent.pending == []


@async_test()
async def test_a_brain_bug_in_begin_run_is_traced_and_the_run_goes_on():
    rig = Rig()
    rig.llm.script("a1", sleep_resp())

    def broken(agent_id):
        raise RuntimeError("begin bug")

    rig.brain.begin_run = broken  # type: ignore[method-assign]
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert agent.stats["failures"] == 0 and len(rig.llm.calls) == 1
    assert any("begin bug" in e["message"] for e in rig.trace.of("error", "a1"))


@async_test()
async def test_a_tool_that_raises_becomes_an_error_result_and_the_run_goes_on():
    rig = Rig()

    def broken(agent_id, args, *, gen_id, seen_seq):
        raise RuntimeError("tool bug")

    rig.brain.tool_handlers["speak"] = broken
    rig.llm.script("a1", llm_resp(SPEAK), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    result = rig.llm.calls[1].messages[3]
    assert result["role"] == "tool" and json.loads(result["content"])["ok"] is False
    assert "RuntimeError" in result["content"] and "tool bug" in result["content"]
    assert agent.stats["failures"] == 0
    assert any("tool bug" in e["message"] for e in rig.trace.of("error", "a1"))
    assert rig.usage.by_actor["a1"].tool_calls == 2


@async_test()
async def test_malformed_tool_arguments_never_reach_the_brain_and_the_model_is_told():
    rig = Rig()
    bad = ToolCallData(id="bad", name="speak", arguments_json="{not json")
    rig.llm.script("a1", llm_resp(bad), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.brain.names() == ["sleep"]  # ``speak`` was never executed
    result = rig.llm.calls[1].messages[3]
    assert result["tool_call_id"] == "bad" and json.loads(result["content"])["ok"] is False
    assert "speak" in result["content"]
    traced = rig.trace.of("tool_call", "a1")[0]
    assert traced["name"] == "speak" and traced["args"]["_raw"] == "{not json"


@async_test()
async def test_tool_call_ids_are_made_unique_so_every_call_has_exactly_one_result():
    rig = Rig()
    calls = [
        ToolCallData(id="x", name="speak", arguments_json='{"channel_id":"pub","content":"1"}'),
        ToolCallData(id="x", name="speak", arguments_json='{"channel_id":"pub","content":"2"}'),
        ToolCallData(id="", name="speak", arguments_json='{"channel_id":"pub","content":"3"}'),
    ]
    rig.llm.script("a1", llm_resp(*calls), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    messages = rig.llm.calls[1].messages
    asked = [c["id"] for c in messages[2]["tool_calls"]]
    answered = [m["tool_call_id"] for m in messages[3:]]
    assert len(set(asked)) == 3 and all(asked) and asked == answered
    assert all(c.paired for c in rig.llm.calls)


@async_test()
async def test_a_rejected_commit_is_just_a_tool_result_and_counts_as_a_rejection():
    rig = Rig()
    rig.world.on_submit = lambda intent: Receipt.reject("NO_FLOOR", "not your turn")
    rig.llm.script("a1", llm_resp(SPEAK), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    result = json.loads(rig.llm.calls[1].messages[3]["content"])
    assert result["ok"] is False and result["code"] == "NO_FLOOR"  # the receipt is the tool result
    assert rig.usage.by_actor["a1"].rejections == 1


@async_test()
async def test_a_garbage_tool_outcome_from_the_brain_is_survived():
    rig = Rig()
    rig.brain.tool_handlers["speak"] = lambda agent_id, args, *, gen_id, seen_seq: "not a ToolOutcome"  # type: ignore[assignment,return-value]
    rig.llm.script("a1", llm_resp(SPEAK), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.llm.calls[1].messages[3] == {"role": "tool", "tool_call_id": rig.llm.calls[1].messages[2]["tool_calls"][0]["id"], "content": "not a ToolOutcome"}


@async_test()
async def test_a_broken_usage_trace_or_event_sink_never_kills_a_run():
    class Boom:
        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise RuntimeError(f"{name} is down")

            return fail

    world, brain, llm = FakeWorld(), None, FakeLlm()
    brain = FakeBrain(world)
    llm.script("a1", llm_resp(SPEAK, thought="hmm", content="text"), sleep_resp())
    agent = Agent("a1", "player", world, brain, llm, Boom(), Boom(), Boom(), fast_config())  # type: ignore[arg-type]
    agent.activate()
    wake(agent)
    await wait_until(lambda: agent.stats["runs"] == 1)
    await agent.stop()
    assert agent.stats["failures"] == 0 and [i.type for i in world.submitted] == ["speak"]


# ======================================================================= accounting & trace


@async_test()
async def test_usage_and_trace_follow_the_contract():
    rig = Rig()

    def reject(agent_id, args, *, gen_id, seen_seq):
        receipt = Receipt.reject("NO_FLOOR", "no floor", holders=["a2"])
        return ToolOutcome(text=receipt.tool_json(), receipt=receipt)

    rig.brain.tool_handlers["speak"] = reject
    rig.llm.script(
        "a1",
        llm_resp(
            SPEAK,
            ("sleep", {}),
            thought="let me think",
            content="my inner voice",
            prompt_tokens=300,
            completion_tokens=40,
            cache_read_tokens=700,
            cache_write_tokens=50,
            provider_id="prov",
            model_id="m1",
            input_price_per_m=1.0,
            output_price_per_m=2.0,
            cache_read_price_per_m=0.1,
            cache_write_price_per_m=1.25,
        ),
    )
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    actor = rig.usage.by_actor["a1"]
    assert (actor.activations, actor.calls, actor.tool_calls, actor.thinking_rounds, actor.rejections) == (1, 1, 2, 1, 1)
    assert (actor.prompt_tokens, actor.cache_read_tokens, actor.cache_write_tokens, actor.completion_tokens) == (300, 700, 50, 40)
    assert actor.cost_usd == pytest.approx((300 * 1.0 + 700 * 0.1 + 50 * 1.25 + 40 * 2.0) / 1e6)
    assert actor.cache_hit_rate == pytest.approx(700 / 1050) and "prov::m1" in rig.usage.by_model

    names = rig.trace.names("a1")
    for expected in ("wake", "conversation", "llm_call", "thought", "content", "tool_call", "receipt", "run_end"):
        assert expected in names, expected
    call = rig.trace.of("llm_call", "a1")[0]
    assert call["model"] == "m1" and call["prompt_tokens"] == 300 and call["tool_calls"] == ["speak", "sleep"]
    assert (call["cache_read_tokens"], call["cache_write_tokens"], call["epoch"]) == (700, 50, 1)
    assert rig.trace.of("thought", "a1")[0]["text"] == "let me think"
    tool_calls = rig.trace.of("tool_call", "a1")
    assert [t["name"] for t in tool_calls] == ["speak", "sleep"] and tool_calls[0]["args"]["content"] == "hello"
    receipt = rig.trace.of("receipt", "a1")[0]
    assert receipt["ok"] is False and receipt["code"] == "NO_FLOOR"
    assert "messages" not in call  # full prompts are only traced on request


@async_test()
async def test_the_conversation_can_be_traced_on_request():
    rig = Rig()
    rig.llm.script("a1", sleep_resp())
    agent = rig.agent(trace_messages=True)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert roles(rig.trace.of("llm_call", "a1")[0]["messages"]) == ["system", "user"]


@async_test()
async def test_max_tokens_comes_from_the_scenario_unless_overridden():
    rig = Rig()
    rig.world.spec.defaults.llm.max_tokens = 777
    rig.llm.script("a1", sleep_resp(), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        agent.config.max_tokens = 55
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert [c.max_tokens for c in rig.llm.calls] == [777, 55]


# ============================================================================ the three states


@async_test()
async def test_an_agent_is_thinking_acting_or_sleeping_and_nothing_else():
    rig = Rig()
    rig.llm.script("a1", speak_resp(), sleep_resp())
    agent = rig.agent()
    assert agent.status == "sleeping"
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
        assert agent.status == "sleeping"
    assert rig.sink.status_events("a1") == [
        ("sleeping", None), ("thinking", None), ("acting", None), ("thinking", None), ("acting", None), ("sleeping", None),
    ]
    assert {e.data["state"] for e in rig.sink.of_type("agent.status")} == {"sleeping", "thinking", "acting"}  # never idle / stopped
    assert all(set(e.data) == {"id", "state", "until"} for e in rig.sink.of_type("agent.status"))


@async_test()
async def test_a_status_is_published_only_when_it_changes():
    rig = Rig()
    rig.llm.script("a1", *[speak_resp() for _ in range(5)], sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    states = rig.sink.statuses("a1")
    assert all(a != b for a, b in zip(states, states[1:]))  # no two equal events in a row


# ================================================================================== lifecycle


@async_test()
async def test_stop_cancels_a_running_worker_drops_the_pending_and_shows_sleeping():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=sleep_resp()))
    agent = rig.agent()
    agent.activate()
    wake(agent, "m1")
    await wait_until(lambda: len(rig.llm.calls) == 1)
    wake(agent, "m2")  # pending when the stop comes
    stopping = agent.stop()
    assert agent.status == "sleeping" and agent.pending == [] and not agent.busy  # the stop itself is synchronous
    await stopping
    assert rig.llm.cancelled == ["a1"] and agent.is_quiet()
    assert rig.sink.statuses("a1") == ["sleeping", "thinking", "sleeping"]
    await agent.stop()  # idempotent: no further event
    assert rig.sink.statuses("a1") == ["sleeping", "thinking", "sleeping"]
    await asyncio.sleep(0.1)
    assert len(rig.llm.calls) == 1  # nothing restarts by itself
    assert roles(rig.conversation().messages) == ["user"]  # the conversation survives a stop


@async_test()
async def test_a_stopped_agent_can_be_armed_again_and_continues_its_conversation():
    rig = Rig()
    rig.llm.script("a1", sleep_resp(), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
    async with running_agent(agent):  # pause -> resume with the very same agent
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert len(rig.brain.starts) == 1 and len(rig.brain.injects) == 1  # one conversation, a delta for the second activation
    assert rig.sink.statuses("a1") == [
        "sleeping", "thinking", "acting", "sleeping", "sleeping", "thinking", "acting", "sleeping",
    ]  # (the repeated "sleeping" is the new arming: a client that connected meanwhile sees the state again)


@async_test()
async def test_a_stop_requested_before_the_first_activation_ran_is_not_forgotten():
    rig = Rig()
    agent = rig.agent()
    agent.activate()
    wake(agent)
    await agent.stop()  # the scheduled activation has not had its turn yet
    await asyncio.sleep(0.05)
    assert rig.llm.calls == [] and agent.status == "sleeping" and agent.stats["activations"] == 0


@async_test()
async def test_is_quiet_reflects_pending_work():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=sleep_resp()))
    agent = rig.agent()
    async with running_agent(agent):
        assert agent.is_quiet()
        wake(agent)
        assert not agent.is_quiet()  # an activation is scheduled
        await wait_until(lambda: len(rig.llm.calls) == 1)
        assert not agent.is_quiet() and agent.busy  # a worker is running
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
        assert agent.is_quiet() and not agent.busy
