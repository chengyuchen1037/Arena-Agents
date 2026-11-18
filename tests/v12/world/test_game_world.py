"""The game module inside the real ``World`` assembly (``World.new_game`` / ``load`` / ``board`` / ``snapshot``)."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from app.world.port import WorldPort
from app.world.types import ENGINE_ID, MODERATOR_ID
from app.world.world import World

from .test_game_kit import PLAYER_IDS, PLAYER_NAMES, ManualClock, ask, choice_options, make_spec, mod


def new_game(seed: int | None = 5, ids: list[str] | None = None) -> World:
    players = [
        {"id": pid, "seat": str(i), "name": PLAYER_NAMES.get(pid, pid), "persona": f"{pid} 的设定"}
        for i, pid in enumerate(ids or PLAYER_IDS, start=1)
    ]
    return World.new_game(make_spec(), players, ManualClock(), seed=seed)


def test_new_game_creates_players_system_dms_and_deals_without_running():
    world = new_game()
    assert isinstance(world, WorldPort)
    assert world.state.status == "idle" and world.running is False
    assert [p.id for p in world.state.players] == PLAYER_IDS
    assert Counter(p.role for p in world.state.players) == Counter({"alpha": 1, "beta": 1, "gamma": 2})
    assert {c.id for c in world.state.channels} == {f"dm-{p}" for p in PLAYER_IDS}  # only the engine's DMs

    deal_entries = [e for e in world.ledger if e.type == "player.updated"]
    assert [e.data["player_id"] for e in deal_entries] == PLAYER_IDS
    assert all(e.actor_id == ENGINE_ID and e.data["reason"] == "deal" for e in deal_entries)
    for p in PLAYER_IDS:  # each player's own board already tells them who they are (D24)
        me = world.board(p)["me"]
        assert me["role"] and me["tags"] and me["brief"]


def test_new_game_is_reproducible_for_a_seed_and_draws_a_stored_seed_without_one():
    one, two = new_game(seed=21), new_game(seed=21)
    assert [p.role for p in one.state.players] == [p.role for p in two.state.players]
    seeds = {new_game(seed=None).state.rng.seed for _ in range(4)}
    assert 0 not in seeds and len(seeds) > 1


def test_new_game_rejects_a_pool_that_does_not_fit():
    with pytest.raises(ValueError):
        new_game(ids=["u1", "u2", "u3"])


def test_a_whole_request_cycle_through_the_world_port():
    world = new_game()
    world.set_running(True, "start")
    assert mod(world, "create_channel", channel_id="public", name="公共", members=PLAYER_IDS, mode="free").ok
    rid = mod(world, "open_request", to=PLAYER_IDS, kind="choice", question="同意吗？", options=choice_options(), sealed=True).data["request_id"]
    for pid in PLAYER_IDS:
        assert ask(world, pid, "respond", request_id=rid, value="yes").ok

    assert world.state.requests[rid].status == "closed"
    board = world.board(MODERATOR_ID)
    assert board["requests"] == [] and {c["id"] for c in board["channels"]} >= {"public", "dm-u1"}
    snap = world.snapshot()
    assert snap["status"] == "running" and [r["status"] for r in snap["requests"]] == ["closed"]
    assert {c["id"] for c in snap["channels"]} == {c.id for c in world.state.channels}
    json.dumps(snap, ensure_ascii=False)


def test_memory_is_delegated_to_the_game_module():
    world = new_game()
    assert world.write_note("u1", "线索", "第一条") == "第一条"
    assert world.read_notes_text("u1") == "#### 📌 线索\n第一条"
    info = world.compact("u1", "摘要", keep_recent_count=1)
    assert info["ok"] is True and world.memory("u1").summary == "摘要"


def test_load_restores_everything_the_game_module_owns_and_comes_back_paused():
    world = new_game()
    world.set_running(True, "start")
    mod(world, "create_channel", channel_id="public", name="公共", members=PLAYER_IDS, mode="free")
    rid = mod(world, "open_request", to=["u1", "u2"], kind="text", question="说说看", sealed=True, duration_s=40).data["request_id"]
    ask(world, "u1", "respond", request_id=rid, value="我先说")
    ask(world, "u3", "challenge", content="我有个问题")
    mod(world, "enable_ability", ability_id="ping")
    ask(world, "u2", "use_ability", ability_id="ping")
    mod(world, "random", kind="int", lo=1, hi=100)
    world.write_note("u4", "记录", "内容")
    world.clock.advance(10)
    data = json.loads(json.dumps(world.dump()))  # through JSON, as a save would be

    clock = ManualClock(data["clock_now"])
    restored = World.load(data, world.spec, clock)
    assert restored.state.status == "paused" and restored.running is False
    assert restored.seq == world.seq
    assert restored.state.requests[rid].answers["u1"].value == "我先说"
    assert restored.state.tickets["t1"].status == "open"
    assert restored.state.abilities["ping"].uses == {"u2": 1}
    assert restored.read_notes_text("u4") == "#### 📌 记录\n内容"
    assert restored.board("u1")["requests"][0]["my_answer"]["value"] == "我先说"

    restored.set_running(True, "resumed")
    restored.drain_outbox()
    restored.redeliver()
    wakes = sorted((d.target, d.envelope.payload["event"]) for d in restored.drain_outbox() if d.envelope.priority == "urgent")
    assert wakes == [(MODERATOR_ID, "challenge.opened"), ("u2", "request.opened")]

    assert [restored.rng.random() for _ in range(3)] == [world.rng.random() for _ in range(3)]
    assert ask(restored, "u2", "respond", request_id=rid, value="我也说").ok  # closes after the restore
    assert restored.state.requests[rid].status == "closed"
    restored.clock.advance(1)
    restored.tick()  # nothing left to expire
    assert world.state.requests[rid].status == "open"  # the original object is untouched
