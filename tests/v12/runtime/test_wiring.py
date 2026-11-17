"""The runtime pieces together: real EventBus / UsageBook / TraceLog / ConversationBook / GatedEngineLlm behind the
agents, driven by a Supervisor (v0.12.1)."""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

from app.engine.llm import LLMClient
from app.runtime.agent import Agent
from app.runtime.bus import EventBus
from app.runtime.clock import ManualClock
from app.runtime.conversation import ConversationBook
from app.runtime.llm_gate import GatedEngineLlm, LlmGate
from app.runtime.supervisor import Supervisor, SupervisorConfig
from app.runtime.trace_log import TraceLog
from app.runtime.usage import UsageBook

from .fakes import (
    MODERATOR,
    FakeBrain,
    FakeEngineClient,
    FakeWorld,
    async_test,
    dump,
    fast_config,
    llm_resp,
    normal,
    settle,
    sleep_resp,
    urgent,
    wait_until,
)


def test_gated_engine_llm_calls_the_real_client_with_arguments_it_accepts():
    """Guards against drift: the keywords GatedEngineLlm passes must bind to ``LLMClient.chat_stream``."""
    signature = inspect.signature(LLMClient.chat_stream)
    signature.bind(
        None,  # self
        [{"role": "user", "content": "x"}],
        is_moderator=True,
        tools=None,
        on_chunk=None,
        max_tokens_override=100,
        binding=None,
    )
    assert signature.parameters["on_chunk"].default is None


@async_test()
async def test_real_components_work_together_end_to_end(tmp_path):
    world = FakeWorld(ManualClock(0.0))
    brain = FakeBrain(world)
    bus = EventBus()
    usage = UsageBook()
    book = ConversationBook()
    trace = TraceLog(tmp_path / "logs", "sess-1")
    trace.add_secret("provider-secret-key-123")
    leaky = "sk-abcdef1234567890XYZ"
    price = dict(provider_id="prov", model_id="m1", input_price_per_m=1.0, output_price_per_m=3.0, cache_read_price_per_m=0.1, cache_write_price_per_m=1.25)
    client = FakeEngineClient(
        {
            "a1": [
                llm_resp(
                    ("speak", {"channel_id": "pub", "content": f"my key is {leaky} / provider-secret-key-123"}),
                    thought="plan: say it",
                    prompt_tokens=1000,
                    completion_tokens=100,
                    cache_read_tokens=4000,
                    cache_write_tokens=200,
                    **price,
                ),
                sleep_resp(15, prompt_tokens=500, completion_tokens=10, cache_read_tokens=5200, **price),
                sleep_resp(prompt_tokens=300, completion_tokens=5, cache_read_tokens=5600, **price),
            ],
            MODERATOR: [sleep_resp(provider_id="prov", model_id="m2")],
        }
    )
    bindings = {"a1": SimpleNamespace(provider_id="prov", model_id="m1"), MODERATOR: SimpleNamespace(provider_id="prov2", model_id="m2")}
    llm = GatedEngineLlm(client, bindings.get, LlmGate(), timeout_s=5)
    config = fast_config()
    seen_costs: list[float] = []
    usage.on_record = lambda agent_id, resp: seen_costs.append(round(usage.total_cost_usd, 9))
    agents = {
        "a1": Agent("a1", "player", world, brain, llm, usage, trace, bus, config, book=book, context_window=lambda: 1_000_000),
        MODERATOR: Agent(MODERATOR, "moderator", world, brain, llm, usage, trace, bus, config, book=book),
    }
    sup = Supervisor(
        world,
        agents,
        bus,
        config=SupervisorConfig(tick_interval=0.02, watchdog_s=100.0, metrics_interval_s=0.03),
        usage=usage,
        trace=trace,
        book=book,
    )
    _, queue = bus.subscribe()
    sup.start({"a1": [urgent("floor.granted", channel_id="pub")], MODERATOR: [urgent("game.started", kind="notice")]})
    await wait_until(lambda: agents["a1"].stats["runs"] == 1 and agents[MODERATOR].stats["runs"] == 1)
    await wait_until(lambda: usage.by_actor.get("a1") and usage.by_actor["a1"].calls == 2)
    await settle()
    # a1 sleeps with a 15 s timer: the game clock runs it out, the supervisor's tick wakes it
    world.clock.advance(15)
    await wait_until(lambda: agents["a1"].stats["runs"] == 2)
    await settle()
    await sup.stop()
    trace.close()

    # --- the model client was called the way the engine expects
    by_agent = {c["agent_id"]: c for c in client.calls}
    assert by_agent[MODERATOR]["is_moderator"] is True and by_agent["a1"]["is_moderator"] is False
    assert all(c["on_chunk"] is None for c in client.calls)  # nothing is streamed (D5)
    assert by_agent["a1"]["binding"] is bindings["a1"] and by_agent[MODERATOR]["binding"] is bindings[MODERATOR]
    assert by_agent["a1"]["max_tokens_override"] == world.spec.defaults.llm.max_tokens
    assert world.submitted[0].payload["channel_id"] == "pub"
    # ... and a1's requests extend one another: one conversation, fixed system and tools
    a1_calls = [c for c in client.calls if c["agent_id"] == "a1"]
    assert len(a1_calls) == 3
    for earlier, later in zip(a1_calls, a1_calls[1:]):
        assert dump(later["messages"])[: len(earlier["messages"])] == dump(earlier["messages"])
        assert len(later["messages"]) > len(earlier["messages"])
    assert a1_calls[0]["tools"] == a1_calls[2]["tools"] == brain.tools

    # --- accounting: four kinds of tokens, each call's own price snapshot, the hook, a save / load
    actor = usage.by_actor["a1"]
    assert (actor.calls, actor.activations, actor.tool_calls, actor.thinking_rounds) == (3, 2, 3, 1)
    assert (actor.prompt_tokens, actor.cache_read_tokens, actor.cache_write_tokens, actor.completion_tokens) == (1800, 14800, 200, 115)
    expected = (1800 * 1.0 + 14800 * 0.1 + 200 * 1.25 + 115 * 3.0) / 1e6
    assert actor.cost_usd == pytest.approx(expected) and set(usage.by_model) == {"prov::m1", "prov::m2"}
    assert actor.cache_hit_rate == pytest.approx(14800 / (1800 + 14800 + 200))
    assert len(seen_costs) == 4 and seen_costs == sorted(seen_costs) and seen_costs[-1] == pytest.approx(usage.total_cost_usd, abs=1e-9)
    assert UsageBook.model_validate(json.loads(json.dumps(usage.model_dump()))) == usage
    assert usage.total_tokens == usage.prompt_tokens + usage.cache_read_tokens + usage.cache_write_tokens + usage.completion_tokens

    # --- the spectator bus: status (three states + until) and metrics only, valid wire format, increasing ids
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    types = {e.type for e in events}
    assert {"agent.status", "agent.metrics"} <= types
    statuses = [e.data for e in events if e.type == "agent.status" and e.data["id"] == "a1"]
    assert {s["state"] for s in statuses} == {"sleeping", "thinking", "acting"}
    assert any(s["state"] == "sleeping" and s["until"] == 15.0 for s in statuses)
    metrics = [e.data for e in events if e.type == "agent.metrics" and e.data["id"] == "a1"][-1]
    assert metrics["cache_read_tokens"] == 14800 and metrics["activations"] == 2
    joined = " ".join(e.json() for e in events)
    assert "plan: say it" not in joined  # thoughts never reach the UI
    assert [e.id for e in events] == sorted(e.id for e in events)
    for event in events:
        assert json.loads(event.json())["type"] == event.type and event.sse().startswith(f"id: {event.id}\n")
    assert bus.last_id == events[-1].id

    # --- the trace holds the details the UI never sees, with secrets scrubbed
    files = trace.files()
    assert [p.name for p in files] == ["trace-0001.jsonl"]
    text = files[0].read_text(encoding="utf-8")
    assert leaky not in text and "provider-secret-key-123" not in text
    records = [json.loads(line) for line in text.splitlines()]
    names = {r["event"] for r in records}
    assert {"wake", "conversation", "llm_call", "thought", "tool_call", "receipt", "run_end", "sleep", "timer"} <= names
    thought = next(r for r in records if r["event"] == "thought")
    assert thought["agent_id"] == "a1" and thought["text"] == "plan: say it"
    speak = next(r for r in records if r["event"] == "tool_call" and r["name"] == "speak")
    assert speak["args"]["content"] == "my key is sk-*** / ***"
    call = next(r for r in records if r["event"] == "llm_call" and r["agent_id"] == "a1")
    assert (call["cache_read_tokens"], call["cache_write_tokens"]) == (4000, 200)


