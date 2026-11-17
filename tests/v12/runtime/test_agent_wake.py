"""Activation (v0.12.1): every delivered envelope wakes the agent at once; what is delivered together, or while
a model call is in flight, is merged; interrupts cancel only the work they invalidate; anti-starvation; gen ids.

There is no debounce, no ambient level and no buffer that waits: a sleeping agent is activated on the very next
loop iteration, a busy one is told at the next round boundary."""

from __future__ import annotations

import asyncio

import pytest

from .fakes import (
    Rig,
    Step,
    async_test,
    env,
    llm_resp,
    normal,
    revoked,
    running_agent,
    settle,
    sleep_resp,
    speak_resp,
    urgent,
    wait_until,
)


def events(envelopes):
    return [e.payload["event"] for e in envelopes]


def labels(envelopes):
    return [e.payload.get("label") for e in envelopes]


def roles(messages):
    return [m["role"] for m in messages]


def worker_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("worker:") and not t.done()]


# ======================================================================== immediate activation


@async_test()
async def test_a_message_wakes_a_sleeping_agent_at_once_there_is_no_debounce():
    rig = Rig()
    rig.llm.script("a1", sleep_resp())
    agent = rig.agent()
    loop = asyncio.get_running_loop()
    async with running_agent(agent):
        started = loop.time()
        agent.deliver(normal("message.new", channel_id="pub", label="m1"))
        await wait_until(lambda: rig.llm.calls)
        assert rig.llm.calls[0].started - started < 0.05  # the next loop iteration, nothing waits
        await wait_until(lambda: agent.stats["runs"] == 1)
    ctx = rig.brain.starts[0]
    assert ctx.kind == "start" and ctx.after_seq == 0 and ctx.agent_id == "a1" and labels(ctx.reasons) == ["m1"]
    assert rig.brain.begins == ["a1"] and agent.stats["activations"] == 1
    assert rig.usage.by_actor["a1"].activations == 1
    wake = rig.trace.of("wake", "a1")[0]
    assert wake["gen"] == ctx.gen_id and wake["reasons"][0]["event"] == "message.new" and wake["retry"] is False


