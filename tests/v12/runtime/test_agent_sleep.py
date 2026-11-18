"""``sleep(seconds)`` (v0.12.1): a timed sleep is a deadline on the *game* clock (frozen while the game is paused)
that lives in the conversation, so it survives stopping and re-creating the agent; any arrival voids it."""

from __future__ import annotations

import asyncio
import math

import pytest

from app.runtime.clock import ManualClock
from app.runtime.ports import ToolOutcome
from app.world.types import ENGINE_ID

from .fakes import (
    FakeWorld,
    Rig,
    Step,
    async_test,
    llm_resp,
    normal,
    running_agent,
    settle,
    sleep_resp,
    speak_resp,
    wait_until,
)


def wake(agent, label: str = "m", **kw) -> None:
    kw.setdefault("channel_id", "pub")
    agent.deliver(normal("message.new", label=label, **kw))


def events(envelopes):
    return [e.payload["event"] for e in envelopes]


def timed_rig(start: float = 100.0) -> Rig:
    return Rig(world=FakeWorld(ManualClock(start)))


# ================================================================================ setting it


@async_test()
async def test_sleep_with_seconds_sets_a_deadline_on_the_game_clock_and_publishes_it():
    rig = timed_rig(100.0)
    rig.llm.script("a1", speak_resp(), sleep_resp(30))
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
        conv = rig.conversation()
        assert conv.sleep_until == 130.0 and conv.slept_s == 30.0
        assert agent.sleep_until == 130.0 and agent.has_timer
        assert rig.sink.status_events("a1")[-1] == ("sleeping", 130.0)  # the UI shows "wakes at 130"
    assert rig.trace.of("sleep", "a1")[0]["until"] == 130.0
    assert rig.trace.of("run_end", "a1")[0]["sleep_s"] == 30.0


@async_test()
async def test_sleep_without_seconds_sleeps_until_something_arrives():
    rig = timed_rig()
    rig.llm.script("a1", sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
        assert rig.conversation().sleep_until is None and not agent.has_timer
        assert rig.sink.status_events("a1")[-1] == ("sleeping", None)
        assert agent.timer_due(10**9) is False and agent.fire_timer(10**9) is False


@async_test()
async def test_thinking_and_acting_never_carry_an_until():
    rig = timed_rig()
    rig.llm.script("a1", speak_resp(), sleep_resp(30), speak_resp(), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        wake(agent, "m2")  # voids the timer
        await wait_until(lambda: agent.stats["runs"] == 2)
    for state, until in rig.sink.status_events("a1"):
        assert until is None or state == "sleeping"
    assert ("sleeping", 130.0) in rig.sink.status_events("a1") and rig.sink.status_events("a1")[-1] == ("sleeping", None)


@async_test()
async def test_several_sleeps_in_one_answer_use_the_shortest_valid_timer():
    rig = timed_rig(0.0)
    answer = llm_resp(
        ("sleep", {"seconds": 50}),
        ("sleep", {"seconds": 20}),
        ("sleep", {"seconds": 0}),  # not a timer
        ("sleep", {"seconds": -5}),
        ("sleep", {"seconds": "soon"}),
        ("sleep", {"seconds": float("nan")}),
        ("sleep", {}),
    )
    rig.llm.script("a1", answer)
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.conversation().sleep_until == 20.0


@async_test()
async def test_a_timer_that_is_not_a_positive_number_means_no_timer():
    for value in (0, -1, "x", None, math.inf, float("nan")):
        rig = timed_rig()
        rig.brain.tool_handlers["sleep"] = lambda agent_id, args, *, gen_id, seen_seq, v=value: ToolOutcome(
            text="{}", ends_run=True, sleep_s=v
        )
        rig.llm.script("a1", sleep_resp())
        agent = rig.agent()
        async with running_agent(agent):
            wake(agent)
            await wait_until(lambda: agent.stats["runs"] == 1)
        assert rig.conversation().sleep_until is None, value


@async_test()
async def test_a_timer_asked_for_without_ending_the_run_is_ignored():
    rig = timed_rig()
    rig.brain.tool_handlers["speak"] = lambda agent_id, args, *, gen_id, seen_seq: ToolOutcome(text="{}", sleep_s=5)
    rig.llm.script("a1", speak_resp(), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.conversation().sleep_until is None and len(rig.llm.calls) == 2  # the run went on, no timer


@async_test()
async def test_a_plain_answer_without_tools_is_a_sleep_without_a_timer():
    rig = timed_rig()
    rig.brain.nudge_text = None
    rig.llm.script("a1", llm_resp(content="nothing to do"))
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.conversation().sleep_until is None and rig.sink.status_events("a1")[-1] == ("sleeping", None)


@async_test()
async def test_no_timer_is_armed_when_something_arrived_as_the_run_ended():
    rig = timed_rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=sleep_resp(30)), sleep_resp(20))
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: len(rig.llm.calls) == 1)
        wake(agent, "m2")  # while the model asks for a 30 s sleep
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 2)
        assert rig.conversation().sleep_until == rig.world.clock.now() + 20  # only the sleep that really ended the story
    assert agent.stats["activations"] == 2


