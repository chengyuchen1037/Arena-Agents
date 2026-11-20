"""Contract tests for the files part of tools/mock_ui_server.py (``tools/mock_ui_bots.py`` ``MockContract`` / ``MockRules`` and
``tools/mock_ui_hub.py`` ``MockGames``), v0.14: the official contract (③), the rules and their study materials (④), the games of "对局管理" (⑤).

The page is built against the mock, so its answers must have the shapes and the refusals of the real API (docs/v0.12-contracts.md §13.7 "files",
§13.9).  Checked here: ``GET / PUT /api/contract`` (+ ``:reset``, ``?download=1``, the problems of a contract that does not pass), ``GET / PUT
/api/rules/{sid}`` (+ ``:reset``, the materials), the effect of a change on every bot (``stale``), ``GET /api/games`` with the v0.14 fields,
``GET /api/games/{id}/detail``, ``DELETE`` with its ``note``, ``.arenagame`` export / import (``on_conflict`` ask / overwrite / copy, ``exists``),
and the file names of the downloads (H42).
"""

from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import mock_ui_bots as mb  # noqa: E402
import mock_ui_hub as hubmod  # noqa: E402
import mock_ui_server as mock  # noqa: E402

NOTE = "bot 已经通过复盘形成的记忆不会被删除，也无法通过删除对局撤销。"
SID = mb.DEFAULT_SCENARIO_ID


def _client(*, speed: float = 2000.0):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    app = mock.create_app(speed=speed, budget_tokens=0)
    return app, TestClient(app)


def _game(app):
    return app.state.mock_holder["game"]


def _events(app, *types, after: int = 0):
    return [e for e in _game(app).bus.ring if e["type"] in types and e["id"] > after]


def _statuses(client) -> list[str]:
    return [b["init"]["status"] for b in client.get("/api/bots").json()["bots"]]


def _error(response, status: int, code: str | None = None) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert isinstance(body["detail"], str) and any("\u4e00" <= ch <= "\u9fff" for ch in body["detail"]), body
    if code is not None:
        assert body.get("code") == code, body
    return body


# ================================================================================================= the official contract
def test_the_contract_is_a_markdown_file_with_its_source_and_what_it_has_to_mention() -> None:
    app, c = _client()
    with c as client:
        body = client.get("/api/contract").json()
        assert set(body) == {"markdown", "source", "chars", "must_read_chars", "required_terms", "problems"} and body["problems"] == []
        assert body["chars"] == len(body["markdown"]) and 0 < body["must_read_chars"] <= 1200
        assert body["source"]["origin"] == "default" and body["source"]["version"] == "v0.14.0" and body["source"]["imported_at"] is None
        assert len(body["source"]["sha256"]) == 64
        terms = body["required_terms"]
        assert {"speak", "speak_template", "pass", "respond", "jot", "read_contract", "NO_FLOOR", "TOO_LONG", "FORMAT", "NOT_FINISHED"} <= set(terms) and len(terms) == 35
        assert mb.validate_contract(body["markdown"]) == [] and body["markdown"].startswith("---\nformat: arena-contract\nversion: v0.14.0\n---\n")
        assert mb.mentions(body["markdown"], "speak") and not mb.mentions("speak_template", "speak"), "a name is a word: speak is not found in speak_template"
        download = client.get("/api/contract", params={"download": "1"})
        assert download.status_code == 200 and download.content.decode("utf-8") == body["markdown"]
        assert download.headers["content-type"] == "text/markdown; charset=utf-8"
        assert download.headers["content-disposition"] == "attachment; filename=\"contract-v0.14.0.md\"; filename*=UTF-8''contract-v0.14.0.md"


