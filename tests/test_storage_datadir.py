"""The data directory (v0.13, F20 / F37): where it is, that looking never creates it, and the process lock."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

import app.config
from app.storage import reset_stores_for_tests
from app.storage.datadir import (
    LOCK_FILENAME,
    DataDirLockedError,
    ensure_data_dir,
    held_data_dir,
    release_data_dir,
)
from app.storage.errors import StorageError
from app.storage.paths import get_data_dir

REPOSITORY = Path(__file__).resolve().parents[1]


def project() -> Path:
    """The program directory of this test (conftest points ``BASE_DIR`` at a temporary project)."""
    return Path(app.config.BASE_DIR).resolve()

HOLDER = """
import sys
from app.storage.datadir import ensure_data_dir
ensure_data_dir()
print("locked", flush=True)
sys.stdin.read()
"""
PROBE = """
import sys
from app.storage.datadir import DataDirLockedError, ensure_data_dir
try:
    ensure_data_dir()
except DataDirLockedError as exc:
    print("refused:", exc)
    sys.exit(3)
print("ok")
"""


def _env(data_dir: Path) -> dict[str, str]:
    env = {**os.environ, "ARENA_DATA_DIR": str(data_dir), "PYTHONPATH": str(REPOSITORY), "PYTHONUTF8": "1"}
    env.pop("ARENA_DEMO", None)
    return env


def probe(data_dir: Path) -> subprocess.CompletedProcess[str]:
    """Another process tries to lock ``data_dir`` (and gives the lock back by exiting)."""
    return subprocess.run(
        [sys.executable, "-c", PROBE], env=_env(data_dir), cwd=REPOSITORY, capture_output=True, text=True, timeout=60
    )


@pytest.fixture()
def holder():
    """Start a second process that holds the lock of a directory until the fixture says stop."""
    started: list[subprocess.Popen[str]] = []

    def start(data_dir: Path) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, "-c", HOLDER], env=_env(data_dir), cwd=REPOSITORY,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        started.append(process)
        assert process.stdout.readline().strip() == "locked", process.stderr.read()
        return process

    yield start
    for process in started:
        if process.poll() is None:
            process.stdin.close()
            process.wait(timeout=30)
        for stream in (process.stdout, process.stderr):
            stream.close()


# ------------------------------------------------------------------------------------------ location
def test_data_dir_precedence_and_looking_never_creates_it(tmp_path, monkeypatch) -> None:
    override = tmp_path / "override"
    monkeypatch.setenv("ARENA_DATA_DIR", str(override))
    assert get_data_dir() == override.resolve()
    assert not override.exists()

    monkeypatch.setenv("ARENA_DATA_DIR", "   ")  # blank counts as not set
    assert get_data_dir() == (project() / "data").resolve()

    monkeypatch.delenv("ARENA_DATA_DIR")
    assert get_data_dir() == (project() / "data").resolve()
    assert not (project() / "data").exists() and not (project() / ".demo-data").exists()


def test_the_default_is_data_next_to_the_program_whatever_the_current_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ARENA_DATA_DIR", raising=False)
    monkeypatch.setattr(app.config, "BASE_DIR", REPOSITORY)  # the real program directory (read-only use)
    elsewhere = tmp_path / "somewhere" / "else"
    elsewhere.mkdir(parents=True)
    monkeypatch.chdir(elsewhere)
    assert get_data_dir() == (REPOSITORY / "data").resolve()
    assert not (elsewhere / "data").exists()


def test_the_override_is_resolved_and_expanded(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ARENA_DATA_DIR", str(tmp_path / "a" / ".." / "b"))
    assert get_data_dir() == (tmp_path / "b").resolve()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("ARENA_DATA_DIR", "~/arena-data")
    assert get_data_dir() == (tmp_path / "arena-data").resolve()


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "demo"])
def test_demo_mode_keeps_its_data_apart_from_the_real_history(value, monkeypatch) -> None:
    monkeypatch.delenv("ARENA_DATA_DIR", raising=False)
    monkeypatch.setenv("ARENA_DEMO", value)
    assert get_data_dir() == (project() / ".demo-data" / "demo").resolve()


@pytest.mark.parametrize("value", ["", "0", "false", "no", "  "])
def test_demo_switched_off_means_the_real_directory(value, monkeypatch) -> None:
    monkeypatch.delenv("ARENA_DATA_DIR", raising=False)
    monkeypatch.setenv("ARENA_DEMO", value)
    assert get_data_dir() == (project() / "data").resolve()


def test_an_explicit_data_dir_wins_over_demo_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ARENA_DEMO", "1")
    monkeypatch.setenv("ARENA_DATA_DIR", str(tmp_path / "mine"))
    assert get_data_dir() == (tmp_path / "mine").resolve()


# ------------------------------------------------------------------------------------------ the lock
def test_ensure_creates_the_directory_and_takes_the_lock(tmp_path, monkeypatch) -> None:
    target = tmp_path / "fresh" / "nested" / "data"
    monkeypatch.setenv("ARENA_DATA_DIR", str(target))
    assert held_data_dir() is None

    assert ensure_data_dir() == target.resolve()
    assert target.is_dir() and (target / LOCK_FILENAME).is_file()
    assert held_data_dir() == target.resolve()
    assert ensure_data_dir() == target.resolve()  # idempotent: no second lock, no error
    assert [p.name for p in target.iterdir()] == [LOCK_FILENAME]  # nothing else is created


def test_a_second_process_is_refused_until_the_first_one_lets_go(tmp_path, monkeypatch) -> None:
    target = tmp_path / "shared"
    monkeypatch.setenv("ARENA_DATA_DIR", str(target))
    ensure_data_dir()

    refused = probe(target)
    assert refused.returncode == 3 and "另一个 Arena-Agents 实例" in refused.stdout, refused.stderr

    release_data_dir()
    assert held_data_dir() is None
    assert probe(target).returncode == 0  # free again (and the probe's own lock died with its process)
    assert probe(target).returncode == 0


def test_we_are_refused_while_another_process_holds_the_directory_and_nothing_is_changed(tmp_path, monkeypatch, holder) -> None:
    target = tmp_path / "busy"
    holder(target)
    monkeypatch.setenv("ARENA_DATA_DIR", str(target))

    with pytest.raises(DataDirLockedError, match="另一个 Arena-Agents 实例"):
        ensure_data_dir()

    assert isinstance(DataDirLockedError("x"), StorageError)  # (the API layer answers 503 for any StorageError)
    assert held_data_dir() is None
    assert [p.name for p in target.iterdir()] == [LOCK_FILENAME]  # no roster, no settings, nothing was written


def test_the_lock_dies_with_its_process(tmp_path, monkeypatch, holder) -> None:
    target = tmp_path / "crashy"
    process = holder(target)
    monkeypatch.setenv("ARENA_DATA_DIR", str(target))
    with pytest.raises(DataDirLockedError):
        ensure_data_dir()
    process.kill()  # no clean shutdown: the lock file stays behind, the lock does not
    process.wait(timeout=30)
    assert (target / LOCK_FILENAME).exists()
    assert ensure_data_dir() == target.resolve()


def test_another_directory_is_locked_before_the_old_one_is_given_back(tmp_path, monkeypatch, holder) -> None:
    first, second, third = tmp_path / "one", tmp_path / "two", tmp_path / "three"
    monkeypatch.setenv("ARENA_DATA_DIR", str(first))
    ensure_data_dir()
    assert probe(first).returncode == 3

    monkeypatch.setenv("ARENA_DATA_DIR", str(second))
    ensure_data_dir()
    assert held_data_dir() == second.resolve()
    assert probe(first).returncode == 0  # one lock at a time: the old directory was released
    assert probe(second).returncode == 3

    holder(third)
    monkeypatch.setenv("ARENA_DATA_DIR", str(third))
    with pytest.raises(DataDirLockedError):
        ensure_data_dir()
    assert held_data_dir() == second.resolve() and probe(second).returncode == 3  # a failed switch keeps the old lock


def test_a_file_system_without_locks_does_not_stop_the_application_but_says_so(tmp_path, monkeypatch, caplog) -> None:
    """Some network shares / bind mounts cannot lock at all: that is not "another server is running"."""
    import errno

    from app.storage import datadir

    def unsupported(*args, **kwargs):
        raise OSError(errno.ENOLCK, "No locks available")

    module, name = (datadir.msvcrt, "locking") if os.name == "nt" else (datadir.fcntl, "flock")
    monkeypatch.setattr(module, name, unsupported)
    target = tmp_path / "nolocks"
    monkeypatch.setenv("ARENA_DATA_DIR", str(target))
    with caplog.at_level("WARNING", logger="app.storage.datadir"):
        assert ensure_data_dir() == target.resolve()
    assert "不支持" in caplog.text or "does not support file locks" in caplog.text
    assert held_data_dir() == target.resolve()  # the directory is in use by us (idempotent), just not locked
    release_data_dir()


def test_a_busy_lock_is_told_apart_from_a_missing_one_by_the_error_code(tmp_path, monkeypatch) -> None:
    import errno

    from app.storage import datadir

    module, name = (datadir.msvcrt, "locking") if os.name == "nt" else (datadir.fcntl, "flock")
    monkeypatch.setenv("ARENA_DATA_DIR", str(tmp_path / "contended"))
    for code in (errno.EACCES, errno.EAGAIN):
        def busy(*args, _code=code, **kwargs):
            raise OSError(_code, "busy")

        monkeypatch.setattr(module, name, busy)
        with pytest.raises(DataDirLockedError):
            ensure_data_dir()
        assert held_data_dir() is None


def test_resetting_the_stores_for_tests_releases_the_lock(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ARENA_DATA_DIR", str(tmp_path / "d"))
    ensure_data_dir()
    reset_stores_for_tests()
    assert held_data_dir() is None
    assert probe(tmp_path / "d").returncode == 0


def test_a_data_directory_that_is_a_file_is_a_storage_error_not_a_lock_problem(tmp_path, monkeypatch) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("ARENA_DATA_DIR", str(blocker))
    with pytest.raises(StorageError) as raised:
        ensure_data_dir()
    assert not isinstance(raised.value, DataDirLockedError)
    assert held_data_dir() is None


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symbolic links")
def test_a_symlinked_lock_file_is_refused(tmp_path, monkeypatch) -> None:
    target = tmp_path / "linked"
    target.mkdir()
    elsewhere = tmp_path / "elsewhere.lock"
    elsewhere.write_text("x", encoding="utf-8")
    try:
        (target / LOCK_FILENAME).symlink_to(elsewhere)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not permitted here")
    monkeypatch.setenv("ARENA_DATA_DIR", str(target))
    with pytest.raises(StorageError, match="符号链接"):
        ensure_data_dir()
    assert held_data_dir() is None


# -------------------------------------------------------------------------- application start-up
def test_the_orchestrator_refuses_to_start_on_a_locked_directory_and_writes_nothing(tmp_path, monkeypatch, holder) -> None:
    from app.game.orchestrator import GameOrchestrator

    target = tmp_path / "busy-orch"
    holder(target)
    monkeypatch.setenv("ARENA_DATA_DIR", str(target))
    orch = GameOrchestrator()

    with pytest.raises(DataDirLockedError):
        asyncio.run(orch.startup())

    assert not orch._autosave_enabled
    assert [p.name for p in target.iterdir()] == [LOCK_FILENAME]  # not even a roster was created


def test_the_server_refuses_to_start_on_a_locked_directory(tmp_path, monkeypatch, holder) -> None:
    from app.main import app, lifespan

    target = tmp_path / "busy-app"
    holder(target)
    monkeypatch.setenv("ARENA_DATA_DIR", str(target))

    async def enter() -> None:
        async with lifespan(app):
            pytest.fail("the application must not come up on a directory another server uses")

    with pytest.raises(DataDirLockedError):
        asyncio.run(enter())


def test_an_unusable_data_directory_leaves_the_server_up_but_degraded(tmp_path, monkeypatch) -> None:
    from app.main import app, lifespan

    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("ARENA_DATA_DIR", str(blocker))

    async def run() -> bool:
        async with lifespan(app):
            return bool(app.state.degraded)

    assert asyncio.run(run()) is True
    assert blocker.read_text(encoding="utf-8") == "x"  # whatever was there stays


def test_startup_reports_an_unusable_directory_and_saves_nothing(tmp_path, monkeypatch) -> None:
    from app.game.orchestrator import GameOrchestrator

    blocker = tmp_path / "a-file-2"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("ARENA_DATA_DIR", str(blocker))
    orch = GameOrchestrator()

    report = asyncio.run(orch.startup())

    assert report["recovery_error"] and report["restored"] is None
    assert orch._recovery_pending and not orch._autosave_enabled
    assert "数据目录不可用" in orch.session.restore_notice