# ================================================================================== firing it


@async_test()
async def test_the_timer_is_due_at_the_deadline_and_wakes_the_agent_with_a_timer_notice():
    rig = timed_rig(0.0)
    rig.llm.script("a1", sleep_resp(30), speak_resp(), sleep_resp())
    agent = rig.agent()
    clock = rig.world.clock
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
        clock.advance(29.9)
        assert agent.timer_due(clock.now()) is False and agent.fire_timer() is False
        clock.advance(0.1)
        assert agent.timer_due(clock.now()) is True
        assert agent.fire_timer() is True  # the supervisor does this from its tick loop
        assert agent.timer_due(clock.now()) is False and agent.fire_timer() is False  # one wake-up per timer
        await wait_until(lambda: agent.stats["runs"] == 2)
        assert rig.conversation().sleep_until is None  # consumed
        clock.advance(1000)
        assert agent.timer_due(clock.now()) is False
    (delta,) = rig.brain.injects
    (notice,) = delta.reasons
    assert notice.kind == "notice" and notice.priority == "normal" and notice.sender == ENGINE_ID
    assert notice.payload == {"event": "timer.elapsed", "slept_s": 30.0}
    assert rig.llm.calls[1].messages[-1]["content"] == "DELTA[a1] after=0 reasons=[timer.elapsed]"
    assert rig.trace.of("timer", "a1")[0]["slept_s"] == 30.0


@async_test()
async def test_a_message_before_the_deadline_voids_the_timer_for_good():
    rig = timed_rig(0.0)
    hold = asyncio.Event()
    rig.llm.script("a1", sleep_resp(30), Step(hold=hold, resp=sleep_resp()))
    agent = rig.agent()
    clock = rig.world.clock
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        assert rig.conversation().sleep_until == 30.0
        clock.advance(10)
        wake(agent, "m2")
        assert agent.timer_due(clock.now() + 1000) is False  # an activation is already on its way
        await wait_until(lambda: len(rig.llm.calls) == 2)  # the activation voided the timer ...
        assert rig.conversation().sleep_until is None  # ... at once, even while the model is still thinking
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 2)
        clock.advance(1000)  # long past the old deadline
        assert agent.timer_due(clock.now()) is False and agent.fire_timer() is False
    assert "timer.elapsed" not in [e for d in rig.brain.injects for e in events(d.reasons)]
    assert rig.sink.status_events("a1")[-1] == ("sleeping", None)


