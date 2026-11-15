"""Workspace API security tests (v0.11).

The workspace routes act on the server's own filesystem, so:

* ``POST /api/workspaces/pick-native-folder`` and ``POST /api/workspaces/select`` are loopback
  only - even a valid API token must not let a remote client choose a server folder;
* read responses (``GET /api/workspaces`` and ``GET /api/state``) never contain absolute paths
  for non-loopback clients;
* a running game refuses the switch with 409 and the current workspace is preserved;
* without workspace support in the storage layer the write endpoints answer a clear 503.

Everything runs against the in-memory fake module from ``sec_api_fakes``; no real workspace,
user directory or disk is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import workspaces as workspace_api
from app.main import app
from sec_api_fakes import install

TOKEN = "s3cret-token-value"
ENV_NAMES = (
    "ARENA_API_TOKEN",
    "ARENA_ALLOWED_ORIGINS",
    "ARENA_DISABLE_WORKSPACE",
    "ARENA_WORKSPACE_DIR",
    "ARENA_ALLOW_PRIVATE_PROVIDERS",
    "HOST",
)
_ROOT = Path("C:/fake-arena-workspaces")
FAKE_ROOT = str(_ROOT)


def fake_path(*parts: str) -> str:
    return str(_ROOT.joinpath(*parts))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def env(monkeypatch):
    return install(monkeypatch)


@pytest.fixture
def client(env):
    return TestClient(app)


@pytest.fixture
def remote(env):
    return TestClient(app, client=("203.0.113.5", 50000))


@pytest.fixture
def ready(env):
    """Workspace support enabled with an active workspace."""
    env.workspace.set_enabled(True)
    return env


def detail(response) -> str:
    body = response.json()
    assert set(body) == {"detail"}
    assert isinstance(body["detail"], str) and body["detail"]
    return body["detail"]


def _keys(payload) -> set[str]:
    if isinstance(payload, dict):
        found = {str(key) for key in payload}
        for value in payload.values():
            found |= _keys(value)
        return found
    if isinstance(payload, list):
        found: set[str] = set()
        for value in payload:
            found |= _keys(value)
        return found
    return set()


def assert_no_server_paths(response) -> None:
    payload = response.json()
    assert not any("path" in key.lower() for key in _keys(payload))
    assert "fake-arena-workspaces" not in json.dumps(payload)


# ------------------------------------------------------------------ listing
def test_workspaces_reports_disabled_when_the_feature_is_off(client, env):
    response = client.get("/api/workspaces")
    assert response.status_code == 200
    assert response.json() == {"enabled": False, "current": None, "recent": [], "native_picker": False}


def test_local_workspace_listing_includes_current_recent_and_default(ready, client):
    response = client.get("/api/workspaces")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["native_picker"] is True
    assert body["default_path"] == fake_path("Default")
    current = body["current"]
    assert current["id"] == "ws-test"
    assert current["name"] == "Test Workspace"
    assert current["path"] == fake_path("Test")
    assert current["last_session"] == "sec-test"
    assert current["status"] == "ok"
    assert [item["name"] for item in body["recent"]] == ["Test Workspace", "Other Workspace"]
    assert all("path" in item for item in body["recent"])


def test_remote_workspace_listing_never_contains_absolute_paths(ready, remote, monkeypatch):
    response = remote.get("/api/workspaces")
    assert response.status_code == 200
    assert_no_server_paths(response)
    body = response.json()
    assert body["enabled"] is True
    assert body["native_picker"] is False
    assert "default_path" not in body
    assert "path" not in body["current"]
    assert all("path" not in item for item in body["recent"])
    assert body["current"]["name"] == "Test Workspace"  # names/ids stay visible

    # A configured token authenticates the remote reader, but still hides server paths.
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    with_token = remote.get("/api/workspaces", headers={"X-Arena-Token": TOKEN})
    assert with_token.status_code == 200
    assert_no_server_paths(with_token)


def test_state_workspace_summary_hides_paths_from_remote_readers(ready, client, remote):
    local_state = client.get("/api/state").json()
    assert local_state["workspace"]["enabled"] is True
    assert local_state["workspace"]["path"] == fake_path("Test")
    assert local_state["workspace"]["name"] == "Test Workspace"

    remote_state = remote.get("/api/state")
    assert remote_state.status_code == 200
    assert "fake-arena-workspaces" not in json.dumps(remote_state.json())
    assert "path" not in remote_state.json()["workspace"]
    assert remote_state.json()["workspace"]["name"] == "Test Workspace"


def test_state_normalises_the_nested_storage_payload_and_keeps_status(ready, env, client, remote):
    def nested_payload(*, include_path: bool = True):
        current = {"id": "ws-1", "name": "Nested", "created_at": "2026-01-01T00:00:00Z"}
        if include_path:
            current["path"] = fake_path("Nested")
        return {
            "enabled": True,
            "current": current,
            "last_session": "sess-1",
            "status": "ok",
            "degraded": False,
        }

    env.orch.workspace_payload.side_effect = nested_payload
    workspace = client.get("/api/state").json()["workspace"]
    assert workspace == {
        "enabled": True,
        "id": "ws-1",
        "name": "Nested",
        "created_at": "2026-01-01T00:00:00Z",
        "path": fake_path("Nested"),
        "last_session": "sess-1",
        "status": "ok",
        "degraded": False,
    }
    assert "fake-arena-workspaces" not in json.dumps(remote.get("/api/state").json())

    listing = client.get("/api/workspaces").json()
    assert listing["current"]["id"] == "ws-1"
    assert listing["current"]["last_session"] == "sess-1"


def test_state_keeps_working_when_the_workspace_payload_breaks(ready, env, client):
    env.orch.workspace_payload.side_effect = RuntimeError("boom")
    response = client.get("/api/state")
    assert response.status_code == 200
    assert response.json()["workspace"] == {"enabled": False}


# --------------------------------------------------------------- token gate
@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/api/workspaces", None),
        ("post", "/api/workspaces/pick-native-folder", None),
        ("post", "/api/workspaces/select", {"path": fake_path("Other")}),
    ],
)
def test_workspace_endpoints_require_the_token_when_configured(client, monkeypatch, method, path, body):
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    response = client.request(method.upper(), path, json=body)
    assert response.status_code == 401
    detail(response)


# ------------------------------------------------------- native picker route
def test_native_picker_returns_the_path_without_switching(ready, client, env):
    response = client.post("/api/workspaces/pick-native-folder")
    assert response.status_code == 200
    assert response.json() == {"path": fake_path("Picked")}
    assert env.workspace.path == fake_path("Test")  # picking never switches
    env.orch.switch_workspace.assert_not_called()


def test_native_picker_failure_falls_back_to_a_clear_error(ready, client, env):
    env.workspace.picker_error = env.workspace.WorkspaceError("没有可用的图形界面")
    response = client.post("/api/workspaces/pick-native-folder")
    assert response.status_code == 400
    assert "图形界面" in detail(response)

    env.workspace.picker_error = RuntimeError("tkinter exploded")
    response = client.post("/api/workspaces/pick-native-folder")
    assert response.status_code == 503
    assert "手动输入" in detail(response)


def test_native_picker_is_loopback_only_even_with_a_token(ready, remote, monkeypatch):
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    headers = {"X-Arena-Token": TOKEN}
    assert remote.post("/api/workspaces/pick-native-folder", headers=headers).status_code == 403
    assert remote.post("/api/workspaces/select", json={"path": fake_path("Other")}, headers=headers).status_code == 403
    # the loopback client is allowed with the same token
    local = TestClient(app)
    assert local.post("/api/workspaces/pick-native-folder", headers=headers).status_code == 200


# ------------------------------------------------------------- switch route
def test_select_calls_the_engine_and_returns_workspace_and_session(ready, client, env):
    target = fake_path("Other")
    response = client.post(
        "/api/workspaces/select",
        json={"path": target, "initialize_existing": True, "create_if_missing": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["workspace"]["path"] == target
    assert body["workspace"]["enabled"] is True
    assert body["session"]["session_id"] == "sec-test"
    # internal session fields stay private
    assert "moderator_messages" not in body["session"]
    assert "prompt_cache" not in body["session"]
    assert "scenario_snapshot" not in body["session"]
    env.orch.switch_workspace.assert_awaited_once_with(
        target, initialize_existing=True, create_if_missing=False
    )


def test_select_maps_a_running_game_to_409_and_keeps_the_current_workspace(ready, client, env):
    env.orch.switch_workspace.side_effect = RuntimeError("对局进行中，不能切换工作文件夹；请先暂停对局")
    response = client.post("/api/workspaces/select", json={"path": fake_path("Other")})
    assert response.status_code == 409
    assert "对局进行中" in detail(response)
    assert env.workspace.path == fake_path("Test")


def test_select_maps_an_invalid_target_to_400_and_keeps_the_current_workspace(ready, client, env):
    env.orch.switch_workspace.side_effect = env.workspace.WorkspaceError("该文件夹还不是工作文件夹")
    response = client.post("/api/workspaces/select", json={"path": fake_path("NotAWorkspace")})
    assert response.status_code == 400
    assert "工作文件夹" in detail(response)
    assert env.workspace.path == fake_path("Test")


def test_select_validates_the_path_before_calling_the_engine(ready, client, env):
    for path in ("", "   ", "bad\x00path", "x" * 5000):
        response = client.post("/api/workspaces/select", json={"path": path})
        assert response.status_code == 400
        detail(response)
    assert client.post("/api/workspaces/select", json={"path": 5}).status_code == 422
    assert client.post(
        "/api/workspaces/select", json={"path": fake_path("Other"), "extra": True}
    ).status_code == 422
    env.orch.switch_workspace.assert_not_called()


def test_select_does_not_accept_remote_clients_even_authenticated(ready, remote, env, monkeypatch):
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    response = remote.post(
        "/api/workspaces/select",
        json={"path": fake_path("Other")},
        headers={"X-Arena-Token": TOKEN},
    )
    assert response.status_code == 403
    detail(response)
    env.orch.switch_workspace.assert_not_called()


def test_select_rejects_cross_origin_writes(ready, client, env):
    response = client.post(
        "/api/workspaces/select",
        json={"path": fake_path("Other")},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    env.orch.switch_workspace.assert_not_called()


# ------------------------------------------------------- unavailable/off states
def test_disabled_feature_answers_503_for_the_write_routes(client, env):
    disabled = client.get("/api/workspaces").json()
    assert disabled["enabled"] is False
    picking = client.post("/api/workspaces/pick-native-folder")
    assert picking.status_code == 503
    assert "未启用" in detail(picking)
    switching = client.post("/api/workspaces/select", json={"path": fake_path("Other")})
    assert switching.status_code == 503
    assert "未启用" in detail(switching)
    env.orch.switch_workspace.assert_not_called()


def test_missing_storage_workspace_support_answers_clear_503(client, monkeypatch, env):
    monkeypatch.setattr(workspace_api, "workspace_module", None)
    monkeypatch.setattr(workspace_api, "_load_workspace_module", lambda: None)
    listing = client.get("/api/workspaces")
    assert listing.status_code == 200
    assert listing.json()["enabled"] is False
    for response in (
        client.post("/api/workspaces/pick-native-folder"),
        client.post("/api/workspaces/select", json={"path": fake_path("Other")}),
    ):
        assert response.status_code == 503
        assert "不可用" in detail(response)
    env.orch.switch_workspace.assert_not_called()


def test_workspace_module_import_is_lazy(monkeypatch):
    """The API must not require ``app.storage.workspace`` at import time (clear 503 instead)."""
    import importlib

    import app.api.workspaces as module

    assert importlib.import_module("app.api.workspaces") is module
    # the helper tolerates an ImportError-free None as well
    monkeypatch.setattr(module, "_load_workspace_module", lambda: None)
    monkeypatch.setattr(module, "workspace_module", None)
    assert module._ws() is None