def test_a_contract_that_does_not_pass_is_refused_with_every_problem_and_changes_nothing() -> None:
    app, c = _client()
    with c as client:
        good = client.get("/api/contract").json()["markdown"]
        cases = [
            ("", ["契约是空的。"]),
            ("没有格式头\n## 必读\nx\n## 参考\ny", ["缺少格式头：文件开头需要用 --- 包住的一段，其中写 format: arena-contract。"]),
            (good.replace("format: arena-contract", "format: other"), ["格式头里的 format 必须是 arena-contract（现在是 other）。"]),
            (good.replace("version: v0.14.0", "version: 有 空格"), ["格式头里的 version 只能用字母、数字、点、下划线和连字符，最多 32 个字符。"]),
            (good.replace("## 必读", "## 别的"), ["缺少“## 必读”一节。"]),
            (good + "\n## 参考\n再来一节\n", ["“## 参考”出现了 2 次，只能有一节。"]),
            (good.replace("## 必读\n\n", "## 必读\n\n" + "字" * 1300 + "\n\n"), ["“必读”最多 1200 字（现在 "]),
            (good.replace("`jot`", "").replace("`TOO_LONG`", ""), ["契约没有提到这些工具：jot。每个工具的名字都要出现在契约里。", "契约没有提到这些回执码：TOO_LONG。每个回执码都要出现在契约里。"]),
        ]
        for markdown, expected in cases:
            r = client.put("/api/contract", json={"markdown": markdown})
            assert r.status_code == 400 and r.json()["code"] == "invalid", (markdown[:40], r.text)
            problems = r.json()["problems"]
            assert all(any(p.startswith(e) for p in problems) for e in expected), (expected, problems)
            assert r.json()["detail"].startswith("契约没有保存：")
        assert client.get("/api/contract").json()["markdown"] == good, "nothing was saved"
        assert client.put("/api/contract", json={}).status_code == 422 and client.put("/api/contract", json={"markdown": 3}).status_code == 422
        assert client.put("/api/contract", json={"markdown": good, "x": 1}).status_code == 422


def test_a_new_contract_makes_every_initialization_stale_and_reset_brings_the_default_back() -> None:
    app, c = _client()
    with c as client:
        client.get("/api/state")
        before = _game(app).bus.latest_id
        assert _statuses(client) == ["ready"] * 12
        custom = client.get("/api/contract").json()["markdown"].replace("version: v0.14.0", "version: custom-1").replace("# 平台契约", "# 我改过的平台契约")
        body = client.put("/api/contract", json={"markdown": custom}).json()
        assert body["markdown"] == custom and body["source"]["origin"] == "imported" and body["source"]["version"] == "custom-1"
        assert body["source"]["imported_at"] and body["source"]["sha256"] != mb.sha256_text(mb.default_contract_markdown())
        bots = client.get("/api/bots").json()
        assert _statuses(client) == ["stale"] * 12 and all(b["init"]["stale"] == ["contract"] for b in bots["bots"]), "the contract is one of the four sources"
        assert bots["ready"]["ok"] is False and bots["ready"]["problems"][0]["items"][0]["message"] == "初始化之后官方契约变了，需要重新初始化。"
        assert _events(app, "bots.updated", after=before), "the bots were told: the page shows ⚠ on every card"
        assert client.get("/api/state").json()["session"]["system_status"]["code"] == "uninitialized"
        assert client.get("/api/contract", params={"download": 1}).headers["content-disposition"].startswith("attachment; filename=\"contract-custom-1.md\"")
        assert [p for e in _game(app).bus.ring for p in mock.validate_event(e)] == []
        # a contract without a version in its header is "custom": the file name carries the date of the import
        nameless = custom.replace("version: custom-1\n", "")
        client.put("/api/contract", json={"markdown": nameless})
        assert client.get("/api/contract").json()["source"]["version"] == "custom"
        name = client.get("/api/contract", params={"download": "1"}).headers["content-disposition"]
        assert "filename=\"contract-custom-" in name and name.count("-") >= 2
        # reset: the default again - the initializations match it again
        reset = client.post("/api/contract:reset").json()
        assert reset["source"]["origin"] == "default" and reset["markdown"] == mb.default_contract_markdown()
        assert _statuses(client) == ["ready"] * 12


