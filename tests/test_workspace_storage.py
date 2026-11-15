from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.config
import app.storage.workspace as workspace
from app.providers.store import ProviderStore, get_provider_store
from app.storage import clear_store_caches, reset_stores_for_tests
from app.storage.paths import get_data_dir
from app.storage.sessions import SessionStore
from app.storage.settings import SettingsStore, get_settings_store


@pytest.fixture(autouse=True)
def temporary_workspace_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_DISABLE_WORKSPACE", "0")
    monkeypatch.setenv("ARENA_WORKSPACE_DIR", str(tmp_path / "default-workspace"))
    reset_stores_for_tests()
    yield
    reset_stores_for_tests()


def test_create_open_and_initialize_existing_preserves_user_files(tmp_path) -> None:
    root = tmp_path / "workspace-a"
    info = workspace.create_workspace(root, name="工作区 A")
    assert workspace.get_active_workspace() is None
    assert workspace.open_workspace(root) == info
    assert info.data_dir == root / ".arena"
    assert info.id and info.name == "工作区 A"
    marker = json.loads((root / workspace.WORKSPACE_MARKER).read_text(encoding="utf-8"))
    assert marker == {
        "schema_version": 1,
        "workspace_id": info.id,
        "name": "工作区 A",
        "created_at": info.created_at,
    }
    assert (info.data_dir / "logs").is_dir()
    assert (info.data_dir / "exports").is_dir()
    assert workspace.workspace_payload(info, include_path=False) == {
        "id": info.id,
        "name": info.name,
        "created_at": info.created_at,
    }

    existing = tmp_path / "existing"
    existing.mkdir()
    user_file = existing / "keep.txt"
    user_file.write_text("do-not-change", encoding="utf-8")
    with pytest.raises(workspace.WorkspaceError):
        workspace.create_workspace(existing)
    assert not (existing / workspace.WORKSPACE_MARKER).exists()
    assert not (existing / ".arena").exists()

    initialized = workspace.create_workspace(existing, initialize_existing=True)
    assert initialized.root == existing.resolve()
    assert user_file.read_text(encoding="utf-8") == "do-not-change"


def test_workspace_path_validation_and_disable_switch(tmp_path, monkeypatch) -> None:
    with pytest.raises(workspace.WorkspaceError):
        workspace.create_workspace(Path("relative-workspace"))
    with pytest.raises(workspace.WorkspaceError):
        workspace.create_workspace(Path(tmp_path.anchor))
    with pytest.raises(workspace.WorkspaceError):
        workspace.create_workspace(Path(app.config.BASE_DIR))
    with pytest.raises(workspace.WorkspaceError):
        workspace.create_workspace(Path(app.config.BASE_DIR) / "child")

    monkeypatch.setenv("ARENA_DISABLE_WORKSPACE", "true")
    assert workspace.workspace_enabled() is False
    assert get_data_dir() == workspace.get_app_dir()
    with pytest.raises(workspace.WorkspaceError):
        workspace.ensure_initial_workspace()
    assert not (tmp_path / "default-workspace").exists()


def test_lock_conflict_keeps_previous_workspace_and_successful_switch_releases_old(
    tmp_path,
) -> None:
    first = workspace.create_workspace(tmp_path / "first", name="First")
    second = workspace.create_workspace(tmp_path / "second", name="Second")
    workspace.activate_workspace(first)
    assert workspace.get_active_workspace() == first
    assert get_data_dir() == first.data_dir

    conflicting_fd = workspace._prepare_lock_file(second.root)
    try:
        with pytest.raises(workspace.WorkspaceError):
            workspace.activate_workspace(second)
        assert workspace.get_active_workspace() == first
        assert get_data_dir() == first.data_dir
    finally:
        workspace._release_lock(conflicting_fd)

    workspace.activate_workspace(second)
    assert workspace.get_active_workspace() == second
    assert get_data_dir() == second.data_dir
    recent = workspace.list_recent_workspaces()
    assert recent[0]["path"] == str(second.root)
    assert recent[0]["name"] == "Second"

    # The old lock was released only after the new activation succeeded.
    old_lock_fd = workspace._prepare_lock_file(first.root)
    workspace._release_lock(old_lock_fd)
    workspace.deactivate_workspace()
    assert workspace.get_active_workspace() is None


