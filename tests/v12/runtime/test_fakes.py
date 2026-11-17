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
    assert_paired,
    async_test,
    dump,
    llm_resp,
    normal,
    run_async,
    sleep_resp,
    urgent,
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


def test_fake_world_has_a_manual_game_clock():
    world = FakeWorld()
    assert world.clock.now() == 0.0
    world.clock.advance(5)
    assert world.clock.now() == 5.0


def test_fake_brain_follows_the_v0121_protocol_and_records_what_it_is_asked():
    brain = FakeBrain(FakeWorld())
    brain.begin_run("a1")
    brain.reset_conversation("a1")
    assert brain.begins == ["a1"] and brain.resets == ["a1"]
    text, seen = brain.start_message("a1", WakeContext(agent_id="a1", kind="start", gen_id=3, reasons=[urgent("floor.granted", channel_id="pub")]))
    assert "floor.granted@pub" in text and seen == 0 and brain.starts[0].gen_id == 3
    # a DELTA says nothing when there is nothing to tell, and lists the reasons otherwise
    assert brain.inject_message("a1", WakeContext(agent_id="a1", kind="inject", gen_id=3, reasons=[])) is None
    delta = brain.inject_message("a1", WakeContext(agent_id="a1", kind="inject", gen_id=4, reasons=[normal("message.new", label="m")], after_seq=7))
    assert delta is not None and delta[0] == "DELTA[a1] after=7 reasons=[message.new#m]"
    sleep = brain.execute_tool("a1", "sleep", {"seconds": 12}, gen_id=3, seen_seq=0)
    assert sleep.ends_run is True and sleep.sleep_s == 12
    assert brain.execute_tool("a1", "sleep", {}, gen_id=3, seen_seq=0).sleep_s is None
    assert brain.execute_tool("a1", "compact_context", {}, gen_id=3, seen_seq=0).rebuild is True
    outcome = brain.execute_tool("a1", "speak", {"channel_id": "c", "content": "x"}, gen_id=3, seen_seq=7)
    assert outcome.receipt is not None and outcome.receipt.ok
    assert brain.executed[-1]["seen_seq"] == 7
    assert brain.world.submitted[0].gen_id == 3 and brain.world.submitted[0].based_on_seq == 7  # type: ignore[union-attr]
    assert not hasattr(brain, "wait") and brain.system_prompt("a1") == "SYSTEM[a1]"


@async_test()
async def test_fake_llm_plays_its_script_then_falls_back_to_the_default():
    llm = FakeLlm()
    llm.script("a1", sleep_resp(), RuntimeError("boom"))
    assert (await llm.complete("a1", [], None)).tool_calls[0].name == "sleep"
    with pytest.raises(RuntimeError, match="boom"):
        await llm.complete("a1", [], None)
    assert (await llm.complete("a1", [], None)).tool_calls == []  # script exhausted: a plain answer
    assert [c.index for c in llm.calls_of("a1")] == [1, 2, 3]
    assert llm.in_flight == 0


@async_test()
async def test_fake_llm_keeps_a_deep_snapshot_of_every_request_for_byte_comparisons():
    llm = FakeLlm()
    messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "first"}]
    await llm.complete("a1", messages, [{"type": "function"}])
    messages[1]["content"] = "mutated afterwards"  # a runtime that rewrote its history would show here
    messages.append({"role": "user", "content": "more"})
    call = llm.calls[0]
    assert call.messages[1]["content"] == "first" and len(call.messages) == 2
    assert call.lines == dump(call.messages) and call.tools == [{"type": "function"}] and call.paired


@async_test()
async def test_fake_llm_flags_requests_with_unpaired_tool_calls():
    llm = FakeLlm()
    bad = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "speak", "arguments": "{}"}}]},
        {"role": "user", "content": "a result is missing"},
    ]
    good = bad[:2] + [{"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    await llm.complete("a1", bad, None)
    await llm.complete("a1", good, None)
    assert [c.paired for c in llm.calls] == [False, True]
    with pytest.raises(AssertionError):
        assert_paired(bad)
    assert_paired(good)
    with pytest.raises(AssertionError):
        assert_paired([{"role": "tool", "tool_call_id": "c9", "content": "orphan"}])


@async_test()
async def test_fake_llm_hold_is_a_cancellable_await_point():
    llm = FakeLlm()
    gate = asyncio.Event()
    llm.script("a1", Step(hold=gate, resp=sleep_resp()))
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


def test_llm_resp_can_carry_cache_tokens_and_all_four_prices():
    resp = llm_resp(prompt_tokens=10, cache_read_tokens=20, cache_write_tokens=30, input_price_per_m=1.0, cache_read_price_per_m=0.1, cache_write_price_per_m=1.25, provider_id="p", model_id="m")
    assert (resp.prompt_tokens, resp.cache_read_tokens, resp.cache_write_tokens) == (10, 20, 30)
    assert (resp.input_price_per_m, resp.cache_read_price_per_m, resp.cache_write_price_per_m) == (1.0, 0.1, 1.25)
    assert llm_resp(finish_reason="length").finish_reason == "length" and llm_resp(("sleep", {})).finish_reason == "tool_calls"


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