# ================================================================================================= the rules
def test_the_rules_are_scenario_files_with_a_list_of_study_materials() -> None:
    app, c = _client()
    with c as client:
        listing = client.get("/api/rules").json()
        assert [s["id"] for s in listing["scenarios"]] == [SID]
        item = listing["scenarios"][0]
        assert set(item) == {"id", "version", "title", "origin", "imported_at", "sha256", "chars", "materials", "problem"}
        assert (item["version"], item["origin"], item["imported_at"], item["materials"], item["problem"]) == ("2.2.0", "default", None, [], None)
        assert item["chars"] > 100 and len(item["sha256"]) == 64 and "狼人杀" in item["title"]
        full = client.get(f"/api/rules/{SID}").json()
        assert {k: v for k, v in full.items() if k != "markdown"} == item and full["markdown"].startswith("---\nschema_version: 2\nid: werewolf_12p\n")
        download = client.get(f"/api/rules/{SID}", params={"download": "1"})
        assert download.headers["content-disposition"] == "attachment; filename=\"werewolf_12p-2.2.0.scenario.md\"; filename*=UTF-8''werewolf_12p-2.2.0.scenario.md"
        assert download.content.decode("utf-8") == full["markdown"] and download.headers["content-type"] == "text/markdown; charset=utf-8"
        _error(client.get("/api/rules/nope"), 404, "not_found")


def test_a_scenario_file_is_checked_and_a_change_makes_every_bot_stale() -> None:
    app, c = _client()
    with c as client:
        client.get("/api/state")
        full = client.get(f"/api/rules/{SID}").json()
        before = _game(app).bus.latest_id
        new = full["markdown"].replace("version: 2.2.0", "version: 2.3.0")
        out = client.put(f"/api/rules/{SID}", json={"markdown": new}).json()
        assert out["version"] == "2.3.0" and out["origin"] == "imported" and out["imported_at"] and out["sha256"] != full["sha256"] and "markdown" not in out
        bots = client.get("/api/bots").json()
        assert _statuses(client) == ["stale"] * 12 and all(b["init"]["stale"] == ["scenario"] for b in bots["bots"])
        assert _events(app, "bots.updated", after=before)
        assert client.get(f"/api/rules/{SID}", params={"download": 1}).headers["content-disposition"].startswith("attachment; filename=\"werewolf_12p-2.3.0.scenario.md\"")
        bad = [
            ("", "剧本文件 werewolf_12p.md 是空的"),
            ("没有文件头", "缺少文件头"),
            (new.replace("schema_version: 2", "schema_version: 1"), "schema_version 必须是 2"),
            (new.replace("id: werewolf_12p", "id: another"), "剧本文件里的 id 是「another」，与这个剧本的 id「werewolf_12p」不同"),
            (new.replace("主持人手册", "别的"), "缺少“主持人手册”"),
            (new.replace("title: ", "titel: "), "缺少 title"),
        ]
        for markdown, fragment in bad:
            r = client.put(f"/api/rules/{SID}", json={"markdown": markdown})
            assert r.status_code == 400 and fragment in r.json()["detail"] and r.json()["code"] == "invalid", (fragment, r.text)
            assert r.json()["problems"] and fragment in r.json()["problems"][0]
        assert client.get(f"/api/rules/{SID}").json()["version"] == "2.3.0", "nothing changed"
        _error(client.put("/api/rules/nope", json={"markdown": new}), 404, "not_found")
        assert client.put(f"/api/rules/{SID}", json={}).status_code == 422
        reset = client.post(f"/api/rules/{SID}:reset").json()
        assert reset["version"] == "2.2.0" and reset["origin"] == "default" and reset["sha256"] == full["sha256"]
        assert _statuses(client) == ["ready"] * 12, "the default again: the initializations match it again"
        _error(client.post("/api/rules/nope:reset"), 404, "not_found")
        assert client.post(f"/api/rules/{SID}:explode").status_code == 404
        # only the default scenario has a default to go back to
        stores = app.state.mock_stores
        stores.rules.scenarios["other"] = {**stores.rules.scenarios[SID], "markdown": stores.rules.scenarios[SID]["markdown"].replace("id: werewolf_12p", "id: other")}
        _error(client.post("/api/rules/other:reset"), 400, "no_default")
        # a file that was edited by hand in the data directory: the item says why it cannot be played
        stores.rules.scenarios["other"]["markdown"] = "kaputt"
        assert "other.md" in client.get("/api/rules").json()["scenarios"][0]["problem"] or any(
            "other.md" in (s["problem"] or "") for s in client.get("/api/rules").json()["scenarios"])


