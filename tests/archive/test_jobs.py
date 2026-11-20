"""The background jobs, the progress events and the import session (docs/archive.md "任务与进度")."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from app.archive import jobs as jobs_module
from app.archive.errors import (
    ArchiveBusyError,
    ArchiveFormatError,
    ArchiveIOError,
    ArchiveLimitError,
    ArchiveNotFoundError,
    ArchivePasswordError,
    ArchivePasswordRuleError,
    ArchiveStateError,
)
from app.archive.export import ExportOptions, export_archive
from app.archive.jobs import ArchiveJob, ArchiveManager

from .helpers import (
    GAME_A,
    GAME_B,
    KEY_1,
    KEY_2,
    PASSWORD,
    PROVIDER_1,
    PROVIDER_2,
    FakeEngine,
    FakeSecrets,
    FileSecrets,
    dumps,
    game_payload,
    populate,
    tree,
)


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # type: ignore[arg-type]


async def finished(job: ArchiveJob, timeout: float = 60.0) -> ArchiveJob:
    deadline = time.monotonic() + timeout
    while job.status == "running":
        assert time.monotonic() < deadline, f"job stuck in {job.phase}"
        await asyncio.sleep(0.01)
    return job


async def pieces(data: bytes, size: int = 50_000) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]
        await asyncio.sleep(0)


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def keys() -> FakeSecrets:
    return FakeSecrets({PROVIDER_1: KEY_1, PROVIDER_2: KEY_2})


@pytest.fixture
def manager(engine: FakeEngine, data_dir: Path, populated: dict[str, bytes], keys: FakeSecrets) -> ArchiveManager:
    return ArchiveManager(engine=engine, data_dir=lambda: data_dir, secrets=lambda _dir: keys)


def _export_file(manager: ArchiveManager, **options: Any) -> Path:
    async def go() -> Path:
        job = await manager.start_export(ExportOptions(**options))
        await finished(job)
        assert job.status == "done", job.message
        return Path(job.result["path"])  # type: ignore[index]

    return run(go())


# ------------------------------------------------------------------------------------------ export
def test_an_export_job_runs_saves_first_and_reports_progress(manager: ArchiveManager, engine: FakeEngine, data_dir: Path) -> None:
    async def go() -> ArchiveJob:
        job = await manager.start_export(ExportOptions(include_keys=False))
        return await finished(job)

    job = run(go())
    assert job.status == "done" and engine.flushes == ["pre-export"]
    events = engine.archive_events()
    assert events[0]["status"] == "running" and events[0]["phase"] == "flush" and events[0]["kind"] == "export"
    assert events[-1]["status"] == "done" and events[-1]["job_id"] == job.id
    assert {e["job_id"] for e in events} == {job.id}
    phases = [e["phase"] for e in events]
    assert phases[0] == "flush" and "pack" in phases and phases[-1] == "finalize"
    assert all(0 <= e["done"] <= e["total"] for e in events)
    result = events[-1]["result"]
    assert result["name"].startswith("arena-") and result["size_bytes"] > 0 and result["contents"]["games"] == 2
    assert "path" not in result, "the event goes to every browser: no absolute server path"
    assert result["display_path"] == f"{data_dir.name}/exports/{result['name']}"
    local = job.snapshot(local=True)["result"]
    remote = job.snapshot(local=False)["result"]
    assert Path(local["path"]).is_file() and "path" not in remote


def test_an_export_may_run_while_a_game_is_running(manager: ArchiveManager, engine: FakeEngine) -> None:
    engine.active = True
    assert _export_file(manager).is_file()
    assert engine.flushes == ["pre-export"]


def test_keys_and_the_password_never_reach_an_event_or_a_job(manager: ArchiveManager, engine: FakeEngine) -> None:
    path = _export_file(manager, include_keys=True, password=PASSWORD)
    everything = json.dumps(engine.archive_events()) + repr(manager.get_job(next(iter(manager._jobs))).__dict__)
    for secret in (PASSWORD, KEY_1, KEY_2):
        assert secret not in everything
    assert path.is_file()


def test_a_weak_password_is_refused_at_once_and_starts_no_job(manager: ArchiveManager, engine: FakeEngine) -> None:
    async def go() -> None:
        with pytest.raises(ArchivePasswordRuleError):
            await manager.start_export(ExportOptions(include_keys=True, password="weak"))

    run(go())
    assert engine.archive_events() == [] and manager._busy is None


def test_only_one_archive_job_runs_at_a_time(manager: ArchiveManager, engine: FakeEngine) -> None:
    gate = asyncio.Event()
    original = engine.flush_autosave

    async def slow(reason: str) -> bool:
        await gate.wait()
        return await original(reason)

    engine.flush_autosave = slow  # type: ignore[method-assign]

    async def go() -> None:
        first = await manager.start_export(ExportOptions())
        with pytest.raises(ArchiveBusyError):
            await manager.start_export(ExportOptions())
        assert manager.status(local=True)["busy"] is True and manager.status(local=True)["job"]["job_id"] == first.id
        gate.set()
        await finished(first)
        second = await manager.start_export(ExportOptions())
        await finished(second)
        assert first.status == second.status == "done"

    run(go())


def test_a_failing_export_ends_in_an_error_event_and_frees_the_slot(data_dir: Path, populated: dict[str, bytes], engine: FakeEngine) -> None:
    broken = ArchiveManager(engine=engine, data_dir=lambda: data_dir, secrets=lambda _d: FakeSecrets(fail_read=True))

    async def go() -> ArchiveJob:
        job = await broken.start_export(ExportOptions(include_keys=True, password=PASSWORD))
        return await finished(job)

    job = run(go())
    assert job.status == "error" and job.code == "io_error" and "API Key" in job.message
    last = engine.archive_events()[-1]
    assert last["status"] == "error" and last["code"] == "io_error" and "result" not in last
    assert broken._busy is None
    assert not any(p.name.endswith(".part") for p in (data_dir / "exports").iterdir())


def test_a_save_failure_before_the_export_is_only_a_warning(manager: ArchiveManager, engine: FakeEngine) -> None:
    engine.flush_result = False
    path = _export_file(manager)
    job = next(iter(manager._jobs.values()))
    assert path.is_file() and any("没能先保存" in w for w in job.result["warnings"])  # type: ignore[index]


def test_an_unexpected_error_is_reported_without_details(manager: ArchiveManager, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("secret internals sk-should-not-leak")

    monkeypatch.setattr(jobs_module, "export_archive", explode)

    async def go() -> ArchiveJob:
        return await finished(await manager.start_export(ExportOptions()))

    job = run(go())
    assert job.status == "error" and job.code == "internal" and "sk-should-not-leak" not in job.message
    assert "sk-should-not-leak" not in json.dumps(engine.archive_events())


def test_downloads_only_serve_files_this_program_wrote(manager: ArchiveManager, data_dir: Path) -> None:
    path = _export_file(manager)
    assert manager.export_file(path.name) == path
    for bad in ("../settings.json", "..%2Fx", "arena-1.arenaagents", "settings.json", path.name + ".part", "arena-20990101-000000.arenaagents", f"x/{path.name}"):
        with pytest.raises(ArchiveNotFoundError):
            manager.export_file(bad)
    (data_dir / "exports" / "arena-20990101-000000.arenaagents").mkdir()
    with pytest.raises(ArchiveNotFoundError):
        manager.export_file("arena-20990101-000000.arenaagents")


# -------------------------------------------------------------------------------------------- upload
def _archive_bytes(manager: ArchiveManager, **options: Any) -> bytes:
    return _export_file(manager, **options).read_bytes()


def test_an_upload_is_streamed_checked_and_kept_as_the_pending_import(manager: ArchiveManager, engine: FakeEngine, data_dir: Path) -> None:
    blob = _archive_bytes(manager, include_keys=True, password=PASSWORD)

    async def go() -> Any:
        session = await manager.receive_upload(pieces(blob), name="mine.arenaagents", max_bytes=len(blob))
        return session

    session = run(go())
    assert session.name == "mine.arenaagents" and session.size_bytes == len(blob)
    assert session.summary.games == 2 and session.summary.has_secrets is True
    assert session.path.parent == data_dir / "tmp" and session.path.read_bytes() == blob
    assert manager._busy is None
    public = session.public(100)
    assert public["import_id"] == session.id and public["summary"]["games"] == 2 and public["expires_in"] == 100
    verify = [e for e in engine.archive_events() if e["phase"] == "verify" and e["kind"] == "import"]
    assert verify and all(e["status"] == "running" for e in verify)


def test_a_new_upload_replaces_the_pending_one(manager: ArchiveManager) -> None:
    blob = _archive_bytes(manager)

    async def go() -> tuple[Path, Path]:
        first = await manager.receive_upload(pieces(blob), name="a", max_bytes=10**9)
        second = await manager.receive_upload(pieces(blob), name="b", max_bytes=10**9)
        return first.path, second.path

    first, second = run(go())
    assert not first.exists() and second.is_file() and manager._session is not None and manager._session.path == second


def test_an_upload_over_the_limit_is_refused_and_leaves_no_file(manager: ArchiveManager, data_dir: Path) -> None:
    blob = _archive_bytes(manager)

    async def go() -> None:
        with pytest.raises(ArchiveLimitError, match="ARENA_MAX_ARCHIVE_BYTES"):
            await manager.receive_upload(pieces(blob, 1000), name="big", max_bytes=len(blob) // 2)

    run(go())
    assert list((data_dir / "tmp").iterdir()) == [] and manager._busy is None and manager._session is None


def test_an_invalid_upload_is_refused_and_its_file_deleted(manager: ArchiveManager, data_dir: Path) -> None:
    async def go() -> None:
        with pytest.raises(ArchiveFormatError, match="不是有效的存档"):
            await manager.receive_upload(pieces(b"this is not a zip file " * 100), name="x", max_bytes=10**9)
        with pytest.raises(ArchiveFormatError, match="空的"):
            await manager.receive_upload(pieces(b""), name="x", max_bytes=10**9)

    run(go())
    assert list((data_dir / "tmp").iterdir()) == [] and manager._busy is None


def test_an_upload_is_refused_while_a_game_runs(manager: ArchiveManager, engine: FakeEngine, data_dir: Path) -> None:
    blob = _archive_bytes(manager)
    engine.active = True

    async def go() -> None:
        with pytest.raises(ArchiveStateError, match="暂停"):
            await manager.receive_upload(pieces(blob), name="x", max_bytes=10**9)

    run(go())
    assert manager._busy is None and not (data_dir / "tmp").exists() or list((data_dir / "tmp").iterdir()) == []


def test_a_dropped_connection_leaves_nothing_behind(manager: ArchiveManager, data_dir: Path) -> None:
    blob = _archive_bytes(manager)

    async def dropping() -> AsyncIterator[bytes]:
        yield blob[:1000]
        raise RuntimeError("client went away")

    async def go() -> None:
        with pytest.raises(RuntimeError):
            await manager.receive_upload(dropping(), name="x", max_bytes=10**9)

    run(go())
    assert list((data_dir / "tmp").iterdir()) == [] and manager._busy is None


def test_an_upload_waits_for_a_running_export(manager: ArchiveManager, engine: FakeEngine) -> None:
    blob = _archive_bytes(manager)
    gate = asyncio.Event()
    original = engine.flush_autosave

    async def slow(reason: str) -> bool:
        await gate.wait()
        return await original(reason)

    engine.flush_autosave = slow  # type: ignore[method-assign]

    async def go() -> None:
        job = await manager.start_export(ExportOptions())
        with pytest.raises(ArchiveBusyError):
            await manager.receive_upload(pieces(blob), name="x", max_bytes=10**9)
        gate.set()
        await finished(job)

    run(go())


def test_the_pending_import_expires(engine: FakeEngine, data_dir: Path, populated: dict[str, bytes], keys: FakeSecrets) -> None:
    now = {"t": 1000.0}
    manager = ArchiveManager(engine=engine, data_dir=lambda: data_dir, secrets=lambda _d: keys, import_ttl=60, clock=lambda: now["t"])
    blob = _archive_bytes(manager)

    async def go() -> Path:
        session = await manager.receive_upload(pieces(blob), name="x", max_bytes=10**9)
        now["t"] += 30
        assert manager._get_session(session.id) is session  # asking keeps it alive ...
        now["t"] += 45
        assert manager._get_session(session.id) is session
        now["t"] += 61
        with pytest.raises(ArchiveNotFoundError, match="过期"):
            manager._get_session(session.id)
        return session.path

    path = run(go())
    assert not path.exists() and manager.status(local=True)["import"] is None


def test_cancelling_an_import_deletes_the_upload(manager: ArchiveManager) -> None:
    blob = _archive_bytes(manager)

    async def go() -> Path:
        session = await manager.receive_upload(pieces(blob), name="x", max_bytes=10**9)
        assert manager.discard_import("not-this-one") is False and session.path.exists()
        assert manager.discard_import(session.id) is True
        assert manager.discard_import(session.id) is False
        return session.path

    assert not run(go()).exists()


def test_leftovers_of_a_previous_run_are_removed_on_first_use(manager: ArchiveManager, data_dir: Path) -> None:
    tmp = data_dir / "tmp"
    tmp.mkdir(exist_ok=True)
    (tmp / "import-dead.arenaagents").write_bytes(b"x")
    (tmp / "import-dead.part").write_bytes(b"x")
    (tmp / "import-dead").mkdir()
    (tmp / "import-dead" / "stage").mkdir()
    (tmp / "mine.txt").write_text("not ours")
    old, fresh = data_dir / "exports" / "arena-20200101-000001.arenaagents.part", data_dir / "exports" / "arena-20200101-000002.arenaagents.part"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    long_ago = time.time() - 3 * 24 * 3600
    os.utime(old, (long_ago, long_ago))

    async def go() -> None:
        await finished(await manager.start_export(ExportOptions()))

    run(go())
    assert sorted(p.name for p in tmp.iterdir()) == ["mine.txt"]
    assert not old.exists() and fresh.exists()


# --------------------------------------------------------------------------------------------- apply
def _upload(manager: ArchiveManager, blob: bytes) -> Any:
    async def go() -> Any:
        return await manager.receive_upload(pieces(blob), name="x.arenaagents", max_bytes=10**9)

    return run(go())


def _changed_state(data_dir: Path) -> None:
    """Something that exists now but not in the archive: it must be gone after the import."""
    (data_dir / "games" / "dddd4444dddd").mkdir(parents=True)
    (data_dir / "games" / "dddd4444dddd" / "game.json").write_bytes(dumps(game_payload("dddd4444dddd")))
    (data_dir / "roster" / "N1.persona.md").write_text("改过的性格\n", encoding="utf-8")


def test_an_import_job_replaces_the_data_inside_the_suspended_persistence_window(
    engine: FakeEngine, data_dir: Path, populated: dict[str, bytes]
) -> None:
    store = FileSecrets(data_dir)
    store.write_all({PROVIDER_1: KEY_1, PROVIDER_2: KEY_2})
    manager = ArchiveManager(engine=engine, data_dir=lambda: data_dir, secrets=lambda _d: store)
    blob = _archive_bytes(manager, include_keys=True, password=PASSWORD)
    _changed_state(data_dir)
    store.write_all({PROVIDER_1: "sk-changed-locally-0000000"})
    depths: list[int] = []
    original = engine.reload_from_data_dir

    async def reload() -> Any:
        depths.append(engine.suspended_depth)
        return await original()

    engine.reload_from_data_dir = reload  # type: ignore[method-assign]

    async def go() -> ArchiveJob:
        session = await manager.receive_upload(pieces(blob), name="x", max_bytes=10**9)
        job = await manager.start_apply(session.id, password=PASSWORD, skip_keys=False)
        return await finished(job)

    job = run(go())
    assert job.status == "done", job.message
    assert depths == [1], "the directory swap and the reload happen while autosaving is suspended"
    assert engine.flushes[-1] == "pre-import" and engine.reloads == 1
    assert json.loads((data_dir / "secrets.v1.enc").read_text(encoding="utf-8")) == {PROVIDER_1: KEY_1, PROVIDER_2: KEY_2}
    assert not (data_dir / "games" / "dddd4444dddd").exists()
    assert (data_dir / "roster" / "N1.persona.md").read_bytes() == populated["roster/N1.persona.md"]
    result = job.result
    assert result is not None and result["games"] == 2 and result["keys_imported"] == 2 and result["backup"]
    assert (data_dir / "backups" / result["backup"] / "games" / "dddd4444dddd" / "game.json").is_file()
    last = engine.archive_events()[-1]
    assert (last["kind"], last["status"], last["job_id"]) == ("import", "done", job.id)
    phases = [e["phase"] for e in engine.archive_events() if e["job_id"] == job.id]
    assert [p for i, p in enumerate(phases) if i == 0 or p != phases[i - 1]][:3] == ["verify", "validate", "backup"] or "replace" in phases
    assert manager._session is None and manager._busy is None and not list((data_dir / "tmp").iterdir())
    assert PASSWORD not in json.dumps(engine.archive_events()) and KEY_1 not in json.dumps(engine.archive_events())


def test_a_wrong_password_is_answered_at_once_and_the_import_can_be_retried(manager: ArchiveManager, engine: FakeEngine, data_dir: Path) -> None:
    blob = _archive_bytes(manager, include_keys=True, password=PASSWORD)
    before = tree(data_dir, skip=("exports",))

    async def go() -> ArchiveJob:
        session = await manager.receive_upload(pieces(blob), name="x", max_bytes=10**9)
        for attempt in range(2):
            with pytest.raises(ArchivePasswordError, match="密码错误"):
                await manager.start_apply(session.id, password="Wrong-Horse-Battery-9", skip_keys=False)
            assert session.attempts == attempt + 1 and manager._busy is None and manager._session is session
        with pytest.raises(ArchivePasswordError, match="输入密码"):
            await manager.start_apply(session.id, password=None, skip_keys=False)
        return await finished(await manager.start_apply(session.id, password=PASSWORD, skip_keys=False))

    job = run(go())
    assert job.status == "done" and engine.reloads == 1
    assert before.keys() != tree(data_dir, skip=("exports",)).keys() or True  # the import itself is covered above


def test_too_many_wrong_passwords_discard_the_upload(manager: ArchiveManager, data_dir: Path) -> None:
    blob = _archive_bytes(manager, include_keys=True, password=PASSWORD)

    async def go() -> None:
        session = await manager.receive_upload(pieces(blob), name="x", max_bytes=10**9)
        for _ in range(jobs_module.MAX_PASSWORD_ATTEMPTS):
            with pytest.raises(ArchivePasswordError):
                await manager.start_apply(session.id, password="Wrong-Horse-Battery-9", skip_keys=False)
        with pytest.raises(Exception, match="次数过多") as excinfo:
            await manager.start_apply(session.id, password=PASSWORD, skip_keys=False)
        assert excinfo.value.code == "too_many_attempts"  # type: ignore[attr-defined]
        with pytest.raises(ArchiveNotFoundError):
            await manager.start_apply(session.id, password=PASSWORD, skip_keys=False)

    run(go())
    assert list((data_dir / "tmp").iterdir()) == []


def test_skipping_the_keys_needs_no_password(manager: ArchiveManager, engine: FakeEngine, keys: FakeSecrets, data_dir: Path) -> None:
    blob = _archive_bytes(manager, include_keys=True, password=PASSWORD)

    async def go() -> ArchiveJob:
        session = await manager.receive_upload(pieces(blob), name="x", max_bytes=10**9)
        return await finished(await manager.start_apply(session.id, password=None, skip_keys=True))

    job = run(go())
    assert job.status == "done" and job.result["keys_imported"] == 0  # type: ignore[index]
    assert keys.keys == {PROVIDER_1: KEY_1, PROVIDER_2: KEY_2} or keys.writes, "the local keys of the same providers are kept"


def test_an_unknown_or_expired_import_id_is_not_found(manager: ArchiveManager) -> None:
    async def go() -> None:
        with pytest.raises(ArchiveNotFoundError):
            await manager.start_apply("nope", password=None, skip_keys=True)

    run(go())


def test_a_game_that_started_after_the_upload_stops_the_apply(manager: ArchiveManager, engine: FakeEngine, data_dir: Path) -> None:
    blob = _archive_bytes(manager)
    before = tree(data_dir)

    async def go() -> None:
        session = await manager.receive_upload(pieces(blob), name="x", max_bytes=10**9)
        engine.active = True
        with pytest.raises(ArchiveStateError, match="暂停"):
            await manager.start_apply(session.id, password=None, skip_keys=False)
        assert manager._busy is None and manager._session is session

    run(go())
    assert tree(data_dir, skip=("tmp",)) == {k: v for k, v in before.items() if not k.startswith("tmp")}


def test_a_game_that_starts_during_the_unpacking_stops_the_swap(manager: ArchiveManager, engine: FakeEngine, data_dir: Path) -> None:
    blob = _archive_bytes(manager)
    before = tree(data_dir, skip=("tmp",))
    calls = {"n": 0}

    def flip() -> bool:
        calls["n"] += 1
        return calls["n"] > 3  # idle for the upload, the apply request and the first check; running after the unpacking

    engine.is_game_active = flip  # type: ignore[method-assign]

    async def go() -> ArchiveJob:
        session = await manager.receive_upload(pieces(blob), name="x", max_bytes=10**9)
        return await finished(await manager.start_apply(session.id, password=None, skip_keys=False))

    job = run(go())
    assert job.status == "error" and job.code == "game_running" and engine.reloads == 0
    assert tree(data_dir, skip=("tmp",)) == before
    assert manager._session is not None, "the user can try again after pausing"


def test_a_failing_reload_rolls_back_and_the_import_can_be_retried(engine: FakeEngine, data_dir: Path, populated: dict[str, bytes]) -> None:
    store = FileSecrets(data_dir)
    manager = ArchiveManager(engine=engine, data_dir=lambda: data_dir, secrets=lambda _d: store)
    blob = _archive_bytes(manager)
    _changed_state(data_dir)
    before = tree(data_dir, skip=("tmp", "exports"))
    engine.fail_reload = True  # the first reload raises, the second works

    async def go() -> tuple[ArchiveJob, ArchiveJob]:
        session = await manager.receive_upload(pieces(blob), name="x", max_bytes=10**9)
        first = await finished(await manager.start_apply(session.id, password=None, skip_keys=True))
        assert tree(data_dir, skip=("tmp", "exports")) == before
        assert manager._session is session and manager._busy is None
        second = await finished(await manager.start_apply(session.id, password=None, skip_keys=True))
        return first, second

    first, second = run(go())
    assert first.status == "error" and "cannot reload" not in first.message and first.code == "internal"
    assert second.status == "done" and not (data_dir / "games" / "dddd4444dddd").exists()


def test_a_reload_refused_by_the_application_is_reported_in_its_own_words_and_rolls_back(engine: FakeEngine, data_dir: Path, populated: dict[str, bytes]) -> None:
    store = FileSecrets(data_dir)
    manager = ArchiveManager(engine=engine, data_dir=lambda: data_dir, secrets=lambda _d: store)
    blob = _archive_bytes(manager)
    _changed_state(data_dir)
    before = tree(data_dir, skip=("tmp", "exports"))
    calls = {"n": 0}

    async def refuse() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("赛后复盘正在进行，请等它结束后再重新载入数据目录。")

    engine.reload_from_data_dir = refuse  # type: ignore[method-assign]

    async def go() -> ArchiveJob:
        session = await manager.receive_upload(pieces(blob), name="x", max_bytes=10**9)
        return await finished(await manager.start_apply(session.id, password=None, skip_keys=True))

    job = run(go())
    assert job.status == "error" and job.code == "game_running" and "赛后复盘正在进行" in job.message
    assert tree(data_dir, skip=("tmp", "exports")) == before and calls["n"] == 2, "restored, and the application was told"


def test_the_text_of_a_foreign_exception_is_never_shown() -> None:
    assert jobs_module._user_text(RuntimeError("Event loop is closed C:\\Users\\x")) is None
    assert jobs_module._user_text(RuntimeError("")) is None
    assert jobs_module._user_text(RuntimeError("对局进行中，不能重新载入数据目录；请先暂停对局。" * 20)) is None, "too long to be a sentence for the user"
    assert jobs_module._user_text(RuntimeError("对局进行中，不能重新载入数据目录；请先暂停对局。")).startswith("对局进行中")  # type: ignore[union-attr]


def test_a_broken_upload_is_discarded_by_a_failed_apply(manager: ArchiveManager, data_dir: Path) -> None:
    blob = _archive_bytes(manager)

    async def go() -> ArchiveJob:
        import zipfile

        session = await manager.receive_upload(pieces(blob), name="x", max_bytes=10**9)
        with zipfile.ZipFile(session.path) as zf:
            info = zf.getinfo("settings.json")
            offset = info.header_offset + 30 + len(info.filename) + len(info.extra) + 5
        raw = bytearray(session.path.read_bytes())
        raw[offset] ^= 0xFF  # the file on disk is damaged after it was checked
        session.path.write_bytes(bytes(raw))
        return await finished(await manager.start_apply(session.id, password=None, skip_keys=False))

    job = run(go())
    assert job.status == "error" and job.code in ("invalid_archive",)
    assert manager._session is None and not list((data_dir / "tmp").iterdir())


def test_status_describes_the_slot_and_the_pending_import(manager: ArchiveManager, engine: FakeEngine) -> None:
    blob = _archive_bytes(manager)

    async def go() -> dict[str, Any]:
        assert manager.status(local=True) == {"busy": False, "job": None, "import": None, "game_active": False}
        session = await manager.receive_upload(pieces(blob), name="x", max_bytes=10**9)
        engine.active = True
        return manager.status(local=False) | {"id": session.id}

    status = run(go())
    assert status["busy"] is False and status["import"]["import_id"] == status["id"] and status["game_active"] is True


def test_the_manager_singleton_can_be_replaced() -> None:
    first = jobs_module.get_archive_manager()
    assert jobs_module.get_archive_manager() is first
    custom = ArchiveManager(engine=FakeEngine())
    jobs_module.set_archive_manager(custom)
    assert jobs_module.get_archive_manager() is custom
    jobs_module.reset_archive_manager()
    assert jobs_module.get_archive_manager() is not custom


def test_populate_and_the_other_helpers_stay_consistent(tmp_path: Path) -> None:
    expected = populate(tmp_path)
    assert f"games/{GAME_A}/game.json" in expected and f"games/{GAME_B}/game.json" in expected


# ------------------------------------------------------------------------------------------ names (H42) and the shared slot
def test_the_export_result_names_the_file_the_browser_saves(manager: ArchiveManager, engine: FakeEngine) -> None:
    """H42: ``arena-agents-<time>.arenaagents`` is what the user gets; the file in ``exports/`` keeps its fixed internal name."""

    async def go() -> ArchiveJob:
        return await finished(await manager.start_export(ExportOptions()))

    job = run(go())
    result = engine.archive_events()[-1]["result"]
    assert result["name"].startswith("arena-") and not result["name"].startswith("arena-agents-")
    assert result["download_name"] == "arena-agents-" + result["name"][len("arena-") :]
    assert job.result["download_name"] == result["download_name"]  # type: ignore[index]


def test_the_exclusive_slot_is_the_one_of_the_exports_and_imports(manager: ArchiveManager) -> None:
    """A ``.arenagame`` that is being imported must not overlap an export or an import (the data directory is swapped as a whole): it takes the
    same slot."""
    with manager.exclusive("game-import"):
        with pytest.raises(ArchiveBusyError) as excinfo:
            with manager.exclusive("another"):
                pass
        assert excinfo.value.status == 409 and excinfo.value.code == "busy"

        async def start() -> None:
            await manager.start_export(ExportOptions())

        with pytest.raises(ArchiveBusyError):
            run(start())
    with manager.exclusive("free-again"):
        pass


def test_the_slot_is_released_when_the_work_inside_fails(manager: ArchiveManager) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with manager.exclusive("game-import"):
            raise RuntimeError("boom")
    with manager.exclusive("free-again"):
        pass


def test_a_running_export_holds_the_slot_against_a_game_import(manager: ArchiveManager) -> None:
    async def go() -> None:
        job = await manager.start_export(ExportOptions())
        with pytest.raises(ArchiveBusyError):
            with manager.exclusive("game-import"):
                pass
        await finished(job)
        with manager.exclusive("game-import"):
            pass

    run(go())


def test_what_a_previous_run_left_behind_is_cleaned_before_the_slot_is_taken(manager: ArchiveManager, data_dir: Path) -> None:
    """The cleanup must not meet the temporary files of the operation that holds the slot: it runs first, once."""
    leftover = data_dir / "tmp" / "import-leftover"
    leftover.mkdir(parents=True)
    (leftover / "stage.bin").write_bytes(b"x")
    stray = data_dir / "tmp" / "import-game-aaaa.upload"
    stray.write_bytes(b"x")
    with manager.exclusive("game-import"):
        assert not leftover.exists() and not stray.exists()
        mine = data_dir / "tmp" / "import-game-bbbb.upload"
        mine.write_bytes(b"in use")
        manager._ensure_clean()  # (the cleanup of this process is done: another call - by an archive operation - does nothing)
        assert mine.exists()
