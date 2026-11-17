"""The persistent conversation (v0.12.1): append-only requests (so a provider's prefix cache hits), what is and is
not written, compaction by orchestration (a reminder at ``compact_at``, the emergency trim at ``trim_at``)."""

from __future__ import annotations

import asyncio
import json
import re

from app.runtime.agent import COMPACT_NUDGE, TRIM_NOTICE
from app.runtime.conversation import tools_signature

from .fakes import (
    LlmCall,
    Rig,
    Step,
    assert_paired,
    async_test,
    llm_resp,
    normal,
    revoked,
    running_agent,
    settle,
    sleep_resp,
    speak_resp,
    urgent,
    wait_until,
)


def wake(agent, label: str = "m", **kw) -> None:
    kw.setdefault("channel_id", "pub")
    agent.deliver(normal("message.new", label=label, **kw))


def roles(messages):
    return [m["role"] for m in messages]


def assert_extends(earlier: LlmCall, later: LlmCall) -> None:
    """``later`` starts with exactly the bytes of ``earlier`` and then says more (the prefix cache can hit)."""
    before, after = earlier.lines, later.lines
    assert len(after) > len(before), "the next request must be longer than the previous one"
    for index, (old, new) in enumerate(zip(before, after)):
        assert old == new, f"request prefix differs at message {index}:\n  was: {old[:200]}\n  now: {new[:200]}"


# ============================================================================= append-only


@async_test()
async def test_every_request_extends_the_previous_one_byte_for_byte_across_rounds_and_activations():
    rig = Rig()
    rig.llm.script(
        "a1",
        speak_resp("one"),  # activation 1, round 1
        llm_resp(("speak", {"channel_id": "pub", "content": "two"}), ("speak", {"channel_id": "pub", "content": "2b"})),
        sleep_resp(),  # activation 1 ends
        speak_resp("three"),  # activation 2
        sleep_resp(),
        sleep_resp(),  # activation 3
    )
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 2)
        wake(agent, "m3")
        await wait_until(lambda: agent.stats["runs"] == 3)
    calls = rig.llm.calls
    assert len(calls) == 6 and all(c.paired for c in calls)
    for earlier, later in zip(calls, calls[1:]):
        assert_extends(earlier, later)
    # system and tools: the same bytes in every request
    assert len({c.lines[0] for c in calls}) == 1
    assert len({json.dumps(c.tools, ensure_ascii=False) for c in calls}) == 1
    assert len(rig.brain.starts) == 1 and len(rig.brain.injects) == 2  # one opening, two deltas
    assert rig.conversation().epoch == 1  # the prefix was never rewritten


@async_test()
async def test_the_second_activation_adds_exactly_the_last_answer_its_tool_results_and_the_delta():
    rig = Rig()
    rig.llm.script("a1", speak_resp("one"), sleep_resp(), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 2)
    last_of_first, first_of_second = rig.llm.calls[1], rig.llm.calls[2]
    assert_extends(last_of_first, first_of_second)
    added = first_of_second.messages[len(last_of_first.messages):]
    assert roles(added) == ["assistant", "tool", "user"]  # the answer of the last call, its result, the delta
    assert added[0]["tool_calls"][0]["function"]["name"] == "sleep"
    assert added[2]["content"].startswith("DELTA[a1] after=")


@async_test()
async def test_a_cancelled_call_is_not_written_and_the_next_request_still_extends_the_cancelled_one():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=speak_resp("never written")), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(revoked("pub"))
        await wait_until(lambda: agent.stats["runs"] == 1)
    cancelled, restarted = rig.llm.calls
    assert_extends(cancelled, restarted)  # the cancelled request is the prefix of the restarted one ...
    assert roles(restarted.messages[len(cancelled.messages):]) == ["user"]  # ... plus only the new delta
    assert not any("never written" in json.dumps(m) for m in rig.conversation().messages)
    assert rig.brain.names() == ["sleep"]


@async_test()
async def test_a_discarded_stale_answer_is_not_written_either():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=speak_resp("LATE"), swallow_cancel_s=0.2), sleep_resp())
    agent = rig.agent(cancel_grace_s=0.03)
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: len(rig.llm.calls) == 1)
        agent.deliver(revoked("pub"))
        await wait_until(lambda: agent.stats["runs"] == 1)
        await wait_until(lambda: rig.llm.completed.count("a1") == 2, timeout=2)  # the stale answer has arrived by now
        await settle(10)
    assert not any("LATE" in json.dumps(m) for m in rig.conversation().messages)
    assert roles(rig.conversation().messages) == ["user", "user", "assistant", "tool"]


