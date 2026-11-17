"""Supervisor (v0.12.1): start / stop, the pump, ENGINE deliveries, world ticks, agent sleep timers, the watchdog
(a warning, never a pause), metrics.  Agents are not tasks any more: the only tasks are the supervisor's three loops
and one worker per run."""

from __future__ import annotations

import asyncio

import pytest

from app.runtime.agent import Agent, AgentConfig
from app.runtime.clock import ManualClock
from app.runtime.conversation import ConversationBook
from app.runtime.supervisor import Supervisor, SupervisorConfig
from app.world.types import ENGINE_ID, Receipt

from .fakes import (
    MODERATOR,
    FakeWorld,
    Rig,
    Step,
    async_test,
    env,
    llm_resp,
    normal,
    settle,
    sleep_resp,
    speak_resp,
    urgent,
    wait_until,
)

SPEAK = ("speak", {"channel_id": "pub", "content": "hello"})


def fast_supervisor_config(**overrides) -> SupervisorConfig:
    values = {
        "tick_interval": 0.02,
        "watchdog_s": 0.15,
        "watchdog_poll_s": 0.02,
        "max_watchdog_wakes": 2,
        "metrics_interval_s": 0.05,
    }
    values.update(overrides)
    return SupervisorConfig(**values)


def make_supervisor(
    rig: Rig, ids=(MODERATOR, "a1", "a2"), *, on_engine=None, on_stall=None, **cfg
) -> tuple[Supervisor, dict[str, Agent]]:
    agents = {aid: rig.agent(aid, "moderator" if aid == MODERATOR else "player") for aid in ids}
    sup = Supervisor(
        rig.world,
        agents,
        rig.sink,
        on_engine=on_engine,
        config=fast_supervisor_config(**cfg),
        on_stall=on_stall,
        usage=rig.usage,
        trace=rig.trace,
        book=rig.book,
    )
    return sup, agents


def starts_of(rig: Rig, agent_id: str):
    return [ctx for ctx in rig.brain.starts if ctx.agent_id == agent_id]


def injects_of(rig: Rig, agent_id: str):
    return [ctx for ctx in rig.brain.injects if ctx.agent_id == agent_id]


def reasons_of(rig: Rig, agent_id: str) -> list[str]:
    """Every wake reason the agent was ever told, in order (openings and deltas)."""
    contexts = sorted(starts_of(rig, agent_id) + injects_of(rig, agent_id), key=lambda c: c.gen_id)
    return [e.payload["event"] for c in contexts for e in c.reasons]


def labels(envelopes):
    return [e.payload.get("label") for e in envelopes]


def others() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]


# =============================================================================== lifecycle


@async_test()
async def test_start_arms_every_agent_and_stop_leaves_no_task_behind():
    rig = Rig()
    sup, agents = make_supervisor(rig)
    assert not sup.running
    await sup.start()  # start() is synchronous, but awaiting its result is allowed too
    assert sup.running
    # no task per agent: only the three loops (workers exist only while a model call is being made)
    assert {t.get_name() for t in others()} == {"supervisor:tick", "supervisor:watchdog", "supervisor:metrics"}
    assert all(a.status == "sleeping" for a in agents.values())
    assert all(rig.sink.statuses(agent_id) == ["sleeping"] for agent_id in agents)
    assert rig.world.notifier is not None
    with pytest.raises(RuntimeError):
        sup.start()  # already running

    await sup.stop()
    assert others() == []  # nothing leaked: loops, workers
    assert not sup.running
    assert all(rig.sink.statuses(agent_id) == ["sleeping"] for agent_id in agents)  # still just the one: nothing to change
    await sup.stop()  # idempotent


@async_test()
async def test_a_stopped_supervisor_can_be_started_again_and_the_conversations_continue():
    rig = Rig()
    rig.llm.script("a1", sleep_resp(), sleep_resp())
    sup, agents = make_supervisor(rig)
    sup.start({"a1": [urgent("game.started", kind="notice")]})
    await wait_until(lambda: agents["a1"].stats["runs"] == 1)
    await sup.stop()
    assert others() == []
    sup.start({"a1": [urgent("game.resumed", kind="notice")]})  # pause -> resume
    await wait_until(lambda: agents["a1"].stats["runs"] == 2)
    await sup.stop()
    assert len(starts_of(rig, "a1")) == 1 and [e.payload["event"] for e in injects_of(rig, "a1")[0].reasons] == ["game.resumed"]
    assert reasons_of(rig, "a1") == ["game.started", "game.resumed"]


@async_test()
async def test_stop_right_after_start_does_not_hang_or_leak():
    rig = Rig()
    sup, agents = make_supervisor(rig)
    sup.start({"a1": [urgent("game.started", kind="notice")]})
    await sup.stop()  # the activation has not even had its turn
    assert others() == []
    assert all(a.status == "sleeping" for a in agents.values())
    assert rig.llm.calls == []


