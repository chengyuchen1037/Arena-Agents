"""The soft-pause gate and the agents / supervisor behind it (v0.13.1, G2-G5, G8, G13; contract section 12.2).

A closed gate cancels nothing: a model call in flight finishes, its answer is written and its tools run; the agent then
waits in front of its next call (state ``held``).  New activations, retries and timer wake-ups stop in front of their first
call.  What arrives meanwhile is merged into one message when the gate opens.  The wait can be cancelled by ``stop()`` and
by an interrupt.  The watchdog stays quiet while the gate is closed and while a person holds a floor without a deadline.
"""

from __future__ import annotations

import asyncio
import random
from collections import defaultdict

import pytest

from app.runtime.call_log import CallLog
from app.runtime.clock import ManualClock
from app.runtime.llm_gate import GatedEngineLlm, LlmGate, LoggedLlm
from app.runtime.soft_gate import GateClosed, SoftGate
from app.runtime.supervisor import Supervisor, SupervisorConfig

from .test_agent_fuzz import RandomLlm, check_agent, random_envelope
from .fakes import (
    MODERATOR,
    FakeWorld,
    Rig,
    Step,
    async_test,
    llm_resp,
    normal,
    revoked,
    running_agent,
    settle,
    sleep_resp,
    urgent,
    wait_until,
)

SPEAK = ("speak", {"channel_id": "pub", "content": "hello"})


def wake(agent, label: str = "m", **kw) -> None:
    kw.setdefault("channel_id", "pub")
    agent.deliver(normal("message.new", label=label, **kw))


def labels(envelopes):
    return [e.payload.get("label") for e in envelopes]


def roles(messages):
    return [m["role"] for m in messages]


# ================================================================================== the gate itself


def test_the_gate_is_open_at_first_and_closing_and_opening_are_idempotent() -> None:
    gate = SoftGate()
    assert gate.closed is False and gate.state is None and gate.payload() is None
    gate.open()  # nothing to open
    gate.close()
    gate.close()
    assert gate.closed is True and gate.state == "held" and gate.payload() == {"state": "held", "in_flight": 0}
    gate.open()
    gate.open()
    assert gate.closed is False and gate.payload() is None


def test_the_phase_follows_the_calls_in_flight() -> None:
    gate = SoftGate()
    gate.call_started()
    gate.call_started()
    assert gate.in_flight == 2 and gate.state is None  # open: nothing is draining
    gate.close()
    assert gate.payload() == {"state": "draining", "in_flight": 2}
    gate.call_finished()
    assert gate.payload() == {"state": "draining", "in_flight": 1}
    gate.call_finished()
    gate.call_finished()  # (never below zero)
    assert gate.payload() == {"state": "held", "in_flight": 0}


@async_test()
async def test_changes_are_reported_one_loop_turn_later_once_and_never_for_the_closing_and_the_opening() -> None:
    heard: list = []
    gate = SoftGate(on_change=lambda g: heard.append(g.payload()))
    gate.call_started()
    gate.call_started()
    gate.close()
    await settle()
    assert heard == []  # the caller announces the closing itself
    gate.call_finished()
    assert heard == []  # not synchronously ...
    gate.call_finished()  # ... and two answers in the same turn are one report, with the final view
    await settle()
    assert heard == [{"state": "held", "in_flight": 0}]
    gate.call_started()  # (an agent that was let through while closed cannot happen; the count still follows)
    gate.call_finished()
    await settle()
    assert heard == [{"state": "held", "in_flight": 0}]  # the same view again is not a change
    gate.open()
    gate.call_started()
    gate.call_finished()
    await settle()
    assert heard == [{"state": "held", "in_flight": 0}]  # open: nothing to report


@async_test()
async def test_the_phases_of_two_answers_are_reported_in_order() -> None:
    heard: list = []
    gate = SoftGate(on_change=lambda g: heard.append(g.payload()))
    gate.call_started()
    gate.call_started()
    gate.close()
    gate.call_finished()
    await settle()
    gate.call_finished()
    await settle()
    assert heard == [{"state": "draining", "in_flight": 1}, {"state": "held", "in_flight": 0}]


@async_test()
async def test_a_failing_listener_does_not_break_the_gate_and_dispose_stops_the_reports() -> None:
    calls: list[int] = []

    def broken(gate: SoftGate) -> None:
        calls.append(gate.in_flight)
        raise RuntimeError("listener bug")

    gate = SoftGate(on_change=broken)
    gate.call_started()
    gate.call_started()
    gate.close()
    gate.call_finished()
    await settle()
    assert calls == [1]
    gate.dispose()
    gate.call_finished()
    await settle()
    assert calls == [1] and gate.closed is False  # disposed: open, and quiet


@async_test()
async def test_waiting_at_the_gate_ends_when_it_opens_and_can_be_cancelled() -> None:
    gate = SoftGate()
    assert await asyncio.wait_for(gate.wait_open(), 1) is None  # open: returns at once
    gate.close()
    waiter = asyncio.ensure_future(gate.wait_open())
    await settle()
    assert not waiter.done() and gate.held == 1
    gate.open()
    await asyncio.wait_for(waiter, 1)
    assert gate.held == 0

    gate.close()
    cancelled = asyncio.ensure_future(gate.wait_open())
    await settle()
    assert gate.held == 1
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert gate.held == 0
    gate.dispose()


