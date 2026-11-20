"""The optional manifest hooks of a brain and one brain per seat (v0.14, H15a, contracts §13.5).

``mount_manifest(agent_id)`` is asked after a conversation was created or rebuilt and becomes the trace event ``mounts``
(``gen``, ``epoch``, ``blocks``); ``hint_manifest(agent_id)`` is asked after every ``inject_message`` that appended a message and
becomes ``hint`` (``gen``, ``tags``, ``entries``) when it answers with something.  Both are found with ``getattr``: a brain
without them works as before.  A trace never carries a body.  And the runtime has no notion of "the" brain: every agent talks
to the brain it was given, so every seat can have its own.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.bots.types import MountBlock
from app.runtime.agent import Agent
from app.runtime.ports import ToolOutcome
from app.runtime.supervisor import Supervisor
from app.runtime.trace_log import TraceLog

from .fakes import (
    MODERATOR,
    FakeBrain,
    FakeWorld,
    Rig,
    Step,
    async_test,
    fast_config,
    llm_resp,
    normal,
    running_agent,
    settle,
    sleep_resp,
    speak_resp,
    wait_until,
)
from .test_supervisor import fast_supervisor_config

BLOCKS = [
    {"id": "core.identity", "layer": "L0", "chars": 120, "sha": "0123456789ab"},
    {"id": "l2.playbook", "layer": "L2", "chars": 640, "sha": "ba9876543210"},
]
HINT = {"tags": ["t1", "t2"], "entries": ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]}


class HookBrain(FakeBrain):
    """A brain that offers both hooks; ``blocks`` / ``hint`` are what it answers (``None`` = nothing to tell)."""

    def __init__(self, world: FakeWorld | None = None) -> None:
        super().__init__(world)
        self.blocks: Any = [dict(b) for b in BLOCKS]
        self.hint: Any = dict(HINT)
        self.mount_calls: list[str] = []
        self.hint_calls: list[str] = []

    def mount_manifest(self, agent_id: str) -> Any:
        self.mount_calls.append(agent_id)
        return self.blocks

    def hint_manifest(self, agent_id: str) -> Any:
        self.hint_calls.append(agent_id)
        return self.hint


def wake(agent: Agent, label: str = "m", **kw: Any) -> None:
    kw.setdefault("channel_id", "pub")
    agent.deliver(normal("message.new", label=label, **kw))


def hooked_rig(**brain_fields: Any) -> tuple[Rig, HookBrain]:
    rig = Rig()
    brain = HookBrain(rig.world)
    for key, value in brain_fields.items():
        setattr(brain, key, value)
    rig.brain = brain
    return rig, brain


# ============================================================================= mounts


@async_test()
async def test_mounts_are_traced_once_when_the_conversation_is_created_and_not_again_for_deltas():
    rig, brain = hooked_rig()
    rig.llm.script("a1", speak_resp("one"), sleep_resp(), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert brain.mount_calls == ["a1"]  # asked once, about its own seat
    assert rig.trace.of("mounts", "a1") == [{"gen": 1, "epoch": 1, "blocks": BLOCKS}]
    names = rig.trace.names("a1")
    assert names.index("conversation") < names.index("mounts") < names.index("llm_call")  # before the first model call
    assert names.count("mounts") == 1


@async_test()
async def test_a_rebuild_traces_the_mounts_again_with_the_new_epoch_and_keeps_system_and_tools():
    rig, brain = hooked_rig()
    rig.llm.script("a1", llm_resp(("compact_context", {"summary_markdown": "摘要"})), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert brain.mount_calls == ["a1", "a1"]
    first, second = rig.trace.of("mounts", "a1")
    assert (first["epoch"], second["epoch"]) == (1, 2) and first["blocks"] == second["blocks"] == BLOCKS
    assert first["gen"] == second["gen"] == 1  # (a rebuild happens inside the run that asked for it)
    names = rig.trace.names("a1")
    first_mounts, last_mounts = names.index("mounts"), len(names) - 1 - names[::-1].index("mounts")
    assert names.count("mounts") == 2 and first_mounts < names.index("rebuild") < last_mounts  # the second follows the rebuild
    conversation = rig.conversation()
    assert conversation.epoch == 2 and conversation.system == "SYSTEM[a1]"


@async_test()
async def test_a_mount_block_object_is_accepted_like_its_dict():
    block = MountBlock(id="core.voice", layer="L0", chars=88, sha="cafecafecafe")
    rig, _brain = hooked_rig(blocks=[block, {"id": "anchor", "layer": "L0", "chars": 80, "sha": "000000000000"}])
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    [mounts] = rig.trace.of("mounts", "a1")
    assert mounts["blocks"][0] == block.to_dict() == {"id": "core.voice", "layer": "L0", "chars": 88, "sha": "cafecafecafe"}
    assert len(mounts["blocks"]) == 2


@async_test()
async def test_an_empty_manifest_is_still_a_manifest_and_a_missing_one_writes_nothing():
    rig, _brain = hooked_rig(blocks=[])
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert rig.trace.of("mounts", "a1") == [{"gen": 1, "epoch": 1, "blocks": []}]

    silent, _ = hooked_rig(blocks=None)  # the brain has nothing to say about this conversation
    other = silent.agent("a2")
    async with running_agent(other):
        wake(other)
        await wait_until(lambda: other.stats["runs"] == 1)
    assert silent.trace.of("mounts") == [] and silent.trace.of("error") == []


# ============================================================================= hint


@async_test()
async def test_a_hint_is_traced_after_the_opening_message_and_after_every_delta():
    rig, brain = hooked_rig()
    rig.llm.script("a1", sleep_resp(), sleep_resp(), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")  # the FULL opening message (``start_message``): it may carry a hint, too
        await wait_until(lambda: agent.stats["runs"] == 1)
        assert rig.trace.of("hint", "a1") == [{"gen": 1, **HINT}] and brain.hint_calls == ["a1"]
        wake(agent, "m2")  # a DELTA (``inject_message``)
        await wait_until(lambda: agent.stats["runs"] == 2)
        wake(agent, "m3")
        await wait_until(lambda: agent.stats["runs"] == 3)
    assert rig.trace.of("hint", "a1") == [{"gen": 1, **HINT}, {"gen": 2, **HINT}, {"gen": 3, **HINT}]
    assert brain.hint_calls == ["a1", "a1", "a1"]
    names = rig.trace.names("a1")
    # right after each message: the opening (conversation, mounts, hint) and then inject, hint - per delta
    assert [n for n in names if n in ("conversation", "mounts", "inject", "hint")] == [
        "conversation", "mounts", "hint", "inject", "hint", "inject", "hint",
    ]
    assert names.index("hint") < names.index("llm_call")  # the opening hint is there before the first model call


@async_test()
async def test_a_hint_is_traced_after_a_merge_of_what_arrived_during_a_call_too():
    rig, brain = hooked_rig()
    hold = asyncio.Event()
    rig.brain.nudge_text = "NUDGE"
    rig.llm.script("a1", Step(hold=hold, resp=llm_resp(content="hmm")), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: len(rig.llm.calls) == 1)
        wake(agent, "m2")  # arrives while the call is in flight: merged at the next round boundary
        await settle()
        hold.set()
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert [r["kind"] for r in rig.trace.of("inject", "a1")] == ["merge"]
    assert rig.trace.of("hint", "a1") == [{"gen": 1, **HINT}, {"gen": 1, **HINT}]  # the opening, then the merge
    assert brain.hint_calls == ["a1", "a1"]
    names = rig.trace.names("a1")
    assert [n for n in names if n in ("conversation", "mounts", "inject", "hint")] == [
        "conversation", "mounts", "hint", "inject", "hint",
    ]


@async_test()
async def test_a_rebuilt_conversation_opens_with_its_own_mounts_and_hint():
    rig, brain = hooked_rig()
    rig.llm.script("a1", llm_resp(("compact_context", {"summary_markdown": "摘要"})), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    assert brain.hint_calls == ["a1", "a1"] and brain.mount_calls == ["a1", "a1"]
    names = rig.trace.names("a1")
    assert [n for n in names if n in ("conversation", "mounts", "hint", "rebuild")] == [
        "conversation", "mounts", "hint", "rebuild", "mounts", "hint",
    ]
    assert rig.trace.of("hint", "a1") == [{"gen": 1, **HINT}, {"gen": 1, **HINT}]


@async_test()
async def test_no_hint_is_traced_when_the_brain_answers_with_nothing():
    for nothing in (None, {}, [], ""):
        rig, brain = hooked_rig(hint=nothing)
        rig.llm.script("a1", sleep_resp(), sleep_resp())
        agent = rig.agent()
        async with running_agent(agent):
            wake(agent, "m1")
            await wait_until(lambda: agent.stats["runs"] == 1)
            wake(agent, "m2")
            await wait_until(lambda: agent.stats["runs"] == 2)
        assert brain.hint_calls == ["a1", "a1"], nothing  # it was asked after the opening and after the delta ...
        assert rig.trace.of("hint", "a1") == [], nothing  # ... and had nothing to say


@async_test()
async def test_a_delta_that_added_no_message_is_not_asked_for_a_hint():
    rig, brain = hooked_rig()
    brain.inject_hook = lambda ctx: None  # nothing new to tell: no message is appended
    rig.llm.script("a1", sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert brain.hint_calls == ["a1"] and rig.trace.of("hint", "a1") == [{"gen": 1, **HINT}]  # the opening only


@async_test()
async def test_a_hint_keeps_its_tags_and_entries_lists_and_carries_further_short_fields():
    rig, _brain = hooked_rig(hint={"tags": "single", "kind": "speak", "gen": 99, "event": "x", "n": 3})
    rig.llm.script("a1", sleep_resp(), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 2)
    opening, delta = rig.trace.of("hint", "a1")
    # ``tags`` / ``entries`` are always lists; a name the trace line owns (gen, event ...) never overrides it
    assert opening == {"gen": 1, "tags": ["single"], "entries": [], "kind": "speak", "n": 3}
    assert delta == {"gen": 2, "tags": ["single"], "entries": [], "kind": "speak", "n": 3}


# ============================================================================= robustness


@async_test()
async def test_a_brain_without_the_hooks_works_as_before_and_writes_neither_event():
    rig = Rig()  # the plain FakeBrain has no mount_manifest / hint_manifest
    assert not hasattr(rig.brain, "mount_manifest") and not hasattr(rig.brain, "hint_manifest")
    rig.llm.script("a1", speak_resp("one"), sleep_resp(), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 2)
    names = rig.trace.names("a1")
    assert "mounts" not in names and "hint" not in names and "error" not in names
    assert rig.brain.names() == ["speak", "sleep", "sleep"]


@async_test()
async def test_a_hook_that_is_not_callable_is_ignored():
    rig = Rig()
    rig.brain.mount_manifest = None  # type: ignore[attr-defined]
    rig.brain.hint_manifest = "nope"  # type: ignore[attr-defined]
    rig.llm.script("a1", sleep_resp(), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert "mounts" not in rig.trace.names("a1") and "error" not in rig.trace.names("a1")


@async_test()
async def test_a_failing_hook_is_traced_as_an_error_and_never_stops_the_run():
    rig, brain = hooked_rig()

    def broken(agent_id: str) -> Any:
        raise RuntimeError("manifest broke")

    brain.mount_manifest = broken  # type: ignore[method-assign]
    brain.hint_manifest = broken  # type: ignore[method-assign]
    rig.llm.script("a1", speak_resp("one"), sleep_resp(), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 2)
    assert rig.brain.names() == ["speak", "sleep", "sleep"] and agent.stats["failures"] == 0  # the game went on
    messages = [r["message"] for r in rig.trace.of("error", "a1")]
    assert any("brain.mount_manifest failed" in m for m in messages) and any("brain.hint_manifest failed" in m for m in messages)
    assert rig.trace.of("mounts", "a1") == [] and rig.trace.of("hint", "a1") == []
    assert rig.world.submitted[0].type == "speak"


@async_test()
async def test_a_trace_never_carries_a_body_whatever_the_brain_hands_over():
    prose = "这是一大段提示词正文，" * 12
    assert len(prose) > 64
    rig, _brain = hooked_rig(
        blocks=[{"id": "l2.digest", "layer": "L2", "chars": len(prose), "sha": "0123456789ab", "text": prose, "extra": {"body": prose}}],
        hint={"tags": ["t1", prose], "entries": ["0123456789ab"], "note": prose},
    )
    rig.llm.script("a1", sleep_resp(), sleep_resp())
    agent = rig.agent()
    async with running_agent(agent):
        wake(agent, "m1")
        await wait_until(lambda: agent.stats["runs"] == 1)
        wake(agent, "m2")
        await wait_until(lambda: agent.stats["runs"] == 2)
    [mounts] = rig.trace.of("mounts", "a1")
    hints = rig.trace.of("hint", "a1")
    assert len(hints) == 2  # (after the opening message and after the delta)
    text = json.dumps([mounts, *hints], ensure_ascii=False)
    assert "提示词正文" not in text  # not a single word of it
    [block] = mounts["blocks"]
    assert (block["id"], block["layer"], block["chars"], block["sha"]) == ("l2.digest", "L2", len(prose), "0123456789ab")
    assert block["text"].startswith("…[") and str(len(prose)) in block["text"] and block["extra"]["body"].startswith("…[")
    for hint in hints:
        assert hint["tags"][0] == "t1" and hint["tags"][1].startswith("…[") and hint["entries"] == ["0123456789ab"]
        assert hint["note"].startswith("…[") and str(len(prose)) in hint["note"]


@async_test()
async def test_the_real_trace_file_holds_only_ids_layers_lengths_and_hashes(tmp_path):
    rig, _brain = hooked_rig()
    trace = TraceLog(tmp_path, "g1")
    rig.trace = trace  # type: ignore[assignment]
    rig.llm.script("a1", sleep_resp(), sleep_resp())
    agent = rig.agent()
    try:
        async with running_agent(agent):
            wake(agent, "m1")
            await wait_until(lambda: agent.stats["runs"] == 1)
            wake(agent, "m2")
            await wait_until(lambda: agent.stats["runs"] == 2)
    finally:
        trace.close()
    records = [json.loads(line) for path in trace.files() for line in path.read_text(encoding="utf-8").splitlines()]
    mounts = [r for r in records if r["event"] == "mounts"]
    hints = [r for r in records if r["event"] == "hint"]
    assert len(mounts) == 1 and mounts[0]["agent_id"] == "a1" and mounts[0]["blocks"] == BLOCKS
    assert len(hints) == 2  # after the opening message and after the delta
    for hint in hints:
        assert hint["tags"] == HINT["tags"] and hint["entries"] == HINT["entries"]
    for record in (mounts[0], *hints):
        assert {"ts", "event", "agent_id", "gen"} <= set(record)


# ============================================================================= one brain per seat


class SeatBrain(FakeBrain):
    """The brain of ONE seat: its own system prompt, its own tools, optionally its own hooks.  Every call about another seat
    is a bug and is written to ``foreign``."""

    def __init__(self, world: FakeWorld, seat: str, *, hooks: bool = True, tool_names: tuple[str, ...] | None = None) -> None:
        super().__init__(world)
        self.seat = seat
        self.foreign: list[tuple[str, str]] = []
        self.mount_calls: list[str] = []
        self.hint_calls: list[str] = []
        if tool_names is not None:
            self.tools = [
                {"type": "function", "function": {"name": n, "description": n, "parameters": {"type": "object", "properties": {}}}}
                for n in tool_names
            ]
        if hooks:  # instance attributes: only these seats have the hooks at all
            self.mount_manifest = self._mount_manifest  # type: ignore[assignment]
            self.hint_manifest = self._hint_manifest  # type: ignore[assignment]

    def _mine(self, what: str, agent_id: str) -> None:
        if agent_id != self.seat:
            self.foreign.append((what, agent_id))

    def system_prompt(self, agent_id: str) -> str:
        self._mine("system_prompt", agent_id)
        return f"SYSTEM[{self.seat}] own prompt of {self.seat}"

    def tool_schemas(self, agent_id: str) -> list[dict[str, Any]]:
        self._mine("tool_schemas", agent_id)
        return super().tool_schemas(agent_id)

    def begin_run(self, agent_id: str) -> None:
        self._mine("begin_run", agent_id)
        super().begin_run(agent_id)

    def reset_conversation(self, agent_id: str) -> None:
        self._mine("reset_conversation", agent_id)
        super().reset_conversation(agent_id)

    def start_message(self, agent_id: str, ctx: Any) -> tuple[str, int]:
        self._mine("start_message", agent_id)
        return super().start_message(agent_id, ctx)

    def inject_message(self, agent_id: str, ctx: Any) -> tuple[str, int] | None:
        self._mine("inject_message", agent_id)
        return super().inject_message(agent_id, ctx)

    def no_tool_nudge(self, agent_id: str, content: str, round_idx: int) -> str | None:
        self._mine("no_tool_nudge", agent_id)
        return super().no_tool_nudge(agent_id, content, round_idx)

    def execute_tool(self, agent_id: str, name: str, args: dict[str, Any], *, gen_id: int, seen_seq: int) -> ToolOutcome:
        self._mine("execute_tool", agent_id)
        return super().execute_tool(agent_id, name, args, gen_id=gen_id, seen_seq=seen_seq)

    def _mount_manifest(self, agent_id: str) -> list[dict[str, Any]]:
        self._mine("mount_manifest", agent_id)
        self.mount_calls.append(agent_id)
        return [{"id": f"core.{self.seat}", "layer": "L0", "chars": 10 + len(self.seat), "sha": f"{self.seat:0<12}"[:12]}]

    def _hint_manifest(self, agent_id: str) -> dict[str, Any]:
        self._mine("hint_manifest", agent_id)
        self.hint_calls.append(agent_id)
        return {"tags": [f"tag-{self.seat}"], "entries": [f"{self.seat:0<12}"[:12]]}


def _seat_table(rig: Rig) -> tuple[dict[str, SeatBrain], dict[str, Agent], Supervisor]:
    brains = {
        "a1": SeatBrain(rig.world, "a1"),
        "a2": SeatBrain(rig.world, "a2", tool_names=("speak", "sleep")),  # a different tool list: no compact_context
        "a3": SeatBrain(rig.world, "a3", hooks=False),  # a brain of another kind: no hooks at all
        MODERATOR: SeatBrain(rig.world, MODERATOR, hooks=False),
    }
    agents = {
        seat: Agent(
            seat, "moderator" if seat == MODERATOR else "player", rig.world, brain, rig.llm, rig.usage, rig.trace, rig.sink,
            fast_config(), book=rig.book,
        )
        for seat, brain in brains.items()
    }
    supervisor = Supervisor(
        rig.world, agents, rig.sink, config=fast_supervisor_config(), usage=rig.usage, trace=rig.trace, book=rig.book
    )
    return brains, agents, supervisor


@async_test()
async def test_every_seat_has_its_own_brain_and_nothing_is_mixed_up_between_them():
    rig = Rig()
    brains, agents, supervisor = _seat_table(rig)
    for seat in ("a1", "a2", "a3"):
        rig.llm.script(seat, speak_resp(f"I am {seat}"), sleep_resp(), sleep_resp())
    rig.llm.script(MODERATOR, sleep_resp(), sleep_resp())
    supervisor.start()
    try:
        for seat in ("a1", "a2", "a3"):
            rig.world.push(seat, normal("message.new", label=f"{seat}-m1", channel_id="pub"))
        await wait_until(lambda: all(agents[s].stats["runs"] == 1 for s in ("a1", "a2", "a3")))

        # every conversation was built by its own brain: own system prompt, own tools, own opening message
        for seat in ("a1", "a2", "a3"):
            conversation = rig.book.get(seat)
            assert conversation.system == f"SYSTEM[{seat}] own prompt of {seat}"
            assert conversation.messages[0]["content"].startswith(f"START[{seat}]")
            assert len(brains[seat].starts) == 1 and brains[seat].starts[0].agent_id == seat
        assert [t["function"]["name"] for t in rig.book.get("a1").tools] == ["speak", "sleep", "compact_context"]
        assert [t["function"]["name"] for t in rig.book.get("a2").tools] == ["speak", "sleep"]
        assert rig.book.get("a1").tools_sig != rig.book.get("a2").tools_sig
        assert MODERATOR not in rig.book.agents() and brains[MODERATOR].starts == []  # nobody woke the moderator

        # every tool call went to the brain of the seat that made it, and the world saw that seat as the actor
        for seat in ("a1", "a2", "a3"):
            assert [e["agent_id"] for e in brains[seat].executed] == [seat, seat]  # its speak and its sleep
        assert sorted(i.actor_id for i in rig.world.submitted) == ["a1", "a2", "a3"]

        # a later wake of ONE seat goes to that seat's brain only (a delta), the others are not asked
        rig.world.push("a2", normal("message.new", label="a2-m2", channel_id="pub"))
        await wait_until(lambda: agents["a2"].stats["runs"] == 2)
        assert len(brains["a2"].injects) == 1 and brains["a1"].injects == [] and brains["a3"].injects == []
        assert brains["a2"].injects[0].agent_id == "a2"
    finally:
        await supervisor.stop()
    assert all(brain.foreign == [] for brain in brains.values()), {s: b.foreign for s, b in brains.items()}


@async_test()
async def test_each_seats_manifests_are_traced_under_its_own_id_and_a_seat_without_hooks_writes_none():
    rig = Rig()
    brains, agents, supervisor = _seat_table(rig)
    for seat in ("a1", "a2", "a3"):
        rig.llm.script(seat, sleep_resp(), sleep_resp())
    supervisor.start()
    try:
        for seat in ("a1", "a2", "a3"):
            rig.world.push(seat, normal("message.new", label=f"{seat}-m1", channel_id="pub"))
        await wait_until(lambda: all(agents[s].stats["runs"] == 1 for s in ("a1", "a2", "a3")))
        for seat in ("a1", "a2", "a3"):  # a second wake: a DELTA
            rig.world.push(seat, normal("message.new", label=f"{seat}-m2", channel_id="pub"))
        await wait_until(lambda: all(agents[s].stats["runs"] == 2 for s in ("a1", "a2", "a3")))
    finally:
        await supervisor.stop()

    for seat in ("a1", "a2"):
        [mounts] = rig.trace.of("mounts", seat)
        assert mounts["blocks"] == [{"id": f"core.{seat}", "layer": "L0", "chars": 12, "sha": f"{seat:0<12}"[:12]}]
        opening, delta = rig.trace.of("hint", seat)  # one after the opening message, one after the delta
        for hint in (opening, delta):
            assert hint["tags"] == [f"tag-{seat}"] and hint["entries"] == [f"{seat:0<12}"[:12]]
        assert (opening["gen"], delta["gen"]) == (1, 2)
        assert brains[seat].mount_calls == [seat] and brains[seat].hint_calls == [seat, seat]
    assert rig.trace.of("mounts", "a3") == [] and rig.trace.of("hint", "a3") == []  # no hooks: no events, no errors
    assert rig.trace.of("error", "a3") == []
    assert [r[1] for r in rig.trace.records if r[0] == "mounts"] == ["a1", "a2"]


@async_test()
async def test_one_seat_rebuilding_its_conversation_touches_no_other_seat():
    rig = Rig()
    brains, agents, supervisor = _seat_table(rig)
    rig.llm.script("a1", llm_resp(("compact_context", {"summary_markdown": "摘要"})), sleep_resp())
    rig.llm.script("a3", speak_resp("hello"), sleep_resp())
    supervisor.start()
    try:
        rig.world.push("a3", normal("message.new", label="x", channel_id="pub"))
        await wait_until(lambda: agents["a3"].stats["runs"] == 1)
        before = list(rig.book.get("a3").messages)
        rig.world.push("a1", normal("message.new", label="y", channel_id="pub"))
        await wait_until(lambda: agents["a1"].stats["runs"] == 1)
    finally:
        await supervisor.stop()
    assert rig.book.get("a1").epoch == 2 and rig.book.get("a3").epoch == 1
    assert brains["a1"].resets == ["a1"] and brains["a3"].resets == []  # only the brain of the rebuilt seat was reset
    assert rig.book.get("a3").messages == before  # the other conversation was not touched
    assert [r["epoch"] for r in rig.trace.of("mounts", "a1")] == [1, 2]
    assert all(brain.foreign == [] for brain in brains.values())
