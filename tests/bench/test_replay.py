"""``bench.replay``: a saved game's ledger -> situation steps -> the real engine again; and the comparison that says whether that worked.

The games here are played on the real engine in the test (nothing from ``data/``), then saved in the shape of ``game.json``.  One test replays every saved
game of the machine it runs on - read-only, and only when asked for (``ARENA_BENCH_SAVED_GAMES=1``: they are the user's own data) - because that is the
feasibility check of H47 itself."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from bench_helpers import NAMES, Game, play_long_game

from bench.loaders import load_game
from bench.play import build_scene
from bench.replay import (
    ARTIFACT_CHARS,
    Unconvertible,
    compare_ledgers,
    convert_entry,
    convert_ledger,
    decision_points,
    deal_roles,
    diff_values,
    game_players,
    replay_game,
    report_text,
    request_defaults,
    situation_from_game,
    situation_to_data,
    situation_to_yaml,
)
from bench.situation import parse_situation

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def long_game() -> dict[str, Any]:
    return play_long_game()


# ------------------------------------------------------------------------------------------------ the replay is exact


def test_a_game_played_on_the_engine_replays_exactly_and_ends_in_the_same_state(long_game: dict[str, Any]) -> None:
    result = replay_game(long_game)
    led = result.ledger_report
    assert led["saved"] == led["replayed"] == led["matched"] == len(long_game["world"]["ledger"]) > 50
    assert led["identical"] and led["mismatches"] == [] and led["artifacts"] == 0 and led["gt_drift"] == 0 and led["first_mismatch"] is None
    assert result.state_report == {"equal": True, "differences": [], "count": 0}
    assert result.refused == [] and result.conversion.gaps == [] and result.exact and result.deal == "seed"
    types = {e["type"] for e in long_game["world"]["ledger"]}
    assert {"message", "channel.created", "channel.updated", "floor.opened", "floor.granted", "floor.token_ended", "floor.completed", "floor.closed", "request.opened",
            "request.answered", "request.closed", "request.cancelled", "ability.enabled", "ability.used", "ability.disabled", "player.updated", "phase.changed",
            "dashboard.updated", "random.drawn", "game.status"} <= types


def test_every_prefix_of_the_game_replays_to_the_same_prefix_of_the_ledger(long_game: dict[str, Any]) -> None:
    last = long_game["world"]["ledger"][-1]["seq"]
    for upto in (20, 33, 47, last - 1):
        result = replay_game(long_game, upto_seq=upto)
        # (the unit is an action: the engine writes what follows from the last one in the same breath, so the replay may run a few entries past ``upto``)
        assert result.conversion.upto_seq == upto and upto <= len(result.world.ledger) == upto + result.ledger_report["overshoot"]
        assert result.ledger_report["identical"] and result.state_report is None  # (a prefix has no end state to compare: only the whole game does)
    # a decision point is the end of its action: no overshoot there
    point = next(p for p in decision_points(long_game) if p["kind"] == "request")
    assert replay_game(long_game, upto_seq=point["seq"]).ledger_report["overshoot"] == 0
    # cut in the middle of an action's consequences (a speech is followed by the end of its token and the next grant): the rest is written and compared too
    middle = next(e for e in long_game["world"]["ledger"] if e["type"] == "floor.token_ended")["seq"]
    cut = replay_game(long_game, upto_seq=middle)
    assert cut.ledger_report["overshoot"] >= 1 and cut.ledger_report["identical"] and "一并比对" in report_text(cut)


def test_the_converter_turns_actions_into_tool_calls_and_leaves_the_engines_own_entries_out(long_game: dict[str, Any]) -> None:
    conversion = convert_ledger(long_game)
    tools = [(s.actor, s.tool) for s in conversion.steps if s.kind == "tool"]
    assert ("moderator", "set_floor_queue") in tools and ("N1", "speak") in tools and ("N3", "speak_template") in tools
    assert [t for _actor, t in tools].count("use_ability") == 1
    assert ("N3", "challenge_moderator") in tools and ("moderator", "resolve_challenge") in tools and ("moderator", "reopen_request") in tools and ("moderator", "end_game") in tools
    assert ("moderator", "hold_game") in tools and ("moderator", "random") in tools and ("moderator", "update_dashboard") in tools
    assert conversion.actions == len([s for s in conversion.steps if s.kind in ("tool", "running")]) and conversion.ids == ["N1", "N2", "N3", "N4"]
    assert conversion.roles == [p.role for p in replay_game(long_game).world.state.players] and conversion.seed == 21 and conversion.scenario == "minimal_test"
    # no tool call stands for what the engine did by itself (the deal, the system DMs, the floors it handed on, the closing of a request)
    assert not [s for s in conversion.steps if s.kind == "tool" and s.tool == "announce" and s.args.get("template_id") in (None, "")]
    assert any(s.kind == "tick" for s in conversion.steps)  # (a tick at the time of every reaction that came from a timer)
    first_speech = next(s for s in conversion.steps if s.tool == "speak")
    assert first_speech.at == 3.0 and first_speech.meta["seq"] == long_game["world"]["ledger"][[e["seq"] for e in long_game["world"]["ledger"]].index(first_speech.meta["seq"])]["seq"]


def test_a_request_default_comes_from_the_saved_state_and_from_what_it_did(long_game: dict[str, Any]) -> None:
    ledger = long_game["world"]["ledger"]
    assert request_defaults(ledger) == {"r1": "N1"}  # (a ledger alone has only the defaults that fired)
    state = long_game["world"]["state"]
    assert request_defaults(ledger, state)["r1"] == "N1" and "r2" not in request_defaults(ledger, state)
    opened = next(s for s in convert_ledger(long_game).steps if s.tool == "open_request")
    assert opened.args["default"] == "N1" and opened.args["sealed"] is True and opened.args["options"][0] == {"id": "N1", "label": "N1"}


def test_roles_come_from_the_deal_entries_and_the_replay_falls_back_on_them_when_the_seed_deals_differently(long_game: dict[str, Any]) -> None:
    assert deal_roles(long_game["world"]["ledger"], ["N1", "N2", "N3", "N4"]) == [p["role"] for p in long_game["world"]["state"]["players"]]
    with pytest.raises(ValueError, match="没有这些座位的发牌记录：N9"):
        deal_roles(long_game["world"]["ledger"], ["N1", "N9"])
    recorded = deal_roles(long_game["world"]["ledger"], ["N1", "N2", "N3", "N4"])
    other_seed = copy.deepcopy(long_game)
    other_seed["world"]["state"]["rng"]["seed"] = 99991  # (the saved deal was made with another seed than the one this file says)
    result = replay_game(other_seed)
    assert [p.role for p in result.world.state.players] == recorded  # whatever the seed deals, the seats get the roles the ledger recorded
    # the explicit deal does not shuffle, so the generator is not used for it and every *later* random draw differs - and nothing else does
    for replayed in (result, replay_game(long_game, deal="explicit")):
        by_type = replayed.ledger_report["mismatch_by_type"]
        assert replayed.deal == "explicit" and set(by_type) == {"random.drawn"} and 1 <= by_type["random.drawn"] <= 3 and not replayed.exact  # (a draw may agree by chance)


def test_the_scenario_can_be_the_saved_one_or_the_current_file(long_game: dict[str, Any]) -> None:
    assert replay_game(long_game, spec_source="current").exact and replay_game(long_game, spec_source="saved").exact
    no_spec = {k: v for k, v in long_game.items() if k != "scenario_spec"}
    assert replay_game(no_spec).exact  # (without a saved scenario the file is used)


# ------------------------------------------------------------------------------------------------ gaps are named


def test_a_difference_the_engine_makes_is_reported_with_the_entry_and_the_values(long_game: dict[str, Any]) -> None:
    """What the replay reproduces is what the *actions* say; what the engine *makes of them* is what is compared.  So the difference is planted in an
    engine's own entry (a tally the engine computed), not in an action (the replay would just do what the tampered action says)."""
    tampered = copy.deepcopy(long_game)
    entry = next(e for e in tampered["world"]["ledger"] if e["type"] == "request.closed")
    entry["data"]["tally"]["N3"] = 99
    result = replay_game(tampered)
    led = result.ledger_report
    assert not led["identical"] and led["matched"] == led["compared"] - 1 and led["mismatch_by_type"] == {"request.closed": 1}
    assert "tally.N3" in led["first_mismatch"] and f"#{entry['seq']} request.closed" in led["first_mismatch"] and "99" in led["first_mismatch"] and not result.exact
    text = report_text(result, game_id="g1")
    assert "重放 g1" in text and "第一处不同" in text and text.endswith("有缺口，见上。")
    assert "可行" in report_text(replay_game(long_game), game_id="g0")