@pytest.mark.parametrize("priority", ["normal", "urgent"])
@pytest.mark.parametrize("kind", ["message", "floor", "request", "challenge", "ability", "notice"])
@async_test()
async def test_every_kind_and_priority_wakes_alike(kind, priority):
    rig = Rig()
    rig.llm.script("a1", sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(env("something.happened", kind=kind, priority=priority, channel_id="pub"))
        await wait_until(lambda: agent.stats["runs"] == 1, timeout=1)
    assert len(rig.llm.calls) == 1 and events(rig.brain.starts[0].reasons) == ["something.happened"]


@async_test()
async def test_everything_delivered_in_one_synchronous_pump_is_one_activation():
    rig = Rig()
    rig.llm.script("a1", sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        for n in range(5):  # no await in between: the supervisor's pump delivers like this
            agent.deliver(normal("message.new", channel_id="pub", label=f"n{n}"))
        assert rig.llm.calls == [] and len(agent.pending) == 5  # nothing started yet, nothing lost
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.brain.starts) == 1 and len(rig.llm.calls) == 1 and agent.stats["activations"] == 1
    assert labels(rig.brain.starts[0].reasons) == ["n0", "n1", "n2", "n3", "n4"]  # arrival order, none sorted away
    assert agent.pending == []


@async_test()
async def test_priority_does_not_reorder_the_reasons():
    rig = Rig()
    rig.llm.script("a1", sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(normal("message.new", label="a"))
        agent.deliver(urgent("floor.granted", label="b"))
        agent.deliver(normal("message.new", label="c"))
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert labels(rig.brain.starts[0].reasons) == ["a", "b", "c"]  # as committed, like the ledger


@async_test()
async def test_a_later_message_wakes_the_sleeping_agent_again_with_a_delta_not_a_new_conversation():
    rig = Rig()
    rig.llm.script("a1", sleep_resp(), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(normal("message.new", label="m1"))
        await wait_until(lambda: agent.stats["runs"] == 1)
        assert agent.status == "sleeping" and agent.is_quiet()
        agent.deliver(normal("message.new", label="m2"))
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert len(rig.brain.starts) == 1  # the conversation is created once ...
    (delta,) = rig.brain.injects  # ... and every later activation is a DELTA
    assert delta.kind == "inject" and labels(delta.reasons) == ["m2"] and delta.gen_id > rig.brain.starts[0].gen_id
    assert agent.stats["activations"] == 2 and rig.usage.by_actor["a1"].activations == 2
    assert rig.brain.begins == ["a1", "a1"]  # begin_run opens every run, continuing or not
    second = rig.llm.calls[-1].messages
    assert roles(second)[-2:] == ["tool", "user"] and second[-1]["content"].startswith("DELTA[a1]")


@async_test()
async def test_the_agent_is_not_woken_by_what_it_did_itself():
    # keeping the agent's own entries away from it is the world's job (it never delivers them back); the runtime
    # must not invent a wake-up out of a tool call either
    rig = Rig()
    rig.llm.script("a1", speak_resp("hi"), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(normal("message.new", label="m1"))
        await wait_until(lambda: agent.stats["runs"] == 1)
        await asyncio.sleep(0.1)
        assert agent.is_quiet() and agent.pending == [] and agent.status == "sleeping"
    assert len(rig.llm.calls) == 2 and agent.stats["activations"] == 1
    assert [i.type for i in rig.world.submitted] == ["speak"]


# ================================================================================= merging


@async_test()
async def test_messages_arriving_during_a_call_become_one_injection_in_arrival_order():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=speak_resp()), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(normal("message.new", channel_id="pub", label="first"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        for n in range(1, 5):  # four messages, each in a loop turn of its own
            agent.deliver(normal("message.new", channel_id="pub", label=f"m{n}"))
            await settle(3)
        assert len(rig.llm.calls) == 1 and rig.brain.injects == [] and len(agent.pending) == 4  # told at the boundary
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.brain.starts) == 1 and agent.stats["activations"] == 1  # one run, one activation
    (merge,) = rig.brain.injects
    assert merge.kind == "inject" and labels(merge.reasons) == ["m1", "m2", "m3", "m4"]  # N messages -> 1 injection
    assert merge.gen_id == rig.brain.starts[0].gen_id and agent.stats["injections"] == 1
    second = rig.llm.calls[1].messages
    assert roles(second) == ["system", "user", "assistant", "tool", "user"]
    assert second[-1]["content"].count("DELTA[") == 1
    assert second[-1]["content"].endswith("message.new@pub#m1,message.new@pub#m2,message.new@pub#m3,message.new@pub#m4]")
    assert rig.trace.of("inject", "a1")[-1]["kind"] == "merge"


@async_test()
async def test_when_nothing_arrives_between_rounds_the_conversation_gets_no_extra_message():
    rig = Rig()
    rig.llm.script("a1", speak_resp(), speak_resp(), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(normal("message.new", label="m1"))
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.brain.injects == []  # three rounds, nothing pending: the brain is not even asked for a delta
    assert [roles(c.messages) for c in rig.llm.calls][-1] == ["system", "user", "assistant", "tool", "assistant", "tool"]


@async_test()
async def test_a_message_arriving_as_the_agent_goes_to_sleep_wakes_it_again_at_once():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=sleep_resp()), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(normal("message.new", label="m0"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(normal("message.new", label="m1"))  # while the model decides to sleep
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert agent.stats["activations"] == 2 and labels(rig.brain.injects[0].reasons) == ["m1"]
    second = rig.llm.calls[1].messages
    assert roles(second) == ["system", "user", "assistant", "tool", "user"]  # [.., sleep call, its result, the delta]
    assert rig.sink.statuses("a1").count("sleeping") == 2  # at the start and at the very end: no flicker in between
    assert rig.sink.statuses("a1")[-1] == "sleeping"


@async_test()
async def test_a_plain_answer_with_something_pending_continues_the_same_run():
    rig = Rig()
    rig.brain.nudge_text = None
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(content="thinking out loud")), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(normal("message.new", label="m0"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(normal("message.new", label="m1"))
        await settle()
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.llm.calls) == 2 and len(rig.brain.starts) == 1 and agent.stats["activations"] == 1
    second = rig.llm.calls[1].messages
    assert roles(second) == ["system", "user", "assistant", "user"] and second[2]["content"] == "thinking out loud"
    assert second[3]["content"].startswith("DELTA[a1]") and "#m1" in second[3]["content"]


@async_test()
async def test_a_plain_answer_with_nothing_pending_ends_the_run():
    rig = Rig()
    rig.brain.nudge_text = None
    rig.llm.script("a1", llm_resp(content="nothing to do"))
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(normal("message.new", label="m0"))
        await wait_until(lambda: agent.stats["runs"] == 1)
        await asyncio.sleep(0.05)
    assert len(rig.llm.calls) == 1 and rig.trace.of("run_end", "a1")[0]["reason"] == "no_tool"
    assert rig.conversation().sleep_until is None  # the same as sleeping until something arrives


# ==================================================================================== interrupts


@async_test()
async def test_an_interrupt_for_the_focus_cancels_the_call_and_wakes_the_agent_again_with_it():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=speak_resp()), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(revoked("pub"))
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.llm.cancelled == ["a1"] and agent.stats["cancels"] == 1 and agent.stats["activations"] == 2
    assert rig.brain.names() == ["sleep"]  # the cancelled call's ``speak`` was never executed
    assert rig.world.submitted == []
    (delta,) = rig.brain.injects
    assert events(delta.reasons) == ["floor.revoked"]  # the revocation is what the agent is woken with
    assert delta.gen_id > rig.brain.starts[0].gen_id
    cancel = rig.trace.of("cancel", "a1")[0]
    assert cancel["envelope"]["channel_id"] == "pub" and cancel["envelope"]["event"] == "floor.revoked"
    # the cancelled call left no trace in the conversation: START, the new delta, then the answer that counted
    assert roles(rig.conversation().messages) == ["user", "user", "assistant", "tool"]
    assert rig.llm.calls[1].messages[2]["content"].startswith("DELTA[a1]")


@async_test()
async def test_a_run_cancelled_before_it_told_the_agent_anything_hands_its_reasons_to_the_next_run():
    # found by the fuzz test: the activation had taken the envelopes, the interrupt came before the worker's first
    # step - without this the agent would never have heard about them
    rig = Rig()
    rig.llm.script("a1", sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(normal("message.new", channel_id="pub", label="m1"))
        await asyncio.sleep(0)  # the activation ran (a worker exists), the worker has not had its first step
        assert agent.busy and rig.brain.begins == [] and rig.llm.calls == []
        agent.deliver(revoked("pub"))
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert agent.stats["cancels"] == 1 and rig.llm.cancelled == []  # it never even reached a model call
    (opening,) = rig.brain.starts
    assert events(opening.reasons) == ["message.new", "floor.revoked"] and labels(opening.reasons)[0] == "m1"


@async_test()
async def test_an_interrupt_for_another_channel_is_just_merged():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=speak_resp()), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(revoked("elsewhere"))
        await settle()
        assert rig.llm.cancelled == [] and agent.busy
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.brain.names() == ["speak", "sleep"] and agent.stats["activations"] == 1  # the run carried on and acted
    assert events(rig.brain.injects[0].reasons) == ["floor.revoked"]  # but was told at the boundary


@async_test()
async def test_an_interrupt_matches_the_request_id_of_the_focus():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(("respond", {"request_id": "r1", "value": "yes"}))), sleep_resp())
    agent = rig.agent()

    def cancelled(request_id: str):
        return env("request.cancelled", kind="request", interrupt=True, request_id=request_id)

    async with running_agent(agent):
        agent.deliver(urgent("request.opened", kind="request", channel_id="dm-a1", request_id="r1"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(cancelled("r2"))  # somebody else's request: irrelevant
        await settle()
        assert rig.llm.cancelled == []
        agent.deliver(cancelled("r1"))  # the one this run is answering
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.llm.cancelled == ["a1"] and "respond" not in rig.brain.names()
    assert events(rig.brain.injects[0].reasons) == ["request.cancelled", "request.cancelled"]  # r2 and r1, as they came


@async_test()
async def test_an_interrupt_without_channel_or_request_matches_nothing():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=sleep_resp()))
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(env("floor.revoked", kind="floor", interrupt=True))
        await settle()
        assert rig.llm.cancelled == []
        hold.set()
        await wait_until(lambda: agent.stats["runs"] >= 1)


@async_test()
async def test_the_focus_grows_with_what_a_run_is_told():
    rig = Rig()
    first, second = asyncio.Event(), asyncio.Event()
    rig.llm.script("a1", Step(hold=first, resp=speak_resp()), Step(hold=second, resp=sleep_resp()), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="A"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(urgent("request.opened", kind="request", channel_id="B", request_id="rb"))
        agent.deliver(revoked("B"))  # B is not part of the focus yet: merged, no cancellation
        await settle()
        assert rig.llm.cancelled == []
        first.set()  # round boundary: both are told, and B joins the focus
        await wait_until(lambda: len(rig.llm.calls) == 2)
        assert rig.llm.cancelled == [] and events(rig.brain.injects[0].reasons) == ["request.opened", "floor.revoked"]
        agent.deliver(revoked("B"))  # now it invalidates the work in flight
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.llm.cancelled == ["a1"] and rig.brain.names() == ["speak", "sleep"]


@async_test()
async def test_two_interrupts_in_one_pump_cancel_once_and_the_restart_carries_both():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=speak_resp()), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(revoked("pub"))
        agent.deliver(normal("message.new", channel_id="pub", label="after"))
        agent.deliver(revoked("pub", event="floor.frozen"))
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert agent.stats["cancels"] == 1 and rig.llm.cancelled == ["a1"]
    assert events(rig.brain.injects[0].reasons) == ["floor.revoked", "message.new", "floor.frozen"]  # in order, all of them


@async_test()
async def test_the_new_run_starts_only_after_the_cancelled_one_has_unwound():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=speak_resp(), swallow_cancel_s=0.08), sleep_resp())
    agent = rig.agent(cancel_grace_s=1.0)
    loop = asyncio.get_running_loop()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        cancelled_at = loop.time()
        agent.deliver(revoked("pub"))
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.llm.peak == 1  # never two model calls of one agent at the same time
    assert rig.llm.calls[1].started - cancelled_at >= 0.07  # it waited for the old call to give up
    assert rig.brain.names() == ["sleep"] and rig.world.submitted == []  # and the late answer of the old one was dropped
    assert rig.llm.completed.count("a1") == 2  # (the stubborn call did answer - nobody used it)


@async_test()
async def test_a_stubborn_worker_is_abandoned_after_the_grace_period_and_its_late_answer_never_counts():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=speak_resp("LATE"), swallow_cancel_s=0.4), sleep_resp())
    agent = rig.agent(cancel_grace_s=0.05)
    loop = asyncio.get_running_loop()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        cancelled_at = loop.time()
        agent.deliver(revoked("pub"))
        await wait_until(lambda: agent.stats["runs"] == 1)
        assert loop.time() - cancelled_at < 0.35  # the agent did not wait for the stubborn call to finish
        await wait_until(lambda: rig.llm.completed.count("a1") == 2, timeout=3)  # ... which answers eventually
        await asyncio.sleep(0.1)
    assert rig.brain.names() == ["sleep"] and rig.world.submitted == []  # its tool call was discarded
    assert not any("LATE" in str(m) for m in rig.conversation().messages)  # and never written to the conversation
    assert rig.trace.of("discard", "a1")
    assert any("abandoned" in e["message"] for e in rig.trace.of("error", "a1"))
    assert agent.stats["failures"] == 0