def test_study_materials_are_files_of_a_scenario_and_changing_them_makes_the_bots_stale() -> None:
    app, c = _client()
    with c as client:
        client.get("/api/state")
        before = _game(app).bus.latest_id
        item = client.put(f"/api/rules/{SID}/materials/openings", json={"markdown": "# 常见开局\n\n第一天先听完警上发言。\n", "title": "开局"}).json()
        assert item["id"] == SID and item["materials"] == [{"slug": "openings", "title": "开局", "chars": len("# 常见开局\n\n第一天先听完警上发言。\n"), "sha256": item["materials"][0]["sha256"]}]
        assert _statuses(client) == ["stale"] * 12 and client.get("/api/bots").json()["bots"][0]["init"]["stale"] == ["materials"]
        assert _events(app, "bots.updated", after=before)
        got = client.get(f"/api/rules/{SID}/materials/openings").json()
        assert got == {"slug": "openings", "title": "开局", "markdown": "# 常见开局\n\n第一天先听完警上发言。\n", "chars": got["chars"], "sha256": got["sha256"]}
        no_title = client.put(f"/api/rules/{SID}/materials/notes-2", json={"markdown": "## 随手记\n正文"}).json()
        assert [m["slug"] for m in no_title["materials"]] == ["notes-2", "openings"] and no_title["materials"][0]["title"] == "随手记", "the title comes from the first line"
        download = client.get(f"/api/rules/{SID}/materials/openings", params={"download": "1"})
        assert download.headers["content-disposition"] == "attachment; filename=\"werewolf_12p-openings.material.md\"; filename*=UTF-8''werewolf_12p-openings.material.md"
        assert download.content.decode("utf-8") == got["markdown"]
        assert [m["slug"] for m in client.get("/api/rules").json()["scenarios"][0]["materials"]] == ["notes-2", "openings"]
        for slug, body, status in (("Bad Slug", {"markdown": "x"}, 400), ("x" * 33, {"markdown": "x"}, 400), ("ok", {"markdown": "  "}, 400), ("ok", {"markdown": "x" * (100 * 1024 + 1)}, 413),
                                   ("ok", {}, 422), ("ok", {"markdown": "x", "y": 1}, 422)):
            assert client.put(f"/api/rules/{SID}/materials/{slug}", json=body).status_code == status, (slug, body)
        assert "材料的标识只能用小写字母、数字和连字符" in client.put(f"/api/rules/{SID}/materials/Bad", json={"markdown": "x"}).json()["detail"]
        _error(client.put(f"/api/rules/{SID}/materials/big", json={"markdown": "字" * 40_000}), 413, "too_large")  # (a Chinese character is 3 bytes: 120 KB)
        for i in range(18):  # 2 are there already: the 21st is one too many
            assert client.put(f"/api/rules/{SID}/materials/m-{i}", json={"markdown": f"材料 {i}"}).status_code == 200
        _error(client.put(f"/api/rules/{SID}/materials/m-18", json={"markdown": "x"}), 400, "too_many")
        assert client.put(f"/api/rules/{SID}/materials/m-3", json={"markdown": "改过的"}).status_code == 200, "replacing one is no new one"
        for i in range(18):
            client.delete(f"/api/rules/{SID}/materials/m-{i}")
        _error(client.put("/api/rules/nope/materials/x", json={"markdown": "x"}), 404, "not_found")
        _error(client.get(f"/api/rules/{SID}/materials/nope"), 404, "not_found")
        # deleting the last change brings the old hashes back
        client.delete(f"/api/rules/{SID}/materials/openings")
        left = client.delete(f"/api/rules/{SID}/materials/notes-2").json()
        assert left["materials"] == [] and _statuses(client) == ["ready"] * 12
        _error(client.delete(f"/api/rules/{SID}/materials/notes-2"), 404, "not_found")


