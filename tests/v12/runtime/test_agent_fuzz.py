"""Randomised stress of the activation / run state machine (v0.12.1): whatever arrives, however slow or flaky the
model is, and whenever timers run out, the invariants of docs/v0.12-contracts.md section 10.2 must hold.

A model that answers after a random delay with a random mix of tool calls (speak, sleep with and without a timer,
compact_context), plain text and failures is driven by random envelopes (some of them interrupts); the game clock
advances at random and due timers fire.  Afterwards:

* never two model calls of one agent at the same time;
* a cancelled run never acts afterwards; every tool call belongs to a started run;
* every request is paired (no tool call without its result) and extends the previous request of the same
  conversation epoch byte for byte;
* every wake-up is accounted for: ended, cancelled or failed;
* every envelope that was delivered was told to the model (it is part of some opening / delta);
* nothing is left running or pending and the agent sleeps.
"""

from __future__ import annotations

import asyncio
import copy
import json
import random

import pytest

from app.llm.types import LLMResponse
from app.runtime.clock import ManualClock
from app.runtime.supervisor import Supervisor, SupervisorConfig

from .fakes import (
    MODERATOR,
    FakeLlm,
    FakeWorld,
    LlmCall,
    Rig,
    assert_paired,
    async_test,
    env,
    Step,
    llm_resp,
    normal,
    revoked,
    running_agent,
    urgent,
    wait_until,
)


class RandomLlm(FakeLlm):
    """Answers after a random delay with a random mix of tool calls, plain text and (rarely) errors."""

    def __init__(self, rng: random.Random, rig: Rig, error_rate: float = 0.04) -> None:
        super().__init__()
        self.rng = rng
        self.rig = rig
        self.error_rate = error_rate
        self.epochs: list[int] = []  # the conversation epoch each call was made under (parallel to ``calls``)

    async def complete(self, agent_id, messages, tools, *, max_tokens=None):  # type: ignore[override]
        loop = asyncio.get_running_loop()
        snapshot = copy.deepcopy(messages)
        try:
            assert_paired(snapshot)
            paired = True
        except AssertionError:
            paired = False
        conv = self.rig.book.get(agent_id)
        self.epochs.append(conv.epoch if conv is not None else 0)
        self.calls.append(LlmCall(agent_id, snapshot, None, max_tokens, len(self.calls_of(agent_id)) + 1, loop.time(), paired))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.rng.random() * 0.03)
            roll = self.rng.random()
            if roll < self.error_rate:
                raise RuntimeError("random upstream failure")
            if roll < 0.45:
                return llm_resp(("speak", {"channel_id": "A", "content": "x"}))
            if roll < 0.52:
                return llm_resp(("compact_context", {}))
            if roll < 0.62:
                return llm_resp(("sleep", {"seconds": round(self.rng.random() * 12 + 1, 2)}))
            if roll < 0.80:
                return llm_resp(("sleep", {}))
            return LLMResponse(content="just thinking")
        except asyncio.CancelledError:
            self.cancelled.append(agent_id)
            raise
        finally:
            self.in_flight -= 1


def random_envelope(rng: random.Random):
    channel = rng.choice(["A", "B", "C"])
    roll = rng.random()
    if roll < 0.15:
        return urgent("floor.granted", channel_id=channel)
    if roll < 0.25:
        return urgent("request.opened", kind="request", channel_id=channel, request_id=f"r{rng.randint(1, 3)}")
    if roll < 0.60:
        return normal("message.new", channel_id=channel)
    if roll < 0.85:
        return revoked(channel)
    return env("request.cancelled", kind="request", interrupt=True, request_id=f"r{rng.randint(1, 3)}")


