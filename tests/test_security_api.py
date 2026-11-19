"""Security regression tests for the HTTP API (v0.9, review items 1-4, 9, 10, 13, 23)."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from app.api import security
from app.game.session import GameSession
from app.main import app
from app.world.loader import load_scenario_v2
from app.world.spec import ScenarioError, ScenarioSpec
from sec_api_fakes import install

TOKEN = "s3cret-token-value"
SECURITY_ENV = (
    "ARENA_API_TOKEN",
    "ARENA_ALLOWED_ORIGINS",
    "ARENA_ALLOWED_BASE_HOSTS",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "ARENA_ALLOW_PRIVATE_PROVIDERS",
    "HOST",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in SECURITY_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def env(monkeypatch):
    """Replace the orchestrator singleton and the provider/session stores with fakes."""

    return install(monkeypatch)


@pytest.fixture
def fake(env):
    return env.orch


@pytest.fixture
def client(fake):
    return TestClient(app)


@pytest.fixture
def remote_client(fake):
    return TestClient(app, client=("203.0.113.5", 50000))


def assert_string_detail(response) -> str:
    body = response.json()
    assert set(body) == {"detail"}
    assert isinstance(body["detail"], str) and body["detail"]
    return body["detail"]


# ---------------------------------------------------------------- health
def test_health_is_unauthenticated_and_minimal(client, monkeypatch):
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    response = client.get("/api/health")
    assert response.status_code == 200
    from app.config import APP_VERSION

    assert response.json() == {"status": "ok", "version": APP_VERSION}
    assert app.version == APP_VERSION


# ------------------------------------------------------------- token auth
def test_no_token_configured_leaves_api_open(client):
    assert client.get("/api/state").status_code == 200


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_token_counts_as_unset(client, monkeypatch, value):
    monkeypatch.setenv("ARENA_API_TOKEN", value)
    assert client.get("/api/state").status_code == 200


def test_token_required_missing_wrong_and_correct(client, monkeypatch):
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)

    missing = client.get("/api/state")
    assert missing.status_code == 401
    assert_string_detail(missing)

    wrong = client.get("/api/state", headers={"X-Arena-Token": "nope"})
    assert wrong.status_code == 401
    assert_string_detail(wrong)

    ok = client.get("/api/state", headers={"X-Arena-Token": TOKEN})
    assert ok.status_code == 200

    post_missing = client.post("/api/control/pause")
    assert post_missing.status_code == 401
    post_ok = client.post("/api/control/pause", headers={"X-Arena-Token": TOKEN})
    assert post_ok.status_code == 200


def test_query_token_only_accepted_for_events_get(client, monkeypatch):
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    # ?token= does not authenticate ordinary endpoints
    assert client.get(f"/api/state?token={TOKEN}").status_code == 401
    assert client.post(f"/api/control/pause?token={TOKEN}").status_code == 401
    # ...and a wrong token on the SSE endpoint is rejected
    assert client.get("/api/events?token=wrong").status_code == 401

    def make_request(path: str, method: str, query: bytes) -> Request:
        return Request(
            {
                "type": "http",
                "method": method,
                "path": path,
                "query_string": query,
                "headers": [],
            }
        )

    assert security.check_token(make_request("/api/events", "GET", f"token={TOKEN}".encode())) is True
    with pytest.raises(HTTPException) as excinfo:
        security.check_token(make_request("/api/state", "GET", f"token={TOKEN}".encode()))
    assert excinfo.value.status_code == 401


def test_static_and_index_do_not_need_token(client, monkeypatch):
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    assert client.get("/").status_code in (200, 404)  # never 401
    assert client.get("/static/does-not-exist.js").status_code == 404


# ---------------------------------------------------------- origin check
def test_cross_origin_post_is_rejected(client):
    response = client.post("/api/control/pause", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403
    assert_string_detail(response)


def test_same_origin_and_no_origin_post_allowed(client):
    assert client.post("/api/control/pause", headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post("/api/control/pause").status_code == 200


def test_null_origin_rejected_and_get_not_checked(client):
    assert client.post("/api/control/pause", headers={"Origin": "null"}).status_code == 403
    assert client.get("/api/state", headers={"Origin": "http://evil.example"}).status_code == 200


def test_allowed_origins_env(client, monkeypatch):
    monkeypatch.setenv("ARENA_ALLOWED_ORIGINS", "https://arena.example.com, http://localhost:3000")
    headers = {"Origin": "https://arena.example.com"}
    assert client.post("/api/control/pause", headers=headers).status_code == 200
    headers = {"Origin": "http://localhost:3000"}
    assert client.post("/api/control/pause", headers=headers).status_code == 200
    headers = {"Origin": "https://other.example.com"}
    assert client.post("/api/control/pause", headers=headers).status_code == 403


def test_cross_origin_rejected_even_with_valid_token(client, monkeypatch):
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    response = client.post(
        "/api/control/pause",
        headers={"X-Arena-Token": TOKEN, "Origin": "http://evil.example"},
    )
    assert response.status_code == 403


# ------------------------------------------------------ loopback limiting
def test_remote_client_cannot_use_mutating_config_endpoints_without_token(remote_client, fake):
    assert remote_client.get("/api/state").status_code == 200  # read-only stays open
    assert remote_client.post("/api/control/pause").status_code == 200  # game control stays open

    response = remote_client.post("/api/config/llm", json={"model": "x"})
    assert response.status_code == 403
    assert_string_detail(response)
    assert "DEEPSEEK_MODEL" not in os.environ


def test_remote_client_with_token_may_configure(remote_client, monkeypatch, env):
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    response = remote_client.post(
        "/api/providers",
        json={
            "name": "P",
            "protocol": "openai_chat",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-test-key-0001",
        },
        headers={"X-Arena-Token": TOKEN},
    )
    assert response.status_code == 201


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("testclient", True),
        ("localhost", True),
        (None, True),
        ("192.168.1.5", False),
        ("172.17.0.1", False),
        ("203.0.113.5", False),
        ("evil.example", False),
    ],
)
def test_is_loopback_host(host, expected):
    assert security.is_loopback_host(host) is expected


# ------------------------------------------- legacy /config/llm (v0.10)
def test_legacy_config_post_is_gone_and_never_touches_env_or_disk(client, fake, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    response = client.post(
        "/api/config/llm", json={"api_key": "sk-must-not-be-used", "model": "x"}
    )
    assert response.status_code == 410
    assert "/api/providers" in assert_string_detail(response)
    assert "DEEPSEEK_API_KEY" not in os.environ
    assert not list(tmp_path.rglob(".env"))
    fake.reload_config.assert_not_called()


def test_legacy_config_get_is_derived_from_moderator_binding_without_key(client, env):
    from sec_api_fakes import SECRET_KEY, bind

    provider = env.providers.seed(models=("m1",))
    env.session.model_bindings = {"moderator": bind(provider.id, "m1", "low")}
    body = client.get("/api/config/llm").json()
    assert body["configured"] is True
    assert body["api_key_set"] is True
    assert body["model"] == "m1"
    assert body["reasoning_effort"] == "low"
    assert body["provider_id"] == provider.id
    assert "masked_api_key" not in body
    assert SECRET_KEY not in json.dumps(body)


def test_legacy_config_get_unconfigured(client, env):
    body = client.get("/api/config/llm").json()
    assert body["configured"] is False
    assert body["api_key_set"] is False


# ------------------------------------------------------------ validation
def test_pydantic_422_is_a_plain_string_without_input_echo(client):
    response = client.post(
        "/api/providers",
        json={
            "name": "x",
            "protocol": "nope-sk-leak-1234",
            "base_url": "https://api.example.com",
            "api_key": "sk-leak-5678",
        },
    )
    assert response.status_code == 422
    detail = assert_string_detail(response)
    assert "protocol" in detail
    assert "sk-leak" not in detail

    response = client.put("/api/roster/N1", content=b"[]", headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert_string_detail(response)


def test_unknown_route_detail_is_string(client):
    response = client.get("/api/nope")
    assert response.status_code == 404
    assert_string_detail(response)


# -------------------------------------------------------- session_id/save
@pytest.mark.parametrize(
    "bad",
    ["../evil", "..\\evil", "a/b", "a b", "", "x" * 65, "evil.json", "C:\\x", "a\nb", "é"],
)
def test_session_id_validator_rejects_path_like_values(bad):
    with pytest.raises(ValidationError):
        GameSession(session_id=bad)


@pytest.mark.parametrize("good", ["abc123", "config-test", "a_b-C", "x" * 64, "saved_f0a371c49908"])
def test_session_id_validator_accepts_safe_values(good):
    assert GameSession(session_id=good).session_id == good


def test_default_session_id_is_valid():
    assert GameSession().session_id


def test_there_is_no_save_endpoint_any_more_the_game_saves_itself(client, fake, tmp_path, monkeypatch):
    """v0.13 (F26): no "保存" button and no ``/api/session/save`` - every change is saved within half a second."""
    monkeypatch.chdir(tmp_path)
    response = client.post("/api/session/save")
    assert response.status_code in (404, 405)
    fake.persist_session.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []  # nothing is written next to the code


# ------------------------------------------------------------ new game
def test_new_game_without_body_uses_scenario_default(client, fake):
    assert client.post("/api/control/new").status_code == 200
    fake.new_game.assert_awaited_once_with(randomize_identities=None)
    fake.new_game.reset_mock()
    assert client.post("/api/control/new", json={"randomize_identities": False}).status_code == 200
    fake.new_game.assert_awaited_once_with(randomize_identities=False)


def test_new_game_scenario_error_is_400(client, fake):
    fake.new_game = AsyncMock(side_effect=ScenarioError("broken scenario"))
    response = client.post("/api/control/new")
    assert response.status_code == 400
    assert "broken scenario" in assert_string_detail(response)


def test_new_game_config_reload_error_is_400(client, fake):
    fake.new_game = AsyncMock(side_effect=RuntimeError("config.yml unreadable"))
    response = client.post("/api/control/new")
    assert response.status_code == 400
    assert "config.yml unreadable" in assert_string_detail(response)


# ---------------------------------------------------------------- import
def test_there_is_no_single_game_import_any_more(client, fake):
    """v0.13 (F26): a game comes back through the history (``/api/games``) or an archive import, never as a pasted JSON."""
    for path in ("/api/session/import", "/api/sessions/abc/load"):
        response = client.post(path, content=b"{}", headers={"Content-Type": "application/json"})
        assert response.status_code in (404, 405), path
    fake.load_session_data.assert_not_awaited()


# -------------------------------------------------- scenario spec unit
def test_real_scenario_spec_roundtrips_through_json():
    spec = load_scenario_v2("scenarios/minimal_test.md")
    restored = ScenarioSpec.model_validate(json.loads(json.dumps(spec.model_dump(mode="json"))))
    assert restored == spec


# -------------------------------------------------------- startup warning
@pytest.mark.parametrize(
    "host,token,expected",
    [
        ("0.0.0.0", "", True),
        ("::", "", True),
        ("192.168.1.10", "", True),
        ("0.0.0.0", TOKEN, False),
        ("127.0.0.1", "", False),
        ("localhost", "", False),
        ("::1", "", False),
        ("", "", False),
    ],
)
def test_startup_warning_only_when_exposed_without_token(monkeypatch, host, token, expected):
    monkeypatch.setenv("HOST", host)
    monkeypatch.setenv("ARENA_API_TOKEN", token)
    assert security.warn_if_exposed() is expected


def test_startup_warning_is_logged(monkeypatch, caplog):
    monkeypatch.setenv("HOST", "0.0.0.0")
    with caplog.at_level("WARNING", logger="uvicorn.error"):
        security.warn_if_exposed()
    assert any("ARENA_API_TOKEN" in r.getMessage() for r in caplog.records)
