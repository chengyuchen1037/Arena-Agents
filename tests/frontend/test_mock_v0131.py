"""Contract tests for the v0.13.1 parts of tools/mock_ui_server.py that the roster / observe front end is built against:

* the error log (G32-G36, docs/v0.12-contracts.md §12.3): ``llm.call`` events, ``GET /api/llm/calls``, the injectable failed call;
* the thinking record (G21-G26, §12.4): ``GET /api/agents/{id}/thoughts`` (paging, 409 while another seat is played);
* memory and persona (G37-G44, §12.5): ``limits``, ``memory_parts``, the new ``PUT``, ``refine``, ``review.progress.changes``.

The mock is not the backend: ``tests/test_call_log_api.py`` / ``test_thoughts_api.py`` / ``test_roster_api.py`` test the real one.
These tests keep the two in the same shape, so that a front end built against the mock works against the server.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
MOCK_PATH = ROOT / "tools" / "mock_ui_server.py"


def _load_mock():
    spec = importlib.util.spec_from_file_location("arena_mock_ui_server_v0131", MOCK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mock = _load_mock()
IDS = [f"N{i}" for i in range(1, 13)]
CJK = re.compile(r"[\u4e00-\u9fff]")


def _client():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    app = mock.create_app(speed=2000.0, budget_tokens=0)  # fast, and no budget pause on the way
    return app, TestClient(app)


def _wait_for(predicate, timeout: float = 15.0, step: float = 0.03):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("timed out")


def _game(app):
    return app.state.mock_holder["game"]


def _events(app, *types):
    return [e for e in _game(app).bus.ring if e["type"] in types]


def _play(client) -> None:
    """Play the whole scripted game (it takes a few dozen milliseconds at this speed)."""
    assert client.post("/api/control/start").status_code == 200
    _wait_for(lambda: client.get("/api/state").json()["session"]["status"] == "finished")


def _put(client, pid="N3", **body):
    return client.put(f"/api/roster/{pid}", json=body)


def _seat(client, pid="N3"):
    return next(s for s in client.get("/api/roster").json()["seats"] if s["id"] == pid)


# ===================================================================================================== error log
def test_the_scripted_game_writes_one_line_per_model_call_and_the_endpoint_serves_the_same_lines() -> None:
    app, c = _client()
    with c as client:
        _play(client)
        events = _events(app, "llm.call")
        assert events, "every scripted model call is a line of the log"
        assert [p for e in events for p in mock.validate_event(e)] == []
        calls = [e["data"]["call"] for e in events]
        assert [x["id"] for x in calls] == list(range(1, len(calls) + 1)), "ids count up from 1 inside one game"
        # who made the calls: the moderator and the players, with the model they are bound to; the numbers are the call's own
        assert {x["actor_id"] for x in calls} <= {"moderator", *IDS}
        ok = [x for x in calls if x["status"] == "ok"]
        assert ok and all(x["purpose"] == "turn" and x["kind"] == "call" and x["attempt"] in (1, 2) for x in ok)
        assert all(x["provider_id"] and x["model_id"] and x["elapsed_ms"] > 0 and x["prompt_tokens"] > 0 and x["completion_tokens"] > 0 for x in ok)
        # a retried call: the retry line comes first, the call that then succeeds is its second attempt
        retries = [x for x in calls if x["status"] == "retry"]
        assert retries and all(x["http_status"] == 429 and x["error_class"] == "RateLimitError" and x["message"] for x in retries)
        assert [x["errors"] for x in [e["data"] for e in events]][-1] == 0, "a retry is not an error: the red badge stays empty"

        body = client.get("/api/llm/calls", params={"limit": 2000}).json()
        assert set(body) == {"calls", "last_id", "total", "errors", "retries"}
        assert body["calls"] == calls
        assert (body["last_id"], body["total"], body["errors"]) == (len(calls), len(calls), 0)
        assert body["retries"] == len(retries)

        # after: only the lines above an id; limit: the NEWEST lines (still oldest first); errors_only: retry / timeout / error
        tail = client.get("/api/llm/calls", params={"after": len(calls) - 3}).json()
        assert [x["id"] for x in tail["calls"]] == [len(calls) - 2, len(calls) - 1, len(calls)]
        assert [x["id"] for x in client.get("/api/llm/calls", params={"limit": 5}).json()["calls"]] == list(range(len(calls) - 4, len(calls) + 1))
        only = client.get("/api/llm/calls", params={"errors_only": "true", "limit": 2000}).json()
        assert [x["id"] for x in only["calls"]] == [x["id"] for x in retries]
        assert (only["total"], only["last_id"]) == (len(calls), len(calls)), "the counters are those of the whole log, not of the answer"
        assert client.get("/api/llm/calls", params={"after": len(calls)}).json()["calls"] == []
        for bad in ({"limit": 0}, {"limit": 2001}, {"after": -1}):
            assert client.get("/api/llm/calls", params=bad).status_code == 422, bad


def test_a_call_line_has_exactly_the_contract_fields_and_never_a_prompt_an_answer_a_header_or_a_key() -> None:
    app, c = _client()
    with c as client:
        _play(client)
        client.post("/api/mock/llm-fail", json={"actor_id": "N5", "message": "x" * 900})
        for call in client.get("/api/llm/calls", params={"limit": 2000}).json()["calls"]:
            assert set(call) == mock.CALL_KEYS
            assert mock.validate_call(call) == []
            assert len(call["message"] or "") <= 500
        # the validator says no to anything that is not a field of the contract
        good = next(x for x in client.get("/api/llm/calls", params={"limit": 2000}).json()["calls"] if x["status"] == "ok")
        for forbidden in ("prompt", "headers", "api_key", "response", "content"):
            assert mock.validate_call({**good, forbidden: "secret"}), forbidden
        assert mock.validate_call({**good, "status": "weird"}) and mock.validate_call({**good, "kind": "x"}) and mock.validate_call({**good, "id": 0})
        assert mock.validate_call({**good, "kind": "event"}), "an event has no attempt and is a runtime line"
        assert mock.validate_event({"id": 1, "type": "llm.call", "data": {"call": good}}), "errors is part of the event"
        assert mock.validate_event({"id": 1, "type": "llm.call", "data": {"call": good, "errors": -1}})
        assert mock.validate_event({"id": 1, "type": "llm.call", "data": {"call": good, "errors": 0, "prompt": "x"}})


def test_an_injected_failure_is_retries_then_the_error_then_the_agent_giving_up() -> None:
    app, c = _client()
    with c as client:
        before = client.get("/api/llm/calls").json()
        assert (before["calls"], before["last_id"], before["errors"]) == ([], 0, 0)
        out = client.post("/api/mock/llm-fail", json={"actor_id": "N3", "retries": 2}).json()
        assert out["ok"] is True and out["ids"] == [1, 2, 3, 4] and out["errors"] == 2
        body = client.get("/api/llm/calls", params={"errors_only": "true"}).json()
        lines = body["calls"]
        assert [(x["kind"], x["status"], x["attempt"]) for x in lines] == [("call", "retry", 1), ("call", "retry", 2), ("call", "error", 3), ("event", "error", None)]
        assert all(x["actor_id"] == "N3" and x["actor_name"] == "Carol" for x in lines)
        assert [x["purpose"] for x in lines] == ["turn", "turn", "turn", "runtime"]
        assert all(x["http_status"] == 429 and x["error_class"] == "RateLimitError" for x in lines)
        assert (body["errors"], body["retries"], body["total"]) == (2, 2, 4)
        # the lines were pushed as they happened, the counter of the red badge with them; the "重试用尽" toast event stays
        events = _events(app, "llm.call")
        assert [e["data"]["errors"] for e in events] == [0, 0, 1, 2]
        gave_up = _events(app, "error")
        assert len(gave_up) == 1 and gave_up[0]["data"]["agent_id"] == "N3" and "Carol" in gave_up[0]["data"]["message"]
        assert mock.validate_event(gave_up[0]) == []

        # a timeout, and a cancelled request (neither is an error of its own: a cancel is a pause, not a failure)
        out = client.post("/api/mock/llm-fail", json={"actor_id": "moderator", "status": "timeout", "retries": 0, "http_status": None, "give_up": False}).json()
        assert len(out["ids"]) == 1
        timeout = client.get("/api/llm/calls").json()["calls"][-1]
        assert (timeout["status"], timeout["actor_name"], timeout["error_class"], timeout["http_status"]) == ("timeout", "主持人", "TimeoutError", None)
        assert timeout["elapsed_ms"] == 120000 and "120" in timeout["message"]
        out = client.post("/api/mock/llm-fail", json={"actor_id": "N4", "status": "cancelled", "retries": 0}).json()
        assert len(out["ids"]) == 1, "a cancelled request is not given up: there is nothing to give up"
        cancelled = client.get("/api/llm/calls").json()
        assert cancelled["calls"][-1]["status"] == "cancelled" and cancelled["calls"][-1]["message"] is None
        assert cancelled["errors"] == 3, "timeout + 2 errors; a retry and a cancel do not count"
        assert len(_events(app, "error")) == 1, "give_up=false (and a cancel) publish no error event"
        for e in _events(app, "llm.call"):
            assert mock.validate_event(e) == []

        # a review and a refine are model calls too: purpose and actor say which
        client.post("/api/mock/finish")
        client.post("/api/game/review", json={"ids": ["N1"]})
        _wait_for(lambda: client.get("/api/state").json()["session"]["review"]["done"] == ["N1"])
        assert client.post("/api/roster/N2/refine", json={"kind": "persona", "text": "爱反问"}).status_code == 200
        by_purpose = {x["purpose"]: x for x in client.get("/api/llm/calls").json()["calls"] if x["purpose"] in ("review", "refine")}
        assert by_purpose["review"]["actor_id"] == "N1" and by_purpose["review"]["status"] == "ok"
        assert by_purpose["refine"]["actor_id"] == "roster" and by_purpose["refine"]["actor_name"] == "名册"
        assert by_purpose["refine"]["model_id"] == "deepseek-chat", "the moderator's model writes the draft"

        # bad input
        assert client.post("/api/mock/llm-fail", json={"actor_id": "N99"}).status_code == 404
        assert client.post("/api/mock/llm-fail", json={"status": "weird"}).status_code == 422
        assert client.post("/api/mock/llm-fail", json={"retries": 9}).status_code == 422


def test_a_new_game_starts_with_an_empty_log_and_the_log_is_a_ring_of_2000_lines() -> None:
    app, c = _client()
    with c as client:
        client.post("/api/mock/llm-fail", json={"actor_id": "N3"})
        assert client.get("/api/llm/calls").json()["total"] == 4
        client.post("/api/control/new")
        fresh = client.get("/api/llm/calls").json()
        assert (fresh["calls"], fresh["last_id"], fresh["total"], fresh["errors"], fresh["retries"]) == ([], 0, 0, 0, 0)
        assert client.post("/api/mock/llm-fail", json={"actor_id": "N4", "retries": 0, "give_up": False}).json()["ids"] == [1]
        game = _game(app)
        for _ in range(mock.CALL_RING + 5):
            game.log_call("N1")
        body = client.get("/api/llm/calls", params={"limit": 2000}).json()
        assert body["total"] == mock.CALL_RING and body["last_id"] == mock.CALL_RING + 6
        assert body["calls"][0]["id"] == 7 and body["calls"][-1]["id"] == body["last_id"], "the oldest lines fell out of the ring, the ids go on"
        assert body["errors"] == 0, "the counters follow the ring: the error line fell out of it too"


def test_a_rebuilt_log_is_numbered_from_1_again_the_way_the_backend_does_after_a_restart() -> None:
    """The case the page's read mark has to survive (static/js/calllog.js, tests/frontend/test_calllog_ui.js): after a restart, a load or an
    import the server rebuilds the log from the trace - a SHORTER log, ids from 1, the failed lines a few ms off, nothing announced."""
    app, c = _client()
    with c as client:
        _play(client)
        client.post("/api/mock/llm-fail", json={"actor_id": "N3", "retries": 1})
        before = client.get("/api/llm/calls", params={"limit": 2000}).json()
        announced = len(_events(app, "llm.call"))
        out = client.post("/api/mock/llm-rebuild", json={"drop_every": 4, "jitter_ms": 3}).json()
        after = client.get("/api/llm/calls", params={"limit": 2000}).json()
        assert out == {"ok": True, "total": after["total"], "last_id": after["last_id"], "errors": after["errors"], "retries": after["retries"]}
        # the lines a trace does not hold are gone; what is left is numbered 1..n again, in the same order
        gone = [x for x in before["calls"] if x["status"] == "ok" and x["purpose"] == "turn" and x["id"] % 4 == 0]
        assert gone and after["total"] == before["total"] - len(gone) and after["last_id"] == after["total"] < before["last_id"]
        assert [x["id"] for x in after["calls"]] == list(range(1, after["total"] + 1))
        kept = [x for x in before["calls"] if x not in gone]
        assert [(x["actor_id"], x["status"], x["attempt"]) for x in after["calls"]] == [(x["actor_id"], x["status"], x["attempt"]) for x in kept]
        # a failed line carries the time of its trace record (3 ms off the live one); the others keep theirs
        stamp = lambda text: datetime.fromisoformat(text.replace("Z", "+00:00"))  # noqa: E731
        for was, now in zip(kept, after["calls"]):
            shift = (stamp(now["ts"]) - stamp(was["ts"])).total_seconds() * 1000
            assert round(shift) == (0 if was["status"] == "ok" else 3), (was["status"], shift)
        assert (after["errors"], after["retries"]) == (before["errors"], before["retries"]), "the failures are all still there"
        assert [p for x in after["calls"] for p in mock.validate_call(x)] == []
        # nothing is announced: a client learns of it when it reads the log - or from the next line, which has a LOW id
        assert len(_events(app, "llm.call")) == announced
        assert client.post("/api/mock/llm-fail", json={"actor_id": "N4", "retries": 0, "give_up": False}).json()["ids"] == [after["last_id"] + 1]
        assert _events(app, "llm.call")[-1]["data"]["call"]["id"] == after["last_id"] + 1 < before["last_id"]
        # nothing dropped, nothing shifted
        same = client.post("/api/mock/llm-rebuild", json={"drop_every": 0, "jitter_ms": 0}).json()
        assert same["total"] == after["total"] + 1
        assert client.get("/api/llm/calls", params={"limit": 2000}).json()["calls"][:after["total"]] == after["calls"]
        # bad input
        assert client.post("/api/mock/llm-rebuild", json={"drop_every": -1}).status_code == 422
        assert client.post("/api/mock/llm-rebuild", json={"jitter_ms": "3"}).status_code == 422
        assert client.post("/api/mock/llm-rebuild", json={"drop_every": 5000}).status_code == 422


# ================================================================================================ thinking record
def test_the_thinking_record_is_paged_from_the_newest_and_has_no_tool_calls() -> None:
    app, c = _client()
    with c as client:
        _play(client)
        agent = max(IDS, key=lambda p: len(_game(app).thoughts.get(p, [])))  # the player who thought the most
        full: list[dict] = []
        cursor = None
        pages = 0
        while True:  # walk back through the whole record, three at a time
            params = {"limit": 3, **({"before": cursor} if cursor else {})}
            page = client.get(f"/api/agents/{agent}/thoughts", params=params).json()
            assert set(page) == {"agent_id", "name", "items", "next_before", "has_more"}
            assert (page["agent_id"], page["name"]) == (agent, _game(app).name(agent))
            assert len(page["items"]) <= 3
            full = page["items"] + full  # a page is oldest -> newest: an older page goes in FRONT
            pages += 1
            if not page["has_more"]:
                assert page["next_before"] is None
                break
            assert page["next_before"] == page["items"][0]["id"], "the cursor is the position of the oldest item of the page"
            cursor = page["next_before"]
        assert pages > 1 and len(full) > 3
        assert len({t["id"] for t in full}) == len(full), "ids are unique and stable (the page can use them as keys)"
        assert [t["id"] for t in full] == [t["id"] for t in _game(app).thoughts[agent]], "oldest -> newest, nothing lost or repeated"
        for t in full:
            assert set(t) == {"id", "ts", "gt", "gen", "round", "kind", "text", "model"}
            assert re.fullmatch(r"\d+:\d+", t["id"]) and t["kind"] in ("thought", "content") and t["text"] and t["model"]
            assert t["round"] >= 1 and t["gen"] >= 1 and t["gt"] is not None
        gens = [t["gen"] for t in full]
        assert gens == sorted(gens) and len(set(gens)) > 1, "one generation per wake-up: the page draws a divider where it changes"
        # the newest page is the default and is the tail of the record
        newest = client.get(f"/api/agents/{agent}/thoughts").json()
        assert [t["id"] for t in newest["items"]] == [t["id"] for t in full][-len(newest["items"]):]
        assert client.get("/api/agents/moderator/thoughts").json()["items"], "the moderator thinks too"
        # nothing of it travels in an event or in the session (D5 stays: it is read on demand, from a trusted client)
        wire = str([e["data"] for e in _game(app).bus.ring]) + str(client.get("/api/state").json())
        assert full[0]["text"] not in wire


def test_the_thinking_record_refuses_what_makes_no_sense_and_is_empty_before_an_agent_acted() -> None:
    app, c = _client()
    with c as client:
        empty = client.get("/api/agents/N7/thoughts").json()
        assert (empty["items"], empty["next_before"], empty["has_more"]) == ([], None, False), "no record yet: 200 and an empty page"
        r = client.get("/api/agents/N99/thoughts")
        assert r.status_code == 404 and r.json()["detail"] == "没有这个玩家。"
        assert client.get("/api/agents/N3/thoughts", params={"before": "not-a-cursor"}).status_code == 400
        assert CJK.search(client.get("/api/agents/N3/thoughts", params={"before": "x"}).json()["detail"])
        for bad in (0, 201):
            assert client.get("/api/agents/N3/thoughts", params={"limit": bad}).status_code == 422


def test_while_a_seat_is_played_the_others_thoughts_are_closed_until_the_game_is_over() -> None:
    app, c = _client()
    with c as client:
        assert client.get("/api/agents/N4/thoughts").status_code == 200, "nobody is played: open"
        assert client.post("/api/takeover", json={"seat": "N3"}).status_code == 200
        mine = client.get("/api/agents/N3/thoughts")
        assert mine.status_code == 200, "the played seat's own record (its agent's thoughts so far) is allowed"
        for other in ("N4", "moderator"):
            denied = client.get(f"/api/agents/{other}/thoughts")
            assert denied.status_code == 409
            assert denied.json()["detail"] == "接管期间不能查看其他玩家的思考记录，对局结束后开放。"
        assert client.get("/api/agents/N99/thoughts").status_code == 404, "an unknown id is a 404 first"
        # the error log is only metadata: it can be read while a seat is played
        assert client.get("/api/llm/calls").status_code == 200
        client.post("/api/mock/finish")
        assert client.get("/api/state").json()["session"]["status"] == "finished"
        assert client.get("/api/agents/N4/thoughts").status_code == 200, "after the end it is open again"
        assert client.get("/api/agents/moderator/thoughts").status_code == 200


# ============================================================================== memory (impressions) and persona
def test_the_roster_carries_limits_and_a_structured_memory_per_seat() -> None:
    app, c = _client()
    with c as client:
        roster = client.get("/api/roster").json()
        assert roster["limits"] == {"impression_chars": 80, "experience_items": 3, "experience_chars": 300, "memory_chars": 1500,
                                    "legacy_chars": 600, "persona_chars": 500, "persona_item_chars": 40}
        for seat in roster["seats"]:
            parts = seat["memory_parts"]
            assert set(parts) == {"record", "impressions", "experience", "legacy"}
            assert list(parts["impressions"]) == [p for p in IDS if p != seat["id"]], "every OTHER seat, in seat order, never oneself"
            assert set(parts["impressions"].values()) == {""} and parts["experience"] == [] and parts["legacy"] == "" and parts["record"] == ""
            assert seat["memory"] == "", "nothing to remember: the text is empty too"
        # PUT with memory_parts: only the parts given change; the canonical text has a line per other seat
        r = _put(client, "N3", memory_parts={"impressions": {"N5": "发言短，站边快。", "N12": "爱悍跳"}, "experience": ["先听完再站边。"]})
        assert r.status_code == 200
        seat = r.json()
        assert seat["memory_parts"]["impressions"]["N5"] == "发言短，站边快。" and seat["memory_parts"]["impressions"]["N1"] == ""
        lines = seat["memory"].splitlines()
        assert lines[0] == "## 印象" and "- N5 Eve：发言短，站边快。" in lines and "- N1 Alice：（暂无）" in lines
        assert lines[-3:] == ["", "## 经验", "- 先听完再站边。"]
        assert not any(line.startswith("- N3 ") for line in lines), "no impression of oneself"
        assert _seat(client, "N3") == seat
        # a second PUT changes the impression it names and leaves the rest (and the lessons) alone
        again = _put(client, "N3", memory_parts={"impressions": {"N1": "话少"}}).json()
        assert again["memory_parts"]["impressions"]["N1"] == "话少" and again["memory_parts"]["impressions"]["N5"] == "发言短，站边快。"
        assert again["memory_parts"]["experience"] == ["先听完再站边。"]
        assert _put(client, "N3", memory_parts={}).json()["memory_parts"] == again["memory_parts"], "{} changes nothing"
        # "" is no impression; `record` is accepted and ignored (a client may send back what it read)
        cleared = _put(client, "N3", memory_parts={"impressions": {"N1": ""}, "record": "场次 99"}).json()
        assert cleared["memory_parts"]["impressions"]["N1"] == "" and cleared["memory_parts"]["record"] == ""
        # the whole text of a seat can be saved back as it is: nothing changes (the parse and the render are inverse)
        assert _put(client, "N3", memory=cleared["memory"]).json()["memory_parts"] == cleared["memory_parts"]
        # a rename shows in the labels of every file (the key of an impression is the id)
        assert _put(client, "N5", name="Evelyn").status_code == 200
        assert "- N5 Evelyn：发言短，站边快。" in _seat(client, "N3")["memory"]


def test_a_put_is_applied_as_a_whole_or_not_at_all_and_says_what_is_too_long() -> None:
    app, c = _client()
    with c as client:
        _put(client, "N3", persona="先别动", memory_parts={"experience": ["留着"]})
        # 400: over a limit; the message names the part, how long it is and the limit
        r = _put(client, "N3", memory_parts={"impressions": {"N5": "x" * 81}})
        assert r.status_code == 400 and r.json()["detail"] == "N5 Eve 的印象最多 80 字（现在 81 字），请精简。"
        assert _put(client, "N3", memory_parts={"impressions": {"N5": "x" * 80}}).status_code == 200
        r = _put(client, "N3", memory_parts={"experience": ["a", "b", "c", "d"]})
        assert r.status_code == 400 and r.json()["detail"] == "经验最多 3 条（现在 4 条），请精简。"
        r = _put(client, "N3", memory_parts={"experience": ["x" * 150, "y" * 151]})
        assert r.status_code == 400 and r.json()["detail"] == "经验合计最多 300 字（现在 301 字），请精简。"
        r = _put(client, "N3", memory_parts={"legacy": "z" * 601})
        assert r.status_code == 400 and "旧记忆最多 600 字（现在 601 字）" in r.json()["detail"]
        r = _put(client, "N3", persona="p" * 501)
        assert r.status_code == 400 and r.json()["detail"] == "性格设定最多 500 个字符（现在 501 个），请精简。"
        assert _put(client, "N3", persona="p" * 500).status_code == 200, "exactly the limit is fine"
        # 422: the request makes no sense
        assert _put(client, "N3", memory_parts={"impressions": {"N3": "自己"}}).status_code == 422
        assert _put(client, "N3", memory_parts={"impressions": {"N99": "没有这个人"}}).status_code == 422
        assert _put(client, "N3", memory="x", memory_parts={}).status_code == 422
        assert _put(client, "N3", memory_parts={"mood": "x"}).status_code == 422
        assert _put(client, "N3", memory_parts={"experience": "not a list"}).status_code == 422
        assert _put(client, "N3", mood="x").status_code == 422
        # atomic: the persona of a request that is refused is not applied either (nor a name)
        before = _seat(client, "N3")
        r = _put(client, "N3", persona="新的", name="Caroline", memory_parts={"experience": ["a", "b", "c", "d"]})
        assert r.status_code == 400
        assert _seat(client, "N3") == before and before["persona"] == "p" * 500
        # the whole memory (the full text) is checked the same way
        too_long = "## 印象\n- N5 Eve：" + "x" * 81
        assert _put(client, "N3", memory=too_long).status_code == 400
        # the 1500 characters of the whole file: only extreme names get there (eleven full impressions and three full
        # lessons fit with room to spare when the names are of a usual length)
        parts = mock.empty_parts("N1")
        parts["impressions"] = {pid: "y" * 80 for pid in parts["impressions"]}
        parts["experience"] = ["z" * 100] * 3
        usual = {pid: "n" * 10 for pid in IDS}
        mock.check_memory(parts, "N1", usual)
        assert len(mock.memory_body(parts, "N1", usual)) <= mock.LIMITS["memory_chars"]
        with pytest.raises(mock.RosterRefusal) as exc:
            mock.check_memory(parts, "N1", {pid: "n" * 60 for pid in IDS})
        assert exc.value.status == 400 and "整份记忆" in exc.value.detail and "1500" in exc.value.detail


def test_the_old_memory_of_v0_13_0_lands_in_the_legacy_section_and_can_be_tidied_up() -> None:
    app, c = _client()
    with c as client:
        out = client.post("/api/mock/legacy-memory", json={"id": "N3"}).json()["seat"]
        parts = out["memory_parts"]
        assert "第 1 局：我是狼人" in parts["legacy"] and "Bob 总是附和别人" in parts["legacy"]
        assert set(parts["impressions"].values()) == {""} and parts["experience"] == []
        assert out["memory"].endswith("## 旧记忆（待整理）\n" + parts["legacy"]) and "## 印象" in out["memory"]
        # "用主持人模型整理": refine with the legacy text, apply what comes back AND delete the legacy section
        draft = client.post("/api/roster/N3/refine", json={"kind": "memory", "text": "N2 Bob：总是附和别人\n悍跳失败要复盘"}).json()
        assert set(draft) == {"draft", "memory_parts"}
        assert set(draft["memory_parts"]) == {"impressions", "experience"}, "no legacy, no record: applying it alone does not delete the old text"
        assert draft["memory_parts"]["impressions"]["N2"] == "总是附和别人" and "悍跳失败要复盘" in draft["memory_parts"]["experience"]
        assert list(draft["memory_parts"]["impressions"]) == [p for p in IDS if p != "N3"]
        assert draft["draft"].startswith("## 印象\n") and "\n\n## 经验\n" in draft["draft"] and "旧记忆" not in draft["draft"]
        assert _seat(client, "N3")["memory_parts"]["legacy"] == parts["legacy"], "refine saves nothing"
        saved = _put(client, "N3", memory_parts={**draft["memory_parts"], "legacy": ""}).json()
        assert saved["memory_parts"]["legacy"] == "" and saved["memory_parts"]["impressions"]["N2"] == "总是附和别人"
        assert "旧记忆" not in saved["memory"]
        # a free text without any heading is migrated by PUT too; over the legacy limit the mock cuts the newest 600 (on READ)
        long_text = "old " * 400
        migrated = client.post("/api/mock/legacy-memory", json={"id": "N4", "text": long_text}).json()["seat"]
        assert 590 <= len(migrated["memory_parts"]["legacy"]) <= 600 and migrated["memory_parts"]["legacy"].startswith("…")
        # clear: everything, the legacy text included
        client.post("/api/roster/memory:clear", json={})
        assert _seat(client, "N4")["memory_parts"]["legacy"] == "" and _seat(client, "N3")["memory"] == ""


def test_refine_gives_a_persona_in_the_fixed_format_and_memory_in_structure_and_saves_nothing() -> None:
    app, c = _client()
    with c as client:
        draft = client.post("/api/roster/N3/refine", json={"kind": "persona", "text": "冷静的逻辑派，喜欢用票型说话\n句子短，先结论后理由"}).json()
        assert set(draft) == {"draft"}
        text = draft["draft"]
        heads = re.findall(r"^【([^】]+)】", text, flags=re.M)
        assert heads == list(mock.PERSONA_HEADS), "the headings of the template, in order"
        assert text.startswith("【定位】冷静的逻辑派，喜欢用票型说话\n") and "- 句子短，先结论后理由" in text
        assert len(text) <= 500
        assert all(len(re.sub(r"^(?:- |【[^】]+】)", "", line)) <= 40 for line in text.splitlines()), "every item is cut to 40 characters"
        long = client.post("/api/roster/N3/refine", json={"kind": "persona", "text": "很长" * 60}).json()["draft"]
        assert long.splitlines()[0] == "【定位】" + ("很长" * 20)[:39] + "…" and len(long) <= 500, "an item that is too long is cut with an ellipsis"
        assert _seat(client, "N3")["persona"] == "", "a draft is only a proposal"
        # the draft of a persona fits the limit it will be saved under
        assert _put(client, "N3", persona=text).status_code == 200
        # memory: the notes are merged into what the seat remembers NOW (an impression it has stays unless the notes name it)
        _put(client, "N3", memory_parts={"impressions": {"N1": "旧印象"}, "experience": ["旧经验"]})
        m = client.post("/api/roster/N3/refine", json={"kind": "memory", "text": "N5：爱悍跳"}).json()
        assert m["memory_parts"]["impressions"]["N5"] == "爱悍跳" and m["memory_parts"]["impressions"]["N1"] == "旧印象"
        assert m["memory_parts"]["experience"] == ["旧经验"] and "- N5 Eve：爱悍跳" in m["draft"]
        too_long = client.post("/api/roster/N3/refine", json={"kind": "memory", "text": "N5：" + "长" * 200}).json()
        assert len(too_long["memory_parts"]["impressions"]["N5"]) == 80 and too_long["memory_parts"]["impressions"]["N5"].endswith("…")
        assert _seat(client, "N3")["memory_parts"]["impressions"]["N5"] == "", "still nothing saved"
        assert client.post("/api/roster/N3/refine", json={"kind": "persona", "text": "x" * 6001}).status_code == 400
        assert client.post("/api/roster/moderator/refine", json={"kind": "memory", "text": "x"}).status_code == 400


def test_a_finished_review_says_what_it_changed_and_overwrites_the_memory() -> None:
    app, c = _client()
    with c as client:
        client.post("/api/mock/finish")
        client.post("/api/mock/legacy-memory", json={"id": "N4"})  # an old memory the review is to tidy up
        _put(client, "N4", memory_parts={"impressions": {"N1": "我手写的印象", "N2": "另一条"}, "experience": []})
        assert client.post("/api/game/review", json={"ids": ["N4"]}).json() == {"started": ["N4"]}
        _wait_for(lambda: client.get("/api/state").json()["session"]["review"]["done"] == ["N4"])
        events = [e["data"] for e in _events(app, "review.progress")]
        assert [(d["id"], d["state"]) for d in events] == [("N4", "running"), ("N4", "done")]
        assert "changes" not in events[0], "only a finished review carries them"
        changes = events[1]["changes"]
        assert mock.validate_review_changes(changes) == []
        assert changes["impressions"] and all(c_["before"] != c_["after"] for c_ in changes["impressions"]), "only what changed is listed"
        assert {c_["id"] for c_ in changes["impressions"]} <= {p for p in IDS if p != "N4"}, "never an impression of oneself"
        assert changes["experience"] == {"before": [], "after": [mock.LESSON_BANK[0]]}
        seat = _seat(client, "N4")
        parts = seat["memory_parts"]
        assert parts["legacy"] == "", "the review tidied the old text away"
        assert all(parts["impressions"].values()), "every other seat has an impression after a review"
        assert parts["record"].startswith("场次 1 · 胜 ") and "胜率" in parts["record"]
        assert seat["memory"].startswith("## 战绩\n场次 1") and "## 对局记忆" not in seat["memory"], "overwritten, never appended"
        for c_ in changes["impressions"]:
            assert parts["impressions"][c_["id"]] == c_["after"]
        # impressions are about style and habits: no identity conclusions are written (the identities are dealt anew)
        assert not any(word in text for text in parts["impressions"].values() for word in ("狼人", "预言家", "女巫", "平民"))
        # the second reviewed player: his own memory, the same shape
        client.post("/api/game/review", json={"ids": ["N5"]})
        _wait_for(lambda: client.get("/api/state").json()["session"]["review"]["done"] == ["N4", "N5"])
        done = [e["data"] for e in _events(app, "review.progress") if e["data"]["state"] == "done"][-1]
        assert done["id"] == "N5" and len(done["changes"]["impressions"]) == 11, "nothing was remembered yet: all eleven are new"
        assert [p for e in _events(app, "review.progress") for p in mock.validate_event(e)] == []
