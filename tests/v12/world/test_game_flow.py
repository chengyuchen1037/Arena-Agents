"""Randomness, read-only helpers, hold / end game, and agent memory (contracts §2.3)."""

from __future__ import annotations

import pytest

from app.world.core import WorldCore
from app.world.game import GameModule
from app.world.state import WorldState
from app.world.types import ENGINE_ID, MODERATOR_ID, Code, LedgerEntry

from .test_game_kit import (
    PLAYER_IDS,
    ManualClock,
    ask,
    deliveries,
    entries,
    events,
    make_world,
    mod,
    to,
)


def restore(world: WorldCore, *, running: bool = True) -> WorldCore:
    """Rebuild the world from ``dump()`` the way ``World.load`` does."""
    data = world.dump()
    clone = WorldCore(
        world.spec,
        ManualClock(data["clock_now"]),
        state=WorldState.model_validate(data["state"]),
        ledger=[LedgerEntry.model_validate(e) for e in data["ledger"]],
    )
    game = GameModule(clone)
    clone.add_module(game)
    clone.game = game  # type: ignore[attr-defined]
    game.load(data["modules"]["game"])
    if running:
        clone.set_running(True, "restored")
    return clone


# --------------------------------------------------------------------------- random


def test_random_int_choice_and_shuffle_are_recorded_for_the_moderator_only():
    world = make_world(seed=3)
    rolled = mod(world, "random", kind="int", lo=1, hi=6)
    assert rolled.ok and 1 <= rolled.data["result"] <= 6

    picked = mod(world, "random", kind="choice", items=["a", "b", "c", "d"], k=2)
    assert len(picked.data["result"]) == 2 and len(set(picked.data["result"])) == 2
    assert set(picked.data["result"]) <= {"a", "b", "c", "d"}

    shuffled = mod(world, "random", kind="shuffle", items=[1, 2, 3, 4, 5])
    assert sorted(shuffled.data["result"]) == [1, 2, 3, 4, 5]

    drawn = entries(world, "random.drawn")
    assert [e.data["kind"] for e in drawn] == ["int", "choice", "shuffle"]
    assert drawn[0].data == {"kind": "int", "args": {"lo": 1, "hi": 6}, "result": rolled.data["result"]}
    assert drawn[1].data["args"] == {"items": ["a", "b", "c", "d"], "k": 2}
    assert drawn[2].data["args"] == {"items": [1, 2, 3, 4, 5]}
    for entry in drawn:
        assert entry.actor_id == MODERATOR_ID and entry.visible_to_ids == [MODERATOR_ID]
    for player_id in PLAYER_IDS:
        assert [e for e in world.entries_visible(player_id) if e.type == "random.drawn"] == []


def test_random_is_reproducible_for_the_same_seed():
    def draws(seed: int) -> list:
        world = make_world(seed=seed)
        out = [mod(world, "random", kind="int", lo=1, hi=1000).data["result"] for _ in range(5)]
        out.append(mod(world, "random", kind="shuffle", items=list(range(10))).data["result"])
        out.append(mod(world, "random", kind="choice", items=list(range(10)), k=3).data["result"])
        return out

    assert draws(42) == draws(42)
    assert draws(42) != draws(43)


def test_random_continues_identically_after_dump_and_load():
    world = make_world(seed=9)
    mod(world, "random", kind="int", lo=1, hi=1000)
    clone = restore(world)

    expected = [mod(world, "random", kind="int", lo=1, hi=10**6).data["result"] for _ in range(4)]
    again = [mod(clone, "random", kind="int", lo=1, hi=10**6).data["result"] for _ in range(4)]
    assert expected == again
    shuffled = mod(world, "random", kind="shuffle", items=list(range(8))).data["result"]
    assert mod(clone, "random", kind="shuffle", items=list(range(8))).data["result"] == shuffled


def test_random_payload_errors_come_from_the_gate():
    world = make_world()
    seq = world.seq
    assert mod(world, "random", kind="int", lo=5, hi=1).code == Code.INVALID_PARAMS
    assert mod(world, "random", kind="choice", items=["a"], k=2).code == Code.INVALID_PARAMS
    assert world.seq == seq


# --------------------------------------------------------------------------- evaluate / get_ledger


def test_evaluate_runs_a_check_and_converts_players_to_ids():
    world = make_world()
    receipt = mod(world, "evaluate", check_id="red_alive")
    assert receipt.ok and receipt.data["value"] == 1 and receipt.seq is None
    blue = mod(world, "evaluate", check_id="blue_ids")
    assert sorted(blue.data["value"]) == sorted(p.id for p in world.state.players if p.faction == "blue")

    player_out = next(p for p in world.state.players if p.faction == "red")
    mod(world, "update_player", player_id=player_out.id, add_tags=["out"])
    assert mod(world, "evaluate", check_id="red_alive").data["value"] == 0