# ================================================================================================= games (对局管理)
def test_the_list_of_games_has_what_the_management_page_shows() -> None:
    app, c = _client()
    with c as client:
        body = client.get("/api/games").json()
        live, old = body["games"][0], body["games"][1:]
        assert live["current"] is True and live["session_id"] == body["current"] and [g["session_id"] for g in old] == [
            "mock-old-1", "mock-old-2", "mock-old-3", "mock-old-4", "mock-broken-5"]
        keys = {"session_id", "title", "scenario_name", "scenario_title", "status", "winner", "created_at", "started_at", "finished_at", "updated_at", "duration_gt",
                "players", "tokens", "cost_usd", "human_involved", "schema_version", "size_bytes", "log_bytes", "readable", "review", "participants_brief", "current"}
        assert all(keys <= set(g) for g in body["games"])
        assert [g["schema_version"] for g in old] == [6, 6, 5, 6, None] and [g["readable"] for g in old] == [True, True, True, True, False]
        assert [g["review"] for g in old] == [{"done": 5, "total": 12}, {"done": 0, "total": None}, {"done": 0, "total": None}, {"done": 12, "total": 12}, {"done": 0, "total": None}]
        assert old[0]["participants_brief"] == mb.DEFAULT_NAMES and old[4]["status"] == "unreadable" and old[4]["participants_brief"] == []
        assert all(g["size_bytes"] > 0 and g["log_bytes"] >= 0 for g in body["games"]) and live["players"] == 12
        assert live["review"] == {"done": 0, "total": None} and live["participants_brief"] == mb.DEFAULT_NAMES


def test_the_detail_of_a_game_says_who_sat_where_what_they_were_and_whether_they_were_reviewed() -> None:
    app, c = _client()
    with c as client:
        detail = client.get("/api/games/mock-old-1/detail").json()
        assert set(detail) == {"participants", "files", "human_spans"}
        rows = detail["participants"]
        assert [r["seat"] for r in rows] == [f"N{i}" for i in range(1, 13)] and [r["name"] for r in rows] == mb.DEFAULT_NAMES
        assert all(set(r) == {"seat", "bot_id", "name", "avatar", "role", "faction", "review"} for r in rows)
        assert [r["review"] for r in rows] == ["done"] * 5 + ["pending"] * 7, "5 of 12 were reviewed"
        assert rows[2]["role"] == "狼人" and rows[2]["faction"] == "狼人阵营" and rows[3]["role"] == "预言家" and rows[3]["faction"] == "好人阵营"
        assert detail["files"]["game_bytes"] > 0 and detail["files"]["log_bytes"] > 0 and detail["human_spans"] == []
        # a game that is not over: no identity yet; a v5 game cannot be reviewed (``na``) and has no bots; a person's stretch of the ledger is listed
        paused = client.get("/api/games/mock-old-2/detail").json()
        # (like the real server: no identity yet; ``pending`` - not ``na`` - for a game that has bots but is not over)
        assert all(r["role"] is None and r["faction"] is None and r["review"] == "pending" for r in paused["participants"])
        assert paused["human_spans"] == [{"seat": "N3", "from_seq": 40, "to_seq": 218}] and paused["files"]["prev_bytes"] > 0
        v5 = client.get("/api/games/mock-old-3/detail").json()
        assert all(r["bot_id"] == "" and r["review"] == "na" and r["role"] for r in v5["participants"])
        assert all(r["bot_id"].startswith("b-") for r in rows)
        # the current game: seats and bots as they are now; nothing over yet
        live = client.get("/api/state").json()["session"]
        mine = client.get(f"/api/games/{live['session_id']}/detail").json()
        assert [r["bot_id"] for r in mine["participants"]] == [p["bot_id"] for p in live["participants"]] and all(r["role"] is None for r in mine["participants"])
        broken = client.get("/api/games/mock-broken-5/detail")
        assert broken.status_code == 422 and broken.json() == {"detail": hubmod.UNREADABLE_SAVE}, "a save that cannot be opened: 422, no code"
        _error(client.get("/api/games/nope/detail"), 404, "not_found")
        assert client.get("/api/games/bad.id/detail").status_code == 400