@async_test()
async def test_concurrent_stop_calls_wait_for_the_same_shutdown():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=sleep_resp()))
    sup, agents = make_supervisor(rig)
    sup.start({"a1": [urgent("game.started", kind="notice")]})
    await wait_until(lambda: len(rig.llm.calls) == 1)
    await asyncio.gather(sup.stop(), sup.stop(), sup.stop())
    assert others() == [] and rig.llm.cancelled == ["a1"]


@async_test()
async def test_stop_cancels_in_flight_workers_of_every_agent_and_every_agent_shows_sleeping():
    rig = Rig()
    hold = asyncio.Event()
    for agent_id in (MODERATOR, "a1", "a2"):
        rig.llm.script(agent_id, Step(hold=hold, resp=sleep_resp()))
    sup, agents = make_supervisor(rig)
    sup.start({aid: [urgent("game.started", kind="notice")] for aid in agents})
    await wait_until(lambda: len(rig.llm.calls) == 3)
    assert all(a.status == "thinking" for a in agents.values())
    await sup.stop()
    assert sorted(rig.llm.cancelled) == sorted([MODERATOR, "a1", "a2"])
    assert others() == []
    assert rig.brain.executed == []  # nothing was committed on the way down
    for agent_id in agents:  # the UI is told once: whatever it was doing, it now sleeps (never "stopped" / "idle")
        assert rig.sink.statuses(agent_id) == ["sleeping", "thinking", "sleeping"]


@async_test()
async def test_initial_wakes_are_delivered_and_open_work_is_redelivered_and_pumped_at_once():
    rig = Rig()
    rig.llm.script(MODERATOR, sleep_resp())
    rig.llm.script("a1", sleep_resp())
    rig.world.on_redeliver = lambda: rig.world.push("a1", urgent("request.opened", kind="request", request_id="r1"))
    sup, agents = make_supervisor(rig)
    sup.start({MODERATOR: [urgent("game.started", kind="notice")], "ghost": [urgent("x")]})  # unknown ids are ignored
    assert rig.world.redeliver_count == 1
    await wait_until(lambda: agents[MODERATOR].stats["runs"] == 1 and agents["a1"].stats["runs"] == 1)
    await sup.stop()
    assert reasons_of(rig, MODERATOR) == ["game.started"]
    assert reasons_of(rig, "a1") == ["request.opened"]


@async_test()
async def test_agents_may_be_given_as_a_plain_list_and_deliver_reports_unknown_targets():
    rig = Rig()
    rig.llm.script("a1", sleep_resp())
    a1 = rig.agent("a1")
    sup = Supervisor(rig.world, [a1], rig.sink, config=fast_supervisor_config())
    sup.start()
    assert sup.deliver("a1", urgent("game.resumed", kind="notice")) is True
    assert sup.deliver("nobody", urgent("x")) is False
    await wait_until(lambda: a1.stats["runs"] == 1)
    await sup.stop()


@async_test()
async def test_the_supervisor_shares_one_conversation_book_with_all_its_agents():
    rig = Rig()
    shared = ConversationBook()
    private = [Agent(aid, "player", rig.world, rig.brain, rig.llm, rig.usage, rig.trace, rig.sink, AgentConfig()) for aid in ("a1", "a2")]
    assert private[0].book is not private[1].book
    sup = Supervisor(rig.world, private, rig.sink, config=fast_supervisor_config(), book=shared)
    assert sup.book is shared and all(a.book is shared for a in private)
    # without an explicit book the supervisor adopts its first agent's, and everybody uses it
    agents = [Agent(aid, "player", rig.world, rig.brain, rig.llm, rig.usage, rig.trace, rig.sink, AgentConfig()) for aid in ("b1", "b2")]
    sup2 = Supervisor(rig.world, agents, rig.sink, config=fast_supervisor_config())
    assert sup2.book is agents[0].book and agents[1].book is agents[0].book
    assert len(sup.book) == 0  # an empty book is still a book (it is falsy, but must not be replaced)
    empty = ConversationBook()
    sup3 = Supervisor(rig.world, [], rig.sink, book=empty)
    assert sup3.book is empty


@async_test()
async def test_the_supervisor_can_set_the_compaction_thresholds_of_all_its_agents():
    rig = Rig()
    agents = {aid: rig.agent(aid) for aid in ("a1", "a2")}
    sup = Supervisor(rig.world, agents, rig.sink, config=fast_supervisor_config(compact_at=0.5, trim_at=0.8))
    assert all(a.config.compact_at == 0.6 and a.config.trim_at == 0.9 for a in agents.values())
    sup.start()
    assert all(a.config.compact_at == 0.5 and a.config.trim_at == 0.8 for a in agents.values())
    await sup.stop()
    # unset: the agents keep what they were built with
    keep = {aid: rig.agent(aid, compact_at=0.3) for aid in ("b1",)}
    sup2 = Supervisor(rig.world, keep, rig.sink, config=fast_supervisor_config())
    sup2.start()
    assert keep["b1"].config.compact_at == 0.3
    await sup2.stop()


