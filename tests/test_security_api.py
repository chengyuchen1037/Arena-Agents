"""Security regression tests for the HTTP API (v0.9, review items 1-4, 9, 10, 13, 23)."""

from __future__ import annotations

import copy
import json
import os
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from app.api import routes, security
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
    "ARENA_MAX_IMPORT_BYTES",
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
    assert response.json() == {"status": "ok", "version": "0.12.0"}
    assert app.version == "0.12.0"


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
    response = remote_client.post("/api/session/import", content=b"{}")
    assert response.status_code == 403
    response = remote_client.post("/api/sessions/abc/load")
    assert response.status_code == 403
    fake.load_session_data.assert_not_awaited()
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


def test_save_refuses_traversal_session_id_and_never_writes_outside_saves(client, fake, tmp_path):
    # model_copy skips validation, simulating a session that bypassed the validator.
    for bad in ("../../escape", "..\\..\\escape"):
        fake.session = GameSession().model_copy(update={"session_id": bad})
        response = client.post("/api/session/save")
        assert response.status_code == 400
        assert_string_detail(response)
    assert not any(tmp_path.rglob("*escape*"))
    fake.persist_session.assert_not_awaited()


def test_save_goes_through_external_store_and_returns_no_server_path(client, fake, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    response = client.post("/api/session/save")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ok", "filename", "session"}
    assert body["filename"].endswith("_sec-test.json")
    assert str(tmp_path) not in response.text
    fake.persist_session.assert_awaited_once_with("manual")
    # nothing is written next to the code any more
    assert list(tmp_path.iterdir()) == []


def test_save_failure_is_reported_not_swallowed(client, fake):
    from sec_api_fakes import StorageError

    fake.persist_session = AsyncMock(side_effect=StorageError("主密钥不可用，无法保存"))
    response = client.post("/api/session/save")
    assert response.status_code == 503
    assert "主密钥" in assert_string_detail(response)


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
def real_spec() -> dict:
    return copy.deepcopy(load_scenario_v2("scenarios/minimal_test.md").model_dump(mode="json"))


def valid_session_payload() -> dict:
    session = GameSession(session_id="imported-1", title="Imported", scenario_spec=real_spec())
    return json.loads(session.model_dump_json())


def post_import(client, payload) -> object:
    data = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode()
    return client.post(
        "/api/session/import", content=data, headers={"Content-Type": "application/json"}
    )


def assert_import_rejected(client, fake, payload, status=(400, 422)) -> str:
    before = fake.session
    response = post_import(client, payload)
    assert response.status_code in status, response.text
    detail = assert_string_detail(response)
    fake.load_session_data.assert_not_awaited()
    fake.force_pause.assert_not_awaited()
    assert fake.session is before
    return detail


def test_import_valid_payload_succeeds_and_hides_internals(client, fake):
    payload = valid_session_payload()
    payload["world"] = {"marker": "SECRET-WORLD-MARKER"}
    response = post_import(client, {"session": payload})
    assert response.status_code == 200
    fake.load_session_data.assert_awaited_once()
    assert "SECRET-WORLD-MARKER" not in response.text
    assert "world" not in response.json()["session"]


def test_import_never_overwrites_an_existing_stored_session(client, fake, env):
    env.sessions.sessions["imported-1"] = {"session_id": "imported-1"}
    response = post_import(client, valid_session_payload())
    assert response.status_code == 200
    assert response.json()["renamed_from"] == "imported-1"
    loaded = fake.load_session_data.await_args.args[0]
    assert loaded["session_id"] != "imported-1"
    assert loaded["session_id"].isalnum()
    assert env.sessions.sessions == {"imported-1": {"session_id": "imported-1"}}


def test_import_with_a_real_scenario_spec_succeeds(client, fake):
    assert post_import(client, valid_session_payload()).status_code == 200
    fake.load_session_data.assert_awaited_once()


def test_import_of_an_old_engine_save_is_refused_with_an_explanation(client, fake):
    payload = valid_session_payload()
    payload["schema_version"] = 1
    detail = assert_import_rejected(client, fake, payload, status=(400,))
    assert "旧版本" in detail


def test_import_too_large_rejected_without_touching_game(client, fake, monkeypatch):
    monkeypatch.setenv("ARENA_MAX_IMPORT_BYTES", "2000")
    payload = valid_session_payload()
    payload["title"] = "x" * 5000
    response = post_import(client, payload)
    assert response.status_code == 413
    assert_string_detail(response)
    fake.load_session_data.assert_not_awaited()


def test_import_default_limit_is_20_mb(monkeypatch):
    assert security.max_import_bytes() == 20 * 1024 * 1024
    monkeypatch.setenv("ARENA_MAX_IMPORT_BYTES", "garbage")
    assert security.max_import_bytes() == 20 * 1024 * 1024
    monkeypatch.setenv("ARENA_MAX_IMPORT_BYTES", "1234")
    assert security.max_import_bytes() == 1234


def test_import_streamed_body_over_limit_rejected(client, fake, monkeypatch):
    monkeypatch.setenv("ARENA_MAX_IMPORT_BYTES", "1000")

    def chunks():
        for _ in range(5):
            yield b" " * 400  # no Content-Length header: chunked upload

    response = client.post("/api/session/import", content=chunks())
    assert response.status_code == 413
    fake.load_session_data.assert_not_awaited()


@pytest.mark.parametrize("raw", [b"not json", b"", b"\xff\xfe", b"[1,2,3]", b'"string"', b"null", b"[" * 5000])
def test_import_malformed_json_rejected(client, fake, raw):
    assert_import_rejected(client, fake, raw, status=(400,))


@pytest.mark.parametrize(
    "mutation",
    [
        {"session_id": "../../etc/passwd"},
        {"session_id": "a/b"},
        {"status": "exploding"},
        {"participants": "nope"},
        {"participants": [{"bogus": 1}]},
        {"usage_stats": "many"},
        {"model_bindings": {"p01": {"provider_id": 1}}},
    ],
)
def test_import_invalid_session_structure_rejected(client, fake, mutation):
    payload = valid_session_payload()
    payload.update(mutation)
    assert_import_rejected(client, fake, payload, status=(400,))


@pytest.mark.parametrize(
    "scenario_spec",
    [
        [],
        {},
        {"id": "x"},
        {"id": "", "version": "1"},
        {"id": "x", "version": "1", "schema_version": 1},
        {"id": "x", "version": "1", "templates": {"t": {"text": "x", "users": "everyone"}}},
        {"id": "x", "version": "1", "setup": {"identity_pool": [{"role": "ghost", "count": 1}]}},
        {"id": "x", "version": "1", "unexpected_key": {"a": 1}},
        {"id": "x", "version": "1", "defaults": {"floor": {"duration_s": "soon"}}},
    ],
)
def test_import_invalid_scenario_spec_rejected(client, fake, scenario_spec):
    payload = valid_session_payload()
    payload["scenario_spec"] = scenario_spec
    assert_import_rejected(client, fake, payload, status=(400,))


def test_import_deeply_nested_or_unknown_spec_content_rejected(client, fake):
    deep: dict = {}
    cursor = deep
    for _ in range(100):
        cursor["x"] = {}
        cursor = cursor["x"]
    spec = real_spec()
    spec["extra"] = deep  # extra="forbid": nothing unknown gets through, however it is nested
    payload = valid_session_payload()
    payload["scenario_spec"] = spec
    assert_import_rejected(client, fake, payload, status=(400,))


def test_engine_side_value_error_is_mapped_to_400(client, fake):
    fake.load_session_data = AsyncMock(side_effect=ScenarioError("engine refused"))
    response = post_import(client, valid_session_payload())
    assert response.status_code == 400
    assert "engine refused" in assert_string_detail(response)


def test_failed_import_leaves_real_orchestrator_game_untouched(monkeypatch):
    """End to end against the real singleton: a rejected import changes nothing."""

    from app.game.orchestrator import orchestrator as real

    monkeypatch.setattr(routes, "orchestrator", real)
    real_client = TestClient(app)
    session_before = real.session
    snapshot_before = session_before.model_dump_json()
    status_before = session_before.status
    forged = valid_session_payload()
    forged["session_id"] = "../../escape"
    assert post_import(real_client, forged).status_code == 400
    forged = valid_session_payload()
    forged["scenario_spec"] = {"id": "x", "version": "1", "schema_version": 1}
    assert post_import(real_client, forged).status_code == 400
    assert real.session is session_before
    assert real.session.status == status_before
    assert real.session.model_dump_json() == snapshot_before


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