@async_test()
async def test_the_third_consecutive_interrupt_is_merged_instead_and_a_normal_end_starts_the_count_over():
    rig = Rig()
    h1, h2, h3, h4 = (asyncio.Event() for _ in range(4))
    rig.llm.script(
        "a1",
        Step(hold=h1, resp=sleep_resp()),  # run 1: cancelled
        Step(hold=h2, resp=sleep_resp()),  # run 2: cancelled
        Step(hold=h3, resp=speak_resp()),  # run 3: survives the third interrupt
        sleep_resp(),  # run 3, round 2: ends normally
        Step(hold=h4, resp=sleep_resp()),  # run 4: cancelled again (the count was reset)
        sleep_resp(),
    )
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)

        agent.deliver(revoked("pub"))
        await wait_until(lambda: len(rig.llm.calls) == 2)
        agent.deliver(revoked("pub"))
        await wait_until(lambda: len(rig.llm.calls) == 3)
        assert rig.llm.cancelled == ["a1", "a1"] and agent.stats["cancels"] == 2

        agent.deliver(revoked("pub"))  # the third one: merged, not cancelled
        await settle(8)
        assert rig.llm.cancelled == ["a1", "a1"] and len(rig.llm.calls) == 3 and agent.busy
        assert rig.trace.of("inject_instead_of_cancel", "a1")
        h3.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
        assert len(rig.llm.calls) == 4  # run 3 got a second round: the revocation was merged into it
        assert events(rig.brain.injects[-1].reasons) == ["floor.revoked"]

        # a run ended normally: interrupts cancel again
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 5)
        agent.deliver(revoked("pub"))
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert agent.stats["cancels"] == 3 and rig.llm.cancelled == ["a1", "a1", "a1"]


