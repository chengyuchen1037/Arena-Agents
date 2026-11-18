"""Contract tests for tools/mock_ui_server.py (the v0.13.0 UI mock) and, through it, for the SSE v2 client store.

* every event the mock script publishes must match docs/v0.12-contracts.md §8.2 + §10.6 (``validate_event``);
* the REAL client store (static/js/store.js, run with node) must reduce the whole event log to exactly
  the session the mock holds (so the mock's events are complete and the store's reducer is faithful);
* what the script is meant to show is really in the log: ids N1..N12 with nicknames, the three agent states,
  the wolves' share_answers request with a changed answer, the sealed vote, the budget warning -> pause ->
  raise -> continue, the review;
* resume (Last-Event-ID) semantics of the mock's event bus;
* the HTTP endpoints the front end calls (roster, budget, price source, review, ...) answer with the shapes
  and the status codes it expects.

``validate_event`` / ``replay_fast`` are also meant for checking the real backend's output.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
MOCK_PATH = ROOT / "tools" / "mock_ui_server.py"


def _load_mock():
    spec = importlib.util.spec_from_file_location("arena_mock_ui_server", MOCK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mock = _load_mock()

_WINDOWS_NODE = Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"


def _find_node() -> str | None:
    env = os.environ.get("NODE_BIN")
    if env and Path(env).exists():
        return env
    return shutil.which("node") or (str(_WINDOWS_NODE) if _WINDOWS_NODE.exists() else None)


NODE = _find_node()
NAMES = ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo"]
IDS = [f"N{i}" for i in range(1, 13)]


@pytest.fixture(scope="module")
def replay() -> dict:
    return mock.replay_fast()


def _events(replay: dict, *types: str) -> list[dict]:
    return [ev for ev in replay["events"] if ev["type"] in types]


# --------------------------------------------------------------------------------- the event log
def test_every_event_of_the_scripted_game_matches_the_contract(replay: dict) -> None:
    problems = [f"#{ev['id']} {ev['type']}: {p}" for ev in replay["events"] for p in mock.validate_event(ev)]
    assert problems == []
    assert mock.validate_session(replay["initial"]) == []
    assert mock.validate_session(replay["final"]) == []


def test_the_script_covers_every_event_type_the_front_end_consumes(replay: dict) -> None:
    types = {ev["type"] for ev in replay["events"]}
    assert mock.REQUIRED_EVENTS <= types, mock.REQUIRED_EVENTS - types
    # the v0.12.1 additions are all in there
    assert {"request.answer", "budget.updated", "budget.warning", "budget.reached", "review.progress"} <= types
    ids = [ev["id"] for ev in replay["events"]]
    assert ids == sorted(ids) and len(set(ids)) == len(ids), "event ids strictly increase"
    final = replay["final"]
    assert final["status"] == "finished"
    assert final["winner"]
    kinds = {m["kind"] for m in final["messages"]}
    assert {"speech", "announce", "system", "challenge", "reply", "ability"} <= kinds
    assert any(m["truncated"] for m in final["messages"]), "one speech is cut by the per-message limit"
    assert {t["verdict"] for t in final["tickets"]} == {"upheld", "rejected"}
    # the free discussion, the queue and the frozen channel all showed up
    channel_updates = [ev["data"]["channel"] for ev in _events(replay, "channel.updated")]
    assert any(c["mode"] == "queue" for c in channel_updates)
    assert any(c["mode"] == "free" for c in channel_updates)
    assert any(c["frozen"] for c in channel_updates)
    assert any(ev["data"]["floor"]["kind"] == "queue" and ev["data"]["floor"]["active"] for ev in _events(replay, "floor.changed"))
    # the main script is about a minute long (virtual time) and the whole replay is instant
    assert 40 <= replay["duration"] <= 90
    assert final["review"]["available"] is True and final["review"]["done"] == IDS
    assert not any(ev["type"] == "snapshot" for ev in replay["events"])


def test_internal_ids_are_n1_to_n12_with_nicknames_and_the_moderator_text_names_both(replay: dict) -> None:
    for session in (replay["initial"], replay["final"]):
        assert [p["id"] for p in session["participants"]] == IDS
        assert [p["name"] for p in session["participants"]] == NAMES
        assert [str(p["seat"]) for p in session["participants"]] == [str(i) for i in range(1, 13)]
        assert all(mock.validate_avatar(p["avatar"]) == [] for p in session["participants"])
    dumped = json.dumps(replay["events"], ensure_ascii=False)
    assert not re.search(r"\bp\d{2}\b", dumped), "no v0.12.0 ids (p01 ...) anywhere"
    assert not any(k.startswith("execution_depth") for k in _all_keys(replay["events"]) | _all_keys(replay["final"]))
    # "N3 Carol": the id with the nickname, exactly how the models see players (the UI adds a hover on "N3")
    moderator_texts = [ev["data"]["content"] for ev in _events(replay, "message.committed") if ev["data"]["from"] == "moderator"]
    assert any("N3 Carol" in t for t in moderator_texts)
    assert any(re.search(r"N\d+ [A-Z][a-z]+", t) for t in moderator_texts)
    # every sender of a message is a known actor, and its name is the nickname (never the id)
    senders = {(ev["data"]["from"], ev["data"]["from_name"]) for ev in _events(replay, "message.committed")}
    for sender, name in senders:
        if sender in IDS:
            assert name == NAMES[IDS.index(sender)]
        else:
            assert sender in {"moderator", "engine"}
    # the 12 avatar descriptions are all different from each other
    assert len({json.dumps(p["avatar"], sort_keys=True) for p in replay["final"]["participants"]}) == 12


def test_agent_states_are_only_thinking_acting_sleeping_and_a_wake_up_time_only_belongs_to_sleep(replay: dict) -> None:
    statuses = _events(replay, "agent.status")
    assert {ev["data"]["state"] for ev in statuses} == {"thinking", "acting", "sleeping"}
    timed = [ev["data"] for ev in statuses if ev["data"]["until"] is not None]
    assert timed and all(d["state"] == "sleeping" for d in timed)
    assert all(isinstance(d["until"], (int, float)) and d["until"] > 0 for d in timed)
    # a game that ends: everybody sleeps (D8); the moderator too
    final = replay["final"]
    assert {p["state"] for p in final["participants"]} == {"sleeping"}
    assert final["moderator"]["state"] == "sleeping"
    assert "stopped" not in json.dumps(replay["events"]) and '"idle"' not in json.dumps([ev["data"] for ev in statuses])


def test_the_wolves_kill_is_a_share_answers_request_and_somebody_changes_their_mind(replay: dict) -> None:
    opened = {ev["data"]["request"]["id"]: ev["data"]["request"] for ev in _events(replay, "request.opened")}
    kill = next(r for r in opened.values() if r["share_answers"])
    assert kill["to"] == ["N3", "N6", "N10", "N12"]
    assert kill["auto_close"] == "unanimous" and kill["sealed"] is False
    assert kill["opened_seq"] > 0 and kill["answers"] == []
    answers = [ev["data"] for ev in _events(replay, "request.answer") if ev["data"]["request_id"] == kill["id"]]
    # every shared answer is visible to the moderator and to all four wolves
    assert all(set(a["visible_to_ids"]) == {"moderator", *kill["to"]} for a in answers)
    seen: dict[str, str] = {}
    changed = set()
    for a in answers:
        if a["player_id"] in seen and seen[a["player_id"]] != a["value"]:
            changed.add(a["player_id"])
        seen[a["player_id"]] = a["value"]
    assert changed, "somebody changed their answer midway"
    closed = [ev["data"] for ev in _events(replay, "request.closed") if ev["data"]["request_id"] == kill["id"]]
    assert closed and closed[0]["reason"] == "unanimous"
    assert closed[0]["result"]["leaders"] == ["N5"] and closed[0]["result"]["tally"]["N5"] == 4
    final_kill = next(r for r in replay["final"]["requests"] if r["id"] == kill["id"])
    assert [a["player_id"] for a in final_kill["answers"]] == ["N3", "N6", "N10", "N12"], "answers are in seat order"
    assert {a["value"] for a in final_kill["answers"]} == {"N5"}


def test_sealed_votes_reach_the_moderator_only_and_progress_never_carries_answers(replay: dict) -> None:
    opened = {ev["data"]["request"]["id"]: ev["data"]["request"] for ev in _events(replay, "request.opened")}
    vote = next(r for r in opened.values() if r["sealed"])
    assert vote["share_answers"] is False and len(vote["to"]) == 12
    for ev in _events(replay, "request.answer"):
        if ev["data"]["request_id"] == vote["id"]:
            assert ev["data"]["visible_to_ids"] == ["moderator"], "a sealed answer is for the moderator (god view) only"
    for ev in _events(replay, "request.progress"):
        assert set(ev["data"]) == {"request_id", "answered_by", "total"}
    final_vote = next(r for r in replay["final"]["requests"] if r["id"] == vote["id"])
    assert len(final_vote["answers"]) == 12 and final_vote["status"] == "closed"
    assert final_vote["result"]["leaders"] == ["N10"]
    assert final_vote["result"]["tally"]["N10"] == 6
    # the last-words request is a text request to one player
    last_words = next(r for r in opened.values() if r["kind"] == "text")
    assert last_words["to"] == ["N10"] and last_words["options"] == []


def test_the_budget_warns_at_80_percent_pauses_at_100_and_continues_after_the_limit_is_raised(replay: dict) -> None:
    events = replay["events"]
    index = {t: next(i for i, ev in enumerate(events) if ev["type"] == t) for t in ("budget.warning", "budget.reached")}
    assert index["budget.warning"] < index["budget.reached"]
    warning = events[index["budget.warning"]]["data"]["budget"]
    reached = events[index["budget.reached"]]["data"]["budget"]
    assert warning["state"] == "warning" and warning["used_tokens"] >= 0.8 * warning["max_tokens"]
    assert reached["state"] == "reached" and reached["used_tokens"] >= reached["max_tokens"] and reached["force_stop"] is True
    # the pause because of the budget, then the user raises the limit and presses 继续
    paused = next(i for i, ev in enumerate(events) if ev["type"] == "game.status" and ev["data"]["reason"] == "budget")
    assert events[paused]["data"]["status"] == "paused" and paused > index["budget.reached"]
    update = next(ev["data"] for ev in events[paused:] if ev["type"] == "session.updated" and ev["data"]["pause_reason"] == "budget")
    assert update["status"] == "paused" and "预算" in update["active_status_text"]
    raised = next(ev["data"]["budget"] for ev in events[paused:] if ev["type"] == "budget.updated" and ev["data"]["budget"]["max_tokens"] > reached["max_tokens"])
    assert raised["state"] == "ok"
    resumed = next(i for i, ev in enumerate(events[paused:], paused) if ev["type"] == "game.status" and ev["data"]["status"] == "running")
    assert resumed > paused
    assert replay["final"]["budget"]["used_tokens"] == replay["final"]["usage_stats"]["total_tokens"]
    # token kinds: the cache columns are used (E11 / E22)
    usage = replay["final"]["usage_stats"]
    assert usage["cache_read_tokens"] > 0 and usage["cache_write_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["cache_read_tokens"] + usage["cache_write_tokens"] + usage["completion_tokens"]


def test_the_review_reports_progress_for_every_player_and_marks_them_done(replay: dict) -> None:
    progress = [ev["data"] for ev in _events(replay, "review.progress")]
    assert {d["id"] for d in progress} == set(IDS)
    for pid in IDS:
        states = [d["state"] for d in progress if d["id"] == pid]
        assert states == ["running", "done"], pid
    # the review only starts after the game finished, and it writes the stats into the memory text
    first = next(i for i, ev in enumerate(replay["events"]) if ev["type"] == "review.progress")
    last_finish = max(i for i, ev in enumerate(replay["events"]) if ev["type"] == "game.status" and ev["data"]["status"] == "finished")
    assert first > last_finish
    assert replay["final"]["review"] == {"available": True, "done": IDS, "running": False}


def _all_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            keys.add(k)
            keys |= _all_keys(v)
    elif isinstance(value, list):
        for v in value:
            keys |= _all_keys(v)
    return keys


def test_no_thoughts_or_tool_details_travel_on_the_wire(replay: dict) -> None:
    keys = _all_keys(replay["events"]) | _all_keys(replay["final"])
    for banned in ("thought", "thoughts", "reasoning", "reasoning_content", "tool_name", "tool_args", "tool_result", "stream_chunk", "streaming_draft"):
        assert banned not in keys, banned
    # "tool_calls" exists, but only as the per-agent COUNT inside agent.metrics / usage_stats (never details)
    for ev in replay["events"]:
        if ev["type"] == "agent.metrics":
            assert isinstance(ev["data"]["tool_calls"], int)
        else:
            assert "tool_calls" not in _all_keys(ev["data"]), ev["type"]
    for m in replay["final"]["usage_stats"]["by_actor"].values():
        assert isinstance(m["tool_calls"], int)
    types = {ev["type"] for ev in replay["events"]}
    assert not {t for t in types if t.startswith(("thought", "tool", "stream"))}


def test_challenge_messages_are_private_to_the_player_and_the_moderator(replay: dict) -> None:
    for m in replay["final"]["messages"]:
        if m["kind"] in ("challenge", "reply"):
            assert m["channel_id"].startswith("dm-")
            assert set(m["visible_to_ids"]) == {"moderator", m["channel_id"][3:]}
            assert m["ticket_id"]
        if m["channel_id"] == "wolves":
            assert set(m["visible_to_ids"]) <= {"moderator", "N3", "N6", "N10", "N12"}


def test_validator_rejects_broken_events() -> None:
    ok = {"id": 1, "type": "phase.changed", "data": {"text": "x"}}
    assert mock.validate_event(ok) == []
    bad = [
        {"id": 1, "type": "phase.changed", "data": {}},
        {"id": 1, "type": "no.such.event", "data": {}},
        {"id": "1", "type": "phase.changed", "data": {"text": "x"}},
        # three agent states only; a wake-up time only for a sleeping agent
        {"id": 1, "type": "agent.status", "data": {"id": "N1", "state": "stopped", "until": None}},
        {"id": 1, "type": "agent.status", "data": {"id": "N1", "state": "idle", "until": None}},
        {"id": 1, "type": "agent.status", "data": {"id": "N1", "state": "thinking", "until": 5.0}},
        {"id": 1, "type": "agent.status", "data": {"id": "N1", "state": "sleeping"}},
        # progress carries no answers; a closed request carries its result
        {"id": 1, "type": "request.progress", "data": {"request_id": "r1", "answered_by": [], "total": 3, "answers": {"N1": "N2"}}},
        {"id": 1, "type": "request.closed", "data": {"request_id": "r1", "reason": "deadline", "answered_by": [], "defaulted": []}},
        {"id": 1, "type": "request.closed", "data": {"request_id": "r1", "reason": "whatever", "answered_by": [], "defaulted": [], "result": None}},
        {"id": 1, "type": "request.answer", "data": {"request_id": "r1", "player_id": "N1", "value": "N2", "label": "2号", "reason": ""}},
        {"id": 1, "type": "game.status", "data": {"status": "finished"}},
        {"id": 1, "type": "session.updated", "data": {"status": "paused", "participants": []}},
        {"id": 1, "type": "session.updated", "data": {"execution_depth": "high"}},
        {"id": 1, "type": "budget.warning", "data": {"budget": {"max_tokens": 10, "state": "careful"}}},
        {"id": 1, "type": "review.progress", "data": {"id": "N1", "state": "finished", "message": ""}},
        {"id": 1, "type": "roster.updated", "data": {"participants": [{"id": "N1", "name": "A", "avatar": {"v": 2}}]}},
        {"id": 1, "type": "message.committed", "data": {"seq": 1, "ts": "x", "channel_id": "public", "from": "N1", "from_name": "A",
                                                          "content": "c", "kind": "speech", "template_id": None, "truncated": None,
                                                          "ticket_id": None, "visible_to_ids": [], "thought": "secret"}},
    ]
    for ev in bad:
        assert mock.validate_event(ev), ev
    good = [
        {"id": 2, "type": "agent.status", "data": {"id": "N1", "state": "sleeping", "until": 42.5}},
        {"id": 3, "type": "agent.status", "data": {"id": "N1", "state": "acting", "until": None}},
        {"id": 4, "type": "review.progress", "data": {"id": "N1", "state": "done", "message": "ok"}},
        {"id": 5, "type": "archive.progress", "data": {"job_id": "j1", "kind": "export", "phase": "pack", "done": 3, "total": 12, "status": "running"}},
    ]
    for ev in good:
        assert mock.validate_event(ev) == [], ev
    session = mock.replay_fast(epilogue=False, review=False)["initial"]
    assert mock.validate_session(session) == []
    broken = copy.deepcopy(session)
    broken["execution_depth"] = "high"
    broken["participants"][0]["state"] = "stopped"
    broken["budget"]["state"] = "weird"
    problems = mock.validate_session(broken)
    assert len(problems) >= 3


# --------------------------------------------------------------------------------- the real store
@pytest.mark.skipif(NODE is None, reason="node executable not found")
def test_the_client_store_reduces_the_event_log_to_the_servers_final_session(replay: dict, tmp_path: Path) -> None:
    log = tmp_path / "replay.json"
    log.write_text(json.dumps(replay, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [NODE, str(ROOT / "tests" / "frontend" / "replay_check.js"), str(log)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "replay ok" in result.stdout


@pytest.mark.skipif(NODE is None, reason="node executable not found")
def test_the_store_ignores_a_replayed_prefix_after_a_reconnect(replay: dict, tmp_path: Path) -> None:
    """Events <= the last applied id are skipped by the app; applying the log twice via the store's own
    staleness rule must therefore equal applying it once."""
    script = tmp_path / "twice.js"
    script.write_text(
        """
