"""``/api/contract`` (contract §13.7, the "files" table): read, download, replace, restore the default; success and every error.

The API runs in a small app of its own with a fake orchestrator that records ``sources_changed`` calls; the stores (contract, rules, bots)
are the real ones on the isolated data directory of the tests, so "a change makes every bot stale" is checked through the real hashes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.agents.contract import required_terms
from app.api import bots as bots_api
from app.api import contract as contract_api
from app.bots.types import sha256_text
from app.storage.bots import get_bot_store
from app.storage.contract import get_contract_store
from app.storage.errors import StorageError

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = (ROOT / "prompts" / "contract.md").read_text(encoding="utf-8")
INIT_FILES = {
    "arch_notes": "我的操作笔记。",
    "digest": "我的规则要点。",
    "playbook": "- [把握:中|支持:1|反例:0|来源:初始化|#投票] 先看票型。",
}


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
    app.include_router(contract_api.router)

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
    monkeypatch.setattr(contract_api, "orchestrator", orch)
    return orch


@pytest.fixture
def client(fake) -> TestClient:
    return TestClient(make_app())


def custom(version: str = "v1.2.3") -> str:
    return SHIPPED.replace("version: v0.14.0", f"version: {version}")


def error_of(response, status: int, code: str | None = None) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert isinstance(body["detail"], str) and body["detail"]
    if code is not None:
        assert body.get("code") == code, body
    return body


# ======================================================================================================================
# reading
# ======================================================================================================================
def test_the_contract_in_force_with_what_the_ui_shows(client):
    body = client.get("/api/contract").json()
    assert set(body) == {"markdown", "source", "chars", "must_read_chars", "required_terms", "problems"}
    assert body["markdown"] == SHIPPED and body["chars"] == len(SHIPPED) and 0 < body["must_read_chars"] <= 1200
    assert body["source"] == {"origin": "default", "version": "v0.14.0", "imported_at": None, "sha256": sha256_text(SHIPPED)}
    assert body["required_terms"] == required_terms() and "speak" in body["required_terms"] and "TOO_LONG" in body["required_terms"]
    assert body["problems"] == []


def test_the_download_is_the_file_with_a_safe_name(client):
    response = client.get("/api/contract?download=1")
    assert response.status_code == 200 and response.content.decode("utf-8") == SHIPPED
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.headers["content-disposition"] == "attachment; filename=\"contract-v0.14.0.md\"; filename*=UTF-8''contract-v0.14.0.md"
    client.put("/api/contract", json={"markdown": SHIPPED.replace("version: v0.14.0\n", "")})  # (a contract that names no version)
    name = client.get("/api/contract?download=1").headers["content-disposition"]
    assert re.fullmatch(r"attachment; filename=\"contract-custom-\d{8}\.md\"; filename\*=UTF-8''contract-custom-\d{8}\.md", name), name
    client.put("/api/contract", json={"markdown": custom("v2.0.0-rc.1")})
    assert "contract-v2.0.0-rc.1.md" in client.get("/api/contract?download=1").headers["content-disposition"]


def test_a_contract_edited_by_hand_into_something_unusable_is_shown_with_its_problems(client, isolated_external_storage: Path):
    get_contract_store()
    path = isolated_external_storage / "contract" / "contract.md"
    path.write_text(SHIPPED.replace("TOO_LONG", "TOO_LNG"), encoding="utf-8", newline="\n")
    body = client.get("/api/contract").json()
    assert body["source"]["origin"] == "imported" and any("TOO_LONG" in p for p in body["problems"])
    assert client.get("/api/contract?download=1").status_code == 200  # (it can still be downloaded to be fixed)
    path.unlink()
    body = client.get("/api/contract").json()
    assert body["markdown"] == "" and body["chars"] == 0 and body["must_read_chars"] == 0 and body["problems"] == ["官方契约是空的，请导入契约或恢复默认。"]
    assert error_of(client.get("/api/contract?download=1"), 404, "not_found")["detail"] == "官方契约是空的，没有可以下载的文件。"
    assert client.post("/api/contract:reset").json()["markdown"] == SHIPPED  # (and "恢复默认" brings it back)


# ======================================================================================================================
# replacing
# ======================================================================================================================
def test_a_valid_contract_replaces_the_current_one_and_the_orchestrator_is_told(client, fake):
    body = client.put("/api/contract", json={"markdown": "\ufeff" + custom().replace("\n", "\r\n")})
    assert body.status_code == 200
    data = body.json()
    assert data["markdown"] == custom() and data["source"]["origin"] == "imported" and data["source"]["version"] == "v1.2.3"
    assert isinstance(data["source"]["imported_at"], int) and data["source"]["sha256"] == sha256_text(custom()) and data["problems"] == []
    assert fake.calls == [("contract", None)]
    assert client.get("/api/contract").json() == data


def test_every_bot_becomes_stale_when_the_contract_changes(client):
    store = get_bot_store()
    bot = store.lineup()[0]
    sources = bots_api.current_sources()
    store.commit_init(bot, sources.scenario_id, INIT_FILES, model="m", duration_s=1, sources=sources)
    assert store.summary(bot, sources=bots_api.current_sources())["init"]["status"] == "ready"
    assert client.put("/api/contract", json={"markdown": custom()}).status_code == 200
    init = store.summary(bot, sources=bots_api.current_sources())["init"]
    assert init["status"] == "stale" and init["stale"] == ["contract"]
    assert client.post("/api/contract:reset").status_code == 200  # (the same text as at the initialization: ready again)
    assert store.summary(bot, sources=bots_api.current_sources())["init"]["status"] == "ready"


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda t: "", "契约是空的"),
        (lambda t: t.split("---\n", 2)[2], "缺少格式头"),
        (lambda t: t.replace("## 必读", "## 别的"), "缺少“## 必读”一节"),
        (lambda t: t.replace("## 必读\n", "## 必读\n" + "这是一段很长的话。" * 140 + "\n\n", 1), "“必读”最多 1200 字"),
        (lambda t: re.sub(r"(?<![A-Za-z0-9_])jot(?![A-Za-z0-9_])", "jxt", t), "契约没有提到这些工具：jot"),
        (lambda t: t.replace("TOO_LONG", "TOO_LNG"), "契约没有提到这些回执码：TOO_LONG"),
    ],
)
def test_a_contract_that_cannot_be_used_is_refused_with_its_problems(client, fake, mutate, fragment):
    response = client.put("/api/contract", json={"markdown": mutate(SHIPPED)})
    body = error_of(response, 400, "invalid")
    assert any(fragment in problem for problem in body["problems"]), body
    assert body["detail"].startswith("契约没有保存：") and fragment in body["detail"]
    assert client.get("/api/contract").json()["markdown"] == SHIPPED and fake.calls == []  # (nothing written, nobody told)


def test_several_problems_are_counted_and_all_listed(client):
    text = SHIPPED.replace("format: arena-contract", "format: other").replace("TOO_LONG", "TOO_LNG")
    body = error_of(client.put("/api/contract", json={"markdown": text}), 400)
    assert len(body["problems"]) == 2 and body["detail"].startswith("契约没有保存：共 2 处问题：") and all(p in body["detail"] for p in body["problems"])


def test_a_request_that_is_not_a_contract_is_a_422(client):
    assert client.put("/api/contract", json={}).status_code == 422
    assert client.put("/api/contract", json={"markdown": 5}).status_code == 422
    assert client.put("/api/contract", json={"markdown": SHIPPED, "extra": 1}).status_code == 422
    assert client.put("/api/contract", json=["a list"]).status_code == 422
    assert client.put("/api/contract", content=b"not json", headers={"content-type": "application/json"}).status_code == 422
    assert client.put("/api/contract", json={"markdown": "x" * 2_000_001}).status_code == 422  # (a sane cap on the body)
    assert client.get("/api/contract").json()["markdown"] == SHIPPED


# ======================================================================================================================
# restoring the default
# ======================================================================================================================
def test_the_default_comes_back(client, fake):
    client.put("/api/contract", json={"markdown": custom()})
    fake.calls.clear()
    data = client.post("/api/contract:reset").json()
    assert data["markdown"] == SHIPPED and data["source"] == {"origin": "default", "version": "v0.14.0", "imported_at": None, "sha256": sha256_text(SHIPPED)}
    assert fake.calls == [("contract", None)]
    assert client.post("/api/contract:reset").status_code == 200  # (idempotent)


def test_a_default_that_is_missing_cannot_be_restored(client, fake, monkeypatch):
    store = get_contract_store()
    monkeypatch.setattr(type(store), "default_path", lambda self: store.data_dir / "no-such-default.md")
    body = error_of(client.post("/api/contract:reset"), 503, "default_missing")
    assert "找不到默认契约文件" in body["detail"] and body["problems"] and fake.calls == []


# ======================================================================================================================
# the orchestrator hook
# ======================================================================================================================
def test_a_hook_that_fails_never_fails_the_request(client, fake, caplog):
    fake.fail = True
    with caplog.at_level("WARNING"):
        response = client.put("/api/contract", json={"markdown": custom()})
    assert response.status_code == 200 and response.json()["source"]["version"] == "v1.2.3"
    assert fake.calls == [("contract", None)] and "sources_changed failed after a change of the contract" in caplog.text


def test_an_orchestrator_without_the_hook_is_fine(monkeypatch):
    monkeypatch.setattr(contract_api, "orchestrator", object())
    http = TestClient(make_app())
    assert http.put("/api/contract", json={"markdown": custom()}).status_code == 200
    assert http.post("/api/contract:reset").status_code == 200


# ======================================================================================================================
# who may do what
# ======================================================================================================================
def test_reading_is_open_but_every_change_needs_a_trusted_client(fake, monkeypatch):
    stranger = TestClient(make_app(), client=("203.0.113.5", 50000))
    assert stranger.get("/api/contract").status_code == 200 and stranger.get("/api/contract?download=1").status_code == 200
    assert stranger.put("/api/contract", json={"markdown": custom()}).status_code == 403
    assert stranger.post("/api/contract:reset").status_code == 403
    assert fake.calls == [] and stranger.get("/api/contract").json()["markdown"] == SHIPPED
    # with an API token the token decides: nobody without it may read, a stranger with it may change
    monkeypatch.setenv("ARENA_API_TOKEN", "t0ken-value")
    assert stranger.get("/api/contract").status_code == 401
    headers = {"X-Arena-Token": "t0ken-value"}
    assert stranger.put("/api/contract", json={"markdown": custom()}, headers=headers).status_code == 200
    assert stranger.post("/api/contract:reset", headers=headers).status_code == 200
    # a request from another origin is refused whatever the token
    assert stranger.put("/api/contract", json={"markdown": custom()}, headers={**headers, "Origin": "http://evil.example"}).status_code == 403
