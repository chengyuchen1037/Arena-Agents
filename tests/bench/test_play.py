"""``bench.play``: situations played on the REAL engine - the world, the dealing, the steps as intents, the decision point."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.world.types import MODERATOR_ID, Intent
from bench.play import (
    PlayError,
    StepRunner,
    build_scene,
    ledger_fingerprint,
    load_spec,
    resolve_players,
    resolve_refs,
    resolve_roles,
    with_explicit_roles,
)
from bench.situation import SituationError, load_situation, parse_situation

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "bench" / "fixtures" / "situations"

OPEN = {"as": "moderator", "tool": "create_channel", "args": {"channel_id": "public", "name": "公共频道", "type": "public", "members": ["N1", "N2", "N3", "N4"]}}


def situation(steps: list[dict[str, Any]] | None = None, **over: Any):
    data: dict[str, Any] = {
        "format": "arena-situation", "version": 1, "id": "t-play", "scenario": "minimal_test", "seed": 5,
        "players": {"count": 4}, "roles": {"N1": "red", "N2": "red", "N3": "blue", "N4": "blue"},
        "steps": steps if steps is not None else [
            OPEN,
            {"as": "moderator", "tool": "set_floor_queue", "args": {"channel_id": "public", "order": ["N1", "N3"], "limits": {"max_messages": 1}}},
            {"as": "N1", "tool": "speak", "args": {"channel_id": "public", "content": "我先说。"}},
        ],
        "decision": {"seat": "N3"},
    }
    data.update(over)
    return parse_situation({k: v for k, v in data.items() if v is not None})


# ------------------------------------------------------------------------------------------------ the decision point


def test_a_situation_is_played_to_a_decision_point_on_the_real_engine() -> None:
    scene = build_scene(situation())
    world = scene.world
    assert world.running and world.state.dealt  # (dealt when the game "started")
    assert [e.type for e in world.ledger][-5:] == ["floor.opened", "floor.granted", "message", "floor.token_ended", "floor.granted"]
    assert scene.seat == "N3" and [(e.kind, e.payload["event"], e.channel_id) for e in scene.wake] == [("floor", "floor.granted", "public")]
    board = world.board("N3")
    assert board["me"]["role"] == "blue" and any(c["id"] == "public" and c["can_write"] for c in board["channels"])
    assert [r.step.tool for r in scene.records if r.step.kind == "tool"] == ["create_channel", "set_floor_queue", "speak"]
    assert all(r.ok for r in scene.records)
    assert scene.records[-1].seq_after == len(world.ledger) and scene.fingerprint == ledger_fingerprint(world.ledger)
    assert scene.seq0 == len(world.ledger) and scene.after() == []  # (nothing has happened since the decision point)
    receipt = world.submit(Intent(actor_id="N3", based_on_seq=world.seq, type="speak", payload={"channel_id": "public", "content": "轮到我了。"}))
    assert receipt.ok and [(e["type"], e["actor_id"]) for e in scene.after()][0] == ("message", "N3")  # (what the bot's move made the engine write)


def test_a_vote_waits_for_the_seat_that_has_not_answered() -> None:
    scene = build_scene(load_situation(FIXTURES / "_example.yaml"), seat_name="Alice")
    assert [(e.kind, e.payload["event"], e.payload["request_id"]) for e in scene.wake] == [("request", "request.opened", "r1")]
    request = scene.world.board("N3")["requests"][0]
    assert request["id"] == "r1" and request["answered"] is False and scene.runner.labels["vote"]["request_id"] == "r1"
    assert [e.actor_id for e in scene.world.ledger if e.type == "request.answered"] == ["N1", "N2", "N4"]


def test_the_same_situation_is_the_same_world_every_time() -> None:
    first, second = build_scene(situation()), build_scene(situation())
    assert first.fingerprint == second.fingerprint and len(first.world.ledger) == len(second.world.ledger)
    assert build_scene(situation(seed=6, roles=None)).fingerprint != build_scene(situation(seed=5, roles=None)).fingerprint  # (the seed deals)


def test_dealing_at_the_start_and_at_creation_give_the_same_identities() -> None:
    at_start = build_scene(situation(roles=None, deal="start"))
    at_creation = build_scene(situation(roles=None, deal="creation"))
    assert [p.role for p in at_start.world.state.players] == [p.role for p in at_creation.world.state.players]
    assert at_start.world.state.dealt and at_creation.world.state.dealt


def test_explicit_roles_put_every_seat_where_the_script_says() -> None:
    scene = build_scene(situation(roles=["blue", "red", "red", "blue"]))
    assert [(p.id, p.role) for p in scene.world.state.players] == [("N1", "blue"), ("N2", "red"), ("N3", "red"), ("N4", "blue")]
    assert [p.role for p in build_scene(situation(roles={"N1": "red", "N2": "red", "N3": "blue", "N4": "blue"})).world.state.players] == ["red", "red", "blue", "blue"]
    spec = with_explicit_roles(load_spec("minimal_test"), ["blue", "red"])
    assert [(i.role, i.count) for i in spec.setup.identity_pool] == [("blue", 1), ("red", 1)] and spec.setup.shuffle is False
    assert [(i.role, i.count) for i in load_spec("minimal_test").setup.identity_pool] == [("red", 2), ("blue", 2)]  # (the scenario itself is untouched)
    with pytest.raises(SituationError, match="没有这些身份：green"):
        with_explicit_roles(load_spec("minimal_test"), ["green"])
    with pytest.raises(SituationError, match="roles 必须正好覆盖每个座位"):
        resolve_roles(situation(roles={"N1": "red"}), ["N1", "N2"])


def test_the_seat_under_test_takes_the_name_of_the_bot() -> None:
    sit = situation(players={"count": 4, "names": ["Alice", "Bob", "Carol", "David"]})
    spec = load_spec("minimal_test")
    players = resolve_players(sit, spec, seat="N3", seat_name="Bob")
    assert [(p["id"], p["seat"], p["name"]) for p in players] == [("N1", "1", "Alice"), ("N2", "2", "Eve"), ("N3", "3", "Bob"), ("N4", "4", "David")]  # (Bob moved: no two seats share a name)
    assert [p["name"] for p in resolve_players(sit, spec)] == ["Alice", "Bob", "Carol", "David"]
    assert [p["name"] for p in resolve_players(situation(), spec)] == ["Alice", "Bob", "Carol", "David"]  # (names the script does not give: the default library's)
    scene = build_scene(sit, seat_name="Bob")
    assert scene.seat_name == "Bob" and scene.names["N3"] == "Bob" and scene.world.display_name("N3") == "Bob"
    with pytest.raises(SituationError, match="座位 N9 不在这局的 4 个座位里"):
        resolve_players(sit, spec, seat="N9")


def test_the_same_script_can_be_played_for_another_seat() -> None:
    sit = situation(steps=[OPEN, {"as": "moderator", "tool": "set_floor_queue", "args": {"channel_id": "public", "order": ["N1", "N2", "N3", "N4"], "limits": {"max_messages": 1}}},
                           {"say": {"N1": "我先说。", "N2": "我第二。"}, "channel": "public"}], decision={"seats": ["N3", "N4"]})
    assert build_scene(sit, seat="N3").wake[0].channel_id == "public"
    with pytest.raises(PlayError, match="座位 N4 在决策点没有任何待处理的事"):
        build_scene(sit, seat="N4")  # (N3 holds the floor, N4 is behind it: nothing wakes N4)


def test_a_seat_with_nothing_to_do_is_an_error_and_an_explicit_wake_fixes_it() -> None:
    quiet = situation(steps=[OPEN], decision={"seat": "N3"})
    with pytest.raises(PlayError, match="没有任何待处理的事"):
        build_scene(quiet)
    woken = situation(steps=[OPEN], decision={"seat": "N3", "wake": [{"event": "message.new", "channel": "public", "sender": "N1"}]})
    wake = build_scene(woken).wake
    assert [(e.kind, e.payload["event"], e.channel_id, e.sender, e.priority) for e in wake] == [("message", "message.new", "public", "N1", "normal")]


def test_a_wake_since_a_label_is_everything_the_seat_was_told_from_that_step_on() -> None:
    steps = [OPEN,
             {"as": "moderator", "tool": "set_floor_queue", "label": "round", "args": {"channel_id": "public", "order": ["N1", "N3"], "limits": {"max_messages": 1}}},
             {"as": "N1", "tool": "speak", "args": {"channel_id": "public", "content": "我先说。"}}]
    wake = build_scene(situation(steps=steps, decision={"seat": "N3", "wake": {"since": "round"}})).wake
    assert [(e.payload["event"], e.channel_id) for e in wake] == [("message.new", "public"), ("floor.granted", "public")]


# ------------------------------------------------------------------------------------------------ the steps


def test_a_step_the_engine_refuses_stops_the_script_and_says_which() -> None:
    bad = situation(steps=[OPEN, {"as": "N1", "tool": "speak", "args": {"channel_id": "public", "content": "频道还是关着的"}}])
    with pytest.raises(PlayError) as info:
        build_scene(bad)
    assert "steps[1]（N1 的 speak） 被引擎拒绝：CHANNEL_CLOSED" in str(info.value) and info.value.step is not None and info.value.receipt.code == "CHANNEL_CLOSED"
    out_of_turn = situation(steps=[OPEN, {"as": "moderator", "tool": "set_floor_queue", "args": {"channel_id": "public", "order": ["N2", "N1"], "limits": {"max_messages": 1}}},
                                   {"as": "N1", "tool": "speak", "args": {"channel_id": "public", "content": "还没轮到我"}}])
    with pytest.raises(PlayError, match="NO_FLOOR"):
        build_scene(out_of_turn)
    unknown = situation(steps=[{"as": "moderator", "tool": "announce", "args": {"channel_id": "nowhere", "template_id": "t"}}])
    with pytest.raises(PlayError, match="UNKNOWN_CHANNEL"):
        build_scene(unknown)
    elsewhere = situation(steps=[{"as": "N9", "tool": "pass", "args": {"channel_id": "public"}}])
    with pytest.raises(SituationError, match="座位 N9 不在这局里"):
        build_scene(elsewhere)


def test_scripted_players_never_write_stale_drafts() -> None:
    """The scripted speech is submitted with the world's current seq: a ``phase.changed`` right before it (which makes drafts STALE) does not
    refuse it."""
    steps = [OPEN, {"as": "moderator", "tool": "set_floor_queue", "args": {"channel_id": "public", "order": ["N1", "N3"], "limits": {"max_messages": 1}}},
             {"as": "moderator", "tool": "set_phase", "args": {"text": "新阶段"}}, {"as": "N1", "tool": "speak", "args": {"channel_id": "public", "content": "好。"}}]
    assert build_scene(situation(steps=steps)).world.state.phase == "新阶段"


def test_at_moves_the_game_clock_and_fires_the_timers_that_are_due() -> None:
    steps = [OPEN, {"as": "moderator", "tool": "set_floor_queue", "at": 5, "args": {"channel_id": "public", "order": ["N1", "N3"], "limits": {"max_messages": 1, "duration_s": 20}}},
             {"tick": True, "at": 30}]
    scene = build_scene(situation(steps=steps, decision={"seat": "N3"}))
    assert float(scene.clock.now()) == 30.0
    token_ends = [(e.data["holder"], e.data["reason"]) for e in scene.world.ledger if e.type == "floor.token_ended"]
    assert token_ends == [("N1", "timeout")]  # N1 never spoke: its 20 seconds ran out at 25, and the floor went on to N3
    assert [e.payload["event"] for e in scene.wake] == ["floor.granted"]
    runner = StepRunner(scene.world, scene.clock)
    with pytest.raises(PlayError, match="早于现在的游戏时间"):
        runner.run(parse_situation({"format": "arena-situation", "version": 1, "id": "x", "scenario": "minimal_test", "decision": {"seat": "N1"},
                                    "steps": [{"tick": True, "at": 1}]}).steps[0])
    assert runner.run(parse_situation({"format": "arena-situation", "version": 1, "id": "x", "scenario": "minimal_test", "decision": {"seat": "N1"},
                                       "steps": [{"tick": True, "at": 1}]}).steps[0], strict=False).ok  # (a replay ignores a clock that went backwards)


def test_advance_and_the_gate_steps() -> None:
    steps = [OPEN, {"advance": 12.5}, {"running": False, "reason": "user"}, {"running": True}]
    scene = build_scene(situation(steps=steps, decision={"seat": "N3", "wake": [{"event": "message.new", "channel": "public"}]}))
    assert float(scene.clock.now()) == 12.5 and scene.world.running
    assert [e.data["status"] for e in scene.world.ledger if e.type == "game.status"] == ["running", "paused", "running"]


def test_untimed_steps_mark_the_seats_a_person_plays() -> None:
    steps = [OPEN, {"untimed": ["N3"]}, {"as": "moderator", "tool": "set_floor_queue", "args": {"channel_id": "public", "order": ["N3"], "limits": {"max_messages": 1}}}]
    scene = build_scene(situation(steps=steps))
    granted = [e for e in scene.world.ledger if e.type == "floor.granted"][-1]
    assert granted.data.get("untimed") is True and "N3" in scene.world.untimed_holders


def test_labels_carry_the_receipt_of_a_step_into_later_arguments() -> None:
    labels = {"vote": {"request_id": "r7", "seq": 9}}
    assert resolve_refs({"request_id": "@vote", "nested": ["@vote.seq", {"k": "@vote"}], "text": "@N3 你好", "n": 3}, labels) == {
        "request_id": "r7", "nested": [9, {"k": "r7"}], "text": "@N3 你好", "n": 3,
    }
    with pytest.raises(PlayError, match="@nope 没有对应的值"):
        resolve_refs("@nope", labels)
    with pytest.raises(PlayError, match="@vote.missing 没有对应的值"):
        resolve_refs("@vote.missing", labels)


def test_the_moderators_tools_are_the_real_ones_so_a_template_announcement_goes_through_the_gate() -> None:
    steps = [OPEN, {"as": "moderator", "tool": "announce", "args": {"channel_id": "public", "template_id": "out_notice", "params": {"players": ["N4"]}}},
             {"as": "moderator", "tool": "set_floor_queue", "args": {"channel_id": "public", "order": ["N3"], "limits": {"max_messages": 1}}}]
    scene = build_scene(situation(steps=steps))
    messages = [e for e in scene.world.ledger if e.type == "message"]
    assert messages[0].data["kind"] == "announce" and "N4" in messages[0].data["content"] and messages[0].actor_id == MODERATOR_ID


def test_a_scenario_that_is_not_there_is_a_clear_error() -> None:
    with pytest.raises(SituationError, match="找不到剧本 nowhere"):
        build_scene(situation(scenario="nowhere"))
    with pytest.raises(SituationError, match="建不了这局"):
        build_scene(situation(players={"count": 3}, roles=None))  # (the scenario's pool has four identities)