# =============================================================================== the pump


@async_test()
async def test_the_pump_routes_deliveries_and_events_and_a_burst_is_one_pump_and_one_activation_per_agent():
    rig = Rig()
    rig.llm.default = lambda call: sleep_resp()
    sup, agents = make_supervisor(rig, tick_interval=10.0)  # keep the tick loop out of the way
    sup.start()
    await settle()
    pumps: list[int] = []
    original = sup.pump

    def counting(final: bool = False) -> None:
        pumps.append(1)
        original(final)

    sup.pump = counting  # type: ignore[method-assign]
    for n in range(10):
        rig.world.push("a1", normal("message.new", label=n))
    rig.world.push("a2", normal("message.new", label="x"))
    rig.world.emit("message.committed", seq=1, content="hi")
    rig.world.emit("channel.updated", channel={"id": "pub"})
    await settle()
    assert len(pumps) == 1  # thirteen notifications in one loop turn cost one pump
    assert rig.world.notify_calls >= 13
    await wait_until(lambda: agents["a1"].stats["runs"] == 1 and agents["a2"].stats["runs"] == 1)
    assert [e.type for e in rig.sink.events if e.type not in ("agent.status", "agent.metrics")] == ["message.committed", "channel.updated"]
    assert rig.sink.of_type("message.committed")[0].data == {"seq": 1, "content": "hi"}
    # everything the pump handed over in one go is ONE activation of that agent, in the order it was committed
    assert labels(starts_of(rig, "a1")[0].reasons) == list(range(10)) and len(starts_of(rig, "a1")) == 1
    assert labels(starts_of(rig, "a2")[0].reasons) == ["x"] and agents["a1"].stats["activations"] == 1
    assert starts_of(rig, MODERATOR) == []  # nobody else was woken
    await sup.stop()


@async_test()
async def test_deliveries_reach_the_right_agent_in_commit_order_and_unknown_targets_are_dropped():
    rig = Rig()
    rig.llm.default = lambda call: sleep_resp()
    sup, agents = make_supervisor(rig, tick_interval=10.0)
    sup.start()
    rig.world.push("a1", normal("message.new", label=1))
    rig.world.push("nobody", normal("message.new", label="lost"))
    rig.world.push("a1", normal("message.new", label=2))
    rig.world.push("a2", normal("message.new", label=3))
    await wait_until(lambda: agents["a1"].stats["runs"] == 1 and agents["a2"].stats["runs"] == 1)
    assert labels(starts_of(rig, "a1")[0].reasons) == [1, 2] and labels(starts_of(rig, "a2")[0].reasons) == [3]
    await sup.stop()


@async_test()
async def test_every_delivery_wakes_its_agent_at_once_there_is_no_buffering_between_world_and_agent():
    rig = Rig()
    rig.llm.default = lambda call: sleep_resp()
    sup, agents = make_supervisor(rig, tick_interval=10.0)
    sup.start()
    await settle()
    loop = asyncio.get_running_loop()
    started = loop.time()
    rig.world.push("a1", normal("message.new", label="now"))  # an ordinary message: no urgency, no debounce
    await wait_until(lambda: rig.llm.calls)
    assert rig.llm.calls[0].started - started < 0.1
    await sup.stop()


@async_test()
async def test_a_world_state_change_is_published_as_an_event():
    rig = Rig()
    sup, agents = make_supervisor(rig, tick_interval=10.0)
    sup.start()
    rig.world.set_running(False, "hold:test")
    await wait_until(lambda: rig.sink.of_type("game.status"))
    assert rig.sink.of_type("game.status")[0].data["reason"] == "hold:test"
    await sup.stop()


@async_test()
async def test_engine_notices_go_to_the_orchestrator_hook_and_not_to_an_agent():
    rig = Rig()
    got = []
    sup, agents = make_supervisor(rig, on_engine=got.append, tick_interval=10.0)
    sup.start()
    notice = env("hold_game", kind="notice", reason="moderator asked")
    rig.world.push(ENGINE_ID, notice)
    await wait_until(lambda: got)
    await settle()
    assert got == [notice]
    assert all(a.is_quiet() for a in agents.values()) and rig.llm.calls == []
    await sup.stop()


@async_test()
async def test_an_engine_notice_without_a_hook_is_dropped_and_a_failing_hook_is_isolated():
    rig = Rig()
    rig.llm.default = lambda call: sleep_resp()

    def boom(envelope):
        raise RuntimeError("orchestrator bug")

    sup, agents = make_supervisor(rig, on_engine=boom, tick_interval=10.0)
    sup.start()
    rig.world.push(ENGINE_ID, env("hold_game", kind="notice"))
    rig.world.push("a1", normal("message.new", label="after"))  # delivered in the same pump
    rig.world.emit("message.committed", seq=1)
    await wait_until(lambda: agents["a1"].stats["runs"] == 1 and rig.sink.of_type("message.committed"))
    await sup.stop()

    quiet_rig = Rig()
    quiet_sup, _ = make_supervisor(quiet_rig, on_engine=None, tick_interval=10.0)
    quiet_sup.start()
    quiet_rig.world.push(ENGINE_ID, env("end_game", kind="notice"))
    await settle()
    await quiet_sup.stop()


