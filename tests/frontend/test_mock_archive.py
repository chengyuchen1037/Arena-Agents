"""The archive part of the mock UI server (tools/mock_ui_server.py: MockArchive): the front end is developed against it,
so its answers must have the shapes of the real API (docs/v0.12-contracts.md section 11.9)."""

from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import mock_ui_server as mock  # noqa: E402

CASES = json.loads((ROOT / "tests" / "archive" / "password_cases.json").read_text(encoding="utf-8"))
PASSWORD = "Correct-Horse-9"


def make_client(**kwargs):
    app = mock.create_app(speed=1000.0, **kwargs)  # fast progress: the whole job takes a few milliseconds
    return app, TestClient(app)


def wait_job(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 20
    while True:
        body = client.get(f"/api/archive/jobs/{job_id}").json()
        if body["status"] != "running":
            return body
        assert time.monotonic() < deadline, body
        time.sleep(0.01)


def upload(client: TestClient, name: str, data: bytes = b"not a real archive"):
    return client.post(f"/api/archive/import?name={name}", content=data, headers={"Content-Type": "application/octet-stream"})


def archive_events(app) -> list[dict]:
    return [e for e in app.state.mock_archive.bus.ring if e["type"] == "archive.progress"]


@pytest.mark.parametrize("case", CASES, ids=[c["note"] for c in CASES])
def test_the_mock_enforces_the_same_password_rule_as_the_real_one(case: dict) -> None:
    assert (mock.mock_password_problem(case["password"]) is None) is case["ok"]


def test_export_runs_as_a_job_with_validated_progress_events() -> None:
    app, c = make_client()
    with c as client:
        started = client.post("/api/archive/export", json={"include_keys": False, "include_logs": True})
        assert started.status_code == 202 and set(started.json()) == {"job_id", "kind"}
        job = wait_job(client, started.json()["job_id"])
        assert job["status"] == "done" and job["kind"] == "export" and job["phase"] == "finalize"
        result = job["result"]
        assert result["name"].endswith(".arenaagents") and mock.EXPORT_NAME_RE.fullmatch(result["name"])
        assert result["path"].startswith("D:\\Arena-Agents\\data\\exports"), "a fictional path, never the checkout's"
        assert result["display_path"] == f"data/exports/{result['name']}"
        assert result["contents"] == {"games": 3, "providers": 2, "roster_seats": 12, "has_secrets": False, "has_logs": True}
        events = archive_events(app)
        assert events and all(mock.validate_event(e) == [] for e in events)
        assert {e["data"]["job_id"] for e in events} == {job["job_id"]}
        assert [e["data"]["phase"] for e in events if e["data"]["status"] == "running"][0] == "flush"
        assert events[-1]["data"]["status"] == "done" and "path" not in events[-1]["data"]["result"]
        download = client.get(f"/api/archive/exports/{result['name']}")
        assert download.status_code == 200 and f'filename="{result["name"]}"' in download.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(download.content)) as z:
            assert json.loads(z.read("manifest.json"))["format"] == "arenaagents"


def test_export_with_keys_needs_a_strong_password() -> None:
    app, c = make_client()
    with c as client:
        for password in (None, "", "short", "alllowercaseletters"):
            response = client.post("/api/archive/export", json={"include_keys": True, "password": password})
            assert response.status_code == 422 and response.json()["code"] == "weak_password"
            assert response.json()["detail"]
        ok = client.post("/api/archive/export", json={"include_keys": True, "password": PASSWORD, "include_logs": False})
        job = wait_job(client, ok.json()["job_id"])
        assert job["result"]["contents"]["has_secrets"] is True and job["result"]["contents"]["has_logs"] is False
        assert "encrypt" in {e["data"]["phase"] for e in archive_events(app)}


def test_one_job_at_a_time_and_unknown_things_are_404() -> None:
    app, c = make_client()
    with c as client:
        client.post("/api/mock/archive", json={"tick": 0.2})
        app.state.mock_archive.speed = 1.0
        first = client.post("/api/archive/export", json={})
        assert first.status_code == 202
        second = client.post("/api/archive/export", json={})
        assert second.status_code == 409 and second.json()["code"] == "busy"
        status = client.get("/api/archive/status").json()
        assert status["busy"] is True and status["job"]["job_id"] == first.json()["job_id"] and status["import"] is None
        app.state.mock_archive.speed = 1000.0
        assert wait_job(client, first.json()["job_id"])["status"] == "done"
        assert client.get("/api/archive/jobs/nope").status_code == 404
        for name in ("nope.arenaagents", "settings.json", "arena-2026.arenaagents", "..%2Fx"):
            assert client.get(f"/api/archive/exports/{name}").status_code == 404, name


