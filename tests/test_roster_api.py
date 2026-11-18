"""v0.12.1 roster / memory / budget / review HTTP API (contracts 10.4 and 10.8), against a real orchestrator."""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import roster as roster_api
from app.main import app
from test_roster_runtime import IDS, TextLlm, end_at_once, finished_game, make_orch, play_with, wait_for

TOKEN = "s3cret-token-value"


@pytest.fixture
def orch(monkeypatch):
    orchestrator = make_orch(monkeypatch)
    monkeypatch.setattr(roster_api, "orchestrator", orchestrator)
    return orchestrator


@pytest.fixture
def client(orch):
    with TestClient(app) as c:
        yield c


def pack(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def detail(response) -> str:
    body = response.json()
    assert set(body) == {"detail"} and isinstance(body["detail"], str) and body["detail"]
    return body["detail"]


# ------------------------------------------------------------------------------------ the roster
def test_the_roster_has_the_seats_the_ui_edits(client):
    body = client.get("/api/roster").json()
    assert body["memory_enabled"] is True and body["editable"] is True
    assert [s["id"] for s in body["seats"]] == IDS and [s["name"] for s in body["seats"]] == ["Alice", "Bob", "Carol", "David"]
    first = body["seats"][0]
    assert set(first) == {"seat", "id", "name", "default_name", "avatar", "persona", "memory"} and first["seat"] == 1


def test_a_seat_can_be_renamed_and_the_answer_is_the_entry(client, orch):
    response = client.put("/api/roster/N2", json={"name": "小红"})
    assert response.status_code == 200 and response.json()["name"] == "小红" and response.json()["id"] == "N2"
    assert client.get("/api/roster").json()["seats"][1]["name"] == "小红"
    assert orch.public_session()["participants"][1]["name"] == "小红"


@pytest.mark.parametrize(
    "body,status,fragment",
    [
        ({"name": "alice"}, 400, "已被 N1 使用"),
        ({"name": ""}, 400, "不能为空"),
        ({"name": "x" * 40}, 400, "最多"),
        ({"surprise": 1}, 422, ""),
        ({"avatar": "not an object"}, 422, ""),
    ],
)
def test_bad_roster_edits_are_refused_with_a_plain_message(client, body, status, fragment):
    response = client.put("/api/roster/N2", json=body)
    assert response.status_code == status
    assert fragment in detail(response)


def test_an_unknown_seat_is_a_clear_error(client):
    assert "没有这个座位" in detail(client.put("/api/roster/N9", json={"persona": "x"}))
    assert client.put("/api/roster/N1", content=b"[]", headers={"content-type": "application/json"}).status_code == 422


def test_names_and_avatars_need_an_idle_game_but_persona_and_memory_do_not(client, orch, monkeypatch):
    play_with(orch, end_at_once("红队"))
    import asyncio

    async def go():
        await orch.start_or_resume()
        await wait_for(lambda: orch.session.status == "finished")

    # TestClient runs requests in its own loop: use a plain request path to move the game on instead
    orch.session.status = "paused"
    assert client.put("/api/roster/N1", json={"name": "Zed"}).status_code == 409
    assert "开始之前" in detail(client.put("/api/roster/N1", json={"name": "Zed"}))
    assert client.put("/api/roster/N1", json={"avatar": {"v": 1}}).status_code == 409
    ok = client.put("/api/roster/N1", json={"persona": "冷静", "memory": "## 经验与印象\n谨慎"})
    assert ok.status_code == 200 and ok.json()["persona"] == "冷静"
    assert client.get("/api/roster").json()["editable"] is False


def test_avatars_can_be_regenerated_only_when_a_library_exists(client):
    # until the parts library is installed the endpoints say so instead of failing
    for response in (client.post("/api/roster/avatars:regenerate"), client.post("/api/roster/N1/avatar:regenerate"), client.post("/api/roster/moderator/avatar:regenerate")):
        assert response.status_code in (200, 400)
        if response.status_code == 400:
            assert "头像素材库" in detail(response)


# ------------------------------------------------------------------------------------ the moderator's avatar
def test_the_roster_lists_the_moderator_with_an_avatar_of_its_own(client):
    body = client.get("/api/roster").json()
    moderator = body["moderator"]
    assert set(moderator) == {"id", "name", "avatar"} and moderator["id"] == "moderator" and moderator["name"] == "主持人"
    assert moderator["avatar"]["v"] == 1 and moderator["avatar"] not in [s["avatar"] for s in body["seats"]]
    assert [s["id"] for s in body["seats"]] == IDS  # the moderator is not a seat
    assert client.get("/api/state").json()["session"]["moderator"]["avatar"] == moderator["avatar"]


def test_the_moderators_avatar_can_be_regenerated_alone_or_with_everybody(client):
    before = client.get("/api/roster").json()
    one = client.post("/api/roster/moderator/avatar:regenerate")
    assert one.status_code == 200 and one.json()["id"] == "moderator" and one.json()["avatar"] != before["moderator"]["avatar"]
    mid = client.get("/api/roster").json()
    assert mid["moderator"]["avatar"] == one.json()["avatar"]
    assert [s["avatar"] for s in mid["seats"]] == [s["avatar"] for s in before["seats"]]  # the players were not touched

    everybody = client.post("/api/roster/avatars:regenerate")
    assert everybody.status_code == 200 and set(everybody.json()) == {"seats", "moderator"}
    assert [s["id"] for s in everybody.json()["seats"]] == IDS and everybody.json()["moderator"]["id"] == "moderator"
    assert everybody.json()["moderator"]["avatar"] != mid["moderator"]["avatar"]
    assert client.get("/api/roster").json()["moderator"] == everybody.json()["moderator"]


def test_the_moderator_can_be_given_a_chosen_avatar_but_nothing_else(client):
    from app import avatars

    chosen = avatars.make_description(0, [s["avatar"] for s in client.get("/api/roster").json()["seats"]], seed=11)
    put = client.put("/api/roster/moderator", json={"avatar": chosen})
    assert put.status_code == 200 and put.json() == {"id": "moderator", "name": "主持人", "avatar": chosen}
    for body in ({"name": "Boss"}, {"persona": "严厉"}, {"memory": "x"}):
        response = client.put("/api/roster/moderator", json=body)
        assert response.status_code == 400 and "只能更换头像" in detail(response)
    bad = client.put("/api/roster/moderator", json={"avatar": {"v": 1, "face": "nope"}})
    assert bad.status_code == 400 and detail(bad)
    refine = client.post("/api/roster/moderator/refine", json={"kind": "persona", "text": "x"})
    assert refine.status_code == 400 and "主持人没有性格设定" in detail(refine)
    assert client.get("/api/roster").json()["moderator"]["avatar"] == chosen


def test_the_moderators_avatar_is_idle_only_like_the_players(client, orch):
    orch.session.status = "paused"
    for response in (client.post("/api/roster/moderator/avatar:regenerate"), client.post("/api/roster/avatars:regenerate")):
        assert response.status_code == 409 and "开始之前" in detail(response)


# ------------------------------------------------------------------------------------ memory
def test_the_memory_pack_exports_imports_previews_and_clears(client):
    client.put("/api/roster/N1", json={"persona": "性格一", "memory": "## 经验与印象\n记忆一"})
    exported = client.get("/api/roster/memory:export")
    assert exported.status_code == 200 and exported.headers["content-type"] == "application/zip"
    assert 'filename="arena-memory.zip"' in exported.headers["content-disposition"] and exported.headers["cache-control"] == "no-store"
    assert sorted(zipfile.ZipFile(io.BytesIO(exported.content)).namelist()) == ["N1.memory.md", "N1.persona.md"]

    cleared = client.post("/api/roster/memory:clear", json={"include_persona": True})
    assert cleared.status_code == 200 and cleared.json()["seats"][0]["memory"] == "" and cleared.json()["seats"][0]["persona"] == ""

    dry = client.post("/api/roster/memory:import?dry_run=true", content=exported.content, headers={"content-type": "application/zip"})
    assert dry.status_code == 200 and dry.json() == {"dry_run": True, "changes": [{"id": "N1", "name": "Alice", "persona": True, "memory": True}]}
    assert client.get("/api/roster").json()["seats"][0]["memory"] == ""  # a preview changes nothing
    done = client.post("/api/roster/memory:import", content=exported.content, headers={"content-type": "application/zip"})
    assert done.status_code == 200 and done.json()["dry_run"] is False and done.json()["changes"][0]["id"] == "N1"
    assert "记忆一" in client.get("/api/roster").json()["seats"][0]["memory"]


def test_bad_memory_packs_are_refused_and_change_nothing(client):
    client.put("/api/roster/N1", json={"memory": "原样"})
    for data in (b"garbage", b"", pack({"evil.txt": "x"}), pack({"../N1.memory.md": "x"})):
        response = client.post("/api/roster/memory:import", content=data, headers={"content-type": "application/zip"})
        assert response.status_code == 400 and detail(response)
    huge = client.post("/api/roster/memory:import", content=b"x" * (2 * 1024 * 1024 + 10), headers={"content-type": "application/zip"})
    assert huge.status_code == 413
    assert client.get("/api/roster").json()["seats"][0]["memory"] == "原样"


def test_the_memory_switch_is_saved(client):
    assert client.put("/api/roster/memory-enabled", json={"enabled": False}).json()["memory_enabled"] is False
    assert client.get("/api/roster").json()["memory_enabled"] is False
    assert client.put("/api/roster/memory-enabled", json={"enabled": "maybe"}).status_code == 422


# ------------------------------------------------------------------------------------ refine
def test_refine_returns_a_draft_and_never_saves(client, orch):
    orch.llm_port_factory = lambda o: TextLlm("你说话缓慢而坚定。")
    response = client.post("/api/roster/N1/refine", json={"kind": "persona", "text": "说话缓慢"})
    assert response.status_code == 200 and response.json() == {"draft": "你说话缓慢而坚定。"}
    assert client.get("/api/roster").json()["seats"][0]["persona"] == ""


def test_refine_errors_are_plain_and_never_leak_the_upstream(client, orch):
    orch.llm_port_factory = lambda o: TextLlm(fail_for=("moderator",))
    response = client.post("/api/roster/N1/refine", json={"kind": "persona", "text": "x"})
    assert response.status_code == 502 and "sk-secret" not in response.text and "主持人模型" in detail(response)
    assert client.post("/api/roster/N1/refine", json={"kind": "persona", "text": " "}).status_code == 400
    assert client.post("/api/roster/N1/refine", json={"kind": "nope", "text": "x"}).status_code == 400


# ------------------------------------------------------------------------------------ budget
def test_the_budget_can_be_read_and_set(client):
    assert client.get("/api/budget").json() == {
        "max_tokens": None, "max_cost_usd": None, "force_stop": False, "used_tokens": 0, "used_cost_usd": 0.0, "progress": 0.0, "state": "ok",
    }
    put = client.put("/api/budget", json={"max_tokens": 200000, "max_cost_usd": 3, "force_stop": True})
    assert put.status_code == 200 and put.json()["max_tokens"] == 200000 and put.json()["force_stop"] is True
    assert client.get("/api/budget").json()["max_cost_usd"] == 3.0
    cleared = client.put("/api/budget", json={"max_tokens": None, "max_cost_usd": "", "force_stop": False})
    assert cleared.json()["max_tokens"] is None and cleared.json()["max_cost_usd"] is None


@pytest.mark.parametrize("body", [{"max_tokens": -5}, {"max_cost_usd": "lots"}, {"force_stop": "maybe"}, {"unknown": 1}, {"max_tokens": 10**13}])
def test_a_bad_budget_is_a_422_with_a_plain_message(client, body):
    response = client.put("/api/budget", json=body)
    assert response.status_code == 422 and "预算设置无效" in detail(response)
    assert client.get("/api/budget").json()["max_tokens"] is None


def test_a_budget_body_must_be_an_object(client):
    for content in (b"[]", b"7", b"not json"):
        response = client.put("/api/budget", content=content, headers={"content-type": "application/json"})
        assert response.status_code == 400 and "JSON 对象" in detail(response)


# ------------------------------------------------------------------------------------ review
def test_a_review_is_refused_until_the_game_is_over(client):
    response = client.post("/api/game/review", json={})
    assert response.status_code == 409 and "对局结束后" in detail(response)


def test_a_finished_game_can_be_reviewed_through_the_api(monkeypatch):
    orch = make_orch(monkeypatch)
    monkeypatch.setattr(roster_api, "orchestrator", orch)
    import asyncio

    async def scenario():
        await finished_game(orch)
        orch.llm_port_factory = lambda o: TextLlm("API 复盘")
        started = await orch.start_review(["N1"])
        await wait_for(lambda: "N1" in orch.session.review_done)
        return started

    assert asyncio.run(scenario()) == ["N1"]
    with TestClient(app) as client:
        again = client.post("/api/game/review", json={"ids": ["N1"]})
        assert again.status_code == 200 and again.json() == {"started": []}  # once per player and game
        assert "API 复盘" in client.get("/api/roster").json()["seats"][0]["memory"]
        assert client.post("/api/game/review", json={"ids": ["ghost"]}).status_code == 400


# ------------------------------------------------------------------------------------ guards
def test_everything_that_changes_something_needs_the_token(client, monkeypatch):
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    writes = [
        ("put", "/api/roster/N1", {"persona": "x"}),
        ("post", "/api/roster/N1/avatar:regenerate", None),
        ("post", "/api/roster/avatars:regenerate", None),
        ("post", "/api/roster/N1/refine", {"kind": "persona", "text": "x"}),
        ("post", "/api/roster/memory:clear", {}),
        ("put", "/api/roster/memory-enabled", {"enabled": True}),
        ("put", "/api/budget", {}),
        ("post", "/api/game/review", {}),
    ]
    for method, path, body in writes:
        assert client.request(method.upper(), path, json=body).status_code == 401, path
    assert client.get("/api/roster").status_code == 401 and client.get("/api/budget").status_code == 401
    assert client.get("/api/roster", headers={"X-Arena-Token": TOKEN}).status_code == 200
    assert client.post("/api/roster/memory:import", content=b"x").status_code == 401


def test_a_remote_client_may_read_but_not_write_without_a_token(orch):
    with TestClient(app, client=("203.0.113.5", 50000)) as remote:
        assert remote.get("/api/roster").status_code == 200
        assert remote.put("/api/roster/N1", json={"persona": "x"}).status_code == 403
        assert remote.put("/api/budget", json={"max_tokens": 5}).status_code == 403
        assert remote.post("/api/roster/memory:import", content=b"x").status_code == 403
