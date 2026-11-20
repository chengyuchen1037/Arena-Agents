"""Identities are dealt when the game *starts* (v0.14, H52, contracts §13.3).

``World.new_game(..., deal=False)`` creates the players and their system DMs and checks the identity pool but deals
nothing; ``World.deal_identities()`` deals once (``player.updated`` entries with ``reason == "deal"``); ``WorldState.dealt``
says which of the two a world is in, survives a save, and is inferred for saves that predate it.  Until the deal no view
of the world - state board of any viewpoint, god-view snapshot, visible ledger, replay - carries an identity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.game.replay import build_replay
from app.world.loader import load_scenario_v2
from app.world.projection import world_snapshot
from app.world.types import ENGINE_ID, MODERATOR_ID
from app.world.world import World

from .helpers import ManualClock, flush
from .test_game_kit import PLAYER_IDS, PLAYER_NAMES, entries, make_spec
from .test_replay import make_game_spec, payload_of
from .test_replay import players as replay_players

ROOT = Path(__file__).resolve().parents[3]
KIT_PLAYERS = [{"id": pid, "seat": str(i), "name": PLAYER_NAMES[pid]} for i, pid in enumerate(PLAYER_IDS, start=1)]


def new(seed: int = 5, *, deal: bool = False, spec: Any = None, players: list[dict[str, Any]] | None = None) -> World:
    return World.new_game(spec or make_spec(), players or KIT_PLAYERS, ManualClock(), seed=seed, deal=deal)


def roles_of(world: World) -> list[str]:
    return [p.role for p in world.state.players]


def facts(world: World) -> list[tuple[Any, ...]]:
    """The ledger without its wall-clock stamps (``ts`` is the real time of the commit)."""
    return [
        (e.seq, e.type, e.actor_id, e.channel_id, tuple(e.visible_to_ids), json.dumps(e.data, sort_keys=True, ensure_ascii=False))
        for e in world.ledger
    ]


# --------------------------------------------------------------------------- new_game(deal=False)


def test_a_game_created_without_dealing_has_the_dms_and_no_identity_at_all() -> None:
    world = new()
    assert world.state.dealt is False
    assert world.state.status == "idle" and not world.running
    assert [e.type for e in world.ledger] == ["channel.created"] * 4  # the system DMs, nothing else
    assert {c.id for c in world.state.channels} == {f"dm-{pid}" for pid in PLAYER_IDS}
    for p in world.state.players:
        assert (p.role, p.faction, p.tags, p.attributes) == ("", "", [], {})
    assert world.drain_outbox() == []  # nobody is told anything at creation


def test_the_default_still_deals_at_creation_exactly_as_before() -> None:
    world = World.new_game(make_spec(), KIT_PLAYERS, ManualClock(), seed=5)  # no ``deal`` argument
    assert world.state.dealt is True
    assert [e.type for e in world.ledger] == ["channel.created"] * 4 + ["player.updated"] * 4
    assert all(p.role and p.faction and p.tags for p in world.state.players)
    assert all(e.data["reason"] == "deal" for e in entries(world, "player.updated"))
    with pytest.raises(RuntimeError):
        world.deal_identities()


@pytest.mark.parametrize("ids", [PLAYER_IDS[:3], [*PLAYER_IDS, "u5"]])
def test_creating_without_dealing_still_checks_that_the_pool_fits_the_players(ids: list[str]) -> None:
    players = [{"id": pid, "seat": str(i), "name": pid} for i, pid in enumerate(ids, start=1)]
    with pytest.raises(ValueError, match="身份池"):
        World.new_game(make_spec(), players, ManualClock(), seed=1, deal=False)
    with pytest.raises(ValueError, match="身份池"):
        World.new_game(make_spec(), players, ManualClock(), seed=1)  # (the default path says the same)


def test_the_other_checks_of_a_player_list_still_apply_without_dealing() -> None:
    with pytest.raises(ValueError, match="重复"):
        new(players=[*KIT_PLAYERS[:3], {"id": "u1"}])
    with pytest.raises(ValueError, match="保留"):
        new(players=[*KIT_PLAYERS[:3], {"id": "moderator"}])


# --------------------------------------------------------------------------- deal_identities()


@pytest.mark.parametrize("seed", [1, 5, 11, 99])
def test_dealing_at_the_start_gives_exactly_the_deal_of_creation_for_the_same_seed(seed: int) -> None:
    early = new(seed, deal=True)
    late = new(seed)
    result = late.deal_identities()

    assert [(d["player_id"], d["role"], d["faction"]) for d in result] == [
        (p.id, p.role, p.faction) for p in late.state.players
    ]
    assert [p.model_dump() for p in late.state.players] == [p.model_dump() for p in early.state.players]
    assert late.state.dealt is True
    assert facts(late) == facts(early)  # the ledger has the very same shape: the system DMs, then the deal
    assert late.rng.random() == early.rng.random()  # the seeded rng was used the same way


def test_the_deal_entries_are_private_player_updated_entries_written_when_the_deal_happens() -> None:
    world = new()
    assert world.deal_identities() and not [e for e in world.ledger if e.type == "game.status"]
    updates = entries(world, "player.updated")
    assert [e.seq for e in updates] == [5, 6, 7, 8]  # right behind the four system DMs
    for entry, p in zip(updates, world.state.players):
        assert entry.actor_id == ENGINE_ID and entry.channel_id is None
        assert entry.visible_to_ids == [p.id, MODERATOR_ID]
        assert entry.data == {
            "player_id": p.id, "add_tags": p.tags, "remove_tags": [], "set_attributes": p.attributes,
            "tags_after": p.tags, "role": p.role, "faction": p.faction, "reason": "deal",
        }
    # a player reads his own identity only
    for p in world.state.players:
        assert [e.data["player_id"] for e in world.entries_visible(p.id) if e.type == "player.updated"] == [p.id]


def test_when_the_game_is_opened_first_the_deal_entries_come_after_its_game_status() -> None:
    """The order the orchestrator may choose (open the gate, then deal): the ledger shape differs only in position."""
    world = new()
    world.set_running(True, "started")
    world.deal_identities()
    assert [e.type for e in world.ledger] == ["channel.created"] * 4 + ["game.status"] + ["player.updated"] * 4
    assert entries(world, "game.status")[0].data == {"status": "running", "reason": "started"}
    assert all(e.data["reason"] == "deal" for e in entries(world, "player.updated"))
    assert world.state.dealt is True and world.state.status == "running"


def test_dealing_tells_the_spectators_with_player_updated_events_and_nobody_else() -> None:
    world = new()
    world.set_running(True, "started")
    flush(world)
    world.deal_identities()
    deliveries, events = flush(world)
    assert deliveries == []  # an agent reads its identity from its state board when it is first told about the game
    assert [(e.type, e.data["player_id"], e.data["tags"], e.data["out"]) for e in events] == [
        ("player.updated", p.id, p.tags, False) for p in world.state.players
    ]


def test_dealing_twice_is_refused_and_changes_nothing() -> None:
    world = new()
    world.deal_identities()
    before = json.dumps(world.dump(), sort_keys=True)
    with pytest.raises(RuntimeError, match="发过牌"):
        world.deal_identities()
    with pytest.raises(RuntimeError):
        world.game.deal()  # no way around the guard
    assert json.dumps(world.dump(), sort_keys=True) == before
    assert world.drain_events() and not world.drain_outbox()  # (only what the first deal queued)


def test_a_deal_that_does_not_fit_changes_nothing_and_leaves_the_game_undealt() -> None:
    world = new()
    world.state.players.pop()  # three players for a pool of four
    before = json.dumps(world.dump(), sort_keys=True)
    with pytest.raises(ValueError, match="身份池"):
        world.deal_identities()
    assert world.state.dealt is False and roles_of(world) == [""] * 3
    assert json.dumps(world.dump(), sort_keys=True) == before
    assert entries(world, "player.updated") == []


def test_a_scenario_without_an_identity_pool_is_dealt_by_the_call_that_deals_nothing() -> None:
    spec = make_spec(setup={"public_tags": ["active"]})
    world = new(spec=spec)
    assert world.state.dealt is False
    assert world.deal_identities() == [] and world.state.dealt is True
    assert [e.type for e in world.ledger] == ["channel.created"] * 4
    with pytest.raises(RuntimeError):
        world.deal_identities()
    assert new(spec=spec, deal=True).state.dealt is True  # and at creation, as before


# --------------------------------------------------------------------------- nothing leaks before the deal

_REAL = ROOT / "scenarios" / "werewolf_12p.md"


def _identity_words(spec: Any) -> set[str]:
    """Every word of the scenario that tells *who* a player is: role ids, faction ids, identity tags, the private briefs."""
    setup = spec.setup
    words: set[str] = set(setup.roles)
    for role in setup.roles.values():
        words.add(role.faction)
        words.update(t for t in role.tags if t not in setup.public_tags)
        words.add(role.brief)
    return {w for w in words if w}


def _assert_blank_identity(board: dict[str, Any], agent: str) -> None:
    me = board["me"]
    assert (me["role"], me["faction"], me["brief"], me["tags"], me["attributes"]) == ("", "", "", [], {}), agent
    for row in board["players"]:
        assert row["tags"] == [], (agent, row["id"])
        if agent == MODERATOR_ID:
            assert (row["role"], row["faction"], row["attributes"]) == ("", "", {}), row["id"]
        else:
            assert set(row) == {"id", "seat", "name", "tags", "out"}


def _undealt_world(spec: Any, players: list[dict[str, Any]]) -> World:
    world = World.new_game(spec, players, ManualClock(), seed=3, deal=False)
    world.set_running(True, "started")
    flush(world)
    return world


@pytest.mark.parametrize("running", [False, True])
def test_no_view_of_an_undealt_world_carries_an_identity(running: bool) -> None:
    spec = make_spec()
    world = new(spec=spec)
    if running:
        world.set_running(True, "started")
    words = _identity_words(spec)
    assert {"alpha", "beta", "gamma", "red", "blue"} <= words and len(words) >= 8

    for agent in [MODERATOR_ID, *PLAYER_IDS]:
        board = world.board(agent)
        _assert_blank_identity(board, agent)
        text = json.dumps(board, ensure_ascii=False)
        if agent != MODERATOR_ID:
            assert not [w for w in words if w in text], (agent, [w for w in words if w in text])
        assert [e for e in world.entries_visible(agent) if e.type == "player.updated"] == []

    snapshot = world.snapshot()
    assert snapshot == world_snapshot(world)
    assert [p["tags"] for p in snapshot["players"]] == [[]] * 4
    text = json.dumps(snapshot, ensure_ascii=False)
    assert not [w for w in words if w in text], [w for w in words if w in text]


def test_the_same_views_show_the_identities_once_the_deal_is_done() -> None:
    spec = make_spec()
    world = new(spec=spec)
    world.deal_identities()
    for p in world.state.players:
        me = world.board(p.id)["me"]
        assert me["role"] == p.role and me["faction"] == p.faction and me["tags"] == p.tags
        assert me["brief"] == spec.setup.roles[p.role].brief
    moderator = world.board(MODERATOR_ID)
    assert [(r["id"], r["role"], r["faction"]) for r in moderator["players"]] == [
        (p.id, p.role, p.faction) for p in world.state.players
    ]
    assert [p["tags"] for p in world.snapshot()["players"]] == [p.tags for p in world.state.players]


def test_the_werewolf_scenario_shows_no_identity_before_the_deal_in_any_view() -> None:
    spec = load_scenario_v2(_REAL)
    players = [{"id": f"N{i}", "seat": str(i), "name": f"玩家{i}"} for i in range(1, 13)]
    world = _undealt_world(spec, players)
    words = _identity_words(spec)
    assert {"werewolf", "white_wolf", "seer", "witch", "hunter", "guard", "villager", "wolves", "good"} <= words
    assert {"狼人", "白狼王", "平民", "预言家", "女巫", "猎人", "守卫"} <= words

    for agent in [MODERATOR_ID, *(p["id"] for p in players)]:
        board = world.board(agent)
        _assert_blank_identity(board, agent)
        if agent == MODERATOR_ID:
            # (the moderator's list of abilities names the scenario's ability, never a holder)
            assert all(a["holders"] == [] for a in board["abilities"])
        else:
            assert board["abilities"] == []
            text = json.dumps(board, ensure_ascii=False)
            assert not [w for w in words if w in text], (agent, [w for w in words if w in text])
    text = json.dumps(world.snapshot(), ensure_ascii=False)
    assert not [w for w in words if w in text], [w for w in words if w in text]
    assert [p["tags"] for p in world.snapshot()["players"]] == [[]] * 12

    # ... and after the deal every player knows who he is, the moderator knows everybody
    dealt = world.deal_identities()
    assert sorted(d["role"] for d in dealt) == sorted(
        r for item in spec.setup.identity_pool for r in [item.role] * item.count
    )
    for d in dealt:
        me = world.board(d["player_id"])["me"]
        assert me["role"] == d["role"] and me["brief"] == spec.setup.roles[d["role"]].brief and me["tags"]
    assert {p["id"]: p["role"] for p in world.board(MODERATOR_ID)["players"]} == {d["player_id"]: d["role"] for d in dealt}


# --------------------------------------------------------------------------- saving and loading


def test_the_dealt_flag_is_saved_and_a_loaded_undealt_game_deals_like_its_unsaved_twin() -> None:
    spec = make_spec()
    world = new(7)
    data = json.loads(json.dumps(world.dump()))
    assert data["format"] == 1 and data["state"]["dealt"] is False

    loaded = World.load(data, spec, ManualClock(data["clock_now"]))
    assert loaded.state.dealt is False and roles_of(loaded) == [""] * 4
    assert loaded.state.status == "idle" and loaded.seq == world.seq

    twin = new(7)
    twin.deal_identities()
    loaded.deal_identities()
    assert roles_of(loaded) == roles_of(twin) != [""] * 4
    assert facts(loaded) == facts(twin)

    again = json.loads(json.dumps(loaded.dump()))
    assert again["state"]["dealt"] is True
    restored = World.load(again, spec, ManualClock(again["clock_now"]))
    assert restored.state.dealt is True and roles_of(restored) == roles_of(twin)
    with pytest.raises(RuntimeError):
        restored.deal_identities()


def _without_the_key(world: World) -> dict[str, Any]:
    data = json.loads(json.dumps(world.dump()))
    del data["state"]["dealt"]  # what a save of v0.13 and earlier looks like
    return data


def test_a_save_that_predates_the_flag_is_dealt_when_a_player_has_a_role() -> None:
    world = new(deal=True)
    loaded = World.load(_without_the_key(world), make_spec(), ManualClock())
    assert loaded.state.dealt is True and roles_of(loaded) == roles_of(world)
    with pytest.raises(RuntimeError):
        loaded.deal_identities()
    # (a game that was running when it was saved comes back paused, as always)
    running = new(deal=True)
    running.set_running(True, "started")
    old = World.load(_without_the_key(running), make_spec(), ManualClock())
    assert old.state.dealt is True and old.state.status == "paused"


def test_a_save_that_predates_the_flag_is_dealt_when_there_was_nothing_to_deal() -> None:
    spec = make_spec(setup={"public_tags": ["active"]})
    world = new(spec=spec)
    assert world.state.dealt is False
    loaded = World.load(_without_the_key(world), spec, ManualClock())
    assert loaded.state.dealt is True and roles_of(loaded) == [""] * 4


def test_the_inference_leaves_a_hand_made_undealt_world_undealt() -> None:
    world = new()  # a pool, nobody has a role: no v0.13 game ever looked like this, so it is not dealt
    loaded = World.load(_without_the_key(world), make_spec(), ManualClock())
    assert loaded.state.dealt is False
    loaded.deal_identities()
    assert loaded.state.dealt is True


def test_a_key_that_is_present_is_never_overridden_by_the_inference() -> None:
    world = new(deal=True)
    data = json.loads(json.dumps(world.dump()))
    data["state"]["dealt"] = False  # explicit: the world says it was not dealt (whatever the players carry)
    assert World.load(data, make_spec(), ManualClock()).state.dealt is False


# --------------------------------------------------------------------------- the replay


def test_a_game_dealt_at_its_start_replays_from_a_session_without_identities_and_projects_the_deal() -> None:
    spec = make_game_spec()
    world = World.new_game(spec, replay_players(), ManualClock(), seed=11, deal=False)
    flush(world)

    before = build_replay(payload_of(world, spec))
    assert [ev["type"] for ev in before["events"]] == ["channel.created"] * 4  # nothing about identities yet
    assert all(p["tags"] == [] for p in before["initial"]["participants"])

    world.set_running(True, "start")
    flush(world)
    world.deal_identities()
    _, events = flush(world)
    live = [e.data for e in events if e.type == "player.updated"]

    replay = build_replay(payload_of(world, spec))
    assert all(p["tags"] == [] and p["out"] is False for p in replay["initial"]["participants"])  # still "no identities"
    assert [ev["type"] for ev in replay["events"]] == (
        ["channel.created"] * 4 + ["game.status"] + ["player.updated"] * 4
    )  # the deal sits behind the game start
    projected = [ev["data"] for ev in replay["events"] if ev["type"] == "player.updated"]
    assert projected == live and len(projected) == 4 and all(d["tags"] for d in projected)
    seqs = [ev["seq"] for ev in replay["events"]]
    assert seqs == sorted(seqs) and replay["duration_gt"] >= 0
    json.dumps(replay)