def test_evaluate_is_read_only_and_reports_errors():
    world = make_world()
    seq = world.seq
    assert mod(world, "evaluate", check_id="nope").code == Code.INVALID_TARGET
    assert mod(world, "evaluate", check_id="broken").code == Code.INTERNAL_ERROR
    mod(world, "evaluate", check_id="red_alive")
    assert world.seq == seq


def test_get_ledger_returns_only_what_the_moderator_can_see():
    world = make_world()
    mod(world, "open_request", to=["u1", "u2"], kind="text", question="秘密？", sealed=True)
    ask(world, "u1", "respond", request_id="r1", value="机密答案")
    mod(world, "random", kind="int", lo=1, hi=3)
    ask(world, "u1", "challenge", content="质询内容")

    receipt = mod(world, "get_ledger", limit=300)
    assert receipt.ok and receipt.seq is None
    rows = receipt.data["entries"]
    types = [row["type"] for row in rows]
    assert "request.answered" not in types  # sealed answers are not visible to anybody
    assert "机密答案" not in str(rows)
    assert {"request.opened", "random.drawn", "message"} <= set(types)
    # every row is a plain dict with the documented keys, and only entries visible to the moderator
    visible = {e.seq for e in world.entries_visible(MODERATOR_ID)}
    assert all(set(row) == {"seq", "type", "channel_id", "actor_id", "data"} for row in rows)
    assert {row["seq"] for row in rows} == visible
    assert receipt.data["count"] == len(rows) and receipt.data["omitted_older"] == 0


def test_get_ledger_filters_and_limits():
    world = make_world()
    for i in range(5):
        ask(world, "u1", "challenge", content=f"问题 {i}")
    mod(world, "random", kind="int", lo=1, hi=2)

    only_messages = mod(world, "get_ledger", types=["message"], limit=3).data
    assert only_messages["count"] == 3 and only_messages["omitted_older"] == 2
    assert [r["data"]["content"] for r in only_messages["entries"]] == ["问题 2", "问题 3", "问题 4"]

    by_channel = mod(world, "get_ledger", channel_id="dm-u1").data["entries"]
    assert {r["channel_id"] for r in by_channel} == {"dm-u1"} and len(by_channel) == 5

    last = world.ledger[-1].seq
    after = mod(world, "get_ledger", after_seq=last - 1).data["entries"]
    assert [r["seq"] for r in after] == [last]

    prefix = mod(world, "get_ledger", types=["player.", "random*"]).data["entries"]
    assert {r["type"] for r in prefix} == {"player.updated", "random.drawn"}


def test_get_ledger_returns_copies():
    world = make_world()
    ask(world, "u1", "challenge", content="原文")
    rows = mod(world, "get_ledger", types=["message"]).data["entries"]
    rows[0]["data"]["content"] = "被改写"
    assert entries(world, "message")[0].data["content"] == "原文"


# --------------------------------------------------------------------------- hold / end


def test_hold_game_closes_the_gate_and_tells_the_engine():
    world = make_world()
    receipt = mod(world, "hold_game", reason="需要人工确认")
    assert receipt.ok
    assert world.running is False and world.state.status == "paused"

    status = entries(world, "game.status")[-1]
    assert status.data == {"status": "paused", "reason": "hold:需要人工确认"}
    assert sorted(status.visible_to_ids) == sorted([MODERATOR_ID, *PLAYER_IDS])
    assert [e.data for e in events(world, "game.status")] == [
        {"status": "paused", "reason": "hold:需要人工确认", "winner": None}
    ]

    items = deliveries(world)
    engine = to(items, ENGINE_ID, "hold_game")
    assert len(engine) == 1 and engine[0].envelope.kind == "notice"
    assert engine[0].envelope.payload == {"event": "hold_game", "reason": "需要人工确认"}

    refused = ask(world, "u1", "challenge", content="还能说话吗")
    assert refused.code == Code.GAME_NOT_RUNNING
    world.set_running(True, "resumed")
    assert ask(world, "u1", "challenge", content="恢复了").ok


def test_hold_game_without_a_reason():
    world = make_world()
    assert mod(world, "hold_game").ok
    assert entries(world, "game.status")[-1].data["reason"] == "hold"


def test_end_game_finishes_closes_the_gate_and_can_be_continued():
    world = make_world()
    receipt = mod(world, "end_game", winner="蓝队", summary="蓝队完成了目标")
    assert receipt.ok
    assert world.running is False
    assert (world.state.status, world.state.winner, world.state.summary) == ("finished", "蓝队", "蓝队完成了目标")

    statuses = [e for e in entries(world, "game.status") if e.data["status"] == "finished"]
    assert len(statuses) == 1  # the gate closing does not write a second entry
    assert statuses[0].data == {
        "status": "finished",
        "reason": "end_game",
        "winner": "蓝队",
        "summary": "蓝队完成了目标",
    }
    assert sorted(statuses[0].visible_to_ids) == sorted([MODERATOR_ID, *PLAYER_IDS])
    ui = [e for e in events(world, "game.status")]
    assert [e.data["status"] for e in ui] == ["finished"]
    assert ui[0].data["winner"] == "蓝队" and ui[0].data["summary"] == "蓝队完成了目标"

    items = deliveries(world)
    engine = to(items, ENGINE_ID, "end_game")
    assert len(engine) == 1
    assert engine[0].envelope.payload == {"event": "end_game", "winner": "蓝队", "summary": "蓝队完成了目标"}

    assert mod(world, "set_phase", text="还有人吗").code == Code.GAME_NOT_RUNNING

    world.set_running(True, "continued")
    assert world.running and world.state.status == "running"
    assert entries(world, "game.status")[-1].data == {"status": "running", "reason": "continued"}
    assert (world.state.winner, world.state.summary) == (None, "")  # the verdict is withdrawn (D8)
    assert mod(world, "set_phase", text="继续").ok

    # the game can be finished again with a new verdict
    assert mod(world, "end_game", winner="红队", summary="第二次终局").ok
    assert (world.state.status, world.state.winner) == ("finished", "红队")


