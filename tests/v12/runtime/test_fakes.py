"""The test doubles must themselves be faithful: they stand in for the real world / brain / model."""

from __future__ import annotations

import asyncio

import pytest

from app.runtime.ports import Brain, EventSink, LlmPort, TraceSink, WakeContext
from app.world.port import WorldPort
from app.world.types import Intent, Receipt

from .fakes import (
    FakeBrain,
    FakeLlm,
    FakeWorld,
    RecordingSink,
    RecordingTrace,
    Step,
    async_test,
    llm_resp,
    run_async,
    urgent,
    wait_resp,
    wait_until,
)


def test_fake_world_implements_the_world_port():
    assert isinstance(FakeWorld(), WorldPort)


def test_fakes_satisfy_the_runtime_protocols():
    brain: Brain = FakeBrain()
    llm: LlmPort = FakeLlm()
    sink: EventSink = RecordingSink()
    trace: TraceSink = RecordingTrace()
    assert brain and llm and sink and trace


def test_fake_world_submit_is_scriptable_and_records_intents():
    world = FakeWorld()
    intent = Intent(actor_id="a1", type="speak", payload={"channel_id": "c", "content": "x"})
    receipt = world.submit(intent)
    assert receipt.ok and receipt.seq == 1 and world.seq == 1 and world.submitted == [intent]
    world.running = False
    assert world.submit(intent).code == "GAME_NOT_RUNNING"
    world.on_submit = lambda i: Receipt.reject("NO_FLOOR", "nope")
    assert world.submit(intent).code == "NO_FLOOR"


def test_fake_world_queues_are_drained_in_order_and_notify_once_per_change():
    world = FakeWorld()
    calls = []
    world.set_notifier(lambda: calls.append(1))
    world.push("a1", urgent("floor.granted"))
    world.push("a2", urgent("floor.granted"))
    world.emit("x", n=1)
    assert [d.target for d in world.drain_outbox()] == ["a1", "a2"]
    assert [e.type for e in world.drain_events()] == ["x"]
    assert world.drain_outbox() == [] and world.drain_events() == []
    assert len(calls) == 3
    world.set_running(False, "why")
    assert world.running is False and world.drain_events()[0].data["reason"] == "why"


def test_fake_brain_records_what_it_is_asked():
    brain = FakeBrain(FakeWorld())
    text, seen = brain.start_message("a1", WakeContext(agent_id="a1", kind="start", gen_id=3, reasons=[urgent("floor.granted", channel_id="pub")]))
    assert "floor.granted@pub" in text and seen == 0 and brain.starts[0].gen_id == 3
    assert brain.inject_message("a1", WakeContext(agent_id="a1", kind="inject", gen_id=3, reasons=[])) is None
    assert brain.execute_tool("a1", "wait", {}, gen_id=3, seen_seq=0).ends_run is True
    assert brain.execute_tool("a1", "compact_context", {}, gen_id=3, seen_seq=0).rebuild is True
    outcome = brain.execute_tool("a1", "speak", {"channel_id": "c", "content": "x"}, gen_id=3, seen_seq=7)
    assert outcome.receipt is not None and outcome.receipt.ok
    assert brain.executed[-1]["seen_seq"] == 7
    assert brain.world.submitted[0].gen_id == 3 and brain.world.submitted[0].based_on_seq == 7  # type: ignore[union-attr]


@async_test()
async def test_fake_llm_plays_its_script_then_falls_back_to_the_default():
    llm = FakeLlm()
    llm.script("a1", wait_resp(), RuntimeError("boom"))
    assert (await llm.complete("a1", [], None)).tool_calls[0].name == "wait"
    with pytest.raises(RuntimeError, match="boom"):
        await llm.complete("a1", [], None)
    assert (await llm.complete("a1", [], None)).tool_calls == []  # script exhausted: a plain answer
    assert [c.index for c in llm.calls_of("a1")] == [1, 2, 3]
    assert llm.in_flight == 0


@async_test()
async def test_fake_llm_hold_is_a_cancellable_await_point():
    llm = FakeLlm()
    gate = asyncio.Event()
    llm.script("a1", Step(hold=gate, resp=wait_resp()))
    task = asyncio.create_task(llm.complete("a1", [], None))
    await wait_until(lambda: llm.in_flight == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert llm.cancelled == ["a1"] and llm.in_flight == 0 and llm.completed == []


@async_test()
async def test_fake_llm_can_ignore_a_cancellation():
    llm = FakeLlm()
    gate = asyncio.Event()
    llm.script("a1", Step(hold=gate, resp=llm_resp(("speak", {})), swallow_cancel_s=0.02))
    task = asyncio.create_task(llm.complete("a1", [], None))
    await wait_until(lambda: llm.in_flight == 1)
    task.cancel()
    resp = await task  # answers anyway, a little late
    assert resp.tool_calls[0].name == "speak" and llm.cancelled == ["a1"]


def test_run_async_flags_leaked_tasks():
    async def leaky() -> None:
        asyncio.get_running_loop().create_task(asyncio.sleep(10), name="leak")

    with pytest.raises(AssertionError, match="leaked tasks"):
        run_async(leaky())
    run_async(leaky(), leaks=False)  # opting out is possible (asyncio.run cancels the leftover task)


def test_run_async_enforces_a_timeout():
    async def forever() -> None:
        await asyncio.sleep(60)

    with pytest.raises(asyncio.TimeoutError):
        run_async(forever(), timeout=0.05)
