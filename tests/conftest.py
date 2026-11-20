from __future__ import annotations

import base64
import os
import shutil
from pathlib import Path

import pytest

import app.config
from app.storage import reset_stores_for_tests

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_CODE_ASSETS = ("config.yml", "scenarios", "prompts", "knowledge", "static", "bots")  # v0.14: bots/ = the default bot library


# ------------------------------------------------------------------------------ the program directory
# Every test runs against a temporary *program directory* (``app.config.BASE_DIR``) instead of the repository: it holds
# the code assets the application reads (``config.yml``, ``scenarios/`` ...) and nothing else, so nothing a test does can
# land in the repository, and the real ``data/`` is never looked at.  The assets are COPIED - once per session, not once
# per test - never linked: a symbolic link needs a privilege on Windows, and a directory junction is followed by some
# deleters (PowerShell 5.1's ``Remove-Item -Recurse``, older ``shutil.rmtree``), which would then delete the repository's
# own ``static/`` when ``pytest-of-*`` is cleaned.  What a test leaves behind in the shared copy is removed afterwards.


def _copy_asset(name: str, project: Path) -> None:
    source = _REPOSITORY_ROOT / name
    if source.is_dir():
        shutil.copytree(source, project / name)
    elif source.is_file():
        shutil.copy2(source, project / name)
    # else: the asset is not there (e.g. ``bots/`` before the default library exists): nothing to copy


def _snapshot(project: Path) -> dict[str, tuple[int, int]]:
    """``{relative path: (size, mtime_ns)}`` of every file of the code assets in ``project``."""
    state: dict[str, tuple[int, int]] = {}
    for name in _CODE_ASSETS:
        root = project / name
        if root.is_file():
            stat = root.stat()
            state[name] = (stat.st_size, stat.st_mtime_ns)
        elif root.is_dir():
            for dirpath, _dirnames, filenames in os.walk(root):
                for filename in filenames:
                    path = Path(dirpath) / filename
                    stat = path.stat()
                    state[path.relative_to(project).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return state


def _remove_inside(project: Path, path: Path) -> None:
    """Delete ``path`` - which must lie inside ``project`` - whatever it is (a directory is never followed through a link)."""
    assert project in path.parents, f"refusing to delete {path}: it is not inside the temporary program directory"
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _restore_program_dir(project: Path, pristine: dict[str, tuple[int, int]]) -> None:
    """Leave ``project`` as it was made: only the code assets, unchanged."""
    for entry in list(project.iterdir()):
        if entry.name not in _CODE_ASSETS:
            _remove_inside(project, entry)
    if _snapshot(project) != pristine:  # a test changed, added or removed an asset file
        for name in _CODE_ASSETS:
            _remove_inside(project, project / name)
            _copy_asset(name, project)


@pytest.fixture(scope="session")
def _program_dir(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, tuple[int, int]]]:
    project = tmp_path_factory.mktemp("program")
    for name in _CODE_ASSETS:
        _copy_asset(name, project)
    return project, _snapshot(project)


@pytest.fixture(autouse=True)
def isolated_external_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _program_dir: tuple[Path, dict[str, tuple[int, int]]],
):
    """Keep every test away from real user configuration and secrets: the data directory is a temporary one
    (``ARENA_DATA_DIR``), the program directory a temporary copy of the code assets, the master key a fixed test key."""
    data_dir = tmp_path / "arena-external-data"
    project_dir, pristine = _program_dir
    _restore_program_dir(project_dir, pristine)
    monkeypatch.setattr(app.config, "BASE_DIR", project_dir)
    monkeypatch.delenv("ARENA_DEMO", raising=False)
    monkeypatch.setenv("ARENA_DATA_DIR", str(data_dir))
    monkeypatch.setenv(
        "ARENA_MASTER_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii"),
    )
    monkeypatch.delenv("ARENA_MASTER_KEY_FILE", raising=False)
    for key in list(os.environ):
        if key.startswith("DEEPSEEK_"):
            monkeypatch.delenv(key, raising=False)
    reset_stores_for_tests()
    yield data_dir
    reset_stores_for_tests()
    _restore_program_dir(project_dir, pristine)