@async_test()
async def test_an_interrupt_delivered_from_inside_a_tool_call_never_leaves_a_half_written_batch():
    # (the supervisor never does this - the pump runs in its own loop callback - but the invariant must hold anyway:
    # the assistant message and ALL its results are written in one stretch, so a cancellation cannot unpair them)
    rig = Rig()
    agent = rig.agent()

    def speak_and_get_revoked(agent_id, args, *, gen_id, seen_seq):
        agent.deliver(revoked("pub"))  # arrives while the batch is being executed
        from app.runtime.ports import ToolOutcome

        return ToolOutcome(text='{"ok":true}')

    rig.brain.tool_handlers["speak"] = speak_and_get_revoked
    rig.llm.script("a1", llm_resp(("speak", {"channel_id": "pub", "content": "1"}), ("speak", {"channel_id": "pub", "content": "2"})), sleep_resp())
    async with running_agent(agent):
        agent.deliver(urgent("floor.granted", channel_id="pub"))
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert agent.stats["cancels"] == 1 and len(rig.llm.calls) == 2  # the doomed run made no further call
    assert_paired(rig.conversation().messages) and all(c.paired for c in rig.llm.calls)
    messages = rig.conversation().messages
    assert roles(messages) == ["user", "assistant", "tool", "tool", "user", "assistant", "tool"]
    assert messages[4]["content"].startswith("DELTA[a1]")  # the revocation woke the agent again, after the whole batch


@async_test()
async def test_system_and_tools_are_fixed_when_the_conversation_is_created():
    rig = Rig()
    rig.llm.script("a1", sleep_resp(), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        conv = rig.conversation()
        assert conv.system == "SYSTEM[a1]" and conv.tools_sig == tools_signature(rig.brain.tools) and conv.epoch == 1
        # the brain changes its mind later: the running conversation does not follow (the cache prefix must not move)
        rig.brain.tools.append({"type": "function", "function": {"name": "late_tool", "description": "x", "parameters": {}}})
        rig.brain.system_prompt = lambda agent_id: "SYSTEM CHANGED"  # type: ignore[method-assign]
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 2)
    first, second = rig.llm.calls[0], rig.llm.calls[-1]
    assert second.messages[0]["content"] == "SYSTEM[a1]" and second.tools == first.tools
    assert not any(t["function"]["name"] == "late_tool" for t in second.tools)
    assert rig.conversation().tools is not rig.brain.tools  # a private copy


@async_test()
async def test_the_seen_seq_advances_with_every_message_and_feeds_the_next_delta_and_the_tools():
    rig = Rig()
    rig.world.seq = 5
    rig.llm.script("a1", llm_resp(("speak", {"channel_id": "pub", "content": "x"})), sleep_resp(), sleep_resp(), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        assert rig.conversation().seen_seq == 5  # the opening reflected seq 5
        rig.world.seq = 9  # other things happened meanwhile
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 2)
        assert rig.brain.injects[0].after_seq == 5 and rig.conversation().seen_seq == 9  # the delta reflects seq 9
        rig.world.seq = 12
        wake(agent, "m3")
        await wait_until(lambda: agent.stats["runs"] == 3)
    assert rig.brain.injects[1].after_seq == 9 and rig.conversation().seen_seq == 12
    # every commit is based on what the conversation had been told at that moment (the STALE check relies on it)
    assert [t["seen_seq"] for t in rig.brain.executed] == [5, 5, 9, 12]


@async_test()
async def test_a_new_agent_object_with_the_same_book_continues_the_same_conversation():
    # pause -> resume: the orchestrator builds new agents but hands them the same book
    rig = Rig()
    rig.llm.script("a1", speak_resp(), sleep_resp(), sleep_resp())
    first = rig.agent()
    async with running_agent(first):
        wake(first, "m1")
        await wait_until(lambda: first.stats["runs"] == 1)
    second = rig.agent()  # a brand new Agent
    async with running_agent(second):
        wake(second, "m2")
        await wait_until(lambda: second.stats["runs"] == 1)
    assert len(rig.brain.starts) == 1 and len(rig.brain.injects) == 1  # no second opening
    assert_extends(rig.llm.calls[1], rig.llm.calls[2])