@async_test()
async def test_a_waiter_that_wakes_into_a_gate_that_closed_again_keeps_waiting() -> None:
    gate = SoftGate()
    gate.close()
    waiter = asyncio.ensure_future(gate.wait_open())
    await settle()
    gate.open()
    gate.close()  # opened and closed again before the waiter ran
    await settle()
    assert not waiter.done() and gate.held == 1
    gate.open()
    await asyncio.wait_for(waiter, 1)


# ================================================================================== an agent at the gate


@async_test()
async def test_an_open_gate_changes_nothing_for_an_agent() -> None:
    rig = Rig()
    gate = SoftGate()
    rig.llm.script("a1", llm_resp(SPEAK), sleep_resp())
    agent = rig.agent(soft_gate=gate)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.sink.statuses("a1") == ["sleeping", "thinking", "acting", "thinking", "acting", "sleeping"]
    assert gate.in_flight == 0 and gate.held == 0 and "held" not in rig.trace.names("a1")


@async_test()
async def test_a_call_in_flight_finishes_its_tools_run_and_the_agent_then_waits_in_front_of_the_next_call() -> None:
    rig = Rig()
    gate = SoftGate()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(SPEAK)), sleep_resp())
    agent = rig.agent(soft_gate=gate)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: len(rig.llm.calls) == 1)
        assert gate.in_flight == 1
        gate.close()
        assert gate.payload() == {"state": "draining", "in_flight": 1}
        hold.set()  # the answer comes in after the click
        await wait_until(lambda: agent.status == "held")
        assert gate.payload() == {"state": "held", "in_flight": 0} and gate.held == 1
        # the answer was written, its tools were carried out (the speech is in the world) ...
        assert [i.type for i in rig.world.submitted] == ["speak"]
        assert roles(rig.conversation().messages) == ["user", "assistant", "tool"]
        assert rig.llm.calls[0].paired
        # ... and nothing else is called while the gate is closed
        await asyncio.sleep(0.15)
        assert len(rig.llm.calls) == 1 and agent.status == "held"
        gate.open()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.llm.calls) == 2 and rig.llm.calls[1].paired
    assert rig.sink.statuses("a1") == ["sleeping", "thinking", "acting", "held", "thinking", "acting", "sleeping"]
    assert rig.trace.names("a1").count("held") == 1 and rig.trace.names("a1").count("released") == 1


@async_test()
async def test_an_agent_that_is_idle_when_the_gate_closes_stays_idle_and_a_new_activation_is_held_before_its_first_call() -> None:
    rig = Rig()
    gate = SoftGate()
    gate.close()
    assert gate.state == "held"  # nothing in flight: held at once
    rig.llm.script("a1", sleep_resp())
    agent = rig.agent(soft_gate=gate)
    async with running_agent(agent):
        await settle()
        assert agent.status == "sleeping"  # "not activated" stays sleeping: only activated agents are held
        wake(agent)
        await wait_until(lambda: agent.status == "held")
        await settle()
        assert rig.llm.calls == [] and agent.busy and not agent.is_quiet()
        assert roles(rig.conversation().messages) == ["user"]  # the opening is in the conversation, the model was not asked
        gate.open()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.llm.calls) == 1


@async_test()
async def test_what_arrives_while_the_agent_is_held_is_merged_into_one_message_and_one_call() -> None:
    rig = Rig()
    gate = SoftGate()
    gate.close()
    rig.llm.script("a1", sleep_resp())
    agent = rig.agent(soft_gate=gate)
    async with running_agent(agent):
        wake(agent, "first")
        await wait_until(lambda: agent.status == "held")
        wake(agent, "second")
        wake(agent, "third")
        assert labels(agent.pending) == ["second", "third"]  # waiting, not lost, not delivered one by one
        assert len(rig.llm.calls) == 0 and rig.brain.injects_of("a1") == []
        gate.open()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.llm.calls) == 1  # one call for everything that came in
    request = rig.llm.calls[0].messages
    assert roles(request) == ["system", "user", "user"]
    assert "first" in request[1]["content"] and "second" in request[2]["content"] and "third" in request[2]["content"]
    assert [labels(c.reasons) for c in rig.brain.injects_of("a1")] == [["second", "third"]] and agent.pending == []
    assert rig.sink.statuses("a1") == ["sleeping", "held", "thinking", "acting", "sleeping"]


@async_test()
async def test_what_arrives_during_the_pause_is_merged_after_a_round_of_tools_too() -> None:
    rig = Rig()
    gate = SoftGate()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(SPEAK)), sleep_resp())
    agent = rig.agent(soft_gate=gate)
    async with running_agent(agent):
        wake(agent, "first")
        await wait_until(lambda: len(rig.llm.calls) == 1)
        gate.close()
        hold.set()
        await wait_until(lambda: agent.status == "held")
        wake(agent, "typed-meanwhile")  # a person's words, delivered while everybody is held
        gate.open()
        await wait_until(lambda: agent.stats["runs"] == 1)
    second = rig.llm.calls[1].messages
    assert roles(second) == ["system", "user", "assistant", "tool", "user"]  # (tools paired, then ONE new message)
    assert "typed-meanwhile" in second[-1]["content"]
    assert len(rig.brain.injects_of("a1")) == 1 and rig.llm.calls[1].paired


