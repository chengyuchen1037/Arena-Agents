"""Contract tests for tools/mock_ui_server.py (the UI mock) and, through it, for the SSE v2 client store.

* every event the mock script publishes must match docs/v0.12-contracts.md §8.2 (``validate_event``);
* the REAL client store (static/js/store.js, run with node) must reduce the whole event log to exactly
  the session the mock holds (so the mock's events are complete and the store's reducer is faithful);
* resume (Last-Event-ID) semantics of the mock's event bus;
* the HTTP endpoints the front end calls respond with the shapes it expects.

``validate_event`` / ``replay_fast`` are also meant for checking the real backend's output.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
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


@pytest.fixture(scope="module")
def replay() -> dict:
    return mock.replay_fast()


# --------------------------------------------------------------------------------- the event log
def test_every_event_of_the_scripted_game_matches_the_contract(replay: dict) -> None:
    problems = [f"#{ev['id']} {ev['type']}: {p}" for ev in replay["events"] for p in mock.validate_event(ev)]
    assert problems == []
    assert mock.validate_session(replay["initial"]) == []
    assert mock.validate_session(replay["final"]) == []


def test_the_script_covers_every_event_type_the_front_end_consumes(replay: dict) -> None:
    types = {ev["type"] for ev in replay["events"]}
    required = {
        "message.committed", "channel.created", "channel.updated", "floor.changed", "player.updated",
        "request.opened", "request.progress", "request.closed", "challenge.opened", "challenge.resolved",
        "ability.used", "phase.changed", "dashboard.updated", "game.status", "agent.status", "agent.metrics",
        "session.updated",
    }
    assert required <= types, required - types
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
    channel_updates = [ev["data"]["channel"] for ev in replay["events"] if ev["type"] == "channel.updated"]
    assert any(c["mode"] == "queue" for c in channel_updates)
    assert any(c["mode"] == "free" for c in channel_updates)
    assert any(c["frozen"] for c in channel_updates)
    assert any(ev["data"]["floor"]["kind"] == "queue" and ev["data"]["floor"]["active"] for ev in replay["events"] if ev["type"] == "floor.changed")


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


def test_no_thoughts_tool_calls_or_answers_travel_on_the_wire(replay: dict) -> None:
    keys = _all_keys(replay["events"]) | _all_keys(replay["final"])
    for banned in ("thought", "thoughts", "reasoning", "reasoning_content", "tool_name", "tool_args", "tool_result",
                   "stream_chunk", "streaming_draft", "answers"):
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
    # a sealed vote reports progress only: who answered, never what
    for ev in replay["events"]:
        if ev["type"] == "request.progress":
            assert set(ev["data"]) == {"request_id", "answered_by", "total"}


def test_challenge_messages_are_private_to_the_player_and_the_moderator(replay: dict) -> None:
    for m in replay["final"]["messages"]:
        if m["kind"] in ("challenge", "reply"):
            assert m["channel_id"].startswith("dm-")
            assert set(m["visible_to_ids"]) == {"moderator", m["channel_id"][3:]}
            assert m["ticket_id"]
        if m["channel_id"] == "wolves":
            assert set(m["visible_to_ids"]) <= {"moderator", "p03", "p06", "p10", "p12"}


def test_validator_rejects_broken_events() -> None:
    ok = {"id": 1, "type": "phase.changed", "data": {"text": "x"}}
    assert mock.validate_event(ok) == []
    bad = [
        {"id": 1, "type": "phase.changed", "data": {}},
        {"id": 1, "type": "no.such.event", "data": {}},
        {"id": "1", "type": "phase.changed", "data": {"text": "x"}},
        {"id": 1, "type": "agent.status", "data": {"id": "p01", "state": "sleeping"}},
        {"id": 1, "type": "request.progress", "data": {"request_id": "r1", "answered_by": [], "total": 3, "answers": {"p01": "p02"}}},
        {"id": 1, "type": "game.status", "data": {"status": "finished"}},
        {"id": 1, "type": "session.updated", "data": {"status": "paused", "participants": []}},
        {"id": 1, "type": "message.committed", "data": {"seq": 1, "ts": "x", "channel_id": "public", "from": "p01", "from_name": "A",
                                                          "content": "c", "kind": "speech", "template_id": None, "truncated": None,
                                                          "ticket_id": None, "visible_to_ids": [], "thought": "secret"}},
    ]
    for ev in bad:
        assert mock.validate_event(ev), ev


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


def test_new_game_snapshot_carries_its_own_event_id() -> None:
    async def scenario() -> None:
        bus = mock.EventBus()
        game = mock.MockGame(bus, speed=1_000_000)
        old_id = game.session["session_id"]
        await game.reset()
        ev = bus.ring[-1]
        assert ev["type"] == "snapshot"
        assert ev["data"]["session"]["session_id"] != old_id
        assert ev["data"]["session"]["event_id"] == ev["id"]
        assert mock.validate_event(ev) == []

    asyncio.run(scenario())


def test_pause_stops_the_replay_and_resume_continues_it() -> None:
    async def scenario() -> None:
        bus = mock.EventBus(ring=50000)
        game = mock.MockGame(bus, speed=200.0)  # ~0.4 s per game
        game.start()
        await asyncio.sleep(0.15)
        assert game.session["status"] == "running"
        game.pause()
        assert game.session["status"] == "paused"
        assert game.session["pause_reason"] == "user"
        assert {p["state"] for p in game.session["participants"]} == {"stopped"}
        assert game.session["moderator"]["state"] == "stopped"
        frozen = bus.latest_id
        await asyncio.sleep(0.3)
        assert bus.latest_id == frozen, "nothing is published while paused"
        game.start()  # resume
        assert game.session["status"] == "running"
        for _ in range(400):
            if game.session["status"] == "finished":
                break
            await asyncio.sleep(0.05)
        assert game.session["status"] == "finished"
        done = bus.latest_id
        # continue after the verdict replays the epilogue and finishes again
        n_messages = len(game.session["messages"])
        game.start()
        assert game.session["status"] == "running"
        assert game.session["winner"] is None  # the verdict is withdrawn while continuing
        for _ in range(400):
            if game.session["status"] == "finished":
                break
            await asyncio.sleep(0.05)
        assert game.session["status"] == "finished"
        assert len(game.session["messages"]) > n_messages
        assert bus.latest_id > done
        assert mock.validate_session(game.snapshot_payload()) == []

    asyncio.run(scenario())


def test_hold_sets_pause_reason_and_the_status_text_carries_the_reason() -> None:
    async def scenario() -> None:
        bus = mock.EventBus(ring=50000)
        game = mock.MockGame(bus, speed=100.0)
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


# --------------------------------------------------------------------------------- HTTP
def test_http_endpoints_the_front_end_calls() -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    app = mock.create_app(speed=2000.0)
    with TestClient(app) as client:
        state = client.get("/api/state").json()
        assert state["app_version"].startswith("v0.12")
        assert mock.validate_session(state["session"]) == []
        assert state["session"]["status"] == "idle"
        assert len(state["session"]["participants"]) == 12
        assert state["workspace"]["enabled"] is True

        assert client.get("/").status_code == 200
        assert client.get("/static/js/app.js").status_code == 200
        providers = client.get("/api/providers").json()["providers"]
        assert providers and providers[0]["models"]
        bindings = client.get("/api/settings/bindings").json()
        assert "moderator" in bindings["bindings"] and bindings["providers"]
        assert client.get("/api/workspaces").json()["current"]["name"]
        assert client.get("/api/sessions").json()["sessions"][0]["current"] is True
        assert client.get("/api/settings/templates").json()["slots"] == 3

        # control endpoints answer {"ok", "session"} with a PublicSession v2
        started = client.post("/api/control/start").json()
        assert started["ok"] is True and mock.validate_session(started["session"]) == []
        assert started["session"]["status"] == "running"
        info = {}
        for _ in range(200):
            info = client.get("/api/mock/info").json()
            if info["status"] == "finished":
                break
            time.sleep(0.05)
        assert info["status"] == "finished"
        finished = client.get("/api/state").json()["session"]
        assert finished["winner"] and finished["event_id"] == info["latest_event_id"]
        assert mock.validate_session(finished) == []

        # force-pause is accepted (it equals pause); nothing is running now, so it is a no-op
        assert client.post("/api/control/force-pause").json()["ok"] is True
        # "继续" after the verdict replays the epilogue; slow the replay down so it can be paused midway
        app.state.mock_holder["game"].speed = 40.0
        resumed = client.post("/api/control/start").json()["session"]
        assert resumed["status"] == "running"
        assert resumed["winner"] is None  # the verdict is withdrawn while the game continues
        paused = client.post("/api/control/pause").json()["session"]
        assert (paused["status"], paused["pause_reason"]) == ("paused", "user")
        assert client.post("/api/control/pause").json()["session"]["status"] == "paused"  # idempotent
        # the moderator's hold_game: only a running game can be put on hold, so resume first
        assert client.post("/api/control/start").json()["session"]["status"] == "running"
        hold = client.post("/api/mock/hold", json={"reason": "测试暂停"}).json()["session"]
        assert (hold["status"], hold["pause_reason"], hold["hold_reason"]) == ("paused", "hold", "测试暂停")
        assert "测试暂停" in hold["active_status_text"]

        new = client.post("/api/control/new").json()["session"]
        assert new["status"] == "idle" and new["session_id"] != finished["session_id"]
        assert new["messages"] == [] and mock.validate_session(new) == []

        assert client.post("/api/session/execution-depth", json={"execution_depth": "high"}).json()["execution_depth"] == "high"
        assert client.post("/api/session/execution-depth", json={"execution_depth": "nope"}).status_code == 400
        saved = client.post("/api/session/save").json()
        assert saved["filename"].endswith(".json") and saved["session"]["session_id"] == new["session_id"]
        # a bad mock-only event is refused by the validator
        assert client.post("/api/mock/emit", json={"type": "agent.status", "data": {"id": "p01", "state": "x"}}).status_code == 422
        assert client.post("/api/mock/emit", json={"type": "phase.changed", "data": {"text": "ok"}}).json()["ok"] is True


def test_static_files_are_served_from_the_repo_static_directory() -> None:
    assert mock.STATIC_DIR == ROOT / "static"
    assert (mock.STATIC_DIR / "index.html").exists()
    assert mock.DEFAULT_PORT == 8731


def test_selfcheck_cli_passes() -> None:
    assert mock.selfcheck() == 0
    # the replay is deterministic for a given seed
    a = mock.replay_fast(seed=3)
    b = mock.replay_fast(seed=3)
    strip = lambda run: [(e["type"], {k: v for k, v in e["data"].items() if k != "ts"}) for e in run["events"]]  # noqa: E731
    assert strip(a) == strip(b)
    c = copy.deepcopy(a["final"]["usage_stats"])
    assert c["total_tokens"] > 0 and c["total_cost_usd"] > 0