@async_test()
async def test_resetting_the_book_makes_the_next_activation_start_a_new_conversation():
    rig = Rig()
    rig.llm.script("a1", sleep_resp(), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        rig.book.reset()  # a new game / an import / a restore from a save
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert len(rig.brain.starts) == 2 and rig.brain.injects == []
    assert roles(rig.llm.calls[1].messages) == ["system", "user"]  # nothing of the old game is carried over


# ====================================================================== compaction by orchestration


def window_agent(rig: Rig, window: int = 1000, **cfg):
    return rig.agent(context_window=lambda: window, **cfg)


@async_test()
async def test_below_the_threshold_nothing_is_said():
    rig = Rig()
    rig.llm.script("a1", speak_resp(prompt_tokens=500), sleep_resp(prompt_tokens=550))
    agent = window_agent(rig)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert not any(COMPACT_NUDGE in json.dumps(c.messages, ensure_ascii=False) for c in rig.llm.calls)
    assert rig.trace.of("compact_nudge", "a1") == [] and rig.conversation().notes == []


@async_test()
async def test_at_compact_at_of_the_window_the_agent_is_reminded_once_to_compact():
    rig = Rig()
    # the input of a call is what missed the cache + what was read from it + what was written to it
    rig.llm.script(
        "a1",
        speak_resp(prompt_tokens=100, cache_read_tokens=450, cache_write_tokens=60),  # 610 >= 0.6 * 1000
        speak_resp(prompt_tokens=100, cache_read_tokens=600),  # still above: but one reminder is enough
        sleep_resp(prompt_tokens=100, cache_read_tokens=700),
    )
    agent = window_agent(rig)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    second, third = rig.llm.calls[1].messages, rig.llm.calls[2].messages
    assert roles(second) == ["system", "user", "assistant", "tool", "user"] and second[-1]["content"] == COMPACT_NUDGE
    assert roles(third) == ["system", "user", "assistant", "tool", "user", "assistant", "tool"]  # not said again
    assert len(rig.trace.of("compact_nudge", "a1")) == 1 and rig.trace.of("compact_nudge", "a1")[0]["used"] == 610
    assert "compact_context" in COMPACT_NUDGE and rig.conversation().epoch == 1  # a reminder: nothing was removed


@async_test()
async def test_the_reminder_shares_a_message_with_what_arrived_and_waits_for_the_next_one_if_the_run_ends():
    rig = Rig()
    hold = asyncio.Event()
    rig.llm.script("a1", Step(hold=hold, resp=speak_resp(prompt_tokens=700)), llm_resp(content="", prompt_tokens=700))
    rig.brain.nudge_text = None
    agent = window_agent(rig)
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: len(rig.llm.calls) == 1)
        wake(agent, "m2")  # arrives during the call: delta + reminder become ONE user message
        await settle()
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
    merged = rig.llm.calls[1].messages[-1]["content"]
    assert merged.startswith("DELTA[a1]") and merged.endswith(COMPACT_NUDGE) and merged.count("\n\n") == 1

    # a run that ends by itself does not spend a model call on the reminder: it rides on the next activation
    rig2 = Rig()
    rig2.brain.nudge_text = None
    rig2.llm.script("a1", llm_resp(content="done", prompt_tokens=800), sleep_resp(prompt_tokens=800))
    agent2 = window_agent(rig2)
    async with running_agent(agent2):
        wake(agent2, "m1")
        await wait_until(lambda: agent2.stats["runs"] == 1)
        assert len(rig2.llm.calls) == 1 and rig2.conversation().notes == [COMPACT_NUDGE]
        wake(agent2, "m2")
        await wait_until(lambda: agent2.stats["runs"] == 2)
    last = rig2.llm.calls[1].messages[-1]["content"]
    assert last.startswith("DELTA[a1]") and last.endswith(COMPACT_NUDGE) and rig2.conversation().notes == []


@async_test()
async def test_compact_context_rebuilds_the_conversation_and_the_reminder_can_come_back_later():
    rig = Rig()
    rig.llm.script(
        "a1",
        speak_resp(prompt_tokens=700),  # -> reminder
        llm_resp(("compact_context", {}), prompt_tokens=720),  # the agent complies
        speak_resp(prompt_tokens=100),  # the new conversation is small again
        speak_resp(prompt_tokens=650),  # ... and grows: the reminder is due again (new conversation)
        sleep_resp(prompt_tokens=660),
    )
    agent = window_agent(rig)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    calls = rig.llm.calls
    assert calls[1].messages[-1]["content"] == COMPACT_NUDGE
    assert roles(calls[2].messages) == ["system", "user"] and calls[2].messages[1]["content"].startswith("START[a1]")
    assert calls[2].messages[0] == calls[0].messages[0] and calls[2].tools == calls[0].tools  # the prefix: system + tools
    assert rig.conversation().epoch == 2 and len(rig.trace.of("compact_nudge", "a1")) == 2
    assert calls[4].messages[-1]["content"] == COMPACT_NUDGE
    assert_extends(calls[2], calls[3])  # after the rebuild the conversation is append-only again
    assert_extends(calls[3], calls[4])