@async_test()
async def test_an_activation_that_made_no_model_call_does_not_reset_the_starvation_count():
    rig = Rig()
    h1, h2, h4 = (asyncio.Event() for _ in range(3))
    rig.llm.script(
        "a1",
        Step(hold=h1, resp=sleep_resp()),  # run 1: cancelled
        Step(hold=h2, resp=sleep_resp()),  # run 2: cancelled
        Step(hold=h4, resp=sleep_resp()),  # run 4: a real run (run 3 will not call the model at all)
        sleep_resp(),
    )
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(revoked("pub"))  # cancellation 1
        await wait_until(lambda: len(rig.llm.calls) == 2)
        rig.brain.inject_hook = lambda ctx: None  # the activation after the next cancellation has "nothing to tell"
        agent.deliver(revoked("pub"))  # cancellation 2
        await wait_until(lambda: agent.stats["runs"] == 1)  # run 3 was a no-op activation: no model call
        assert agent.stats["cancels"] == 2 and len(rig.llm.calls) == 2
        assert rig.trace.of("run_end", "a1")[-1]["reason"] == "nothing"
        rig.brain.inject_hook = None

        agent.deliver(normal("message.new", channel_id="pub", label="x"))  # run 4: a real run
        await wait_until(lambda: len(rig.llm.calls) == 3)
        agent.deliver(revoked("pub"))  # the no-op run was no progress: the guard still holds, this one is merged
        await settle(8)
        assert agent.stats["cancels"] == 2 and rig.llm.cancelled == ["a1", "a1"]
        assert rig.trace.of("inject_instead_of_cancel", "a1")
        h4.set()
        await wait_until(lambda: agent.stats["runs"] >= 3)
    assert events(rig.brain.injects[-1].reasons) == ["floor.revoked"]  # told after run 4, with the next activation


