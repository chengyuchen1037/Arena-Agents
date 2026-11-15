from __future__ import annotations

import json
import os

import pytest

import app.migration as migration
from app.providers.store import get_provider_store
from app.storage.errors import StorageError
from app.storage.sessions import SessionStore, get_session_store
from app.storage.settings import get_settings_store


LEGACY_KEY = "legacy-test-key-not-a-real-secret"


@pytest.fixture(autouse=True)
def enable_migration_only_for_temp_directories(monkeypatch) -> None:
    # The global fixture disables migration. These tests opt in explicitly and
    # every call below passes its own tmp_path as base_dir.
    monkeypatch.delenv("ARENA_DISABLE_LEGACY_MIGRATION", raising=False)


def legacy_cfg() -> dict:
    return {
        "llm": {
            "base_url": "https://legacy.invalid",
            "default_model": "legacy-model",
            "context_window_tokens": 64000,
            "pricing": {"input_price_per_m": 0.27, "output_price_per_m": 1.1},
        }
    }


def quarantine_copy(original_name: str):
    directory = get_session_store().data_dir / "quarantine" / "legacy-saves"
    matches = list(directory.glob(f"*_{original_name}"))
    assert len(matches) == 1
    return matches[0]


def test_disabled_migration_does_not_read_or_write_anything(
    tmp_path, monkeypatch
) -> None:
    base = tmp_path / "disabled-project"
    saves = base / "saves"
    saves.mkdir(parents=True)
    env_original = f"DEEPSEEK_API_KEY={LEGACY_KEY}\n"
    save_original = json.dumps({"session_id": "game", "value": 1})
    (base / ".env").write_text(env_original, encoding="utf-8")
    (saves / "game.json").write_text(save_original, encoding="utf-8")
    external = tmp_path / "disabled-external"
    monkeypatch.setenv("ARENA_DATA_DIR", str(external))
    monkeypatch.setenv("ARENA_DISABLE_LEGACY_MIGRATION", "1")

    report = migration.migrate_legacy_data(base, legacy_cfg())

    assert report["status"] == "disabled"
    assert report["providers_migrated"] == 0
    assert report["sessions_migrated"] == 0
    assert (base / ".env").read_text(encoding="utf-8") == env_original
    assert (saves / "game.json").read_text(encoding="utf-8") == save_original
    assert not external.exists()


def test_migration_verifies_then_quarantines_and_sets_latest_session(
    tmp_path, monkeypatch
) -> None:
    base = tmp_path / "legacy-project"
    saves = base / "saves"
    saves.mkdir(parents=True)
    (base / ".env").write_text(
        f"DEEPSEEK_API_KEY={LEGACY_KEY}\n"
        "DEEPSEEK_BASE_URL=https://legacy.invalid\n"
        "DEEPSEEK_MODEL=legacy-model\n"
        "UNRELATED_SETTING=keep-me\n",
        encoding="utf-8",
    )
    old_payload = {"session_id": "old", "history": ["old-private"]}
    new_payload = {"session_id": "new", "history": ["new-private"]}
    old_source = saves / "old.json"
    new_source = saves / "new.json"
    old_raw = json.dumps(old_payload)
    new_raw = json.dumps(new_payload)
    old_source.write_text(old_raw, encoding="utf-8")
    new_source.write_text(new_raw, encoding="utf-8")
    os.utime(old_source, (100, 100))
    os.utime(new_source, (200, 200))
    monkeypatch.setenv("DEEPSEEK_API_KEY", LEGACY_KEY)
    monkeypatch.setenv("DEEPSEEK_MODEL", "legacy-model")

    report = migration.migrate_legacy_data(base, legacy_cfg())

    assert report["status"] == "ok"
    assert report["providers_migrated"] == 1
    assert report["sessions_migrated"] == 2
    assert report["sessions_failed"] == 0
    assert get_settings_store().get_last_session_id() == "new"
    assert LEGACY_KEY not in str(report)
    assert str(base) not in str(report)
    assert "DEEPSEEK_" not in (base / ".env").read_text(encoding="utf-8")
    assert "UNRELATED_SETTING=keep-me" in (base / ".env").read_text(encoding="utf-8")
    assert not old_source.exists()
    assert not new_source.exists()
    assert quarantine_copy("old.json").read_text(encoding="utf-8") == old_raw
    assert quarantine_copy("new.json").read_text(encoding="utf-8") == new_raw
    assert "DEEPSEEK_API_KEY" not in os.environ

    providers = get_provider_store().list_providers()
    assert len(providers) == 1
    provider = providers[0]
    assert provider.name == "DeepSeek（旧配置迁移）"
    assert provider.source == "legacy-migration"
    assert provider.api_key == LEGACY_KEY
    assert provider.models[0].input_price_per_m == 0.27
    assert provider.models[0].output_price_per_m == 1.1
    assert provider.models[0].context_window_tokens == 64000
    assert get_session_store().load("old") == old_payload
    assert get_session_store().load("new") == new_payload

    second = migration.migrate_legacy_data(base, legacy_cfg())
    assert second["providers_migrated"] == 0
    assert len(get_provider_store().list_providers()) == 1