@async_test()
async def test_a_failing_event_sink_does_not_stop_the_pump():
    rig = Rig()
    published = []

    class FlakySink:
        def publish(self, event):
            if event.type == "boom":
                raise RuntimeError("sink down")
            published.append(event.type)

    sup = Supervisor(rig.world, [rig.agent("a1")], FlakySink(), config=fast_supervisor_config(tick_interval=10.0))
    sup.start()
    rig.world.emit("boom")
    rig.world.emit("fine")
    await wait_until(lambda: "fine" in published)
    await sup.stop()


@async_test()
async def test_what_the_world_produces_after_stop_is_still_published_but_not_delivered_to_agents():
    # e.g. the orchestrator pauses: supervisor.stop(), THEN world.set_running(False) -> a game.status event
    rig = Rig()
    got = []
    sup, agents = make_supervisor(rig, on_engine=got.append, tick_interval=10.0)
    sup.start()
    await sup.stop()
    rig.world.set_running(False, "pause")
    rig.world.push("a1", urgent("floor.granted", channel_id="pub"))  # the agents are stopped: dropped
    rig.world.push(ENGINE_ID, env("end_game", kind="notice", winner="x"))
    await wait_until(lambda: rig.sink.of_type("game.status") and got)  # no flush() needed
    assert rig.sink.of_type("game.status")[0].data["reason"] == "pause"
    assert [e.payload["event"] for e in got] == ["end_game"]
    assert agents["a1"].pending == [] and rig.llm.calls == [] and others() == []


@async_test()
async def test_flush_publishes_pending_events_on_demand():
    rig = Rig()
    sup, agents = make_supervisor(rig, tick_interval=10.0)
    sup.start()
    await sup.stop()
    rig.world.emit("game.status", status="finished")
    sup.flush()  # synchronous: no need to wait for the loop turn
    assert len(rig.sink.of_type("game.status")) == 1
    await settle()
    assert len(rig.sink.of_type("game.status")) == 1  # and not published twice


# ============================================================================= timers & ticks


@async_test()
async def test_the_world_is_ticked_regularly_and_what_a_tick_produces_is_pumped():
    rig = Rig()
    rig.llm.script("a1", sleep_resp())
    fired = []

    def on_tick():
        if rig.world.tick_count == 3 and not fired:
            fired.append(1)
            rig.world.push("a1", urgent("floor.revoked", channel_id="pub"))  # e.g. a token timed out

    rig.world.on_tick = on_tick
    sup, agents = make_supervisor(rig)
    sup.start()
    await wait_until(lambda: rig.world.tick_count >= 5)
    await wait_until(lambda: agents["a1"].stats["runs"] == 1)
    await sup.stop()
    count = rig.world.tick_count
    await asyncio.sleep(0.1)
    assert rig.world.tick_count == count  # polling stopped with the supervisor


@async_test()
async def test_the_game_clock_drives_world_timers_the_world_checks_on_each_tick():
    rig = Rig()
    rig.llm.script("a1", sleep_resp())
    rig.world.deadline = 5.0

    def on_tick():
        if rig.world.clock.now() >= 5.0 and rig.world.deadline is not None:
            rig.world.deadline = None
            rig.world.push("a1", urgent("floor.revoked", kind="floor", channel_id="pub"))

    rig.world.on_tick = on_tick
    sup, agents = make_supervisor(rig, watchdog_s=100.0)
    sup.start()
    await asyncio.sleep(0.1)
    assert agents["a1"].stats["runs"] == 0  # game time has not reached the deadline yet
    rig.world.clock.advance(5.0)
    await wait_until(lambda: agents["a1"].stats["runs"] == 1)
    await sup.stop()


@async_test()
async def test_a_failing_tick_is_reported_once_and_polling_continues():
    rig = Rig()

    def on_tick():
        if rig.world.tick_count <= 2:
            raise RuntimeError("tick bug")

    rig.world.on_tick = on_tick
    sup, _ = make_supervisor(rig)
    sup.start()
    await wait_until(lambda: rig.world.tick_count >= 5)
    await sup.stop()
    assert len(rig.sink.of_type("error")) == 1  # not once per failing tick


def timed_rig(start: float = 0.0) -> Rig:
    return Rig(world=FakeWorld(ManualClock(start)))