def test_a_missing_or_extra_entry_is_a_count_difference() -> None:
    saved = [{"seq": i, "gt": 0.0, "type": "phase.changed", "actor_id": "moderator", "channel_id": None, "visible_to_ids": [], "data": {"text": str(i)}} for i in (1, 2, 3)]
    assert compare_ledgers(saved, saved[:2], 3)["first_mismatch"] == "条数不同：存档 3 条，重放 2 条" and not compare_ledgers(saved, saved[:2], 3)["identical"]
    assert compare_ledgers(saved, saved, 3)["identical"] and compare_ledgers(saved, saved, 3)["overshoot"] == 0
    cut = compare_ledgers(saved, saved, 2)  # a prefix cut in the middle of an action: the rest of it is written, and compared with the saved entries there
    assert cut["identical"] and cut["overshoot"] == 1 and cut["saved"] == 3
    longer = compare_ledgers(saved, [*saved, {**saved[0], "seq": 4}], 3)  # a replay that wrote more than the whole saved game
    assert not longer["identical"] and longer["first_mismatch"] == "条数不同：存档 3 条，重放 4 条" and longer["overshoot"] == 0


def test_an_entry_that_cannot_be_turned_back_into_a_tool_call_is_a_listed_gap(long_game: dict[str, Any]) -> None:
    odd = copy.deepcopy(long_game)
    odd["world"]["ledger"][-1]["type"] = "something.new"
    conversion = convert_ledger(odd)
    assert [(g.seq, g.type) for g in conversion.gaps] == [(odd["world"]["ledger"][-1]["seq"], "something.new")] and "不认识的账本条目类型" in conversion.gaps[0].reason
    with pytest.raises(Unconvertible):
        convert_entry({"type": "message", "actor_id": "moderator", "data": {"kind": "weird"}}, {})
    with pytest.raises(Unconvertible):
        convert_entry({"type": "channel.updated", "actor_id": "moderator", "data": {"changes": {}}, "channel_id": "x"}, {})
    assert convert_entry({"type": "floor.granted", "actor_id": "engine", "data": {}}, {}) is None  # (a reaction)
    assert convert_entry({"type": "player.updated", "actor_id": "engine", "data": {"reason": "deal"}}, {}) is None  # (the deal)


