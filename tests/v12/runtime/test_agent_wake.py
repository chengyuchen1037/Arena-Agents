"""Actor behaviour: priorities, debounce, injection, interruption (focus matching), anti-starvation."""

from __future__ import annotations

import asyncio

from .fakes import (
    Rig,
    Step,
    ambient,
    async_test,
    env,
    llm_resp,
    normal,
    revoked,
    running_agent,
    settle,
    urgent,
    wait_resp,
    wait_until,
)

SPEAK = ("speak", {"channel_id": "pub", "content": "hello"})


def events(envelopes):
    return [e.payload["event"] for e in envelopes]


def labels(envelopes):
    return [e.payload.get("label") for e in envelopes]


def role_sequence(messages):
    return [m["role"] for m in messages]


# =============================================================================== priorities


@async_test()
async def test_urgent_envelope_starts_a_run_immediately():
    rig = Rig()
    rig.llm.script("a1", wait_resp())
    agent = rig.agent(debounce_s=5.0)
    loop = asyncio.get_running_loop()
    async with running_agent(agent):
        started = loop.time()
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: rig.llm.calls)
        assert rig.llm.calls[0].started - started < 0.5  # no debounce for urgent
        await wait_until(lambda: agent.stats["runs"] == 1)
    ctx = rig.brain.starts[0]
    assert ctx.kind == "start" and ctx.agent_id == "a1" and ctx.after_seq == 0
    assert events(ctx.reasons) == ["floor.granted"] and ctx.ambient == []
    first = rig.llm.calls[0]
    assert [m["role"] for m in first.messages] == ["system", "user"]
    assert first.messages[0]["content"] == "SYSTEM[a1]"
    assert "floor.granted@pub" in first.messages[1]["content"]
    assert first.tools == rig.brain.tools
    assert first.max_tokens == 2048  # spec.defaults.llm.max_tokens
    wake = rig.trace.of("wake", "a1")[0]
    assert wake["gen"] == ctx.gen_id and wake["reasons"][0]["event"] == "floor.granted"


@async_test()
async def test_ambient_envelopes_are_only_buffered_until_something_wakes_the_agent():
    rig = Rig()
    rig.llm.script("a1", wait_resp())
    agent = rig.agent(debounce_s=0.05)
    async with running_agent(agent):
        for n in range(3):
            agent.deliver(ambient("message.new", channel_id="pub", label=f"m{n}"))
        agent.deliver(revoked("pub"))  # an interrupt with nothing to interrupt is just information
        await asyncio.sleep(0.25)  # many times the debounce
        assert rig.llm.calls == [] and agent.busy is False
        assert len(agent.pending) == 4
        agent.deliver(urgent("request.opened", kind="request", request_id="r1"))
        await wait_until(lambda: agent.stats["runs"] == 1)
    ctx = rig.brain.starts[0]
    assert events(ctx.reasons) == ["request.opened"]
    assert labels(ctx.ambient)[:3] == ["m0", "m1", "m2"] and len(ctx.ambient) == 4
    assert agent.pending == []  # the run consumed the buffer


@async_test()
async def test_normal_envelopes_are_debounced_from_the_first_one_and_the_window_does_not_slide():
    rig = Rig()
    rig.llm.script("a1", wait_resp())
    agent = rig.agent(debounce_s=0.3)
    loop = asyncio.get_running_loop()
    async with running_agent(agent):
        started = loop.time()
        agent.deliver(normal("message.new", channel_id="free", label="n1"))
        await asyncio.sleep(0.2)
        assert rig.llm.calls == []  # still inside the window
        agent.deliver(normal("message.new", channel_id="free", label="n2"))
        await wait_until(lambda: rig.llm.calls, timeout=3)
        elapsed = rig.llm.calls[0].started - started
        # fixed window: ~0.3 s after the FIRST envelope.  A sliding window would end at >= 0.5 s.
        assert 0.22 <= elapsed < 0.46, elapsed
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert labels(rig.brain.starts[0].reasons) == ["n1", "n2"]  # both are in the same wake


