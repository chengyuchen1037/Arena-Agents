from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.providers.models import ModelBinding
from app.storage.errors import StorageError
from app.storage.paths import get_data_dir
from app.storage.settings import SettingsStore


def test_settings_defaults_bindings_replace_and_last_session(tmp_path) -> None:
    store = SettingsStore(tmp_path / "external")
    assert store.get_settings() == {
        "schema_version": 2,
        "default_bindings": {},
        "last_session_id": None,
        "ui": {},
        "model_templates": [],
    }
    first = ModelBinding(provider_id="p1", model_id="m1", reasoning_depth="low")
    second = ModelBinding(provider_id="p2", model_id="m2", reasoning_depth="xhigh")
    store.save_default_bindings({"moderator": first, "p01": first})
    store.save_default_bindings({"moderator": second})

    reopened = SettingsStore(tmp_path / "external")
    assert reopened.get_default_bindings() == {"moderator": second}
    assert reopened.get_settings()["ui"] == {}
    reopened.set_last_session_id("last-game")
    assert SettingsStore(tmp_path / "external").get_last_session_id() == "last-game"
    reopened.set_last_session_id(None)
    assert reopened.get_last_session_id() is None


def test_settings_reject_invalid_bindings_session_ids_and_corruption(tmp_path) -> None:
    store = SettingsStore(tmp_path / "external")
    with pytest.raises(ValueError):
        store.save_default_bindings(
            {"bad": ModelBinding(provider_id="p", model_id="m", reasoning_depth="low"), "": ModelBinding(provider_id="p", model_id="m")}
        )
    with pytest.raises(ValueError):
        store.set_last_session_id("../escape")
    with pytest.raises(ValueError):
        store.set_last_session_id("NUL")

    store.set_last_session_id("valid")
    path = tmp_path / "external" / "settings.json"
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(StorageError):
        store.get_settings()


def test_data_dir_precedence_and_no_creation(tmp_path, monkeypatch) -> None:
    override = tmp_path / "override"
    monkeypatch.setenv("ARENA_DATA_DIR", str(override))
    assert get_data_dir() == override.resolve()
    assert not override.exists()

    monkeypatch.delenv("ARENA_DATA_DIR")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr("app.storage.paths.platform.system", lambda: "Windows")
    assert get_data_dir() == (tmp_path / "local" / "Arena-Agent").resolve()
    assert not (tmp_path / "local").exists()