# ------------------------------------------------------------------------------------------------ v0.13 truncation leaves a trace, not a difference


def test_the_trace_of_a_v0_13_truncation_is_an_artifact_not_a_mismatch() -> None:
    """v0.13 cut a long speech to the limit and left the whitespace of the cut at its end; the commit gate trims a speech, so a replay writes it a few characters
    shorter - and so are the ``chars_used`` of the floor entries that count it.  That is how an old save differs from its replay, and it is not a gap."""
    g = Game()
    ids = ["N1", "N2", "N3", "N4"]
    g.mod("create_channel", channel_id="public", name="公共频道", type="public", members=ids)
    g.mod("set_floor_queue", channel_id="public", order=["N1", "N2"], limits={"max_messages": 1})
    g.do("N1", "speak", channel_id="public", content="第一段。\n\n第二段。")
    g.do("N2", "speak", channel_id="public", content="好。")
    game = g.saved()
    cut = next(e for e in game["world"]["ledger"] if e["type"] == "message")
    cut["data"]["content"] += "\n\n"  # how v0.13 saved a speech it had cut
    cut["data"]["truncated"] = {"dropped_chars": 40, "kept_chars": len(cut["data"]["content"])}
    ended = next(e for e in game["world"]["ledger"] if e["type"] == "floor.token_ended")
    ended["data"]["chars_used"] += 2
    result = replay_game(game)
    led = result.ledger_report
    assert led["identical"] and led["artifacts"] == 2 and led["mismatches"] == [] and result.exact
    assert "只差 v0.13 截断留下的尾部空白" in report_text(result)
    # ... but a *different* text, or a floor entry that differs without a cut before it, is a real difference
    wrong = copy.deepcopy(game)
    next(e for e in wrong["world"]["ledger"] if e["type"] == "message")["data"]["content"] = "完全不同的话"
    assert not replay_game(wrong).ledger_report["identical"]
    no_cut = copy.deepcopy(game)
    for e in no_cut["world"]["ledger"]:
        e["data"].pop("truncated", None)
    no_cut["world"]["ledger"][[e["seq"] for e in no_cut["world"]["ledger"]].index(cut["seq"])]["data"]["content"] += "!"
    assert not replay_game(no_cut).ledger_report["identical"] and ARTIFACT_CHARS == 8