@async_test()
async def test_a_game_can_be_paused_and_resumed_with_new_agents_and_the_same_book_end_to_end():
    world = FakeWorld(ManualClock(0.0))
    brain = FakeBrain(world)  # (the orchestrator keeps the brain across a pause too)
    book = ConversationBook()
    usage = UsageBook()
    bus = EventBus()
    client = FakeEngineClient(
        {"a1": [llm_resp(("speak", {"channel_id": "pub", "content": "one"}), prompt_tokens=100, cache_read_tokens=0),
                sleep_resp(40, prompt_tokens=120, cache_read_tokens=100),
                sleep_resp(prompt_tokens=30, cache_read_tokens=250)]}
    )
    llm = GatedEngineLlm(client, lambda aid: None, LlmGate(), timeout_s=5)

    def build() -> tuple[Supervisor, dict[str, Agent]]:
        agents = {"a1": Agent("a1", "player", world, brain, llm, usage, None, bus, fast_config(), book=book)}
        return Supervisor(world, agents, bus, config=SupervisorConfig(tick_interval=0.02, watchdog_s=100.0), usage=usage, book=book), agents

    sup, agents = build()
    sup.start({"a1": [normal("message.new", label="m1")]})
    await wait_until(lambda: agents["a1"].stats["runs"] == 1)
    world.clock.advance(15)  # 25 s of the sleep left
    await sup.stop()  # PAUSE
    world.clock.pause()
    world.clock.advance(3600)
    world.clock.resume()

    sup2, agents2 = build()  # RESUME: new agents, the same conversations
    sup2.start()
    await asyncio.sleep(0.1)
    assert agents2["a1"].stats["runs"] == 0  # still asleep: 25 game seconds to go
    world.clock.advance(25)
    await wait_until(lambda: agents2["a1"].stats["runs"] == 1)
    await sup2.stop()
    calls = [c for c in client.calls if c["agent_id"] == "a1"]
    assert len(calls) == 3
    assert dump(calls[2]["messages"])[: len(calls[1]["messages"])] == dump(calls[1]["messages"])  # the cache prefix survived the pause
    assert brain.starts_of("a1")[0].kind == "start" and len(brain.starts_of("a1")) == 1  # one opening in the whole game


import asyncio  # noqa: E402  (kept next to its only user)
