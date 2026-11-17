"""Supervisor: start / stop, the pump, ENGINE deliveries, timer polling, watchdog, metrics, crashes."""

from __future__ import annotations

import asyncio

import pytest

from app.runtime.agent import Agent
from app.runtime.supervisor import Supervisor, SupervisorConfig
from app.world.types import ENGINE_ID, Receipt

from .fakes import (
    MODERATOR,
    Rig,
    Step,
    ambient,
    async_test,
    env,
    llm_resp,
    normal,
    settle,
    urgent,
    wait_resp,
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
        "stop_grace_s": 1.0,
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
    )
    return sup, agents


def starts_of(rig: Rig, agent_id: str):
    return [ctx for ctx in rig.brain.starts if ctx.agent_id == agent_id]


def events(envelopes):
    return [e.payload["event"] for e in envelopes]


def others() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]


# =============================================================================== lifecycle


@async_test()
async def test_start_spawns_every_actor_and_stop_leaves_no_task_behind():
    rig = Rig()
    sup, agents = make_supervisor(rig)
    assert not sup.running
    await sup.start()  # start() is synchronous, but awaiting its result is allowed too
    assert sup.running
    names = {t.get_name() for t in others()}
    assert {"actor:moderator", "actor:a1", "actor:a2", "supervisor:tick", "supervisor:watchdog", "supervisor:metrics"} <= names
    await settle()
    assert all(a.status == "idle" for a in agents.values())
    assert rig.world.notifier is not None
    with pytest.raises(RuntimeError):
        sup.start()  # already running

    await sup.stop()
    assert others() == []  # nothing leaked: actors, loops, workers
    assert not sup.running
    assert all(a.status == "stopped" for a in agents.values())
    for agent_id in agents:
        assert rig.sink.statuses(agent_id) == ["idle", "stopped"]  # one ``stopped`` event per agent
    await sup.stop()  # idempotent


@async_test()
async def test_a_stopped_supervisor_can_be_started_again():
    rig = Rig()
    rig.llm.script("a1", wait_resp(), wait_resp())
    sup, agents = make_supervisor(rig)
    sup.start({"a1": [urgent("game.started", kind="notice")]})
    await wait_until(lambda: agents["a1"].stats["runs"] == 1)
    await sup.stop()
    assert others() == []
    sup.start({"a1": [urgent("game.resumed", kind="notice")]})  # pause -> resume
    await wait_until(lambda: agents["a1"].stats["runs"] == 2)
    await sup.stop()
    assert [events(c.reasons) for c in starts_of(rig, "a1")] == [["game.started"], ["game.resumed"]]


@async_test()
async def test_stop_right_after_start_does_not_hang_or_leak():
    rig = Rig()
    sup, agents = make_supervisor(rig)
    sup.start({"a1": [urgent("game.started", kind="notice")]})
    await sup.stop()  # the actor tasks have not even had their first turn
    assert others() == []
    assert all(a.status == "stopped" for a in agents.values())


@async_test()
async def test_concurrent_stop_calls_wait_for_the_same_shutdown():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=wait_resp()))
    sup, agents = make_supervisor(rig)
    sup.start({"a1": [urgent("game.started", kind="notice")]})
    await wait_until(lambda: len(rig.llm.calls) == 1)
    await asyncio.gather(sup.stop(), sup.stop(), sup.stop())
    assert others() == [] and rig.llm.cancelled == ["a1"]


@async_test()
async def test_stop_cancels_in_flight_workers_of_every_agent():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=wait_resp()))
    rig.llm.script("a2", Step(hold=hold, resp=wait_resp()))
    rig.llm.script(MODERATOR, Step(hold=hold, resp=wait_resp()))
    sup, agents = make_supervisor(rig)
    sup.start({aid: [urgent("game.started", kind="notice")] for aid in agents})
    await wait_until(lambda: len(rig.llm.calls) == 3)
    await sup.stop()
    assert sorted(rig.llm.cancelled) == sorted([MODERATOR, "a1", "a2"])
    assert others() == []
    assert rig.brain.executed == []  # nothing was committed on the way down


