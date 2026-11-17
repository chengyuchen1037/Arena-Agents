from __future__ import annotations

import base64
import os
import shutil
from pathlib import Path

import pytest

import app.config
from app.storage import reset_stores_for_tests

# TODO(v0.12): these v0.11 orchestrator tests are written against the old session container and
# must be rewritten for app.game.orchestrator (task: "port legacy orchestrator tests").
collect_ignore = [
    "test_orchestrator_workspace.py",
    "test_orchestrator_persistence.py",
    "test_orchestrator_real_storage.py",
    "test_orchestrator_bindings.py",
]

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_CODE_ASSETS = ("config.yml", "scenarios", "prompts", "knowledge", "static")


def _link_or_copy_code_asset(name: str, project_dir: Path) -> None:
    """Expose read-only code assets without linking legacy runtime data."""
    source = _REPOSITORY_ROOT / name
    destination = project_dir / name
    try:
        destination.symlink_to(source, target_is_directory=source.is_dir())
    except (OSError, NotImplementedError):
        # Windows commonly denies symlink creation without Developer Mode.
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)


@pytest.fixture(autouse=True)
def isolated_external_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep every test away from real user configuration and legacy secrets."""
    data_dir = tmp_path / "arena-external-data"
    # Keep the test's own cwd empty for legacy "no project-local save" checks.
    # This sibling directory is still unique to this test and outside the repo.
    project_dir = tmp_path.parent / f"{tmp_path.name}-project"
    project_dir.mkdir()
    for asset_name in _CODE_ASSETS:
        _link_or_copy_code_asset(asset_name, project_dir)
    monkeypatch.setattr(app.config, "BASE_DIR", project_dir)
    monkeypatch.setenv("ARENA_DISABLE_LEGACY_MIGRATION", "1")
    monkeypatch.setenv("ARENA_DISABLE_WORKSPACE", "1")
    monkeypatch.delenv("ARENA_WORKSPACE_DIR", raising=False)
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