def check_agent(rig: Rig, llm: RandomLlm, agent_id: str, stats: dict, delivered: list) -> None:
    calls = llm.calls_of(agent_id)
    epochs = [e for call, e in zip(llm.calls, llm.epochs) if call.agent_id == agent_id]

    # requests are paired, and each extends the previous one of the same conversation epoch
    assert all(c.paired for c in calls)
    for (earlier, e1), (later, e2) in zip(zip(calls, epochs), list(zip(calls, epochs))[1:]):
        if e1 == e2:
            a, b = earlier.lines, later.lines
            assert len(b) >= len(a) and b[: len(a)] == a, f"the request of {agent_id} does not extend the previous one"

    # a cancelled run never acts afterwards; every tool call belongs to a started run
    cancelled_gens: set[int] = set()
    started_gens: set[int] = set()
    for event, who, fields in rig.trace.records:
        if who != agent_id:
            continue
        if event == "wake":
            started_gens.add(fields["gen"])
        elif event == "cancel":
            cancelled_gens.add(fields["gen"])
        elif event == "tool_call":
            assert fields["gen"] not in cancelled_gens, f"gen {fields['gen']} acted after being cancelled"
    assert all(t["gen_id"] in started_gens for t in rig.brain.executed if t["agent_id"] == agent_id)
    assert all(i.gen_id in started_gens for i in rig.world.submitted if i.actor_id == agent_id)

    # every wake-up is accounted for
    wakes = rig.trace.of("wake", agent_id)
    assert len(wakes) == stats["runs"] + stats["cancels"] + stats["failures"], (len(wakes), stats)
    assert sum(1 for w in wakes if not w["retry"]) == stats["activations"]

    # nothing that was delivered got lost: it is part of some opening or delta
    told = {id(e) for c in rig.brain.starts_of(agent_id) + rig.brain.injects_of(agent_id) for e in c.reasons}
    assert all(id(e) in told for e in delivered), "an envelope was delivered but never told to the model"

    # the conversation itself is consistent
    conv = rig.book.get(agent_id)
    if conv is not None:
        assert_paired(conv.messages)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_random_traffic_never_breaks_the_invariants(seed):
    @async_test(timeout=60)
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop_errors: list[dict] = []
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        rng = random.Random(seed)
        rig = Rig(world=FakeWorld(ManualClock(0.0)))
        llm = RandomLlm(rng, rig)
        rig.llm = llm
        rig.brain.nudge_text = "NUDGE"
        agent = rig.agent("a1", retry_backoff_s=0.01, retry_backoff_max_s=0.02, cancel_grace_s=0.5)
        delivered: list = []
        async with running_agent(agent):
            end = loop.time() + 1.5
            while loop.time() < end:
                for _ in range(rng.randint(1, 3)):  # sometimes a whole burst in one loop turn
                    envelope = random_envelope(rng)
                    delivered.append(envelope)
                    agent.deliver(envelope)
                if rng.random() < 0.3:
                    rig.world.clock.advance(rng.random() * 8)
                    agent.fire_timer()  # what the supervisor's tick does
                await asyncio.sleep(rng.random() * 0.02)
            await wait_until(agent.is_quiet, timeout=10, message="the agent never settled")
            await asyncio.sleep(0.05)
            assert agent.is_quiet() and agent.status == "sleeping" and not agent.busy
            stats = dict(agent.stats)
        assert len(delivered) > 30 and stats["runs"] + stats["cancels"] + stats["failures"] > 0
        assert llm.peak == 1  # never two model calls of one agent at the same time
        check_agent(rig, llm, "a1", stats, delivered)
        assert loop_errors == [], loop_errors

    scenario()


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_random_traffic_through_the_supervisor_with_several_agents(seed):
    @async_test(timeout=60)
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop_errors: list[dict] = []
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        rng = random.Random(seed)
        rig = Rig(world=FakeWorld(ManualClock(0.0)))
        llm = RandomLlm(rng, rig, error_rate=0.05)
        rig.llm = llm
        ids = (MODERATOR, "a1", "a2", "a3")
        agents = {aid: rig.agent(aid, "moderator" if aid == MODERATOR else "player", retry_backoff_s=0.01, retry_backoff_max_s=0.02, cancel_grace_s=0.5) for aid in ids}
        sup = Supervisor(
            rig.world, agents, rig.sink, config=SupervisorConfig(tick_interval=0.01, watchdog_s=100.0, metrics_interval_s=0.05),
            usage=rig.usage, trace=rig.trace, book=rig.book,
        )
        delivered: dict[str, list] = {aid: [] for aid in ids}

        def on_tick() -> None:  # the world wakes somebody every now and then, and time passes
            if rng.random() < 0.4:
                target = rng.choice(ids)
                envelope = random_envelope(rng)
                delivered[target].append(envelope)
                rig.world.push(target, envelope)
            if rng.random() < 0.2:
                rig.world.clock.advance(rng.random() * 8)
            rig.world.bump()

        rig.world.on_tick = on_tick
        sup.start({MODERATOR: [urgent("game.started", kind="notice")]})
        end = loop.time() + 1.5
        while loop.time() < end:
            for _ in range(rng.randint(0, 4)):
                target = rng.choice(ids)
                envelope = random_envelope(rng)
                delivered[target].append(envelope)
                rig.world.push(target, envelope)  # through the world: a burst in one turn is one pump
            await asyncio.sleep(rng.random() * 0.02)
        rig.world.on_tick = None
        await wait_until(lambda: all(a.is_quiet() for a in agents.values()), timeout=10, message="the agents never settled")
        await asyncio.sleep(0.05)
        stats = {aid: dict(a.stats) for aid, a in agents.items()}
        await sup.stop()

        assert llm.peak <= len(ids)  # one call in flight per agent at most
        for aid in ids:
            calls = llm.calls_of(aid)
            assert all(c.paired for c in calls)
            check_agent(rig, llm, aid, stats[aid], delivered[aid])
            assert agents[aid].status == "sleeping" and rig.sink.statuses(aid)[-1] == "sleeping"
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()] == []
        assert loop_errors == [], loop_errors

    scenario()


@async_test()
async def test_a_burst_that_reaches_the_whole_table_is_one_activation_per_agent_and_they_think_at_once():
    # the worst case of "every message wakes everybody": 12 players + the moderator all hear the same burst
    rig = Rig()
    ids = [MODERATOR] + [f"a{i}" for i in range(12)]
    for aid in ids:
        rig.llm.script(aid, Step(delay=0.05, resp=llm_resp(("sleep", {}))))  # a model that takes a moment
    agents = {aid: rig.agent(aid) for aid in ids}
    sup = Supervisor(rig.world, agents, rig.sink, config=SupervisorConfig(tick_interval=10.0, watchdog_s=100.0), book=rig.book)
    sup.start()
    for n in range(8):  # eight messages committed one after the other, each delivered to all thirteen
        for aid in ids:
            rig.world.push(aid, normal("message.new", channel_id="pub", label=n))
    await wait_until(lambda: all(a.stats["runs"] == 1 for a in agents.values()), timeout=5)
    await sup.stop()
    assert all(a.stats["activations"] == 1 for a in agents.values())  # one activation per agent, not eight
    assert len(rig.llm.calls) == 13 and rig.llm.peak == 13  # every agent thinks at the same time
    assert all(len(c.reasons) == 8 for c in rig.brain.starts) and len(rig.brain.starts) == 13
    assert json.dumps([e.payload["label"] for e in rig.brain.starts[0].reasons]) == "[0, 1, 2, 3, 4, 5, 6, 7]"