@async_test()
async def test_initial_wakes_are_delivered_and_open_work_is_redelivered_and_pumped_at_once():
    rig = Rig()
    rig.llm.script(MODERATOR, wait_resp())
    rig.llm.script("a1", wait_resp())
    rig.world.on_redeliver = lambda: rig.world.push("a1", urgent("request.opened", kind="request", request_id="r1"))
    sup, agents = make_supervisor(rig)
    sup.start({MODERATOR: [urgent("game.started", kind="notice")], "ghost": [urgent("x")]})  # unknown ids are ignored
    assert rig.world.redeliver_count == 1
    assert len(agents["a1"].mailbox) == 1  # already pumped when start() returns
    assert len(agents[MODERATOR].mailbox) == 1
    await wait_until(lambda: agents[MODERATOR].stats["runs"] == 1 and agents["a1"].stats["runs"] == 1)
    await sup.stop()
    assert events(starts_of(rig, MODERATOR)[0].reasons) == ["game.started"]
    assert events(starts_of(rig, "a1")[0].reasons) == ["request.opened"]


@async_test()
async def test_agents_may_be_given_as_a_plain_list_and_deliver_reports_unknown_targets():
    rig = Rig()
    rig.llm.script("a1", wait_resp())
    a1 = rig.agent("a1")
    sup = Supervisor(rig.world, [a1], rig.sink, config=fast_supervisor_config())
    sup.start()
    assert sup.deliver("a1", urgent("game.resumed", kind="notice")) is True
    assert sup.deliver("nobody", urgent("x")) is False
    await wait_until(lambda: a1.stats["runs"] == 1)
    await sup.stop()


# =============================================================================== the pump


@async_test()
async def test_the_pump_routes_deliveries_to_mailboxes_and_events_to_the_sink_once_per_burst():
    rig = Rig()
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
        rig.world.push("a1", ambient("message.new", label=n))
    rig.world.push("a2", ambient("message.new", label="x"))
    rig.world.emit("message.committed", seq=1, content="hi")
    rig.world.emit("channel.updated", channel={"id": "pub"})
    await settle()
    assert len(pumps) == 1  # eleven notifications in one loop turn cost one pump
    assert rig.world.notify_calls >= 13
    await wait_until(lambda: len(agents["a1"].pending) == 10 and len(agents["a2"].pending) == 1)
    assert [e.type for e in rig.sink.events if e.type != "agent.status"] == ["message.committed", "channel.updated"]
    assert rig.sink.of_type("message.committed")[0].data == {"seq": 1, "content": "hi"}
    await sup.stop()


@async_test()
async def test_deliveries_reach_the_right_agent_in_commit_order_and_unknown_targets_are_dropped():
    rig = Rig()
    sup, agents = make_supervisor(rig, tick_interval=10.0)
    sup.start()
    rig.world.push("a1", ambient("message.new", label=1))
    rig.world.push("nobody", ambient("message.new", label="lost"))
    rig.world.push("a1", ambient("message.new", label=2))
    rig.world.push("a2", ambient("message.new", label=3))
    await wait_until(lambda: len(agents["a1"].pending) == 2)
    assert [e.payload["label"] for e in agents["a1"].pending] == [1, 2]
    assert [e.payload["label"] for e in agents["a2"].pending] == [3]
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
    assert got == [notice]
    assert all(len(a.mailbox) == 0 and a.pending == [] for a in agents.values())
    await sup.stop()


