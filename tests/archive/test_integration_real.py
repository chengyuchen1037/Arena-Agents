"""The archive against the REAL application: the real stores (games, providers, roster, settings, the key store with the
test master key), the real orchestrator and its event bus, the real HTTP app.  Nothing here is faked except the models
(no game is played).  This is where "export, change everything, import, and it is as it was" is proved end to end."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.archive import jobs as jobs_module
from app.archive import importer
from app.archive.errors import ArchivePasswordError
from app.archive.export import ExportOptions
from app.archive.jobs import ArchiveJob, ArchiveManager
from app.archive.ports import OrchestratorAdapter
from app.game.orchestrator import GameOrchestrator
from app.providers.store import get_provider_store
from app.storage import reset_stores_for_tests
from app.storage.games import GameStore
from app.storage.roster import RosterStore
from app.storage.secrets import SecretStore
from app.storage.settings import SettingsStore, get_settings_store
from game_fakes import async_test

from .helpers import tree

PASSWORD = "Correct-Horse-Battery-9"
KEY_A = "sk-test-real-A-0123456789abcdef"
KEY_B = "sk-test-real-B-0123456789abcdef"
KEY_LOCAL = "sk-test-local-0123456789abcdef"


async def finished(job: ArchiveJob, timeout: float = 60.0) -> ArchiveJob:
    deadline = time.monotonic() + timeout
    while job.status == "running":
        assert time.monotonic() < deadline, f"stuck in {job.phase}"
        await asyncio.sleep(0.01)
    return job


async def pieces(data: bytes, size: int = 100_000) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]
        await asyncio.sleep(0)


async def booted() -> GameOrchestrator:
    orch = GameOrchestrator()
    report = await orch.startup()
    assert report["recovery_error"] is None
    orch._autosave_delay = 0.02
    return orch


def make_world(data: Path) -> dict[str, Any]:
    """Two providers (one with a key, one without), roster names, a default binding, a paused game with a log."""
    providers = get_provider_store()
    first = providers.create_provider("Alpha", "openai_chat", "https://api.example.com/v1", KEY_A)
    providers.add_model(first.id, "m1", 1.0, 2.0, 64_000)
    second = providers.create_provider("Beta", "anthropic_messages", "https://api.example.org", KEY_B)
    providers.add_model(second.id, "claude-x", 3.0, 15.0, 200_000)
    keyless = providers.create_provider("Gamma", "openai_chat", "https://api.example.net/v1", "")
    RosterStore(data).set_name("N3", "Carol-the-Archived")
    RosterStore(data).set_persona("N1", "说话简短，喜欢反问。")
    return {"alpha": first.id, "beta": second.id, "gamma": keyless.id}


def write_log(data: Path, session_id: str, text: str = "think") -> None:
    folder = data / "logs" / session_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "trace-0001.jsonl").write_text(json.dumps({"event": text}) + "\n", encoding="utf-8")


@pytest.fixture()
def data(isolated_external_storage: Path) -> Path:
    reset_stores_for_tests()
    yield isolated_external_storage
    reset_stores_for_tests()
    jobs_module.reset_archive_manager()


# ---------------------------------------------------------------------------------------------------------
@async_test
async def test_export_change_everything_import_and_it_is_as_it_was(data: Path) -> None:
    orch = await booted()
    ids = make_world(data)
    saved_id = orch.session.session_id
    await orch.persist_session("t")
    write_log(data, saved_id)
    manager = ArchiveManager(engine=OrchestratorAdapter(orch))  # the real data directory, the real key store
    _, queue = orch.bus.subscribe()

    job = await finished(await manager.start_export(ExportOptions(include_keys=True, password=PASSWORD)))
    assert job.status == "done", job.message
    exported = Path(job.result["path"])  # type: ignore[index]
    assert exported.parent == data / "exports" and job.result["contents"]["has_secrets"] is True  # type: ignore[index]
    events = [e for e in iter(lambda: queue.get_nowait() if not queue.empty() else None, None) if e.type == "archive.progress"]
    assert events and events[-1].data["status"] == "done" and "path" not in events[-1].data["result"]

    # ---- the user changes everything after the export
    for provider_id in ids.values():
        get_provider_store().delete_provider(provider_id)
    delta = get_provider_store().create_provider("Delta", "openai_chat", "https://api.example.io/v1", KEY_LOCAL)
    RosterStore(data).set_name("N3", "Somebody Else")
    await orch.new_game()  # a different, newer game becomes current
    new_id = orch.session.session_id
    assert new_id != saved_id
    await orch.flush_autosave("t")
    before_import = tree(data, skip=("exports", "backups", "tmp"))

    blob = exported.read_bytes()
    session = await manager.receive_upload(pieces(blob), name="mine.arenaagents", max_bytes=10**9)
    assert session.summary.has_secrets is True and session.summary.games == 1 and session.summary.providers == 3
    apply_job = await finished(await manager.start_apply(session.id, password=PASSWORD, skip_keys=False))
    assert apply_job.status == "done", apply_job.message
    result = apply_job.result
    assert result is not None and result["games"] == 1 and result["keys_imported"] == 2 and result["backup"]

    # ---- it is as it was at the export
    assert orch.session.session_id == saved_id and orch.session.status in ("idle", "paused")
    providers = {p.id: p for p in get_provider_store().list_providers()}
    assert set(providers) == set(ids.values()) and delta.id not in providers
    assert providers[ids["alpha"]].api_key == KEY_A and providers[ids["beta"]].api_key == KEY_B and providers[ids["gamma"]].api_key == ""
    assert providers[ids["alpha"]].models[0].id == "m1"
    assert orch.roster_payload()["seats"][2]["name"] == "Carol-the-Archived"
    assert (data / "logs" / saved_id / "trace-0001.jsonl").read_text(encoding="utf-8") == json.dumps({"event": "think"}) + "\n"
    assert SecretStore(data).read_all() == {ids["alpha"]: KEY_A, ids["beta"]: KEY_B}
    assert GameStore(data).exists(saved_id) and not GameStore(data).exists(new_id)
    assert get_settings_store().get_last_session_id() == saved_id

    # ---- ... and what was there before the import is in the backup, key file included
    backup = data / "backups" / result["backup"]
    assert (backup / "games" / new_id / "game.json").is_file()
    assert {name for name in tree(backup) if not name.startswith(("exports", "tmp"))} >= {name for name in before_import if name.startswith(("games/", "settings.json", "providers.json", "secrets.v1.enc"))}
    assert SecretStore(backup).read_all() == {delta.id: KEY_LOCAL}, "the old keys stay, encrypted with this machine's key"

    # nothing of the old game was written back into the new data afterwards
    await asyncio.sleep(0.2)
    assert not GameStore(data).exists(new_id)
    assert not any(p.name.startswith("import-") for p in (data / "tmp").iterdir()), "the unpacked copy is gone"
    assert not manager._busy and manager._session is None
    await orch.shutdown()


@async_test
async def test_skipping_the_keys_keeps_this_machines_key_for_the_same_provider(data: Path) -> None:
    orch = await booted()
    ids = make_world(data)
    await orch.persist_session("t")
    manager = ArchiveManager(engine=OrchestratorAdapter(orch))
    exported = Path((await finished(await manager.start_export(ExportOptions(include_keys=True, password=PASSWORD)))).result["path"])  # type: ignore[index]
    get_provider_store().update_provider(ids["alpha"], api_key="sk-test-changed-locally-0123456789")  # same provider, new key here
    get_provider_store().delete_provider(ids["beta"])

    session = await manager.receive_upload(pieces(exported.read_bytes()), name="x", max_bytes=10**9)
    job = await finished(await manager.start_apply(session.id, password=None, skip_keys=True))
    assert job.status == "done", job.message
    assert job.result["keys_imported"] == 0 and job.result["keys_kept"] == 1  # type: ignore[index]
    providers = {p.id: p for p in get_provider_store().list_providers()}
    assert providers[ids["alpha"]].api_key == "sk-test-changed-locally-0123456789", "the key of this machine is kept"
    assert providers[ids["beta"]].api_key == "" and providers[ids["gamma"]].api_key == ""
    assert job.result["providers_without_key"] == ["Beta", "Gamma"]  # type: ignore[index]
    await orch.shutdown()


@async_test
async def test_a_wrong_password_changes_nothing_in_the_real_data(data: Path) -> None:
    orch = await booted()
    ids = make_world(data)
    await orch.persist_session("t")
    manager = ArchiveManager(engine=OrchestratorAdapter(orch))
    exported = Path((await finished(await manager.start_export(ExportOptions(include_keys=True, password=PASSWORD)))).result["path"])  # type: ignore[index]
    get_provider_store().delete_provider(ids["alpha"])
    before = tree(data, skip=("exports", "tmp"))
    session = await manager.receive_upload(pieces(exported.read_bytes()), name="x", max_bytes=10**9)
    with pytest.raises(ArchivePasswordError, match="密码错误或 API Key 部分已损坏"):
        await manager.start_apply(session.id, password="Wrong-Horse-Battery-9", skip_keys=False)
    assert tree(data, skip=("exports", "tmp")) == before
    assert ids["alpha"] not in {p.id for p in get_provider_store().list_providers()}
    await orch.shutdown()


@async_test
async def test_an_import_is_refused_while_the_game_really_runs(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orch = await booted()
    make_world(data)
    await orch.persist_session("t")
    manager = ArchiveManager(engine=OrchestratorAdapter(orch))
    exported = Path((await finished(await manager.start_export(ExportOptions()))).result["path"])  # type: ignore[index]
    session = await manager.receive_upload(pieces(exported.read_bytes()), name="x", max_bytes=10**9)
    monkeypatch.setattr(orch, "_supervisor", object())  # the engine's own idea of "a game is running"
    assert OrchestratorAdapter(orch).is_game_active() is True
    from app.archive.errors import ArchiveStateError

    with pytest.raises(ArchiveStateError, match="暂停"):
        await manager.start_apply(session.id, password=None, skip_keys=True)
    monkeypatch.setattr(orch, "_supervisor", None)
    await orch.shutdown()


@async_test
async def test_exporting_while_the_game_runs_saves_it_first(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orch = await booted()
    orch.session.title = "saved by the export"
    manager = ArchiveManager(engine=OrchestratorAdapter(orch))
    monkeypatch.setattr(orch, "_supervisor", object())  # "running"
    job = await finished(await manager.start_export(ExportOptions()))
    assert job.status == "done", job.message
    monkeypatch.setattr(orch, "_supervisor", None)
    game = json.loads((data / "games" / orch.session.session_id / "game.json").read_text(encoding="utf-8"))
    assert game["title"] == "saved by the export", "pre-export flush: the archive holds the game as it was at that moment"
    inside = importer.inspect(Path(job.result["path"]))  # type: ignore[index]
    assert inside.games == 1
    await orch.shutdown()


# ---------------------------------------------------------------------------------------------------------
def test_the_http_app_exports_and_imports_with_the_real_application(data: Path) -> None:
    """Through the real FastAPI app (lifespan, orchestrator singleton, security dependencies, error handlers)."""
    from app.main import app

    jobs_module.reset_archive_manager()
    with TestClient(app) as client:
        ids = make_world(data)
        assert client.post("/api/control/new").status_code == 200  # a game that is saved
        started = client.post("/api/archive/export", json={"include_keys": True, "password": PASSWORD, "include_logs": False})
        assert started.status_code == 202, started.text
        job = _wait(client, started.json()["job_id"])
        assert job["status"] == "done", job
        name = job["result"]["name"]
        download = client.get(f"/api/archive/exports/{name}")
        assert download.status_code == 200 and download.content[:2] == b"PK"

        get_provider_store().delete_provider(ids["alpha"])
        uploaded = client.post("/api/archive/import?name=a.arenaagents", content=download.content, headers={"Content-Type": "application/octet-stream"})
        assert uploaded.status_code == 200, uploaded.text
        import_id = uploaded.json()["import_id"]
        wrong = client.post(f"/api/archive/import/{import_id}/apply", json={"password": "Wrong-Horse-Battery-9"})
        assert wrong.status_code == 422 and wrong.json()["code"] == "wrong_password"
        applied = client.post(f"/api/archive/import/{import_id}/apply", json={"password": PASSWORD})
        assert applied.status_code == 202, applied.text
        done = _wait(client, applied.json()["job_id"])
        assert done["status"] == "done", done
        assert {p.id: p.api_key for p in get_provider_store().list_providers()}[ids["alpha"]] == KEY_A
        state = client.get("/api/state").json()
        assert state["session"]["session_id"] and "workspace" not in state and state["legacy_data"]["found"] in (True, False)
        assert KEY_A not in client.get("/api/state").text and PASSWORD not in client.get("/api/archive/status").text


def _wait(client: TestClient, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while True:
        body = client.get(f"/api/archive/jobs/{job_id}").json()
        if body["status"] != "running":
            return body
        assert time.monotonic() < deadline, body
        time.sleep(0.05)


def test_the_stores_the_archive_uses_are_the_ones_of_the_application(data: Path) -> None:
    """A guard for the assumptions the archive code makes about storage (file names it packs and unpacks)."""
    ids = make_world(data)
    GameStore(data).save("aaaa1111aaaa", {"session_id": "aaaa1111aaaa", "schema_version": 5})
    SettingsStore(data).set_ui_pref("legacy_data_dismissed", True)
    names = {p.relative_to(data).as_posix() for p in data.rglob("*") if p.is_file()}
    for expected in ("providers.json", "secrets.v1.enc", "roster/roster.json", "roster/N1.persona.md", "games/aaaa1111aaaa/game.json", "settings.json"):
        assert expected in names, f"{expected} is no longer how the application stores its data: the archive must follow"
    providers = json.loads((data / "providers.json").read_text(encoding="utf-8"))
    assert [p["id"] for p in providers["providers"]] == [ids["alpha"], ids["beta"], ids["gamma"]] and "api_key" not in json.dumps(providers)
    assert json.loads((data / "settings.json").read_text(encoding="utf-8"))["ui"]["legacy_data_dismissed"] is True