@async_test()
async def test_an_agent_that_chose_to_sleep_is_woken_by_the_tick_loop_when_its_timer_runs_out():
    rig = timed_rig()
    rig.llm.script("a1", sleep_resp(30), speak_resp(), sleep_resp())
    sup, agents = make_supervisor(rig, watchdog_s=100.0)
    sup.start({"a1": [normal("message.new", label="m1")]})
    await wait_until(lambda: agents["a1"].stats["runs"] == 1)
    assert agents["a1"].sleep_until == 30.0 and rig.sink.status_events("a1")[-1] == ("sleeping", 30.0)
    rig.world.clock.advance(29)
    await asyncio.sleep(0.15)  # many ticks: not yet
    assert agents["a1"].stats["runs"] == 1 and len(rig.llm.calls) == 1
    rig.world.clock.advance(1)
    await wait_until(lambda: agents["a1"].stats["runs"] == 2)
    assert reasons_of(rig, "a1") == ["message.new", "timer.elapsed"]
    assert [i.type for i in rig.world.submitted] == ["speak"]  # it acted on waking
    await asyncio.sleep(0.1)
    assert len(rig.llm.calls) == 3  # one timer, one wake-up
    await sup.stop()


@async_test()
async def test_agent_timers_stand_still_while_the_game_clock_is_paused():
    rig = timed_rig()
    rig.llm.script("a1", sleep_resp(10), sleep_resp())
    sup, agents = make_supervisor(rig, watchdog_s=100.0)
    sup.start({"a1": [normal("message.new", label="m1")]})
    await wait_until(lambda: agents["a1"].stats["runs"] == 1)
    clock = rig.world.clock
    clock.advance(4)
    clock.pause()
    clock.advance(1000)  # wall time, not game time
    await asyncio.sleep(0.15)
    assert agents["a1"].stats["runs"] == 1 and clock.now() == pytest.approx(4.0)
    clock.resume()
    clock.advance(6)
    await wait_until(lambda: agents["a1"].stats["runs"] == 2)
    await sup.stop()


@async_test()
async def test_a_timer_keeps_its_place_across_a_pause_and_a_new_supervisor():
    # the orchestrator's pause -> resume: stop the supervisor, freeze the clock, build new agents, start again
    rig = timed_rig()
    rig.llm.script("a1", sleep_resp(30), sleep_resp())
    sup, agents = make_supervisor(rig, watchdog_s=100.0)
    sup.start({"a1": [normal("message.new", label="m1")]})
    await wait_until(lambda: agents["a1"].stats["runs"] == 1)
    rig.world.clock.advance(12)
    await sup.stop()
    rig.world.clock.pause()
    rig.world.clock.advance(500)  # the game stays paused for a long time
    rig.world.clock.resume()

    sup2, agents2 = make_supervisor(rig, watchdog_s=100.0)  # brand new agents, the same book
    sup2.start()
    assert agents2["a1"].sleep_until == 30.0 and rig.sink.status_events("a1")[-1] == ("sleeping", 30.0)
    await asyncio.sleep(0.1)
    assert agents2["a1"].stats["runs"] == 0  # 18 game seconds to go
    rig.world.clock.advance(18)
    await wait_until(lambda: agents2["a1"].stats["runs"] == 1)
    assert reasons_of(rig, "a1") == ["message.new", "timer.elapsed"]
    await sup2.stop()


@async_test()
async def test_a_world_wake_up_in_the_turn_a_timer_runs_out_is_one_activation_not_two():
    rig = timed_rig()
    rig.llm.script("a1", sleep_resp(30), sleep_resp())
    pushed = []

    def on_tick():
        if rig.world.clock.now() >= 30 and not pushed:
            pushed.append(1)
            rig.world.push("a1", normal("message.new", label="world"))

    rig.world.on_tick = on_tick
    sup, agents = make_supervisor(rig, watchdog_s=100.0)
    sup.start({"a1": [normal("message.new", label="m1")]})
    await wait_until(lambda: agents["a1"].stats["runs"] == 1)
    rig.world.clock.advance(30)
    await wait_until(lambda: agents["a1"].stats["runs"] == 2)
    await asyncio.sleep(0.1)
    assert agents["a1"].stats["activations"] == 2 and reasons_of(rig, "a1") == ["message.new", "message.new"]  # no timer notice
    await sup.stop()


@async_test()
async def test_poll_timers_wakes_only_the_agents_that_are_due_and_survives_a_broken_one():
    rig = timed_rig()
    rig.llm.default = lambda call: sleep_resp(10)
    sup, agents = make_supervisor(rig, ids=("a1", "a2", "a3"), tick_interval=10.0, watchdog_s=100.0)
    sup.start({"a1": [normal("message.new", label=1)], "a2": [normal("message.new", label=2)]})
    await wait_until(lambda: agents["a1"].stats["runs"] == 1 and agents["a2"].stats["runs"] == 1)
    rig.world.clock.advance(10)

    def broken(now):
        raise RuntimeError("timer bug")

    agents["a1"].timer_due = broken  # type: ignore[method-assign]
    assert sup.poll_timers() == 1  # a2 woke although a1's check blew up; a3 never slept with a timer
    await wait_until(lambda: agents["a2"].stats["runs"] == 2)
    assert agents["a1"].stats["runs"] == 1 and agents["a3"].stats["runs"] == 0
    await sup.stop()


