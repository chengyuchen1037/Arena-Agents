"""``/api/bots/**`` and ``/api/moderator`` (contract §13.7, the "botstore" table): every endpoint, its success and its errors.

The API runs in a small app of its own, with a fake orchestrator that has only what the API looks up (``session.status``,
``bots_changed``, ``init_busy_ids``, ``complete_oneshot``), so that these tests do not depend on the game package; the real stores
(bots, settings, providers) work on the isolated data directory of the tests."""

from __future__ import annotations

import io
import json
import re
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import bots as bots_api
from app.archive.errors import ArchiveFormatError
from app.bots.types import CoreFiles
from app.providers.models import ModelBinding
from app.storage import botfiles as bf
from app.storage.bots import RESET_CONFIRM_TEXT, BotStore, get_bot_store
from app.storage.errors import StorageError
from app.storage.settings import SettingsStore

INIT_FILES = {
    "arch_notes": "我的操作笔记。",
    "digest": "我的规则要点。",
    "playbook": "- [把握:中|支持:1|反例:0|来源:初始化|#投票] 先看票型。",
    "questions": "还不确定平票。",
}
MD = (
    "---\nformat: arena-core\nversion: 1\nname: Alice\n---\n# 身份\n一个新的身份。\n\n# 说话方式\n你说话很短。\n「好。」「再说一遍。」「我不信。」\n篇幅：40–100 字\n\n"
    "# 分析方式\n先看发生了什么。\n"
)


# ======================================================================================================================
# the fixtures
# ======================================================================================================================
class FakeOrchestrator:
    def __init__(self) -> None:
        self.session = SimpleNamespace(status="idle", get_model_binding=lambda actor: object() if self.moderator_bound else None)
        self.spec = SimpleNamespace(id="demo")
        self.demo_mode = False
        self.moderator_bound = True
        self.changes: list[bool] = []
        self.busy: set[str] = set()
        self.answer = ""
        self.fail = False
        self.calls: list[tuple[str, list[dict], str]] = []

    def bots_changed(self, *, roster: bool) -> None:
        self.changes.append(roster)

    def init_busy_ids(self) -> set[str]:
        return set(self.busy)

    async def complete_oneshot(self, agent_id, messages, *, max_tokens, purpose):
        self.calls.append((agent_id, messages, purpose))
        if self.fail:
            raise RuntimeError("the provider said: invalid key sk-secret-123")
        return self.answer


class FakeCodec:
    """The ``.arenabot`` zip of the archive package, without its checks: enough to see the wiring (the real one has its own tests)."""

    def pack_arenabot(self, target: Path, *, name: str, origin: str, files) -> None:
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"format": "arenabot", "bot": {"name": name, "origin": origin}}))
            for rel, path in files:
                archive.write(path, f"bot/{rel}")

    def unpack_arenabot(self, source: Path, workdir: Path) -> Path:
        try:
            with zipfile.ZipFile(source) as archive:
                for info in archive.infolist():
                    if info.filename.startswith("bot/") and not info.is_dir():
                        target = workdir / info.filename
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(archive.read(info))
        except zipfile.BadZipFile:
            raise ArchiveFormatError("这不是有效的 .arenabot 文件。") from None
        return workdir / "bot"


def make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(bots_api.router)

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
    monkeypatch.setattr(bots_api, "orchestrator", orch)
    return orch


@pytest.fixture
def client(fake) -> TestClient:
    return TestClient(make_app())


@pytest.fixture
def store(fake) -> BotStore:
    return get_bot_store()


def init_bot(store: BotStore, bot_id: str) -> None:
    sources = bots_api.current_sources()
    store.commit_init(bot_id, sources.scenario_id, INIT_FILES, model="deepseek-flash", duration_s=5, sources=sources)


def ids_of(store: BotStore) -> dict[str, str]:
    return {name: bot_id for bot_id, name in store.names().items()}