def test_import_scenarios_by_file_name() -> None:
    app, c = make_client()
    with c as client:
        demo = upload(client, "my-demo.arenaagents")
        assert demo.status_code == 200
        body = demo.json()
        assert body["name"] == "my-demo.arenaagents" and body["size_bytes"] == len(b"not a real archive") and body["expires_in"] > 0
        summary = body["summary"]
        assert (summary["games"], summary["providers"], summary["has_secrets"], summary["has_logs"]) == (3, 2, False, True)
        assert len(summary["game_list"]) == 3 and summary["provider_names"] == ["DeepSeek", "Claude"]
        assert client.get("/api/archive/status").json()["import"]["import_id"] == body["import_id"]
        assert upload(client, "keys.arenaagents").json()["summary"]["has_secrets"] is True

        refused = {"newer": ("unsupported_version", 422), "corrupt": ("invalid_archive", 422), "huge": ("too_large", 413)}
        for word, (code, status) in refused.items():
            response = upload(client, f"{word}.arenaagents")
            assert (response.status_code, response.json()["code"]) == (status, code), word
            assert response.json()["detail"]
        junk = upload(client, "something.arenaagents", b"hello")
        assert junk.status_code == 422 and junk.json()["code"] == "invalid_archive" and "不是有效的存档" in junk.json()["detail"]
        assert upload(client, "demo.arenaagents", b"").status_code == 422


def test_a_real_archive_is_read_by_its_manifest() -> None:
    app, c = make_client()
    with c as client:
        job = wait_job(client, client.post("/api/archive/export", json={"include_logs": False}).json()["job_id"])
        data = client.get(f"/api/archive/exports/{job['result']['name']}").content
        body = upload(client, "backup.arenaagents", data).json()
        assert body["summary"]["games"] == 3 and body["summary"]["has_logs"] is False and body["summary"]["archive_id"] == job["result"]["archive_id"]


def test_the_upload_limit_and_the_running_game() -> None:
    app = mock.create_app(speed=1.0)  # the game plays in real time: it is still running while the test looks at it
    with TestClient(app) as client:
        client.post("/api/mock/archive", json={"max_bytes": 10})
        assert upload(client, "demo.arenaagents", b"x" * 11).status_code == 413
        client.post("/api/mock/archive", json={"max_bytes": 2 * 1024**3})
        client.post("/api/control/start")
        refused = upload(client, "demo.arenaagents")
        assert refused.status_code == 409 and refused.json()["code"] == "game_running"
        client.post("/api/control/pause")
        assert upload(client, "demo.arenaagents").status_code == 200
        client.post("/api/control/start")
        import_id = client.get("/api/archive/status").json()["import"]["import_id"]
        again = client.post(f"/api/archive/import/{import_id}/apply", json={"skip_keys": True})
        assert again.status_code == 409 and again.json()["code"] == "game_running"
        client.post("/api/control/pause")


def test_apply_replaces_the_game_and_reports_a_result() -> None:
    app, c = make_client()
    with c as client:
        before = client.get("/api/state").json()["session"]["session_id"]
        import_id = upload(client, "demo.arenaagents").json()["import_id"]
        applied = client.post(f"/api/archive/import/{import_id}/apply", json={"skip_keys": False})
        assert applied.status_code == 202 and applied.json()["kind"] == "import"
        job = wait_job(client, applied.json()["job_id"])
        assert job["status"] == "done"
        assert set(job["result"]) == {"backup", "games", "providers", "keys_imported", "keys_kept", "providers_without_key", "warnings"}
        assert job["result"]["games"] == 3 and job["result"]["keys_imported"] == 0 and job["result"]["keys_kept"] == 1
        assert client.get("/api/state").json()["session"]["session_id"] != before, "the data was replaced: a fresh snapshot"
        events = archive_events(app)
        assert all(mock.validate_event(e) == [] for e in events)
        phases = []
        for e in events:
            if e["data"]["kind"] == "import" and e["data"]["phase"] not in phases:
                phases.append(e["data"]["phase"])
        assert phases == ["verify", "validate", "backup", "replace", "reload", "finalize"]
        assert client.get("/api/archive/status").json()["import"] is None
        assert client.post(f"/api/archive/import/{import_id}/apply", json={}).status_code == 404


def test_keys_need_the_password_and_a_wrong_one_can_be_retried() -> None:
    app, c = make_client()
    with c as client:
        import_id = upload(client, "keys.arenaagents").json()["import_id"]
        url = f"/api/archive/import/{import_id}/apply"
        missing = client.post(url, json={})
        assert missing.status_code == 422 and missing.json()["code"] == "wrong_password" and "输入密码" in missing.json()["detail"]
        wrong = client.post(url, json={"password": "Wrong-Horse-Battery-9"})
        assert wrong.json() == {"detail": "密码错误或 API Key 部分已损坏", "code": "wrong_password"} and wrong.status_code == 422
        ok = client.post(url, json={"password": PASSWORD})
        job = wait_job(client, ok.json()["job_id"])
        assert job["status"] == "done" and job["result"]["keys_imported"] == 2 and "keys" in {e["data"]["phase"] for e in archive_events(app)}
        # skipping the keys needs no password
        again = upload(client, "keys.arenaagents").json()["import_id"]
        skipped = client.post(f"/api/archive/import/{again}/apply", json={"skip_keys": True})
        assert wait_job(client, skipped.json()["job_id"])["result"]["keys_imported"] == 0