@async_test()
async def test_an_urgent_envelope_cuts_the_debounce_short_and_takes_the_normals_along():
    rig = Rig()
    rig.llm.script("a1", wait_resp())
    agent = rig.agent(debounce_s=5.0)
    loop = asyncio.get_running_loop()
    async with running_agent(agent):
        started = loop.time()
        agent.deliver(normal("message.new", channel_id="free", label="n1"))
        await asyncio.sleep(0.05)
        assert rig.llm.calls == []
        agent.deliver(urgent("request.opened", kind="request", request_id="r1"))
        await wait_until(lambda: rig.llm.calls)
        assert rig.llm.calls[0].started - started < 1.0
        await wait_until(lambda: agent.stats["runs"] == 1)
        await asyncio.sleep(0.1)
        assert len(rig.llm.calls) == 1  # the debounce timer was cancelled, not fired a second run
    assert events(rig.brain.starts[0].reasons) == ["message.new", "request.opened"]  # arrival order


@async_test()
async def test_a_normal_envelope_that_arrives_during_a_run_wakes_the_agent_after_the_run():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=wait_resp()), wait_resp())
    agent = rig.agent(debounce_s=0.05)
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(normal("message.new", channel_id="free", label="n1"))
        await settle()
        assert len(rig.brain.starts) == 1  # not woken while busy
        hold.set()  # the first run ends with ``wait`` (ends_run) and leaves n1 in the buffer
        await wait_until(lambda: len(rig.llm.calls) == 2, timeout=3)
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert labels(rig.brain.starts[1].reasons) == ["n1"]


@async_test()
async def test_a_normal_envelope_can_carry_its_own_debounce():
    rig = Rig()
    rig.llm.script("a1", wait_resp())
    agent = rig.agent(debounce_s=30.0)
    async with running_agent(agent):
        agent.deliver(normal("message.new", channel_id="free", debounce_s=0.0))
        await wait_until(lambda: rig.llm.calls, timeout=2)  # would take 30 s without the hint
        await wait_until(lambda: agent.stats["runs"] == 1)


@async_test()
async def test_ambient_buffer_is_capped_and_drops_the_oldest():
    rig = Rig()
    agent = rig.agent(ambient_cap=5)
    async with running_agent(agent):
        for n in range(20):
            agent.deliver(ambient("message.new", label=n))
        await settle()
        assert [e.payload["label"] for e in agent.pending] == [15, 16, 17, 18, 19]


# =============================================================================== injection


@async_test()
async def test_a_running_agent_gets_what_arrives_injected_at_the_next_round_boundary():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(SPEAK)), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(normal("message.new", channel_id="free", label="n1"))
        agent.deliver(ambient("message.new", channel_id="pub", label="a1"))
        await settle()
        assert len(rig.llm.calls) == 1 and rig.brain.injects == []
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.brain.starts) == 1  # one run, not two
    (ctx,) = rig.brain.injects
    assert ctx.kind == "inject" and ctx.gen_id == rig.brain.starts[0].gen_id
    assert labels(ctx.reasons) == ["n1"] and labels(ctx.ambient) == ["a1"]
    second = rig.llm.calls[1].messages
    assert role_sequence(second) == ["system", "user", "assistant", "tool", "user"]
    assert second[2]["tool_calls"][0]["function"]["name"] == "speak"
    assert second[3]["tool_call_id"] == second[2]["tool_calls"][0]["id"]  # the call stays paired with its result
    assert second[4]["content"] == "INJECT reasons=[message.new@free] ambient=1"
    assert rig.trace.of("inject", "a1")[0]["reasons"][0]["event"] == "message.new"


@async_test()
async def test_injection_reports_the_last_seen_seq_as_the_baseline():
    rig = Rig()
    rig.world.seq = 10
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(SPEAK)), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        rig.world.seq = 25  # something happened meanwhile (the tool call below adds one more)
        agent.deliver(ambient("message.new", channel_id="pub"))
        await settle()
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.brain.injects[0].after_seq == 10  # what the model had seen when it started
    tool_call = rig.brain.executed[0]
    assert tool_call["seen_seq"] == 10  # ... which is what its commit is based on
    # a later round sees the injected baseline
    assert rig.brain.starts[0].gen_id == tool_call["gen_id"]


@async_test()
async def test_without_tool_calls_the_run_continues_while_an_urgent_envelope_is_pending():
    rig = Rig()
    rig.brain.nudge_text = None
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(content="thinking out loud")), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(urgent("request.opened", kind="request", request_id="r1"))
        await settle()
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.llm.calls) == 2 and len(rig.brain.starts) == 1  # same conversation, not a new run
    second = rig.llm.calls[1].messages
    assert role_sequence(second) == ["system", "user", "assistant", "user"]
    assert second[2]["content"] == "thinking out loud"
    assert second[3]["content"] == "INJECT reasons=[request.opened] ambient=0"


