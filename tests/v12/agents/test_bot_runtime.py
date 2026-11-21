"""A v0.14 bot inside the real runtime: the real world (the minimal scenario), the real ``Agent`` / conversation, a scripted model.

The model is scripted per call, so what it "writes" is its own: the point is what the platform does with it - sends it back with a
receipt (the output gate, the length limit of the world) or lets it through exactly as written - and what the model is shown."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agents.mind import BotBrain
from app.runtime.agent import Agent
from app.runtime.clock import ManualClock
from app.runtime.conversation import ConversationBook
from app.world.loader import load_scenario_v2
from app.world.types import MODERATOR_ID, Intent
from app.world.world import World

from ..runtime.fakes import FakeLlm, RecordingSink, RecordingTrace, async_test, fast_config, llm_resp, running_agent, sleep_resp, wait_until
from .bot_fakes import contract, make_mind

ROOT = Path(__file__).resolve().parents[3]
PLAYERS = [{"id": f"u{i}", "seat": str(i), "name": f"玩家{i}"} for i in range(1, 5)]
IDS = [p["id"] for p in PLAYERS]
LISTY = "我的看法：\n1. 他跳得太快\n2. 票也不对\n所以我投他。"
PROSE = "我觉得二号有问题，他刚才跳得太快，票也对不上，所以这轮我投他。"


def moderate(world: World, type_: str, **payload: Any) -> None:
    receipt = world.submit(Intent(intent_id="m", actor_id=MODERATOR_ID, type=type_, payload=payload))
    assert receipt.ok, receipt


def build(limit: int | None = None) -> tuple[World, BotBrain, FakeLlm, Agent, RecordingTrace]:
    """A game of four seats with a public channel whose floor is with ``u1`` (one message, 120 characters)."""
    spec = load_scenario_v2(ROOT / "scenarios" / "minimal_test.md")
    world = World.new_game(spec, PLAYERS, ManualClock(), seed=3)
    world.set_running(True, "start")
    moderate(world, "mod.create_channel", channel_id="public", name="公共频道", members=IDS, type="public")
    moderate(world, "mod.set_floor_queue", channel_id="public", order=IDS, limits={"max_messages": 1})
    brain = BotBrain(world, spec, "u1", make_mind(name="玩家1"), contract())
    llm = FakeLlm()
    trace = RecordingTrace()
    agent = Agent("u1", "player", world, brain, llm, None, trace, RecordingSink(), fast_config(), book=ConversationBook())
    return world, brain, llm, agent, trace


def deliver(world: World, agent: Agent) -> None:
    for delivery in world.drain_outbox():
        if delivery.target == "u1":
            agent.deliver(delivery.envelope)


def spoken(world: World) -> list[str]:
    return [e.data["content"] for e in world.ledger if e.type == "message" and e.actor_id == "u1"]


def tool_results(call: Any) -> list[dict[str, Any]]:
    return [json.loads(m["content"]) for m in call.messages if m.get("role") == "tool"]


@async_test()
async def test_a_report_is_sent_back_once_and_the_bot_says_it_again_in_its_own_words() -> None:
    world, brain, llm, agent, trace = build()
    llm.script(
        "u1",
        llm_resp(("speak", {"channel_id": "public", "content": LISTY}), thought="先列几点"),
        llm_resp(("speak", {"channel_id": "public", "content": PROSE}), thought="改成口语"),
        sleep_resp(),
    )
    async with running_agent(agent):
        deliver(world, agent)
        await wait_until(lambda: agent.stats["runs"] == 1, message="the run ends")
    calls = llm.calls_of("u1")
    assert len(calls) == 3
    # the report never reached the channel; what did is exactly what the model wrote the second time
    assert spoken(world) == [PROSE]
    first, second = tool_results(calls[1])[0], tool_results(calls[2])[-1]
    assert first["ok"] is False and first["code"] == "FORMAT" and first["data"]["rule"] == "list" and "没有发出，发言权还在" in first["message"]
    assert second["ok"] is True and second["seq"]
    # one conversation: the same system prompt and tools in every request, one message after the other
    assert len({c.messages[0]["content"] for c in calls}) == 1 and calls[0].messages[0]["content"] == brain.system_prompt("u1")
    assert len({json.dumps(c.tools, ensure_ascii=False) for c in calls}) == 1
    assert [m["content"] for m in calls[1].messages[:2]] == [m["content"] for m in calls[0].messages[:2]]  # (the prefix grew, it did not change)
    opening = calls[0].messages[1]["content"]
    assert opening.startswith("【状态板】") and "【硬事实台】" not in opening  # (this world has no facts yet: no such section)
    assert "这次只能发 1 条，≤ 120 字" in opening and "总共" not in opening.split("【我的记忆】")[0]
    assert "【此刻的提醒】\n你的说话方式：" in opening and opening.rstrip().endswith("先想清楚此刻最关键的一两件事，再一次说完整。")
    assert "list_documents" not in json.dumps(calls[0].tools) and '"jot"' in json.dumps(calls[0].tools, ensure_ascii=False)


@async_test()
async def test_a_speech_over_the_limit_is_sent_back_by_the_world_and_never_cut() -> None:
    world, brain, llm, agent, _ = build()
    long_text, short_text = "字" * 150, "字" * 100
    llm.script(
        "u1",
        llm_resp(("speak", {"channel_id": "public", "content": long_text})),
        llm_resp(("speak", {"channel_id": "public", "content": short_text})),
        sleep_resp(),
    )
    async with running_agent(agent):
        deliver(world, agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    calls = llm.calls_of("u1")
    refusal = tool_results(calls[1])[0]
    assert refusal["ok"] is False and refusal["code"] == "TOO_LONG" and refusal["data"] == {"chars": 150, "limit": 120}
    assert "truncated" not in refusal and "没有发出，发言权还在" in refusal["message"]
    assert spoken(world) == [short_text]  # the floor was kept: the second try was the bot's own shorter text, whole
    assert "被拒绝 TOO_LONG" in "\n".join(brain._recent) and "成功" in "\n".join(brain._recent)


@async_test()
async def test_the_runtime_records_what_was_mounted_without_a_word_of_it() -> None:
    world, brain, llm, agent, trace = build()
    llm.script("u1", sleep_resp())
    async with running_agent(agent):
        deliver(world, agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    mounts = trace.of("mounts", "u1")
    assert len(mounts) == 1 and mounts[0]["epoch"] == 1 and mounts[0]["gen"] == 1  # (once: when the conversation was created)
    blocks = mounts[0]["blocks"]
    assert [b["id"] for b in blocks][:3] == ["core.identity", "core.voice", "core.mind"] and blocks[-1]["layer"] == "L3"
    assert all(set(b) == {"id", "layer", "chars", "sha"} and len(b["sha"]) == 12 for b in blocks)
    dumped = json.dumps(mounts, ensure_ascii=False)
    for body in ("你只能通过工具影响这个世界", "打了很多年线下局", "发言前先看状态板"):
        assert body not in dumped  # (ids, layers, lengths and hashes: never what the model was told)
    assert {b["id"] for b in blocks} >= {"l2.digest", "l2.playbook", "l1.contract", "l1.notes", "anchor", "l3.board", "l3.wake", "l3.hint"}


@async_test()
async def test_a_notes_jots_and_the_contract_can_be_used_in_the_middle_of_a_turn() -> None:
    world, brain, llm, agent, _ = build()
    llm.script(
        "u1",
        llm_resp(("write_note", {"title": "计划", "content": "先听完再说"}), ("jot", {"text": "二号的票和话对不上"}), ("read_contract", {"part": "回执码"})),
        llm_resp(("speak", {"channel_id": "public", "content": PROSE})),
        sleep_resp(),
    )
    async with running_agent(agent):
        deliver(world, agent)
        await wait_until(lambda: agent.stats["runs"] == 1)
    calls = llm.calls_of("u1")
    first_round = tool_results(calls[1])
    assert [r["ok"] for r in first_round] == [True, True, True] and first_round[1]["count"] == 1
    assert "`TOO_LONG`" in first_round[2]["content"] and first_round[2]["part"] == "回执码"
    assert world.memory("u1").notes["赛后线索"] == "二号的票和话对不上" and world.memory("u1").notes["计划"] == "先听完再说"
    assert spoken(world) == [PROSE]