def big_speak(i: int, **kw):
    return speak_resp("x" * 300 + f" #{i}", **kw)


@async_test()
async def test_at_trim_at_the_oldest_complete_rounds_are_dropped_and_the_agent_is_told():
    rig = Rig()
    rig.llm.script(
        "a1",
        *[big_speak(i, prompt_tokens=100 + 50 * i) for i in range(1, 5)],  # rounds 1-4: 150 .. 300 tokens
        big_speak(5, prompt_tokens=950),  # >= 0.9 * 1000: the emergency
        sleep_resp(prompt_tokens=400),
    )
    agent = window_agent(rig)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    after = rig.llm.calls[5].messages  # the request that follows the trim
    assert all(c.paired for c in rig.llm.calls) and after[1]["content"].startswith("START[a1]")  # the opening is pinned
    kept = [int(re.search(r"#(\d+)", m["tool_calls"][0]["function"]["arguments"]).group(1)) for m in after if m.get("tool_calls")]
    assert kept and kept[-1] == 5  # the newest round is there ...
    assert len(kept) < 5  # ... and some of the oldest are not
    assert kept == list(range(5 - len(kept) + 1, 6))  # what remains is a contiguous run of the NEWEST rounds
    trim = rig.trace.of("trim", "a1")[0]
    assert trim["rounds"] == 5 - len(kept) and trim["used_before"] == 950 and trim["used_after"] < 600
    assert trim["epoch"] == 2 and rig.conversation().epoch == 2
    notice = after[-1]
    assert notice["role"] == "user" and notice["content"] == TRIM_NOTICE.format(rounds=trim["rounds"])
    assert len(rig.llm.calls[5].lines) < len(rig.llm.calls[4].lines) + 3  # it really got shorter than "just appended"


@async_test()
async def test_a_trim_is_by_complete_rounds_only_so_tool_calls_stay_paired():
    rig = Rig()
    two_calls = llm_resp(
        ("speak", {"channel_id": "pub", "content": "a" * 400}),
        ("speak", {"channel_id": "pub", "content": "b" * 400}),
        prompt_tokens=200,
    )
    rig.llm.script("a1", two_calls, two_calls, big_speak(3, prompt_tokens=980), sleep_resp(prompt_tokens=300))
    agent = window_agent(rig)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert all(c.paired for c in rig.llm.calls)
    assert_paired(rig.conversation().messages)
    assert rig.trace.of("trim", "a1")  # something was dropped, whole rounds of both kinds are fine


@async_test()
async def test_the_newest_round_and_the_opening_are_never_dropped_even_when_that_is_not_enough():
    rig = Rig()
    rig.brain.nudge_text = None
    rig.llm.script("a1", big_speak(1, prompt_tokens=990), llm_resp(content="", prompt_tokens=300))
    agent = window_agent(rig, window=1000)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    # over the limit with a single round: there is nothing that could go (the opening and the newest round stay)
    assert rig.trace.of("trim_failed", "a1") and rig.trace.of("trim", "a1") == []
    assert roles(rig.llm.calls[1].messages) == ["system", "user", "assistant", "tool"]
    assert all(c.paired for c in rig.llm.calls) and rig.conversation().epoch == 1  # nothing to drop: nothing changed


@async_test()
async def test_without_a_known_context_window_there_is_no_reminder_and_no_trim():
    for window in (None, lambda: 0, lambda: (_ for _ in ()).throw(RuntimeError("binding gone"))):
        rig = Rig()
        rig.llm.script("a1", big_speak(1, prompt_tokens=10**9), big_speak(2, prompt_tokens=10**9), sleep_resp())
        agent = rig.agent(context_window=window)
        async with running_agent(agent):
            wake(agent)
            await wait_until(lambda: agent.stats["runs"] == 1)
        assert rig.conversation().epoch == 1 and rig.conversation().notes == []
        assert rig.trace.of("trim", "a1") == [] and rig.trace.of("compact_nudge", "a1") == []


@async_test()
async def test_the_thresholds_are_configurable_and_live():
    rig = Rig()
    rig.llm.script("a1", speak_resp(prompt_tokens=350), sleep_resp(prompt_tokens=350))
    agent = window_agent(rig, compact_at=0.3, trim_at=0.99)
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.llm.calls[1].messages[-1]["content"] == COMPACT_NUDGE
