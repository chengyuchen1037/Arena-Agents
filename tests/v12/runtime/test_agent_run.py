"""Worker behaviour: the LLM/tool loop, nudge, rebuild, budgets, retries, accounting, lifecycle."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.llm.types import ToolCallData
from app.runtime.agent import Agent, AgentConfig
from app.runtime.ports import Brain, ToolOutcome
from app.world.types import Receipt

from .fakes import (
    FakeBrain,
    Rig,
    Step,
    ambient,
    async_test,
    fast_config,
    llm_resp,
    normal,
    running_agent,
    settle,
    urgent,
    wait_resp,
    wait_until,
)

SPEAK = ("speak", {"channel_id": "pub", "content": "hello"})
WAKE = ("floor.granted", {"channel_id": "pub"})


def wake(agent: Agent, event: str = "floor.granted", **kw) -> None:
    kw.setdefault("channel_id", "pub")
    agent.deliver(urgent(event, **kw))


def events(envelopes):
    return [e.payload["event"] for e in envelopes]


def roles(messages):
    return [m["role"] for m in messages]


def test_fake_brain_satisfies_the_brain_protocol():
    brain: Brain = FakeBrain()
    assert brain.system_prompt("a1")


# =============================================================================== the loop


@async_test()
async def test_tool_results_flow_back_and_the_conversation_grows_round_by_round():
    rig = Rig()
    rig.llm.script("a1", llm_resp(SPEAK), llm_resp(SPEAK, SPEAK), wait_resp())
    agent = rig.agent(execution_depth="high")
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
    assert rig.brain.names() == ["speak", "speak", "speak", "wait"]
    assert [i.type for i in rig.world.submitted] == ["speak", "speak", "speak"]  # world tools went through submit
    assert all(i.actor_id == "a1" and i.payload["channel_id"] == "pub" for i in rig.world.submitted)


@async_test()
async def test_messages_handed_to_the_model_are_snapshots_not_the_live_list():
    rig = Rig()
    rig.llm.script("a1", llm_resp(SPEAK), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.llm.calls[0].messages) == 2  # later rounds did not grow the first call's list


@async_test()
async def test_ends_run_stops_the_run_after_the_whole_batch_has_been_executed():
    rig = Rig()
    rig.llm.script("a1", llm_resp(SPEAK, ("wait", {}), SPEAK), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
        await asyncio.sleep(0.1)
    assert rig.brain.names() == ["speak", "wait", "speak"]  # every call of the response got its result
    assert len(rig.llm.calls) == 1  # but no further round
    assert rig.trace.of("run_end", "a1")[0]["reason"] == "ends_run"


@async_test()
async def test_rebuild_replaces_the_conversation_with_a_fresh_context_from_the_world():
    rig = Rig()
    rig.llm.script("a1", llm_resp(("compact_context", {})), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.brain.starts) == 2
    assert rig.brain.starts[1].kind == "start" and events(rig.brain.starts[1].reasons) == ["floor.granted"]
    assert roles(rig.llm.calls[1].messages) == ["system", "user"]  # no old assistant / tool messages
    assert rig.llm.calls[1].messages[1]["content"].startswith("START[a1]")
    assert rig.trace.of("rebuild", "a1")


@async_test()
async def test_rebuild_includes_what_arrived_meanwhile_as_wake_reasons():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(("compact_context", {}))), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(urgent("request.opened", kind="request", request_id="r1"))
        await settle()
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert events(rig.brain.starts[1].reasons) == ["floor.granted", "request.opened"]
    assert rig.brain.injects == [] and len(rig.llm.calls) == 2


@async_test()
async def test_the_round_budget_depends_on_agent_kind_and_execution_depth():
    rig = Rig()
    rig.llm.default = lambda call: llm_resp(SPEAK)  # never stops by itself
    config = fast_config(execution_depth="low")
    player = rig.agent("a1", "player", config)
    moderator = rig.agent("moderator", "moderator", config)
    async with running_agent(player), running_agent(moderator):
        wake(player)
        wake(moderator)
        await wait_until(lambda: player.stats["runs"] == 1 and moderator.stats["runs"] == 1)
        assert len(rig.llm.calls_of("a1")) == 3  # player / low
        assert len(rig.llm.calls_of("moderator")) == 6  # moderator / low
        config.execution_depth = "high"  # shared, mutable: the next run uses the new depth
        wake(player)
        wake(moderator)
        await wait_until(lambda: player.stats["runs"] == 2 and moderator.stats["runs"] == 2)
    assert len(rig.llm.calls_of("a1")) == 3 + 8
    assert len(rig.llm.calls_of("moderator")) == 6 + 16
    assert rig.trace.of("run_end", "a1")[0]["reason"] == "rounds"


def test_default_round_table_matches_the_contract():
    config = AgentConfig()
    expected = {"player": (3, 5, 8, 12), "moderator": (6, 10, 16, 24)}
    for kind, values in expected.items():
        got = []
        for depth in ("low", "medium", "high", "xhigh"):
            config.execution_depth = depth
            got.append(config.rounds_for(kind))
        assert tuple(got) == values
    config.execution_depth = "bogus"  # an unknown depth falls back to medium
    assert config.rounds_for("player") == 5


@async_test()
async def test_an_urgent_envelope_that_arrives_in_the_last_round_starts_the_next_run():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", llm_resp(SPEAK), llm_resp(SPEAK), Step(hold=hold, resp=llm_resp(SPEAK)))
    rig.llm.default = lambda call: wait_resp()  # the follow-up run ends at once
    agent = rig.agent(execution_depth="low")  # 3 rounds for a player
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: len(rig.llm.calls) == 3)  # the third (last) round is in flight
        agent.deliver(urgent("request.opened", kind="request", request_id="r1"))
        await settle()
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert rig.trace.of("run_end", "a1")[0]["reason"] == "rounds"
    assert events(rig.brain.starts[1].reasons) == ["request.opened"]  # not lost: it started the next run
    assert all(not ctx.reasons for ctx in rig.brain.injects)  # and it was not wasted on the exhausted one


# =============================================================================== nudge


@async_test()
async def test_the_nudge_is_offered_once_per_run_even_if_the_brain_would_keep_nudging():
    rig = Rig()
    rig.brain.nudge_text = "NUDGE"
    rig.llm.script(
        "a1",
        llm_resp(content="hmm"),  # run 1: plain text -> nudged
        llm_resp(content="still hmm"),  # run 1: plain text again -> the run ends, no second nudge
        llm_resp(content="again"),  # run 2: nudged again (once per run)
        wait_resp(),
    )
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
        assert len(rig.llm.calls) == 2
        assert rig.brain.nudge_calls == [("a1", "hmm", 0)]
        second = rig.llm.calls[1].messages
        assert roles(second) == ["system", "user", "assistant", "user"]
        assert second[2]["content"] == "hmm" and second[3]["content"] == "NUDGE"
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert [c[1] for c in rig.brain.nudge_calls] == ["hmm", "again"]
    assert len(rig.llm.calls) == 4


@async_test()
async def test_no_nudge_means_the_run_ends_at_the_first_plain_answer():
    rig = Rig()
    rig.brain.nudge_text = None
    rig.llm.script("a1", llm_resp(content="I will just think about it"), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
        await asyncio.sleep(0.05)
    assert len(rig.llm.calls) == 1 and len(rig.brain.nudge_calls) == 1
    assert rig.trace.of("content", "a1")[0]["text"] == "I will just think about it"  # inner monologue: log only
    assert rig.world.submitted == []


@async_test()
async def test_an_empty_answer_adds_no_blank_assistant_message_to_the_nudge():
    rig = Rig()
    rig.brain.nudge_text = "NUDGE"
    rig.llm.script("a1", llm_resp(content=""), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert roles(rig.llm.calls[1].messages) == ["system", "user", "user"]


@async_test()
async def test_the_nudge_and_a_pending_injection_arrive_in_one_user_message():
    rig = Rig()
    rig.brain.nudge_text = "NUDGE"
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(content="hmm")), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(urgent("request.opened", kind="request", request_id="r1"))
        await settle()
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
    last = rig.llm.calls[1].messages
    assert roles(last) == ["system", "user", "assistant", "user"]
    assert last[3]["content"] == "NUDGE\n\nINJECT reasons=[request.opened] ambient=0"


def cut_off(**kw):
    """A response that ended because the output-token limit was hit (all of it spent on thinking)."""
    resp = llm_resp(**kw)
    resp.finish_reason = "length"
    return resp


@async_test()
async def test_a_call_cut_off_by_the_token_limit_without_any_tool_call_is_retried_once_with_a_nudge():
    from app.runtime.agent import CUT_OFF_NUDGE

    rig = Rig()
    rig.brain.nudge_text = None  # the brain would not nudge an empty answer: the runtime must
    rig.llm.script("a1", cut_off(thought="thinking..."), llm_resp(SPEAK), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.llm.calls) == 3
    second = rig.llm.calls[1].messages
    assert roles(second) == ["system", "user", "user"] and second[2]["content"] == CUT_OFF_NUDGE
    assert len(rig.world.submitted) == 1  # the retried round produced the action
    assert any(r.get("why") == "length" for r in rig.trace.of("nudge", "a1"))


@async_test()
async def test_the_cut_off_recovery_happens_at_most_once_per_run():
    rig = Rig()
    rig.brain.nudge_text = None
    rig.llm.script("a1", cut_off(), cut_off(), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
        await asyncio.sleep(0.05)
    assert len(rig.llm.calls) == 2  # the second cut-off ends the run; no endless retrying
    assert len([r for r in rig.trace.of("nudge", "a1") if r.get("why") == "length"]) == 1


@async_test()
async def test_a_normal_stop_without_tools_is_not_mistaken_for_a_cut_off():
    rig = Rig()
    rig.brain.nudge_text = None
    rig.llm.script("a1", llm_resp(content="just thinking"), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
        await asyncio.sleep(0.05)
    assert len(rig.llm.calls) == 1 and not rig.trace.of("nudge", "a1")


# =============================================================================== failures


@async_test()
async def test_an_llm_failure_is_retried_with_backoff_and_the_wake_reasons_are_kept():
    rig = Rig()
    rig.llm.script("a1", RuntimeError("boom1"), TimeoutError("boom2"), wait_resp())
    agent = rig.agent(retry_backoff_s=0.03)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1, timeout=3)
        assert rig.sink.statuses("a1") == ["idle", "thinking", "idle", "thinking", "idle", "thinking", "acting", "idle"]
    assert len(rig.llm.calls) == 3 and agent.stats["failures"] == 2 and agent.stats["gave_up"] == 0
    assert [events(c.reasons) for c in rig.brain.starts] == [["floor.granted"]] * 3  # re-queued with the same reason
    errors = rig.trace.of("error", "a1")
    assert [e["attempt"] for e in errors] == [1, 2] and "boom1" in errors[0]["message"] and "TimeoutError" in errors[1]["message"]
    assert [r["delay_s"] for r in rig.trace.of("retry", "a1")] == [0.03, 0.06]  # exponential
    assert rig.sink.of_type("error") == []  # recovered: nobody is bothered


@async_test()
async def test_a_failure_that_persists_is_given_up_reported_and_the_actor_survives():
    rig = Rig()
    rig.llm.script("a1", RuntimeError("x1"), RuntimeError("x2"), RuntimeError("x3"), wait_resp())
    rig.world.names["a1"] = "Alice"
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["gave_up"] == 1, timeout=3)
        await asyncio.sleep(0.1)
        assert len(rig.llm.calls) == 3  # 1 attempt + 2 retries, then it stops
        assert agent.status == "idle" and agent.busy is False
        (error,) = rig.sink.of_type("error")
        assert "Alice" in error.data["message"] and "a1" in error.data["message"] and "x3" in error.data["message"]
        assert error.data["agent_id"] == "a1"
        # the Actor is still alive: the next wake-up works and the failure counter starts from zero
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.llm.calls) == 4


@async_test()
async def test_an_agent_that_fails_does_not_disturb_the_others():
    rig = Rig()
    rig.llm.script("a1", RuntimeError("down"), RuntimeError("down"), RuntimeError("down"))
    rig.llm.script("a2", llm_resp(SPEAK), wait_resp())
    broken, healthy = rig.agent("a1"), rig.agent("a2")
    async with running_agent(broken), running_agent(healthy):
        wake(broken)
        wake(healthy)
        await wait_until(lambda: healthy.stats["runs"] == 1 and broken.stats["gave_up"] == 1, timeout=3)
        assert healthy.stats["failures"] == 0 and healthy.status == "idle"
        assert [i.actor_id for i in rig.world.submitted] == ["a2"]
    assert len(rig.sink.of_type("error")) == 1


@async_test()
async def test_a_bug_in_the_brain_while_building_the_context_is_isolated_like_an_llm_failure():
    rig = Rig()
    rig.brain.start_error = RuntimeError("context bug")
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["gave_up"] == 1, timeout=3)
        assert rig.llm.calls == []  # never reached the model
        rig.brain.start_error = None
        rig.llm.script("a1", wait_resp())
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)


@async_test()
async def test_a_tool_that_raises_becomes_an_error_result_and_the_run_goes_on():
    rig = Rig()

    def broken(agent_id, args, *, gen_id, seen_seq):
        raise RuntimeError("tool bug")

    rig.brain.tool_handlers["speak"] = broken
    rig.llm.script("a1", llm_resp(SPEAK), wait_resp())
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
    rig.llm.script("a1", llm_resp(bad), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.brain.names() == ["wait"]  # ``speak`` was never executed
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
    rig.llm.script("a1", llm_resp(*calls), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    messages = rig.llm.calls[1].messages
    asked = [c["id"] for c in messages[2]["tool_calls"]]
    answered = [m["tool_call_id"] for m in messages[3:]]
    assert len(set(asked)) == 3 and all(asked) and asked == answered


@async_test()
async def test_a_rejected_commit_is_just_a_tool_result_and_counts_as_a_rejection():
    rig = Rig()
    rig.world.on_submit = lambda intent: Receipt.reject("NO_FLOOR", "not your turn")
    rig.llm.script("a1", llm_resp(SPEAK), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    result = json.loads(rig.llm.calls[1].messages[3]["content"])
    assert result["ok"] is False and result["code"] == "NO_FLOOR"  # the receipt is the tool result
    assert rig.usage.by_actor["a1"].rejections == 1


@async_test()
async def test_an_urgent_wake_during_the_retry_backoff_starts_at_once_and_merges_the_carried_reasons():
    rig = Rig()
    rig.llm.script("a1", RuntimeError("blip"), wait_resp())
    agent = rig.agent(retry_backoff_s=5.0)  # a long backoff that must not be waited out
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["failures"] == 1)
        assert agent.is_quiet() is False  # a retry is pending
        agent.deliver(urgent("request.opened", kind="request", request_id="r1"))
        await wait_until(lambda: agent.stats["runs"] == 1, timeout=2)
        assert agent.is_quiet() is True  # the retry timer was cancelled by the merge
    assert len(rig.llm.calls) == 2
    assert events(rig.brain.starts[1].reasons) == ["floor.granted", "request.opened"]


# =============================================================================== accounting & trace


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
            ("wait", {}),
            thought="let me think",
            content="my inner voice",
            prompt_tokens=300,
            completion_tokens=40,
            provider_id="prov",
            model_id="m1",
            input_price_per_m=1.0,
            output_price_per_m=2.0,
        ),
    )
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    actor = rig.usage.by_actor["a1"]
    assert (actor.calls, actor.tool_calls, actor.thinking_rounds, actor.rejections) == (1, 2, 1, 1)
    assert (actor.prompt_tokens, actor.completion_tokens) == (300, 40)
    assert actor.cost_usd == pytest.approx(300 / 1e6 * 1.0 + 40 / 1e6 * 2.0)
    assert "prov::m1" in rig.usage.by_model

    names = rig.trace.names("a1")
    for expected in ("wake", "llm_call", "thought", "content", "tool_call", "receipt", "run_end"):
        assert expected in names, expected
    call = rig.trace.of("llm_call", "a1")[0]
    assert call["model"] == "m1" and call["prompt_tokens"] == 300 and call["tool_calls"] == ["speak", "wait"]
    assert rig.trace.of("thought", "a1")[0]["text"] == "let me think"
    tool_calls = rig.trace.of("tool_call", "a1")
    assert [t["name"] for t in tool_calls] == ["speak", "wait"] and tool_calls[0]["args"]["content"] == "hello"
    receipt = rig.trace.of("receipt", "a1")[0]
    assert receipt["ok"] is False and receipt["code"] == "NO_FLOOR"
    assert "messages" not in call  # full prompts are only traced on request


@async_test()
async def test_the_conversation_can_be_traced_on_request():
    rig = Rig()
    rig.llm.script("a1", wait_resp())
    agent = rig.agent(trace_messages=True)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert roles(rig.trace.of("llm_call", "a1")[0]["messages"]) == ["system", "user"]


@async_test()
async def test_max_tokens_comes_from_the_scenario_unless_overridden():
    rig = Rig()
    rig.world.spec.defaults.llm.max_tokens = 777
    rig.llm.script("a1", wait_resp(), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
        agent.config.max_tokens = 55
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert [c.max_tokens for c in rig.llm.calls] == [777, 55]


# =============================================================================== lifecycle


@async_test()
async def test_status_events_follow_the_run():
    rig = Rig()
    rig.llm.script("a1", wait_resp())
    agent = rig.agent()
    assert agent.status == "stopped"
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
        assert agent.status == "idle"
    assert rig.sink.statuses("a1") == ["idle", "thinking", "acting", "idle", "stopped"]
    assert all(e.data["id"] == "a1" for e in rig.sink.of_type("agent.status"))  # {id, state} payload


@async_test()
async def test_stop_cancels_a_running_worker_clears_everything_and_is_idempotent():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=wait_resp()))
    agent = rig.agent()
    agent.activate()
    actor = asyncio.get_running_loop().create_task(agent.run(), name="actor:a1")
    wake(agent)
    agent.deliver(ambient("message.new", label="x"))
    await wait_until(lambda: len(rig.llm.calls) == 1)
    agent.deliver(normal("message.new", label="y"))
    await settle()
    await agent.stop()
    assert rig.llm.cancelled == ["a1"]
    assert agent.status == "stopped" and agent.busy is False and agent.is_quiet()
    assert agent.pending == [] and len(agent.mailbox) == 0
    await agent.stop()  # idempotent
    await asyncio.wait_for(actor, 2)  # the actor loop returns by itself
    await asyncio.sleep(0.15)  # a debounce timer that survived stop() would start a run now
    assert len(rig.llm.calls) == 1


@async_test()
async def test_an_agent_can_be_restarted_after_it_was_stopped():
    rig = Rig()
    rig.llm.script("a1", wait_resp(), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    async with running_agent(agent):  # activate() + a new run() task
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert rig.sink.statuses("a1") == ["idle", "thinking", "acting", "idle", "stopped", "idle", "thinking", "acting", "idle", "stopped"]


@async_test()
async def test_a_stop_requested_before_the_actor_task_ever_ran_is_not_forgotten():
    rig = Rig()
    agent = rig.agent()
    agent.activate()
    actor = asyncio.get_running_loop().create_task(agent.run(), name="actor:a1")
    await agent.stop()  # the task has not had its first turn yet
    await asyncio.wait_for(actor, 2)  # it must exit instead of waiting forever
    assert agent.status == "stopped"


@async_test()
async def test_only_one_actor_loop_may_run_per_agent():
    rig = Rig()
    agent = rig.agent()
    async with running_agent(agent):
        with pytest.raises(RuntimeError):
            await agent.run()


@async_test()
async def test_is_quiet_reflects_pending_work():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=wait_resp()))
    agent = rig.agent(debounce_s=0.15)
    async with running_agent(agent):
        assert agent.is_quiet()
        agent.deliver(ambient("message.new"))
        await settle()
        assert agent.is_quiet()  # ambient alone never wakes anybody
        agent.deliver(normal("message.new", label="n"))
        await settle()
        assert not agent.is_quiet()  # a debounce timer is pending
        await wait_until(lambda: len(rig.llm.calls) == 1)
        assert not agent.is_quiet()  # a worker is running
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
        assert agent.is_quiet()


@async_test()
async def test_the_actor_survives_a_bug_while_scheduling():
    rig = Rig()
    rig.llm.script("a1", wait_resp())
    agent = rig.agent()
    original = agent._schedule
    calls = {"n": 0}

    def flaky() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("scheduler bug")
        original()

    agent._schedule = flaky  # type: ignore[method-assign]
    async with running_agent(agent):
        agent.deliver(ambient("message.new"))  # first batch: _schedule raises, the loop must carry on
        await settle()
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert any("scheduler bug" in str(e) or "actor failure" in e["message"] for e in rig.trace.of("error", "a1"))
