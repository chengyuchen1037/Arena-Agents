"""The archive HTTP API (docs/v0.12-contracts.md §11.5, §11.9)."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.archive import router as archive_router
from app.archive import jobs as jobs_module
from app.archive import legacy
from app.archive.format import FORMAT_VERSION
from app.archive.jobs import ArchiveManager

from .helpers import (
    KEY_1,
    KEY_2,
    PASSWORD,
    PROVIDER_1,
    PROVIDER_2,
    FakeEngine,
    FileSecrets,
    dumps,
    game_payload,
    populate,
    tree,
)

TOKEN = "s3cret-token-value"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ARENA_API_TOKEN", "ARENA_ALLOWED_ORIGINS", "ARENA_MAX_ARCHIVE_BYTES"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def store(data_dir: Path) -> FileSecrets:
    return FileSecrets(data_dir)


@pytest.fixture
def manager(engine: FakeEngine, data_dir: Path, populated: dict[str, bytes], store: FileSecrets) -> Iterator[ArchiveManager]:
    store.write_all({PROVIDER_1: KEY_1, PROVIDER_2: KEY_2})
    manager = ArchiveManager(engine=engine, data_dir=lambda: data_dir, secrets=lambda _d: store)
    jobs_module.set_archive_manager(manager)
    yield manager
    jobs_module.reset_archive_manager()


@pytest.fixture(scope="module")
def api() -> FastAPI:
    """Only the archive router, with the real application's error handlers (they never echo a request body)."""
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from app import main as main_module
    from app.storage.errors import StorageError

    app = FastAPI()
    app.add_exception_handler(RequestValidationError, main_module.validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, main_module.http_error_handler)
    app.add_exception_handler(StorageError, main_module.storage_error_handler)
    app.include_router(archive_router)
    return app


@pytest.fixture
def client(api: FastAPI, manager: ArchiveManager) -> Iterator[TestClient]:
    with TestClient(api) as client:  # a persistent event loop: the job tasks keep running between requests
        yield client


@pytest.fixture
def remote(api: FastAPI, manager: ArchiveManager) -> Iterator[TestClient]:
    with TestClient(api, client=("203.0.113.5", 50000)) as client:
        yield client


@pytest.fixture
def bare_client(api: FastAPI, engine: FakeEngine, data_dir: Path, store: FileSecrets) -> Iterator[TestClient]:
    """Like ``client`` but the data directory stays EMPTY (for the "old data found" notice)."""
    jobs_module.set_archive_manager(ArchiveManager(engine=engine, data_dir=lambda: data_dir, secrets=lambda _d: store))
    with TestClient(api) as client:
        yield client
    jobs_module.reset_archive_manager()


def wait_for(client: TestClient, job_id: str, **headers: str) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while True:
        body = client.get(f"/api/archive/jobs/{job_id}", headers=headers).json()
        if body["status"] != "running":
            return body
        assert time.monotonic() < deadline, body
        time.sleep(0.02)