def test_keys_that_do_not_match_the_provider_addresses_are_refused_and_the_upload_stays() -> None:
    app, c = make_client()
    with c as client:
        import_id = upload(client, "mismatch.arenaagents").json()["import_id"]
        url = f"/api/archive/import/{import_id}/apply"
        assert client.post(url, json={"password": "Wrong-Horse-Battery-9"}).json()["code"] == "wrong_password"
        refused = client.post(url, json={"password": PASSWORD})
        assert refused.status_code == 422 and refused.json()["code"] == "keys_mismatch" and "跳过" in refused.json()["detail"]
        assert client.get("/api/archive/status").json()["import"]["import_id"] == import_id
        skipped = client.post(url, json={"skip_keys": True})
        assert wait_job(client, skipped.json()["job_id"])["status"] == "done"


def test_too_many_wrong_passwords_discard_the_upload() -> None:
    app, c = make_client()
    with c as client:
        import_id = upload(client, "keys.arenaagents").json()["import_id"]
        url = f"/api/archive/import/{import_id}/apply"
        for _ in range(mock.MAX_PASSWORD_ATTEMPTS):
            assert client.post(url, json={"password": "Wrong-Horse-Battery-9"}).status_code == 422
        last = client.post(url, json={"password": PASSWORD})
        assert last.status_code == 429 and last.json()["code"] == "too_many_attempts"
        assert client.post(url, json={"password": PASSWORD}).status_code == 404


def test_a_failing_import_ends_in_an_error_event_and_keeps_the_upload() -> None:
    app, c = make_client()
    with c as client:
        before = client.get("/api/state").json()["session"]["session_id"]
        import_id = upload(client, "fail.arenaagents").json()["import_id"]
        job = wait_job(client, client.post(f"/api/archive/import/{import_id}/apply", json={"skip_keys": True}).json()["job_id"])
        assert job["status"] == "error" and job["code"] == "io_error" and "恢复" in job["message"] and "result" not in job
        assert client.get("/api/state").json()["session"]["session_id"] == before, "nothing was replaced"
        assert client.get("/api/archive/status").json()["import"]["import_id"] == import_id, "the user can try again"
        assert all(mock.validate_event(e) == [] for e in archive_events(app))


def test_cancelling_forgets_the_upload() -> None:
    app, c = make_client()
    with c as client:
        import_id = upload(client, "demo.arenaagents").json()["import_id"]
        assert client.delete(f"/api/archive/import/{import_id}").json() == {"ok": True, "discarded": True}
        assert client.delete(f"/api/archive/import/{import_id}").json() == {"ok": True, "discarded": False}
        assert client.get("/api/archive/status").json()["import"] is None


def test_the_old_data_notice_can_be_shown_dismissed_and_reset() -> None:
    app, c = make_client(legacy=True)
    with c as client:
        state = client.get("/api/state").json()
        assert state["legacy_data"] == {"found": True, "dismissed": False, "path": mock.MOCK_LEGACY_WORKSPACE}
        assert client.post("/api/notices/legacy-data:dismiss").json() == {"ok": True}
        assert client.get("/api/state").json()["legacy_data"]["dismissed"] is True
        client.post("/api/mock/archive", json={"legacy": True})
        assert client.get("/api/state").json()["legacy_data"]["dismissed"] is False
        client.post("/api/mock/archive", json={"legacy": False})
        assert client.get("/api/state").json()["legacy_data"] == {"found": False, "dismissed": False}


def test_the_stream_validator_knows_the_archive_event() -> None:
    good = {"job_id": "j", "kind": "import", "phase": "replace", "done": 1, "total": 4, "status": "running"}
    assert mock.validate_archive_event(good) == []
    done = {**good, "phase": "finalize", "status": "done", "result": {"backup": "x", "games": 1}}
    assert mock.validate_archive_event(done) == []
    failed = {**good, "status": "error", "message": "磁盘空间不足", "code": "io_error"}
    assert mock.validate_archive_event(failed) == []
    for broken in (
        {**good, "kind": "backup"},
        {**good, "phase": "pack"},  # an export phase in an import job
        {**good, "status": "weird"},
        {**good, "done": -1},
        {**good, "result": {}},  # only a finished job has a result
        {**done, "result": {"path": "D:\\secret"}},  # never a server path on the stream
        {**failed, "message": ""},
        {**good, "extra": 1},
        {k: v for k, v in good.items() if k != "job_id"},
    ):
        assert mock.validate_archive_event(broken), broken
