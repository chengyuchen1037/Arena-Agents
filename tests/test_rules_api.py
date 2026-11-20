"""``/api/rules`` (contract §13.7, the "files" table): scenarios and study materials - read, download, replace, restore the default, add and
delete materials; success and every error.

Like ``test_contract_api.py``: a small app with a fake orchestrator that records ``sources_changed`` calls, the real stores on the isolated
data directory, so "a change makes every bot stale" is checked through the real hashes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import bots as bots_api
from app.api import rules as rules_api
from app.bots.types import sha256_text
from app.storage.bots import get_bot_store
from app.storage.errors import StorageError
from app.storage.rules import MAX_MATERIAL_BYTES, MAX_MATERIALS, MAX_SCENARIO_BYTES, get_rules_store

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = (ROOT / "scenarios" / "werewolf_12p.md").read_text(encoding="utf-8")
MINIMAL = (ROOT / "scenarios" / "minimal_test.md").read_text(encoding="utf-8")
SID = "werewolf_12p"
INIT_FILES = {
    "arch_notes": "我的操作笔记。",
    "digest": "我的规则要点。",
    "playbook": "- [把握:中|支持:1|反例:0|来源:初始化|#投票] 先看票型。",
}
ITEM_KEYS = {"id", "version", "title", "origin", "imported_at", "sha256", "chars", "materials", "problem"}


class FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.fail = False

    async def sources_changed(self, kind: str, scenario_id: str | None) -> None:
        self.calls.append((kind, scenario_id))
        if self.fail:
            raise RuntimeError("the orchestrator is broken")


def make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(rules_api.router)

    @app.exception_handler(RequestValidationError)
    async def _validation(request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "; ".join(str(e.get("msg")) for e in exc.errors()[:3]) or "Invalid request"})

    @app.exception_handler(StarletteHTTPException)
    async def _http(request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc.detail)})

    @app.exception_handler(StorageError)
    async def _storage(request, exc: StorageError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc) or "存储不可用。"})

    return app


@pytest.fixture
def fake(monkeypatch) -> FakeOrchestrator:
    orch = FakeOrchestrator()
    monkeypatch.setattr(rules_api, "orchestrator", orch)
    return orch


@pytest.fixture
def client(fake) -> TestClient:
    return TestClient(make_app())


def with_version(text: str, version: str) -> str:
    return re.sub(r"^version:.*$", f"version: {version}", text, count=1, flags=re.MULTILINE)


def error_of(response, status: int, code: str | None = None) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert isinstance(body["detail"], str) and body["detail"]
    if code is not None:
        assert body.get("code") == code, body
    return body


def init_first_bot() -> tuple[object, str]:
    store = get_bot_store()
    bot = store.lineup()[0]
    sources = bots_api.current_sources()
    store.commit_init(bot, sources.scenario_id, INIT_FILES, model="m", duration_s=1, sources=sources)
    return store, bot


def init_of(store, bot: str) -> dict:
    return store.summary(bot, sources=bots_api.current_sources())["init"]


# ======================================================================================================================
# scenarios
# ======================================================================================================================
def test_the_scenarios_there_are(client):
    body = client.get("/api/rules").json()
    assert set(body) == {"scenarios"} and [s["id"] for s in body["scenarios"]] == [SID]
    item = body["scenarios"][0]
    assert set(item) == ITEM_KEYS and item["version"] == "2.2.0" and item["title"] == "12 人标准局狼人杀（暗牌局）"
    assert item["origin"] == "default" and item["imported_at"] is None and item["sha256"] == sha256_text(SCENARIO)
    assert item["chars"] == len(SCENARIO) and item["materials"] == [] and item["problem"] is None
    assert "markdown" not in item  # (the list is light: the text comes with the single scenario)


def test_one_scenario_with_its_text_and_a_download(client):
    data = client.get(f"/api/rules/{SID}").json()
    assert set(data) == ITEM_KEYS | {"markdown"} and data["markdown"] == SCENARIO and data["id"] == SID
    response = client.get(f"/api/rules/{SID}?download=1")
    assert response.status_code == 200 and response.content.decode("utf-8") == SCENARIO
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.headers["content-disposition"] == "attachment; filename=\"werewolf_12p-2.2.0.scenario.md\"; filename*=UTF-8''werewolf_12p-2.2.0.scenario.md"
    # a version with characters a file name cannot have
    client.put(f"/api/rules/{SID}", json={"markdown": with_version(SCENARIO, "2.3/beta:1")})
    assert "werewolf_12p-2.3beta1.scenario.md" in client.get(f"/api/rules/{SID}?download=1").headers["content-disposition"]


@pytest.mark.parametrize("sid", ["nothing", "a%20b", "x" * 80, "w:x", "%E4%B8%AD%E6%96%87"])
def test_an_unknown_scenario_is_a_404(client, sid):
    for path in (f"/api/rules/{sid}", f"/api/rules/{sid}?download=1", f"/api/rules/{sid}/materials/m"):
        body = error_of(client.get(path), 404, "not_found")
        assert "没有这个剧本" in body["detail"]


@pytest.mark.parametrize("sid", ["..", "..%2Fescape", "%2E%2E/x"])
def test_a_path_that_climbs_never_reaches_a_scenario(client, sid):
    """The HTTP layer collapses ``..`` before the router sees the path: it is a plain 404 (no route), never a file outside ``rules/``."""
    for path in (f"/api/rules/{sid}", f"/api/rules/{sid}?download=1", f"/api/rules/{sid}/materials/m"):
        assert client.get(path).status_code == 404


def test_a_scenario_is_replaced_and_the_orchestrator_is_told(client, fake):
    custom = with_version(SCENARIO, "2.3.0")
    response = client.put(f"/api/rules/{SID}", json={"markdown": "\ufeff" + custom.replace("\n", "\r\n")})
    assert response.status_code == 200
    item = response.json()
    assert set(item) == ITEM_KEYS and item["version"] == "2.3.0" and item["origin"] == "imported" and isinstance(item["imported_at"], int)
    assert item["sha256"] == sha256_text(custom) and item["chars"] == len(custom) and item["problem"] is None
    assert fake.calls == [("scenario", SID)]
    assert client.get(f"/api/rules/{SID}").json()["markdown"] == custom  # (stored normalized)


def test_every_bot_becomes_stale_when_a_scenario_or_a_material_changes(client):
    store, bot = init_first_bot()
    assert init_of(store, bot)["status"] == "ready"
    assert client.put(f"/api/rules/{SID}", json={"markdown": with_version(SCENARIO, "2.3.0")}).status_code == 200
    assert init_of(store, bot) == {**init_of(store, bot), "status": "stale", "stale": ["scenario"]}
    assert client.post(f"/api/rules/{SID}:reset").status_code == 200  # (the same text as at the initialization: ready again)
    assert init_of(store, bot)["status"] == "ready"
    assert client.put(f"/api/rules/{SID}/materials/notes", json={"markdown": "我的材料"}).status_code == 200
    assert init_of(store, bot)["status"] == "stale" and init_of(store, bot)["stale"] == ["materials"]
    assert client.delete(f"/api/rules/{SID}/materials/notes").status_code == 200
    assert init_of(store, bot)["status"] == "ready"  # (no materials, like at the initialization)


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda t: "", "剧本是空的"),
        (lambda t: t.replace("schema_version: 2", "schema_version: 3", 1), "schema_version 必须是 2（收到 3）"),
        (lambda t: t.split("---", 2)[2], "剧本必须以 YAML front matter（---）开头"),
        (lambda t: t.replace("setup:", "setup: [", 1), "front matter 不是合法的 YAML"),
        (lambda t: t.replace("id: werewolf_12p", "id: other_game", 1), "剧本文件里的 id 是「other_game」，与这个剧本的 id「werewolf_12p」不同"),
        (lambda t: t + "\n" + "补" * MAX_SCENARIO_BYTES, "剧本最多 512 KB"),
    ],
)
def test_a_scenario_the_loader_refuses_is_a_400_with_the_loaders_own_explanation(client, fake, mutate, fragment):
    body = error_of(client.put(f"/api/rules/{SID}", json={"markdown": mutate(SCENARIO)}), 400, "invalid")
    assert fragment in body["detail"] and body["detail"] == " ".join(body["problems"])  # (no prefix: the detail IS the explanation)
    assert client.get(f"/api/rules/{SID}").json()["markdown"] == SCENARIO and fake.calls == []  # (nothing written, nobody told)


def test_the_message_names_the_scenario_by_its_id_never_by_a_server_path(client):
    body = error_of(client.put(f"/api/rules/{SID}", json={"markdown": SCENARIO.replace("schema_version: 2", "schema_version: 3", 1)}), 400)
    assert body["detail"] == "werewolf_12p.md: schema_version 必须是 2（收到 3）" and "\\" not in body["detail"] and "/" not in body["detail"]


@pytest.mark.parametrize("sid", ["a b", "x" * 65, ".hidden", "带中文", "a:b"])
def test_an_id_that_cannot_be_a_folder_name_is_a_400(client, sid):
    body = error_of(client.put(f"/api/rules/{sid}", json={"markdown": SCENARIO}), 400)
    assert "剧本 id 只能由字母、数字" in body["detail"]
    assert get_rules_store().ids() == [SID]


def test_a_new_scenario_can_be_added(client, fake):
    item = client.put("/api/rules/minimal_test", json={"markdown": MINIMAL}).json()
    assert item["id"] == "minimal_test" and item["title"] == "最小测试剧本" and item["origin"] == "imported" and fake.calls == [("scenario", "minimal_test")]
    assert [s["id"] for s in client.get("/api/rules").json()["scenarios"]] == [SID, "minimal_test"]  # (the default first)
    error_of(client.put("/api/rules/minimal_test", json={"markdown": MINIMAL.replace("id: minimal_test", "id: x", 1)}), 400)
    error_of(client.post("/api/rules/minimal_test:reset"), 400, "no_default")  # (only the default scenario has a default)


def test_a_request_that_is_not_a_scenario_is_a_422(client):
    assert client.put(f"/api/rules/{SID}", json={}).status_code == 422
    assert client.put(f"/api/rules/{SID}", json={"markdown": 5}).status_code == 422
    assert client.put(f"/api/rules/{SID}", json={"markdown": SCENARIO, "extra": 1}).status_code == 422
    assert client.put(f"/api/rules/{SID}", json={"markdown": "x" * 2_000_001}).status_code == 422
    assert client.get(f"/api/rules/{SID}").json()["markdown"] == SCENARIO


def test_the_default_scenario_comes_back_and_the_materials_stay(client, fake):
    client.put(f"/api/rules/{SID}/materials/keep", json={"markdown": "材料"})
    client.put(f"/api/rules/{SID}", json={"markdown": with_version(SCENARIO, "9.9.9")})
    fake.calls.clear()
    item = client.post(f"/api/rules/{SID}:reset").json()
    assert item["version"] == "2.2.0" and item["origin"] == "default" and item["imported_at"] is None and item["sha256"] == sha256_text(SCENARIO)
    assert [m["slug"] for m in item["materials"]] == ["keep"] and fake.calls == [("scenario", SID)]
    assert client.post(f"/api/rules/{SID}:reset").status_code == 200  # (idempotent)


def test_a_default_that_is_missing_cannot_be_restored(client, fake, monkeypatch):
    store = get_rules_store()
    monkeypatch.setattr(type(store), "_default_file", lambda self: store.data_dir / "no-such-default.md")
    error_of(client.post(f"/api/rules/{SID}:reset"), 400, "no_default")  # (no file, so no default id: nothing to go back to)
    assert fake.calls == []


def test_a_scenario_edited_by_hand_into_something_unusable_is_still_listed_with_its_problem(client, isolated_external_storage: Path):
    get_rules_store()
    (isolated_external_storage / "rules" / SID / "scenario.md").write_text(SCENARIO.replace("schema_version: 2", "schema_version: 9", 1), encoding="utf-8", newline="\n")
    item = client.get("/api/rules").json()["scenarios"][0]
    assert item["problem"] == "werewolf_12p.md: schema_version 必须是 2（收到 9）" and item["title"] == SID
    assert client.get(f"/api/rules/{SID}?download=1").status_code == 200  # (it can still be downloaded to be fixed)
    assert client.post(f"/api/rules/{SID}:reset").json()["problem"] is None


# ======================================================================================================================
# materials
# ======================================================================================================================
def test_a_material_is_added_read_downloaded_and_deleted(client, fake):
    text = "# 一篇策略文章\r\n\r\n先看事实。\r\n"
    item = client.put(f"/api/rules/{SID}/materials/strategy-1", json={"markdown": text}).json()
    assert set(item) == ITEM_KEYS and [m["slug"] for m in item["materials"]] == ["strategy-1"]
    normalized = "# 一篇策略文章\n\n先看事实。\n"
    assert item["materials"][0] == {"slug": "strategy-1", "title": "一篇策略文章", "chars": len(normalized), "sha256": sha256_text(normalized)}
    assert fake.calls == [("materials", SID)]
    got = client.get(f"/api/rules/{SID}/materials/strategy-1").json()
    assert got == {**item["materials"][0], "markdown": normalized}
    response = client.get(f"/api/rules/{SID}/materials/strategy-1?download=1")
    assert response.content.decode("utf-8") == normalized and response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.headers["content-disposition"] == "attachment; filename=\"werewolf_12p-strategy-1.material.md\"; filename*=UTF-8''werewolf_12p-strategy-1.material.md"
    assert client.get(f"/api/rules/{SID}").json()["materials"] == item["materials"]  # (the list is part of the scenario)
    after = client.delete(f"/api/rules/{SID}/materials/strategy-1").json()
    assert after["materials"] == [] and fake.calls == [("materials", SID), ("materials", SID)]
    error_of(client.get(f"/api/rules/{SID}/materials/strategy-1"), 404, "not_found")


def test_the_title_the_user_gives_is_what_the_list_shows(client):
    item = client.put(f"/api/rules/{SID}/materials/a", json={"markdown": "正文", "title": "  我起的\n名字 "}).json()
    assert item["materials"][0]["title"] == "我起的 名字"
    item = client.put(f"/api/rules/{SID}/materials/a", json={"markdown": "# 文本里的标题"}).json()  # (a whole replacement)
    assert item["materials"][0]["title"] == "文本里的标题"
    item = client.put(f"/api/rules/{SID}/materials/b", json={"markdown": "正文"}).json()
    assert [(m["slug"], m["title"]) for m in item["materials"]] == [("a", "文本里的标题"), ("b", "b")]


@pytest.mark.parametrize("slug", ["Upper", "a_b", "-lead", "x" * 33, "a.md", "a%20b", "%E4%B8%AD%E6%96%87"])
def test_a_bad_slug_is_a_400(client, fake, slug):
    body = error_of(client.put(f"/api/rules/{SID}/materials/{slug}", json={"markdown": "正文"}), 400)
    assert "材料的标识只能用小写字母、数字和连字符" in body["detail"] and fake.calls == []
    error_of(client.get(f"/api/rules/{SID}/materials/{slug}"), 404, "not_found")
    error_of(client.delete(f"/api/rules/{SID}/materials/{slug}"), 404, "not_found")


def test_what_a_material_may_not_be(client, fake):
    assert "学习材料是空的" in error_of(client.put(f"/api/rules/{SID}/materials/m", json={"markdown": "  \n "}), 400)["detail"]
    assert "标题最多 60 字" in error_of(client.put(f"/api/rules/{SID}/materials/m", json={"markdown": "x", "title": "题" * 61}), 400)["detail"]
    too_big = error_of(client.put(f"/api/rules/{SID}/materials/m", json={"markdown": "补" * (MAX_MATERIAL_BYTES // 3 + 10)}), 413, "too_large")
    assert f"学习材料最多 {MAX_MATERIAL_BYTES // 1024} KB" in too_big["detail"]
    assert client.put(f"/api/rules/{SID}/materials/m", json={"markdown": "x", "extra": 1}).status_code == 422
    assert client.put(f"/api/rules/{SID}/materials/m", json={"title": "没有正文"}).status_code == 422
    assert get_rules_store().materials(SID) == {} and fake.calls == []


def test_the_number_of_materials_is_limited(client):
    for index in range(MAX_MATERIALS):
        assert client.put(f"/api/rules/{SID}/materials/m{index}", json={"markdown": f"材料 {index}"}).status_code == 200
    body = error_of(client.put(f"/api/rules/{SID}/materials/one-too-many", json={"markdown": "再来一份"}), 400, "too_many")
    assert f"最多 {MAX_MATERIALS} 份学习材料" in body["detail"]
    assert client.put(f"/api/rules/{SID}/materials/m0", json={"markdown": "换一份"}).status_code == 200  # (replacing is fine)
    assert len(client.get(f"/api/rules/{SID}").json()["materials"]) == MAX_MATERIALS


def test_a_material_of_an_unknown_scenario_or_an_unknown_material_is_a_404(client, fake):
    error_of(client.put("/api/rules/nothing/materials/m", json={"markdown": "正文"}), 404, "not_found")
    error_of(client.delete("/api/rules/nothing/materials/m"), 404, "not_found")
    body = error_of(client.delete(f"/api/rules/{SID}/materials/nothing"), 404, "not_found")
    assert body["detail"] == "没有这份学习材料：nothing" and fake.calls == []


# ======================================================================================================================
# the orchestrator hook, who may do what
# ======================================================================================================================
def test_a_hook_that_fails_never_fails_the_request(client, fake, caplog):
    fake.fail = True
    with caplog.at_level("WARNING"):
        assert client.put(f"/api/rules/{SID}/materials/m", json={"markdown": "正文"}).status_code == 200
        assert client.put(f"/api/rules/{SID}", json={"markdown": with_version(SCENARIO, "2.4.0")}).status_code == 200
    assert fake.calls == [("materials", SID), ("scenario", SID)] and "sources_changed failed after a change of the materials of werewolf_12p" in caplog.text


def test_an_orchestrator_without_the_hook_is_fine(monkeypatch):
    monkeypatch.setattr(rules_api, "orchestrator", object())
    http = TestClient(make_app())
    assert http.put(f"/api/rules/{SID}", json={"markdown": with_version(SCENARIO, "2.4.0")}).status_code == 200
    assert http.put(f"/api/rules/{SID}/materials/m", json={"markdown": "正文"}).status_code == 200
    assert http.post(f"/api/rules/{SID}:reset").status_code == 200


def test_reading_is_open_but_every_change_needs_a_trusted_client(fake, monkeypatch):
    stranger = TestClient(make_app(), client=("203.0.113.5", 50000))
    assert stranger.get("/api/rules").status_code == 200 and stranger.get(f"/api/rules/{SID}?download=1").status_code == 200
    changes = [
        ("put", f"/api/rules/{SID}", {"markdown": SCENARIO}),
        ("post", f"/api/rules/{SID}:reset", None),
        ("put", f"/api/rules/{SID}/materials/m", {"markdown": "正文"}),
        ("delete", f"/api/rules/{SID}/materials/m", None),
    ]
    for method, path, body in changes:
        response = getattr(stranger, method)(path, **({"json": body} if body is not None else {}))
        assert response.status_code == 403, (method, path)
    assert fake.calls == []
    monkeypatch.setenv("ARENA_API_TOKEN", "t0ken-value")
    assert stranger.get("/api/rules").status_code == 401
    headers = {"X-Arena-Token": "t0ken-value"}
    assert stranger.put(f"/api/rules/{SID}/materials/m", json={"markdown": "正文"}, headers=headers).status_code == 200
    assert stranger.delete(f"/api/rules/{SID}/materials/m", headers=headers).status_code == 200
    assert stranger.put(f"/api/rules/{SID}/materials/m", json={"markdown": "正文"}, headers={**headers, "Origin": "http://evil.example"}).status_code == 403