@async_test()
async def test_without_tool_calls_the_run_ends_when_only_ambient_envelopes_are_pending():
    rig = Rig()
    rig.brain.nudge_text = None
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(content="done")), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(ambient("message.new", channel_id="pub", label="a1"))
        await settle()
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
        await asyncio.sleep(0.15)
        assert labels(agent.pending) == ["a1"]  # still buffered for the next wake-up
    assert len(rig.llm.calls) == 1 and rig.brain.injects == []
    assert rig.trace.of("run_end", "a1")[0]["reason"] == "no_tool"


@async_test()
async def test_nothing_worth_telling_means_nothing_is_appended():
    rig = Rig()
    rig.brain.inject_hook = lambda ctx: None
    rig.llm.script("a1", llm_resp(SPEAK), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.brain.injects) == 1
    assert role_sequence(rig.llm.calls[1].messages) == ["system", "user", "assistant", "tool"]


@async_test()
async def test_a_broken_inject_hook_is_traced_and_the_run_continues():
    rig = Rig()
    rig.brain.inject_error = RuntimeError("inject bug")
    rig.llm.script("a1", llm_resp(SPEAK), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert len(rig.llm.calls) == 2 and agent.stats["failures"] == 0
    assert any("inject bug" in e["message"] for e in rig.trace.of("error", "a1"))


# =============================================================================== interruption


@async_test()
async def test_an_interrupt_for_the_focus_channel_cancels_the_run_without_restarting_it():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(SPEAK)))
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(revoked("pub"))
        await wait_until(lambda: rig.llm.cancelled == ["a1"])
        await wait_until(lambda: agent.status == "idle")
        await asyncio.sleep(0.2)  # long enough to notice a pointless restart
        assert len(rig.llm.calls) == 1 and rig.brain.executed == []  # nothing was submitted
        assert agent.busy is False and agent.stats["cancels"] == 1 and agent.stats["runs"] == 0
        assert rig.sink.statuses("a1") == ["idle", "thinking", "idle"]
    cancel = rig.trace.of("cancel", "a1")[0]
    assert cancel["envelope"]["channel_id"] == "pub" and cancel["envelope"]["event"] == "floor.revoked"
    assert [e.payload["event"] for e in agent.pending] == []  # stopped: cleared


@async_test()
async def test_an_interrupt_for_another_channel_does_not_cancel():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(SPEAK)), wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(revoked("elsewhere"))
        await settle()
        assert rig.llm.cancelled == [] and agent.busy
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.brain.names() == ["speak", "wait"]  # the run carried on and acted
    assert [e.payload["event"] for e in rig.brain.injects[0].ambient] == ["floor.revoked"]  # but was told


@async_test()
async def test_an_interrupt_matches_the_request_id_of_the_focus():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(("respond", {"request_id": "r1", "value": "yes"}))))
    agent = rig.agent()

    def cancelled(request_id: str):
        return env("request.cancelled", kind="request", priority="ambient", interrupt=True, request_id=request_id)

    async with running_agent(agent):
        agent.deliver(urgent("request.opened", kind="request", channel_id="dm-a1", request_id="r1"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(cancelled("r2"))  # somebody else's request: irrelevant
        await settle()
        assert rig.llm.cancelled == []
        agent.deliver(cancelled("r1"))  # the one this run is answering
        await wait_until(lambda: rig.llm.cancelled == ["a1"])
        await wait_until(lambda: agent.status == "idle")
    assert rig.brain.executed == []


@async_test()
async def test_an_interrupt_without_channel_or_request_matches_nothing():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=wait_resp()))
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(env("floor.revoked", kind="floor", priority="ambient", interrupt=True))
        await settle()
        assert rig.llm.cancelled == []
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)


@async_test()
async def test_the_focus_grows_with_the_envelopes_a_run_consumes():
    rig = Rig()
    first, second = asyncio.Event(), asyncio.Event()
    rig.llm.script("a1", Step(hold=first, resp=llm_resp(SPEAK)), Step(hold=second, resp=wait_resp()))
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="A"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(urgent("request.opened", kind="request", channel_id="B", request_id="rb"))
        await settle()
        agent.deliver(revoked("B"))  # B is not part of the focus yet: only buffered
        await settle()
        assert rig.llm.cancelled == []
        first.set()  # round boundary: the urgent request for B is injected and B joins the focus
        await wait_until(lambda: len(rig.llm.calls) == 2)
        assert rig.llm.cancelled == []
        agent.deliver(revoked("B"))
        await wait_until(lambda: rig.llm.cancelled == ["a1"])
    assert rig.brain.names() == ["speak"]  # round 1 acted, round 2 was cancelled


