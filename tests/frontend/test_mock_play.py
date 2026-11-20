"""Contract tests of the UI mock for what the v0.14 play features consume (web-play: W1-W4; docs/v0.12-contracts.md §13.8, §13.10).

The page is built against the mock, so the mock has to answer what the page reads.  Python makes the mock's REAL answers (``tools/mock_ui_server.py`` through
``create_app`` and a ``TestClient``); node runs the page's REAL ``store.js`` / ``view.js`` on them (``tests/frontend/test_v014_play_*.js`` test the same
functions against the fixtures):

* **before the deal** (H52): the mock's idle game has ``dealt: false`` and no tags, and the page's card model has no identity tag for any seat;
* **the line-up** (H29): what the page predicts for a move (``Store.applyLineup``: it shows the new seating before the server answers) is what the server
  says afterwards - for every seat the bot, name, avatar and ✔ ⚠ ✖ mark;
* **the status label** (H26): ``View.systemStatus`` on the mock's session agrees with the mock's ``system_status`` in every state (idle ready / not
  initialized, running, paused, finished, a review running, an initialization running);
* **the start check** (H25): the ``409 NOT_READY`` body groups into the players / the contract / the scenario / the moderator, every item with the
  server's sentence and, for every ``fix`` the contract names, a button text;
* **the question at the end of a game** (H31-H33): when a finished game asks for a review, which seats a person played (``human_spans``), the
  ``learn_style`` that goes with them and the proposals that come back;
* **a speech over the limit** (H16): the receipt ``TOO_LONG`` is in ``test_mock_takeover.py``; here the page's count and the server's agree.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import mock_ui_server as mock  # noqa: E402

_WINDOWS_NODE = Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"


def _find_node() -> str | None:
    env = os.environ.get("NODE_BIN")
    if env and Path(env).exists():
        return env
    return shutil.which("node") or (str(_WINDOWS_NODE) if _WINDOWS_NODE.exists() else None)


NODE = _find_node()
pytestmark = pytest.mark.skipif(NODE is None, reason="node executable not found")

IDS = [f"N{i}" for i in range(1, 13)]

#: runs in node: ``argv[2]`` = the repository root, ``argv[3]`` = a JSON file {"op": ..., ...}; prints one JSON line.  The page's own pure code, not a copy of it.
_NODE_SCRIPT = r"""
const fs = require("node:fs");
const path = require("node:path");
const root = process.argv[2]; // (argv[0] is node, argv[1] this script)
const Store = require(path.join(root, "static", "js", "store.js"));
const View = require(path.join(root, "static", "js", "view.js"));
const job = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = {};
const norm = (raw) => Store.normalizeSession(JSON.parse(JSON.stringify(raw)));
if (job.op === "cards") {
  const s = norm(job.session);
  out.dealt = s.dealt;
  out.cards = View.boardTiles(s, "god").map((t) => ({ id: t.id, main: t.main, badges: t.badges.length, botId: t.botId, initStatus: t.initStatus, name: t.name }));
  out.summary = View.boardSummary(s, "god");
}
if (job.op === "lineup") {
  const s = norm(job.before);
  const change = Store.applyLineup(s, job.order);
  out.ok = Boolean(change);
  const seats = (x) => x.participants.slice().sort((a, b) => Number(a.seat) - Number(b.seat)).map((p) => [p.id, p.bot_id, p.name, p.init_status, JSON.stringify(p.avatar)]);
  const server = norm(job.after);
  out.predicted = seats(s);
  out.server = seats(server);
  out.predictedBots = s.bots.bots.map((b) => [b.id, b.seat]);
  out.serverBots = server.bots.bots.map((b) => [b.id, b.seat]);
  out.predictedLineup = s.bots.lineup;
  out.serverLineup = server.bots.lineup;
  change.undo();
  out.undone = JSON.stringify(seats(s)) === JSON.stringify(seats(norm(job.before)));
}
if (job.op === "status") {
  out.rows = job.states.map((state) => {
    const s = norm(state.session);
    const got = View.systemStatus(s);
    return { name: state.name, server: s.system_status, code: got.code, text: got.text, reason: got.reason, label: got.label, dot: got.dot };
  });
}
if (job.op === "events") {
  // the log of events through the real store: after every session.updated the label agrees with the server's
  const s = norm(job.initial);
  out.checked = [];
  for (const ev of job.events) {
    Store.applyEvent(s, { id: ev.id, type: ev.type, data: ev.data }, { clockNow: Number(s.clock) || 0 });
    if (ev.type === "session.updated" && ev.data.system_status) {
      const got = View.systemStatus(s);
      out.checked.push({ id: ev.id, server: ev.data.system_status.code, page: got.code, text: got.text, serverText: ev.data.system_status.text });
    }
  }
}
if (job.op === "startcheck") {
  const s = norm(job.session);
  const m = View.startCheckModel(s, job.problems);
  out.model = m;
  out.sourceItems = job.problems.reduce((n, p) => n + p.items.length, 0);
  out.fixes = Array.from(new Set(job.problems.flatMap((p) => p.items.map((i) => i.fix))));
  out.fixLabels = View.FIX_LABELS;
}
if (job.op === "review") {
  const s = norm(job.session);
  out.prompt = View.reviewPrompt(s, job.dismissed || {});
  out.rows = View.reviewRows(s, job.humans || [], job.choices || {}).map((r) => ({ id: r.id, human: r.human, checked: r.checked, learnStyle: r.learnStyle, done: r.done }));
}
if (job.op === "count") {
  out.counts = job.texts.map((t) => View.countChars(t));
}
process.stdout.write(JSON.stringify(out));
"""


def _node(tmp_path: Path, job: dict) -> dict:
    data = tmp_path / "job.json"
    data.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    script = tmp_path / "check.js"
    script.write_text(_NODE_SCRIPT, encoding="utf-8")
    result = subprocess.run([NODE, str(script), str(ROOT), str(data)], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _client(*, bots: str = "ready", speed: float = 2000.0):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    app = mock.create_app(speed=speed, budget_tokens=0, bots=bots)
    return app, TestClient(app)


def _session(client) -> dict:
    return client.get("/api/state").json()["session"]


def _game(app):
    return app.state.mock_holder["game"]


def _wait_for(predicate, timeout: float = 20.0, step: float = 0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("timed out")


# ================================================================================================= before the deal (H52)
def test_before_the_deal_the_page_draws_twelve_cards_without_any_identity(tmp_path: Path) -> None:
    app, c = _client()
    with c as client:
        idle = _session(client)
        assert idle["dealt"] is False and all(p["tags"] == [] and "role" not in p and "faction" not in p for p in idle["participants"])
        out = _node(tmp_path, {"op": "cards", "session": idle})
        assert out["dealt"] is False and len(out["cards"]) == 12
        assert all(card["main"] is None and card["badges"] == 0 for card in out["cards"]), "no identity tag - not even the grey ?"
        assert out["summary"] is None, "nothing to count before the deal"
        assert [card["initStatus"] for card in out["cards"]] == ["ready"] * 12 and all(card["botId"].startswith("b-") for card in out["cards"])
        # the start deals: the same cards now have their identities
        assert client.post("/api/control/start").status_code == 200
        _wait_for(lambda: any(p["tags"] for p in _session(client)["participants"]))
        running = _session(client)
        assert running["dealt"] is True
        out = _node(tmp_path, {"op": "cards", "session": running})
        assert all(card["main"] is not None and card["main"]["known"] for card in out["cards"]), "the god view sees every identity"
        assert out["summary"] is not None


# ================================================================================================= the line-up (H29)
def test_what_the_page_shows_for_a_move_is_what_the_server_says_afterwards(tmp_path: Path) -> None:
    app, c = _client(bots="mixed")
    with c as client:
        before = _session(client)
        ids = before["bots"]["lineup"]
        marks = [p["init_status"] for p in before["participants"]]
        assert len(set(marks)) >= 3, "the table has ✔ ⚠ ✖ - a mark has to move with its bot"
        for frm, to in ((1, 4), (7, 0), (3, 2), (0, 11)):
            order = list(ids)
            moved = order.pop(frm)
            order.insert(to, moved)
            assert client.put("/api/bots/lineup", json={"order": order}).status_code == 200
            after = _session(client)
            out = _node(tmp_path, {"op": "lineup", "before": before, "after": after, "order": order})
            assert out["ok"] is True and out["undone"] is True
            assert out["predicted"] == out["server"], f"move {frm} -> {to}: bot, name, ✔ ⚠ ✖ and avatar of every seat"
            assert out["predictedBots"] == out["serverBots"] and out["predictedLineup"] == out["serverLineup"] == order
            assert [p["bot_id"] for p in after["participants"]] == order, "the seats keep their ids N1..N12, the bots move"
            before, ids = after, order
        # not a permutation: the server refuses it (and the page's prediction refuses it too: see test_v014_play_view.js)
        assert client.put("/api/bots/lineup", json={"order": ids[1:]}).status_code == 422


def test_after_the_start_the_server_refuses_a_move_with_game_active_and_the_page_says_so_before_it_asks(tmp_path: Path) -> None:
    app, c = _client()
    with c as client:
        ids = _session(client)["bots"]["lineup"]
        assert client.post("/api/control/start").status_code == 200
        refused = client.put("/api/bots/lineup", json={"order": list(reversed(ids))})
        assert refused.status_code == 409 and refused.json()["code"] == "game_active"
        assert isinstance(refused.json()["detail"], str), "the page shows this sentence after its own words when a stale page asks anyway"


# ================================================================================================= the status label (H26)
def test_the_status_label_of_the_page_agrees_with_the_mock_in_every_state(tmp_path: Path) -> None:
    states: list[dict] = []
    app, c = _client(bots="fresh", speed=1.0)  # (real time: the scripted game is still running when the test pauses it)
    with c as client:
        states.append({"name": "idle, nobody initialized", "session": _session(client)})
        assert client.post("/api/mock/bots", json={"preset": "ready"}).status_code == 200
        states.append({"name": "idle, ready", "session": _session(client)})
        assert client.post("/api/control/start").status_code == 200
        states.append({"name": "running", "session": _session(client)})
        client.post("/api/control/pause")
        states.append({"name": "paused by hand", "session": _session(client)})
        client.post("/api/mock/finish")
        states.append({"name": "finished", "session": _session(client)})
    out = _node(tmp_path, {"op": "status", "states": states})
    got = {row["name"]: row for row in out["rows"]}
    assert [(r["name"], r["server"]["code"]) for r in out["rows"]] == [
        ("idle, nobody initialized", "uninitialized"), ("idle, ready", "before_game"), ("running", "in_game"), ("paused by hand", "paused"), ("finished", "finished"),
    ]
    for row in out["rows"]:
        assert row["code"] == row["server"]["code"] and row["text"] == row["server"]["text"], row
    assert got["paused by hand"]["label"] == "已暂停 · 手动暂停" and got["paused by hand"]["reason"] == "手动暂停"
    assert [got[n]["dot"] for n in ("idle, nobody initialized", "idle, ready", "running", "finished")] == ["uninit", "idle", "active", "finished"]


def test_the_status_label_follows_the_events_of_an_initialization_and_of_a_review(tmp_path: Path) -> None:
    app, c = _client(bots="fresh")
    with c as client:
        initial = _session(client)
        before = _game(app).bus.latest_id
        assert client.post("/api/mock/learning", json={"sim_rate": 30.0}).status_code == 200
        assert client.post("/api/bots/init", json={}).status_code == 202
        _wait_for(lambda: client.get("/api/bots").json()["init_job"] is None)
        client.post("/api/mock/finish")
        assert client.post("/api/game/review", json={"ids": ["N1", "N2"]}).status_code == 200
        _wait_for(lambda: _session(client)["review"]["done"] == ["N1", "N2"])
        events = [e for e in _game(app).bus.ring if e["id"] > before]
    out = _node(tmp_path, {"op": "events", "initial": initial, "events": events})
    codes = [row["server"] for row in out["checked"]]
    assert "initializing" in codes and "reviewing" in codes and codes[-1] == "finished", codes
    for row in out["checked"]:
        assert row["page"] == row["server"] and row["text"] == row["serverText"], row


# ================================================================================================= the start check (H25)
def test_the_not_ready_answer_groups_into_what_the_dialog_lists(tmp_path: Path) -> None:
    app, c = _client(bots="mixed")
    with c as client:
        session = _session(client)
        refused = client.post("/api/control/start")
        body = refused.json()
        assert refused.status_code == 409 and body["code"] == "NOT_READY" and isinstance(body["detail"], str) and body["problems"]
        assert _session(client)["status"] == "idle" and _session(client)["dealt"] is False, "nothing was started, nothing was dealt"
        out = _node(tmp_path, {"op": "startcheck", "session": session, "problems": body["problems"]})
        model = out["model"]
        assert model["total"] == out["sourceItems"], "no problem is lost on the way into the dialog"
        assert model["groups"], "at least one group"
        for group in model["groups"]:
            assert group["title"] and group["items"] and group["kind"] in {"contract", "scenario", "player", "moderator", "other"}
            for item in group["items"]:
                assert item["message"] and item["code"]
                if item["fix"] in out["fixLabels"]:
                    assert item["fixLabel"] == out["fixLabels"][item["fix"]], "every fix the contract names has its button"
        assert set(out["fixes"]) <= set(out["fixLabels"]), "the mock only uses fixes the page knows: " + str(out["fixes"])
        players = [g for g in model["groups"] if g["kind"] == "player"]
        assert players and all(g["botId"] and g["seat"] for g in players), "a player's group knows its bot (the button's target)"
        seats = [int(g["seat"][1:]) for g in players]
        assert seats == sorted(seats), "the players are listed in seat order"
        assert model["initCount"] >= 1, "(a bot that only needs its initialization)"


def test_a_broken_contract_and_a_missing_model_come_as_their_own_groups(tmp_path: Path) -> None:
    app, c = _client()
    with c as client:
        session = _session(client)
        assert client.post("/api/mock/files", json={"contract": "invalid"}).status_code == 200
        bindings = client.get("/api/settings/bindings").json()["bindings"]
        client.put("/api/settings/bindings", json={"bindings": {k: v for k, v in bindings.items() if k not in ("moderator", "N4")}})
        body = client.post("/api/control/start").json()
        out = _node(tmp_path, {"op": "startcheck", "session": session, "problems": body["problems"]})
        titles = [g["title"] for g in out["model"]["groups"]]
        assert titles[0] == "官方契约" and "4 号 David" in titles and titles[-1] == "主持人", titles
        by_title = {g["title"]: g for g in out["model"]["groups"]}
        assert [i["fix"] for i in by_title["官方契约"]["items"]] == ["import_contract"]
        assert [i["fixLabel"] for i in by_title["4 号 David"]["items"]] == ["去绑定模型"]
        assert by_title["主持人"]["botId"] is None and by_title["主持人"]["focusId"] == "moderator", "the moderator is not a bot: its button hands 'moderator' to Bots.focus"
        assert by_title["官方契约"]["focusId"] == "" and by_title["4 号 David"]["focusId"] == by_title["4 号 David"]["botId"]


# ================================================================================================= the end of the game (H31-H33)
def test_a_finished_game_asks_for_a_review_until_somebody_was_reviewed(tmp_path: Path) -> None:
    app, c = _client()
    with c as client:
        running = _session(client)
        client.post("/api/control/start")
        client.post("/api/mock/finish")
        finished = _session(client)
        assert finished["review"] == {"available": True, "done": [], "running": False, "supported": True, "deadline_s": 60, "seconds_left": None}
        assert _node(tmp_path, {"op": "review", "session": running})["prompt"] == {"show": False, "why": "not finished"}
        assert _node(tmp_path, {"op": "review", "session": finished})["prompt"] == {"show": True, "why": ""}
        assert _node(tmp_path, {"op": "review", "session": finished, "dismissed": {finished["session_id"]: True}})["prompt"]["why"] == "dismissed"
        assert client.post("/api/game/review", json={"ids": ["N1"]}).status_code == 200
        _wait_for(lambda: _session(client)["review"]["done"] == ["N1"])
        assert _node(tmp_path, {"op": "review", "session": _session(client)})["prompt"] == {"show": False, "why": "reviewed"}
        # a new game starts clean: the question can come again for it
        new = client.post("/api/control/new").json()["session"]
        assert _node(tmp_path, {"op": "review", "session": new})["prompt"]["why"] == "not finished"


def test_the_seats_a_person_played_come_from_human_spans_and_learn_style_gives_them_a_proposal(tmp_path: Path) -> None:
    app, c = _client()
    with c as client:
        assert client.post("/api/takeover", json={"seat": "N3"}).status_code == 200  # (the game has not started: allowed)
        live = _session(client)["session_id"]
        detail = client.get(f"/api/games/{live}/detail").json()
        assert detail["human_spans"] == [{"seat": "N3", "from_seq": 40, "to_seq": 218}], "what the question reads to mark 有人类代打"
        assert client.post("/api/takeover/release").status_code == 200
        assert client.get(f"/api/games/{live}/detail").json()["human_spans"] == [{"seat": "N3", "from_seq": 40, "to_seq": 218}], "handed back: the seat was still played in this game"
        client.post("/api/control/start")
        client.post("/api/mock/finish")
        finished = _session(client)
        spans = client.get(f"/api/games/{live}/detail").json()["human_spans"]
        out = _node(tmp_path, {"op": "review", "session": finished, "humans": [s["seat"] for s in spans]})
        rows = {r["id"]: r for r in out["rows"]}
        assert rows["N3"] == {"id": "N3", "human": True, "checked": True, "learnStyle": True, "done": False}
        assert all(not rows[i]["human"] and not rows[i]["learnStyle"] for i in IDS if i != "N3")
        assert sum(1 for r in rows.values() if r["checked"]) == 12, "all twelve selected"
        # what the window sends: only the seats a person played carry a switch
        before = _game(app).bus.latest_id
        r = client.post("/api/game/review", json={"ids": IDS, "learn_style": {"N3": True}})
        assert r.status_code == 200 and r.json() == {"started": IDS, "deadline_s": 60}
        _wait_for(lambda: len(_session(client)["review"]["done"]) == 12)
        done = {e["data"]["id"]: e["data"] for e in _game(app).bus.ring if e["id"] > before and e["type"] == "review.progress" and e["data"]["state"] == "done"}
        assert done["N3"]["changes"]["proposals"] == 1 and all(done[i]["changes"]["proposals"] == 0 for i in IDS if i != "N3")
        assert all(set(done[i]["changes"]) == {"files", "proposals"} for i in IDS), "what the window shows: the files and the proposals that wait"


def test_without_the_switch_a_played_seat_gets_no_proposal() -> None:
    app, c = _client()
    with c as client:
        client.post("/api/takeover", json={"seat": "N3"})
        client.post("/api/control/start")
        client.post("/api/mock/finish")
        before = _game(app).bus.latest_id
        assert client.post("/api/game/review", json={"ids": ["N3"], "learn_style": {"N3": False}}).status_code == 200
        _wait_for(lambda: _session(client)["review"]["done"] == ["N3"])
        done = [e["data"] for e in _game(app).bus.ring if e["id"] > before and e["type"] == "review.progress" and e["data"]["state"] == "done"]
        assert [d["changes"]["proposals"] for d in done] == [0]


# ================================================================================================= the length of a speech (H16)
def test_the_page_and_the_mock_count_a_speech_the_same_way(tmp_path: Path) -> None:
    """The page's counter and the mock's check: the characters of the trimmed text (not UTF-16 units)."""
    texts = ["字" * 300, "  " + "字" * 300 + "\n", "😀" * 300, "a" * 301, "", "  "]
    counts = _node(tmp_path, {"op": "count", "texts": texts})["counts"]
    assert counts == [300, 300, 300, 301, 0, 0]
    app, c = _client()
    with c as client:
        client.post("/api/takeover", json={"seat": "N3"})
        client.post("/api/control/start")
        limit = mock.DM_MAX_CHARS
        for text, count in zip(["😀" * limit, "😀" * (limit + 1)], (limit, limit + 1)):
            receipt = client.post("/api/seat/speak", json={"channel_id": "dm-N3", "content": text}).json()
            assert receipt["accepted"] is (count <= limit), receipt
            if not receipt["accepted"]:
                assert receipt["code"] == "TOO_LONG" and receipt["data"] == {"chars": count, "limit": limit}
