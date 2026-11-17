"""Randomised stress of the Actor / Worker state machine: whatever arrives and however slow the model is,
the invariants of docs/v0.12-contracts.md §3.2 must hold."""

from __future__ import annotations

import asyncio
import random

import pytest

from app.llm.types import LLMResponse
from app.runtime.ports import ToolOutcome

from .fakes import (
    FakeLlm,
    Rig,
    ambient,
    async_test,
    env,
    llm_resp,
    normal,
    revoked,
    running_agent,
    urgent,
    wait_until,
)


class RandomLlm(FakeLlm):
    """Answers after a random delay with a random mix of tool calls, plain text and (rarely) errors."""

    def __init__(self, rng: random.Random, error_rate: float = 0.04) -> None:
        super().__init__()
        self.rng = rng
        self.error_rate = error_rate

    async def complete(self, agent_id, messages, tools, *, max_tokens=None):  # type: ignore[override]
        self.calls.append(None)  # type: ignore[arg-type]  # only the count matters here
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.rng.random() * 0.03)
            roll = self.rng.random()
            if roll < self.error_rate:
                raise RuntimeError("random upstream failure")
            if roll < 0.55:
                return llm_resp(("speak", {"channel_id": "A", "content": "x"}))
            if roll < 0.65:
                return llm_resp(("compact_context", {}))
            if roll < 0.80:
                return llm_resp(("wait", {}))
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
    if roll < 0.50:
        return normal("message.new", channel_id=channel)
    if roll < 0.70:
        return ambient("message.new", channel_id=channel)
    if roll < 0.90:
        return revoked(channel)
    return env("request.cancelled", kind="request", priority="ambient", interrupt=True, request_id=f"r{rng.randint(1, 3)}")


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_random_traffic_never_breaks_the_invariants(seed):
    @async_test(timeout=40)
    async def scenario() -> None:
        rng = random.Random(seed)
        rig = Rig()
        rig.llm = RandomLlm(rng)  # type: ignore[assignment]
        rig.brain.nudge_text = "NUDGE"
        agent = rig.agent("a1", debounce_s=0.02, retry_backoff_s=0.01, retry_backoff_max_s=0.02, cancel_grace_s=0.5)
        sent = 0
        async with running_agent(agent):
            end = asyncio.get_running_loop().time() + 1.5
            while asyncio.get_running_loop().time() < end:
                for _ in range(rng.randint(1, 3)):  # sometimes a whole burst in one loop turn
                    agent.deliver(random_envelope(rng))
                    sent += 1
                await asyncio.sleep(rng.random() * 0.02)
            await wait_until(agent.is_quiet, timeout=10, message="the agent never settled")
            await asyncio.sleep(0.05)
            assert agent.is_quiet() and agent.status == "idle" and not agent.busy
            # nothing that would wake the agent is left behind
            assert [e for e in agent.pending if e.priority != "ambient"] == []
            stats = dict(agent.stats)
        assert sent > 30 and stats["runs"] + stats["cancels"] + stats["failures"] > 0

        # 1. never two model calls of one agent at the same time
        assert rig.llm.peak == 1

        # 2. a cancelled run never acts afterwards: no tool call carries a generation after its ``cancel``
        cancelled_gens: set[int] = set()
        for event, _agent_id, fields in rig.trace.records:
            if event == "cancel":
                cancelled_gens.add(fields["gen"])
            elif event == "tool_call":
                assert fields["gen"] not in cancelled_gens, f"gen {fields['gen']} acted after being cancelled"

        # 3. every tool call that reached the brain / the world belongs to a started run and a live generation
        started = {ctx.gen_id for ctx in rig.brain.starts}
        assert all(t["gen_id"] in started for t in rig.brain.executed)
        assert all(i.gen_id in started for i in rig.world.submitted)

        # 4. every wake-up that started a run is accounted for: finished, cancelled, or failed
        wakes = len(rig.trace.of("wake", "a1"))
        assert wakes == stats["runs"] + stats["cancels"] + stats["failures"], (wakes, stats)

    scenario()


@async_test()
async def test_stop_while_the_actor_waits_for_a_stubborn_worker_does_not_hang():
    rig = Rig()
    gate = asyncio.Event()
    from .fakes import Step

    # the model call ignores the first cancellation and keeps sleeping for 5 s
    rig.llm.script("a1", Step(hold=gate, resp=llm_resp(("speak", {"channel_id": "A", "content": "x"})), swallow_cancel_s=5.0))
    agent = rig.agent("a1", cancel_grace_s=2.0)
    loop = asyncio.get_running_loop()
    agent.activate()
    actor = loop.create_task(agent.run(), name="actor:a1")
    agent.deliver(urgent("floor.granted", channel_id="A"))
    await wait_until(lambda: len(rig.llm.calls) == 1)
    agent.deliver(revoked("A"))  # the actor now waits (up to 2 s) for the worker to unwind
    await wait_until(lambda: rig.llm.cancelled == ["a1"])
    started = loop.time()
    await agent.stop()  # cancels the sleeping worker a second time and returns promptly
    assert loop.time() - started < 1.0
    await asyncio.wait_for(actor, 2)
    assert rig.brain.executed == [] and agent.status == "stopped"


@async_test()
async def test_a_tool_outcome_object_from_the_brain_is_trusted_but_garbage_is_survived():
    rig = Rig()
    rig.brain.tool_handlers["speak"] = lambda agent_id, args, *, gen_id, seen_seq: "not a ToolOutcome"  # type: ignore[assignment,return-value]
    rig.llm.script("a1", llm_resp(("speak", {"channel_id": "A", "content": "x"})), llm_resp(("wait", {})))
    agent = rig.agent("a1")
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="A"))
        await wait_until(lambda: agent.stats["runs"] == 1)
    result = rig.llm.calls[1].messages[3]
    assert result["role"] == "tool" and result["content"] == "not a ToolOutcome"
    assert isinstance(ToolOutcome(text=""), ToolOutcome)