@async_test()
async def test_an_engine_notice_without_a_hook_is_dropped_and_a_failing_hook_is_isolated():
    rig = Rig()

    def boom(envelope):
        raise RuntimeError("orchestrator bug")

    sup, agents = make_supervisor(rig, on_engine=boom, tick_interval=10.0)
    sup.start()
    rig.world.push(ENGINE_ID, env("hold_game", kind="notice"))
    rig.world.push("a1", ambient("message.new", label="after"))  # delivered in the same pump
    rig.world.emit("message.committed", seq=1)
    await wait_until(lambda: agents["a1"].pending and rig.sink.of_type("message.committed"))
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
    assert len(agents["a1"].mailbox) == 0 and agents["a1"].pending == []
    assert others() == []


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


# =============================================================================== timers


@async_test()
async def test_the_world_is_ticked_regularly_and_what_a_tick_produces_is_pumped():
    rig = Rig()
    rig.llm.script("a1", wait_resp())
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
async def test_the_game_clock_drives_timers_the_world_checks_on_each_tick():
    rig = Rig()
    rig.llm.script("a1", wait_resp())
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


# =============================================================================== watchdog


@async_test()
async def test_a_silent_running_game_wakes_the_moderator():
    rig = Rig()
    rig.llm.default = lambda call: wait_resp()
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
async def test_the_watchdog_escalates_to_on_stall_after_the_configured_number_of_fruitless_wakes():
    rig = Rig()
    rig.llm.default = lambda call: wait_resp()  # the moderator wakes up and does nothing
    stalls: list[str] = []
    sup, agents = make_supervisor(rig, on_stall=stalls.append)
    sup.start()
    await wait_until(lambda: stalls, timeout=4)
    notices = [c for c in starts_of(rig, MODERATOR) if events(c.reasons) == ["watchdog"]]
    assert [c.reasons[0].payload["attempt"] for c in notices] == [1, 2]
    assert len(stalls) == 1 and "对局静止" in stalls[0] and "2" in stalls[0]
    assert sup.stalled
    await asyncio.sleep(0.5)  # a stalled game is left alone: no more wake-ups, no more calls
    assert len(stalls) == 1 and len([c for c in starts_of(rig, MODERATOR)]) == 2
    assert [r["action"] for r in rig.trace.of("watchdog", MODERATOR)] == ["wake", "wake", "stall"]
    await sup.stop()


@async_test()
async def test_the_watchdog_still_works_without_an_on_stall_hook():
    rig = Rig()
    rig.llm.default = lambda call: wait_resp()
    sup, _ = make_supervisor(rig)
    sup.start()
    await wait_until(lambda: sup.stalled, timeout=4)
    await sup.stop()


@async_test()
async def test_ledger_progress_keeps_the_watchdog_quiet_and_resets_its_counter():
    rig = Rig()
    rig.llm.default = lambda call: wait_resp()
    sup, agents = make_supervisor(rig, max_watchdog_wakes=3)

    async def keep_busy(seconds: float) -> None:
        end = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < end:
            rig.world.bump()
            await asyncio.sleep(0.03)

    sup.start()
    await keep_busy(0.5)  # the ledger advances faster than ``watchdog_s``: never silent
    assert starts_of(rig, MODERATOR) == []
    await wait_until(lambda: len(starts_of(rig, MODERATOR)) == 1, timeout=3)  # silence at last: attempt 1
    assert starts_of(rig, MODERATOR)[0].reasons[0].payload["attempt"] == 1
    rig.world.bump()  # progress after the first wake (e.g. the moderator did something)
    await wait_until(lambda: len(starts_of(rig, MODERATOR)) == 2, timeout=3)
    assert starts_of(rig, MODERATOR)[1].reasons[0].payload["attempt"] == 1  # the counter started over
    assert not sup.stalled
    await sup.stop()


@async_test()
async def test_the_watchdog_does_not_fire_while_a_timer_is_pending():
    rig = Rig()
    rig.world.deadline = 99.0  # something will happen by itself
    sup, _ = make_supervisor(rig)
    sup.start()
    await asyncio.sleep(0.5)
    assert starts_of(rig, MODERATOR) == []
    rig.world.deadline = None  # the timer is gone: now the game is silent
    await wait_until(lambda: starts_of(rig, MODERATOR), timeout=3)
    await sup.stop()


