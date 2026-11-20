"""Contract tests for the bots part of tools/mock_ui_server.py (``tools/mock_ui_bots.py`` + ``tools/mock_ui_hub.py``), v0.14.

The page is built against the mock, so its answers must have the shapes and the refusals of the real API (docs/v0.12-contracts.md §13.2, §13.7,
§13.8; ``app/api/bots.py`` and ``tests/test_bots_api.py`` are the spec of the botstore half).  What is checked here:

* ``GET /api/bots`` (``BotsPayload`` with ``limits`` / ``defaults``), the bots at the table in ``PublicSession`` (``bots``, ``participants[i].bot_id`` /
  ``init_status``, ``system_status``), the states a page has to show (``/api/mock/bots`` presets);
* the line-up, names and avatars (idle only, ``game_active``), the frozen bot and its confirmation (``needs_confirm`` with ``will_clear``, a new id,
  the binding that follows), ``name_taken``;
* L0 as the merged file (download, ``PUT`` with every problem, ``notices``, clear), ``core:refine`` and its failures;
* the memory parts (user edits take effect at once and do not touch the initialization, ``DELETE``), the proposals of a review (accept / reject);
* ``.arenabot`` export / import / import-default;
* initialization as a job (``bot.init.progress`` ticks, ``bots.updated``, ``init_busy``, ``game_active``, ``no_binding``, cancel, failures, timeouts,
  the learning use) and the start check (``409 NOT_READY`` with ``Problem``s; nothing is dealt);
* the review of a finished game (``review.progress``: queued -> running -> done / timeout / error, ``changes``, proposals).
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

IDS = [f"N{i}" for i in range(1, 13)]
NAMES = ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo"]
LIMIT_KEYS = {
    "name_chars", "core_limits", "core_extra_max", "core_extra_chars", "extra_title_chars", "arch_notes_chars", "digest_chars", "questions_chars",
    "playbook_max", "playbook_entry_chars", "people_max", "impression_chars", "contract_must_read_chars", "init_timeout_s", "review_timeout_s", "jot_max",
    "jot_chars", "memory_parts", "part_labels",
}
NEEDS_CONFIRM_TEXT = (
    "修改名字或头像会清空这个玩家的初始化结果和全部记忆（操作笔记、规则要点、打法、待解问题、印象、战绩），"
    "底层设定保留。修改后需要重新初始化才能开局。是否确认？"
)


def _client(*, bots: str = "ready", speed: float = 2000.0):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    app = mock.create_app(speed=speed, budget_tokens=0, bots=bots)
    return app, TestClient(app)


def _wait_for(predicate, timeout: float = 20.0, step: float = 0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("timed out")


def _game(app):
    return app.state.mock_holder["game"]


def _events(app, *types, after: int = 0):
    return [e for e in _game(app).bus.ring if e["type"] in types and e["id"] > after]


def _valid(app, after: int = 0) -> list[str]:
    return [f"#{e['id']} {e['type']}: {p}" for e in _game(app).bus.ring if e["id"] > after for p in mock.validate_event(e)]


def _bots(client, **kw):
    return client.get("/api/bots").json()


def _ids(client) -> list[str]:
    return _bots(client)["lineup"]


def _detail(client, bot_id):
    return client.get(f"/api/bots/{bot_id}").json()


def _error(response, status: int, code: str | None = None) -> dict:
    """An error answer: the status, ``detail`` in Chinese (every message the page shows is), and the ``code`` the page switches on."""
    assert response.status_code == status, response.text
    body = response.json()
    assert isinstance(body["detail"], str) and any("\u4e00" <= ch <= "\u9fff" for ch in body["detail"]), body
    if code is not None:
        assert body.get("code") == code, body
    return body


def _no_job(client) -> bool:
    return _bots(client)["init_job"] is None


# ================================================================================================= the table
def test_the_table_is_twelve_bots_with_their_seats_and_what_the_ui_counts_against() -> None:
    app, c = _client()
    with c as client:
        payload = _bots(client)
        assert mock.hubmod.validate_bots_payload(payload, full=True) == []
        assert set(payload) == {"lineup", "bots", "memory_enabled", "init_job", "ready", "learning_usage", "limits", "defaults"}
        assert payload["lineup"] == [b["id"] for b in payload["bots"]] and len(set(payload["lineup"])) == 12
        assert [b["seat"] for b in payload["bots"]] == IDS and [b["name"] for b in payload["bots"]] == NAMES
        assert all(b["id"].startswith("b-") and len(b["id"]) == 10 for b in payload["bots"]), "the internal id: b-<8 hex>, never shown"
        assert all(mock.validate_avatar(b["avatar"]) == [] and b["avatar"] and b["avatar_code"] for b in payload["bots"])
        assert len({b["avatar_code"] for b in payload["bots"]}) == 12
        assert (payload["memory_enabled"], payload["init_job"]) == (True, None)
        assert payload["ready"] == {"ok": True, "problems": []} and payload["learning_usage"] == {"calls": 0, "tokens": 0, "cost_usd": 0.0}
        # the numbers the page counts against: BotStore.limits_payload() (C15 ...)
        limits = payload["limits"]
        assert set(limits) == LIMIT_KEYS
        assert limits["core_limits"] == {"identity": 300, "voice": 600, "mind": 400} and (limits["core_extra_max"], limits["core_extra_chars"]) == (3, 300)
        assert (limits["name_chars"], limits["init_timeout_s"], limits["review_timeout_s"], limits["jot_max"]) == (20, 120, 60, 10)
        assert limits["memory_parts"] == ["arch_notes", "digest", "playbook", "questions", "people"]
        assert limits["part_labels"] == {"arch_notes": "操作笔记", "digest": "规则要点", "playbook": "打法", "questions": "待解问题", "people": "印象"}
        # the default library: twelve names and a line each (what "从默认库导入" offers)
        assert [d["name"] for d in payload["defaults"]] == NAMES and all(d["identity"] and len(d["identity"]) <= 40 and d["avatar"] for d in payload["defaults"])
        # a summary: L0 is complete, initialized, frozen (🔒), a little history
        alice = payload["bots"][0]
        assert alice["core"]["complete"] is True and alice["core"]["missing"] == [] and set(alice["core"]["chars"]) == {"identity", "voice", "mind"}
        assert alice["core"]["extras"] == [{"title": "口头禅", "slug": "x1", "chars": 13}]
        assert alice["init"]["status"] == "ready" and alice["init"]["stale"] == [] and alice["init"]["model"] and alice["init"]["duration_s"]
        assert alice["frozen"] is True and alice["origin"] == "default" and alice["staging"] == 0
        assert alice["memory_stats"]["playbook"] == 3 and alice["memory_stats"]["people"] == 2 and alice["memory_stats"]["record"].startswith("场次 7")
        assert alice["binding"] == {"ok": True, "label": "DeepSeek（模拟） / deepseek-flash"}
        assert payload["bots"][5]["memory_stats"]["record"] == "" and payload["bots"][5]["memory_stats"]["people"] == 0, "nothing learned from games yet"


def test_the_bots_at_the_table_are_in_the_session_and_every_change_is_announced() -> None:
    app, c = _client()
    with c as client:
        session = client.get("/api/state").json()["session"]
        assert mock.validate_session(session) == [] and session["schema_version"] == 6
        live = {k: v for k, v in _bots(client).items() if k not in ("limits", "defaults")}
        assert session["bots"] == live, "PublicSession.bots is the live part of BotsPayload (no limits, no defaults)"
        ids = _ids(client)
        assert [p["bot_id"] for p in session["participants"]] == ids and [p["init_status"] for p in session["participants"]] == ["ready"] * 12
        assert session["system_status"] == {"code": "before_game", "text": "对局前", "reason": ""} and session["dealt"] is False
        before = _game(app).bus.latest_id
        assert client.put("/api/bots/memory-enabled", json={"enabled": False}).json()["memory_enabled"] is False
        updated = _events(app, "bots.updated", after=before)
        assert len(updated) == 1 and set(updated[0]["data"]) == {"bots"} and updated[0]["data"]["bots"]["memory_enabled"] is False
        assert "limits" not in updated[0]["data"]["bots"]
        assert _events(app, "session.updated", after=before)[-1]["data"]["memory_enabled"] is False, "the status bar's switch follows"
        assert _valid(app, before) == []
        assert client.put("/api/bots/memory-enabled", json={"enabled": "maybe"}).status_code == 422
        assert client.put("/api/bots/memory-enabled", json={}).status_code == 422


@pytest.mark.parametrize(
    "preset,statuses,codes",
    [
        ("fresh", ["uninitialized"] * 12, {"not_initialized"}),
        ("mixed", ["ready", "stale", "failed", "uninitialized", "uninitialized", "stale", "stale"] + ["ready"] * 5, {"stale", "init_failed", "not_initialized", "core_missing"}),
        ("empty", ["uninitialized"] * 12, {"core_missing", "not_initialized"}),
    ],
)
def test_the_presets_put_the_bots_into_the_states_a_page_has_to_show(preset: str, statuses: list, codes: set) -> None:
    app, c = _client(bots=preset)
    with c as client:
        payload = _bots(client)
        assert [b["init"]["status"] for b in payload["bots"]] == statuses
        found = {i["code"] for p in payload["ready"]["problems"] for i in p["items"]}
        assert found == codes and payload["ready"]["ok"] is False
        assert mock.hubmod.validate_bots_payload(payload, full=True) == [] and mock.validate_session(client.get("/api/state").json()["session"]) == []
        status = client.get("/api/state").json()["session"]["system_status"]
        assert status == {"code": "uninitialized", "text": "未初始化", "reason": ""}, "H26: not ready bots -> 未初始化 before the game"
    # ... and the same through the switch, any time before the game starts
    app, c = _client()
    with c as client:
        before = _game(app).bus.latest_id if "game" in app.state.mock_holder else 0
        out = client.post("/api/mock/bots", json={"preset": preset}).json()
        assert [b["init"]["status"] for b in out["bots"]] == statuses
        assert _events(app, "bots.updated", "roster.updated", after=before), "the page is told"
        assert client.post("/api/mock/bots", json={"preset": "nonsense"}).status_code == 422
        assert client.put("/api/bots/memory-enabled", json={"enabled": True}).status_code == 200
        client.post("/api/mock/bots", json={"preset": "ready"})
        assert client.post("/api/control/start").status_code == 200
        _error(client.post("/api/mock/bots", json={"preset": "fresh"}), 409, "game_active")
        client.post("/api/control/pause")


def test_the_mixed_preset_explains_every_state_in_words_and_with_a_way_to_fix_it() -> None:
    app, c = _client(bots="mixed")
    with c as client:
        problems = {p["seat"]: p for p in _bots(client)["ready"]["problems"]}
        assert set(problems) == {"N2", "N3", "N4", "N5", "N6", "N7"}
        item = lambda seat, code: next(i for i in problems[seat]["items"] if i["code"] == code)  # noqa: E731
        assert item("N2", "stale") == {"code": "stale", "message": "初始化之后底层设定变了，需要重新初始化。", "fix": "init"}
        assert item("N3", "init_failed")["message"].startswith("上次初始化没有成功：模型调用失败") and item("N3", "init_failed")["fix"] == "init"
        assert item("N4", "not_initialized") == {"code": "not_initialized", "message": "还没有初始化。", "fix": "init"}
        assert item("N6", "core_missing")["fix"] == "import_core" and "身份、说话方式、分析方式都是空的" in item("N6", "core_missing")["message"]
        assert item("N7", "stale")["message"] == "初始化之后官方契约变了，需要重新初始化。"
        assert all(p["scope"] == "bot" and p["bot_id"] and p["name"] for p in problems.values())
        detail = _detail(client, _ids(client)[1])
        assert detail["init"]["status"] == "stale" and detail["init"]["stale"] == ["core"]


# ================================================================================================= line-up, names, avatars
def test_the_lineup_is_a_permutation_and_everything_about_a_seat_follows_the_bot() -> None:
    app, c = _client()
    with c as client:
        ids = _ids(client)
        old_bindings = client.get("/api/settings/bindings").json()["bindings"]
        before = _game(app).bus.latest_id if "game" in app.state.mock_holder else 0
        order = list(reversed(ids))
        out = client.put("/api/bots/lineup", json={"order": order})
        assert out.status_code == 200 and out.json()["lineup"] == order
        assert [b["seat"] for b in out.json()["bots"]] == IDS and [b["name"] for b in out.json()["bots"]] == list(reversed(NAMES))
        session = client.get("/api/state").json()["session"]
        assert [p["name"] for p in session["participants"]] == list(reversed(NAMES)) and [p["bot_id"] for p in session["participants"]] == order
        # the seats' avatars are the bots'; roster.updated carries every seat (and the moderator's avatar)
        roster = _events(app, "roster.updated", after=before)[-1]["data"]
        assert [p["id"] for p in roster["participants"]] == IDS and roster["participants"][0]["name"] == "Leo" and roster["moderator"]["id"] == "moderator"
        assert _events(app, "bots.updated", after=before) and _valid(app, before) == []
        # the model bindings follow the bots (H4): N1 now has what N12 had
        bindings = client.get("/api/settings/bindings").json()["bindings"]
        assert bindings["N1"] == old_bindings["N12"] and bindings["N12"] == old_bindings["N1"] and bindings["moderator"] == old_bindings["moderator"]
        assert session["model_bindings"]["N1"] == old_bindings["N12"]
        # refusals: not a permutation (422 bad_lineup, nothing changes), extra fields (422), after the start (409 game_active)
        for bad in (order[:-1], order + [order[0]], [*order[:-1], "b-ffffffff"], [*order[:-1], order[0]], "nope", [1, 2]):
            _error(client.put("/api/bots/lineup", json={"order": bad}), 422, "bad_lineup")
        assert client.put("/api/bots/lineup", json={"order": order, "x": 1}).status_code == 422
        assert client.put("/api/bots/lineup", json={}).status_code == 422
        assert _ids(client) == order
        assert client.post("/api/control/start").status_code == 200
        body = _error(client.put("/api/bots/lineup", json={"order": ids}), 409, "game_active")
        assert body["detail"] == "阵容只能在对局开始之前修改；请先新建对局。"
        client.post("/api/control/pause")


def test_a_bot_that_has_learned_nothing_is_renamed_in_place_and_a_bad_name_changes_nothing() -> None:
    app, c = _client(bots="fresh")
    with c as client:
        bot = _ids(client)[1]
        out = client.put(f"/api/bots/{bot}", json={"name": "  Bobby "})
        assert out.status_code == 200 and out.json()["id"] == bot and out.json()["name"] == "Bobby" and out.json()["frozen"] is False
        assert client.get("/api/state").json()["session"]["participants"][1]["name"] == "Bobby"
        avatar = _detail(client, _ids(client)[2])["avatar"]
        swapped = client.put(f"/api/bots/{bot}", json={"avatar": avatar}).json()
        assert swapped["id"] == bot and swapped["avatar"] == avatar and swapped["avatar_code"]
        assert client.put(f"/api/bots/{bot}", json={}).json()["id"] == bot, "nothing to change"
        bad = [
            ({"name": "alice"}, 400, "name_taken", "已被 N1 使用"),
            ({"name": ""}, 400, None, "不能为空"),
            ({"name": "x" * 21}, 400, None, "最多 20"),
            ({"name": "a:b"}, 400, None, "冒号"),
            ({"name": "a：b"}, 400, None, "冒号"),
            ({"name": 'a"b'}, 400, None, "引号"),
            ({"avatar": {"v": 1}}, 400, None, "头像"),
            ({"avatar": "face"}, 422, None, ""),
            ({"surprise": 1}, 422, None, ""),
            ({"name": 5}, 422, None, ""),
            ({"confirm_reset": "yes"}, 422, None, ""),
        ]
        for body, status, code, fragment in bad:
            response = client.put(f"/api/bots/{bot}", json=body)
            assert response.status_code == status and fragment in response.json()["detail"], (body, response.text)
            if code:
                assert response.json()["code"] == code
        assert _detail(client, bot)["name"] == "Bobby"
        _error(client.put("/api/bots/b-ffffffff", json={"name": "Zed"}), 404, "not_found")
        # keeping its own name is no duplicate; a name of 20 characters is fine
        assert client.put(f"/api/bots/{bot}", json={"name": "Bobby"}).status_code == 200
        assert client.put(f"/api/bots/{bot}", json={"name": "字" * 20}).status_code == 200


def test_a_bot_that_has_learned_something_needs_a_confirmation_to_change_its_name_or_avatar() -> None:
    app, c = _client()
    with c as client:
        ids = _ids(client)
        bot = ids[0]
        original = _detail(client, bot)
        assert original["frozen"] is True
        body = _error(client.put(f"/api/bots/{bot}", json={"name": "Alicia"}), 409, "needs_confirm")
        assert body["detail"] == NEEDS_CONFIRM_TEXT and body["will_clear"] == ["初始化结果", "操作笔记", "规则要点", "打法", "待解问题", "印象", "战绩"]
        _error(client.put(f"/api/bots/{bot}", json={"name": "Alicia", "confirm_reset": False}), 409, "needs_confirm")
        _error(client.post(f"/api/bots/{bot}/avatar:regenerate"), 409, "needs_confirm")
        assert _detail(client, bot)["name"] == "Alice", "nothing changed"
        before = _game(app).bus.latest_id
        out = client.put(f"/api/bots/{bot}", json={"name": "Alicia", "confirm_reset": True}).json()
        assert out["id"] != bot and out["name"] == "Alicia" and out["seat"] == "N1" and out["frozen"] is False
        assert out["init"]["status"] == "uninitialized" and out["memory"]["arch_notes"] == "" and out["memory"]["record"] == "" and out["memory"]["people"] == {}
        assert out["core"]["markdown"].split("\n---\n", 1)[1] == original["core"]["markdown"].split("\n---\n", 1)[1], "L0 is kept"
        assert _ids(client)[0] == out["id"], "the line-up points at the new id"
        _error(client.get(f"/api/bots/{bot}"), 404, "not_found")
        # the default model binding went with the bot
        assert client.get("/api/settings/bindings").json()["bindings"]["N1"]["model_id"] == "deepseek-flash"
        assert _bots(client)["bots"][0]["binding"]["ok"] is True
        assert _events(app, "roster.updated", after=before) and _events(app, "bots.updated", after=before) and _valid(app, before) == []
        # the avatar is the same kind of change
        second = ids[1]
        regenerated = client.post(f"/api/bots/{second}/avatar:regenerate", json={"confirm_reset": True}).json()
        assert regenerated["id"] != second and regenerated["avatar"] != _detail(client, ids[2])["avatar"]


def test_names_and_avatars_belong_to_the_table_before_the_game_starts() -> None:
    app, c = _client(bots="fresh")
    with c as client:
        assert client.post("/api/control/start").status_code == 409  # (not ready: nothing started)
        assert client.get("/api/state").json()["session"]["status"] == "idle"
        client.post("/api/mock/bots", json={"preset": "ready"})
        ids = _ids(client)  # (a preset makes new bots: new ids)
        client.post("/api/control/start")
        for method, path, body in (
            ("put", f"/api/bots/{ids[1]}", {"name": "Bobby"}),
            ("post", f"/api/bots/{ids[1]}/avatar:regenerate", None),
            ("post", f"/api/bots/{ids[1]}/import-default", {"name": "Zed"}),
            ("put", "/api/moderator", {"avatar": {}}),
            ("post", "/api/moderator/avatar:regenerate", None),
            ("post", "/api/bots/avatars:regenerate", None),
        ):
            response = getattr(client, method)(path, **({"json": body} if body is not None else {}))
            assert response.status_code == 409 and response.json()["code"] == "game_active", (path, response.text)
        assert client.post(f"/api/bots/{ids[1]}/import", content=b"x").status_code == 409
        assert _detail(client, ids[1])["name"] == "Bob"
        # L0 and the memory may be edited in a game (the changes take effect with the next game / the bot's next mounting)
        assert client.put(f"/api/bots/{ids[1]}/memory/arch_notes", json={"content": "改一下"}).status_code == 200
        client.post("/api/control/pause")


def test_new_avatars_for_one_bot_for_everybody_who_has_not_learned_anything_and_for_the_moderator() -> None:
    app, c = _client(bots="mixed")
    with c as client:
        before = {b["id"]: b["avatar"] for b in _bots(client)["bots"]}
        out = client.post("/api/bots/avatars:regenerate").json()
        assert set(out) == {"bots", "skipped"}
        frozen = [b["id"] for b in out["bots"]["bots"] if b["frozen"]]
        assert set(out["skipped"]) == set(frozen) and frozen, "a frozen bot keeps its avatar (it would lose what it learned)"
        after = {b["id"]: b["avatar"] for b in out["bots"]["bots"]}
        assert all(after[i] == before[i] for i in out["skipped"]) and all(after[i] != before[i] for i in after if i not in out["skipped"])
        assert len({json.dumps(a, sort_keys=True) for a in after.values()}) == 12
        # one bot (not frozen): a new avatar, same id
        free = next(i for i in after if i not in out["skipped"])
        one = client.post(f"/api/bots/{free}/avatar:regenerate").json()
        assert one["id"] == free and one["avatar"] != after[free]
        # the moderator is no bot: an avatar is the only thing to choose
        moderator = client.post("/api/moderator/avatar:regenerate").json()
        assert moderator["id"] == "moderator" and mock.validate_avatar(moderator["avatar"]) == [] and moderator["avatar"] not in after.values()
        assert client.put("/api/moderator", json={"avatar": one["avatar"]}).json()["avatar"] == one["avatar"]
        for bad in ({"name": "X"}, {"avatar": one["avatar"], "persona": "x"}):
            assert client.put("/api/moderator", json=bad).status_code == 400
        assert client.put("/api/moderator", json={}).status_code == 400
        assert client.put("/api/moderator", json={"avatar": {"v": 9}}).status_code == 400
        assert client.get("/api/state").json()["session"]["moderator"]["avatar"] == one["avatar"]


# ================================================================================================= L0
def test_the_l0_of_a_bot_is_its_merged_file_and_a_bad_file_is_refused_with_every_problem() -> None:
    app, c = _client()
    with c as client:
        bot = _ids(client)[0]
        core = client.get(f"/api/bots/{bot}/core").json()
        assert set(core) == {"complete", "missing", "chars", "extras", "updated_at", "markdown", "sections"}
        assert core["markdown"].startswith("---\nformat: arena-core\nversion: 1\nname: Alice\n---\n# 身份\n") and core["markdown"].endswith("\n")
        assert core["sections"].keys() == {"identity", "voice", "mind"} and core["extras"] == [
            {"title": "口头禅", "slug": "x1", "chars": 13, "text": "「行，听你的，出了再说。」"}]
        assert _detail(client, bot)["core"] == core
        download = client.get(f"/api/bots/{bot}/core", params={"download": "1"})
        assert download.status_code == 200 and download.content.decode("utf-8") == core["markdown"]
        assert download.headers["content-type"] == "text/markdown; charset=utf-8"
        assert download.headers["content-disposition"] == "attachment; filename=\"Alice.core.md\"; filename*=UTF-8''Alice.core.md"
        _error(client.get("/api/bots/b-ffffffff/core"), 404, "not_found")
        # the merged file round-trips byte for byte (render(parse(x)) == x)
        parsed, hint, _ = mb.parse_core_markdown(core["markdown"])
        assert mb.render_core_markdown(parsed, hint) == core["markdown"]

        # a new file replaces L0; the name in it is only a hint: nothing is renamed, the response says so
        new = core["markdown"].replace("name: Alice", "name: Zoe").replace("你说话短", "你说话很短")
        out = client.put(f"/api/bots/{bot}/core", json={"markdown": new})
        assert out.status_code == 200 and out.json()["name"] == "Alice" and "你说话很短" in out.json()["core"]["sections"]["voice"]
        assert out.json()["notices"] == ["文件里的名字是「Zoe」，只作提示，没有改名（这个玩家仍叫「Alice」）。"]
        assert out.json()["init"]["status"] == "stale" and out.json()["init"]["stale"] == ["core"], "the hash of L0 changed: initialize again, the memory stays"
        assert out.json()["memory"]["arch_notes"], "the memory stays"
        again = client.put(f"/api/bots/{bot}/core", json={"markdown": core["markdown"]}).json()
        assert "notices" not in again and again["init"]["status"] == "ready", "the file of its own name: no notice; the same L0 as at initialization: ready again"

        # every problem at once, in Chinese, naming the section and the numbers; nothing is saved
        broken = "---\nformat: arena-core\nversion: 1\n---\n# 身份\n" + "字" * 301 + "\n# 说话方式\n" + "字" * 612 + "\n# 食谱\nx\n"
        response = client.put(f"/api/bots/{bot}/core", json={"markdown": broken})
        assert response.status_code == 400
        problems = response.json()["problems"]
        assert "「身份」最多 300 字（现在 301 字）。" in problems and "「说话方式」最多 600 字（现在 612 字）。" in problems
        assert any("不认识的分区标题「食谱」" in p for p in problems) and "缺少「分析方式」分区。" in problems
        assert response.json()["detail"].startswith("底层设定没有保存：共 4 处问题：")
        assert _detail(client, bot)["core"]["markdown"] == core["markdown"].replace("name: Alice", "name: Alice"), "nothing changed"
        for markdown, fragment in (
            ("没有文件头\n# 身份\nx", "缺少文件头"),
            ("---\nformat: other\nversion: 1\n---\n# 身份\nx", "format 必须是 arena-core"),
            ("---\nformat: arena-core\nversion: 2\n---\n# 身份\nx", "version 目前只能是 1"),
            ("---\nformat: arena-core\nversion: 1\n---\n# 身份\na\n# 身份\nb\n# 说话方式\nc\n# 分析方式\nd", "「身份」分区出现了 2 次"),
            ("---\nformat: arena-core\nversion: 1\n---\n# 身份\na\n# 说话方式\nb\n# 分析方式\n\n", "「分析方式」不能为空"),
            ("---\nformat: arena-core\nversion: 1\n---\n# 身份\na\n# 说话方式\nb\n# 分析方式\nc\n" + "".join(f"# 附加：{n}\nx\n" for n in "ABCD"), "附加分区最多 3 份"),
        ):
            r = client.put(f"/api/bots/{bot}/core", json={"markdown": markdown})
            assert r.status_code == 400 and any(fragment in p for p in r.json()["problems"]), (fragment, r.text)
        assert client.put(f"/api/bots/{bot}/core", json={}).status_code == 422
        assert client.put(f"/api/bots/{bot}/core", json={"markdown": "x", "y": 1}).status_code == 422


def test_clearing_l0_of_one_bot_or_all_of_them_keeps_the_memory_and_stops_the_game_from_starting() -> None:
    app, c = _client()
    with c as client:
        ids = _ids(client)
        out = client.delete(f"/api/bots/{ids[0]}/core").json()
        assert out["core"]["complete"] is False and out["core"]["missing"] == ["identity", "voice", "mind"] and out["core"]["extras"] == []
        assert out["init"]["status"] == "stale" and out["memory"]["arch_notes"], "initialized -> needs re-initialization; what it learned stays"
        payload = client.post("/api/bots/core:clear", json={"ids": [ids[1], ids[2]]}).json()
        assert [b["core"]["complete"] for b in payload["bots"][:4]] == [False, False, False, True]
        problem = next(p for p in payload["ready"]["problems"] if p["bot_id"] == ids[0])
        assert {i["code"] for i in problem["items"]} == {"core_missing", "stale"} and problem["items"][0]["fix"] == "import_core"
        r = client.post("/api/control/start")
        assert r.status_code == 409 and r.json()["code"] == "NOT_READY" and _game(app).session["dealt"] is False
        everyone = client.post("/api/bots/core:clear", json={}).json()
        assert [b["core"]["complete"] for b in everyone["bots"]] == [False] * 12
        assert client.post("/api/bots/core:clear", json={"ids": ids, "other": 1}).status_code == 422
        assert client.post("/api/bots/core:clear", json={"ids": ["b-ffffffff"]}).status_code == 404
        # the L0 can be imported again: complete, but it needs to be initialized again
        md = mb.render_core_markdown(mb.default_core("Alice"), "Alice")
        back = client.put(f"/api/bots/{ids[0]}/core", json={"markdown": md}).json()
        assert back["core"]["complete"] is True and back["init"]["status"] == "ready", "the same L0 again: the hash matches the initialization"


def test_the_moderators_model_drafts_an_l0_and_nothing_is_saved() -> None:
    app, c = _client()
    with c as client:
        bot = _ids(client)[2]
        before = _detail(client, bot)
        game_calls = len(_game(app).calls)
        out = client.post(f"/api/bots/{bot}/core:refine", json={"text": "脾气急，爱抢话\n一句话说完\n先看票型"}).json()
        assert set(out) == {"draft"}
        core, hint, _ = mb.parse_core_markdown(out["draft"])
        assert hint == "Carol" and "脾气急，爱抢话" in core["identity"], "the draft is a merged file, ready for PUT"
        assert _detail(client, bot) == before, "a draft is only a proposal: nothing is saved"
        line = _game(app).calls[game_calls]
        assert (line["purpose"], line["actor_id"], line["actor_name"], line["status"]) == ("refine", "roster", "名册", "ok")
        assert mock.validate_call(line) == []
        # an over-long draft is still returned, with the warnings (the page shows them, saving is refused by PUT)
        long = client.post(f"/api/bots/{bot}/core:refine", json={"text": "很" * 400}).json()
        assert "draft" in long and any("最多 300 字" in p for p in long["problems"])
        # why it cannot work: no model for the moderator / the model fails / the model did not answer in the format
        for mode, status, code, fragment in (("unbound", 400, None, "没有绑定模型"), ("model", 502, "model_failed", "调用主持人模型失败"), ("format", 400, "bad_draft", "没有按固定格式")):
            assert client.post("/api/mock/learning", json={"refine": mode}).json()["refine"] == mode
            r = client.post(f"/api/bots/{bot}/core:refine", json={"text": "要点"})
            assert r.status_code == status and fragment in r.json()["detail"] and r.json().get("code") == code, (mode, r.text)
        client.post("/api/mock/learning", json={"refine": None})
        assert client.post(f"/api/bots/{bot}/core:refine", json={"text": "要点"}).status_code == 200
        assert client.post(f"/api/bots/{bot}/core:refine", json={"text": "   "}).status_code == 400
        assert client.post(f"/api/bots/{bot}/core:refine", json={"text": "x" * 6001}).status_code == 400
        assert client.post(f"/api/bots/{bot}/core:refine", json={}).status_code == 422
        assert client.post("/api/mock/learning", json={"refine": "weird"}).status_code == 422


# ================================================================================================= memory
def test_the_user_edits_the_memory_parts_and_the_edit_takes_effect_at_once() -> None:
    app, c = _client()
    with c as client:
        bot = _ids(client)[0]
        sid = mb.DEFAULT_SCENARIO_ID
        data = client.put(f"/api/bots/{bot}/memory/arch_notes", json={"content": "  改过的笔记\r\n第二行 "}).json()
        assert data["memory"]["arch_notes"] == "改过的笔记\n第二行" and data["init"]["status"] == "ready", "C9: an edit does not touch the initialization"
        for part, content in {"digest": "改过的要点", "questions": "改过的问题"}.items():
            data = client.put(f"/api/bots/{bot}/memory/{part}", json={"content": content, "scenario_id": sid}).json()
            assert data["memory"][part] == content and data["memory"]["scenario_id"] == sid
        data = client.put(
            f"/api/bots/{bot}/memory/playbook", json={"content": "- [把握:高|支持:4|反例:1|#夜晚] 改过的一条\n无关的话", "scenario_id": sid}
        ).json()
        assert data["memory"]["playbook"] == [{"text": "改过的一条", "confidence": "高", "support": 4, "counter": 1, "source": "", "tags": ["夜晚"]}]
        assert data["memory"]["playbook_md"] == "## 假设\n- [把握:高|支持:4|反例:1|#夜晚] 改过的一条"
        data = client.put(f"/api/bots/{bot}/memory/people", json={"content": "Bob:话少\n* Carol：话多"}).json()
        assert data["memory"]["people"] == {"Bob": "话少", "Carol": "话多"} and data["memory"]["people_md"] == "- Bob：话少\n- Carol：话多"
        assert data["memory_stats"]["people"] == 2 and data["memory_stats"]["playbook"] == 1 and data["memory_stats"]["arch_notes"] == len("改过的笔记\n第二行")
        # the playbook of another scenario is a file of its own
        other = client.put(f"/api/bots/{bot}/memory/digest", json={"content": "另一个剧本里的要点", "scenario_id": "demo"}).json()
        assert other["memory"]["scenario_id"] == "demo" and other["memory"]["digest"] == "另一个剧本里的要点"
        assert client.get(f"/api/bots/{bot}").json()["memory"]["digest"] == "改过的要点", "the current scenario is the default"
        assert client.get(f"/api/bots/{bot}", params={"scenario": "demo"}).json()["memory"]["digest"] == "另一个剧本里的要点"
        assert client.get(f"/api/bots/{bot}", params={"scenario": "../x"}).status_code == 400
        assert client.put(f"/api/bots/{bot}/memory/digest", json={"content": ""}).json()["memory"]["digest"] == "", "empty clears"
        # a tag the scenario does not know is no refusal; the page is told in the answer of the real server only
        assert client.put(f"/api/bots/{bot}/memory/playbook", json={"content": "- [把握:低|#没有这个标签] 一条", "scenario_id": sid}).status_code == 200


def test_a_memory_part_that_is_over_its_limit_or_not_understood_is_refused_and_nothing_changes() -> None:
    app, c = _client()
    with c as client:
        bot = _ids(client)[0]
        sid = mb.DEFAULT_SCENARIO_ID
        before = _detail(client, bot)["memory"]
        r = client.put(f"/api/bots/{bot}/memory/digest", json={"content": "字" * 812})
        assert r.status_code == 400 and r.json()["problems"] == ["「规则要点」最多 800 字（现在 812 字）。"]
        assert r.json()["detail"] == "没有保存：「规则要点」最多 800 字（现在 812 字）。"
        many = "".join(f"- [把握:低] 第 {i} 条\n" for i in range(21)) + "- [把握:低] " + "长" * 90
        problems = client.put(f"/api/bots/{bot}/memory/playbook", json={"content": many, "scenario_id": sid}).json()["problems"]
        assert problems[0] == "打法最多 20 条（现在 22 条）。" and problems[1].startswith("打法第 22 条最多 80 字（现在 90 字）：") and len(problems) == 2
        assert client.put(f"/api/bots/{bot}/memory/playbook", json={"content": "只是一段话"}).json()["problems"][0].startswith("没有读出任何一条打法")
        assert client.put(f"/api/bots/{bot}/memory/people", json={"content": "没有冒号"}).json()["problems"][0].startswith("没有读出任何一条印象")
        too_many = "\n".join(f"- P{i}：x" for i in range(31))
        assert client.put(f"/api/bots/{bot}/memory/people", json={"content": too_many}).json()["problems"] == ["印象最多 30 人（现在 31 人）。"]
        assert "对「Bob」的印象最多 80 字（现在 81 字）。" in client.put(f"/api/bots/{bot}/memory/people", json={"content": "- Bob：" + "x" * 81}).json()["problems"]
        for part in ("nonsense", "record"):
            _error(client.put(f"/api/bots/{bot}/memory/{part}", json={"content": "x"}), 404, "not_found")  # (the record is the engine's)
        assert client.put(f"/api/bots/{bot}/memory/digest", json={"content": "x", "scenario_id": "../x"}).status_code == 400
        _error(client.put("/api/bots/b-ffffffff/memory/digest", json={"content": "x"}), 404, "not_found")
        assert client.put(f"/api/bots/{bot}/memory/digest", json={}).status_code == 422
        assert client.put(f"/api/bots/{bot}/memory/digest", json={"content": 5}).status_code == 422
        assert _detail(client, bot)["memory"] == before


def test_what_a_bot_learned_can_be_cleared_part_by_part_or_all_at_once() -> None:
    app, c = _client()
    with c as client:
        ids = _ids(client)
        bot = ids[0]
        data = client.delete(f"/api/bots/{bot}/memory", params={"part": "people"}).json()
        assert data["memory"]["people"] == {} and data["memory"]["arch_notes"] and data["init"]["status"] == "ready", "one part: the rest stays"
        data = client.delete(f"/api/bots/{bot}/memory", params={"part": "playbook"}).json()
        assert data["memory"]["playbook"] == [] and data["init"]["status"] == "ready"
        assert client.delete(f"/api/bots/{bot}/memory", params={"part": "nonsense"}).status_code == 400
        # all of it: the initialization goes with it (the status is uninitialized), L0 stays, the proposals stay
        data = client.delete(f"/api/bots/{bot}/memory").json()
        assert data["init"]["status"] == "uninitialized" and data["frozen"] is False and data["memory"]["arch_notes"] == "" and data["memory"]["record"] == ""
        assert data["core"]["complete"] is True
        # memory:clear for some / all bots
        out = client.post("/api/bots/memory:clear", json={"ids": [ids[1]]}).json()
        assert [b["init"]["status"] for b in out["bots"][:3]] == ["uninitialized", "uninitialized", "ready"]
        everyone = client.post("/api/bots/memory:clear", json={}).json()
        assert [b["init"]["status"] for b in everyone["bots"]] == ["uninitialized"] * 12 and everyone["ready"]["ok"] is False
        assert all(b["memory_stats"]["arch_notes"] == 0 and b["memory_stats"]["record"] == "" for b in everyone["bots"]) and all(b["core"]["complete"] for b in everyone["bots"])
        assert client.post("/api/bots/memory:clear", json={"ids": ids, "x": 1}).status_code == 422


# ================================================================================================= proposals
def test_a_proposal_of_a_review_is_accepted_or_rejected_by_the_user() -> None:
    app, c = _client()
    with c as client:
        bot = _ids(client)[0]
        stores = app.state.mock_stores
        voice = _detail(client, bot)["core"]["sections"]["voice"]
        p1 = stores.bots.stage_core(bot, "voice", voice + "\n「先说结论。」", "接管者说话短", "复盘 g1")
        p2 = stores.bots.stage_core(bot, "extra:口头禅", "「新口头禅。」", "试试", "复盘 g1")
        p3 = stores.bots.stage_core(bot, "extra:新分区", "新内容", "新增一个分区", "复盘 g1")
        detail = _detail(client, bot)
        assert [(p["section"], p["before"] == voice if p["section"] == "voice" else True) for p in detail["staging"]][:1] == [("voice", True)]
        assert detail["staging"][1]["section"] == "extra:x1" and detail["staging"][1]["before"] == "「行，听你的，出了再说。」", "an extra is named by its slug"
        assert detail["staging"][2] == {**detail["staging"][2], "section": "extra:新分区", "before": ""}
        assert set(detail["staging"][0]) == {"id", "section", "before", "after", "reason", "source", "created_at"} and _bots(client)["bots"][0]["staging"] == 3
        # accept: written into L0, the hash changes, the status is stale; the proposal is gone
        out = client.post(f"/api/bots/{bot}/staging/{p1}:accept").json()
        assert out["core"]["sections"]["voice"].endswith("「先说结论。」") and out["init"]["status"] == "stale" and len(out["staging"]) == 2
        # reject: gone, L0 unchanged
        rejected = client.post(f"/api/bots/{bot}/staging/{p2}:reject").json()
        assert [p["id"] for p in rejected["staging"]] == [p3] and rejected["core"]["extras"][0]["text"] == "「行，听你的，出了再说。」"
        # a proposal that cannot be written (a fourth extra) is refused with the problems and stays on the list
        md = mb.render_core_markdown({**mb.default_core("Alice"), "extras": [("甲", "a"), ("乙", "b"), ("丙", "c")]}, "Alice")
        client.put(f"/api/bots/{bot}/core", json={"markdown": md})
        r = client.post(f"/api/bots/{bot}/staging/{p3}:accept")
        assert r.status_code == 400 and r.json()["problems"] == ["附加分区最多 3 份，已经满了，不能再新增「新分区」。"]
        assert [p["id"] for p in _detail(client, bot)["staging"]] == [p3]
        _error(client.post(f"/api/bots/{bot}/staging/p-00000000:accept"), 404, "not_found")
        _error(client.post(f"/api/bots/{bot}/staging/p-00000000:reject"), 404, "not_found")
        assert client.post(f"/api/bots/{bot}/staging/{p3}:explode").status_code == 404
        # a proposal over a limit never gets that far
        with pytest.raises(mb.FileProblems):
            stores.bots.stage_core(bot, "voice", "字" * 601, "x", "y")


# ================================================================================================= .arenabot
def test_a_bot_is_exported_as_an_arenabot_file_and_imported_over_another_bot() -> None:
    app, c = _client(bots="fresh")
    with c as client:
        ids = _ids(client)
        alice, bob = ids[0], ids[1]
        client.put(f"/api/bots/{alice}", json={"name": "小明"})
        response = client.get(f"/api/bots/{alice}/export")
        assert response.status_code == 200 and response.headers["content-type"] == "application/octet-stream"
        disposition = response.headers["content-disposition"]
        assert disposition.startswith("attachment; filename=\"") and ".arenabot\"" in disposition and "filename*=UTF-8''%E5%B0%8F%E6%98%8E-" in disposition
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            manifest = json.loads(z.read("manifest.json"))
            names = set(z.namelist())
        assert manifest["format"] == "arenabot" and manifest["format_version"] == 1 and manifest["bot"] == {"name": "小明", "origin": "migrated"}
        assert {"bot/bot.json", "bot/core/identity.md", "bot/core/voice.md", "bot/core/mind.md", "bot/core/extra-x1.md"} <= names
        assert all(f["path"] in names and f["sha256"] and f["size"] for f in manifest["files"])
        # import it over Bob: everything of Bob is replaced (name and avatar too); a name another bot has is refused (409 name_taken)
        client.put(f"/api/bots/{alice}", json={"name": "Alice"})
        taken = client.post(f"/api/bots/{bob}/import", content=response.content, headers={"Content-Type": "application/octet-stream"})
        assert taken.status_code == 200, "the name of the file is 小明: nobody has it any more"
        assert taken.json()["id"] == bob and taken.json()["name"] == "小明" and taken.json()["avatar"] and taken.json()["core"]["complete"]
        again = client.get(f"/api/bots/{alice}/export").content
        _error(client.post(f"/api/bots/{bob}/import", content=again), 409, "name_taken")  # (Alice is Alice: Bob's file would have two)
        assert client.post(f"/api/bots/{alice}/import", content=again).status_code == 200, "its own name is no conflict"
        # what cannot be imported: not a zip, a file that was changed, a manifest of another format - 422 with the reasons; nothing changes
        before = _detail(client, bob)
        for data, fragment in ((b"hello", "不是 zip"), (b"", "没有收到"),):
            r = client.post(f"/api/bots/{bob}/import", content=data)
            assert r.status_code in (400, 422) and fragment in r.json()["detail"] or fragment in "".join(r.json().get("problems") or []), (data, r.text)
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(response.content)) as src, zipfile.ZipFile(buf, "w") as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                dst.writestr(item.filename, data.replace("你说话".encode("utf-8"), "你乱说".encode("utf-8")) if item.filename.endswith("voice.md") else data)
        tampered = client.post(f"/api/bots/{bob}/import", content=buf.getvalue())
        assert tampered.status_code == 422 and any("SHA-256" in p for p in tampered.json()["problems"])
        assert _detail(client, bob) == before
        _error(client.post("/api/bots/b-ffffffff/import", content=again), 404, "not_found")


def test_an_arenabot_file_over_the_limit_is_refused_before_it_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    app, c = _client(bots="fresh")
    with c as client:
        monkeypatch.setattr(mb, "MAX_ARENABOT_BYTES", 500)
        bot = _ids(client)[1]
        body = _error(client.post(f"/api/bots/{bot}/import", content=b"x" * 501), 413, "too_large")
        assert "20 MiB" in body["detail"]
        assert client.post(f"/api/bots/{bot}/import", content=b"x" * 400).status_code == 422, "small enough to be read - and no zip"


def test_a_bot_of_the_default_library_replaces_a_bot_and_comes_initialized() -> None:
    app, c = _client(bots="fresh")
    with c as client:
        ids = _ids(client)
        _error(client.post(f"/api/bots/{ids[0]}/import-default", json={"name": "Zed"}), 404, "not_found")
        _error(client.post(f"/api/bots/{ids[0]}/import-default", json={"name": "bob"}), 409, "name_taken")  # (Bob sits at N2; any case)
        assert client.post(f"/api/bots/{ids[0]}/import-default", json={}).status_code == 422
        assert client.post(f"/api/bots/{ids[0]}/import-default", json={"name": "Bob", "x": 1}).status_code == 422
        client.put(f"/api/bots/{ids[1]}", json={"name": "Bobby"})
        out = client.post(f"/api/bots/{ids[0]}/import-default", json={"name": "Bob"}).json()
        assert out["id"] == ids[0] and out["name"] == "Bob" and out["origin"] == "default" and out["core"]["complete"] is True
        assert out["init"]["status"] == "ready" and out["memory"]["playbook"], "a library bot comes initialized under the contract and rules the program ships"
        assert out["core"]["sections"]["voice"] != mb.default_core("Alice")["voice"]
        assert [b["name"] for b in _bots(client)["bots"]][:2] == ["Bob", "Bobby"]
        assert _valid(app) == []


# ================================================================================================= initialization
def _run_init(client, ids=None, **body):
    r = client.post("/api/bots/init", json={**({"ids": ids} if ids is not None else {}), **body})
    assert r.status_code == 202, r.text
    return r.json()


def test_an_initialization_is_a_job_with_one_progress_event_per_bot_and_second() -> None:
    app, c = _client(bots="mixed")
    with c as client:
        before = _game(app).bus.latest_id if "game" in app.state.mock_holder else 0
        job = _run_init(client)
        assert set(job) == {"job_id", "ids", "deadline_s"} and job["deadline_s"] == 120
        ids = _ids(client)
        assert job["ids"] == [ids[1], ids[2], ids[3], ids[4], ids[5], ids[6]], "the default: what is not initialized / stale / failed, in seat order"
        _wait_for(lambda: _no_job(client))
        events = _events(app, "bot.init.progress", after=before)
        assert events and [p for e in events for p in mock.validate_event(e)] == []
        one = [e["data"] for e in events if e["data"]["bot_id"] == ids[3]]
        states = [d["state"] for d in one]
        assert states[0] == "queued" and states[-1] == "done" and "running" in states and set(states) <= {"queued", "running", "done"}
        assert all(d["job_id"] == job["job_id"] and d["name"] == "David" and d["deadline_s"] == 120 for d in one)
        left = [d["seconds_left"] for d in one if d["state"] == "running"]
        assert left and left == sorted(left, reverse=True) and left[0] <= 120 and one[0]["seconds_left"] is None
        written = [d["files_written"] for d in one]
        assert written[0] == [] and written[-1] == ["arch_notes", "digest", "playbook", "questions"] and all(len(a) <= len(b) for a, b in zip(written, written[1:]))
        assert all(a == b[: len(a)] for a, b in zip(written, written[1:])), "the files come in the order they are written"
        payload = _bots(client)
        # (N6 has no L0 at all: its initialization fails at once and says what to do; the others are ready)
        assert [b["init"]["status"] for b in payload["bots"]] == ["ready"] * 5 + ["failed"] + ["ready"] * 6
        assert payload["bots"][5]["init"]["error"].startswith("底层设定不完整：缺少「身份」") and payload["ready"]["ok"] is False
        failed = [e["data"] for e in events if e["data"]["bot_id"] == ids[5]][-1]
        assert failed["state"] == "failed" and failed["error"] == payload["bots"][5]["init"]["error"] and failed["files_written"] == []
        # the bots were told about, and what they cost is shown (H24: not part of any game's budget)
        assert _events(app, "bots.updated", after=before) and payload["learning_usage"]["calls"] >= 15 and payload["learning_usage"]["tokens"] > 0
        calls = [c_ for c_ in _game(app).calls if c_["purpose"] == "init"]
        assert calls and all(c_["actor_id"] in ids and c_["actor_name"] in NAMES and c_["status"] == "ok" for c_ in calls) and all(mock.validate_call(c_) == [] for c_ in calls)
        assert client.get("/api/state").json()["session"]["system_status"] == {"code": "uninitialized", "text": "未初始化", "reason": ""}
        assert _valid(app, before) == []
        # the bots that succeeded are initialized under the current hashes: nothing stale, ready for a game
        client.put(f"/api/bots/{ids[5]}/core", json={"markdown": mb.render_core_markdown(mb.default_core("Frank"), "Frank")})
        _run_init(client)
        _wait_for(lambda: _no_job(client))
        assert _bots(client)["ready"] == {"ok": True, "problems": []}
        assert client.get("/api/state").json()["session"]["system_status"]["code"] == "before_game"


def test_while_a_bot_is_being_initialized_it_cannot_be_changed_and_a_second_job_is_refused() -> None:
    app, c = _client(bots="fresh", speed=1.0)  # real time: the job (a tick = 50 ms, 2 simulated seconds each) takes a second or two
    with c as client:
        client.post("/api/mock/learning", json={"sim_rate": 2.0})
        app.state.mock_hub.tick = 0.05
        ids = _ids(client)
        job = _run_init(client, [ids[0], ids[1]])
        assert [d for d in client.get("/api/bots").json()["init_job"]["ids"]] == job["ids"]
        assert client.get("/api/bots").json()["bots"][0]["init"]["status"] == "initializing"
        status = client.get("/api/state").json()["session"]["system_status"]
        assert status == {"code": "initializing", "text": "初始化中", "reason": ""}
        # the bots of the job: 409 init_busy for every write (names, L0, memory, clearing, import, proposals); another bot is fine
        for method, path, body in (
            ("put", f"/api/bots/{ids[0]}", {"name": "Z"}),
            ("put", f"/api/bots/{ids[0]}/core", {"markdown": "x"}),
            ("delete", f"/api/bots/{ids[0]}/core", None),
            ("put", f"/api/bots/{ids[0]}/memory/arch_notes", {"content": "x"}),
            ("delete", f"/api/bots/{ids[0]}/memory", None),
            ("post", "/api/bots/memory:clear", {"ids": [ids[1]]}),
            ("post", "/api/bots/core:clear", {}),
            ("post", f"/api/bots/{ids[0]}/import-default", {"name": "Zed"}),
        ):
            response = getattr(client, method)(path, **({"json": body} if body is not None else {}))
            assert response.status_code == 409 and response.json()["code"] == "init_busy", (method, path, response.text)
        assert client.put(f"/api/bots/{ids[2]}", json={"name": "Carolyn"}).status_code == 200
        assert client.post("/api/bots/memory:clear", json={"ids": [ids[2]]}).status_code == 200
        _error(client.post("/api/bots/init", json={}), 409, "init_busy")
        r = client.post("/api/control/start")
        assert r.status_code == 409 and r.json()["code"] == "NOT_READY" and any(i["code"] == "init_running" for p in r.json()["problems"] for i in p["items"])
        # cancel: every bot goes back to what it was (nothing was written), the job is gone
        assert client.post("/api/bots/init:cancel").json() == {"ok": True}
        _wait_for(lambda: _no_job(client))
        assert [b["init"]["status"] for b in _bots(client)["bots"][:2]] == ["uninitialized", "uninitialized"]
        cancelled = [e["data"] for e in _events(app, "bot.init.progress") if e["data"]["state"] == "cancelled"]
        assert {d["bot_id"] for d in cancelled} == set(job["ids"]) and all(d["seconds_left"] is None and d["error"] is None for d in cancelled)
        assert client.post("/api/bots/init:cancel").json() == {"ok": True}, "nothing to cancel is fine"
        assert _valid(app) == []


def test_an_initialization_is_refused_during_a_game_without_a_model_and_for_nobody() -> None:
    app, c = _client(bots="fresh", speed=1.0)  # real time: the game is still running when the next request arrives
    with c as client:
        ids = _ids(client)
        # no binding for N2: the whole request is refused and names the bot (409 no_binding with ``problems``)
        bindings = client.get("/api/settings/bindings").json()["bindings"]
        bindings.pop("N2")
        client.put("/api/settings/bindings", json={"bindings": bindings})
        assert _bots(client)["bots"][1]["binding"] == {"ok": False, "label": "未绑定模型"}
        body = _error(client.post("/api/bots/init", json={}), 409, "no_binding")
        assert "Bob" in body["detail"] and body["problems"][0]["items"][0] == {"code": "no_binding", "message": "还没有给这个玩家绑定可用的模型。", "fix": "bind_model"}
        assert body["problems"][0]["bot_id"] == ids[1] and body["problems"][0]["seat"] == "N2"
        assert _bots(client)["init_job"] is None
        problems = _bots(client)["ready"]["problems"]
        assert any(p["scope"] == "binding" and p["seat"] == "N2" and p["items"][0]["fix"] == "bind_model" for p in problems)
        # nobody to initialize / a mistake in the request
        client.post("/api/mock/bots", json={"preset": "ready"})
        assert client.post("/api/bots/init", json={}).status_code == 400
        assert client.post("/api/bots/init", json={"ids": "all"}).status_code == 422
        assert client.post("/api/bots/init", json={"rebuild": "yes"}).status_code == 422
        assert client.post("/api/bots/init", json={"x": 1}).status_code == 422
        _error(client.post("/api/bots/init", json={"ids": ["b-ffffffff"]}), 404, "not_found")
        # a game that is running or paused: not now
        client.put("/api/settings/bindings", json={"bindings": client.get("/api/settings/bindings").json()["bindings"] | {"N2": bindings["N1"]}})
        assert client.post("/api/control/start").status_code == 200
        _error(client.post("/api/bots/init", json={"rebuild": True}), 409, "game_active")
        client.post("/api/control/pause")
        _error(client.post("/api/bots/init", json={"rebuild": True}), 409, "game_active")


def test_rebuild_initializes_all_twelve_again_and_revises_what_a_bot_learned() -> None:
    app, c = _client()
    with c as client:
        ids = _ids(client)
        client.put(f"/api/bots/{ids[0]}/memory/playbook", json={"content": "- [把握:高|支持:9|#夜晚] 我自己写的一条", "scenario_id": mb.DEFAULT_SCENARIO_ID})
        client.put(f"/api/bots/{ids[0]}/memory/people", json={"content": "- Bob：话少"})
        job = _run_init(client, rebuild=True)
        assert job["ids"] == ids
        _wait_for(lambda: _no_job(client), timeout=60)
        alice = _detail(client, ids[0])
        texts = [e["text"] for e in alice["memory"]["playbook"]]
        assert "我自己写的一条" in texts and len(texts) > 1, "a revision, not an overwrite: what the bot knew stays, new entries are added"
        assert alice["memory"]["people"] == {"Bob": "话少"} and alice["init"]["status"] == "ready"
        assert [b["init"]["status"] for b in _bots(client)["bots"]] == ["ready"] * 12


@pytest.mark.parametrize(
    "plan,state,fragment,files",
    [
        ("failed", "failed", "模型调用失败：HTTP 429", []),
        ("invalid", "failed", "缺少必需的记忆文件：打法", ["arch_notes", "digest"]),
        ("timeout", "timeout", "超过 120 秒", []),
    ],
)
def test_an_initialization_can_fail_or_run_out_of_time_and_the_old_files_stay(plan: str, state: str, fragment: str, files: list) -> None:
    app, c = _client(bots="fresh")
    with c as client:
        ids = _ids(client)
        client.put(f"/api/bots/{ids[1]}/memory/arch_notes", json={"content": "我原来的笔记"})
        client.post("/api/mock/learning", json={"init": {"Bob": plan}, "sim_rate": 30.0})
        _run_init(client, [ids[0], ids[1]])
        _wait_for(lambda: _no_job(client), timeout=40)
        bob = _detail(client, ids[1])
        assert bob["init"]["status"] == "failed" and fragment in bob["init"]["error"] and bob["init"]["error"].endswith("原来的文件没有改动。")
        assert bob["memory"]["arch_notes"] == "我原来的笔记" and bob["memory"]["digest"] == "", "all or nothing: the old files are as they were"
        last = [e["data"] for e in _events(app, "bot.init.progress") if e["data"]["bot_id"] == ids[1]][-1]
        assert (last["state"], last["files_written"]) == (state, files) and fragment in last["error"]
        assert last["seconds_left"] == (0 if state == "timeout" else last["seconds_left"])
        assert _detail(client, ids[0])["init"]["status"] == "ready", "the other bots are not affected"
        problem = next(p for p in _bots(client)["ready"]["problems"] if p["bot_id"] == ids[1])
        assert problem["items"][0]["code"] == "init_failed" and fragment in problem["items"][0]["message"] and problem["items"][0]["fix"] == "init"
        assert _valid(app) == []
        # try again: this time it works
        client.post("/api/mock/learning", json={"init": {}})
        _run_init(client, [ids[1]])
        _wait_for(lambda: _no_job(client), timeout=40)
        assert _detail(client, ids[1])["init"]["status"] == "ready" and _detail(client, ids[1])["init"]["error"] is None
        assert client.post("/api/mock/learning", json={"init": {"Bob": "explode"}}).status_code == 422


def test_a_slow_initialization_that_makes_it_in_time_ends_with_few_seconds_left() -> None:
    app, c = _client(bots="fresh")
    with c as client:
        ids = _ids(client)
        client.post("/api/mock/learning", json={"init": {"Alice": "slow"}, "sim_rate": 20.0})
        _run_init(client, [ids[0]])
        _wait_for(lambda: _no_job(client), timeout=40)
        events = [e["data"] for e in _events(app, "bot.init.progress")]
        assert events[-1]["state"] == "done" and events[-1]["seconds_left"] <= 10, "110 of the 120 seconds"
        assert _detail(client, ids[0])["init"]["duration_s"] and _detail(client, ids[0])["init"]["status"] == "ready"


# ================================================================================================= the start check
def test_a_game_that_is_not_ready_is_not_started_and_nothing_is_dealt() -> None:
    app, c = _client(bots="mixed")
    with c as client:
        client.get("/api/state")
        r = client.post("/api/control/start")
        body = _error(r, 409, "NOT_READY")
        assert body["detail"].startswith("还没有准备好，不能开局：") and "（共 6 处）" in body["detail"]
        assert [p["scope"] for p in body["problems"]] == ["bot"] * 6 and [p["seat"] for p in body["problems"]] == ["N2", "N3", "N4", "N5", "N6", "N7"]
        assert [p for p in body["problems"] for _ in [mock.hubmod.validate_problem(p, "p")] if mock.hubmod.validate_problem(p, "p")] == []
        assert body["problems"] == _bots(client)["ready"]["problems"], "the same objects: the dialog and the status bar agree"
        session = client.get("/api/state").json()["session"]
        assert session["status"] == "idle" and session["dealt"] is False and all(p["tags"] == [] for p in session["participants"]), "H52: nothing dealt"
        assert not [e for e in _game(app).bus.ring if e["type"] in ("player.updated", "game.status")]


def test_the_contract_the_scenario_and_the_models_are_part_of_the_start_check_and_say_where_to_fix_them() -> None:
    app, c = _client()
    with c as client:
        client.get("/api/state")
        out = client.post("/api/mock/files", json={"contract": "missing", "scenario": "invalid"}).json()
        scopes = {p["scope"]: p for p in out["problems"]}
        assert out["problems"][0]["scope"] == "contract" and out["problems"][1]["scope"] == "scenario", "the contract, the scenario, then the players"
        assert scopes["contract"]["items"][0] == {"code": "contract_missing", "message": "官方契约是空的，请在设置里导入契约或恢复默认。", "fix": "import_contract"}
        assert scopes["scenario"]["items"][0]["code"] == "scenario_invalid" and scopes["scenario"]["items"][0]["fix"] == "import_rules"
        assert all(i["status"] == "stale" for i in [{"status": b["init"]["status"]} for b in _bots(client)["bots"]]), "the contract is gone: nothing matches its hash"
        r = client.post("/api/control/start")
        assert r.status_code == 409 and r.json()["code"] == "NOT_READY" and "官方契约" in r.json()["detail"] and "剧本" not in r.json()["detail"].split("；")[0]
        client.post("/api/mock/files", json={"contract": "invalid", "scenario": "missing"})
        scopes = {p["scope"]: p["items"][0] for p in _bots(client)["ready"]["problems"] if p["scope"] != "bot"}
        assert scopes["contract"]["code"] == "contract_invalid" and scopes["scenario"]["code"] == "scenario_missing"
        client.post("/api/mock/files", json={"contract": "ok", "scenario": "ok"})
        assert _bots(client)["ready"] == {"ok": True, "problems": []}, "the same contract and scenario again: the initializations match their hashes"
        # a model for nobody: the moderator's and a seat's bindings are part of the check
        bindings = client.get("/api/settings/bindings").json()["bindings"]
        client.put("/api/settings/bindings", json={"bindings": {k: v for k, v in bindings.items() if k not in ("moderator", "N4")}})
        problems = _bots(client)["ready"]["problems"]
        assert [(p["scope"], p["seat"], p["name"]) for p in problems] == [("binding", "N4", "David"), ("binding", None, "主持人")]
        assert all(p["items"][0]["code"] == "no_binding" and p["items"][0]["fix"] == "bind_model" for p in problems)
        assert client.post("/api/mock/files", json={"contract": "weird"}).status_code == 422
        client.put("/api/settings/bindings", json={"bindings": bindings})
        assert _bots(client)["ready"]["ok"] is True
        # the real start: dealt, one player.updated per seat, then running
        r = client.post("/api/control/start")
        assert r.status_code == 200 and r.json()["session"]["dealt"] is True and r.json()["session"]["system_status"]["code"] == "in_game"
        client.post("/api/control/pause")
        assert client.get("/api/state").json()["session"]["system_status"] == {"code": "paused", "text": "已暂停", "reason": "手动暂停"}


# ================================================================================================= the review
def _finish(client, app):
    client.get("/api/state")
    client.post("/api/mock/finish")
    assert client.get("/api/state").json()["session"]["review"]["available"] is True


def test_the_review_of_a_finished_game_counts_down_and_says_what_changed() -> None:
    app, c = _client()
    with c as client:
        _finish(client, app)
        ids = _ids(client)
        before = _game(app).bus.latest_id
        assert client.post("/api/game/review", json={"ids": ["N4", "N5"]}).json() == {"started": ["N4", "N5"], "deadline_s": 60}
        _wait_for(lambda: client.get("/api/state").json()["session"]["review"]["done"] == ["N4", "N5"])
        events = [e["data"] for e in _events(app, "review.progress", after=before)]
        for seat in ("N4", "N5"):
            mine = [d for d in events if d["id"] == seat]
            states = [d["state"] for d in mine]
            assert states[0] == "queued" and states[-1] == "done" and "running" in states
            assert all(d["bot_id"] == ids[int(seat[1:]) - 1] and d["deadline_s"] == 60 for d in mine)
            running = [d["seconds_left"] for d in mine if d["state"] == "running"]
            assert running == sorted(running, reverse=True) and running[0] <= 60 and mine[0]["seconds_left"] is None
            assert mine[-1]["message"] == "已更新印象、打法和战绩" and all(d["changes"] is None for d in mine[:-1])
            changes = mine[-1]["changes"]
            assert {f["file"] for f in changes["files"]} == {"people", "playbook", "questions"} and changes["proposals"] == 0
            assert all(f["before"] != f["after"] for f in changes["files"]), "only what changed is listed"
            people = next(f for f in changes["files"] if f["file"] == "people")
            assert len(mb.parse_people(people["after"])) == 11 and mb.parse_people(people["after"]).keys().isdisjoint({NAMES[int(seat[1:]) - 1]})
            assert not any(word in people["after"] for word in ("狼人", "预言家", "女巫", "平民")), "impressions are about style and habits, never identities"
        assert _events(app, "session.updated", after=before)[0]["data"]["system_status"] == {"code": "reviewing", "text": "复盘中", "reason": ""}
        assert _events(app, "session.updated", after=before)[0]["data"]["review"]["running"] is True
        done = client.get("/api/state").json()["session"]
        assert done["review"] == {"available": True, "done": ["N4", "N5"], "running": False, "supported": True, "deadline_s": 60, "seconds_left": None}
        assert done["system_status"]["code"] == "finished"
        # the record counts the game, and the bots got the changes
        bots = _bots(client)["bots"]
        assert bots[3]["memory_stats"]["people"] == 11 and bots[3]["memory_stats"]["record"].startswith("场次 ")
        assert bots[5]["memory_stats"]["people"] == 0, "the others were not reviewed"
        assert _valid(app, before) == []
        # a review is once per seat and game; a request that makes no sense is refused
        assert client.post("/api/game/review", json={"ids": ["N4"]}).status_code == 409
        assert client.post("/api/game/review", json={"ids": "N1"}).status_code == 422 and client.post("/api/game/review", json={"learn_style": {"N1": "yes"}}).status_code == 422
        purposes = [c_ for c_ in _game(app).calls if c_["purpose"] == "review"]
        assert {c_["actor_id"] for c_ in purposes} == {"N4", "N5"} and all(mock.validate_call(c_) == [] for c_ in purposes)


def test_a_review_that_runs_out_of_time_keeps_what_it_wrote_and_one_that_fails_writes_nothing() -> None:
    app, c = _client()
    with c as client:
        _finish(client, app)
        ids = _ids(client)
        client.post("/api/mock/learning", json={"review": {"N3": "timeout", "N6": "error"}, "sim_rate": 30.0})
        assert client.post("/api/game/review", json={"ids": ["N3", "N6", "N7"]}).status_code == 200
        _wait_for(lambda: not client.get("/api/state").json()["session"]["review"]["running"])
        events = [e["data"] for e in _events(app, "review.progress")]
        timeout = [d for d in events if d["id"] == "N3"][-1]
        assert timeout["state"] == "timeout" and timeout["seconds_left"] == 0 and "已经写入的部分保留" in timeout["message"]
        assert [f["file"] for f in timeout["changes"]["files"]] == ["people"], "what was written by then stays; the rest is not done"
        error = [d for d in events if d["id"] == "N6"][-1]
        assert error["state"] == "error" and error["changes"] is None and "没有写入任何内容" in error["message"]
        assert _bots(client)["bots"][5]["memory_stats"]["people"] == 0, "a failed review changes nothing"
        done = client.get("/api/state").json()["session"]["review"]["done"]
        assert done == ["N3", "N7"], "a timed-out review counts as done; a failed one can be asked for again"
        assert [p for e in _game(app).bus.ring for p in mock.validate_event(e)] == []
        client.post("/api/mock/learning", json={"review": {}})
        assert client.post("/api/game/review", json={"ids": ["N6"]}).json()["started"] == ["N6"]
        assert client.post("/api/mock/learning", json={"review": {"N1": "explode"}}).status_code == 422


def test_the_style_of_a_person_who_played_a_seat_is_offered_as_a_proposal_for_l0() -> None:
    app, c = _client()
    with c as client:
        _finish(client, app)
        ids = _ids(client)
        _game(app).human_seats.add("N3")  # (what taking a seat over does)
        r = client.post("/api/game/review", json={"ids": ["N3", "N4"], "learn_style": {"N3": True, "N4": True}})
        assert r.json()["started"] == ["N3", "N4"]
        _wait_for(lambda: client.get("/api/state").json()["session"]["review"]["done"] == ["N3", "N4"])
        done = {d["id"]: d for d in (e["data"] for e in _events(app, "review.progress")) if d["state"] == "done"}
        assert done["N3"]["changes"]["proposals"] == 1 and done["N4"]["changes"]["proposals"] == 0, "only the seat a person played has one"
        bot = _detail(client, ids[2])
        assert bot["staging"] and bot["staging"][0]["section"] == "voice" and bot["staging"][0]["source"].startswith("复盘 ") and bot["staging"][0]["after"].startswith(bot["core"]["sections"]["voice"])
        assert _bots(client)["bots"][2]["staging"] == 1 and _bots(client)["bots"][3]["staging"] == 0
        accepted = client.post(f"/api/bots/{ids[2]}/staging/{bot['staging'][0]['id']}:accept").json()
        assert "（从接管者的发言里学来的说法）" in accepted["core"]["sections"]["voice"] and accepted["staging"] == [] and accepted["init"]["status"] == "stale"


def test_the_review_is_not_available_before_the_end_and_a_new_game_starts_clean() -> None:
    app, c = _client()
    with c as client:
        started = client.post("/api/game/review", json={})
        assert started.status_code == 409 and "对局结束后" in started.json()["detail"]
        _finish(client, app)
        client.post("/api/game/review", json={"ids": ["N1"]})
        _wait_for(lambda: client.get("/api/state").json()["session"]["review"]["done"] == ["N1"])
        new = client.post("/api/control/new").json()["session"]
        assert new["review"] == {"available": False, "done": [], "running": False, "supported": True, "deadline_s": 60, "seconds_left": None}
        assert new["dealt"] is False and all(p["tags"] == [] for p in new["participants"]), "a new game: nothing dealt"
        assert _bots(client)["bots"][0]["memory_stats"]["people"] == 11, "the bots outlive a game: what a review taught them stays"
        assert new["bots"]["bots"][0]["memory_stats"]["people"] == 11 and mock.validate_session(new) == []


def test_the_bindings_api_speaks_in_seats_and_the_mock_keeps_them_by_bot() -> None:
    app, c = _client()
    with c as client:
        ids = _ids(client)
        hub = app.state.mock_hub
        binding = {"provider_id": "mock-deepseek", "model_id": "deepseek-chat", "reasoning_depth": "low", "context_window_tokens": 128000}
        client.put("/api/settings/bindings", json={"bindings": {**client.get("/api/settings/bindings").json()["bindings"], "N3": binding}})
        assert hub.bot_bindings[ids[2]] == binding, "H4: the binding belongs to the bot"
        client.put("/api/bots/lineup", json={"order": [ids[2], *[i for i in ids if i != ids[2]]]})
        bindings = client.get("/api/settings/bindings").json()["bindings"]
        assert bindings["N1"] == binding and "N3" in bindings and bindings["N3"] != binding, "the seat is the view: the bot took its model to seat 1"
        assert _bots(client)["bots"][0]["binding"] == {"ok": True, "label": "DeepSeek（模拟） / deepseek-chat"}