def test_the_diff_helper_treats_numbers_close_in_time_as_equal() -> None:
    assert diff_values({"a": 1.0, "b": [1, {"c": 2}]}, {"a": 1.005, "b": [1, {"c": 2}]}) == []
    assert diff_values({"a": 1.0}, {"a": 1.5}) == [("a", 1.0, 1.5)]
    assert diff_values({"a": 1}, {"b": 1}) == [("a", 1, "（没有）"), ("b", "（没有）", 1)]
    assert diff_values([1, 2], [1]) == [("（长度）", 2, 1)]


# ------------------------------------------------------------------------------------------------ people at the table


def test_the_seats_a_person_played_are_marked_and_have_no_deadline_in_the_replay() -> None:
    g = Game()
    ids = ["N1", "N2", "N3", "N4"]
    g.mod("create_channel", channel_id="public", name="公共频道", type="public", members=ids)
    start = g.world.seq
    g.world.set_untimed_holders(["N3"])
    g.mod("set_floor_queue", channel_id="public", order=["N3", "N4"], limits={"max_messages": 1, "duration_s": 20})
    g.do("N3", "speak", channel_id="public", content="我是真人，慢慢想了很久。")
    g.world.set_untimed_holders([])
    game = {**g.saved(), "human_spans": [{"seat": "N3", "from_seq": start, "to_seq": None}]}
    conversion = convert_ledger(game)
    assert [s.seats for s in conversion.steps if s.kind == "untimed"] == [("N3",)]
    speak = next(s for s in conversion.steps if s.tool == "speak")
    assert speak.meta["human"] is True and not any(s.meta.get("human") for s in conversion.steps if s.actor == "moderator")
    granted = next(e for e in game["world"]["ledger"] if e["type"] == "floor.granted")
    assert granted["data"].get("untimed") is True and replay_game(game).ledger_report["identical"]


# ------------------------------------------------------------------------------------------------ decision points and situation scripts


def test_the_places_a_seat_is_asked_to_act_are_listed(long_game: dict[str, Any]) -> None:
    points = decision_points(long_game)
    floors = [p for p in points if p["kind"] == "floor"]
    requests = [p for p in points if p["kind"] == "request"]
    assert [p["to"] for p in floors[:4]] == [["N1"], ["N2"], ["N3"], ["N4"]] and floors[0]["phase"] == "第 1 轮 · 发言" and floors[0]["channel"] == "public"
    assert requests[0]["id"] == "r1" and requests[0]["to"] == ["N1", "N2", "N3", "N4"] and requests[0]["phase"] == "第 1 轮 · 投票" and requests[0]["question"] == "请投票"


def test_a_decision_point_of_a_saved_game_becomes_a_situation_a_bot_can_be_run_on(long_game: dict[str, Any]) -> None:
    point = next(p for p in decision_points(long_game) if p["kind"] == "request" and p["id"] == "r1")
    situation = situation_from_game(long_game, upto_seq=point["seq"], seat="N3", situation_id="from-save")
    assert situation.decision.seats == ("N3",) and situation.roles == dict(zip(["N1", "N2", "N3", "N4"], [p["role"] for p in long_game["world"]["state"]["players"]]))
    assert situation.seed == 21 and situation.names == tuple(NAMES) and situation.meta == {"source": "saved game", "game": "test-game", "upto_seq": point["seq"]}
    text = situation_to_yaml(situation, header="from a saved game\ncheck before committing")
    assert text.startswith("# from a saved game\n# check before committing\n") and yaml.safe_load(text)["decision"] == {"seat": "N3", "wake": "redeliver", "expect": ["speak", "speak_template", "pass", "respond", "use_ability"]}
    again = parse_situation(yaml.safe_load(text), source="roundtrip")
    key = lambda s: [(x.kind, x.actor, x.tool, dict(x.args), x.at, x.seats, x.running) for x in s.steps]  # noqa: E731
    assert key(again) == key(situation)
    scene = build_scene(again, seat_name="Carol")
    direct = replay_game(long_game, upto_seq=point["seq"])
    assert len(scene.world.ledger) == len(direct.world.ledger) == point["seq"]
    assert [(e.type, e.actor_id) for e in scene.world.ledger] == [(e.type, e.actor_id) for e in direct.world.ledger]
    assert [(e.kind, e.payload["event"], e.payload["request_id"]) for e in scene.wake] == [("request", "request.opened", "r1")]
    assert scene.world.board("N3")["requests"][0]["question"] == "请投票" and scene.world.board("N3")["me"]["role"] == next(p for p in direct.world.state.players if p.id == "N3").role
    with pytest.raises(ValueError, match="座位 N9 不在这局里"):
        situation_from_game(long_game, upto_seq=point["seq"], seat="N9", situation_id="x")