@async_test()
async def test_an_arrival_that_is_not_worth_a_message_leaves_the_sleep_as_it_was():
    rig = timed_rig(0.0)
    rig.llm.script("a1", sleep_resp(30))
    agent = rig.agent()
    clock = rig.world.clock
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        rig.brain.inject_hook = lambda ctx: None  # the brain has nothing to tell about this one
        clock.advance(10)
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 2)
        assert len(rig.llm.calls) == 1  # no model call ...
        assert rig.conversation().sleep_until == 30.0 and agent.has_timer  # ... and the timer goes on
        assert rig.sink.status_events("a1")[-1] == ("sleeping", 30.0)
        clock.advance(20)
        assert agent.fire_timer() is True
        rig.brain.inject_hook = None


@async_test()
async def test_an_elapsed_timer_is_consumed_even_when_the_brain_has_nothing_to_tell():
    rig = timed_rig(0.0)
    rig.llm.script("a1", sleep_resp(30))
    agent = rig.agent()
    clock = rig.world.clock
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
        rig.brain.inject_hook = lambda ctx: None
        clock.advance(30)
        assert agent.fire_timer() is True
        await wait_until(lambda: agent.stats["runs"] == 2)
        assert rig.conversation().sleep_until is None  # otherwise the tick loop would wake it again and again
        clock.advance(100)
        assert agent.timer_due(clock.now()) is False


# ============================================================================ pause and resume


@async_test()
async def test_the_timer_does_not_run_while_the_game_clock_is_paused():
    rig = timed_rig(0.0)
    rig.llm.script("a1", sleep_resp(10))
    agent = rig.agent()
    clock = rig.world.clock
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
        clock.advance(4)  # 6 s to go
        clock.pause()
        clock.advance(10_000)  # a long pause: the wall clock moves, the game clock does not
        assert clock.now() == pytest.approx(4.0) and agent.timer_due(clock.now()) is False
        clock.resume()
        clock.advance(5.5)
        assert agent.timer_due(clock.now()) is False  # 0.5 s left
        clock.advance(0.5)
        assert agent.timer_due(clock.now()) is True


@async_test()
async def test_a_timer_survives_a_stop_and_a_new_agent_object_with_the_same_book():
    # pause -> resume: new Agent objects, the same ConversationBook (the orchestrator keeps it)
    rig = timed_rig(0.0)
    rig.llm.script("a1", sleep_resp(30), sleep_resp())
    first = rig.agent()
    clock = rig.world.clock
    async with running_agent(first):
        wake(first)
        await wait_until(lambda: first.stats["runs"] == 1)
    assert rig.conversation().sleep_until == 30.0  # stopping does not forget the timer
    assert first.timer_due(100) is False and first.fire_timer(100) is False  # a stopped agent never fires
    clock.advance(12)  # (the game ran 12 more seconds before it was paused)

    second = rig.agent()
    async with running_agent(second):
        assert rig.sink.status_events("a1")[-1] == ("sleeping", 30.0)  # the UI is told again when it is armed
        assert second.has_timer and second.timer_due(clock.now()) is False
        clock.advance(18)
        assert second.timer_due(clock.now()) is True and second.fire_timer() is True
        await wait_until(lambda: second.stats["runs"] == 1)
    assert len(rig.brain.starts) == 1 and events(rig.brain.injects[0].reasons) == ["timer.elapsed"]


@async_test()
async def test_resetting_the_book_forgets_the_timer_with_the_conversation():
    rig = timed_rig(0.0)
    rig.llm.script("a1", sleep_resp(30))
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
        rig.book.reset()  # a new game
        rig.world.clock.advance(100)
        assert agent.has_timer is False and agent.timer_due(rig.world.clock.now()) is False


@async_test()
async def test_a_due_timer_and_a_message_in_the_same_turn_make_one_activation():
    rig = timed_rig(0.0)
    rig.llm.script("a1", sleep_resp(30), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        rig.world.clock.advance(30)
        wake(agent, "m2")  # the world woke it in the very turn the timer ran out
        assert agent.timer_due(rig.world.clock.now()) is False and agent.fire_timer() is False
        await wait_until(lambda: agent.stats["runs"] == 2)
        await settle(10)
    assert agent.stats["activations"] == 2 and events(rig.brain.injects[0].reasons) == ["message.new"]
