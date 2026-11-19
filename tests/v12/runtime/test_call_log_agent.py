"""What the agent leaves in its trace for the thinking record and the error log (v0.13.1, G24, G33, G34): the game time and
the model on ``llm_call`` / ``thought`` / ``content``, whether a monologue came with tool calls, and the failure that is
retried or given up.  With the real gate, the real trace sink of a game and a call log the whole path is checked once."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import openai

from app.runtime.call_log import CallLog, LoggingTrace
from app.runtime.clock import ManualClock
from app.runtime.llm_gate import GatedEngineLlm, LlmGate
from app.runtime.trace_log import TraceLog, read_thoughts

from .fakes import FakeWorld, Rig, async_test, llm_resp, normal, running_agent, sleep_resp, wait_until
from .test_call_log_gate import Client, rate_limit

SPEAK = ("speak", {"channel_id": "pub", "content": "hello"})


def wake(agent, label: str = "m") -> None:
    agent.deliver(normal("message.new", label=label, channel_id="pub"))


@async_test()
async def test_the_calls_of_an_agent_are_traced_with_the_game_time_the_model_and_whether_a_monologue_had_tools():
    rig = Rig(world=FakeWorld(ManualClock(0.0)))
    rig.world.clock.advance(37.5)
    rig.llm.script(
        "a1",
        llm_resp(SPEAK, thought="plan the move", content="I will say it now", provider_id="p", model_id="m-1"),
        llm_resp(thought="deeper", content="just thinking aloud", provider_id="p", model_id="m-2"),  # no tool call at all
        sleep_resp(provider_id="p", model_id="m-3"),
    )
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert [c["gt"] for c in rig.trace.of("llm_call", "a1")] == [37.5, 37.5, 37.5]
    thoughts = rig.trace.of("thought", "a1")
    assert [(t["text"], t["model"], t["gt"], t["round"]) for t in thoughts] == [("plan the move", "m-1", 37.5, 0), ("deeper", "m-2", 37.5, 1)]
    contents = rig.trace.of("content", "a1")
    assert [(c["text"], c["model"], c["with_tools"]) for c in contents] == [
        ("I will say it now", "m-1", True),  # said next to a tool call: a remark on the action
        ("just thinking aloud", "m-2", False),  # no tool call: the agent talked to itself
    ]
    assert {t["gen"] for t in thoughts + contents} == {1}


@async_test()
async def test_an_unreadable_game_clock_leaves_the_game_time_out_not_zero():
    rig = Rig()
    rig.world.clock = SimpleNamespace(now=lambda: 1 / 0)  # type: ignore[assignment]
    rig.llm.script("a1", llm_resp(thought="t", model_id="m"), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.trace.of("llm_call", "a1")[0]["gt"] is None and rig.trace.of("thought", "a1")[0]["gt"] is None


@async_test()
async def test_a_failed_run_says_whether_it_is_repeated_and_what_failed():
    rig = Rig()
    rig.llm.script("a1", rate_limit(), RuntimeError("two"), openai.APIConnectionError(request=httpx.Request("POST", "https://x.invalid")))
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["gave_up"] == 1, timeout=3)
    errors = rig.trace.of("error", "a1")
    assert [e["will_retry"] for e in errors] == [True, True, False]  # two retries, then it gave up
    assert [e["error_class"] for e in errors] == ["RateLimitError", "RuntimeError", "APIConnectionError"]
    assert [e["http_status"] for e in errors] == [429, None, None]
    assert [e["attempt"] for e in errors] == [1, 2, 3]  # (the older fields are still there)
    assert len(rig.trace.of("retry", "a1")) == 2 and len(rig.trace.of("give_up", "a1")) == 1


@async_test()
async def test_the_whole_path_every_attempt_and_every_failed_run_is_a_line_of_the_log(tmp_path):
    trace = TraceLog(tmp_path, "game-1")
    log = CallLog("game-1", clock=lambda: 12.5, names=lambda actor: {"a1": "Alice"}.get(actor, actor))
    log.attach_trace(trace)
    client = Client(retries=[rate_limit()], final=RuntimeError("down for sk-abcdef1234567890"))
    llm = GatedEngineLlm(client, lambda agent_id: SimpleNamespace(provider_id="prov", model_id="m-bound"), LlmGate(), 5, call_log=log)
    rig = Rig(llm=llm)  # type: ignore[arg-type]
    rig.trace = LoggingTrace(trace, log)  # type: ignore[assignment]
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["gave_up"] == 1, timeout=3)
    trace.close()
    # one run, three attempts (the run is repeated twice): per attempt the call's own lines, then the run's failure
    assert [(c["kind"], c["status"]) for c in log.entries()] == [
        ("call", "retry"), ("call", "error"), ("event", "retry"),
        ("call", "retry"), ("call", "error"), ("event", "retry"),
        ("call", "retry"), ("call", "error"), ("event", "error"),
    ]
    assert (log.errors, log.retries) == (4, 5)  # (three failed calls and the give-up; three repeated attempts, two repeated runs)
    assert {c["actor_name"] for c in log.entries()} == {"Alice"}
    event = log.entries()[-1]
    assert (event["purpose"], event["actor_id"], event["gt"]) == ("runtime", "a1", 12.5) and "sk-abcdef" not in json.dumps(log.entries())
    # a restart: the same lines come back from the trace alone
    rebuilt = CallLog("game-1", clock=lambda: 99.0, names=lambda actor: {"a1": "Alice"}.get(actor, actor))
    assert rebuilt.load_from_trace(trace.directory) == 9
    assert [(c["kind"], c["status"], c["attempt"]) for c in rebuilt.entries()] == [(c["kind"], c["status"], c["attempt"]) for c in log.entries()]
    assert [c["gt"] for c in rebuilt.entries()] == [12.5] * 9  # (the game time was traced, not taken from the new clock)


@async_test()
async def test_the_thinking_record_reads_what_the_agent_traced(tmp_path):
    trace = TraceLog(tmp_path, "game-2")
    rig = Rig(world=FakeWorld(ManualClock(0.0)))
    rig.trace = trace  # type: ignore[assignment]
    rig.world.clock.advance(5.0)
    rig.llm.script(
        "a1",
        llm_resp(SPEAK, thought="first plan", content="a remark", model_id="m-1"),
        llm_resp(content="talking to myself", thought="second plan", model_id="m-1"),
        sleep_resp(),
    )
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    trace.close()
    page = read_thoughts(trace.directory, "a1")
    assert [(i["kind"], i["text"], i["gt"], i["gen"], i["round"], i["model"]) for i in page["items"]] == [
        ("thought", "first plan", 5.0, 1, 1, "m-1"),  # (no content here: it came with a tool call)
        ("thought", "second plan", 5.0, 1, 2, "m-1"),
        ("content", "talking to myself", 5.0, 1, 2, "m-1"),
    ]
    assert "public" not in json.dumps(page) and "hello" not in json.dumps(page)  # not the tool call