@async_test()
async def test_without_a_soft_pause_what_arrives_in_flight_is_merged_at_the_round_boundary_as_before() -> None:
    rig = Rig()
    gate = SoftGate()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(SPEAK)), sleep_resp())
    agent = rig.agent(soft_gate=gate)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: len(rig.llm.calls) == 1)
        wake(agent, "late")  # arrives in flight: merged at the round boundary as always (no gate involved)
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert "held" not in rig.trace.names("a1") and len(rig.brain.injects_of("a1")) == 1


@async_test()
async def test_stopping_an_agent_that_is_held_cancels_the_wait_and_leaks_nothing() -> None:
    rig = Rig()
    gate = SoftGate()
    gate.close()
    agent = rig.agent(soft_gate=gate)
    agent.activate()
    wake(agent)
    await wait_until(lambda: agent.status == "held")
    assert gate.held == 1
    await agent.stop()
    assert agent.status == "sleeping" and agent.busy is False and agent.pending == []
    await settle()
    assert gate.held == 0 and rig.llm.calls == []  # (the leak check of the test decorator sees no task left)


@async_test()
async def test_stopping_an_agent_whose_call_is_in_flight_takes_it_out_of_the_count() -> None:
    rig = Rig()
    gate = SoftGate()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=sleep_resp()))
    agent = rig.agent(soft_gate=gate)
    agent.activate()
    wake(agent)
    await wait_until(lambda: gate.in_flight == 1)
    gate.close()
    assert gate.state == "draining"
    await agent.stop()  # a stopped agent holds nobody up
    assert gate.in_flight == 0 and gate.state == "held"
    hold.set()
    await settle()
    assert gate.in_flight == 0 and rig.world.submitted == []


@async_test()
async def test_an_interrupt_does_not_tear_down_a_held_run_it_waits_and_is_merged_after_the_pause() -> None:
    rig = Rig()
    gate = SoftGate()
    gate.close()
    rig.llm.script("a1", sleep_resp())
    agent = rig.agent(soft_gate=gate)
    async with running_agent(agent):
        wake(agent, "first")
        await wait_until(lambda: agent.status == "held")
        before = agent.gen_id
        for _ in range(3):  # (every interrupt used to cancel the held run and rebuild it with one more message)
            agent.deliver(revoked("pub"))
        await settle()
        assert agent.gen_id == before and agent.stats["cancels"] == 0  # the same run, never cancelled
        assert agent.status == "held" and gate.held == 1 and rig.llm.calls == []
        assert len(agent.pending) == 3  # plain pending envelopes, nothing is lost
        assert rig.trace.names("a1").count("inject_instead_of_cancel") == 3
        assert [t["why"] for t in rig.trace.of("inject_instead_of_cancel", "a1")] == ["soft_pause"] * 3
        gate.open()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.llm.calls) == 1 and agent.stats["cancels"] == 0
    request = rig.llm.calls[0].messages
    assert roles(request) == ["system", "user", "user"]  # the opening, and ONE delta that carries all three interrupts
    assert request[2]["content"].count("floor.revoked@pub") == 3
    assert len(rig.brain.injects_of("a1")) == 1 and agent.pending == []


@async_test()
async def test_an_interrupt_during_the_draining_does_not_cancel_the_call_in_flight() -> None:
    rig = Rig()
    gate = SoftGate()
    answer = asyncio.Event()
    rig.llm.script("a1", Step(hold=answer, resp=llm_resp(SPEAK)), sleep_resp())
    agent = rig.agent(soft_gate=gate)
    async with running_agent(agent):
        wake(agent, "first")
        await wait_until(lambda: gate.in_flight == 1)
        gate.close()
        assert gate.state == "draining"
        agent.deliver(revoked("pub"))  # e.g. the moderator's last batch took the floor away - the answer is still wanted
        await settle()
        assert agent.stats["cancels"] == 0 and rig.llm.cancelled == [] and gate.in_flight == 1  # the call is untouched
        answer.set()  # it finishes: written, its tools run (the commit gate decides what the world makes of them) ...
        await wait_until(lambda: agent.status == "held")
        assert gate.payload() == {"state": "held", "in_flight": 0} and [i.type for i in rig.world.submitted] == ["speak"]
        # (the round's tools are paired, and what arrived during the round is merged right behind them, as always)
        assert roles(rig.conversation().messages) == ["user", "assistant", "tool", "user"]
        assert len(rig.llm.calls) == 1 and agent.stats["cancels"] == 0
        gate.open()  # ... and the model hears of the interrupt with its next call, once the pause is over
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.llm.calls) == 2 and rig.llm.cancelled == []
    assert "floor.revoked@pub" in rig.llm.calls[1].messages[-1]["content"] and rig.llm.calls[1].paired
    assert gate.in_flight == 0