def test_deleting_a_game_says_what_it_does_not_delete() -> None:
    app, c = _client()
    with c as client:
        live = client.get("/api/games").json()["current"]
        out = client.delete("/api/games/mock-old-3")
        # (the answer has a fixed shape: a game that is not the current one makes no new game)
        assert out.status_code == 200 and out.json() == {"ok": True, "session_id": "mock-old-3", "note": NOTE, "new_game": False, "new_session_id": None}
        assert client.delete("/api/games/mock-old-3").status_code == 404 and client.delete("/api/games/bad.id").status_code == 400
        assert "mock-old-3" not in [g["session_id"] for g in client.get("/api/games").json()["games"]]
        bots = client.get("/api/bots").json()["bots"]
        assert bots[0]["memory_stats"]["record"].startswith("场次 7"), "the memory of the bots is not touched by deleting a game"
        # an unreadable game can be deleted too (the way to get rid of it)
        assert client.delete("/api/games/mock-broken-5").json()["note"] == NOTE


def test_the_current_game_can_be_deleted_unless_it_runs_and_a_new_empty_game_takes_its_place() -> None:
    """Lead's decision (files stream): idle / paused / finished can be deleted; ``status == "running"`` answers 409; the answer says a new game was
    made (``new_game`` / ``new_session_id``) and the clients get its snapshot."""
    app, c = _client(speed=1.0)
    with c as client:
        live = client.get("/api/games").json()["current"]
        before = _game(app).bus.latest_id
        out = client.delete(f"/api/games/{live}")
        assert out.status_code == 200 and out.json()["note"] == NOTE and out.json()["new_game"] is True
        assert out.json()["new_session_id"] != live and client.get("/api/games").json()["current"] == out.json()["new_session_id"]
        snapshot = [e for e in _events(app, "snapshot", after=before)][-1]
        assert snapshot["data"]["session"]["session_id"] == out.json()["new_session_id"] and mock.validate_event(snapshot) == []
        # a running game cannot be deleted; a paused one can
        assert client.post("/api/control/start").status_code == 200
        running = client.get("/api/games").json()["current"]
        refused = client.delete(f"/api/games/{running}")
        assert refused.status_code == 409 and refused.json() == {"detail": "进行中的对局不能删除，请先暂停。"}
        client.post("/api/control/pause")
        assert client.delete(f"/api/games/{running}").json()["new_game"] is True


def test_a_game_whose_files_are_in_use_cannot_be_deleted_for_the_moment() -> None:
    """The real server: 409 ``in_use`` with ``new_game`` / ``new_session_id`` (the current game's empty successor exists already, the answer says so)."""
    app, c = _client()
    with c as client:
        live = client.get("/api/games").json()["current"]
        assert client.post("/api/mock/games", json={"locked": ["mock-old-2", live]}).json()["locked"] == sorted(["mock-old-2", live])
        busy = client.delete("/api/games/mock-old-2")
        assert busy.status_code == 409 and busy.json()["code"] == "in_use" and busy.json()["new_game"] is False and busy.json()["new_session_id"] is None
        assert "mock-old-2" in [g["session_id"] for g in client.get("/api/games").json()["games"]], "it is still there"
        current = client.delete(f"/api/games/{live}")
        assert current.status_code == 409 and current.json()["code"] == "in_use" and current.json()["new_game"] is True
        assert current.json()["new_session_id"] and current.json()["new_session_id"] != live
        assert client.get("/api/games").json()["current"] == current.json()["new_session_id"], "the empty game was made all the same"
        client.post("/api/mock/games", json={"locked": []})
        assert client.delete("/api/games/mock-old-2").status_code == 200
        assert client.post("/api/mock/games", json={"locked": "x"}).status_code == 422


