"""The assembled ``World`` (kernel + chat + game): new game, dealing, a whole round, save / load."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from app.world.board import build_board
from app.world.port import WorldPort
from app.world.projection import world_snapshot
from app.world.types import MODERATOR_ID, Code
from app.world.world import World

from .helpers import (
    ManualClock,
    do_pass,
    entries,
    envelopes,
    flush,
    make_spec,
    mod,
    speak,
    submit,
    targets,
)

IDS = ["p01", "p02", "p03", "p04"]

SETUP = {
    "identity_pool": [{"role": "lead", "count": 1}, {"role": "crew", "count": 3}],
    "roles": {
        "lead": {"faction": "red", "tags": ["active", "red"], "attributes": {"lives": 2}, "brief": "你是领队。"},
        "crew": {"faction": "blue", "tags": ["active"], "attributes": {}, "brief": "你是队员。"},
    },
    "public_tags": ["active"],
}
ABILITIES = {
    "flare": {
        "holders": "${alive(by_role('lead'))}",
        "quota": 1,
        "freeze_channels": ["public"],
        "reveal": {"channel_id": "public", "text": "{{actor}}亮出了信号。"},
        "description": "亮出信号。",
    }
}


def spec():
    return make_spec(setup=SETUP, abilities=ABILITIES, ui={"out_tags": ["out"]})


def players() -> list[dict[str, Any]]:
    return [{"id": pid, "seat": str(i), "name": f"玩家{i}", "persona": f"性格{i}"} for i, pid in enumerate(IDS, start=1)]


def new_world(seed: int | None = 11, *, running: bool = True) -> World:
    world = World.new_game(spec(), players(), ManualClock(), seed=seed)
    if running:
        world.set_running(True, "start")
    flush(world)
    return world


def lead(world: World) -> str:
    return next(p.id for p in world.state.players if p.role == "lead")


# --------------------------------------------------------------------------- new_game


def test_a_new_game_is_idle_dealt_and_has_a_system_dm_per_player() -> None:
    world = World.new_game(spec(), players(), ManualClock(), seed=5)
    assert isinstance(world, WorldPort)
    assert world.state.status == "idle" and not world.running and world.seq == len(world.ledger) == 8
    assert [p.id for p in world.state.players] == IDS
    assert [(p.seat, p.name, p.persona) for p in world.state.players][1] == ("2", "玩家2", "性格2")
    assert sorted(p.role for p in world.state.players) == ["crew", "crew", "crew", "lead"]
    assert all(p.tags and p.faction for p in world.state.players)
    assert [c.id for c in world.state.channels] == [f"dm-{pid}" for pid in IDS]
    # the system DMs come first, then the deal (D23 / D24)
    assert [e.type for e in world.ledger] == ["channel.created"] * 4 + ["player.updated"] * 4
    dealt = entries(world, "player.updated")[0]
    assert dealt.visible_to_ids == [dealt.data["player_id"], MODERATOR_ID] and dealt.data["reason"] == "deal"
    assert world.state.rng.seed == 5
    # nothing is accepted before the game starts
    assert speak(world, "p01", "dm-p01", "hi").code == Code.GAME_NOT_RUNNING
    assert world.drain_outbox() == []  # nobody is told anything at creation


def test_the_seed_makes_the_deal_reproducible() -> None:
    def deal(seed: int) -> list[str]:
        return [p.role for p in World.new_game(spec(), players(), ManualClock(), seed=seed).state.players]

    assert deal(3) == deal(3)
    assert any(deal(seed) != deal(3) for seed in range(4, 30))
    seeds = {World.new_game(spec(), players(), ManualClock()).state.rng.seed for _ in range(5)}
    assert 0 not in seeds and len(seeds) > 1  # without a seed one is drawn, stored in the save


def test_new_game_rejects_bad_player_lists() -> None:
    clock = ManualClock()
    with pytest.raises(ValueError, match="身份池"):
        World.new_game(spec(), players()[:3], clock, seed=1)  # the pool has 4 identities
    with pytest.raises(ValueError, match="重复"):
        World.new_game(spec(), [*players()[:3], {"id": "p01"}], clock, seed=1)
    with pytest.raises(ValueError, match="保留"):
        World.new_game(spec(), [*players()[:3], {"id": "moderator"}], clock, seed=1)
    with pytest.raises(ValueError, match="缺少 id"):
        World.new_game(spec(), [*players()[:3], {"name": "无名"}], clock, seed=1)


def test_missing_player_fields_get_sensible_defaults() -> None:
    world = World.new_game(make_spec(), [{"id": "a"}, {"id": "b", "name": "乙"}], ManualClock(), seed=1)
    assert [(p.id, p.seat, p.name, p.persona) for p in world.state.players] == [("a", "1", "a", ""), ("b", "2", "乙", "")]


# --------------------------------------------------------------------------- the WorldPort surface


def test_the_world_port_delegations_work() -> None:
    world = new_world()
    board = world.board("p01")
    assert board["me"]["role"] == world.player("p01").role and board["status"] == "running"
    assert world.board(MODERATOR_ID)["me"]["is_moderator"] is True
    assert world.write_note("p01", "线索", "第一条") == "第一条"
    assert world.write_note("p01", "线索", "第二条", "append") == "第一条\n第二条"
    assert "线索" in world.read_notes_text("p01")
    assert world.memory("p01").notes == {"线索": "第一条\n第二条"}
    assert speak(world, "p01", "dm-p01", "一").ok and speak(world, "p01", "dm-p01", "二").ok
    result = world.compact("p01", "摘要", keep_recent_count=1)
    assert result["ok"] and world.memory("p01").summary == "摘要" and world.memory("p01").summary_upto_seq > 0
    assert world.display_name("p03") == "玩家3" and world.display_name(MODERATOR_ID) == "主持人"
    snapshot = world.snapshot()
    assert snapshot == world_snapshot(world)
    assert [c["id"] for c in snapshot["channels"]] == [f"dm-{pid}" for pid in IDS]
    assert snapshot["status"] == "running" and len(snapshot["messages"]) == 2


# --------------------------------------------------------------------------- a whole round


def test_a_queue_round_from_opening_to_the_moderators_wake_up() -> None:
    world = new_world()
    assert mod(world, "create_channel", channel_id="public", name="公共", members=IDS, type="public").ok
    assert mod(world, "set_phase", text="发言阶段").ok
    assert mod(world, "announce", channel_id="public", template_id="notice", params={"text": "依次发言"}).ok
    assert mod(world, "set_floor_queue", channel_id="public", order=IDS, limits={"duration_s": 30}).ok
    flush(world)

    # p01 speaks, p02 waits, p01 passes, p02 speaks twice
    assert speak(world, "p02", "public", "我先说").code == Code.NO_FLOOR
    assert speak(world, "p01", "public", "我是第一个").ok
    assert do_pass(world, "p01", "public").ok
    assert speak(world, "p02", "public", "第二个").ok and speak(world, "p02", "public", "再补一句").ok
    deliveries, events = flush(world)
    assert targets(deliveries, "floor.granted") == ["p02", "p03"]
    assert [e["floor"]["active"] for e in [ev.data for ev in events if ev.type == "floor.changed"]][-1] == "p03"

    # p03 runs out of time, p04 uses its quota
    world.clock.advance(31)
    world.tick()
    assert speak(world, "p04", "public", "最后一个").ok and speak(world, "p04", "public", "完毕").ok
    deliveries, _ = flush(world)
    [done] = envelopes(deliveries, target=MODERATOR_ID, event="floor.completed")
    assert done.priority == "urgent" and done.channel_id == "public"
    assert [(r["holder"], r["reason"]) for r in done.payload["results"]] == [
        ("p01", "pass"),
        ("p02", "used"),
        ("p03", "timeout"),
        ("p04", "used"),
    ]
    assert world.channel("public").mode == "closed"

    # the state boards agree with what happened
    block = next(c for c in build_board(world, "p01")["channels"] if c["id"] == "public")
    assert block["can_write"] is False and block["why_not"].startswith("CHANNEL_CLOSED")
    assert block["floor"]["kind"] == "none"
    mine = build_board(world, "p02")["me"]
    assert mine["role"] == world.player("p02").role
    assert "role" not in next(p for p in build_board(world, "p01")["players"] if p["id"] == "p02")  # others' roles are hidden
    # the visible history of a player holds the round (and not the private DMs of others)
    assert speak(world, "p02", "dm-p02", "私下问一句").ok
    heard = [e.data["content"] for e in world.entries_visible("p01") if e.type == "message"]
    assert "我是第一个" in heard and "私下问一句" not in heard


def test_a_round_with_a_free_discussion_a_challenge_and_an_ability() -> None:
    world = new_world()
    assert mod(world, "create_channel", channel_id="public", name="公共", members=IDS, type="public").ok
    assert mod(world, "open_floor", channel_id="public", mode="free", free_limits={"duration_s": 100, "silence_s": 500}).ok
    assert speak(world, "p01", "public", "自由讨论开始").ok
    # a challenge goes to the moderator through the system DM (not limited by the floor)
    assert submit(world, "p02", "challenge", {"content": "为什么不先投票？"}).ok
    deliveries, _ = flush(world)
    [wake] = envelopes(deliveries, target=MODERATOR_ID, event="challenge.opened")
    assert wake.priority == "urgent" and wake.channel_id == "dm-p02"
    # the ability freezes the free discussion (D25) and wakes the moderator
    assert mod(world, "enable_ability", ability_id="flare").ok
    assert submit(world, lead(world), "use_ability", {"ability_id": "flare", "params": {}}).ok
    assert world.channel("public").frozen
    assert speak(world, "p03", "public", "我还想说").code == Code.NO_FLOOR
    deliveries, _ = flush(world)
    assert sorted(targets(deliveries, "floor.frozen")) == sorted(IDS)
    assert envelopes(deliveries, target=MODERATOR_ID, event="ability.used")
    # the moderator rules, lifts the freeze and closes the discussion at the time advisory
    assert mod(world, "resolve_challenge", ticket_id="t1", verdict="upheld", reply="成立，已记录。").ok
    assert mod(world, "update_channel", channel_id="public", mode="free").ok
    world.clock.advance(100)
    world.tick()
    deliveries, _ = flush(world)
    [advisory] = envelopes(deliveries, target=MODERATOR_ID, event="free.advisory")
    assert advisory.payload["reason"] == "time" and advisory.priority == "urgent"
    assert world.channel("public").mode == "free"  # never closed by itself
    assert mod(world, "close_floor", channel_id="public").ok
    assert speak(world, "p01", "public", "还能说吗").code == Code.CHANNEL_CLOSED


def test_end_game_and_continuing_it() -> None:
    world = new_world()
    assert mod(world, "end_game", winner="蓝队", summary="好局").ok
    assert world.state.status == "finished" and not world.running and world.state.winner == "蓝队"
    assert speak(world, "p01", "dm-p01", "结束后").code == Code.GAME_NOT_RUNNING
    world.set_running(True, "ui")
    assert world.state.status == "running" and world.state.winner is None
    assert [e.data["reason"] for e in entries(world, "game.status")][-1] == "continued"
    assert speak(world, "p01", "dm-p01", "继续了").ok


# --------------------------------------------------------------------------- save / load


def test_load_rejects_foreign_or_damaged_saves() -> None:
    world = new_world()
    data = json.loads(json.dumps(world.dump()))
    with pytest.raises(ValueError, match="格式"):
        World.load({**data, "format": 99}, spec(), ManualClock())
    broken = json.loads(json.dumps(data))
    del broken["ledger"][2]
    with pytest.raises(ValueError, match="不连续"):
        World.load(broken, spec(), ManualClock())


def test_load_restores_a_paused_world_that_resumes_cleanly() -> None:
    world = new_world()
    assert mod(world, "create_channel", channel_id="public", name="公共", members=IDS, type="public").ok
    assert mod(world, "set_floor_queue", channel_id="public", order=IDS).ok
    assert speak(world, "p01", "public", "存档前").ok
    world.clock.advance(10)
    data = json.loads(json.dumps(world.dump()))  # an autosave taken while running
    assert data["state"]["status"] == "running"
    clock = ManualClock(data["clock_now"])
    restored = World.load(data, spec(), clock)
    assert isinstance(restored, WorldPort)
    assert restored.state.status == "paused" and not restored.running and restored.seq == world.seq
    assert [p.model_dump() for p in restored.state.players] == [p.model_dump() for p in world.state.players]
    assert speak(restored, "p01", "public", "关着门").code == Code.GAME_NOT_RUNNING

    restored.set_running(True, "resume")
    restored.redeliver()
    deliveries, _ = flush(restored)
    [env] = envelopes(deliveries, event="floor.granted")
    assert env.payload["seconds"] == 50.0 and deliveries[0].target == "p01"
    assert speak(restored, "p01", "public", "存档后").ok
    assert restored.board("p01")["me"]["role"] == world.player("p01").role
    # the state board of every agent is what the original world shows (same clock, same facts)
    assert speak(world, "p01", "public", "存档后").ok
    for agent in (MODERATOR_ID, *IDS):
        assert build_board(restored, agent) == build_board(world, agent), agent


def _script() -> list[Callable[[World], Any]]:
    def advance(dt: float) -> Callable[[World], Any]:
        return lambda w: (w.clock.advance(dt), w.tick())

    return [
        lambda w: mod(w, "create_channel", channel_id="public", name="公共", members=IDS, type="public"),
        lambda w: mod(w, "set_phase", text="第一轮"),
        lambda w: mod(w, "random", kind="int", lo=1, hi=100),
        lambda w: mod(w, "set_floor_queue", channel_id="public", order=IDS, limits={"duration_s": 30}),
        lambda w: speak(w, "p01", "public", "我是第一个"),
        lambda w: do_pass(w, "p01", "public"),
        lambda w: speak(w, "p02", "public", "第二个发言"),
        advance(31),  # p02 times out
        lambda w: submit(w, "p03", "challenge", {"content": "是不是该先投票？"}),
        lambda w: mod(w, "random", kind="shuffle", items=IDS),
        lambda w: speak(w, "p03", "public", "我说一句"),
        lambda w: mod(w, "pause_queue", channel_id="public"),
        advance(100),
        lambda w: mod(w, "resume_queue", channel_id="public"),
        lambda w: do_pass(w, "p03", "public"),
        lambda w: speak(w, "p04", "public", "最后一个"),
        lambda w: speak(w, "p04", "public", "说完了"),  # the queue is complete: the moderator is woken
        lambda w: mod(
            w, "open_request", to=IDS, kind="choice", question="继续吗？",
            options=[{"id": "y", "label": "是"}, {"id": "n", "label": "否"}], sealed=True, duration_s=50,
        ),
        lambda w: submit(w, "p01", "respond", {"request_id": "r1", "value": "y"}),
        advance(20),
        lambda w: mod(w, "open_floor", channel_id="public", mode="free", free_limits={"duration_s": 200}),
        lambda w: speak(w, "p02", "public", "自由讨论"),
        advance(60),  # the request closes at its deadline; the discussion goes quiet
        lambda w: mod(w, "resolve_challenge", ticket_id="t1", verdict="upheld", reply="好建议"),
        lambda w: mod(w, "enable_ability", ability_id="flare"),
        lambda w: submit(w, lead(w), "use_ability", {"ability_id": "flare", "params": {}}),
        lambda w: speak(w, "p03", "public", "冻结了吗"),
        lambda w: mod(w, "random", kind="choice", items=IDS, k=2),
        lambda w: mod(w, "update_channel", channel_id="public", mode="free"),
        advance(150),
        lambda w: mod(w, "close_floor", channel_id="public"),
        lambda w: mod(w, "end_game", winner="蓝队", summary="结束"),
    ]


def _facts(world: World) -> list[tuple]:
    return [
        (e.seq, e.type, e.actor_id, e.channel_id, tuple(e.visible_to_ids), json.dumps(e.data, sort_keys=True, ensure_ascii=False))
        for e in world.ledger
    ]


def _bounce(world: World) -> None:
    world.set_running(False, "pause")
    world.set_running(True, "resume")
    world.redeliver()


def _save_and_reload(world: World) -> World:
    world.set_running(False, "pause")
    data = json.loads(json.dumps(world.dump(), ensure_ascii=False))
    restored = World.load(data, spec(), ManualClock(data["clock_now"]))
    restored.set_running(True, "resume")
    restored.redeliver()
    return restored


def test_the_script_really_exercises_both_modules() -> None:
    world = new_world()
    outcomes = [step(world) for step in _script()]
    receipts = [o for o in outcomes if hasattr(o, "ok")]
    assert sum(1 for r in receipts if r.ok) >= 20
    kinds = {e.type for e in world.ledger}
    assert {
        "floor.token_ended", "floor.completed", "floor.paused", "floor.resumed", "floor.frozen", "floor.closed",
        "request.opened", "request.closed", "ability.used", "random.drawn", "phase.changed", "game.status",
    } <= kinds
    assert world.state.status == "finished"


def test_a_world_loaded_in_the_middle_of_a_game_plays_on_exactly_like_the_original() -> None:
    script = _script()
    for cut in range(1, len(script)):
        reference = new_world()
        for step in script[:cut]:
            step(reference)
        _bounce(reference)
        for step in script[cut:]:
            step(reference)

        saved = new_world()
        for step in script[:cut]:
            step(saved)
        resumed = _save_and_reload(saved)
        for step in script[cut:]:
            step(resumed)

        assert _facts(resumed) == _facts(reference), f"the ledger differs when loading after step {cut}"
        left = resumed.state.model_dump(exclude={"rng"})
        right = reference.state.model_dump(exclude={"rng"})
        assert left == right, f"the state differs after step {cut}"
        for agent in (MODERATOR_ID, *IDS):
            assert resumed.board(agent) == reference.board(agent), (cut, agent)
        assert resumed.next_deadline() == reference.next_deadline()