def detail_of(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def error_of(response, status: int, code: str | None = None) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert isinstance(body["detail"], str) and body["detail"]
    if code is not None:
        assert body.get("code") == code, body
    return body


# ======================================================================================================================
# the table
# ======================================================================================================================
def test_the_table_is_twelve_bots_with_their_seats_and_what_the_ui_counts_against(client, store):
    body = client.get("/api/bots").json()
    assert set(body) == {"lineup", "bots", "memory_enabled", "init_job", "ready", "learning_usage", "limits", "defaults"}
    assert body["lineup"] == store.lineup() and len(body["bots"]) == 12 and body["memory_enabled"] is True and body["init_job"] is None
    assert [b["id"] for b in body["bots"]] == body["lineup"] and [b["seat"] for b in body["bots"]] == [f"N{i}" for i in range(1, 13)]
    first = body["bots"][0]
    assert set(first) == {"id", "name", "avatar", "avatar_code", "origin", "core", "init", "frozen", "memory_stats", "staging", "seat", "binding"}
    assert first["name"] == "Alice" and first["core"]["complete"] is True and first["init"]["status"] == "uninitialized" and first["frozen"] is False
    assert first["binding"] == {"ok": False, "label": "未绑定模型"}
    assert body["learning_usage"] == {"calls": 0, "tokens": 0, "cost_usd": 0.0}
    assert body["limits"]["core_limits"] == {"identity": 300, "voice": 600, "mind": 400} and body["limits"]["playbook_max"] == 20
    assert [d["name"] for d in body["defaults"]] == [b["name"] for b in body["bots"]] and all(set(d) == {"name", "avatar", "identity"} for d in body["defaults"])
    # nothing can start yet: every bot says why (not initialized, no model)
    assert body["ready"]["ok"] is False and len(body["ready"]["problems"]) == 12
    problem = body["ready"]["problems"][0]
    assert problem["scope"] == "bot" and problem["bot_id"] == first["id"] and problem["seat"] == "N1" and problem["name"] == "Alice"
    assert [i["code"] for i in problem["items"]] == ["not_initialized", "no_binding"] and problem["items"][1]["fix"] == "bind_model"


def test_a_bot_with_a_model_that_works_is_ready(client, store, monkeypatch):
    from app.providers.store import get_provider_store

    provider_store = get_provider_store()
    provider = provider_store.create_provider("演示商", "openai_chat", "https://api.example.com/v1", "sk-test-key")
    provider_store.add_model(provider.id, "m1", 1.0, 2.0)
    bot = store.lineup()[0]
    SettingsStore(store.data_dir).save_bot_binding(bot, ModelBinding(provider_id=provider.id, model_id="m1", reasoning_depth="low"))
    init_bot(store, bot)
    first = client.get("/api/bots").json()["bots"][0]
    assert first["binding"] == {"ok": True, "label": "演示商 / m1"} and first["init"]["status"] == "ready" and first["frozen"] is True
    assert first["id"] not in [p["bot_id"] for p in client.get("/api/bots").json()["ready"]["problems"]]
    # a model that is gone, or a provider without a key
    SettingsStore(store.data_dir).save_bot_binding(bot, ModelBinding(provider_id="gone", model_id="x", reasoning_depth="low"))
    assert client.get("/api/bots").json()["bots"][0]["binding"] == {"ok": False, "label": "gone / x（已不存在）"}
    monkeypatch.setenv("ARENA_DEMO", "1")  # (scripted bots call no model)
    assert client.get("/api/bots").json()["bots"][0]["binding"] == {"ok": True, "label": "演示模式"}


def test_the_lineup_can_be_changed_while_the_game_is_idle(client, store, fake):
    order = store.lineup()
    reverse = list(reversed(order))
    body = client.put("/api/bots/lineup", json={"order": reverse})
    assert body.status_code == 200
    data = body.json()
    assert data["lineup"] == reverse and [b["id"] for b in data["bots"]] == reverse and data["bots"][0]["seat"] == "N1" and data["bots"][0]["name"] == "Leo"
    assert "limits" not in data and "defaults" not in data  # (the live part only; limits and defaults come with GET)
    assert store.lineup() == reverse and fake.changes == [True]  # (the orchestrator is told: the idle game takes the new seats)
    assert client.get("/api/bots").json()["lineup"] == reverse


@pytest.mark.parametrize("order", [[], ["b-00000000"] * 12, "abc", None])
def test_a_line_up_that_is_not_a_permutation_is_refused(client, store, fake, order):
    response = client.put("/api/bots/lineup", json={"order": order})
    assert response.status_code == 422 and isinstance(response.json()["detail"], str)
    assert store.lineup() == store.lineup() and fake.changes == []


def test_a_wrong_line_up_is_422_with_its_own_code_and_changes_nothing(client, store, fake):
    order = store.lineup()
    body = error_of(client.put("/api/bots/lineup", json={"order": order[:-1]}), 422, "bad_lineup")
    assert "排列" in body["detail"]
    error_of(client.put("/api/bots/lineup", json={"order": [*order[:-1], "b-ffffffff"]}), 422, "bad_lineup")
    error_of(client.put("/api/bots/lineup", json={"order": [order[0]] * 12}), 422, "bad_lineup")
    assert client.put("/api/bots/lineup", json={"order": order, "extra": 1}).status_code == 422
    assert store.lineup() == order and fake.changes == []


def test_the_lineup_belongs_to_the_table_before_the_game_starts(client, store, fake):
    fake.session.status = "running"
    order = store.lineup()
    body = error_of(client.put("/api/bots/lineup", json={"order": list(reversed(order))}), 409, "game_active")
    assert body["detail"] == "阵容只能在对局开始之前修改；请先新建对局。"
    assert store.lineup() == order and fake.changes == []
    for status in ("paused", "finished"):
        fake.session.status = status
        error_of(client.put("/api/bots/lineup", json={"order": list(reversed(order))}), 409, "game_active")


def test_every_change_needs_a_trusted_client_but_reading_does_not(store, fake, monkeypatch):
    stranger = TestClient(make_app(), client=("203.0.113.5", 50000))
    assert stranger.get("/api/bots").status_code == 200
    for method, path, body in [
        ("put", "/api/bots/lineup", {"order": store.lineup()}),
        ("post", "/api/bots/core:clear", {}),
        ("post", "/api/bots/memory:clear", {}),
        ("put", "/api/bots/memory-enabled", {"enabled": False}),
        ("post", "/api/bots/avatars:regenerate", None),
        ("put", f"/api/bots/{store.lineup()[0]}", {"name": "Nope"}),
        ("put", f"/api/bots/{store.lineup()[0]}/core", {"markdown": MD}),
        ("delete", f"/api/bots/{store.lineup()[0]}/core", None),
        ("put", f"/api/bots/{store.lineup()[0]}/memory/digest", {"content": "x"}),
        ("post", f"/api/bots/{store.lineup()[0]}/import-default", {"name": "Bob"}),
        ("put", "/api/moderator", {"avatar": {}}),
        ("post", "/api/moderator/avatar:regenerate", None),
    ]:
        response = getattr(stranger, method)(path, **({"json": body} if body is not None else {}))
        assert response.status_code == 403, (method, path, response.text)
    assert store.name_of(store.lineup()[0]) == "Alice"
    # with an API token the token decides: a stranger with it may change things, nobody without it may read
    monkeypatch.setenv("ARENA_API_TOKEN", "t0ken-value")
    assert stranger.get("/api/bots").status_code == 401
    assert stranger.get("/api/bots", headers={"X-Arena-Token": "t0ken-value"}).status_code == 200
    assert stranger.put("/api/bots/memory-enabled", json={"enabled": False}, headers={"X-Arena-Token": "t0ken-value"}).status_code == 200


# ======================================================================================================================
# clearing, the switch, avatars
# ======================================================================================================================
def test_the_l0_of_some_or_all_bots_can_be_cleared(client, store, fake):
    ids = store.lineup()
    init_bot(store, ids[0])
    body = client.post("/api/bots/core:clear", json={"ids": [ids[0], ids[1]]}).json()
    assert [b["core"]["complete"] for b in body["bots"][:3]] == [False, False, True]
    assert body["bots"][0]["init"]["status"] == "stale" and body["bots"][0]["core"]["missing"] == ["identity", "voice", "mind"]
    assert store.core(ids[0]) == CoreFiles() and store.core(ids[2]).complete and fake.changes == [False]
    assert any(i["code"] == "core_missing" for p in body["ready"]["problems"] if p["bot_id"] == ids[0] for i in p["items"])  # (the start check stops there)
    body = client.post("/api/bots/core:clear").json()  # (no body: everybody)
    assert all(not b["core"]["complete"] for b in body["bots"])
    error_of(client.post("/api/bots/core:clear", json={"ids": ["b-ffffffff"]}), 404, "not_found")
    assert client.post("/api/bots/core:clear", json={"ids": ids, "other": 1}).status_code == 422


def test_what_the_bots_learned_can_be_forgotten_but_their_l0_stays(client, store, fake):
    ids = store.lineup()
    for bot in ids[:2]:
        init_bot(store, bot)
        store.add_record(bot, "角色", True)
    store.write_part(ids[0], "people", "- Bob：话少", "demo")
    body = client.post("/api/bots/memory:clear", json={"ids": [ids[0]]}).json()
    assert body["bots"][0]["init"]["status"] == "uninitialized" and body["bots"][0]["frozen"] is False
    assert body["bots"][0]["memory_stats"] == {"arch_notes": 0, "digest": 0, "playbook": 0, "questions": 0, "people": 0, "record": ""}
    assert body["bots"][1]["init"]["status"] == "ready" and store.core(ids[0]).complete
    body = client.post("/api/bots/memory:clear", json={}).json()
    assert all(b["init"]["status"] == "uninitialized" for b in body["bots"]) and fake.changes == [False, False]
    error_of(client.post("/api/bots/memory:clear", json={"ids": ["nonsense"]}), 404, "not_found")


def test_clearing_is_refused_for_a_bot_that_is_being_initialized(client, store, fake):
    ids = store.lineup()
    init_bot(store, ids[0])
    fake.busy = {ids[0]}
    assert error_of(client.post("/api/bots/memory:clear", json={"ids": [ids[0]]}), 409, "init_busy")["detail"] == "正在初始化，请等它结束（或先取消）后再修改。"
    error_of(client.post("/api/bots/memory:clear"), 409, "init_busy")  # (everybody includes it)
    error_of(client.post("/api/bots/core:clear"), 409, "init_busy")
    error_of(client.put(f"/api/bots/{ids[0]}/core", json={"markdown": MD}), 409, "init_busy")
    error_of(client.put(f"/api/bots/{ids[0]}/memory/arch_notes", json={"content": "x"}), 409, "init_busy")
    error_of(client.put(f"/api/bots/{ids[0]}", json={"name": "Other"}), 409, "init_busy")
    error_of(client.post("/api/bots/avatars:regenerate"), 409, "init_busy")
    assert client.post("/api/bots/memory:clear", json={"ids": [ids[1]]}).status_code == 200  # (another bot is fine)
    assert store.read_part(ids[0], "arch_notes") == INIT_FILES["arch_notes"]
    fake.busy = set()
    assert client.post("/api/bots/memory:clear", json={"ids": [ids[0]]}).status_code == 200


def test_the_memory_switch(client, store, fake):
    assert client.put("/api/bots/memory-enabled", json={"enabled": False}).json()["memory_enabled"] is False
    assert store.memory_enabled() is False and client.get("/api/bots").json()["memory_enabled"] is False
    assert client.put("/api/bots/memory-enabled", json={"enabled": True}).json()["memory_enabled"] is True and store.memory_enabled() is True
    assert client.put("/api/bots/memory-enabled", json={"enabled": "maybe"}).status_code == 422
    assert client.put("/api/bots/memory-enabled", json={}).status_code == 422
    fake.session.status = "running"
    assert client.put("/api/bots/memory-enabled", json={"enabled": False}).status_code == 200  # (a running game keeps the snapshot it started with)


def test_new_avatars_for_everybody_who_has_not_learned_anything(client, store, fake):
    ids = store.lineup()
    init_bot(store, ids[3])
    before = {i: store.summary(i)["avatar"] for i in ids}
    body = client.post("/api/bots/avatars:regenerate").json()
    assert set(body) == {"bots", "skipped"} and body["skipped"] == [ids[3]]
    after = {b["id"]: b["avatar"] for b in body["bots"]["bots"]}
    assert after[ids[3]] == before[ids[3]] and all(after[i] != before[i] for i in ids if i != ids[3])
    assert fake.changes == [True]
    fake.session.status = "paused"
    error_of(client.post("/api/bots/avatars:regenerate"), 409, "game_active")


# ======================================================================================================================
# one bot
# ======================================================================================================================
def test_the_detail_of_a_bot(client, store):
    bot = store.lineup()[1]
    init_bot(store, bot)
    store.write_part(bot, "people", "- Alice：话少", "demo")
    data = detail_of(client.get(f"/api/bots/{bot}"))
    assert data["id"] == bot and data["name"] == "Bob" and data["seat"] == "N2" and data["binding"]["ok"] is False
    assert set(data) >= {"core", "memory", "staging", "init", "frozen", "memory_stats"} and isinstance(data["staging"], list)
    assert data["core"]["markdown"].startswith("---\nformat: arena-core\nversion: 1\nname: Bob\n---\n# 身份\n")
    assert set(data["core"]["sections"]) == {"identity", "voice", "mind"} and data["core"]["extras"] == []
    default_scenario = data["memory"]["scenario_id"]
    assert default_scenario == "werewolf_12p" and data["memory"]["arch_notes"] == INIT_FILES["arch_notes"] and data["memory"]["digest"] == INIT_FILES["digest"]
    assert data["memory"]["people"] == {"Alice": "话少"}  # (the impressions belong to the bot, not to a scenario)
    other = detail_of(client.get(f"/api/bots/{bot}?scenario=demo"))
    assert other["memory"]["scenario_id"] == "demo" and other["memory"]["digest"] == "" and other["memory"]["playbook"] == []  # (what the bot wrote for a scenario is for that scenario)
    assert other["memory"]["arch_notes"] == INIT_FILES["arch_notes"] and other["memory"]["people"] == {"Alice": "话少"}
    assert error_of(client.get(f"/api/bots/{bot}?scenario=../x"), 400)["detail"]
    error_of(client.get("/api/bots/b-ffffffff"), 404, "not_found")
    error_of(client.get("/api/bots/lineup"), 404, "not_found")
    error_of(client.get("/api/bots/..%2Fescape"), 404)


def test_an_uninitialized_bot_is_renamed_in_place(client, store, fake):
    bot = store.lineup()[1]
    data = detail_of(client.put(f"/api/bots/{bot}", json={"name": "  Bobby "}))
    assert data["id"] == bot and data["name"] == "Bobby" and store.name_of(bot) == "Bobby" and fake.changes == [True]
    from app.storage.bots import pick_avatars

    new_avatar = pick_avatars(1, [store.summary(i)["avatar"] for i in store.lineup()])[0]
    data = detail_of(client.put(f"/api/bots/{bot}", json={"avatar": new_avatar}))
    assert data["id"] == bot and data["avatar"] == new_avatar and data["avatar_code"] and fake.changes == [True, True]
    assert detail_of(client.put(f"/api/bots/{bot}", json={}))["id"] == bot  # (nothing to change)
    fake.session.status = "running"
    assert detail_of(client.put(f"/api/bots/{bot}", json={}))["name"] == "Bobby"  # (nothing to change is fine at any time)


@pytest.mark.parametrize(
    "body,status,code,fragment",
    [
        ({"name": "alice"}, 400, "name_taken", "已被 N1 使用"),
        ({"name": ""}, 400, None, "不能为空"),
        ({"name": "x" * 21}, 400, None, "最多 20"),
        ({"name": "a:b"}, 400, None, "冒号"),
        ({"avatar": {"v": 1}}, 400, None, "头像"),
        ({"avatar": "face"}, 422, None, ""),
        ({"surprise": 1}, 422, None, ""),
        ({"name": 5}, 422, None, ""),
    ],
)
def test_a_bad_rename_is_refused_and_changes_nothing(client, store, fake, body, status, code, fragment):
    bot = store.lineup()[1]
    response = client.put(f"/api/bots/{bot}", json=body)
    error_of(response, status, code)
    assert fragment in response.json()["detail"]
    assert store.name_of(bot) == "Bob" and fake.changes == []


def test_a_name_and_an_avatar_belong_to_the_table_before_the_game_starts(client, store, fake):
    bot = store.lineup()[1]
    fake.session.status = "running"
    assert error_of(client.put(f"/api/bots/{bot}", json={"name": "Bobby"}), 409, "game_active")["detail"] == "名字和头像只能在对局开始之前修改；请先新建对局。"
    error_of(client.post(f"/api/bots/{bot}/avatar:regenerate"), 409, "game_active")
    error_of(client.post(f"/api/bots/{bot}/import-default", json={"name": "Zed"}), 409, "game_active")
    error_of(client.put("/api/moderator", json={"avatar": {}}), 409, "game_active")
    error_of(client.post("/api/moderator/avatar:regenerate"), 409, "game_active")
    error_of(client.post(f"/api/bots/{bot}/import", content=b"x"), 409, "game_active")
    assert store.name_of(bot) == "Bob" and fake.changes == []
    error_of(client.put("/api/bots/b-ffffffff", json={}), 404, "not_found")


def test_a_bot_that_has_learned_something_needs_a_confirmation_to_change_its_name(client, store, fake):
    ids = store.lineup()
    bot = ids[0]
    init_bot(store, bot)
    store.add_record(bot, "角色", True)
    settings = SettingsStore(store.data_dir)
    binding = ModelBinding(provider_id="p", model_id="m", reasoning_depth="low")
    settings.save_bot_binding(bot, binding)
    body = error_of(client.put(f"/api/bots/{bot}", json={"name": "Alicia"}), 409, "needs_confirm")
    assert body["detail"] == RESET_CONFIRM_TEXT and body["will_clear"] == ["初始化结果", "操作笔记", "规则要点", "打法", "待解问题", "印象", "战绩"]
    error_of(client.put(f"/api/bots/{bot}", json={"name": "Alicia", "confirm_reset": False}), 409, "needs_confirm")
    assert store.name_of(bot) == "Alice" and store.is_frozen(bot) and fake.changes == []
    data = detail_of(client.put(f"/api/bots/{bot}", json={"name": "Alicia", "confirm_reset": True}))
    assert data["id"] != bot and data["name"] == "Alicia" and data["seat"] == "N1" and data["init"]["status"] == "uninitialized" and data["frozen"] is False
    assert data["core"]["complete"] is True and data["memory"]["arch_notes"] == "" and data["memory"]["record"] == ""  # (L0 kept; the rest cleared)
    assert store.lineup()[0] == data["id"] and settings.get_bot_binding(data["id"]) == binding and fake.changes == [True]
    assert client.get("/api/bots").json()["lineup"][0] == data["id"]
    error_of(client.get(f"/api/bots/{bot}"), 404, "not_found")  # (the old id is gone)


def test_a_new_avatar_for_one_bot(client, store, fake):
    ids = store.lineup()
    before = store.summary(ids[2])["avatar"]
    data = detail_of(client.post(f"/api/bots/{ids[2]}/avatar:regenerate"))
    assert data["id"] == ids[2] and data["avatar"] != before and fake.changes == [True]
    init_bot(store, ids[2])
    body = error_of(client.post(f"/api/bots/{ids[2]}/avatar:regenerate", json={}), 409, "needs_confirm")
    assert body["will_clear"]
    data = detail_of(client.post(f"/api/bots/{ids[2]}/avatar:regenerate", json={"confirm_reset": True}))
    assert data["id"] != ids[2] and data["name"] == "Carol"
    error_of(client.post("/api/bots/b-ffffffff/avatar:regenerate"), 404, "not_found")


# ======================================================================================================================
# L0
# ======================================================================================================================
def test_the_l0_of_a_bot_is_its_merged_file(client, store):
    bot = store.lineup()[0]
    core = detail_of(client.get(f"/api/bots/{bot}/core"))
    assert core["complete"] is True and core["markdown"] == store.core_markdown(bot) and core["sections"]["identity"] == store.core(bot).identity
    assert set(core) == {"complete", "missing", "chars", "extras", "updated_at", "markdown", "sections"} | {"extras"}
    download = client.get(f"/api/bots/{bot}/core?download=1")
    assert download.status_code == 200 and download.content.decode("utf-8") == core["markdown"]
    assert download.headers["content-type"] == "text/markdown; charset=utf-8"
    assert download.headers["content-disposition"] == "attachment; filename=\"Alice.core.md\"; filename*=UTF-8''Alice.core.md"
    error_of(client.get("/api/bots/b-ffffffff/core"), 404, "not_found")
    error_of(client.get("/api/bots/b-ffffffff/core?download=1"), 404, "not_found")


def test_the_download_name_is_safe_and_speaks_chinese(client, store):
    bot = store.lineup()[0]
    store.update_bot(bot, name="小明/A*B?")
    response = client.get(f"/api/bots/{bot}/core?download=1")
    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''%E5%B0%8F%E6%98%8EAB.core.md" in disposition  # (the characters a file name cannot have are gone)
    assert "/" not in disposition.split("filename*=")[1] and "*" not in disposition.split("filename*=")[1]
    assert disposition.startswith('attachment; filename="__AB.core.md"')  # (and an ASCII fallback for old clients)
    store.update_bot(bot, name="///")
    assert "filename*=UTF-8''bot.core.md" in client.get(f"/api/bots/{bot}/core?download=1").headers["content-disposition"]


def test_a_merged_file_replaces_the_l0_and_the_name_in_it_is_only_a_hint(client, store, fake):
    bot = store.lineup()[1]  # (Bob; the file says Alice)
    init_bot(store, bot)
    data = detail_of(client.put(f"/api/bots/{bot}/core", json={"markdown": MD}))
    assert data["name"] == "Bob" and data["core"]["sections"]["identity"] == "一个新的身份。" and store.core(bot).voice.startswith("你说话很短。")
    assert data["notices"] == ["文件里的名字是「Alice」，只作提示，没有改名（这个玩家仍叫「Bob」）。"]
    assert data["init"]["status"] == "stale" and data["init"]["stale"] == ["core"] and fake.changes == [False]
    same = detail_of(client.put(f"/api/bots/{bot}/core", json={"markdown": MD.replace("name: Alice", "name: bob")}))
    assert "notices" not in same  # (the same name in another case is no news)
    nameless = detail_of(client.put(f"/api/bots/{bot}/core", json={"markdown": MD.replace("name: Alice\n", "")}))
    assert "notices" not in nameless


def test_a_merged_file_that_is_not_acceptable_is_refused_with_every_problem(client, store, fake):
    bot = store.lineup()[0]
    before = store.core(bot)
    bad = MD.replace("先看发生了什么。", "").replace("你说话很短。", "说" * 612)
    body = error_of(client.put(f"/api/bots/{bot}/core", json={"markdown": bad}), 400)
    assert body["problems"] == ["「分析方式」不能为空。", "「说话方式」最多 600 字（现在 {0} 字）。".format(len("说" * 612 + "\n「好。」「再说一遍。」「我不信。」\n篇幅：40–100 字"))]
    assert body["detail"].startswith("底层设定没有保存：共 2 处问题：")
    one = error_of(client.put(f"/api/bots/{bot}/core", json={"markdown": MD.replace("version: 1", "version: 2")}), 400)
    assert one["problems"] == ["文件头的 version 目前只能是 1（现在是「2」）。"] and one["detail"] == "底层设定没有保存：文件头的 version 目前只能是 1（现在是「2」）。"
    assert error_of(client.put(f"/api/bots/{bot}/core", json={"markdown": "随便写点什么"}), 400)["problems"][0].startswith("缺少文件头")
    assert store.core(bot) == before and fake.changes == []
    assert client.put(f"/api/bots/{bot}/core", json={}).status_code == 422
    assert client.put(f"/api/bots/{bot}/core", json={"markdown": MD, "x": 1}).status_code == 422
    error_of(client.put("/api/bots/b-ffffffff/core", json={"markdown": MD}), 404, "not_found")


def test_the_l0_of_one_bot_can_be_cleared(client, store, fake):
    bot = store.lineup()[2]
    init_bot(store, bot)
    data = detail_of(client.delete(f"/api/bots/{bot}/core"))
    assert data["core"]["complete"] is False and data["core"]["markdown"].endswith("# 身份\n\n# 说话方式\n\n# 分析方式\n") and data["init"]["status"] == "stale"
    assert store.core(bot) == CoreFiles() and store.core(store.lineup()[0]).complete and fake.changes == [False]
    error_of(client.delete("/api/bots/b-ffffffff/core"), 404, "not_found")


# ---------------------------------------------------------------------------------------------------- refine
def test_the_moderators_model_drafts_an_l0_and_nothing_is_saved(client, store, fake):
    bot = store.lineup()[0]
    before = store.core(bot)
    fake.answer = "```markdown\n" + MD.replace("name: Alice", "name: 别的名字") + "```"
    body = client.post(f"/api/bots/{bot}/core:refine", json={"text": "  话更少，更毒舌  "}).json()
    assert set(body) == {"draft"}
    parsed = bf.parse_core_markdown(body["draft"])
    assert parsed.name == "Alice" and parsed.core.identity == "一个新的身份。" and body["draft"] == bf.render_core_markdown(parsed.core, "Alice")  # (the draft is canonical, with the bot's name)
    assert store.core(bot) == before and fake.changes == []  # (not saved)
    (agent, messages, purpose), = fake.calls
    assert agent == "moderator" and purpose == "refine" and [m["role"] for m in messages] == ["system", "user"]
    assert "【整理底层设定】" in messages[0]["content"] and "话更少，更毒舌" in messages[1]["content"] and "【当前设定】" in messages[1]["content"]
    assert store.core(bot).identity in messages[1]["content"]  # (the points are merged into what the bot is now)
    assert "（玩家名：Alice）" in messages[1]["content"]
    # a bot that has no L0 yet: the model is told there is none
    store.clear_core([bot])
    client.post(f"/api/bots/{bot}/core:refine", json={"text": "一个新手"})
    assert "（还没有）" in fake.calls[-1][1][1]["content"]


def test_a_draft_that_is_over_a_limit_is_shown_with_the_problems(client, store, fake):
    bot = store.lineup()[0]
    fake.answer = MD.replace("先看发生了什么。", "想" * 450)
    body = client.post(f"/api/bots/{bot}/core:refine", json={"text": "x"}).json()
    assert body["problems"] == ["「分析方式」最多 400 字（现在 450 字）。"] and "想" * 450 in body["draft"]
    fake.answer = MD.replace("一个新的身份。", "")  # (a section that came back empty)
    assert client.post(f"/api/bots/{bot}/core:refine", json={"text": "x"}).json()["problems"] == ["「身份」还是空的。"]


def test_a_draft_without_the_header_or_with_chatter_is_still_understood(client, store, fake):
    bot = store.lineup()[0]
    body_only = MD.split("---\n", 2)[2]
    fake.answer = "好的，下面是整理后的设定：\n\n" + body_only
    draft = client.post(f"/api/bots/{bot}/core:refine", json={"text": "x"}).json()["draft"]
    assert bf.parse_core_markdown(draft).name == "Alice" and bf.parse_core_markdown(draft).core.identity == "一个新的身份。"
    fake.answer = "好的！\n" + MD + "\n希望对你有帮助。"
    assert client.post(f"/api/bots/{bot}/core:refine", json={"text": "x"}).status_code == 200


def test_a_refine_that_cannot_work_says_why(client, store, fake):
    bot = store.lineup()[0]
    error_of(client.post(f"/api/bots/{bot}/core:refine", json={"text": "   "}), 400)
    assert "要点太长了" in error_of(client.post(f"/api/bots/{bot}/core:refine", json={"text": "字" * 6001}), 400)["detail"]
    fake.moderator_bound = False
    assert "主持人还没有绑定模型" in error_of(client.post(f"/api/bots/{bot}/core:refine", json={"text": "x"}), 400)["detail"]
    fake.demo_mode = True  # (the scripted bots need no binding)
    fake.answer = MD
    assert client.post(f"/api/bots/{bot}/core:refine", json={"text": "x"}).status_code == 200
    fake.moderator_bound, fake.demo_mode = True, False
    fake.answer = ""
    assert error_of(client.post(f"/api/bots/{bot}/core:refine", json={"text": "x"}), 400)["detail"] == "模型没有返回内容，请稍后重试。"
    fake.answer = "我不知道该怎么写。"
    body = error_of(client.post(f"/api/bots/{bot}/core:refine", json={"text": "x"}), 400, "bad_draft")
    assert "没有按固定格式" in body["detail"] and body["problems"]
    fake.answer = "---\nformat: other\nversion: 1\n---\n# 身份\nx\n"
    error_of(client.post(f"/api/bots/{bot}/core:refine", json={"text": "x"}), 400, "bad_draft")
    fake.fail = True
    body = error_of(client.post(f"/api/bots/{bot}/core:refine", json={"text": "x"}), 502, "model_failed")
    assert body["detail"] == "调用主持人模型失败，请检查模型绑定和网络后重试。" and "sk-secret" not in json.dumps(body)  # (what the provider said is never echoed)
    error_of(client.post("/api/bots/b-ffffffff/core:refine", json={"text": "x"}), 404, "not_found")
    assert client.post(f"/api/bots/{bot}/core:refine", json={}).status_code == 422


def test_the_refine_uses_the_v0_13_one_shot_port_until_the_orchestrator_has_a_public_one(client, store, monkeypatch):
    class Port:
        async def complete(self, agent_id, messages, tools, *, max_tokens):
            self.seen = (agent_id, max_tokens)
            return SimpleNamespace(content=MD)

    class LegacyOrchestrator(FakeOrchestrator):
        complete_oneshot = None  # (no public method yet)

    legacy = LegacyOrchestrator()
    port, recorded = Port(), []
    legacy._oneshot_port = lambda: port
    legacy._record_oneshot = lambda who, response: recorded.append(who)
    monkeypatch.setattr(bots_api, "orchestrator", legacy)
    bot = store.lineup()[0]
    assert client.post(f"/api/bots/{bot}/core:refine", json={"text": "x"}).status_code == 200
    assert port.seen == ("moderator", 6000) and recorded == ["roster"]  # (accounted like the v0.13 "complete it" calls)
    del legacy._oneshot_port
    error_of(client.post(f"/api/bots/{bot}/core:refine", json={"text": "x"}), 501, "not_implemented")


# ======================================================================================================================
# memory and proposals
# ======================================================================================================================
def test_the_parts_of_the_memory_can_be_edited_by_the_user(client, store, fake):
    bot = store.lineup()[0]
    init_bot(store, bot)
    scenario = bots_api.current_sources().scenario_id
    data = detail_of(client.put(f"/api/bots/{bot}/memory/arch_notes", json={"content": "  改过的笔记\r\n第二行 "}))
    assert data["memory"]["arch_notes"] == "改过的笔记\n第二行" and data["init"]["status"] == "ready"  # (an edit does not change the initialization)
    for part, content in {"digest": "改过的要点", "questions": "改过的问题"}.items():
        data = detail_of(client.put(f"/api/bots/{bot}/memory/{part}", json={"content": content, "scenario_id": scenario}))
        assert data["memory"][part] == content and data["memory"]["scenario_id"] == scenario
    data = detail_of(client.put(f"/api/bots/{bot}/memory/playbook", json={"content": "- [把握:高|支持:4|反例:1|#夜晚] 改过的一条\n无关的话", "scenario_id": scenario}))
    assert [(e["text"], e["confidence"], e["support"], e["tags"]) for e in data["memory"]["playbook"]] == [("改过的一条", "高", 4, ["夜晚"])]
    assert data["memory"]["playbook_md"] == "## 假设\n- [把握:高|支持:4|反例:1|#夜晚] 改过的一条"
    data = detail_of(client.put(f"/api/bots/{bot}/memory/people", json={"content": "Bob:话少\n* Carol：话多"}))
    assert data["memory"]["people"] == {"Bob": "话少", "Carol": "话多"} and data["memory_stats"]["people"] == 2
    assert fake.changes == [False] * 5
    # the scenario of the game is the default one
    data = detail_of(client.put(f"/api/bots/{bot}/memory/digest", json={"content": "另一个剧本里的要点"}))
    assert data["memory"]["scenario_id"] == "demo" and store.read_part(bot, "digest", "demo") == "另一个剧本里的要点" and store.read_part(bot, "digest", scenario) == "改过的要点"
    assert detail_of(client.put(f"/api/bots/{bot}/memory/digest", json={"content": ""}))["memory"]["digest"] == ""  # (empty clears)


def test_a_part_that_is_over_its_limit_or_not_understood_is_refused(client, store, fake):
    bot = store.lineup()[0]
    store.write_part(bot, "digest", "原来的要点", "demo")
    body = error_of(client.put(f"/api/bots/{bot}/memory/digest", json={"content": "字" * 812}), 400)
    assert body["problems"] == ["「规则要点」最多 800 字（现在 812 字）。"] and body["detail"] == "没有保存：「规则要点」最多 800 字（现在 812 字）。"
    assert store.read_part(bot, "digest", "demo") == "原来的要点"
    many = "".join(f"- [把握:低] 第 {i} 条\n" for i in range(21)) + "- [把握:低] " + "长" * 90
    problems = error_of(client.put(f"/api/bots/{bot}/memory/playbook", json={"content": many}), 400)["problems"]
    assert problems[0] == "打法最多 20 条（现在 22 条）。" and problems[1].startswith("打法第 22 条最多 80 字（现在 90 字）：") and len(problems) == 2
    assert error_of(client.put(f"/api/bots/{bot}/memory/playbook", json={"content": "只是一段话"}), 400)["problems"][0].startswith("没有读出任何一条打法")
    assert error_of(client.put(f"/api/bots/{bot}/memory/people", json={"content": "没有冒号"}), 400)["problems"][0].startswith("没有读出任何一条印象")
    error_of(client.put(f"/api/bots/{bot}/memory/nonsense", json={"content": "x"}), 404, "not_found")
    error_of(client.put(f"/api/bots/{bot}/memory/record", json={"content": "x"}), 404, "not_found")  # (the record is the engine's)
    assert error_of(client.put(f"/api/bots/{bot}/memory/digest", json={"content": "x", "scenario_id": "../x"}), 400)["detail"]
    error_of(client.put("/api/bots/b-ffffffff/memory/digest", json={"content": "x"}), 404, "not_found")
    assert client.put(f"/api/bots/{bot}/memory/digest", json={}).status_code == 422
    assert fake.changes == []


def test_what_a_bot_learned_can_be_cleared_part_by_part_or_all_at_once(client, store, fake):
    bot = store.lineup()[0]
    init_bot(store, bot)
    scenario = bots_api.current_sources().scenario_id
    fake.spec = SimpleNamespace(id=scenario)  # (the game is about the scenario the bot was initialized for)
    store.write_part(bot, "people", "- Bob：话少", scenario)
    data = detail_of(client.delete(f"/api/bots/{bot}/memory?part=people"))
    assert data["memory"]["people"] == {} and data["init"]["status"] == "ready" and data["memory"]["arch_notes"] == INIT_FILES["arch_notes"]
    data = detail_of(client.delete(f"/api/bots/{bot}/memory?part=playbook"))
    assert data["memory"]["playbook"] == [] and data["memory"]["digest"] == INIT_FILES["digest"] and data["memory"]["scenario_id"] == scenario
    store.write_part(bot, "digest", "另一个剧本的", "demo")
    assert detail_of(client.delete(f"/api/bots/{bot}/memory?part=digest&scenario=demo"))["memory"]["digest"] == ""
    assert store.read_part(bot, "digest", scenario) == INIT_FILES["digest"]  # (only that scenario's)
    data = detail_of(client.delete(f"/api/bots/{bot}/memory"))  # (everything it learned, and the initialization with it)
    assert data["init"]["status"] == "uninitialized" and data["memory"]["arch_notes"] == "" and data["memory"]["digest"] == "" and data["core"]["complete"] is True
    assert fake.changes == [False] * 4
    assert "part 必须是" in error_of(client.delete(f"/api/bots/{bot}/memory?part=record"), 400)["detail"]
    error_of(client.delete(f"/api/bots/{bot}/memory?part=digest&scenario=../x"), 400)
    error_of(client.delete("/api/bots/b-ffffffff/memory"), 404, "not_found")


def test_a_proposal_of_a_review_is_accepted_or_rejected_by_the_user(client, store, fake):
    bot = store.lineup()[0]
    init_bot(store, bot)
    old_voice = store.core(bot).voice
    first = store.stage_core(bot, "voice", "更短的说话方式。\n「嗯。」「好。」「再说。」\n篇幅：30–60 字", "接管者的风格", "复盘 g1")
    second = store.stage_core(bot, "mind", "另一种分析方式。", "试试", "复盘 g1")
    data = detail_of(client.get(f"/api/bots/{bot}"))
    assert data["staging"] == store.staging(bot) and [p["section"] for p in data["staging"]] == ["voice", "mind"] and data["staging"][0]["before"] == old_voice
    assert data["init"]["status"] == "ready"  # (a proposal changes nothing until it is accepted)
    data = detail_of(client.post(f"/api/bots/{bot}/staging/{first}:accept"))
    assert data["core"]["sections"]["voice"].startswith("更短的说话方式。") and [p["id"] for p in data["staging"]] == [second]
    assert data["init"]["status"] == "stale" and data["init"]["stale"] == ["core"] and fake.changes == [False]
    data = detail_of(client.post(f"/api/bots/{bot}/staging/{second}:reject"))
    assert data["staging"] == [] and data["core"]["sections"]["mind"] != "另一种分析方式。" and fake.changes == [False, False]
    for action in ("accept", "reject"):
        error_of(client.post(f"/api/bots/{bot}/staging/{first}:{action}"), 404, "not_found")  # (once)
        error_of(client.post(f"/api/bots/{bot}/staging/p-00000000:{action}"), 404, "not_found")
        error_of(client.post(f"/api/bots/b-ffffffff/staging/{first}:{action}"), 404, "not_found")


def test_a_proposal_that_cannot_be_written_stays_on_the_list(client, store):
    bot = store.lineup()[0]
    store.set_core(bot, CoreFiles("身份。", "说话。", "分析。", (("一", "x"), ("二", "x"), ("三", "x"))))
    pid = store.stage_core(bot, "extra:二", "改", "r", "s")
    proposal = json.loads((store.bot_dir(bot) / "staging" / f"{pid}.json").read_text(encoding="utf-8"))
    proposal.update(id="p-11111111", section="extra:新的")
    (store.bot_dir(bot) / "staging" / "p-11111111.json").write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
    body = error_of(client.post(f"/api/bots/{bot}/staging/p-11111111:accept"), 400)
    assert "最多 3 份" in body["problems"][0] and body["detail"].startswith("没有采纳：")
    assert "p-11111111" in [p["id"] for p in store.staging(bot)] and len(store.core(bot).extras) == 3


# ======================================================================================================================
# export and import
# ======================================================================================================================
@pytest.fixture
def codec(monkeypatch) -> FakeCodec:
    fake_codec = FakeCodec()
    monkeypatch.setattr(bots_api, "_codec", lambda: fake_codec)
    return fake_codec


def test_a_bot_is_exported_as_an_arenabot_file(client, store, codec):
    bot = store.lineup()[0]
    store.update_bot(bot, name="小明")
    init_bot(store, bot)
    response = client.get(f"/api/bots/{bot}/export")
    assert response.status_code == 200 and response.headers["content-type"] == "application/octet-stream"
    disposition = response.headers["content-disposition"]
    assert re.fullmatch(r"attachment; filename=\"__-\d{8}-\d{6}\.arenabot\"; filename\*=UTF-8''%E5%B0%8F%E6%98%8E-\d{8}-\d{6}\.arenabot", disposition), disposition
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = sorted(archive.namelist())
        assert json.loads(archive.read("manifest.json"))["bot"] == {"name": "小明", "origin": "default"}
    assert "bot/bot.json" in names and "bot/core/voice.md" in names and "bot/arch/notes.md" in names
    assert list((store.data_dir / "tmp").iterdir()) == []  # (the temporary file is gone once it was sent)
    error_of(client.get("/api/bots/b-ffffffff/export"), 404, "not_found")


def test_exporting_and_importing_say_so_while_the_archive_package_cannot_do_it(client, store, fake, monkeypatch):
    import app.archive as archive_package

    monkeypatch.setitem(sys.modules, "app.archive.arenabot", None)  # (what a missing module looks like)
    monkeypatch.delattr(archive_package, "arenabot", raising=False)
    bot = store.lineup()[0]
    assert "还没有就绪" in error_of(client.get(f"/api/bots/{bot}/export"), 501, "not_implemented")["detail"]
    error_of(client.post(f"/api/bots/{bot}/import", content=b"x"), 501, "not_implemented")
    assert store.name_of(bot) == "Alice" and fake.changes == []


def test_a_file_replaces_a_bot_and_the_old_state_goes_with_it(client, store, fake, codec, tmp_path):
    ids = store.lineup()
    donor_store = BotStore(tmp_path / "donor", seats=3, library_dir=tmp_path / "none")
    donor_store.bootstrap()
    donor = donor_store.lineup()[0]
    donor_store.update_bot(donor, name="Donor")
    donor_store.set_core(donor, CoreFiles("捐赠者的身份。", "说话。", "分析。"))
    donor_store.write_part(donor, "people", "- Alice：爱带节奏", "demo")
    buffer = io.BytesIO()
    codec.pack_arenabot(tmp_path / "donor.arenabot", name="Donor", origin="user", files=donor_store.bot_files(donor))
    payload = (tmp_path / "donor.arenabot").read_bytes()
    buffer.write(payload)
    data = detail_of(client.post(f"/api/bots/{ids[1]}/import?name=donor.arenabot", content=payload))
    assert data["id"] == ids[1] and data["name"] == "Donor" and data["core"]["sections"]["identity"] == "捐赠者的身份。" and fake.changes == [True]
    assert store.lineup() == ids and store.read_part(ids[1], "people") == "- Alice：爱带节奏"
    assert list((store.data_dir / "tmp").iterdir()) == []  # (no upload and no unpacked directory is left)


def test_an_import_that_cannot_be_done_changes_nothing(client, store, fake, codec, tmp_path):
    ids = store.lineup()
    donor_store = BotStore(tmp_path / "donor", seats=3, library_dir=tmp_path / "none")
    donor_store.bootstrap()
    donor = donor_store.lineup()[0]  # named Alice, like the bot in seat 1 of this table
    codec.pack_arenabot(tmp_path / "a.arenabot", name="Alice", origin="user", files=donor_store.bot_files(donor))
    body = error_of(client.post(f"/api/bots/{ids[1]}/import", content=(tmp_path / "a.arenabot").read_bytes()), 409, "name_taken")
    assert "已被 N1 使用" in body["detail"] and store.name_of(ids[1]) == "Bob"
    donor_store.set_core(donor, CoreFiles("身份。", "说话。", "分析。"))
    (donor_store.bot_dir(donor) / "core" / "voice.md").write_text("说" * 700 + "\n", encoding="utf-8")  # (written by hand, past the limit)
    donor_store.update_bot(donor, name="Zed")
    codec.pack_arenabot(tmp_path / "b.arenabot", name="Zed", origin="user", files=donor_store.bot_files(donor))
    body = error_of(client.post(f"/api/bots/{ids[1]}/import", content=(tmp_path / "b.arenabot").read_bytes()), 422)
    assert body["problems"] == ["「说话方式」最多 600 字（现在 700 字）。"] and body["detail"].startswith("这个 bot 文件不能导入：")
    body = error_of(client.post(f"/api/bots/{ids[1]}/import", content=b"this is not a zip"), 422, "invalid_archive")  # (the archive's own error keeps its status and code)
    assert body["detail"] == "这不是有效的 .arenabot 文件。"
    assert "没有收到文件内容" in error_of(client.post(f"/api/bots/{ids[1]}/import", content=b""), 400)["detail"]
    error_of(client.post("/api/bots/b-ffffffff/import", content=b"x"), 404, "not_found")
    assert store.name_of(ids[1]) == "Bob" and fake.changes == [] and list((store.data_dir / "tmp").iterdir()) == []


def test_a_file_over_the_limit_is_refused_before_it_is_read(client, store, codec, monkeypatch):
    bot = store.lineup()[1]
    monkeypatch.setattr(bots_api, "MAX_ARENABOT_BYTES", 1000)
    body = error_of(client.post(f"/api/bots/{bot}/import", content=b"x" * 1001), 413, "too_large")
    assert "20 MiB" in body["detail"]
    assert client.post(f"/api/bots/{bot}/import", content=b"x" * 900).status_code == 422  # (small enough: it is read - and is no archive)
    assert list((store.data_dir / "tmp").iterdir()) == []


def test_a_bot_of_the_default_library_replaces_a_bot(client, store, fake, tmp_path):
    ids = store.lineup()
    for_seat = ids[11]  # Leo
    assert detail_of(client.post(f"/api/bots/{for_seat}/import-default", json={"name": "leo"}))["name"] == "Leo"
    error_of(client.post(f"/api/bots/{for_seat}/import-default", json={"name": "Alice"}), 409, "name_taken")  # (another bot at the table has it)
    error_of(client.post(f"/api/bots/{for_seat}/import-default", json={"name": "Nobody"}), 404, "not_found")
    error_of(client.post("/api/bots/b-ffffffff/import-default", json={"name": "Leo"}), 404, "not_found")
    assert client.post(f"/api/bots/{for_seat}/import-default", json={}).status_code == 422
    # a bot that was renamed can take its default back
    store.update_bot(for_seat, name="Leon")
    data = detail_of(client.post(f"/api/bots/{for_seat}/import-default", json={"name": "Leo"}))
    assert data["name"] == "Leo" and data["core"]["complete"] is True and data["origin"] == "default" and fake.changes == [True, True]


# ======================================================================================================================
# the moderator
# ======================================================================================================================
def test_the_moderators_avatar_is_the_only_thing_to_choose(client, store, fake):
    from app.storage.bots import pick_avatars

    current = store.moderator()["avatar"]
    new = pick_avatars(1, [current, *[store.summary(i)["avatar"] for i in store.lineup()]])[0]
    body = client.put("/api/moderator", json={"avatar": new}).json()
    assert body == {"id": "moderator", "name": "主持人", "avatar": new} and store.moderator()["avatar"] == new and fake.changes == [True]
    body = client.post("/api/moderator/avatar:regenerate").json()
    assert set(body) == {"id", "name", "avatar"} and body["avatar"] not in (new, current) and fake.changes == [True, True]
    error_of(client.put("/api/moderator", json={"avatar": {"v": 1}}), 400)
    assert "只能更换头像" in error_of(client.put("/api/moderator", json={"avatar": new, "name": "x"}), 400)["detail"]
    assert "只能更换头像" in error_of(client.put("/api/moderator", json={"persona": "x"}), 400)["detail"]
    assert "avatar" in error_of(client.put("/api/moderator", json={}), 400)["detail"]
    assert store.moderator()["avatar"] == body["avatar"]


# ======================================================================================================================
# the default model bindings still speak in seats
# ======================================================================================================================
def test_the_bindings_api_speaks_in_seats_and_the_settings_keep_them_by_bot(store, fake, monkeypatch):
    """``/api/settings/bindings`` (app/api/providers.py): keyed by seat id and ``moderator``; stored by bot id (H4)."""
    from app.api import providers as providers_api
    from app.providers.store import get_provider_store

    provider_store = get_provider_store()
    provider = provider_store.create_provider("演示商", "openai_chat", "https://api.example.com/v1", "sk-test-key")
    provider_store.add_model(provider.id, "m1", 1.0, 2.0)
    settings = SettingsStore(store.data_dir)

    class Orch:
        """What ``update_model_bindings`` of the real orchestrator does with the defaults (merge, save by seat)."""

        demo_mode = False

        def __init__(self) -> None:
            self.session = SimpleNamespace(
                status="idle", participants=[SimpleNamespace(id=f"N{i}") for i in range(1, 13)], model_bindings={}
            )

        def get_model_binding(self, actor_id):
            return None

        async def update_model_bindings(self, mapping):
            defaults = settings.get_default_bindings()
            defaults.update(mapping)
            settings.save_default_bindings(defaults)

    monkeypatch.setattr(providers_api, "orchestrator", Orch())
    app = FastAPI()
    app.include_router(providers_api.router)
    http = TestClient(app)
    binding = {"provider_id": provider.id, "model_id": "m1", "reasoning_depth": "low"}
    response = http.put("/api/settings/bindings", json={"bindings": {"N1": binding, "N3": {**binding, "reasoning_depth": "high"}, "moderator": binding}})
    assert response.status_code == 200, response.text
    ids = store.lineup()
    stored = json.loads((store.data_dir / "settings.json").read_text(encoding="utf-8"))["default_bindings"]
    assert set(stored) == {ids[0], ids[2], "moderator"}  # (by bot, not by seat)
    shown = http.get("/api/settings/bindings").json()
    assert sorted(shown["default_bindings"]) == ["N1", "N3", "moderator"] and shown["default_bindings"]["N3"]["reasoning_depth"] == "high"
    # a bot that is moved takes its binding with it
    store.set_lineup([ids[2], ids[1], ids[0], *ids[3:]])
    shown = http.get("/api/settings/bindings").json()
    assert shown["default_bindings"]["N1"]["reasoning_depth"] == "high" and shown["default_bindings"]["N3"]["reasoning_depth"] == "low" and "N2" not in shown["default_bindings"]
    assert http.put("/api/settings/bindings", json={"bindings": {"N13": binding}}).status_code == 400  # (there is no such seat)



# ======================================================================================================================
# export and import with the REAL codec (app/archive/arenabot.py): nothing is faked between the HTTP request and the files
# ======================================================================================================================
def donor_with_everything(store: BotStore) -> str:
    """The bot in seat 1: an L0 of its own, notes, impressions - what a ``.arenabot`` has to carry."""
    donor = store.lineup()[0]
    store.set_core(donor, CoreFiles("捐赠者的身份。", "说话。", "分析。"))
    assert store.write_part(donor, "arch_notes", "先听完再表态。").ok
    assert store.write_part(donor, "people", "- Bob：爱带节奏").ok
    return donor


def test_a_bot_travels_through_the_api_with_the_real_codec(client, store, fake):
    ids = store.lineup()
    donor = donor_with_everything(store)
    exported = client.get(f"/api/bots/{donor}/export")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
    assert {"manifest.json", "bot/bot.json", "bot/core/identity.md", "bot/arch/notes.md", "bot/people.md"} <= names
    assert manifest["format"] == "arenabot" and manifest["bot"] == {"name": "Alice", "origin": "default"}
    assert all(item["path"].startswith("bot/") for item in manifest["files"])
    assert list((store.data_dir / "tmp").iterdir()) == []

    # (the donor gives its name up - its memory goes with a rename: that is confirmed - so that the file's name is free at this table)
    store.update_bot(donor, name="Zed", confirm_reset=True)
    data = detail_of(client.post(f"/api/bots/{ids[1]}/import?name=a.arenabot", content=exported.content))
    assert data["id"] == ids[1] and data["name"] == "Alice" and data["core"]["sections"]["identity"] == "捐赠者的身份。"
    assert store.lineup()[1:] == ids[1:] and store.name_of(store.lineup()[0]) == "Zed", "(the renamed donor has a new id: only seat 1 changed)"
    assert store.read_part(ids[1], "people") == "- Bob：爱带节奏" and store.read_part(ids[1], "arch_notes") == "先听完再表态。"
    assert fake.changes == [True] and list((store.data_dir / "tmp").iterdir()) == []


def zip_without_a_manifest_that_fits(*members: tuple[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members:
            archive.writestr(name, data)
        archive.writestr("manifest.json", "{}")
    return buffer.getvalue()


def test_the_real_codec_refuses_what_is_not_a_bot_file_and_the_bot_stays_as_it_was(client, store, fake, tmp_path):
    from app.archive import arenagame
    from app.storage.games import GameStore

    ids = store.lineup()
    donor = donor_with_everything(store)
    good = client.get(f"/api/bots/{donor}/export").content
    store.update_bot(donor, name="Zed", confirm_reset=True)
    before = {rel: path.read_bytes() for rel, path in store.bot_files(ids[1])}

    # not a zip at all
    body = error_of(client.post(f"/api/bots/{ids[1]}/import", content=b"this is not a zip"), 422, "invalid_archive")
    assert "不是有效的存档文件" in body["detail"]
    # a path that leaves the folder
    body = error_of(client.post(f"/api/bots/{ids[1]}/import", content=zip_without_a_manifest_that_fits(("bot/../../evil.md", b"x"))), 422, "invalid_archive")
    assert ".." in body["detail"]
    # a member the format does not know (a nested archive)
    body = error_of(client.post(f"/api/bots/{ids[1]}/import", content=zip_without_a_manifest_that_fits(("bot/inner.zip", b"PK"))), 422, "invalid_archive")
    assert "不认识的文件" in body["detail"]
    # a game file handed in by mistake says what it is
    games = GameStore(tmp_path / "games")
    from app.game.session import GameSession

    games.save("g1", GameSession(session_id="g1").model_dump(mode="json"))
    arenagame.export_game(games, "g1", tmp_path / "g.arenagame")
    body = error_of(client.post(f"/api/bots/{ids[1]}/import", content=(tmp_path / "g.arenagame").read_bytes()), 422, "invalid_archive")
    assert "这是对局文件" in body["detail"] and "不是 bot 文件" in body["detail"]
    # a file that was changed after it was made (the same length: only the checksum notices)
    changed = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(good)) as source, zipfile.ZipFile(changed, "w", zipfile.ZIP_DEFLATED) as out:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == "bot/people.md":
                data = data.replace("Bob".encode("utf-8"), "Eve".encode("utf-8"))
            out.writestr(info, data)
    body = error_of(client.post(f"/api/bots/{ids[1]}/import", content=changed.getvalue()), 422, "invalid_archive")
    assert "SHA-256" in body["detail"]

    assert {rel: path.read_bytes() for rel, path in store.bot_files(ids[1])} == before and store.name_of(ids[1]) == "Bob"
    assert fake.changes == [] and list((store.data_dir / "tmp").iterdir()) == []


# ======================================================================================================================
# what the roster API's tests used to guard: a lone surrogate in a request is a plain 400, never a 500 at the write
# ======================================================================================================================
@pytest.mark.parametrize(
    "path, raw",
    [
        ("", b'{"name": "Zed\\ud800"}'),
        ("", b'{"name": "\\udfff"}'),
        ("/memory/people", b'{"content": "- Bob\\uff1aa\\ud800"}'),
        ("/memory/arch_notes", b'{"content": "ok\\ud83d"}'),
        ("/core", b'{"markdown": "---\\nformat: arena-core\\nversion: 1\\nname: A\\n---\\n# \\u8eab\\u4efd\\n\\ud800"}'),
    ],
)
def test_a_lone_surrogate_in_a_request_is_a_plain_400_and_changes_nothing(client, store, fake, path, raw):
    bot = store.lineup()[1]
    before = {rel: p.read_bytes() for rel, p in store.bot_files(bot)}
    method = client.put
    response = method(f"/api/bots/{bot}{path}", content=raw, headers={"content-type": "application/json"})
    assert response.status_code in (400, 422), response.text
    assert isinstance(response.json().get("detail"), str) and response.json()["detail"]
    assert {rel: p.read_bytes() for rel, p in store.bot_files(bot)} == before and fake.changes == []