@async_test()
async def test_after_a_cancellation_an_urgent_envelope_restarts_the_run_at_once():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(SPEAK)), wait_resp())
    agent = rig.agent(debounce_s=5.0)
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(revoked("pub"))
        agent.deliver(urgent("request.opened", kind="request", request_id="r9"))  # same batch
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.llm.cancelled == ["a1"] and len(rig.llm.calls) == 2
    first, restarted = rig.brain.starts
    assert events(restarted.reasons) == ["request.opened"]  # the revoked floor is no longer a reason ...
    assert events(restarted.ambient) == ["floor.revoked"]  # ... but the new run is told about it
    assert restarted.gen_id > first.gen_id


@async_test()
async def test_after_a_cancellation_a_pending_normal_envelope_restarts_the_run_at_once():
    # contracts §3.2: "取消后，只有缓冲里仍有 urgent / normal 时才立即重启" - no debounce on a restart
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(SPEAK)), wait_resp())
    agent = rig.agent(debounce_s=30.0)
    loop = asyncio.get_running_loop()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        cancelled_at = loop.time()
        agent.deliver(revoked("pub"))
        agent.deliver(normal("message.new", channel_id="free", label="n1"))
        await wait_until(lambda: len(rig.llm.calls) == 2, timeout=3)  # would be 30 s with the debounce
        assert rig.llm.calls[1].started - cancelled_at < 1.0
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.llm.cancelled == ["a1"] and labels(rig.brain.starts[1].reasons) == ["n1"]


@async_test()
async def test_a_normal_envelope_after_a_restart_is_debounced_again():
    # the "restart at once" applies to what was pending when the cancellation happened, not forever
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(SPEAK)), wait_resp())
    agent = rig.agent(debounce_s=0.3)
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(revoked("pub"))  # nothing urgent / normal pending: back to idle, no restart
        await wait_until(lambda: agent.status == "idle")
        agent.deliver(normal("message.new", channel_id="free", label="n1"))
        await asyncio.sleep(0.1)
        assert len(rig.llm.calls) == 1  # the debounce applies again
        await wait_until(lambda: len(rig.llm.calls) == 2, timeout=3)


@async_test()
async def test_envelopes_are_processed_in_arrival_order_so_a_new_grant_survives_the_old_revocation():
    rig = Rig()
    rig.llm.script("a1", wait_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(revoked("A"))  # the old floor ends ...
        agent.deliver(urgent("floor.granted", channel_id="A"))  # ... and a new one is granted right after
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.llm.cancelled == []  # the revocation must not kill the run started for the new grant
    (ctx,) = rig.brain.starts
    assert events(ctx.reasons) == ["floor.granted"] and events(ctx.ambient) == ["floor.revoked"]


# =============================================================================== anti-starvation


@async_test()
async def test_the_third_consecutive_interrupt_is_injected_instead_of_cancelling_and_a_normal_end_resets():
    rig = Rig()
    h1, h2, h3, h4 = (asyncio.Event() for _ in range(4))
    rig.llm.script(
        "a1",
        Step(hold=h1, resp=wait_resp()),  # run 1 - cancelled
        Step(hold=h2, resp=wait_resp()),  # run 2 - cancelled
        Step(hold=h3, resp=llm_resp(SPEAK)),  # run 3 - survives the third interrupt
        wait_resp(),  # run 3, round 2
        Step(hold=h4, resp=wait_resp()),  # run 4 - cancelled again (the counter was reset)
        wait_resp(),  # run 5
    )
    agent = rig.agent(debounce_s=5.0)

    def interrupt_and_regrant() -> None:
        agent.deliver(revoked("pub"))
        agent.deliver(urgent("floor.granted", channel_id="pub"))

    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)

        interrupt_and_regrant()
        await wait_until(lambda: len(rig.llm.calls) == 2)
        assert rig.llm.cancelled == ["a1"]

        interrupt_and_regrant()
        await wait_until(lambda: len(rig.llm.calls) == 3)
        assert rig.llm.cancelled == ["a1", "a1"] and agent.stats["cancels"] == 2

        interrupt_and_regrant()  # the third one: injected, not cancelled
        await settle()
        await asyncio.sleep(0.05)
        assert rig.llm.cancelled == ["a1", "a1"] and len(rig.llm.calls) == 3 and agent.busy
        assert rig.trace.of("inject_instead_of_cancel", "a1")
        h3.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
        assert len(rig.llm.calls) == 4  # run 3 got a second round: the pending grant was injected into it
        (ctx,) = rig.brain.injects
        assert events(ctx.reasons) == ["floor.granted"] and events(ctx.ambient) == ["floor.revoked"]

        # a run ended normally -> interrupts cancel again
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 5)
        interrupt_and_regrant()
        await wait_until(lambda: rig.llm.cancelled == ["a1", "a1", "a1"])
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert agent.stats["cancels"] == 3