@async_test()
async def test_the_watchdog_does_not_fire_while_the_game_is_paused():
    rig = Rig()
    rig.world.running = False
    sup, _ = make_supervisor(rig)
    sup.start()
    await asyncio.sleep(0.5)
    assert starts_of(rig, MODERATOR) == []
    rig.world.running = True
    await wait_until(lambda: starts_of(rig, MODERATOR), timeout=3)
    # the clock of the silence started when the game resumed, not when it was paused
    assert starts_of(rig, MODERATOR)[0].reasons[0].payload["idle_s"] < 0.45
    await sup.stop()


@async_test()
async def test_the_watchdog_does_not_fire_while_an_agent_is_busy_or_has_work_queued():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=wait_resp()))
    rig.llm.default = lambda call: wait_resp()
    sup, agents = make_supervisor(rig)
    sup.start({"a1": [urgent("request.opened", kind="request", request_id="r1")]})
    await wait_until(lambda: len(rig.llm.calls) == 1)
    await asyncio.sleep(0.5)  # a1 has been "thinking" for 3x the watchdog period
    assert starts_of(rig, MODERATOR) == []
    hold.set()
    await wait_until(lambda: starts_of(rig, MODERATOR), timeout=3)  # idle again -> silence is detected
    await sup.stop()


# =============================================================================== metrics


@async_test()
async def test_agent_metrics_are_published_when_they_change_and_not_otherwise():
    rig = Rig()
    rig.llm.script("a1", llm_resp(SPEAK, thought="hmm", prompt_tokens=100, completion_tokens=10), wait_resp(prompt_tokens=50, completion_tokens=5))
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
    assert last["tool_calls"] == 2 and last["thinking_rounds"] == 1 and last["rejections"] == 0
    assert set(last) == {"id", "calls", "prompt_tokens", "completion_tokens", "cost_usd", "tool_calls", "thinking_rounds", "rejections"}
    for earlier, later in zip(metrics, metrics[1:]):
        assert earlier != later  # never the same numbers twice in a row


@async_test()
async def test_agent_metrics_are_throttled_per_interval():
    rig = Rig()
    rig.llm.default = lambda call: wait_resp()
    sup, agents = make_supervisor(rig, metrics_interval_s=0.25, watchdog_s=100.0)
    sup.start()
    loop = asyncio.get_running_loop()
    started = loop.time()
    for n in range(40):  # 40 runs in about a second: lots of changes
        agents["a1"].deliver(urgent("floor.granted", channel_id="pub", label=n))
        await asyncio.sleep(0.025)
    elapsed = loop.time() - started
    await sup.stop()  # publishes the final numbers
    count = len([e for e in rig.sink.of_type("agent.metrics") if e.data["id"] == "a1"])
    assert 1 <= count <= elapsed / 0.25 + 2  # at most one per interval (+ the final one)


# =============================================================================== crashes & integration


@async_test()
async def test_a_crashed_actor_is_rebuilt_from_the_world_and_gets_its_open_work_back():
    rig = Rig()
    rig.llm.script("a1", wait_resp())
    sup, agents = make_supervisor(rig, tick_interval=10.0)
    a1 = agents["a1"]
    original = a1.mailbox.drain_arrival
    crashed = []

    def flaky():
        if not crashed:
            crashed.append(1)
            raise RuntimeError("actor bug")  # outside the per-envelope guard: the loop task dies
        return original()

    a1.mailbox.drain_arrival = flaky  # type: ignore[method-assign]
    sup.start()
    rig.world.on_redeliver = lambda: rig.world.push("a1", urgent("request.opened", kind="request", request_id="r1"))
    a1.deliver(ambient("message.new"))  # wakes the loop -> it crashes
    await wait_until(lambda: rig.world.redeliver_count == 2)  # start() + the restart
    await wait_until(lambda: a1.stats["runs"] == 1, timeout=3)
    assert events(starts_of(rig, "a1")[0].reasons) == ["request.opened"]
    await sup.stop()