# =============================================================================== the watchdog


@async_test()
async def test_a_silent_running_game_wakes_the_moderator_and_nobody_else():
    rig = Rig()
    rig.llm.default = lambda call: sleep_resp()
    sup, agents = make_supervisor(rig)
    sup.start()
    await wait_until(lambda: starts_of(rig, MODERATOR), timeout=3)
    first = starts_of(rig, MODERATOR)[0]
    (reason,) = first.reasons
    assert reason.kind == "notice" and reason.priority == "urgent" and reason.sender == ENGINE_ID
    assert reason.payload["event"] == "watchdog"
    assert reason.payload["attempt"] == 1 and reason.payload["max_attempts"] == 2
    assert reason.payload["idle_s"] >= 0.1
    assert starts_of(rig, "a1") == [] and starts_of(rig, "a2") == []  # only the moderator is woken
    assert rig.trace.of("watchdog", MODERATOR)[0]["action"] == "wake"
    await sup.stop()


@async_test()
async def test_the_watchdog_warns_once_after_the_fruitless_wakes_keeps_trying_and_never_pauses_the_game():
    rig = Rig()
    rig.llm.default = lambda call: sleep_resp()  # the moderator wakes up and does nothing
    stalls: list[str | None] = []
    sup, agents = make_supervisor(rig, on_stall=stalls.append)
    sup.start()
    await wait_until(lambda: stalls, timeout=4)
    assert len(stalls) == 1 and "对局静止" in stalls[0] and "2" in stalls[0] and sup.stalled
    woken = [r.reasons[0].payload["attempt"] for r in starts_of(rig, MODERATOR) + injects_of(rig, MODERATOR)]
    assert sorted(woken)[:2] == [1, 2]
    # the warning is not a verdict: the moderator is still poked, the warning is not repeated, nothing is paused
    await wait_until(lambda: len(starts_of(rig, MODERATOR) + injects_of(rig, MODERATOR)) >= 4, timeout=4)
    assert len(stalls) == 1 and rig.world.running is True and sup.running
    assert [r["action"] for r in rig.trace.of("watchdog", MODERATOR)][:3] == ["wake", "wake", "stall"]
    assert rig.sink.of_type("game.status") == []  # the supervisor never touched the game status
    await sup.stop()


@async_test()
async def test_progress_after_a_stall_clears_it_with_on_stall_none():
    rig = Rig()
    rig.llm.default = lambda call: sleep_resp()
    stalls: list[str | None] = []
    sup, agents = make_supervisor(rig, on_stall=stalls.append)
    sup.start()
    await wait_until(lambda: len(stalls) == 1 and isinstance(stalls[0], str), timeout=4)
    rig.world.bump()  # the ledger moves again
    await wait_until(lambda: len(stalls) == 2, timeout=2)
    assert stalls[1] is None and sup.stalled is False
    await sup.stop()
    assert any(r["action"] == "recovered" for r in rig.trace.of("watchdog", MODERATOR))  # and it is in the trace


@async_test()
async def test_the_watchdog_still_works_without_an_on_stall_hook():
    rig = Rig()
    rig.llm.default = lambda call: sleep_resp()
    sup, _ = make_supervisor(rig)
    sup.start()
    await wait_until(lambda: sup.stalled, timeout=4)
    await sup.stop()


@async_test()
async def test_a_failing_on_stall_hook_does_not_kill_the_watchdog():
    rig = Rig()
    rig.llm.default = lambda call: sleep_resp()
    calls: list[str | None] = []

    def boom(reason):
        calls.append(reason)
        raise RuntimeError("banner bug")

    sup, _ = make_supervisor(rig, on_stall=boom)
    sup.start()
    await wait_until(lambda: calls, timeout=4)
    rig.world.bump()
    await wait_until(lambda: len(calls) == 2, timeout=2)  # the clearing call still happens
    await sup.stop()


@async_test()
async def test_ledger_progress_keeps_the_watchdog_quiet_and_resets_its_counter():
    rig = Rig()
    rig.llm.default = lambda call: sleep_resp()
    sup, agents = make_supervisor(rig, max_watchdog_wakes=3, watchdog_s=0.3)

    async def keep_busy(seconds: float) -> None:
        end = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < end:
            rig.world.bump()
            await asyncio.sleep(0.03)

    sup.start()
    await keep_busy(0.7)  # the ledger advances much faster than ``watchdog_s``: never silent
    assert starts_of(rig, MODERATOR) == []
    await wait_until(lambda: len(starts_of(rig, MODERATOR)) == 1, timeout=3)  # silence at last: attempt 1
    assert starts_of(rig, MODERATOR)[0].reasons[0].payload["attempt"] == 1
    rig.world.bump()  # progress after the first wake (e.g. the moderator did something)
    await wait_until(lambda: len(injects_of(rig, MODERATOR)) == 1, timeout=3)
    assert injects_of(rig, MODERATOR)[0].reasons[0].payload["attempt"] == 1  # the counter started over
    assert not sup.stalled
    await sup.stop()