def test_duplicate_session_ids_keep_newest_id_and_rename_older_deterministically(
    tmp_path,
) -> None:
    base = tmp_path / "duplicate-project"
    saves = base / "saves"
    saves.mkdir(parents=True)
    older_source = saves / "a-older.json"
    newest_source = saves / "z-newest.json"
    older_payload = {"session_id": "shared", "value": "older"}
    newest_payload = {"session_id": "shared", "value": "newest"}
    older_source.write_text(json.dumps(older_payload), encoding="utf-8")
    newest_source.write_text(json.dumps(newest_payload), encoding="utf-8")
    os.utime(older_source, (100, 100))
    os.utime(newest_source, (300, 300))
    expected_older_id = migration._duplicate_session_id(
        "shared", older_source, older_payload
    )

    report = migration.migrate_legacy_data(base, legacy_cfg())

    assert report["sessions_migrated"] == 2
    assert report["sessions_failed"] == 0
    assert get_session_store().load("shared") == newest_payload
    assert get_session_store().load(expected_older_id) == {
        "session_id": expected_older_id,
        "value": "older",
    }
    assert get_settings_store().get_last_session_id() == "shared"
    assert not older_source.exists()
    assert not newest_source.exists()
    assert json.loads(quarantine_copy("a-older.json").read_text(encoding="utf-8")) == older_payload
    assert json.loads(quarantine_copy("z-newest.json").read_text(encoding="utf-8")) == newest_payload


def test_invalid_or_conflicting_legacy_save_is_preserved(tmp_path) -> None:
    base = tmp_path / "project"
    saves = base / "saves"
    saves.mkdir(parents=True)
    invalid = saves / "invalid.json"
    invalid.write_text("not-json", encoding="utf-8")
    store = get_session_store()
    store.save("collision", {"session_id": "collision", "value": "external"})
    conflicting = saves / "collision.json"
    conflicting.write_text(
        json.dumps({"session_id": "collision", "value": "legacy"}), encoding="utf-8"
    )

    report = migration.migrate_legacy_data(base, legacy_cfg())

    assert report["sessions_migrated"] == 0
    assert report["sessions_failed"] == 2
    assert invalid.read_text(encoding="utf-8") == "not-json"
    assert json.loads(conflicting.read_text(encoding="utf-8"))["value"] == "legacy"
    assert store.load("collision")["value"] == "external"
    assert get_settings_store().get_last_session_id() is None


def test_environment_cleanup_only_removes_exact_dotenv_matches(
    tmp_path, monkeypatch
) -> None:
    base = tmp_path / "environment-project"
    base.mkdir()
    (base / ".env").write_text(
        f"DEEPSEEK_API_KEY={LEGACY_KEY}\n"
        "DEEPSEEK_BASE_URL=https://legacy.invalid\n"
        "DEEPSEEK_MODEL=legacy-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "docker-key-from-another-source")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://docker.invalid")
    monkeypatch.setenv("DEEPSEEK_MODEL", "legacy-model")
    monkeypatch.setenv("DEEPSEEK_DOCKER_ONLY", "keep-docker-value")

    report = migration.migrate_legacy_data(base, legacy_cfg())

    assert report["env_updated"] is True
    assert os.environ["DEEPSEEK_API_KEY"] == "docker-key-from-another-source"
    assert os.environ["DEEPSEEK_BASE_URL"] == "https://docker.invalid"
    assert "DEEPSEEK_MODEL" not in os.environ
    assert os.environ["DEEPSEEK_DOCKER_ONLY"] == "keep-docker-value"


def test_env_rewrite_failure_preserves_original_and_environment(
    tmp_path, monkeypatch
) -> None:
    base = tmp_path / "env-rewrite-project"
    base.mkdir()
    original = f"DEEPSEEK_API_KEY={LEGACY_KEY}\nKEEP=yes\n"
    (base / ".env").write_text(original, encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", LEGACY_KEY)

    def fail_write(*args, **kwargs):
        raise StorageError("simulated write failure")

    monkeypatch.setattr(migration, "atomic_write", fail_write)
    report = migration.migrate_legacy_data(base, legacy_cfg())

    assert report["status"] == "partial"
    assert report["provider_status"] == "migrated"
    assert report["env_updated"] is False
    assert (base / ".env").read_text(encoding="utf-8") == original
    assert os.environ["DEEPSEEK_API_KEY"] == LEGACY_KEY
    assert LEGACY_KEY not in str(report)


def test_session_save_failure_preserves_legacy_file(tmp_path, monkeypatch) -> None:
    base = tmp_path / "project"
    saves = base / "saves"
    saves.mkdir(parents=True)
    source = saves / "game.json"
    original = json.dumps({"session_id": "game", "value": 1})
    source.write_text(original, encoding="utf-8")

    def fail_save(self, session_id, payload):
        raise StorageError("simulated encrypted save failure")

    monkeypatch.setattr(SessionStore, "save", fail_save)
    report = migration.migrate_legacy_data(base, legacy_cfg())

    assert report["sessions_failed"] == 1
    assert source.read_text(encoding="utf-8") == original
    assert LEGACY_KEY not in str(report)


def test_quarantine_move_failure_preserves_legacy_original(tmp_path, monkeypatch) -> None:
    base = tmp_path / "project"
    saves = base / "saves"
    saves.mkdir(parents=True)
    source = saves / "game.json"
    original = json.dumps({"session_id": "game", "value": 1})
    source.write_text(original, encoding="utf-8")

    def fail_move(path, data_dir):
        raise StorageError("simulated quarantine move failure")

    monkeypatch.setattr(migration, "_move_to_quarantine", fail_move)
    report = migration.migrate_legacy_data(base, legacy_cfg())

    assert report["sessions_migrated"] == 0
    assert report["sessions_failed"] == 1
    assert source.read_text(encoding="utf-8") == original
    assert not (
        get_session_store().data_dir / "quarantine" / "legacy-saves" / "game.json"
    ).exists()