def test_a_game_is_exported_as_an_arenagame_file_and_imported_with_a_choice_when_the_id_exists() -> None:
    app, c = _client()
    with c as client:
        response = client.get("/api/games/mock-old-1/export")
        assert response.status_code == 200 and response.headers["content-type"] == "application/octet-stream"
        disposition = response.headers["content-disposition"]
        assert disposition.startswith("attachment; filename=\"game-mock-old-1-") and disposition.count(".arenagame") == 2
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            manifest = json.loads(z.read("manifest.json"))
            names = set(z.namelist())
        assert manifest["format"] == "arenagame" and manifest["format_version"] == 1 and manifest["session_id"] == "mock-old-1"
        assert {"games/mock-old-1/game.json", "logs/mock-old-1/trace-0001.jsonl"} <= names and "games/mock-old-1/game.prev.json" not in names
        assert all(f["path"] in names and f["sha256"] and f["size"] for f in manifest["files"])
        # a save that cannot be opened (v4, damaged): 422 invalid_archive; unknown: 404
        _error(client.get("/api/games/mock-broken-5/export"), 422, "invalid_archive")
        _error(client.get("/api/games/nope/export"), 404, "not_found")

        # the id exists: ask (the default) -> 409 exists; overwrite replaces it; copy keeps both under a new id
        asked = client.post("/api/games/import?name=old.arenagame", content=response.content)
        assert asked.status_code == 409 and asked.json()["code"] == "exists" and asked.json()["session_id"] == "mock-old-1" and asked.json()["detail"]
        _error(client.post("/api/games/import?on_conflict=whatever", content=response.content), 400, "bad_request")
        replaced = client.post("/api/games/import?on_conflict=overwrite", content=response.content).json()
        assert replaced == {"session_id": "mock-old-1", "status": "finished", "replaced": True, "copied": False}
        copied = client.post("/api/games/import?on_conflict=copy", content=response.content).json()
        assert copied["copied"] is True and copied["replaced"] is False and copied["session_id"].startswith("mock-old-1-copy") and copied["session_id"] != "mock-old-1"
        listing = [g["session_id"] for g in client.get("/api/games").json()["games"]]
        assert "mock-old-1" in listing and copied["session_id"] in listing and len(listing) == len(set(listing))
        # delete it, then import it as a new game: no conflict
        client.delete("/api/games/mock-old-2")
        two = client.get("/api/games/mock-old-1/export").content
        client.delete("/api/games/mock-old-1")
        fresh = client.post("/api/games/import?name=x.arenagame", content=two).json()
        assert fresh == {"session_id": "mock-old-1", "status": "finished", "replaced": False, "copied": False}
        assert client.get("/api/games/mock-old-1/detail").status_code == 200
        # the current game can be exported, but not overwritten (409 current); as a copy it can be imported.  Its id counts as an existing id for `ask`
        live = client.get("/api/games").json()["current"]
        mine = client.get(f"/api/games/{live}/export")
        assert mine.status_code == 200
        assert client.post("/api/games/import", content=mine.content).json()["code"] == "exists"
        _error(client.post("/api/games/import?on_conflict=overwrite", content=mine.content), 409, "current")
        assert client.post("/api/games/import?on_conflict=copy", content=mine.content).json()["copied"] is True
        # another import (or an archive export / import) is running: 409 busy
        client.post("/api/mock/games", json={"importing": True})
        _error(client.post("/api/games/import?on_conflict=copy", content=mine.content), 409, "busy")
        client.post("/api/mock/games", json={"importing": False})
        assert client.post("/api/games/import?on_conflict=copy", content=mine.content).status_code == 200


def test_the_current_game_cannot_be_exported_while_it_runs_or_a_seat_is_played() -> None:
    """The file holds every identity: the same rule as the replay of the current game (409, the real server's sentence)."""
    app, c = _client(speed=1.0)
    with c as client:
        live = client.get("/api/games").json()["current"]
        assert client.get(f"/api/games/{live}/export").status_code == 200, "an idle game can"
        assert client.post("/api/control/start").status_code == 200
        r = client.get(f"/api/games/{live}/export")
        assert r.status_code == 409 and r.json()["detail"] == "对局进行中或正在接管一个座位，不能导出当前对局：请先暂停并退出接管。"
        client.post("/api/control/pause")
        assert client.get(f"/api/games/{live}/export").status_code == 200, "paused: fine"
        # a seat that is played: the same refusal (before the start the takeover is allowed; a paused game's takeover needs the game to be paused)
        assert client.post("/api/takeover", json={"seat": "N3"}).status_code == 200
        r = client.get(f"/api/games/{live}/export")
        assert r.status_code == 409 and "不能导出当前对局" in r.json()["detail"]
        assert client.post("/api/takeover/release").status_code == 200
        assert client.get(f"/api/games/{live}/export").status_code == 200