# --------------------------------------------------------------------------- memory


def test_notes_overwrite_append_and_format():
    world = make_world()
    game = world.game
    assert game.read_notes_text("u1") == "（当前你的私人笔记本为空，可调用 write_note 记录关键推理与信息）"

    assert game.write_note("u1", "怀疑名单", "u2 发言矛盾") == "u2 发言矛盾"
    assert game.write_note("u1", "怀疑名单", "u3 也可疑", mode="append") == "u2 发言矛盾\nu3 也可疑"
    assert game.write_note("u1", "计划", "  先观察  ") == "先观察"
    assert game.write_note("u1", "", "无标题内容") == "无标题内容"
    assert game.write_note("u1", "怀疑名单", "全部作废") == "全部作废"  # overwrite replaces

    assert game.read_notes_text("u1") == (
        "#### 📌 怀疑名单\n全部作废\n\n#### 📌 计划\n先观察\n\n#### 📌 默认笔记\n无标题内容"
    )
    assert game.read_notes_text("u2").startswith("（当前你的私人笔记本为空")  # notes are private per agent


def test_append_to_a_missing_note_creates_it():
    world = make_world()
    assert world.game.write_note("u1", "新", "第一行", mode="append") == "第一行"


def test_memory_persists_through_dump_and_load():
    world = make_world()
    world.game.write_note(MODERATOR_ID, "流程", "第 2 步")
    world.game.compact("u1", "到目前为止的摘要", 2)
    clone = restore(world)

    assert clone.game.read_notes_text(MODERATOR_ID) == "#### 📌 流程\n第 2 步"
    memory = clone.game.memory("u1")
    assert memory.summary == "到目前为止的摘要" and memory.compactions == 1
    assert memory.summary_upto_seq == world.game.memory("u1").summary_upto_seq


def test_compact_keeps_only_the_newest_visible_entries_unsummarized():
    world = make_world()
    for i in range(6):
        mod(world, "set_phase", text=f"阶段 {i}")  # 6 entries visible to u1
    visible = [e.seq for e in world.entries_visible("u1")]
    assert len(visible) == 8  # u1's deal entry, the game start status and the six phases

    info = world.game.compact("u1", "摘要一", keep_recent_count=3)
    assert info["ok"] is True and info["compactions"] == 1 and info["kept_entries"] == 3
    memory = world.game.memory("u1")
    assert memory.summary == "摘要一" and memory.summary_upto_seq == visible[-4]
    assert [e.seq for e in world.entries_visible("u1", after_seq=memory.summary_upto_seq)] == visible[-3:]

    # a second compaction can only move the cut forward
    mod(world, "set_phase", text="阶段 6")
    again = world.game.compact("u1", "摘要二", keep_recent_count=100)
    assert again["summary_upto_seq"] == memory.summary_upto_seq
    assert world.game.memory("u1").summary == "摘要二" and again["compactions"] == 2

    everything = world.game.compact("u1", "摘要三", keep_recent_count=0)
    assert everything["summary_upto_seq"] == world.entries_visible("u1")[-1].seq and everything["kept_entries"] == 0


def test_compact_is_per_agent_and_rejects_an_empty_summary():
    world = make_world()
    mod(world, "set_phase", text="阶段")
    refused = world.game.compact("u1", "   ")
    assert refused["ok"] is False and world.game.memory("u1").compactions == 0
    world.game.compact("u1", "只属于 u1", 1)
    assert world.game.memory("u2").summary == "" and world.game.memory("u2").summary_upto_seq == 0


@pytest.mark.parametrize("keep", [None, 10])
def test_compact_default_keep_count_is_ten(keep):
    world = make_world()
    for i in range(12):
        mod(world, "set_phase", text=f"阶段 {i}")
    info = world.game.compact("u1", "摘要") if keep is None else world.game.compact("u1", "摘要", keep)
    assert info["keep_recent_count"] == 10 and info["kept_entries"] == 10


def test_game_module_board_wrapper_delegates_to_build_board():
    world = make_world()
    board = world.game.board("u1")
    assert board["me"]["id"] == "u1" and board["me"]["is_moderator"] is False
