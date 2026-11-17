"""The runtime pieces together: real EventBus / UsageBook / TraceLog / GatedEngineLlm behind the agents."""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

from app.engine.llm import LLMClient
from app.runtime.agent import Agent
from app.runtime.bus import EventBus
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
    fast_config,
    llm_resp,
    settle,
    urgent,
    wait_resp,
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
    world = FakeWorld()
    brain = FakeBrain(world)
    bus = EventBus()
    usage = UsageBook()
    trace = TraceLog(tmp_path / "logs", "sess-1")
    trace.add_secret("provider-secret-key-123")
    leaky = "sk-abcdef1234567890XYZ"
    client = FakeEngineClient(
        {
            "a1": [
                llm_resp(
                    ("speak", {"channel_id": "pub", "content": f"my key is {leaky} / provider-secret-key-123"}),
                    thought="plan: say it",
                    prompt_tokens=1000,
                    completion_tokens=100,
                    provider_id="prov",
                    model_id="m1",
                    input_price_per_m=1.0,
                    output_price_per_m=3.0,
                ),
                wait_resp(prompt_tokens=500, completion_tokens=10, provider_id="prov", model_id="m1", input_price_per_m=1.0, output_price_per_m=3.0),
            ],
            MODERATOR: [wait_resp(provider_id="prov", model_id="m2")],
        }
    )
    bindings = {"a1": SimpleNamespace(provider_id="prov", model_id="m1"), MODERATOR: SimpleNamespace(provider_id="prov2", model_id="m2")}
    llm = GatedEngineLlm(client, bindings.get, LlmGate(max_concurrent=2, per_provider=1), timeout_s=5)
    config = fast_config()
    agents = {
        "a1": Agent("a1", "player", world, brain, llm, usage, trace, bus, config),
        MODERATOR: Agent(MODERATOR, "moderator", world, brain, llm, usage, trace, bus, config),
    }
    sup = Supervisor(
        world,
        agents,
        bus,
        config=SupervisorConfig(tick_interval=0.02, watchdog_s=100.0, metrics_interval_s=0.03),
        usage=usage,
        trace=trace,
    )
    _, queue = bus.subscribe()
    sup.start({"a1": [urgent("floor.granted", channel_id="pub")], MODERATOR: [urgent("game.started", kind="notice")]})
    await wait_until(lambda: agents["a1"].stats["runs"] == 1 and agents[MODERATOR].stats["runs"] == 1)
    await wait_until(lambda: usage.by_actor.get("a1") and usage.by_actor["a1"].calls == 2)
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

    # --- accounting, with each call's own price snapshot, survives a save / load
    actor = usage.by_actor["a1"]
    assert (actor.calls, actor.tool_calls, actor.thinking_rounds) == (2, 2, 1)
    assert usage.total_cost_usd > 0 and set(usage.by_model) == {"prov::m1", "prov::m2"}
    assert UsageBook.model_validate(json.loads(json.dumps(usage.model_dump()))) == usage

    # --- the spectator bus saw only status / metrics here, in valid wire format, with increasing ids
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    types = {e.type for e in events}
    assert {"agent.status", "agent.metrics"} <= types
    assert "thought" not in " ".join(e.json() for e in events) and "plan: say it" not in " ".join(e.json() for e in events)
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
    assert {"wake", "llm_call", "thought", "tool_call", "receipt", "run_end"} <= names
    thought = next(r for r in records if r["event"] == "thought")
    assert thought["agent_id"] == "a1" and thought["text"] == "plan: say it"
    speak = next(r for r in records if r["event"] == "tool_call" and r["name"] == "speak")
    assert speak["args"]["content"] == "my key is sk-*** / ***"
