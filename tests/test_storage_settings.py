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


# ------------------------------------------------------------------------------ v0.12.1: seat renaming
def test_bindings_saved_under_the_v0_12_0_seat_names_are_read_under_the_new_ones(tmp_path):
    import json

    from app.storage.settings import SettingsStore, migrate_legacy_actor_ids

    binding = {"provider_id": "prov", "model_id": "m", "reasoning_depth": "medium", "context_window_tokens": 1000000}
    other = {**binding, "model_id": "other"}
    (tmp_path / "settings.json").write_text(
        json.dumps({"schema_version": 1, "default_bindings": {"moderator": binding, "p01": binding, "p12": other, "N2": other}, "last_session_id": None, "ui": {}}),
        encoding="utf-8",
    )
    store = SettingsStore(tmp_path)
    bindings = store.get_default_bindings()
    assert sorted(bindings, key=lambda k: (k != "moderator", int(k[1:]) if k[1:].isdigit() else 0)) == ["moderator", "N1", "N2", "N12"]
    assert bindings["N12"].model_id == "other" and bindings["N1"].model_id == "m"
    # the file is left alone until the user saves new bindings
    assert "p01" in json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["default_bindings"]
    # a binding that already exists under the new name wins over the legacy one
    assert migrate_legacy_actor_ids({"p02": {"x": 1}, "N2": {"x": 2}}) == {"N2": {"x": 2}}
    assert migrate_legacy_actor_ids({"moderator": 1, "p09": 2, "pXX": 3, "p100": 4}) == {"moderator": 1, "N9": 2, "pXX": 3, "p100": 4}