@async_test()
async def test_a_call_dropped_by_an_interrupt_before_the_pause_is_not_counted_when_the_pause_comes() -> None:
    rig = Rig()
    gate = SoftGate()
    never = asyncio.Event()
    rig.llm.script("a1", Step(hold=never, resp=sleep_resp()), sleep_resp())
    agent = rig.agent(soft_gate=gate)
    async with running_agent(agent):
        wake(agent, "first")
        await wait_until(lambda: gate.in_flight == 1)
        agent.deliver(revoked("pub"))  # no pause yet: an interrupt cancels the work in flight as it always did ...
        assert agent.stats["cancels"] == 1 and gate.in_flight == 0  # ... and the dropped call is not "in flight" any more
        gate.close()
        assert gate.payload() == {"state": "held", "in_flight": 0}  # so the pause is complete at once
        await wait_until(lambda: agent.status == "held" and gate.held == 1)
        assert len(rig.llm.calls) == 1  # the new run waits in front of its first call
        gate.open()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert gate.in_flight == 0 and len(rig.llm.calls) == 2


@async_test()
async def test_a_retry_after_a_failure_stops_at_the_gate_too() -> None:
    rig = Rig()
    gate = SoftGate()
    rig.llm.script("a1", RuntimeError("boom"), sleep_resp())
    agent = rig.agent(soft_gate=gate, retry_backoff_s=0.25)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["failures"] == 1)
        gate.close()  # the retry is due in a quarter of a second
        await wait_until(lambda: agent.status == "held", timeout=3)
        assert len(rig.llm.calls) == 1  # only the call that failed
        assert gate.in_flight == 0
        gate.open()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.llm.calls) == 2 and gate.in_flight == 0


@async_test()
async def test_a_timer_wake_up_stops_at_the_gate_too() -> None:
    rig = Rig(world=FakeWorld(ManualClock()))
    gate = SoftGate()
    rig.llm.script("a1", sleep_resp(10), sleep_resp())
    agent = rig.agent(soft_gate=gate)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
        assert agent.has_timer
        gate.close()
        rig.world.clock.advance(11)
        assert agent.fire_timer() is True  # (the supervisor's tick loop does this)
        await wait_until(lambda: agent.status == "held")
        assert len(rig.llm.calls) == 1
        gate.open()
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert len(rig.llm.calls) == 2


@async_test()
async def test_the_gate_counts_every_agent_that_waits_for_a_model() -> None:
    rig = Rig()
    heard: list = []
    gate = SoftGate(on_change=lambda g: heard.append(g.payload()))
    holds = {aid: asyncio.Event() for aid in ("a1", "a2")}
    for aid, hold in holds.items():
        rig.llm.script(aid, Step(hold=hold, resp=sleep_resp()))
    agents = [rig.agent(aid, soft_gate=gate) for aid in holds]
    for agent in agents:
        agent.activate()
        wake(agent)
    await wait_until(lambda: gate.in_flight == 2)
    gate.close()
    assert gate.payload() == {"state": "draining", "in_flight": 2}
    holds["a1"].set()
    await wait_until(lambda: gate.in_flight == 1)
    await settle()
    holds["a2"].set()
    await wait_until(lambda: gate.in_flight == 0)
    await settle()
    assert heard == [{"state": "draining", "in_flight": 1}, {"state": "held", "in_flight": 0}]
    for agent in agents:
        await agent.stop()


# ================================================================================== the pause ends while the last answer comes in


@async_test()
async def test_a_report_that_is_due_is_dropped_when_the_pause_ends_in_the_same_turn() -> None:
    heard: list = []
    gate = SoftGate(on_change=lambda g: heard.append(g.payload()))
    gate.call_started()
    gate.close()
    gate.call_finished()  # "everybody is held" is due one loop turn from now ...
    gate.open()  # ... but the pause is over before that turn comes
    await settle()
    assert heard == [] and gate.payload() is None and gate.in_flight == 0


@async_test()
async def test_the_pause_ending_in_the_very_turn_the_last_answer_comes_back_leaves_nothing_held_or_announced() -> None:
    rig = Rig()
    heard: list = []
    gate = SoftGate(on_change=lambda g: heard.append(g.payload()))
    answer = asyncio.Event()
    rig.llm.script("a1", Step(hold=answer, resp=llm_resp(SPEAK)), sleep_resp())
    agent = rig.agent(soft_gate=gate)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: gate.in_flight == 1)
        gate.close()
        assert gate.payload() == {"state": "draining", "in_flight": 1}
        answer.set()  # the answer is on its way back ...
        gate.open()  # ... and the pause ends before the loop has run anything
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert "held" not in rig.sink.statuses("a1")  # the agent never waited: the gate was open when it got to it
    assert gate.in_flight == 0 and gate.held == 0
    assert heard == []  # a pause that never got to "held" is not announced as a phase change
    assert len(rig.llm.calls) == 2 and all(c.paired for c in rig.llm.calls)
    assert [i.type for i in rig.world.submitted] == ["speak"]  # the answer was written and its tool ran


@async_test()
async def test_the_pause_closing_again_in_the_same_turn_holds_the_agent_after_the_answer_and_announces_it_once() -> None:
    rig = Rig()
    heard: list = []
    gate = SoftGate(on_change=lambda g: heard.append(g.payload()))
    answer = asyncio.Event()
    rig.llm.script("a1", Step(hold=answer, resp=llm_resp(SPEAK)), sleep_resp())
    agent = rig.agent(soft_gate=gate)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: gate.in_flight == 1)
        gate.close()
        answer.set()
        gate.open()  # a click on "继续" ...
        gate.close()  # ... and at once on "暂停" again, all before the loop runs
        await wait_until(lambda: agent.status == "held")
        assert gate.payload() == {"state": "held", "in_flight": 0} and len(rig.llm.calls) == 1
        assert [i.type for i in rig.world.submitted] == ["speak"]  # the answer in flight was written, its tool ran
        await wait_until(lambda: heard == [{"state": "held", "in_flight": 0}])  # told once: draining -> held
        gate.open()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.llm.calls) == 2 and gate.in_flight == 0 and heard == [{"state": "held", "in_flight": 0}]