@async_test()
async def test_an_actor_that_keeps_crashing_is_given_up_with_an_error_event():
    rig = Rig()
    sup, agents = make_supervisor(rig, max_actor_restarts=1, tick_interval=10.0)
    a1 = agents["a1"]

    def always_crash():
        raise RuntimeError("actor bug")

    a1.mailbox.drain_arrival = always_crash  # type: ignore[method-assign]
    # every (re)start redelivers something, so every incarnation of the actor crashes on its first turn
    rig.world.on_redeliver = lambda: rig.world.push("a1", ambient("message.new"))
    sup.start()
    await wait_until(lambda: rig.sink.of_type("error"), timeout=3)
    await asyncio.sleep(0.2)  # no third incarnation
    await sup.stop()
    (error,) = rig.sink.of_type("error")
    assert error.data["agent_id"] == "a1" and "a1" in error.data["message"]
    assert rig.world.redeliver_count == 2  # start() + the single allowed restart


@async_test()
async def test_a_whole_exchange_flows_through_world_pump_actors_workers_and_back():
    rig = Rig()
    moderator_wakes: list[str] = []

    def on_submit(intent):
        rig.world.seq += 1
        seq = rig.world.seq
        if intent.type == "announce":
            rig.world.push("a1", urgent("floor.granted", channel_id="pub", seq=seq))
            rig.world.emit("message.committed", seq=seq, **{"from": MODERATOR})
        elif intent.type == "speak" and intent.actor_id == "a1":
            rig.world.push("a2", normal("message.new", channel_id="pub", seq=seq, label="from-a1"))
            rig.world.push(MODERATOR, ambient("message.new", channel_id="pub", seq=seq))
            rig.world.emit("message.committed", seq=seq, **{"from": "a1"})
        elif intent.type == "speak" and intent.actor_id == "a2":
            rig.world.push(MODERATOR, urgent("floor.completed", kind="floor", channel_id="pub", seq=seq))
            rig.world.emit("message.committed", seq=seq, **{"from": "a2"})
        return Receipt.accept(seq=seq)

    rig.world.on_submit = on_submit
    rig.llm.script(MODERATOR, llm_resp(("announce", {"channel_id": "pub", "template_id": "t"})), wait_resp(), wait_resp())
    rig.llm.script("a1", llm_resp(SPEAK), wait_resp())
    rig.llm.script("a2", llm_resp(SPEAK), wait_resp())
    sup, agents = make_supervisor(rig, watchdog_s=100.0)
    sup.start({MODERATOR: [urgent("game.started", kind="notice")]})
    await wait_until(
        lambda: [i.type for i in rig.world.submitted].count("speak") == 2 and agents[MODERATOR].stats["runs"] == 2,
        timeout=5,
    )
    await wait_until(lambda: all(a.status == "idle" for a in agents.values()))
    await sup.stop()

    assert [(i.type, i.actor_id) for i in rig.world.submitted] == [("announce", MODERATOR), ("speak", "a1"), ("speak", "a2")]
    assert events(starts_of(rig, "a1")[0].reasons) == ["floor.granted"]
    assert events(starts_of(rig, "a2")[0].reasons) == ["message.new"]  # woken by a normal envelope, after the debounce
    assert events(starts_of(rig, MODERATOR)[1].reasons) == ["floor.completed"]
    assert [e.data["from"] for e in rig.sink.of_type("message.committed")] == [MODERATOR, "a1", "a2"]
    assert rig.sink.of_type("error") == []
    assert all(a.stats["failures"] == 0 for a in agents.values())