@async_test()
async def test_cancelled_workers_never_leak_tasks():
    rig = Rig()
    h1, h2 = asyncio.Event(), asyncio.Event()
    rig.llm.script("a1", Step(hold=h1, resp=wait_resp()), Step(hold=h2, resp=wait_resp()), wait_resp())

    def worker_tasks() -> list[asyncio.Task]:
        return [t for t in asyncio.all_tasks() if t.get_name().startswith("worker:") and not t.done()]

    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        assert len(worker_tasks()) == 1
        for _ in range(2):
            agent.deliver(revoked("pub"))
            agent.deliver(urgent("floor.granted", channel_id="pub"))
            await settle(8)
            assert len(worker_tasks()) <= 1  # the cancelled worker is gone, never two at once
        await wait_until(lambda: agent.stats["runs"] == 1)
        await settle()
        assert worker_tasks() == []
        assert agent.stats["cancels"] == 2
    assert worker_tasks() == []


# =============================================================================== stale generations


@async_test()
async def test_a_stale_worker_that_ignores_cancellation_never_touches_the_world():
    rig = Rig()
    hold = asyncio.Event()
    # The model call ignores the cancellation and answers 0.4 s late with a tool call.
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(SPEAK), swallow_cancel_s=0.4))
    agent = rig.agent(cancel_grace_s=0.05)  # the actor gives up waiting after 50 ms
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(revoked("pub"))
        await wait_until(lambda: rig.llm.cancelled == ["a1"])
        await wait_until(lambda: agent.status == "idle")  # the actor moved on without the worker
        assert agent.busy is False
        await wait_until(lambda: rig.llm.completed == ["a1"], timeout=3)  # ... which answers eventually
        await asyncio.sleep(0.1)
    assert rig.brain.executed == [] and rig.world.submitted == []  # its tool call was discarded
    assert agent.stats["runs"] == 0 and agent.stats["failures"] == 0
    assert rig.trace.of("discard", "a1")
    assert any("abandoned" in e["message"] for e in rig.trace.of("error", "a1"))


@async_test()
async def test_a_stale_worker_that_finishes_within_the_grace_period_is_also_discarded():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(SPEAK), swallow_cancel_s=0.03))
    agent = rig.agent(cancel_grace_s=1.0)
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(revoked("pub"))
        await wait_until(lambda: agent.status == "idle")
        await asyncio.sleep(0.1)
    assert rig.llm.completed == ["a1"]  # it did answer ...
    assert rig.brain.executed == [] and rig.world.submitted == []  # ... but nobody acted on it
    assert agent.stats["runs"] == 0


@async_test()
async def test_every_run_gets_a_new_gen_id_that_reaches_the_tools_and_the_start_context():
    rig = Rig()
    rig.llm.script("a1", llm_resp(SPEAK, ("wait", {})), llm_resp(SPEAK, ("wait", {})))
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: agent.stats["runs"] == 1)
        first_gen = agent.gen_id
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: agent.stats["runs"] == 2)
        second_gen = agent.gen_id
    gens = [ctx.gen_id for ctx in rig.brain.starts]
    assert gens == [first_gen, second_gen] and gens[1] > gens[0]
    assert [t["gen_id"] for t in rig.brain.executed] == [gens[0], gens[0], gens[1], gens[1]]
    assert [i.gen_id for i in rig.world.submitted] == [gens[0], gens[1]]  # the intents carry it too