# ================================================================================== a port with a concurrency limit


class SlowClient:
    """Stands in for ``LLMClient.chat_stream``: every request blocks until it is released; it remembers which agent's
    request really STARTED (the system prompt of the fake brain names the agent) and which were cancelled."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.cancelled: list[str] = []
        self.release: dict[str, asyncio.Event] = defaultdict(asyncio.Event)

    async def chat_stream(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        agent_id = messages[0]["content"][len("SYSTEM[") : -1]
        self.started.append(agent_id)
        try:
            await self.release[agent_id].wait()
        except asyncio.CancelledError:
            self.cancelled.append(agent_id)
            raise
        return sleep_resp()


def queued_table(limit: int = 1, ids=("a1", "a2", "a3")):
    """Agents behind the real model port with ``llm_concurrency = limit``: only ``limit`` requests can be on the wire."""
    rig = Rig()
    client = SlowClient()
    gate = SoftGate()
    llm_gate = LlmGate(max_concurrent=limit)
    rig.llm = GatedEngineLlm(client, lambda agent_id: None, llm_gate, timeout_s=30, soft_gate=gate)  # type: ignore[assignment]
    agents = {aid: rig.agent(aid, soft_gate=gate) for aid in ids}
    return rig, client, gate, llm_gate, agents


def wake_all(agents) -> None:
    for agent in agents.values():
        agent.activate()
        wake(agent)


@async_test()
async def test_calls_that_only_wait_for_a_concurrency_slot_are_not_in_flight_and_are_never_sent_after_the_click() -> None:
    rig, client, gate, llm_gate, agents = queued_table()
    wake_all(agents)
    await wait_until(lambda: len(client.started) == 1 and llm_gate.waiting == 2)
    first = client.started[0]
    assert gate.in_flight == 1  # three agents are waiting for a model, but only ONE request is on the wire
    assert all(a.status == "thinking" for a in agents.values())

    gate.close()  # the click
    assert gate.payload() == {"state": "draining", "in_flight": 1}  # not 3: a queued call is not an answer on its way
    queued = [agent for aid, agent in agents.items() if aid != first]
    client.release[first].set()  # the request on the wire is allowed to finish ...
    await wait_until(lambda: all(a.status == "held" for a in queued))  # ... the two that waited go to the gate instead
    assert client.started == [first]  # nothing was sent after the click
    assert gate.payload() == {"state": "held", "in_flight": 0} and gate.held == 2
    assert llm_gate.in_flight == 0 and llm_gate.waiting == 0  # and the slots were given back
    assert agents[first].status == "sleeping" and agents[first].stats["runs"] == 1
    await asyncio.sleep(0.1)
    assert client.started == [first]  # (it stays that way: nothing leaves while the gate is closed)

    for event_id in agents:  # after the pause the queued requests go out as usual, one at a time
        client.release[event_id].set()
    gate.open()
    await wait_until(lambda: all(a.stats["runs"] == 1 for a in agents.values()), timeout=5)
    assert sorted(client.started) == ["a1", "a2", "a3"] and client.started[0] == first
    assert gate.in_flight == 0 and gate.held == 0 and llm_gate.in_flight == 0 and llm_gate.waiting == 0
    for agent in agents.values():
        await agent.stop()


@async_test()
async def test_the_queued_agents_are_asked_again_in_one_message_after_the_pause() -> None:
    rig, client, gate, llm_gate, agents = queued_table(ids=("a1", "a2"))
    wake_all(agents)
    await wait_until(lambda: len(client.started) == 1 and llm_gate.waiting == 1)
    first = client.started[0]
    second = next(aid for aid in agents if aid != first)
    gate.close()
    client.release[first].set()
    await wait_until(lambda: agents[second].status == "held")
    wake(agents[second], "typed-meanwhile")  # a person's words arrive while the call of this agent is parked
    client.release[second].set()
    gate.open()
    await wait_until(lambda: agents[second].stats["runs"] == 1)
    assert client.started == [first, second]  # one request for it, not one per message
    assert len(rig.brain.injects_of(second)) == 1  # the words were merged into the one message it was asked with
    assert labels(rig.brain.injects_of(second)[0].reasons) == ["typed-meanwhile"]
    for agent in agents.values():
        await agent.stop()


@async_test()
async def test_stopping_agents_with_calls_on_the_wire_and_in_the_queue_leaves_no_count_behind() -> None:
    rig, client, gate, llm_gate, agents = queued_table()
    wake_all(agents)
    await wait_until(lambda: len(client.started) == 1 and llm_gate.waiting == 2)
    first = client.started[0]
    gate.close()
    assert gate.payload() == {"state": "draining", "in_flight": 1}
    await agents[first].stop()  # the request on the wire is cancelled: it is not "in flight" any more
    assert client.cancelled == [first] and gate.in_flight == 0 and gate.state == "held"
    queued = [agent for aid, agent in agents.items() if aid != first]
    await wait_until(lambda: all(a.status == "held" for a in queued))
    for agent in queued:  # stopping an agent that waits at the gate
        await agent.stop()
    await settle()
    assert client.started == [first] and gate.held == 0 and gate.in_flight == 0
    assert llm_gate.in_flight == 0 and llm_gate.waiting == 0
    assert all(a.status == "sleeping" and not a.busy for a in agents.values())


@async_test()
async def test_a_call_cancelled_while_it_waits_for_a_slot_was_never_counted_and_is_asked_again() -> None:
    rig, client, gate, llm_gate, agents = queued_table(ids=("a1", "a2"))
    wake_all(agents)
    await wait_until(lambda: len(client.started) == 1 and llm_gate.waiting == 1)
    first = client.started[0]
    second = next(aid for aid in agents if aid != first)
    agents[second].deliver(revoked("pub"))  # no pause: an interrupt cancels the call that waits - nothing was on the wire
    await settle()
    assert agents[second].stats["cancels"] == 1 and client.cancelled == []  # (the wire was never involved)
    assert gate.in_flight == 1 and client.started == [first]  # still only the one request, still counted once
    await wait_until(lambda: llm_gate.waiting == 1)  # the agent asked again, behind the request on the wire
    for aid in agents:
        client.release[aid].set()
    await wait_until(lambda: all(a.stats["runs"] == 1 for a in agents.values()), timeout=5)
    assert gate.in_flight == 0 and llm_gate.in_flight == 0 and llm_gate.waiting == 0
    for agent in agents.values():
        await agent.stop()


@async_test()
async def test_the_port_refuses_to_send_while_the_gate_is_closed_gives_the_slot_back_and_logs_nothing() -> None:
    gate = SoftGate()
    gate.close()
    log = CallLog("s")
    client = SlowClient()
    llm_gate = LlmGate(max_concurrent=1)
    port = GatedEngineLlm(client, lambda agent_id: None, llm_gate, call_log=log, soft_gate=gate)
    for _ in range(2):  # (the slot is free again after each refusal)
        with pytest.raises(GateClosed):
            await port.complete("a1", [{"role": "system", "content": "SYSTEM[a1]"}], None)
    assert client.started == [] and gate.in_flight == 0 and llm_gate.in_flight == 0 and llm_gate.waiting == 0
    assert len(log) == 0  # a request that was never sent is not a line of the error log
    gate.open()
    client.release["a1"].set()
    resp = await port.complete("a1", [{"role": "system", "content": "SYSTEM[a1]"}], None)
    assert resp.tool_calls and client.started == ["a1"] and gate.in_flight == 0 and len(log) == 1  # open: as always


@async_test()
async def test_a_port_without_slots_keeps_the_agents_own_count_also_behind_the_error_log_wrapper() -> None:
    rig = Rig()
    gate = SoftGate()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=sleep_resp()))
    rig.llm = LoggedLlm(rig.llm, CallLog("s"))  # type: ignore[assignment]  # how the orchestrator wraps bots and fakes
    agent = rig.agent(soft_gate=gate)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: gate.in_flight == 1)  # counted by the agent: the wrapped fake has no gate of its own
        gate.close()
        assert gate.payload() == {"state": "draining", "in_flight": 1}
        hold.set()
        await wait_until(lambda: gate.payload() == {"state": "held", "in_flight": 0})
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert gate.in_flight == 0


# ================================================================================== the supervisor


def supervisor_config(**overrides) -> SupervisorConfig:
    values = {
        "tick_interval": 0.02, "watchdog_s": 0.15, "watchdog_poll_s": 0.02, "max_watchdog_wakes": 2,
        "metrics_interval_s": 0.05,
    }
    values.update(overrides)
    return SupervisorConfig(**values)


def gated_supervisor(rig: Rig, gate: SoftGate | None, ids=(MODERATOR, "a1", "a2"), **cfg) -> tuple[Supervisor, dict]:
    agents = {aid: rig.agent(aid, "moderator" if aid == MODERATOR else "player") for aid in ids}
    sup = Supervisor(
        rig.world, agents, rig.sink, config=supervisor_config(**cfg), usage=rig.usage, trace=rig.trace, book=rig.book,
        soft_gate=gate,
    )
    return sup, agents


def starts_of(rig: Rig, agent_id: str):
    return [ctx for ctx in rig.brain.starts if ctx.agent_id == agent_id]


@async_test()
async def test_the_supervisor_hands_its_gate_to_every_agent() -> None:
    rig = Rig()
    gate = SoftGate()
    sup, agents = gated_supervisor(rig, gate)
    assert sup.soft_gate is gate and all(a.soft_gate is gate for a in agents.values())
    plain_sup, plain = gated_supervisor(Rig(), None)
    assert plain_sup.soft_gate is None and all(a.soft_gate is None for a in plain.values())


@async_test()
async def test_the_watchdog_stays_quiet_while_the_gate_is_closed_and_counts_nothing() -> None:
    rig = Rig()
    rig.llm.default = lambda call: sleep_resp()
    stalls: list = []
    gate = SoftGate()
    gate.close()
    sup, _ = gated_supervisor(rig, gate)
    sup.on_stall = stalls.append
    loop = asyncio.get_running_loop()
    sup.start()
    closed_at = loop.time()
    await asyncio.sleep(1.0)  # more than six watchdog periods, all of them soft-paused (a lower bound: slower is fine)
    assert starts_of(rig, MODERATOR) == [] and stalls == [] and sup.stalled is False
    assert rig.trace.of("watchdog", MODERATOR) == []  # no wake-up, no stall, nothing counted
    held_for = loop.time() - closed_at
    gate.open()
    await wait_until(lambda: starts_of(rig, MODERATOR), timeout=10)  # silence at last: the first wake-up
    first = starts_of(rig, MODERATOR)[0].reasons[0].payload
    assert first["event"] == "watchdog" and first["attempt"] == 1  # the count started at 1, not after the pause
    # The time spent held is not idle time.  Had it been counted, ``idle_s`` would be at least as long as the pause; it is
    # a few watchdog periods instead (compared with what the pause really lasted, so a slow machine cannot upset it).
    assert first["idle_s"] < held_for
    await sup.stop()


@async_test()
async def test_a_soft_pause_forgets_the_fruitless_wakes_and_takes_the_stall_banner_down() -> None:
    rig = Rig()
    rig.llm.default = lambda call: sleep_resp()  # the moderator wakes up and does nothing: the banner goes up
    gate = SoftGate()
    stalls: list = []
    sup, _ = gated_supervisor(rig, gate, max_watchdog_wakes=1)
    sup.on_stall = stalls.append
    sup.start()
    await wait_until(lambda: any(isinstance(s, str) for s in stalls), timeout=10)  # "the game is stuck" is on the screen
    assert sup.stalled is True and "对局静止" in stalls[-1]

    gate.close()  # the click: nothing is stuck now, a person is thinking
    await wait_until(lambda: stalls[-1] is None and sup.stalled is False, timeout=5)  # the banner comes down at once
    actions = [r["action"] for r in rig.trace.of("watchdog", MODERATOR)]
    assert actions[-1] == "soft_pause" and actions.count("wake") >= 1
    marked = len(actions)
    await asyncio.sleep(0.6)  # four watchdog periods, soft-paused: nothing is counted and no banner comes back
    assert len(rig.trace.of("watchdog", MODERATOR)) == marked and stalls[-1] is None and sup.stalled is False

    gate.open()  # the silence is measured afresh: the first wake-up after the pause is the FIRST one again ...
    await wait_until(lambda: len(rig.trace.of("watchdog", MODERATOR)) > marked, timeout=10)
    after = rig.trace.of("watchdog", MODERATOR)[marked:]
    assert after[0]["action"] == "wake" and after[0]["wakes"] == 1  # ... not one more than before the pause
    await wait_until(lambda: stalls[-1] is not None, timeout=10)  # (and the banner needs its fruitless wake-up again)
    await sup.stop()


@async_test()
async def test_the_watchdog_does_not_wake_the_moderator_while_a_person_holds_a_floor_without_a_deadline() -> None:
    rig = Rig()
    rig.llm.default = lambda call: sleep_resp()
    rig.world.untimed_open = True  # (a person holds a public floor with no deadline; ``next_deadline()`` is None)
    stalls: list = []
    sup, _ = gated_supervisor(rig, None)
    sup.on_stall = stalls.append
    sup.start()
    await asyncio.sleep(1.0)  # more than six watchdog periods (a lower bound: the positive control below proves it was armed)
    assert starts_of(rig, MODERATOR) == [] and stalls == [] and sup.stalled is False
    rig.world.untimed_open = False  # the person has spoken: the game is silent now
    await wait_until(lambda: starts_of(rig, MODERATOR), timeout=10)
    assert starts_of(rig, MODERATOR)[0].reasons[0].payload["event"] == "watchdog"
    await sup.stop()


@async_test()
async def test_a_world_without_the_question_is_not_waiting_for_anybody() -> None:
    class Plain(FakeWorld):
        untimed_floor_open = None  # type: ignore[assignment]  # (a world that does not know untimed floors at all)

    rig = Rig(world=Plain())
    rig.llm.default = lambda call: sleep_resp()
    sup, _ = gated_supervisor(rig, None)
    sup.start()
    await wait_until(lambda: starts_of(rig, MODERATOR), timeout=3)  # the old behaviour: silence is detected
    await sup.stop()


@async_test()
async def test_stopping_the_supervisor_while_agents_are_held_leaves_nothing_behind() -> None:
    rig = Rig()
    gate = SoftGate()
    gate.close()
    sup, agents = gated_supervisor(rig, gate, watchdog_s=100.0)
    sup.start({"a1": [urgent("game.started", kind="notice")], MODERATOR: [urgent("game.started", kind="notice")]})
    await wait_until(lambda: agents["a1"].status == "held" and agents[MODERATOR].status == "held")
    assert gate.held == 2 and rig.llm.calls == []
    await sup.stop()
    assert all(a.status == "sleeping" for a in agents.values())
    await settle()
    assert gate.held == 0  # (and no task is left: checked by the decorator)


class GatedRandomLlm(RandomLlm):
    """The random model of the agent fuzz that notes every call that *starts* while the gate is closed."""

    def __init__(self, rng, rig, gate: SoftGate, **kw) -> None:
        super().__init__(rng, rig, **kw)
        self.gate = gate
        self.started_while_closed: list[str] = []

    async def complete(self, agent_id, messages, tools, *, max_tokens=None):  # type: ignore[override]
        if self.gate.closed:
            self.started_while_closed.append(agent_id)
        return await super().complete(agent_id, messages, tools, max_tokens=max_tokens)


@pytest.mark.parametrize("seed", [21, 22, 23, 24])
def test_random_traffic_with_a_soft_pause_that_comes_and_goes_keeps_the_invariants(seed) -> None:
    """Whatever arrives and whenever the gate closes or opens: no model call ever *starts* while it is closed, the count
    of calls in flight is exact, nothing is lost, paired and leaked, and every agent ends asleep."""

    @async_test(timeout=60)
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop_errors: list[dict] = []
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        rng = random.Random(seed)
        rig = Rig(world=FakeWorld(ManualClock(0.0)))
        heard: list = []
        gate = SoftGate(on_change=lambda g: heard.append(g.payload()))
        llm = GatedRandomLlm(rng, rig, gate, error_rate=0.05)
        rig.llm = llm
        ids = (MODERATOR, "a1", "a2", "a3")
        agents = {
            aid: rig.agent(
                aid, "moderator" if aid == MODERATOR else "player", retry_backoff_s=0.01, retry_backoff_max_s=0.02,
                cancel_grace_s=0.5,
            )
            for aid in ids
        }
        sup = Supervisor(
            rig.world, agents, rig.sink, config=SupervisorConfig(tick_interval=0.01, watchdog_s=100.0, metrics_interval_s=0.05),
            usage=rig.usage, trace=rig.trace, book=rig.book, soft_gate=gate,
        )
        delivered: dict[str, list] = {aid: [] for aid in ids}
        flips = {"closed": 0, "opened": 0}

        def flip() -> None:
            if gate.closed:
                gate.open()
                flips["opened"] += 1
            else:
                gate.close()
                flips["closed"] += 1

        def on_tick() -> None:
            if rng.random() < 0.4:
                target = rng.choice(ids)
                envelope = random_envelope(rng)
                delivered[target].append(envelope)
                rig.world.push(target, envelope)
            if rng.random() < 0.2:
                rig.world.clock.advance(rng.random() * 8)
            if rng.random() < 0.08:
                flip()
            rig.world.bump()

        rig.world.on_tick = on_tick
        sup.start({MODERATOR: [urgent("game.started", kind="notice")]})
        end = loop.time() + 1.8
        while loop.time() < end:
            for _ in range(rng.randint(0, 4)):
                target = rng.choice(ids)
                envelope = random_envelope(rng)
                delivered[target].append(envelope)
                rig.world.push(target, envelope)
            if rng.random() < 0.1:
                flip()
            if gate.closed:  # (the numbers the clients are told are always the real ones)
                assert 0 <= gate.in_flight <= len(ids) and gate.held <= len(ids)
            await asyncio.sleep(rng.random() * 0.02)
        rig.world.on_tick = None
        gate.open()  # the end of the pause: everything that was held goes on and the table settles
        await wait_until(lambda: all(a.is_quiet() for a in agents.values()), timeout=10, message="the agents never settled")
        await asyncio.sleep(0.05)
        stats = {aid: dict(a.stats) for aid, a in agents.items()}
        await sup.stop()

        assert flips["closed"] >= 2 and flips["opened"] >= 2  # the gate really came and went
        assert llm.started_while_closed == []  # a closed gate lets no call start
        assert gate.in_flight == 0 and gate.held == 0 and llm.in_flight == 0
        assert all(h is None or h["state"] in ("draining", "held") for h in heard)
        for aid in ids:
            check_agent(rig, llm, aid, stats[aid], delivered[aid])
            assert agents[aid].status == "sleeping" and rig.sink.statuses(aid)[-1] == "sleeping"
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()] == []
        assert loop_errors == [], loop_errors

    scenario()


@async_test()
async def test_a_soft_pause_never_cancels_the_world_it_only_stops_new_calls() -> None:
    """The world keeps ticking and pumping while the agents are held: deliveries pile up as pending, nothing is lost."""
    rig = Rig()
    rig.llm.default = lambda call: sleep_resp()
    gate = SoftGate()
    gate.close()
    sup, agents = gated_supervisor(rig, gate, watchdog_s=100.0)
    sup.start({"a1": [normal("message.new", label="x", channel_id="pub")]})
    await wait_until(lambda: agents["a1"].status == "held")
    rig.world.push("a1", normal("message.new", label="y", channel_id="pub"))
    rig.world.push("a1", normal("message.new", label="z", channel_id="pub"))
    await wait_until(lambda: len(agents["a1"].pending) == 2)
    ticks = rig.world.tick_count
    await asyncio.sleep(0.12)
    assert rig.world.tick_count > ticks  # the world is ticked all the time
    assert rig.llm.calls == []
    gate.open()
    await wait_until(lambda: len(rig.llm.calls) == 1)
    assert labels(rig.brain.injects_of("a1")[0].reasons) == ["y", "z"]
    await sup.stop()