def test_an_arenagame_file_that_is_none_or_has_been_changed_is_refused() -> None:
    app, c = _client()
    with c as client:
        good = client.get("/api/games/mock-old-1/export").content
        for data, fragment in ((b"hello", "不是 zip"), (b"", "没有收到")):
            r = client.post("/api/games/import?on_conflict=copy", content=data)
            assert r.status_code in (400, 422) and fragment in r.json()["detail"], (data, r.text)
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(good)) as src, zipfile.ZipFile(buf, "w") as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                dst.writestr(item.filename, data.replace(b"mock-old-1", b"mock-old-9") if item.filename.startswith("logs/") else data)
        tampered = client.post("/api/games/import?on_conflict=copy", content=buf.getvalue())
        assert tampered.status_code == 422 and tampered.json()["code"] == "invalid_archive" and "SHA-256" in tampered.json()["detail"]
        # a manifest of another format, a game of an unsupported save format
        other = io.BytesIO()
        with zipfile.ZipFile(other, "w") as z:
            z.writestr("manifest.json", json.dumps({"format": "arenabot", "format_version": 1}))
        wrong = client.post("/api/games/import?on_conflict=copy", content=other.getvalue())
        assert wrong.status_code == 422 and wrong.json()["code"] == "invalid_archive"
        old = io.BytesIO()
        with zipfile.ZipFile(old, "w") as z:
            game = json.dumps({"session_id": "g4", "schema_version": 4})
            z.writestr("manifest.json", json.dumps({"format": "arenagame", "format_version": 1, "session_id": "g4",
                                                    "files": [{"path": "games/g4/game.json", "size": len(game), "sha256": mb.sha256_text(game)}]}))
            z.writestr("games/g4/game.json", game)
        r = client.post("/api/games/import?on_conflict=copy", content=old.getvalue())
        assert r.status_code == 422 and "格式 5 或 6" in r.json()["detail"] and r.json()["code"] == "invalid_archive"
        assert "mock-old-9" not in [g["session_id"] for g in client.get("/api/games").json()["games"]]


# ================================================================================================= the pieces the page keeps using
def test_the_history_dialog_still_loads_and_replays_what_the_management_lists() -> None:
    app, c = _client()
    with c as client:
        assert client.get("/api/games/mock-old-1/replay").status_code == 200
        # a save that cannot be opened (the real server's words): the replay answers 422 like the detail, "继续" 400 with the old-format notice
        replay = client.get("/api/games/mock-broken-5/replay")
        assert replay.status_code == 422 and replay.json() == {"detail": hubmod.UNREADABLE_SAVE}
        load = client.post("/api/games/mock-broken-5/load")
        assert load.status_code == 400 and load.json() == {"detail": hubmod.UNREADABLE_LOAD}
        assert client.post("/api/games/mock-old-2/load").json()["session"]["session_id"] == "mock-old-2"


def test_the_download_names_never_carry_what_a_file_system_would_choke_on() -> None:
    assert mb.safe_filename('A/b\\c:d*e?f"g<h>i|j') == "Abcdefghij"
    assert mb.safe_filename("  ..  ", "bot") == "bot" and mb.safe_filename("", "bot") == "bot"
    header = hubmod.content_disposition("小明.core.md")
    assert header == "attachment; filename=\"__.core.md\"; filename*=UTF-8''%E5%B0%8F%E6%98%8E.core.md"
    assert "\n" not in header and "\"" not in header.split("filename=")[1].split(";")[0].strip("\"")