def test_workspace_switch_isolates_stores_and_preserves_old_data(tmp_path) -> None:
    first = workspace.create_workspace(tmp_path / "first")
    second = workspace.create_workspace(tmp_path / "second")
    workspace.activate_workspace(first)
    clear_store_caches()
    provider = get_provider_store().create_provider(
        "First provider", "openai_chat", "https://example.invalid", "first-key"
    )
    get_settings_store().set_last_session_id("first-session")

    workspace.activate_workspace(second)
    clear_store_caches()
    assert get_provider_store().list_providers() == []
    assert get_settings_store().get_last_session_id() is None

    workspace.activate_workspace(first)
    clear_store_caches()
    assert get_provider_store().get_provider(provider.id).api_key == "first-key"
    assert get_settings_store().get_last_session_id() == "first-session"
    assert (first.data_dir / "providers.v1.enc").is_file()
    assert not (second.data_dir / "providers.v1.enc").exists()


def make_v10_data(app_dir: Path):
    provider_store = ProviderStore(app_dir)
    provider = provider_store.create_provider(
        "Legacy", "openai_chat", "https://legacy.invalid", "legacy-key"
    )
    provider_store.add_model(provider.id, "legacy-model", 0.5, 1.5)
    SessionStore(app_dir).save("legacy-session", {"session_id": "legacy-session", "v": 1})
    # A genuine schema-1 settings file verifies non-destructive upgrades.
    (app_dir / "settings.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "default_bindings": {},
                "last_session_id": "legacy-session",
                "ui": {},
            }
        ),
        encoding="utf-8",
    )
    return provider


def test_v10_data_is_copied_verified_and_never_deleted(tmp_path) -> None:
    app_dir = tmp_path / "arena-external-data"
    provider = make_v10_data(app_dir)
    source_before = {
        path.relative_to(app_dir): path.read_bytes()
        for path in app_dir.rglob("*")
        if path.is_file()
    }

    info, report = workspace.ensure_initial_workspace()

    assert report["status"] == "ok"
    assert report["migration"]["status"] == "copied"
    assert report["migration"]["copied_files"] >= 3
    assert workspace.get_active_workspace() == info
    copied_provider = ProviderStore(info.data_dir).get_provider(provider.id)
    assert copied_provider.api_key == "legacy-key"
    assert copied_provider.auto_append_path is True
    assert SettingsStore(info.data_dir).get_last_session_id() == "legacy-session"
    assert SessionStore(info.data_dir).load("legacy-session") == {
        "session_id": "legacy-session",
        "v": 1,
    }
    for relative, original in source_before.items():
        assert (app_dir / relative).read_bytes() == original

    workspace.deactivate_workspace()
    clear_store_caches()
    reopened, second_report = workspace.ensure_initial_workspace()
    assert reopened.id == info.id
    assert second_report["migration"]["status"] == "existing"
    for relative, original in source_before.items():
        assert (app_dir / relative).read_bytes() == original


def test_v10_copy_failure_is_degraded_and_rolls_back_only_new_copies(
    tmp_path, monkeypatch
) -> None:
    app_dir = tmp_path / "arena-external-data"
    make_v10_data(app_dir)
    source_before = {
        path.relative_to(app_dir): path.read_bytes()
        for path in app_dir.rglob("*")
        if path.is_file()
    }
    target = tmp_path / "default-workspace" / ".arena"
    original_snapshot = workspace._runtime_snapshot

    def fail_on_target(data_dir):
        if Path(data_dir) == target:
            raise workspace.WorkspaceError("simulated target verification failure")
        return original_snapshot(data_dir)

    monkeypatch.setattr(workspace, "_runtime_snapshot", fail_on_target)
    info, report = workspace.ensure_initial_workspace()

    assert report["status"] == "degraded"
    assert report["error"]
    assert report["migration"]["status"] == "failed"
    assert workspace.get_active_workspace() == info
    assert not (target / "providers.v1.enc").exists()
    assert not (target / "settings.json").exists()
    for relative, original in source_before.items():
        assert (app_dir / relative).read_bytes() == original


def test_root_symlink_is_rejected_when_supported(tmp_path) -> None:
    real = workspace.create_workspace(tmp_path / "real")
    link = tmp_path / "workspace-link"
    try:
        link.symlink_to(real.root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable on this test host")
    with pytest.raises(workspace.WorkspaceError):
        workspace.open_workspace(link)