def test_a_situation_converted_to_data_keeps_everything_a_script_can_say() -> None:
    sit = parse_situation({
        "format": "arena-situation", "version": 1, "id": "full", "title": "题", "tags": ["L2"], "scenario": "minimal_test", "seed": 3, "deal": "creation", "clock": 2,
        "players": {"count": 4, "names": NAMES}, "roles": ["red", "red", "blue", "blue"],
        "steps": [{"as": "moderator", "tool": "set_phase", "at": 1, "label": "p", "args": {"text": "a"}, "meta": {"k": 1}}, {"advance": 5}, {"tick": True}, {"running": False, "reason": "user"}, {"untimed": ["N1"]}],
        "decision": {"seats": ["N3", "N4"], "wake": [{"event": "message.new", "channel": "public"}], "expect": ["speak"]}, "brief": "简介",
        "score": [{"kind": "choice", "request": "r1", "correct": ["N1"]}], "judge": {"L2": {"x": 1}}, "meta": {"pair": "p"},
    })
    again = parse_situation(yaml.safe_load(yaml.safe_dump(situation_to_data(sit), allow_unicode=True)), source="rt")
    key = lambda s: [(x.kind, x.actor, x.tool, dict(x.args), x.at, x.label, x.seconds, x.running, x.reason, x.seats, dict(x.meta)) for x in s.steps]  # noqa: E731
    assert key(again) == key(sit) and [s.kind for s in again.steps] == ["tool", "advance", "tick", "running", "untimed"]
    assert again.score == sit.score and again.player_count == 4 and again.names == tuple(NAMES)
    assert (again.decision.seats, again.decision.expect, again.decision.wake.mode) == (("N3", "N4"), ("speak",), "explicit")
    assert (again.title, again.tags, again.seed, again.deal, again.clock, again.roles, again.brief, again.judge, again.meta) == (
        "题", ("L2",), 3, "creation", 2.0, ("red", "red", "blue", "blue"), "简介", {"L2": {"x": 1}}, {"pair": "p"})


def test_the_players_of_a_saved_game_keep_their_ids_names_and_personas() -> None:
    players = game_players({"participants": [{"id": "N1", "seat": "1", "name": "Alice", "persona": "话少"}, {"id": "N2", "name": "Bob"}]})
    assert players == [{"id": "N1", "seat": "1", "name": "Alice", "persona": "话少"}, {"id": "N2", "seat": "2", "name": "Bob", "persona": ""}]


# ------------------------------------------------------------------------------------------------ the games of this machine


SAVED_GAMES_ENV = "ARENA_BENCH_SAVED_GAMES"


def _saved_games() -> list[Path]:
    found: list[Path] = []
    for base in (ROOT / "data" / "games", ROOT / ".demo-data" / "real" / "games"):
        found.extend(sorted(base.glob("*/game.json")) if base.is_dir() else [])
    return found


@pytest.mark.skipif(
    os.environ.get(SAVED_GAMES_ENV) != "1",
    reason=f"opt-in: it reads the user's own saved games (read only) - run it with {SAVED_GAMES_ENV}=1 on a machine that has some",
)
def test_every_saved_game_on_this_machine_replays_exactly_on_the_current_engine() -> None:
    """The feasibility check of H47 itself, on the real games (read only): a v0.13 game's actions, converted and played on today's engine, give its ledger
    again - which is what lets a decision point of a real game be a benchmark situation.  If this fails, the engine changed what an old game means.
    (Off by default: saved games are the user's own data.  ``python -m bench replay --data-dir data`` prints the same survey.)"""
    games = _saved_games()
    if not games:
        pytest.skip("no saved games on this machine")
    problems = []
    for path in games:
        try:
            game = load_game(path)
        except ValueError:
            continue
        if not (game.get("world") or {}).get("ledger"):
            continue
        result = replay_game(game, spec_source="saved")
        if not result.exact:
            problems.append(f"{path.parent.name}: {report_text(result, game_id=path.parent.name)}")
    assert not problems, "\n".join(problems)