@async_test()
async def test_the_watchdog_does_not_fire_while_a_world_timer_is_pending():
    rig = Rig()
    rig.llm.default = lambda call: sleep_resp()
    rig.world.deadline = 99.0  # something will happen by itself
    sup, _ = make_supervisor(rig)
    sup.start()
    await asyncio.sleep(0.5)
    assert starts_of(rig, MODERATOR) == []
    rig.world.deadline = None  # the timer is gone: now the game is silent
    await wait_until(lambda: starts_of(rig, MODERATOR), timeout=3)
    await sup.stop()


@async_test()
async def test_the_watchdog_respects_a_timed_sleep_of_the_moderator_but_not_those_of_the_players():
    rig = timed_rig()
    rig.llm.script(MODERATOR, sleep_resp(1000))  # the moderator decided to wait a long (game) time
    rig.llm.script("a1", sleep_resp(1000))
    rig.llm.default = lambda call: sleep_resp()
    sup, agents = make_supervisor(rig)
    sup.start({MODERATOR: [urgent("game.started", kind="notice")], "a1": [normal("message.new", label="x")]})
    await wait_until(lambda: agents[MODERATOR].stats["runs"] == 1 and agents["a1"].stats["runs"] == 1)
    await asyncio.sleep(0.6)  # four watchdog windows
    assert len(starts_of(rig, MODERATOR)) == 1 and injects_of(rig, MODERATOR) == []  # that is its plan, not a stall
    # ... but once the moderator has no timer any more the silence counts, although a player still has one
    rig.book.get(MODERATOR).sleep_until = None
    assert agents["a1"].has_timer
    await wait_until(lambda: injects_of(rig, MODERATOR), timeout=3)
    assert injects_of(rig, MODERATOR)[0].reasons[0].payload["event"] == "watchdog"
    await sup.stop()


@async_test()
async def test_the_watchdog_does_not_fire_while_the_game_is_paused():
    rig = Rig()
    rig.llm.default = lambda call: sleep_resp()
    rig.world.running = False
    sup, _ = make_supervisor(rig)
    sup.start()
    await asyncio.sleep(0.8)  # five times the watchdog period, all of it paused
    assert starts_of(rig, MODERATOR) == []
    rig.world.running = True
    await wait_until(lambda: starts_of(rig, MODERATOR), timeout=3)
    # the clock of the silence started when the game resumed, not when it was paused (that would be >= 0.95)
    assert starts_of(rig, MODERATOR)[0].reasons[0].payload["idle_s"] < 0.6
    await sup.stop()


@async_test()
async def test_the_watchdog_does_not_fire_while_an_agent_is_busy_or_has_work_queued():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=sleep_resp()))
    rig.llm.default = lambda call: sleep_resp()
    sup, agents = make_supervisor(rig)
    sup.start({"a1": [urgent("request.opened", kind="request", request_id="r1")]})
    await wait_until(lambda: len(rig.llm.calls) == 1)
    await asyncio.sleep(0.5)  # a1 has been "thinking" for three times the watchdog period
    assert starts_of(rig, MODERATOR) == []
    hold.set()
    await wait_until(lambda: starts_of(rig, MODERATOR), timeout=3)  # quiet again -> the silence is detected
    await sup.stop()


# =============================================================================== metrics


@async_test()
async def test_agent_metrics_are_published_when_they_change_and_not_otherwise():
    rig = Rig()
    rig.llm.script(
        "a1",
        llm_resp(SPEAK, thought="hmm", prompt_tokens=100, completion_tokens=10, cache_read_tokens=400),
        sleep_resp(prompt_tokens=50, completion_tokens=5),
    )
    sup, agents = make_supervisor(rig, watchdog_s=100.0)
    sup.start({"a1": [urgent("game.started", kind="notice")]})
    await wait_until(lambda: agents["a1"].stats["runs"] == 1)
    await wait_until(lambda: any(e.data["calls"] == 2 for e in rig.sink.of_type("agent.metrics")), timeout=2)
    await asyncio.sleep(0.2)  # nothing changes any more
    seen = len(rig.sink.of_type("agent.metrics"))
    await asyncio.sleep(0.2)
    assert len(rig.sink.of_type("agent.metrics")) == seen
    await sup.stop()

    metrics = [e.data for e in rig.sink.of_type("agent.metrics")]
    assert {m["id"] for m in metrics} == {"a1"}  # agents that did nothing publish nothing
    last = metrics[-1]
    assert last["calls"] == 2 and last["prompt_tokens"] == 150 and last["completion_tokens"] == 15
    assert last["cache_read_tokens"] == 400 and last["activations"] == 1
    assert last["tool_calls"] == 2 and last["thinking_rounds"] == 1 and last["rejections"] == 0
    assert last["cache_hit_rate"] == pytest.approx(400 / 550)
    assert set(last) == {
        "id", "activations", "calls", "prompt_tokens", "cache_read_tokens", "cache_write_tokens", "completion_tokens",
        "cost_usd", "tool_calls", "thinking_rounds", "rejections", "cache_hit_rate",
    }
    for earlier, later in zip(metrics, metrics[1:]):
        assert earlier != later  # never the same numbers twice in a row