@async_test()
async def test_cancelled_workers_never_leak_tasks():
    rig = Rig()
    h1, h2 = asyncio.Event(), asyncio.Event()
    rig.llm.script("a1", Step(hold=h1, resp=sleep_resp()), Step(hold=h2, resp=sleep_resp()), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        assert len(worker_tasks()) == 1
        for _ in range(2):
            agent.deliver(revoked("pub"))
            await settle(8)
            assert len(worker_tasks()) <= 1  # the cancelled worker is gone, never two at once
        await wait_until(lambda: agent.stats["runs"] == 1)
        await settle()
        assert worker_tasks() == [] and agent.stats["cancels"] == 2
    assert worker_tasks() == []


# ===================================================================================== gen ids


@async_test()
async def test_every_run_gets_a_new_gen_id_that_reaches_the_tools_the_contexts_and_the_intents():
    rig = Rig()
    rig.llm.script("a1", llm_resp(("speak", {"channel_id": "pub", "content": "1"}), ("sleep", {})), speak_resp("2"), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(normal("message.new", label="m1"))
        await wait_until(lambda: agent.stats["runs"] == 1)
        first_gen = agent.gen_id
        agent.deliver(normal("message.new", label="m2"))
        await wait_until(lambda: agent.stats["runs"] == 2)
        second_gen = agent.gen_id
    assert rig.brain.starts[0].gen_id == first_gen and rig.brain.injects[0].gen_id == second_gen > first_gen
    assert [t["gen_id"] for t in rig.brain.executed] == [first_gen, first_gen, second_gen, second_gen]
    assert [i.gen_id for i in rig.world.submitted] == [first_gen, second_gen]  # the intents carry it too


@async_test()
async def test_a_cancelled_run_is_stale_for_good_so_nothing_of_it_reaches_the_world():
    rig = Rig()
    hold = asyncio.Event()
    # the stubborn model answers (with a tool call) after the cancellation, while the NEW run is still thinking
    new_run = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=speak_resp("OLD"), swallow_cancel_s=0.1), Step(hold=new_run, resp=sleep_resp()))
    agent = rig.agent(cancel_grace_s=0.03)
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(revoked("pub"))
        await wait_until(lambda: len(rig.llm.calls) == 2)  # the new run is in flight ...
        await wait_until(lambda: rig.llm.completed.count("a1") == 1, timeout=2)  # ... when the old answer arrives
        await settle(10)
        assert rig.world.submitted == [] and rig.brain.executed == []
        new_run.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.brain.names() == ["sleep"] and rig.world.submitted == []


# =========================================================================== stopped / not armed


@async_test()
async def test_envelopes_are_ignored_until_the_agent_is_armed_and_after_it_stopped():
    rig = Rig()
    rig.llm.script("a1", sleep_resp())
    agent = rig.agent()
    agent.deliver(normal("message.new", label="too early"))  # never activated
    await settle(5)
    assert rig.llm.calls == [] and agent.pending == []
    agent.activate()
    agent.deliver(normal("message.new", label="m1"))
    await wait_until(lambda: agent.stats["runs"] == 1)
    await agent.stop()
    agent.deliver(normal("message.new", label="too late"))
    await asyncio.sleep(0.05)
    assert len(rig.llm.calls) == 1 and agent.pending == [] and agent.is_quiet()
