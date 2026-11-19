"""The UI mock plays the person who takes over a seat (v0.13, contract §11.7).

The mock re-implements the seat rules of ``app/world/seat.py`` for its scripted world so that the front end can be
developed and shown without the engine: this file checks that (1) the takeover / release / re-deal rules are the
ones of the plan (F2, F4, F5), (2) the scripted game waits for the person where their seat would act and goes on
when they act (or when the deadline passes), (3) every event it publishes matches the contract and the real client
store reduces the log of such a game to the session the mock holds.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
MOCK_PATH = ROOT / "tools" / "mock_ui_server.py"


def _load_mock():
    spec = importlib.util.spec_from_file_location("arena_mock_ui_server_takeover", MOCK_PATH)
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


def run(coro_factory):
    """Run ``coro_factory()`` on the virtual-time loop of the mock (the whole script takes milliseconds)."""
    return asyncio.run(coro_factory(), loop_factory=mock.VirtualLoop)


def make_game(**kw):
    bus = mock.EventBus(ring=100000)
    game = mock.MockGame(bus, speed=1.0, seed=7, budget=mock.default_budget(None, False), **kw)
    return bus, game


# ---------------------------------------------------------------------------------- the person (a bot)
async def human_bot(game, seat: str, plan: dict, log: list) -> None:
    """Acts like a person at ``seat``: answers what waits, speaks when it holds the floor, once in the free talk."""
    spoke_free = False
    explode_done = False
    while game.session["status"] != "finished" and game.takeover_seat == seat:
        await game.wait(0.2)
        if game.takeover_seat != seat or game.session["status"] != "running":
            continue
        t = game.compute_takeover()
        for r in t["requests"]:
            if r["answered"]:
                continue
            req = next(x for x in game.session["requests"] if x["id"] == r["id"])
            if r["kind"] == "choice":
                ids = [o["id"] for o in req["options"]]
                value = plan.get("prefer") if plan.get("prefer") in ids else ids[0]
            else:
                value = plan.get("text", "我是真人写的遗言。")
            rc = game.seat_respond(r["id"], value)
            log.append(("respond", r["id"], value, rc["accepted"]))
        for cid, st in t["channels"].items():
            floor = st["floor"]
            if floor and floor["mine"] and st["can_write"]:
                rc = game.seat_speak(cid, f"（真人）轮到我了：{cid}", t["seq"])
                log.append(("speak", cid, rc["accepted"]))
                rc = game.seat_pass(cid)
                log.append(("pass", cid, rc["accepted"]))
            elif cid == "public" and floor and floor["kind"] == "free" and st["can_write"] and not spoke_free:
                rc = game.seat_speak(cid, "（真人）自由讨论里说一句。", t["seq"])
                log.append(("speak-free", cid, rc["accepted"]))
                spoke_free = rc["accepted"]
        for ab in t["abilities"]:
            if plan.get("explode") and not explode_done:
                target = plan["explode"]
                rc = game.seat_ability(ab["id"], {"target": target, "declaration": "我是真人白狼王！"})
                log.append(("ability", ab["id"], rc["accepted"]))
                explode_done = rc["accepted"]


async def play_with_human(seat: str, plan: dict | None = None, *, release_at: float | None = None) -> dict:
    plan = plan or {}
    bus, game = make_game()
    initial = game.snapshot_payload()
    game.take_over(seat)
    game.gate.set()
    game.set_status("running", reason="started")
    log: list = []
    tasks = [asyncio.ensure_future(game.play()), asyncio.ensure_future(human_bot(game, seat, plan, log))]
    if release_at is not None:
        async def hand_back() -> None:
            while game.clock < release_at:
                await asyncio.sleep(0.05)
            game.pause()
            game.release_seat()
            game.start()

        tasks.append(asyncio.ensure_future(hand_back()))
    await asyncio.gather(*tasks)
    await game.drain_bg()
    return {"initial": initial, "events": list(bus.ring), "final": game.snapshot_payload(), "game": game, "log": log}


def _msgs(session: dict, sender: str) -> list[dict]:
    return [m for m in session["messages"] if m["from"] == sender]


# ---------------------------------------------------------------------------------- the rules
def test_takeover_rules_follow_the_plan() -> None:
    async def scenario() -> None:
        bus, game = make_game()
        s = game.session
        assert s["takeover"] == {"seat": None, "reshuffle_available": True}
        with pytest.raises(mock.TakeoverError) as err:
            game.take_over("moderator")
        assert err.value.status == 422
        with pytest.raises(mock.TakeoverError) as err:
            game.take_over("N99")
        assert err.value.status == 404
        game.take_over("N3")
        assert game.takeover_seat == "N3" and game.player("N3")["state"] == "human"
        game.take_over("N3")  # the same seat again: nothing happens
        with pytest.raises(mock.TakeoverError) as err:
            game.take_over("N4")
        assert err.value.status == 409 and "请先退出" in err.value.detail

        events = [e for e in bus.ring if e["type"] == "takeover.updated"]
        assert len(events) == 1 and mock.validate_event(events[0]) == []
        payload = events[0]["data"]["takeover"]
        assert payload["seat"] == "N3" and payload["reshuffle_available"] is True
        assert set(payload["channels"]) == {"dm-N3"}  # nothing but the private chat exists before the game starts
        assert payload["channels"]["dm-N3"]["can_write"] is True and payload["channels"]["dm-N3"]["max_chars"] == mock.DM_MAX_CHARS
        assert payload["attention"] == [] and payload["requests"] == [] and payload["abilities"] == []

        # a running game: no takeover, no release; every action is refused with a 409
        game.gate.set()
        game.set_status("running", reason="started")
        with pytest.raises(mock.TakeoverError) as err:
            game.release_seat()
        assert err.value.status == 409 and "暂停" in err.value.detail
        game.gate.clear()
        game.set_status("paused", reason="user", pause_reason="user")
        with pytest.raises(mock.TakeoverError) as err:
            game.seat_speak("dm-N3", "hi", None)
        assert err.value.status == 409 and "已暂停" in err.value.detail
        game.release_seat()
        assert game.takeover_seat is None and game.player("N3")["state"] == "sleeping"
        last = [e for e in bus.ring if e["type"] == "takeover.updated"][-1]
        assert last["data"]["takeover"] == {"seat": None, "reshuffle_available": False}  # (the game has started)
        game.release_seat()  # nobody is played: nothing happens
        with pytest.raises(mock.TakeoverError) as err:
            game.seat_pass("public")
        assert err.value.status == 409 and "请先接管" in err.value.detail
        # a finished game cannot be taken over, a paused one can
        game.take_over("N4")
        game.release_seat()
        game.session["status"] = "finished"
        with pytest.raises(mock.TakeoverError):
            game.take_over("N4")

    run(scenario)


def test_the_redeal_is_only_possible_before_the_game_has_started() -> None:
    async def scenario() -> None:
        bus, game = make_game()
        game.take_over("N2")
        before = [p["tags"][0] for p in game.session["participants"]]
        game.reshuffle()
        after = [p["tags"][0] for p in game.session["participants"]]
        assert sorted(before) == sorted(after)  # the same cards, dealt again
        snapshot = [e for e in bus.ring if e["type"] == "snapshot"][-1]
        assert mock.validate_session(snapshot["data"]["session"]) == []
        assert snapshot["data"]["session"]["takeover"]["seat"] == "N2"
        assert snapshot["data"]["session"]["takeover"]["reshuffle_available"] is True
        game.gate.set()
        game.set_status("running", reason="started")
        with pytest.raises(mock.TakeoverError) as err:
            game.reshuffle()
        assert err.value.status == 409 and "已经开始" in err.value.detail
        game.gate.clear()
        game.set_status("paused", reason="user", pause_reason="user")
        with pytest.raises(mock.TakeoverError):
            game.reshuffle()  # not idle any more, and it has started

    run(scenario)


def test_the_seat_state_says_what_the_gate_says() -> None:
    async def scenario() -> None:
        bus, game = make_game()
        game.take_over("N4")
        # private chat of the seat: free, 600 characters; the closed public channel after the set-up
        game.create_channel("public", "公共频道", mock.EVERYONE)
        game.channel("public")["mode"] = "closed"
        state = game.compute_takeover()["channels"]
        assert state["public"]["can_write"] is False and state["public"]["code"] == "CHANNEL_CLOSED"
        assert "已关闭" in state["public"]["why_not"] and state["public"]["max_chars"] is None
        # a queue: waiting for the turn, then holding the floor
        queue = [{"holder": h, "state": "pending", "messages_used": 0, "max_messages": 2} for h in ("N2", "N4", "N6")]
        queue[0]["state"] = "active"
        game.channel("public").update(mode="queue")
        game.set_floor("public", {"kind": "queue", "active": "N2", "paused": False, "seconds_left": 90.0, "queue": queue, "free": None})
        waiting = game.compute_takeover()["channels"]["public"]
        assert waiting["can_write"] is False and waiting["code"] == "NO_FLOOR" and "还没轮到你" in waiting["why_not"]
        assert "Bob" in waiting["why_not"] and "N2" not in waiting["why_not"], "the speaker is named by nickname"
        queue[0]["state"], queue[1]["state"] = "done", "active"
        game.set_floor("public", {"kind": "queue", "active": "N4", "paused": False, "seconds_left": 90.0, "queue": queue, "free": None})
        mine = game.compute_takeover()
        st = mine["channels"]["public"]
        assert st["can_write"] and st["can_pass"] and st["floor"]["mine"] and st["floor"]["messages_left"] == 2
        assert st["max_chars"] == mock.FREE_MAX_CHARS and mine["attention"] == ["public"]
        # frozen / paused: neither writing nor passing
        game.channel("public")["frozen"] = True
        frozen = game.compute_takeover()["channels"]["public"]
        assert frozen["can_write"] is False and frozen["can_pass"] is False and "冻结" in frozen["why_not"]
        game.channel("public")["frozen"] = False
        # a free discussion counts the seat's messages
        game.open_free("public")
        free = game.compute_takeover()["channels"]["public"]
        assert free["can_write"] and free["floor"] == {
            "kind": "free", "mine": False, "paused": False, "messages_left": mock.FREE_MAX_MESSAGES,
            "chars_left": None, "seconds_left": None, "deadline_clock": None, "untimed": False,
        }
        game.channel("public")["floor"]["free"]["message_counts"]["N4"] = mock.FREE_MAX_MESSAGES
        used = game.compute_takeover()["channels"]["public"]
        assert used["can_write"] is False and used["code"] == "QUOTA_EXCEEDED"

    run(scenario)


def test_the_seat_actions_are_checked_like_the_engine_checks_them() -> None:
    async def scenario() -> None:
        bus, game = make_game()
        game.take_over("N4")
        game.gate.set()
        game.set_status("running", reason="started")
        game.create_channel("public", "公共频道", mock.EVERYONE, mode="free")
        game.open_free("public")
        long_text = "字" * (mock.FREE_MAX_CHARS + 25)
        rc = game.seat_speak("public", long_text, None)
        assert rc["accepted"] and rc["truncated"] == {"dropped_chars": 25, "kept_chars": mock.FREE_MAX_CHARS}
        assert _msgs(game.session, "N4")[-1]["content"] == "字" * mock.FREE_MAX_CHARS
        assert game.seat_speak("public", "   ", None)["code"] == "INVALID_PARAMS"
        assert game.seat_speak("nowhere", "hi", None)["code"] == "UNKNOWN_CHANNEL"
        assert game.seat_speak("dm-N5", "hi", None)["code"] == "UNKNOWN_CHANNEL"  # somebody else's chat
        # STALE: a phase change after the draft began
        seen = game.session["seq"]
        game.set_phase("第 2 天")
        stale = game.seat_speak("public", "写了很久的草稿", seen)
        assert stale["accepted"] is False and stale["code"] == "STALE" and stale["data"]["latest_seq"] == game.session["seq"]
        assert game.seat_speak("public", "确认后重发", game.session["seq"])["accepted"]
        # a closed channel refuses with the gate's reason
        game.close_floor("public")
        closed = game.seat_speak("public", "hi", None)
        assert closed["code"] == "CHANNEL_CLOSED" and closed["accepted"] is False
        # requests: unknown / not a recipient / invalid option
        rid = game.open_request("投谁？", ["N1", "N2"], ["N4", "N5"], sealed=True)
        assert game.seat_respond("r99", "N1")["code"] == "UNKNOWN_REQUEST"
        assert game.seat_respond(rid, "N9")["code"] == "INVALID_TARGET"
        ok = game.seat_respond(rid, "N1")
        assert ok["accepted"] and ok["data"]["replaced"] is False and ok["data"]["closed"] is False
        again = game.seat_respond(rid, "N2")  # a changed answer is the last one
        assert again["accepted"] and again["data"]["replaced"] is True
        mine = [r for r in game.compute_takeover()["requests"] if r["id"] == rid][0]
        assert mine["answered"] and mine["my_answer"]["value"] == "N2" and mine["channel_id"] == "dm-N4" or mine["channel_id"] in {"public", "dm-N4"}
        other = game.open_request("只问别人", ["N1", "N2"], ["N5"], sealed=True)
        assert game.seat_respond(other, "N1")["code"] == "FORBIDDEN"
        # abilities: nothing is enabled, and the seat does not hold it
        assert game.seat_ability(mock.EXPLODE, {})["code"] == "FORBIDDEN"
        assert game.seat_ability("nope", {})["code"] == "UNKNOWN_ABILITY"
        for event in bus.ring:
            assert mock.validate_event(event) == [], event

    run(scenario)


# ---------------------------------------------------------------------------------- the whole game
def test_a_person_plays_a_wolf_through_the_whole_game() -> None:
    out = run(lambda: play_with_human("N3", {"prefer": "N5"}))
    game, final, events, log = out["game"], out["final"], out["events"], out["log"]
    assert final["status"] == "finished" and final["takeover"]["seat"] == "N3"
    assert [p["state"] for p in final["participants"] if p["id"] == "N3"] == ["human"]
    # the seat never has an agent: no agent.status / agent.metrics for it
    assert not [e for e in events if e["type"] in ("agent.status", "agent.metrics") and e["data"]["id"] == "N3"]
    # they answered the wolves' request (and the unanimous vote closed it), voted, spoke in the queue and in the free talk
    answers = {a["player_id"]: a["value"] for r in final["requests"] for a in r["answers"] if r["share_answers"]}
    assert answers["N3"] == "N5"
    wolves_request = next(r for r in final["requests"] if r["share_answers"])
    assert wolves_request["status"] == "closed" and wolves_request["result"]["reason"] == "unanimous"
    vote = next(r for r in final["requests"] if r["sealed"])
    assert any(a["player_id"] == "N3" for a in vote["answers"]) and vote["status"] == "closed"
    spoken = [m["content"] for m in _msgs(final, "N3")]
    assert any("轮到我了：public" in c for c in spoken) and any("自由讨论里说一句" in c for c in spoken)
    # the script's own N3 challenge and chatter are gone: a person's words are the person's
    assert not [m for m in _msgs(final, "N3") if m["kind"] == "challenge"]
    assert all(a[-1] is True for a in log), log
    # every event is valid and the person's state was pushed (and never contradicts the participants)
    for ev in events:
        assert mock.validate_event(ev) == [], ev
    pushed = [e for e in events if e["type"] == "takeover.updated"]
    assert len(pushed) >= 10
    assert any(e["data"]["takeover"]["attention"] for e in pushed), "the seat was asked to act at some point"
    assert any(e["data"]["takeover"]["abilities"] == [] for e in pushed)
    assert mock.validate_session(final) == []
    assert game.token is None


def test_a_person_who_does_not_answer_lets_the_request_run_into_its_deadline() -> None:
    async def scenario() -> dict:
        bus, game = make_game()
        initial = game.snapshot_payload()
        game.take_over("N9")
        game.gate.set()
        game.set_status("running", reason="started")

        async def ends_the_turn() -> None:
            # (v0.13.1, G10) a turn in the PUBLIC channel has no deadline - the game would wait for it for ever (see
            # test_mock_softpause.py).  This person ends that turn without a word and then answers nothing: the
            # requests do have deadlines.
            while game.session["status"] != "finished":
                await game.wait(0.2)
                state = game.compute_takeover()["channels"].get("public")
                if state and state["floor"] and state["floor"]["mine"]:
                    assert game.seat_pass("public")["accepted"]

        passing = asyncio.ensure_future(ends_the_turn())
        await game.play()  # nobody speaks or answers for N9
        passing.cancel()
        return {"final": game.snapshot_payload(), "events": list(bus.ring), "initial": initial}

    out = run(scenario)
    final = out["final"]
    assert final["status"] == "finished"
    vote = next(r for r in final["requests"] if r["sealed"])
    assert vote["status"] == "closed" and vote["result"]["reason"] == "deadline" and "N9" in vote["result"]["missing"]
    assert not _msgs(final, "N9"), "a person who stays silent says nothing: the script does not speak for them"
    for ev in out["events"]:
        assert mock.validate_event(ev) == [], ev


def test_the_person_who_plays_the_white_wolf_king_decides_about_the_explosion() -> None:
    out = run(lambda: play_with_human("N12", {"prefer": "N5", "explode": "N7"}))
    final, events, log = out["final"], out["events"], out["log"]
    used = [e for e in events if e["type"] == "ability.used"]
    assert len(used) == 1 and used[0]["data"] == {"ability_id": mock.EXPLODE, "actor_id": "N12"}
    assert ("ability", mock.EXPLODE, True) in log
    boom = [m for m in final["messages"] if m["kind"] == "ability"]
    assert len(boom) == 1 and "我是真人白狼王" in boom[0]["content"] and boom[0]["from"] == "N12"
    outs = {p["id"] for p in final["participants"] if p["out"]}
    assert {"N12", "N7"} <= outs
    assert [c["frozen"] for c in final["channels"] if c["id"] == "public"] == [False], "the moderator ruled: unfrozen again"
    # the ability was offered only while it was enabled, with the params of the scenario
    offered = [e["data"]["takeover"]["abilities"] for e in events if e["type"] == "takeover.updated" and e["data"]["takeover"]["abilities"]]
    assert offered and offered[0][0]["id"] == mock.EXPLODE
    assert [p["name"] for p in offered[0][0]["params"]] == ["target", "declaration"]
    assert offered[0][0]["params"][0]["candidates"] and "N12" in offered[0][0]["params"][0]["candidates"]
    assert final["takeover"]["abilities"] == []
    for ev in events:
        assert mock.validate_event(ev) == [], ev


def test_the_white_wolf_king_who_does_not_explode_just_plays_on() -> None:
    out = run(lambda: play_with_human("N12", {"prefer": "N5"}))
    final, events = out["final"], out["events"]
    assert not [e for e in events if e["type"] == "ability.used"]
    assert not [m for m in final["messages"] if m["kind"] == "ability"]
    assert "N12" not in {p["id"] for p in final["participants"] if p["out"]}
    assert final["status"] == "finished"


def test_a_seat_handed_back_mid_game_is_played_by_the_script_again() -> None:
    out = run(lambda: play_with_human("N6", {"prefer": "N5"}, release_at=6.0))
    final, events = out["final"], out["events"]
    assert final["status"] == "finished" and final["takeover"] == {"seat": None, "reshuffle_available": False}
    assert [p["state"] for p in final["participants"] if p["id"] == "N6"] == ["sleeping"]
    assert any(e["type"] == "takeover.updated" and e["data"]["takeover"]["seat"] is None for e in events)
    # after the hand-over the script's agent N6 speaks again (its queue speech) and changes its mind in the night request
    assert any(m["from"] == "N6" and "N4 的发言有点急" in m["content"] for m in final["messages"])
    assert not [m for m in final["messages"] if m["from"] == "N6" and "（真人）" in m["content"]], "the person had no turn before the hand-over"
    for ev in events:
        assert mock.validate_event(ev) == [], ev


# ---------------------------------------------------------------------------------- the real client store
@pytest.mark.skipif(NODE is None, reason="node executable not found")
@pytest.mark.parametrize("seat,plan", [("N3", {"prefer": "N5"}), ("N12", {"prefer": "N5", "explode": "N7"}), ("N5", {})])
def test_the_client_store_reduces_the_log_of_a_game_with_a_person_to_the_servers_session(seat: str, plan: dict, tmp_path: Path) -> None:
    out = run(lambda: play_with_human(seat, plan))
    log = tmp_path / "replay.json"
    log.write_text(
        json.dumps({"initial": out["initial"], "events": out["events"], "final": out["final"], "duration": 0}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = subprocess.run(
        [NODE, str(ROOT / "tests" / "frontend" / "replay_check.js"), str(log), "--human"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "replay ok" in result.stdout


# ---------------------------------------------------------------------------------- HTTP
def _client():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    # real time (the demo budget would pause a game that runs at high speed before the next request)
    app = mock.create_app(speed=1.0)
    return app, TestClient(app)


def test_http_takeover_and_seat_endpoints_answer_with_the_documented_statuses() -> None:
    app, c = _client()
    with c as client:
        state = client.get("/api/state").json()["session"]
        assert state["takeover"] == {"seat": None, "reshuffle_available": True}
        assert client.post("/api/takeover", json={"seat": "moderator"}).status_code == 422
        assert client.post("/api/takeover", json={"seat": "N99"}).status_code == 404
        assert client.post("/api/takeover", json={}).status_code == 422
        assert client.post("/api/seat/speak", json={"channel_id": "dm-N3", "content": "hi"}).status_code == 409  # nobody is played
        taken = client.post("/api/takeover", json={"seat": "N3"})
        assert taken.status_code == 200 and taken.json()["session"]["takeover"]["seat"] == "N3"
        assert mock.validate_session(taken.json()["session"]) == []
        assert [p["state"] for p in taken.json()["session"]["participants"] if p["id"] == "N3"] == ["human"]
        busy = client.post("/api/takeover", json={"seat": "N4"})
        assert busy.status_code == 409 and "请先退出" in busy.json()["detail"]
        refused = client.post("/api/seat/speak", json={"channel_id": "dm-N3", "content": "hi"})
        assert refused.status_code == 409 and "还没有开始" in refused.json()["detail"], "the game has not started"
        assert client.post("/api/seat/speak", json={"channel_id": 7, "content": "hi"}).status_code == 422
        # the re-deal works before the start, with a snapshot to every client ...
        redeal = client.post("/api/control/shuffle-identities")
        assert redeal.status_code == 200 and redeal.json()["session"]["takeover"]["reshuffle_available"] is True
        # ... a release works while the game is idle ...
        released = client.post("/api/takeover/release")
        assert released.status_code == 200 and released.json()["session"]["takeover"]["seat"] is None
        assert client.post("/api/takeover/release").status_code == 200  # nobody is played: nothing happens
        # ... and once the game runs: no takeover, no re-deal
        assert client.post("/api/control/start").status_code == 200
        busy = client.post("/api/takeover", json={"seat": "N3"})
        assert busy.status_code == 409 and "暂停" in busy.json()["detail"]
        late = client.post("/api/control/shuffle-identities")
        assert late.status_code == 409 and "已经开始" in late.json()["detail"]
        assert client.post("/api/control/pause").status_code == 200
        assert client.post("/api/control/shuffle-identities").status_code == 409, "a paused game has started, too"
        again = client.post("/api/takeover", json={"seat": "N3"})
        assert again.status_code == 200 and again.json()["session"]["takeover"]["reshuffle_available"] is False