@async_test()
async def test_agent_metrics_are_throttled_per_interval():
    rig = Rig()
    rig.llm.default = lambda call: sleep_resp()
    sup, agents = make_supervisor(rig, metrics_interval_s=0.25, watchdog_s=100.0)
    sup.start()
    loop = asyncio.get_running_loop()
    started = loop.time()
    for n in range(40):  # 40 activations in about a second: lots of changes
        agents["a1"].deliver(normal("message.new", label=n))
        await asyncio.sleep(0.025)
    elapsed = loop.time() - started
    await sup.stop()  # publishes the final numbers
    count = len([e for e in rig.sink.of_type("agent.metrics") if e.data["id"] == "a1"])
    assert 1 <= count <= elapsed / 0.25 + 2  # at most one per interval (+ the final one)


# =============================================================================== integration


@async_test()
async def test_one_failing_agent_does_not_disturb_the_others():
    rig = Rig()
    rig.llm.script("a1", RuntimeError("down"), RuntimeError("down"), RuntimeError("down"))
    rig.llm.script("a2", llm_resp(SPEAK), sleep_resp())
    sup, agents = make_supervisor(rig, watchdog_s=100.0)
    sup.start({"a1": [normal("message.new", label=1)], "a2": [normal("message.new", label=2)]})
    await wait_until(lambda: agents["a2"].stats["runs"] == 1 and agents["a1"].stats["gave_up"] == 1, timeout=3)
    await sup.stop()
    assert [i.actor_id for i in rig.world.submitted] == ["a2"]
    assert [e.data["agent_id"] for e in rig.sink.of_type("error")] == ["a1"]


@async_test()
async def test_a_whole_exchange_flows_through_world_pump_agents_and_back():
    rig = Rig()

    def on_submit(intent):
        rig.world.seq += 1
        seq = rig.world.seq
        if intent.type == "announce":
            rig.world.push("a1", urgent("floor.granted", channel_id="pub", seq=seq))
            rig.world.emit("message.committed", seq=seq, **{"from": MODERATOR})
        elif intent.type == "speak" and intent.actor_id == "a1":
            rig.world.push("a2", normal("message.new", channel_id="pub", seq=seq, label="from-a1"))
            rig.world.push(MODERATOR, normal("message.new", channel_id="pub", seq=seq, label="from-a1"))
            rig.world.emit("message.committed", seq=seq, **{"from": "a1"})
        elif intent.type == "speak" and intent.actor_id == "a2":
            rig.world.push(MODERATOR, urgent("floor.completed", kind="floor", channel_id="pub", seq=seq))
            rig.world.emit("message.committed", seq=seq, **{"from": "a2"})
        return Receipt.accept(seq=seq)

    rig.world.on_submit = on_submit
    rig.brain.nudge_text = None
    rig.llm.script(MODERATOR, llm_resp(("announce", {"channel_id": "pub", "template_id": "t"})))
    rig.llm.script("a1", llm_resp(SPEAK))
    rig.llm.script("a2", llm_resp(SPEAK))
    rig.llm.default = lambda call: sleep_resp()
    sup, agents = make_supervisor(rig, watchdog_s=100.0)
    sup.start({MODERATOR: [urgent("game.started", kind="notice")]})
    await wait_until(lambda: [i.type for i in rig.world.submitted].count("speak") == 2, timeout=5)
    await wait_until(lambda: all(a.is_quiet() for a in agents.values()) and "floor.completed" in reasons_of(rig, MODERATOR))
    await sup.stop()

    assert [(i.type, i.actor_id) for i in rig.world.submitted] == [("announce", MODERATOR), ("speak", "a1"), ("speak", "a2")]
    assert reasons_of(rig, "a1")[0] == "floor.granted"
    assert reasons_of(rig, "a2")[0] == "message.new"  # an ordinary message woke it at once, no debounce
    assert reasons_of(rig, MODERATOR)[0] == "game.started" and "floor.completed" in reasons_of(rig, MODERATOR)
    assert [e.data["from"] for e in rig.sink.of_type("message.committed")] == [MODERATOR, "a1", "a2"]
    assert rig.sink.of_type("error") == []
    assert all(a.stats["failures"] == 0 for a in agents.values()) and all(a.status == "sleeping" for a in agents.values())
    assert all(c.paired for c in rig.llm.calls)