const fs = require('fs'); const assert = require('assert');
const Store = require(process.argv[2]);
const d = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
let s = Store.normalizeSession(JSON.parse(JSON.stringify(d.initial)));
let last = 0;
const feed = (events) => { for (const ev of events) { if (Store.isStaleEvent(last, ev)) continue; s = Store.applyEvent(s, ev).session; last = Math.max(last, ev.id); } };
feed(d.events.slice(0, 200));
feed(d.events.slice(150));            // a resume that re-sends 150..200
const once = Store.normalizeSession(JSON.parse(JSON.stringify(d.initial)));
let t = once; for (const ev of d.events) t = Store.applyEvent(t, ev).session;
assert.deepStrictEqual(s.messages, t.messages);
assert.deepStrictEqual(s.usage_stats.by_actor, t.usage_stats.by_actor);
assert.deepStrictEqual(s.requests.map((r) => r.answers), t.requests.map((r) => r.answers));
assert.deepStrictEqual(s.budget, t.budget);
console.log('ok');
""",
        encoding="utf-8",
    )
    log = tmp_path / "replay.json"
    log.write_text(json.dumps(replay, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [NODE, str(script), str(ROOT / "static" / "js" / "store.js"), str(log)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------------------------------- bus / resume
def test_event_bus_resume_semantics() -> None:
    async def scenario() -> None:
        bus = mock.EventBus(ring=5)
        for i in range(8):
            bus.publish("phase.changed", {"text": str(i)})
        assert bus.latest_id == 8
        # inside the ring: everything after the id is replayed
        replay, q = bus.subscribe(6)
        assert [e["id"] for e in replay] == [7, 8]
        bus.unsubscribe(q)
        # exactly at the head: nothing to replay (no snapshot needed)
        replay, q = bus.subscribe(8)
        assert replay == []
        bus.unsubscribe(q)
        # the ring no longer holds event 3 (ids 4..8 remain): a snapshot is needed
        replay, q = bus.subscribe(2)
        assert replay is None
        bus.unsubscribe(q)
        # the oldest still-continuous id
        replay, q = bus.subscribe(3)
        assert [e["id"] for e in replay] == [4, 5, 6, 7, 8]
        bus.unsubscribe(q)
        # unknown / future ids and first connections: snapshot
        for last in (None, 99, -1):
            replay, q = bus.subscribe(last)
            assert replay is None
            bus.unsubscribe(q)
        # live delivery to a subscriber
        replay, q = bus.subscribe(8)
        bus.publish("phase.changed", {"text": "live"})
        ev = await asyncio.wait_for(q.get(), 1)
        assert ev["id"] == 9
        # drop_all closes every stream (a None sentinel) so browsers reconnect
        assert bus.drop_all() == 1
        assert await asyncio.wait_for(q.get(), 1) is None

    asyncio.run(scenario())


def test_new_game_snapshot_carries_its_own_event_id_and_keeps_the_budget_limits() -> None:
    async def scenario() -> None:
        bus = mock.EventBus()
        game = mock.MockGame(bus, speed=1_000_000)
        old_id = game.session["session_id"]
        game.set_budget(max_tokens=777_000, max_cost_usd=3.5, force_stop=False)
        await game.reset()
        ev = bus.ring[-1]
        assert ev["type"] == "snapshot"
        assert ev["data"]["session"]["session_id"] != old_id
        assert ev["data"]["session"]["event_id"] == ev["id"]
        assert mock.validate_event(ev) == []
        budget = ev["data"]["session"]["budget"]
        assert (budget["max_tokens"], budget["max_cost_usd"], budget["force_stop"]) == (777_000, 3.5, False)
        assert (budget["used_tokens"], budget["used_cost_usd"], budget["state"]) == (0, 0, "ok")
        assert ev["data"]["session"]["review"] == {"available": False, "done": [], "running": False}

    asyncio.run(scenario())


def test_pause_puts_everybody_to_sleep_and_resume_continues_it() -> None:
    async def scenario() -> None:
        bus = mock.EventBus(ring=50000)
        game = mock.MockGame(bus, speed=40.0, budget=mock.default_budget(None, False))  # >= 1.3 s per game
        game.start()
        await asyncio.sleep(0.15)
        assert game.session["status"] == "running"
        game.pause()
        assert game.session["status"] == "paused"
        assert game.session["pause_reason"] == "user"
        assert {p["state"] for p in game.session["participants"]} == {"sleeping"}
        assert {p["sleep_until"] for p in game.session["participants"]} == {None}
        assert game.session["moderator"]["state"] == "sleeping"
        frozen = bus.latest_id
        clock = game.session["clock"]
        await asyncio.sleep(0.3)
        assert bus.latest_id == frozen, "nothing is published while paused"
        assert game.session["clock"] == clock, "the game clock stops with the game (D18)"
        game.start()  # resume
        assert game.session["status"] == "running"
        for _ in range(400):
            if game.session["status"] == "finished":
                break
            await asyncio.sleep(0.05)
        assert game.session["status"] == "finished"
        assert game.session["review"]["available"] is True
        done = bus.latest_id
        # continue after the verdict replays the epilogue and finishes again
        n_messages = len(game.session["messages"])
        game.start()
        assert game.session["status"] == "running"
        assert game.session["winner"] is None  # the verdict is withdrawn while continuing
        assert game.session["review"]["available"] is False
        for _ in range(400):
            if game.session["status"] == "finished":
                break
            await asyncio.sleep(0.05)
        assert game.session["status"] == "finished"
        assert len(game.session["messages"]) > n_messages
        assert bus.latest_id > done
        assert mock.validate_session(game.snapshot_payload()) == []

    asyncio.run(scenario())


def test_a_budget_pause_refuses_to_resume_until_the_limit_is_raised_or_force_stop_is_off() -> None:
    async def scenario() -> None:
        bus = mock.EventBus(ring=50000)
        game = mock.MockGame(bus, speed=400.0, budget=mock.default_budget(20_000, True))
        game.start()
        for _ in range(400):
            if game.session["status"] == "paused":
                break
            await asyncio.sleep(0.02)
        s = game.session
        assert (s["status"], s["pause_reason"]) == ("paused", "budget")
        assert s["budget"]["state"] == "reached" and s["budget"]["used_tokens"] >= 20_000
        assert "预算" in s["active_status_text"]
        assert {p["state"] for p in s["participants"]} == {"sleeping"}
        with pytest.raises(ValueError, match="预算上限"):
            game.start()  # still over the limit
        assert s["status"] == "paused"
        # raising the limit clears the state; the user may now continue
        game.set_budget(max_tokens=10_000_000, max_cost_usd=None, force_stop=True)
        assert s["budget"]["state"] == "ok"
        game.start()
        assert s["status"] == "running" and s["pause_reason"] == ""
        for _ in range(600):
            if s["status"] == "finished":
                break
            await asyncio.sleep(0.02)
        assert s["status"] == "finished"
        # without "force stop" the game just goes on after the limit is reached
        bus2 = mock.EventBus(ring=50000)
        game2 = mock.MockGame(bus2, speed=400.0, budget=mock.default_budget(20_000, False))
        game2.start()
        for _ in range(600):
            if game2.session["status"] == "finished":
                break
            await asyncio.sleep(0.02)
        assert game2.session["status"] == "finished"
        assert game2.session["budget"]["state"] == "reached"
        assert not any(ev["type"] == "game.status" and ev["data"]["reason"] == "budget" for ev in bus2.ring)

    asyncio.run(scenario())


def test_hold_sets_pause_reason_and_the_status_text_carries_the_reason() -> None:
    async def scenario() -> None:
        bus = mock.EventBus(ring=50000)
        game = mock.MockGame(bus, speed=100.0, budget=mock.default_budget(None, False))
        game.start()
        await asyncio.sleep(0.1)
        game.pause(reason="hold", hold_reason="请确认警长归属")
        s = game.session
        assert (s["status"], s["pause_reason"], s["hold_reason"]) == ("paused", "hold", "请确认警长归属")
        assert "请确认警长归属" in s["active_status_text"]
        update = [e for e in bus.ring if e["type"] == "session.updated"][-1]["data"]
        assert update["pause_reason"] == "hold" and update["hold_reason"] == "请确认警长归属"
        await game._cancel_task()

    asyncio.run(scenario())


def test_the_review_only_works_after_the_end_and_never_twice() -> None:
    async def scenario() -> None:
        bus = mock.EventBus(ring=50000)
        game = mock.MockGame(bus, speed=1_000_000)
        with pytest.raises(ValueError, match="对局结束后"):
            game.start_review(None)
        await game.jump_to_end()
        assert game.session["status"] == "finished" and game.session["review"]["available"] is True
        assert game.start_review(["N3", "N4", "nobody"]) == ["N3", "N4"]
        with pytest.raises(ValueError, match="进行中"):
            game.start_review(["N5"])
        await game.review_task
        assert game.session["review"]["done"] == ["N3", "N4"]
        with pytest.raises(ValueError, match="都已复盘"):
            game.start_review(["N3"])
        assert game.start_review(None) == [p for p in IDS if p not in ("N3", "N4")]
        await game.review_task
        assert game.session["review"] == {"available": True, "done": IDS, "running": False}
        # the memory text of a reviewed player got the stats
        assert "战绩" in game.roster.seats["N3"]["memory"]

    asyncio.run(scenario())


# --------------------------------------------------------------------------------- HTTP
def _client():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    app = mock.create_app(speed=2000.0)
    return app, TestClient(app)


def _wait_for(predicate, timeout: float = 10.0, step: float = 0.03):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("timed out")


def _bus(app):
    return app.state.mock_holder["game"].bus


def test_http_state_pages_and_static_files() -> None:
    app, c = _client()
    with c as client:
        state = client.get("/api/state").json()
        assert state["app_version"].startswith("v0.13.0")
        assert mock.validate_session(state["session"]) == []
        assert state["session"]["status"] == "idle"
        assert [p["id"] for p in state["session"]["participants"]] == IDS
        assert state["session"]["budget"]["max_tokens"] == mock.DEMO_BUDGET_TOKENS
        assert state["session"]["memory_enabled"] is True
        assert "workspace" not in state, "v0.13: there is no workspace any more"
        assert state["legacy_data"] == {"found": False, "dismissed": False}
        assert client.get("/api/health").json()["version"].startswith("v0.13.0")

        index = client.get("/")
        assert index.status_code == 200 and 'data-platform' in index.text
        assert index.headers["cache-control"].startswith("no-store")
        for script in ("app", "core", "store", "view", "avatar", "platform", "layout", "providers", "roster", "budget", "archive"):
            assert client.get(f"/static/js/{script}.js").status_code == 200, script
        assert client.get("/static/css/style.css").status_code == 200
        parts = client.get("/static/avatar/parts.json")
        assert parts.status_code == 200
        library = parts.json()
        assert library["version"] == 1 and library["size"] == 20 and {"face", "hair", "eyes", "mouth", "accessory"} <= set(library["parts"])
        # every avatar of the roster can be drawn from the library the page will get
        for seat in client.get("/api/roster").json()["seats"]:
            av = seat["avatar"]
            assert av["face"] in library["parts"]["face"] and av["hair"] in library["parts"]["hair"]
            assert av["skin"] in library["palettes"]["skin"] and av["hair_color"] in library["palettes"]["hair_color"]

        assert client.get("/api/providers").json()["providers"][0]["models"]
        bindings = client.get("/api/settings/bindings").json()
        assert "moderator" in bindings["bindings"] and bindings["providers"]
        assert client.get("/api/workspaces").status_code in (404, 405)  # v0.13: no workspaces
        assert client.get("/static/js/workspace.js").status_code == 404
        assert client.get("/api/games").json()["games"][0]["current"] is True
        assert client.get("/api/sessions").status_code in (404, 405)  # v0.13: replaced by /api/games
        assert client.get("/api/settings/templates").json()["slots"] == 3
        # the execution depth is gone (backend removed it)
        assert client.post("/api/session/execution-depth", json={"execution_depth": "high"}).status_code in (404, 405)


def test_http_controls_game_flow_and_the_budget_pause() -> None:
    app, c = _client()
    with c as client:
        # control endpoints answer {"ok", "session"} with a PublicSession v3
        started = client.post("/api/control/start").json()
        assert started["ok"] is True and mock.validate_session(started["session"]) == []
        assert started["session"]["status"] == "running"
        # the demo budget (150K, force stop) pauses the game at 100 %
        info = _wait_for(lambda: (i := client.get("/api/mock/info").json())["status"] == "paused" and i)
        assert info["budget"]["state"] == "reached"
        paused = client.get("/api/state").json()["session"]
        assert (paused["status"], paused["pause_reason"]) == ("paused", "budget")
        # 继续 is refused while the limit still stops the game ...
        refused = client.post("/api/control/start")
        assert refused.status_code == 409 and "上限" in refused.json()["detail"]
        # ... the user raises the limit in the dialog (PUT /api/budget) and continues
        raised = client.put("/api/budget", json={"max_tokens": 50_000_000, "max_cost_usd": None, "force_stop": True}).json()
        assert raised["state"] == "ok" and raised["max_tokens"] == 50_000_000
        resumed = client.post("/api/control/start").json()["session"]
        assert resumed["status"] == "running" and resumed["pause_reason"] == ""
        _wait_for(lambda: client.get("/api/mock/info").json()["status"] == "finished")
        finished = client.get("/api/state").json()["session"]
        assert finished["winner"] and finished["review"]["available"] is True
        assert finished["event_id"] == client.get("/api/mock/info").json()["latest_event_id"]
        assert mock.validate_session(finished) == []

        # force-pause is accepted (it equals pause); nothing is running now, so it is a no-op
        assert client.post("/api/control/force-pause").json()["ok"] is True
        # "继续" after the verdict replays the epilogue; slow the replay down so it can be paused midway
        app.state.mock_holder["game"].speed = 40.0
        resumed = client.post("/api/control/start").json()["session"]
        assert resumed["status"] == "running"
        assert resumed["winner"] is None  # the verdict is withdrawn while the game continues
        assert resumed["review"]["available"] is False
        paused = client.post("/api/control/pause").json()["session"]
        assert (paused["status"], paused["pause_reason"]) == ("paused", "user")
        assert {p["state"] for p in paused["participants"]} == {"sleeping"}
        assert client.post("/api/control/pause").json()["session"]["status"] == "paused"  # idempotent
        # the moderator's hold_game: only a running game can be put on hold, so resume first
        assert client.post("/api/control/start").json()["session"]["status"] == "running"
        hold = client.post("/api/mock/hold", json={"reason": "测试暂停"}).json()["session"]
        assert (hold["status"], hold["pause_reason"], hold["hold_reason"]) == ("paused", "hold", "测试暂停")
        assert "测试暂停" in hold["active_status_text"]

        new = client.post("/api/control/new").json()["session"]
        assert new["status"] == "idle" and new["session_id"] != finished["session_id"]
        assert new["messages"] == [] and mock.validate_session(new) == []
        assert new["budget"]["max_tokens"] == 50_000_000, "the limits survive a new game, the used amounts do not"
        assert new["budget"]["used_tokens"] == 0 and new["budget"]["state"] == "ok"
        assert new["review"] == {"available": False, "done": [], "running": False}
        assert client.post("/api/session/save").status_code in (404, 405)  # v0.13: the game saves itself
        # a bad mock-only event is refused by the validator
        assert client.post("/api/mock/emit", json={"type": "agent.status", "data": {"id": "N1", "state": "x", "until": None}}).status_code == 422
        assert client.post("/api/mock/emit", json={"type": "phase.changed", "data": {"text": "ok"}}).json()["ok"] is True


def test_http_roster_names_and_avatars_are_idle_only_and_publish_roster_updated() -> None:
    app, c = _client()
    with c as client:
        roster = client.get("/api/roster").json()
        assert roster["editable"] is True and roster["memory_enabled"] is True
        seats = roster["seats"]
        assert [s["id"] for s in seats] == IDS and [s["name"] for s in seats] == NAMES and [s["seat"] for s in seats] == list(range(1, 13))
        assert all(s["default_name"] == s["name"] and s["persona"] == "" and s["memory"] == "" for s in seats)
        assert all(mock.validate_avatar(s["avatar"]) == [] and s["avatar"] is not None for s in seats)
        before_ids = _bus(app).latest_id if "game" in app.state.mock_holder else 0

        # rename: PUT /api/roster/{id}
        r = client.put("/api/roster/N3", json={"name": "  Caroline "})
        assert r.status_code == 200 and r.json()["name"] == "Caroline"
        participants = client.get("/api/state").json()["session"]["participants"]
        assert next(p for p in participants if p["id"] == "N3")["name"] == "Caroline"
        updates = [e for e in _bus(app).ring if e["type"] == "roster.updated" and e["id"] > before_ids]
        assert updates and updates[-1]["data"]["participants"] == [{"id": "N3", "name": "Caroline", "avatar": r.json()["avatar"]}]
        assert mock.validate_event(updates[-1]) == []
        # refusals: empty / too long / a duplicate (also of the DEFAULT of another seat) / unknown seat / not a string
        for bad, what in (("", "不能为空"), ("   ", "不能为空"), ("x" * 21, "最多 20"), ("Bob", "已被其他玩家使用"), (7, "必须是文字")):
            resp = client.put("/api/roster/N3", json={"name": bad})
            assert resp.status_code == 422 and what in resp.json()["detail"], (bad, resp.text)
        assert client.put("/api/roster/N3", json={"name": "x" * 20}).status_code == 200  # 20 characters are fine
        assert client.put("/api/roster/N99", json={"name": "Z"}).status_code == 404
        # keeping its own name is not a duplicate
        assert client.put("/api/roster/N3", json={"name": "x" * 20}).status_code == 200

        # regenerate one avatar / all avatars
        old = client.get("/api/roster").json()["seats"]
        one = client.post("/api/roster/N5/avatar:regenerate").json()
        assert one["id"] == "N5" and one["avatar"] != old[4]["avatar"] and mock.validate_avatar(one["avatar"]) == []
        assert client.get("/api/roster").json()["seats"][3]["avatar"] == old[3]["avatar"]
        everyone = client.post("/api/roster/avatars:regenerate").json()["seats"]
        assert len(everyone) == 12 and len({json.dumps(s["avatar"], sort_keys=True) for s in everyone}) == 12
        assert sum(1 for a, b in zip(old, everyone) if a["avatar"] != b["avatar"]) >= 11
        last = [e for e in _bus(app).ring if e["type"] == "roster.updated"][-1]
        assert [p["id"] for p in last["data"]["participants"]] == IDS
        # an explicit avatar description must be valid
        assert client.put("/api/roster/N1", json={"avatar": {"v": 9}}).status_code == 422
        assert client.put("/api/roster/N1", json={"avatar": everyone[1]["avatar"]}).status_code == 200

        # the moderator has a random avatar of its own: it is in the roster and in the session, never a seat
        roster_now = client.get("/api/roster").json()
        moderator = roster_now["moderator"]
        assert moderator["id"] == "moderator" and moderator["name"] == "主持人" and mock.validate_avatar(moderator["avatar"]) == []
        assert moderator["avatar"] is not None and moderator["avatar"] not in [s["avatar"] for s in everyone]
        assert client.get("/api/state").json()["session"]["moderator"]["avatar"] == moderator["avatar"]
        before_mod = _bus(app).latest_id
        rolled = client.post("/api/roster/moderator/avatar:regenerate").json()
        assert rolled["id"] == "moderator" and rolled["avatar"] != moderator["avatar"] and mock.validate_avatar(rolled["avatar"]) == []
        assert [s["avatar"] for s in client.get("/api/roster").json()["seats"]] == [s["avatar"] for s in roster_now["seats"]]  # players untouched
        event = [e for e in _bus(app).ring if e["type"] == "roster.updated" and e["id"] > before_mod][-1]
        assert event["data"]["participants"] == [] and event["data"]["moderator"] == {"id": "moderator", "avatar": rolled["avatar"]}
        assert mock.validate_event(event) == []
        assert client.get("/api/state").json()["session"]["moderator"]["avatar"] == rolled["avatar"]
        everybody = client.post("/api/roster/avatars:regenerate").json()
        assert set(everybody) == {"seats", "moderator"} and everybody["moderator"]["avatar"] != rolled["avatar"]
        assert everybody["moderator"]["avatar"] not in [s["avatar"] for s in everybody["seats"]]
        # the moderator only has an avatar to change
        chosen = everybody["seats"][1]["avatar"]
        assert client.put("/api/roster/moderator", json={"avatar": chosen}).json() == {"id": "moderator", "name": "主持人", "avatar": chosen}
        for body in ({"name": "Boss"}, {"persona": "x"}, {"memory": "x"}):
            assert client.put("/api/roster/moderator", json=body).status_code == 400
        assert client.put("/api/roster/moderator", json={"avatar": {"v": 9}}).status_code == 422
        assert client.post("/api/roster/moderator/refine", json={"kind": "persona", "text": "x"}).status_code == 400

        # once the game started: names and avatars are locked (409), the texts stay editable
        client.post("/api/control/start")
        assert client.get("/api/roster").json()["editable"] is False
        assert client.put("/api/roster/N3", json={"name": "Nope"}).status_code == 409
        assert client.post("/api/roster/N3/avatar:regenerate").status_code == 409
        assert client.post("/api/roster/moderator/avatar:regenerate").status_code == 409
        assert client.put("/api/roster/moderator", json={"avatar": chosen}).status_code == 409
        assert client.post("/api/roster/avatars:regenerate").status_code == 409
        assert client.put("/api/roster/N3", json={"persona": "still fine"}).status_code == 200
        assert client.put("/api/roster/memory-enabled", json={"enabled": False}).status_code == 409


def test_http_roster_persona_memory_and_refine() -> None:
    app, c = _client()
    with c as client:
        r = client.put("/api/roster/N3", json={"persona": "说话简短", "memory": "Bob 总是附和"})
        assert r.status_code == 200 and (r.json()["persona"], r.json()["memory"]) == ("说话简短", "Bob 总是附和")
        seat = next(s for s in client.get("/api/roster").json()["seats"] if s["id"] == "N3")
        assert (seat["persona"], seat["memory"]) == ("说话简短", "Bob 总是附和")
        assert client.put("/api/roster/N3", json={"persona": "x" * 20001}).status_code == 422
        assert client.put("/api/roster/N3", json={"memory": 5}).status_code == 422
        # a text edit does not rename: no roster.updated for it
        assert not [e for e in _bus(app).ring if e["type"] == "roster.updated"]

        # "用主持人模型完善": a DRAFT comes back, nothing is saved by the call
        for kind in ("persona", "memory"):
            draft = client.post("/api/roster/N3/refine", json={"kind": kind, "text": "爱反问"})
            assert draft.status_code == 200 and "爱反问" in draft.json()["draft"], kind
        assert next(s for s in client.get("/api/roster").json()["seats"] if s["id"] == "N3")["persona"] == "说话简短"
        assert client.post("/api/roster/N3/refine", json={"kind": "persona", "text": "  "}).status_code == 422
        assert client.post("/api/roster/N3/refine", json={"kind": "mood", "text": "x"}).status_code == 422
        assert client.post("/api/roster/N99/refine", json={"kind": "persona", "text": "x"}).status_code == 404


def test_http_memory_clear_and_the_load_memory_switch() -> None:
    app, c = _client()
    with c as client:
        client.put("/api/roster/N3", json={"persona": "P3", "memory": "M3"})
        client.put("/api/roster/N5", json={"memory": "M5"})
        # (v0.13: the memory pack export / import is gone - the archive covers it)
        assert client.get("/api/roster/memory:export").status_code in (404, 405)
        assert client.post("/api/roster/memory:import?dry_run=true", content=b"x").status_code in (404, 405)

        # clear: memory only, or memory and persona
        assert client.post("/api/roster/memory:clear", json={"include_persona": False}).json() == {"ok": True}
        seats = {s["id"]: s for s in client.get("/api/roster").json()["seats"]}
        assert (seats["N3"]["persona"], seats["N3"]["memory"], seats["N5"]["memory"]) == ("P3", "", "")
        assert client.post("/api/roster/memory:clear", json={"include_persona": True}).status_code == 200
        assert next(s for s in client.get("/api/roster").json()["seats"] if s["id"] == "N3")["persona"] == ""
        assert client.post("/api/roster/memory:clear").status_code == 200  # no body: memory only

        # "本局加载记忆": PUT /api/roster/memory-enabled publishes session.updated
        before = _bus(app).latest_id
        assert client.put("/api/roster/memory-enabled", json={"enabled": False}).json() == {"enabled": False}
        assert client.get("/api/roster").json()["memory_enabled"] is False
        assert client.get("/api/state").json()["session"]["memory_enabled"] is False
        update = [e for e in _bus(app).ring if e["type"] == "session.updated" and e["id"] > before][-1]
        assert update["data"]["memory_enabled"] is False and mock.validate_event(update) == []
        assert client.put("/api/roster/memory-enabled", json={"enabled": True}).json() == {"enabled": True}


def test_http_budget_dialog_endpoints() -> None:
    app, c = _client()
    with c as client:
        b = client.get("/api/budget").json()
        assert (b["max_tokens"], b["max_cost_usd"], b["force_stop"]) == (mock.DEMO_BUDGET_TOKENS, None, True)
        assert mock.validate_budget(b) == [] and b["used_tokens"] == 0 and b["state"] == "ok"
        saved = client.put("/api/budget", json={"max_tokens": 1000, "max_cost_usd": 0.5, "force_stop": False})
        assert saved.status_code == 200
        assert (saved.json()["max_tokens"], saved.json()["max_cost_usd"], saved.json()["force_stop"]) == (1000, 0.5, False)
        assert client.get("/api/budget").json() == saved.json()
        assert client.get("/api/state").json()["session"]["budget"] == saved.json()
        # a null clears the limit
        cleared = client.put("/api/budget", json={"max_tokens": None, "max_cost_usd": None, "force_stop": True}).json()
        assert (cleared["max_tokens"], cleared["max_cost_usd"], cleared["force_stop"]) == (None, None, True)
        for bad in ({"max_tokens": 0}, {"max_tokens": -5}, {"max_tokens": 1.5}, {"max_tokens": "10"}, {"max_cost_usd": 0}, {"max_cost_usd": -1}, {"max_cost_usd": "x"}):
            resp = client.put("/api/budget", json=bad)
            assert resp.status_code == 422, bad
        # the saved limits are published to every client as budget.updated
        events = [e for e in _bus(app).ring if e["type"] == "budget.updated"]
        assert events and mock.validate_event(events[-1]) == []
        assert events[-1]["data"]["budget"]["max_tokens"] is None


def test_http_price_source_four_prices_and_suggestions() -> None:
    app, c = _client()
    with c as client:
        src = client.get("/api/pricing/source").json()
        assert src["url"] == src["default_url"] == mock.DEFAULT_PRICING_URL
        assert src["entries"] > 0 and src["error"] is None and src["fetched_at"]
        # only https URLs
        assert client.put("/api/pricing/source", json={"url": "http://example.com/p.json"}).status_code == 422
        assert client.put("/api/pricing/source", json={"url": ""}).status_code == 422
        ok = client.put("/api/pricing/source", json={"url": "https://example.com/prices.json"}).json()
        assert ok["url"] == "https://example.com/prices.json" and client.get("/api/pricing/source").json()["url"] == ok["url"]
        refreshed = client.post("/api/pricing/refresh").json()
        assert refreshed["error"] is None and refreshed["entries"] >= src["entries"]
        # a failing source keeps the entries and reports the error
        client.put("/api/pricing/source", json={"url": "https://example.com/fail.json"})
        failed = client.post("/api/pricing/refresh").json()
        assert failed["error"] and failed["entries"] == refreshed["entries"]

        # models carry four prices + their source
        providers = {p["id"]: p for p in client.get("/api/providers").json()["providers"]}
        chat = providers["mock-deepseek"]["models"][0]
        assert {"input_price_per_m", "cache_read_price_per_m", "cache_write_price_per_m", "output_price_per_m", "price_source"} <= set(chat)
        assert chat["cache_write_price_per_m"] is None, "cache prices may be empty (null = the input price)"
        assert providers["mock-claude"]["models"][0]["cache_write_price_per_m"] == 3.75

        # discover-models brings a `suggested` price per model (null without a match)
        found = client.post("/api/providers/mock-deepseek/discover-models").json()
        by_id = {m["id"]: m["suggested"] for m in found["models"]}
        assert by_id["mock-new-model"] is None
        sug = by_id["deepseek-reasoner"]
        assert (sug["input_price_per_m"], sug["output_price_per_m"], sug["cache_read_price_per_m"], sug["cache_write_price_per_m"]) == (0.55, 2.19, 0.14, None)
        assert sug["context_window_tokens"] == 128000 and sug["key"] == "deepseek/deepseek-reasoner"
        assert "deepseek-chat" in found["existing_models"]
        # ... and GET /api/pricing/match answers for a hand-typed id (case, date suffix, provider prefix)
        m = client.get("/api/pricing/match", params={"provider_id": "mock-claude", "model_id": "claude-sonnet-4-5-20250929"}).json()
        assert m["matched"] is True and m["cache_write_price_per_m"] == 3.75 and m["input_price_per_m"] == 3.0
        assert client.get("/api/pricing/match", params={"provider_id": "mock-deepseek", "model_id": "DeepSeek-Chat"}).json()["matched"] is True
        assert client.get("/api/pricing/match", params={"provider_id": "mock-deepseek", "model_id": "nope"}).json()["matched"] is False

        # saving a model: four prices; the source says whether they are the table's or hand-made
        same = {"model_id": "deepseek-reasoner", "input_price_per_m": 0.55, "cache_read_price_per_m": 0.14, "cache_write_price_per_m": None,
                "output_price_per_m": 2.19, "context_window_tokens": 128000}
        created = client.post("/api/providers/mock-deepseek/models", json=same)
        assert created.status_code == 201 and created.json()["price_source"] == "litellm:deepseek/deepseek-reasoner"
        edited = client.post("/api/providers/mock-deepseek/models", json={**same, "model_id": "mock-new-model", "output_price_per_m": 9.9})
        assert edited.json()["price_source"] == "manual" and edited.json()["cache_write_price_per_m"] is None
        client.patch("/api/providers/mock-deepseek/models", json={"model_id": "deepseek-reasoner", "cache_write_price_per_m": 0.6})
        patched = next(m for m in client.get("/api/providers").json()["providers"][0]["models"] if m["id"] == "deepseek-reasoner")
        assert patched["cache_write_price_per_m"] == 0.6 and patched["price_source"] == "manual"


def test_http_review_after_the_game_and_the_stall_notice() -> None:
    app, c = _client()
    with c as client:
        # no review before the verdict
        started = client.post("/api/game/review", json={})
        assert started.status_code == 409 and "对局结束后" in started.json()["detail"]
        client.post("/api/mock/finish")
        assert client.get("/api/state").json()["session"]["review"]["available"] is True
        assert client.post("/api/game/review", json={"ids": ["N1"]}).json() == {"started": ["N1"]}
        _wait_for(lambda: client.get("/api/state").json()["session"]["review"]["done"] == ["N1"])
        assert client.post("/api/game/review", json={"ids": ["N1"]}).status_code == 409  # already reviewed
        rest = client.post("/api/game/review").json()["started"]  # no body: everybody who is left
        assert rest == IDS[1:]
        _wait_for(lambda: client.get("/api/state").json()["session"]["review"]["done"] == IDS)
        assert client.post("/api/game/review").status_code == 409
        progress = [e["data"] for e in _bus(app).ring if e["type"] == "review.progress"]
        assert [(d["id"], d["state"]) for d in progress] == [(pid, st) for pid in IDS for st in ("running", "done")]
        # the review wrote into the players' memory text (visible in the roster)
        assert "战绩" in next(s for s in client.get("/api/roster").json()["seats"] if s["id"] == "N4")["memory"]

        # the watchdog's stall notice: a banner, no pause; an empty notice clears it
        out = client.post("/api/mock/stall", json={"notice": "对局似乎停滞了"}).json()
        assert out["stall_notice"] == "对局似乎停滞了"
        update = [e for e in _bus(app).ring if e["type"] == "session.updated"][-1]
        assert update["data"]["stall_notice"] == "对局似乎停滞了" and mock.validate_event(update) == []
        assert client.get("/api/state").json()["session"]["stall_notice"] == "对局似乎停滞了"
        assert client.post("/api/mock/stall", json={"notice": ""}).json()["stall_notice"] == ""
        assert client.get("/api/state").json()["session"]["stall_notice"] == ""


def test_http_mock_helpers_frame_speed_disconnect() -> None:
    app, c = _client()
    with c as client:
        frame = client.get("/api/mock/frame", params={"w": 390, "h": 844, "q": 'platform=phone&input=touch"><script>'})
        assert frame.status_code == 200 and frame.headers["content-type"].startswith("text/html")
        assert "width:390px;height:844px" in frame.text
        assert 'src="/?platform=phone&input=touch' in frame.text and "<script" not in frame.text, "the query is sanitised"
        small = client.get("/api/mock/frame", params={"w": 10, "h": 99999})
        assert "width:200px;height:4000px" in small.text
        assert client.post("/api/mock/speed", json={"speed": 3}).json() == {"ok": True, "speed": 3.0}
        assert client.post("/api/mock/speed", json={"speed": "fast"}).status_code == 400
        assert client.post("/api/mock/speed", json={"speed": 0}).json()["speed"] == 0.01
        assert client.post("/api/mock/disconnect").json() == {"ok": True, "dropped": 0}
        info = client.get("/api/mock/info").json()
        assert {"speed", "latest_event_id", "subscribers", "status", "ring", "clock", "budget"} <= set(info)


# --------------------------------------------------------------------------------- v0.13: the history of games
def test_http_history_of_games_list_delete_load() -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    # normal speed: after "start" the script really is still running when the next request arrives
    with TestClient(mock.create_app(speed=1.0)) as client:
        body = client.get("/api/games").json()
        live = body["current"]
        games = body["games"]
        assert games[0]["session_id"] == live and games[0]["current"] is True
        assert [g["session_id"] for g in games[1:]] == ["mock-old-1", "mock-old-2", "mock-old-3"]
        assert all(g["current"] is False for g in games[1:])
        keys = {"title", "scenario_title", "status", "winner", "created_at", "updated_at", "duration_gt", "players", "tokens", "cost_usd", "human_involved"}
        assert all(keys <= set(g) for g in games)
        assert {g["session_id"]: g["human_involved"] for g in games[1:]} == {"mock-old-1": False, "mock-old-2": True, "mock-old-3": False}

        # the live game cannot be deleted; an unknown / malformed id is a clear error
        assert client.delete(f"/api/games/{live}").status_code == 409
        assert client.delete("/api/games/nope").status_code == 404
        assert client.delete("/api/games/bad.id").status_code == 400
        assert client.delete("/api/games/mock-old-3").json() == {"ok": True, "session_id": "mock-old-3"}
        assert [g["session_id"] for g in client.get("/api/games").json()["games"]][1:] == ["mock-old-1", "mock-old-2"]

        # continuing a game: a running game is not replaced, the live one cannot be loaded again, unknown ones 404
        assert client.post(f"/api/games/{live}/load").status_code == 409
        assert client.post("/api/games/nope/load").status_code == 404
        started = client.post("/api/control/start").json()
        assert started["session"]["status"] == "running"
        refused = client.post("/api/games/mock-old-2/load")
        assert refused.status_code == 409 and "暂停" in refused.json()["detail"]
        client.post("/api/control/pause")
        loaded = client.post("/api/games/mock-old-2/load").json()
        assert loaded["ok"] is True and loaded["session"]["session_id"] == "mock-old-2"
        assert client.get("/api/games").json()["current"] == "mock-old-2"


def test_http_replay_of_a_saved_game_is_the_script_cut_down_to_what_a_ledger_holds() -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    app, c = _client()
    with c as client:
        saved = client.get("/api/games/mock-old-1/replay")
        assert saved.status_code == 200
        data = saved.json()
        assert data["version"] == 1 and set(data) == {"version", "game", "initial", "events", "duration_gt"}
        assert data["game"]["session_id"] == "mock-old-1" and data["game"]["winner"] and data["game"]["human_involved"] is False
        assert data["initial"]["session_id"] == "mock-old-1" and data["initial"]["status"] == "idle"
        events = data["events"]
        assert len(events) > 100 and all(set(ev) == {"seq", "gt", "type", "data"} for ev in events)
        assert {ev["type"] for ev in events} <= mock.REPLAY_EVENT_TYPES, "no agent states, counters or review progress in a replay"
        assert not any(ev["type"].startswith(("agent.", "budget.", "review.")) for ev in events)
        gts = [ev["gt"] for ev in events]
        assert gts == sorted(gts) and gts[-1] == data["duration_gt"] > 30, "game-clock seconds, never going back"
        assert [ev["seq"] for ev in events] == sorted({ev["seq"] for ev in events}), "one step per event, in order"
        assert client.get("/api/games/mock-old-2/replay").json()["game"]["human_involved"] is True
        assert client.get("/api/games/nope/replay").status_code == 404
        assert client.get("/api/games/bad.id/replay").status_code == 400
        # the same answer twice (it is built once)
        assert client.get("/api/games/mock-old-1/replay").json()["events"] == events

    # the live game: not while it runs (normal speed: it is still running when the next request arrives)
    with TestClient(mock.create_app(speed=1.0)) as slow:
        live = slow.get("/api/games").json()["current"]
        assert slow.get(f"/api/games/{live}/replay").status_code == 200  # idle: nothing to spoil
        slow.post("/api/control/start")
        assert slow.get(f"/api/games/{live}/replay").status_code == 409


def test_the_replay_of_a_saved_game_rebuilds_the_final_session_with_the_real_client_store(tmp_path: Path) -> None:
    if NODE is None:
        pytest.skip("node executable not found")
    run = mock.history_replay(seed=7)
    events = [{"id": i, "gt": ev["gt"], "type": ev["type"], "data": ev["data"]} for i, ev in enumerate(run["events"], start=1)]
    log = tmp_path / "history-replay.json"
    # (the mock's script is shorter than a real game: 3 requests, 32 messages, 14 channels)
    minimums = {"messages": 20, "requests": 3, "channels": 14}
    log.write_text(json.dumps({"initial": run["initial"], "events": events, "final": run["final"], "minimums": minimums}, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [NODE, str(ROOT / "tests" / "frontend" / "replay_projection_check.js"), str(log)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_static_files_are_served_from_the_repo_static_directory() -> None:
    assert mock.STATIC_DIR == ROOT / "static"
    assert (mock.STATIC_DIR / "index.html").exists()
    assert mock.DEFAULT_PORT == 8731
    assert mock.PARTS_FIXTURE.exists(), "the minimal sprite library the tests and the fallback use"


def test_selfcheck_cli_passes_and_the_replay_is_deterministic() -> None:
    assert mock.selfcheck() == 0
    # the replay is deterministic for a given seed
    a = mock.replay_fast(seed=3)
    b = mock.replay_fast(seed=3)
    strip = lambda run: [(e["type"], {k: v for k, v in e["data"].items() if k != "ts"}) for e in run["events"]]  # noqa: E731
    assert strip(a) == strip(b)
    c = copy.deepcopy(a["final"]["usage_stats"])
    assert c["total_tokens"] > 0 and c["total_cost_usd"] > 0
    # a different seed plays a different game (the voting order / thinking times differ) but is just as valid
    other = mock.replay_fast(seed=11)
    assert [p for ev in other["events"] for p in mock.validate_event(ev)] == []
    # without the budget demo and review the script is shorter and never pauses
    plain = mock.replay_fast(budget_demo=False, review=False, epilogue=False)
    assert not any(ev["type"] in ("budget.warning", "budget.reached", "review.progress") for ev in plain["events"])
    assert not any(ev["type"] == "game.status" and ev["data"]["reason"] == "budget" for ev in plain["events"])