def export_via_api(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post("/api/archive/export", json={"include_logs": True, **body})
    assert response.status_code == 202, response.text
    return wait_for(client, response.json()["job_id"])


# ------------------------------------------------------------------------------------------- export
def test_export_then_download(client: TestClient, data_dir: Path) -> None:
    started = client.post("/api/archive/export", json={"include_keys": False, "include_logs": True})
    assert started.status_code == 202 and set(started.json()) == {"job_id", "kind"} and started.json()["kind"] == "export"
    job = wait_for(client, started.json()["job_id"])
    assert job["status"] == "done" and job["phase"] == "finalize" and job["kind"] == "export"
    result = job["result"]
    assert result["name"].endswith(".arenaagents") and result["size_bytes"] > 0 and result["contents"]["games"] == 2
    assert Path(result["path"]) == data_dir / "exports" / result["name"], "a client on this machine gets the real path"
    assert result["display_path"] == f"{data_dir.name}/exports/{result['name']}"
    download = client.get(f"/api/archive/exports/{result['name']}")
    assert download.status_code == 200 and download.content == Path(result["path"]).read_bytes()
    disposition = download.headers["content-disposition"]
    # (H42: the user's browser saves "arena-agents-<time>.arenaagents"; the file in exports/ keeps the fixed name "arena-<time>.arenaagents")
    assert result["download_name"] == "arena-agents-" + result["name"][len("arena-"):]
    assert disposition == f'attachment; filename="{result["download_name"]}"; filename*=UTF-8\'\'{result["download_name"]}'
    assert download.headers["content-type"].startswith("application/octet-stream")


def test_a_remote_client_never_sees_the_server_path(client: TestClient, remote: TestClient, manager: ArchiveManager) -> None:
    job = export_via_api(client)
    job_id = next(iter(manager._jobs))
    seen = remote.get(f"/api/archive/jobs/{job_id}").json()
    assert "path" not in seen["result"] and seen["result"]["display_path"] and job["result"]["path"]


def test_export_with_keys_encrypts_them_and_never_returns_the_password(client: TestClient, engine: FakeEngine, data_dir: Path) -> None:
    job = export_via_api(client, include_keys=True, password=PASSWORD)
    assert job["status"] == "done" and job["result"]["contents"]["has_secrets"] is True
    raw = Path(job["result"]["path"]).read_bytes()
    assert KEY_1.encode() not in raw and PASSWORD.encode() not in raw
    assert PASSWORD not in json.dumps(job) and PASSWORD not in json.dumps(engine.archive_events())


@pytest.mark.parametrize("body", [{"include_keys": True}, {"include_keys": True, "password": "short"}, {"include_keys": True, "password": "alllowercaseletters"}])
def test_a_weak_password_is_a_422_with_a_code_and_no_echo(client: TestClient, manager: ArchiveManager, body: dict) -> None:
    response = client.post("/api/archive/export", json=body)
    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "weak_password" and payload["detail"]
    assert body.get("password", "\0") not in response.text
    assert manager._busy is None and not manager._jobs


def test_unknown_request_fields_and_oversized_passwords_are_rejected(client: TestClient) -> None:
    assert client.post("/api/archive/export", json={"include_keys": False, "sneaky": 1}).status_code == 422
    response = client.post("/api/archive/export", json={"include_keys": True, "password": "A1!" * 1000})
    assert response.status_code == 422 and "A1!A1!" not in response.text


def test_a_second_job_while_one_runs_is_a_409(client: TestClient, engine: FakeEngine, manager: ArchiveManager) -> None:
    import asyncio
    import threading

    release = threading.Event()
    original = engine.flush_autosave

    async def slow(reason: str) -> bool:
        while not release.is_set():
            await asyncio.sleep(0.01)
        return await original(reason)

    engine.flush_autosave = slow  # type: ignore[method-assign]
    first = client.post("/api/archive/export", json={})
    assert first.status_code == 202
    second = client.post("/api/archive/export", json={})
    assert second.status_code == 409 and second.json()["code"] == "busy"
    status = client.get("/api/archive/status").json()
    assert status["busy"] is True and status["job"]["job_id"] == first.json()["job_id"]
    release.set()
    assert wait_for(client, first.json()["job_id"])["status"] == "done"


def test_a_failed_export_is_reported_by_the_job(client: TestClient, monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    (data_dir / "providers.json").write_bytes(dumps({"schema_version": 1, "providers": [{"id": "p", "api_key": KEY_1}]}))
    job = export_via_api(client)
    assert job["status"] == "error" and job["code"] == "invalid_archive" and "API Key" in job["message"]
    assert KEY_1 not in json.dumps(job)


def test_unknown_jobs_and_files_are_404(client: TestClient, data_dir: Path) -> None:
    assert client.get("/api/archive/jobs/nope").status_code == 404
    for name in ("nope.arenaagents", "..%2Fsettings.json", "arena-20990101-000000.arenaagents", "settings.json", "arena-2026.arenaagents"):
        assert client.get(f"/api/archive/exports/{name}").status_code == 404, name
    assert client.get("/api/archive/exports/..%5C..%5Csettings.json").status_code == 404


def test_a_download_cannot_leave_the_exports_folder(client: TestClient, data_dir: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("outside")
    (data_dir / "exports").mkdir(exist_ok=True)
    link = data_dir / "exports" / "arena-20990101-000001.arenaagents"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symbolic links")
    assert client.get(f"/api/archive/exports/{link.name}").status_code == 404


# --------------------------------------------------------------------------------------------- import
def _upload(client: TestClient, blob: bytes, name: str = "x.arenaagents") -> Any:
    return client.post(f"/api/archive/import?name={name}", content=blob, headers={"Content-Type": "application/octet-stream"})


@pytest.fixture
def blob(client: TestClient) -> bytes:
    return Path(export_via_api(client, include_keys=True, password=PASSWORD)["result"]["path"]).read_bytes()


def test_upload_check_password_and_apply(client: TestClient, blob: bytes, data_dir: Path, engine: FakeEngine, store: FileSecrets) -> None:
    (data_dir / "games" / "dddd4444dddd").mkdir()
    (data_dir / "games" / "dddd4444dddd" / "game.json").write_bytes(dumps(game_payload("dddd4444dddd")))
    uploaded = _upload(client, blob, "my archive.arenaagents")
    assert uploaded.status_code == 200, uploaded.text
    body = uploaded.json()
    assert body["name"] == "my archive.arenaagents" and body["size_bytes"] == len(blob) and body["expires_in"] > 0
    assert body["summary"]["games"] == 2 and body["summary"]["has_secrets"] is True and body["summary"]["providers"] == 2
    import_id = body["import_id"]

    wrong = client.post(f"/api/archive/import/{import_id}/apply", json={"password": "Wrong-Horse-Battery-9", "skip_keys": False})
    assert wrong.status_code == 422 and wrong.json() == {"detail": "密码错误或 API Key 部分已损坏", "code": "wrong_password"}
    assert (data_dir / "games" / "dddd4444dddd").exists(), "a wrong password changed nothing"

    applied = client.post(f"/api/archive/import/{import_id}/apply", json={"password": PASSWORD, "skip_keys": False})
    assert applied.status_code == 202 and applied.json()["kind"] == "import"
    job = wait_for(client, applied.json()["job_id"])
    assert job["status"] == "done", job
    assert job["result"]["games"] == 2 and job["result"]["keys_imported"] == 2 and job["result"]["backup"]
    assert not (data_dir / "games" / "dddd4444dddd").exists() and engine.reloads == 1
    assert (data_dir / "backups" / job["result"]["backup"] / "games" / "dddd4444dddd").is_dir()
    assert store.read_all() == {PROVIDER_1: KEY_1, PROVIDER_2: KEY_2}
    assert PASSWORD not in json.dumps(job) + json.dumps(engine.archive_events())
    gone = client.post(f"/api/archive/import/{import_id}/apply", json={"skip_keys": True})
    assert gone.status_code == 404 and gone.json()["code"] == "not_found"


def test_keys_that_no_longer_match_the_provider_addresses_are_refused_but_the_upload_stays(client: TestClient, blob: bytes, tmp_path: Path, engine: FakeEngine) -> None:
    from .helpers import mutate_archive

    source = tmp_path / "keys.arenaagents"
    source.write_bytes(blob)

    def retarget(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
        providers = json.loads(members["providers.json"])
        providers["providers"][0]["base_url"] = "https://evil.example.com"
        members["providers.json"] = dumps(providers)

    forged = mutate_archive(source, tmp_path / "forged.arenaagents", retarget, fix_manifest=True).read_bytes()
    uploaded = _upload(client, forged)
    assert uploaded.status_code == 200 and uploaded.json()["summary"]["has_secrets"] is True, "the archive itself is valid"
    import_id = uploaded.json()["import_id"]
    url = f"/api/archive/import/{import_id}/apply"
    refused = client.post(url, json={"password": PASSWORD})
    assert refused.status_code == 422 and refused.json()["code"] == "keys_mismatch"
    assert "不一致" in refused.json()["detail"] and "跳过 API Key" in refused.json()["detail"] and "evil" not in refused.text
    assert client.get("/api/archive/status").json()["import"]["import_id"] == import_id, "the upload is kept"
    assert engine.reloads == 0
    # the user follows the advice: the rest of the archive goes in, without the keys
    applied = client.post(url, json={"skip_keys": True})
    assert applied.status_code == 202
    job = wait_for(client, applied.json()["job_id"])
    assert job["status"] == "done" and job["result"]["keys_imported"] == 0


def test_skipping_the_keys_needs_no_password(client: TestClient, blob: bytes) -> None:
    import_id = _upload(client, blob).json()["import_id"]
    applied = client.post(f"/api/archive/import/{import_id}/apply", json={"skip_keys": True})
    assert applied.status_code == 202
    assert wait_for(client, applied.json()["job_id"])["result"]["keys_imported"] == 0


def test_an_archive_with_keys_needs_a_password_or_the_skip(client: TestClient, blob: bytes) -> None:
    import_id = _upload(client, blob).json()["import_id"]
    response = client.post(f"/api/archive/import/{import_id}/apply", json={})
    assert response.status_code == 422 and response.json()["code"] == "wrong_password" and "输入密码" in response.json()["detail"]


def test_cancelling_forgets_the_upload(client: TestClient, blob: bytes, data_dir: Path) -> None:
    import_id = _upload(client, blob).json()["import_id"]
    assert list((data_dir / "tmp").glob("import-*"))
    assert client.delete(f"/api/archive/import/{import_id}").json() == {"ok": True, "discarded": True}
    assert not list((data_dir / "tmp").glob("import-*"))
    assert client.delete(f"/api/archive/import/{import_id}").json() == {"ok": True, "discarded": False}
    assert client.post(f"/api/archive/import/{import_id}/apply", json={"skip_keys": True}).status_code == 404


def test_an_invalid_upload_is_a_422_that_names_the_reason(client: TestClient, data_dir: Path) -> None:
    response = _upload(client, b"this is not an archive" * 50)
    assert response.status_code == 422 and response.json()["code"] == "invalid_archive" and "不是有效的存档" in response.json()["detail"]
    assert not list((data_dir / "tmp").glob("import-*"))
    assert _upload(client, b"").status_code == 422


def test_a_newer_format_is_a_422_with_its_own_code(client: TestClient, blob: bytes, tmp_path: Path) -> None:
    from .helpers import mutate_archive

    source = tmp_path / "in.arenaagents"
    source.write_bytes(blob)
    newer = mutate_archive(source, tmp_path / "newer.arenaagents", lambda members, manifest: manifest.update(format_version=FORMAT_VERSION + 1))
    response = _upload(client, newer.read_bytes())
    assert response.status_code == 422 and response.json()["code"] == "unsupported_version" and "升级" in response.json()["detail"]


def test_the_upload_limit_is_enforced_by_the_declared_and_by_the_real_size(client: TestClient, blob: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARENA_MAX_ARCHIVE_BYTES", str(len(blob) - 1))
    declared = _upload(client, blob)
    assert declared.status_code == 413 and declared.json()["code"] == "too_large"

    def chunks() -> Iterator[bytes]:
        for start in range(0, len(blob), 20_000):
            yield blob[start : start + 20_000]

    streamed = client.post("/api/archive/import", content=chunks())  # chunked: no Content-Length to check up front
    assert streamed.status_code == 413 and streamed.json()["code"] == "too_large"
    monkeypatch.setenv("ARENA_MAX_ARCHIVE_BYTES", str(len(blob)))
    assert _upload(client, blob).status_code == 200


def test_an_import_is_refused_while_a_game_runs(client: TestClient, blob: bytes, engine: FakeEngine) -> None:
    engine.active = True
    response = _upload(client, blob)
    assert response.status_code == 409 and response.json()["code"] == "game_running"
    engine.active = False
    import_id = _upload(client, blob).json()["import_id"]
    engine.active = True
    again = client.post(f"/api/archive/import/{import_id}/apply", json={"skip_keys": True})
    assert again.status_code == 409 and again.json()["code"] == "game_running"


def test_a_file_name_is_only_a_label(client: TestClient, blob: bytes) -> None:
    body = _upload(client, blob, "..%5C..%5Cevil%3Cscript%3E.arenaagents").json()
    assert "<" not in body["name"] and "\\" not in body["name"] and "/" not in body["name"] and body["name"].endswith(".arenaagents")


# ------------------------------------------------------------------------------------------ security
def test_every_route_needs_the_token_when_one_is_configured(client: TestClient, monkeypatch: pytest.MonkeyPatch, manager: ArchiveManager) -> None:
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    calls = [
        ("get", "/api/archive/status"),
        ("get", "/api/archive/jobs/x"),
        ("get", "/api/archive/exports/arena-20260101-000000.arenaagents"),
        ("post", "/api/archive/export"),
        ("post", "/api/archive/import"),
        ("post", "/api/archive/import/x/apply"),
        ("delete", "/api/archive/import/x"),
        ("post", "/api/notices/legacy-data:dismiss"),
    ]
    for method, path in calls:
        assert getattr(client, method)(path).status_code == 401, path
        assert getattr(client, method)(path, headers={"X-Arena-Token": "nope"}).status_code == 401, path
    ok = client.post("/api/archive/export", json={}, headers={"X-Arena-Token": TOKEN})
    assert ok.status_code == 202
    wait_for(client, ok.json()["job_id"], **{"X-Arena-Token": TOKEN})


def test_a_download_link_may_carry_the_token_in_the_query_but_nothing_else_may(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    job = export_via_api(client)
    name = job["result"]["name"]
    job_id = next(iter(jobs_module.get_archive_manager()._jobs))
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    assert client.get(f"/api/archive/exports/{name}").status_code == 401
    assert client.get(f"/api/archive/exports/{name}?token=wrong").status_code == 401
    assert client.get(f"/api/archive/exports/{name}?token={TOKEN}").status_code == 200
    assert client.get(f"/api/archive/jobs/{job_id}?token={TOKEN}").status_code == 401, "only the download accepts ?token="
    assert client.get(f"/api/archive/exports/..%2F{name}?token={TOKEN}").status_code in (401, 404)


def test_writes_are_refused_to_remote_clients_without_a_token(remote: TestClient, manager: ArchiveManager) -> None:
    for method, path in (("post", "/api/archive/export"), ("post", "/api/archive/import"), ("post", "/api/archive/import/x/apply"), ("delete", "/api/archive/import/x"), ("post", "/api/notices/legacy-data:dismiss")):
        assert getattr(remote, method)(path).status_code == 403, path
    assert remote.get("/api/archive/status").status_code == 200, "reading is allowed"


def test_remote_clients_with_a_token_may_export_and_download(remote: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    headers = {"X-Arena-Token": TOKEN}
    started = remote.post("/api/archive/export", json={}, headers=headers)
    assert started.status_code == 202
    job = wait_for(remote, started.json()["job_id"], **headers)
    assert "path" not in job["result"]
    assert remote.get(f"/api/archive/exports/{job['result']['name']}", headers=headers).status_code == 200


def test_a_cross_origin_post_is_refused(client: TestClient) -> None:
    response = client.post("/api/archive/export", json={}, headers={"Origin": "http://evil.example"})
    assert response.status_code == 403


def test_no_password_or_key_reaches_the_log(client: TestClient, blob: bytes, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    import_id = _upload(client, blob).json()["import_id"]
    client.post(f"/api/archive/import/{import_id}/apply", json={"password": "Wrong-Horse-Battery-9"})
    applied = client.post(f"/api/archive/import/{import_id}/apply", json={"password": PASSWORD})
    wait_for(client, applied.json()["job_id"])
    text = "\n".join(record.getMessage() + str(record.exc_text or "") for record in caplog.records)
    for secret in (PASSWORD, "Wrong-Horse-Battery-9", KEY_1, KEY_2):
        assert secret not in text


# ------------------------------------------------------------------------------------ legacy notice
@pytest.fixture
def old_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    workspace = home / "Arena-Agents" / "Default"
    (workspace / ".arena").mkdir(parents=True)
    (workspace / ".arena-workspace.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(legacy, "_home", lambda: home)
    monkeypatch.setattr(legacy.platform, "system", lambda: "Windows")
    return workspace


def test_the_legacy_notice_follows_the_documented_rules(data_dir: Path, old_home: Path) -> None:
    state = legacy.legacy_data_state()
    assert state == {"found": True, "dismissed": False, "path": str(old_home)}
    assert legacy.legacy_data_state(include_path=False) == {"found": True, "dismissed": False}

    populate(data_dir)  # the new data directory is no longer empty: nothing to convert
    assert legacy.legacy_data_state() == {"found": False, "dismissed": False}


def test_an_empty_games_folder_still_counts_as_empty(data_dir: Path, old_home: Path) -> None:
    (data_dir / "games").mkdir()
    assert legacy.legacy_data_state()["found"] is True
    (data_dir / "providers.json").write_text("{}", encoding="utf-8")
    assert legacy.legacy_data_state()["found"] is False


def test_the_notice_needs_the_marker_file_and_the_data_folder(data_dir: Path, old_home: Path) -> None:
    (old_home / ".arena-workspace.json").unlink()
    assert legacy.detect_legacy_workspace() is None
    (old_home / ".arena-workspace.json").write_text("{}", encoding="utf-8")
    (old_home / ".arena").rmdir()
    assert legacy.detect_legacy_workspace() is None


def test_linux_looks_in_the_xdg_data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "h"
    monkeypatch.setattr(legacy, "_home", lambda: home)
    monkeypatch.setattr(legacy.platform, "system", lambda: "Linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    expected = home / ".local" / "share" / "Arena-Agent" / "workspace" / "default"
    assert legacy.default_legacy_path() == expected
    (expected / ".arena").mkdir(parents=True)
    (expected / ".arena-workspace.json").write_text("{}", encoding="utf-8")
    assert legacy.detect_legacy_workspace() == expected
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert legacy.default_legacy_path() == tmp_path / "xdg" / "Arena-Agent" / "workspace" / "default"


def test_dismissing_is_remembered_in_the_settings(bare_client: TestClient, data_dir: Path, old_home: Path) -> None:
    assert legacy.legacy_data_state()["dismissed"] is False
    assert bare_client.post("/api/notices/legacy-data:dismiss").json() == {"ok": True}
    assert legacy.legacy_data_state() == {"found": True, "dismissed": True, "path": str(old_home)}
    settings = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    assert settings["ui"]["legacy_data_dismissed"] is True and settings["schema_version"] == 2
    assert bare_client.post("/api/notices/legacy-data:dismiss").json() == {"ok": True}, "idempotent"


def test_dismissing_keeps_the_other_ui_settings(bare_client: TestClient, data_dir: Path, old_home: Path) -> None:
    (data_dir / "settings.json").write_bytes(dumps({"schema_version": 2, "default_bindings": {}, "last_session_id": None, "ui": {"budget_defaults": {"max_tokens": 1000}}, "model_templates": []}))
    bare_client.post("/api/notices/legacy-data:dismiss")
    settings = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    assert settings["ui"] == {"budget_defaults": {"max_tokens": 1000}, "legacy_data_dismissed": True}
    assert legacy.legacy_data_state()["found"] is True, "settings alone do not make the data directory non-empty"


def test_a_damaged_settings_file_does_not_break_the_notice(data_dir: Path, old_home: Path) -> None:
    (data_dir / "settings.json").write_text("{ not json", encoding="utf-8")
    assert legacy.legacy_data_state() == {"found": True, "dismissed": False, "path": str(old_home)}


def test_state_carries_the_legacy_notice(monkeypatch: pytest.MonkeyPatch, data_dir: Path, old_home: Path) -> None:
    from sec_api_fakes import install

    from app.main import app

    install(monkeypatch)
    local = TestClient(app)
    remote = TestClient(app, client=("203.0.113.5", 50000))
    assert local.get("/api/state").json()["legacy_data"] == {"found": True, "dismissed": False, "path": str(old_home)}
    assert remote.get("/api/state").json()["legacy_data"] == {"found": True, "dismissed": False}, "no path for a remote reader"
    assert "workspace" not in local.get("/api/state").json()


def test_the_real_app_serves_the_archive_routes() -> None:
    from app.main import app

    paths = set(app.openapi()["paths"])
    for path in (
        "/api/archive/export",
        "/api/archive/jobs/{job_id}",
        "/api/archive/exports/{name}",
        "/api/archive/import",
        "/api/archive/import/{import_id}/apply",
        "/api/archive/import/{import_id}",
        "/api/notices/legacy-data:dismiss",
    ):
        assert path in paths, path


def test_the_two_stores_are_not_confused(data_dir: Path) -> None:
    """A tree helper sanity check used by the other tests in this file."""
    populate(data_dir)
    assert "settings.json" in tree(data_dir)
