"""Default model bindings are stored by bot (H4) and spoken in seats: ``app.storage.settings`` translates through the line-up."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.providers.models import ModelBinding
from app.storage.bots import BotStore
from app.storage.settings import SettingsStore


def binding(model: str, depth: str = "low") -> ModelBinding:
    return ModelBinding(provider_id="prov", model_id=model, reasoning_depth=depth)


def raw_bindings(data_dir: Path) -> dict:
    return json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))["default_bindings"]


@pytest.fixture
def table(tmp_path: Path) -> tuple[BotStore, SettingsStore, list[str]]:
    store = BotStore(tmp_path / "data", seats=3, library_dir=tmp_path / "no-library")
    store.bootstrap()
    return store, SettingsStore(store.data_dir), store.lineup()


def test_without_bots_the_bindings_are_stored_and_read_as_they_are(tmp_path: Path):
    settings = SettingsStore(tmp_path)
    settings.save_default_bindings({"moderator": binding("mod"), "N1": binding("one"), "p02": binding("two")})
    assert sorted(raw_bindings(tmp_path)) == ["N1", "moderator", "p02"]
    assert sorted(settings.get_default_bindings()) == ["N1", "moderator", "p02"]
    assert settings.get_bot_binding("b-00000001") is None
    assert settings.migrate_seat_bindings() is False  # (no line-up: nothing to move them to)
    assert sorted(raw_bindings(tmp_path)) == ["N1", "moderator", "p02"]


def test_a_seat_is_stored_under_the_bot_that_sits_there(table):
    store, settings, (a, b, c) = table
    settings.save_default_bindings({"moderator": binding("mod"), "N1": binding("one"), "N3": binding("three", "high")})
    assert raw_bindings(store.data_dir) == {
        "moderator": binding("mod").model_dump(mode="json"),
        a: binding("one").model_dump(mode="json"),
        c: binding("three", "high").model_dump(mode="json"),
    }
    seen = settings.get_default_bindings()
    assert sorted(seen) == ["N1", "N3", "moderator"] and seen["N1"] == binding("one") and seen["N3"] == binding("three", "high")
    assert settings.get_bot_bindings() == {"moderator": binding("mod"), a: binding("one"), c: binding("three", "high")}
    assert settings.get_bot_binding(a) == binding("one") and settings.get_bot_binding(b) is None and settings.get_bot_binding(c) == binding("three", "high")


def test_moving_a_bot_to_another_seat_moves_its_binding_along(table):
    store, settings, (a, b, c) = table
    settings.save_default_bindings({"N1": binding("one"), "N2": binding("two"), "moderator": binding("mod")})
    store.set_lineup([c, a, b])
    seen = settings.get_default_bindings()
    assert seen["N2"] == binding("one") and seen["N3"] == binding("two") and "N1" not in seen and seen["moderator"] == binding("mod")
    assert raw_bindings(store.data_dir)[a] == binding("one").model_dump(mode="json")  # (the file did not change)
    # the orchestrator's read-modify-write round trip keeps every binding with its bot
    defaults = settings.get_default_bindings()
    defaults["N1"] = binding("new-for-carol")
    settings.save_default_bindings(defaults)
    assert settings.get_bot_binding(c) == binding("new-for-carol") and settings.get_bot_binding(a) == binding("one") and settings.get_bot_binding(b) == binding("two")


def test_saving_replaces_the_whole_set_like_it_always_did(table):
    store, settings, (a, b, c) = table
    settings.save_default_bindings({"N1": binding("one"), "N2": binding("two"), "moderator": binding("mod")})
    settings.save_default_bindings({"N2": binding("two-b")})
    assert raw_bindings(store.data_dir) == {b: binding("two-b").model_dump(mode="json")}
    assert settings.get_default_bindings() == {"N2": binding("two-b")}
    settings.save_default_bindings({})
    assert settings.get_default_bindings() == {} and raw_bindings(store.data_dir) == {}


def test_a_bot_that_is_not_at_the_table_has_no_seat_but_keeps_its_binding(table):
    store, settings, (a, b, c) = table
    settings.save_bot_binding("b-deadbeef", binding("elsewhere"))
    settings.save_bot_binding(a, binding("one"))
    assert settings.get_default_bindings() == {"N1": binding("one")}  # (the guest has no seat)
    assert settings.get_bot_binding("b-deadbeef") == binding("elsewhere")
    settings.save_default_bindings(settings.get_default_bindings())  # a save from the seat view replaces the set
    assert settings.get_bot_binding("b-deadbeef") is None


def test_the_binding_of_one_bot_can_be_set_and_removed(table):
    store, settings, (a, b, c) = table
    settings.save_bot_binding(b, binding("two"))
    assert settings.get_default_bindings() == {"N2": binding("two")}
    settings.save_bot_binding(b, None)
    assert settings.get_bot_binding(b) is None and raw_bindings(store.data_dir) == {}
    before = (store.data_dir / "settings.json").read_bytes()
    settings.save_bot_binding(b, None)  # nothing to remove: nothing written
    assert (store.data_dir / "settings.json").read_bytes() == before
    with pytest.raises(ValueError):
        settings.save_bot_binding("", binding("x"))
    settings.save_bot_binding(a, {"provider_id": "p", "model_id": "m"})  # (a plain dict is validated as a binding)
    assert settings.get_bot_binding(a).model_id == "m"


def test_a_new_identity_takes_the_binding_with_it(table):
    store, settings, (a, b, c) = table
    settings.save_bot_binding(a, binding("one"))
    assert settings.rename_binding_key(a, "b-12345678") is True
    assert settings.get_bot_binding("b-12345678") == binding("one") and settings.get_bot_binding(a) is None
    before = (store.data_dir / "settings.json").read_bytes()
    assert settings.rename_binding_key(a, "b-99999999") is False  # (it has none now)
    assert (store.data_dir / "settings.json").read_bytes() == before
    fresh = SettingsStore(store.data_dir.parent / "nothing-here")
    assert fresh.rename_binding_key(a, "b-12345678") is False and not (fresh.data_dir / "settings.json").exists()  # (not even the file)


def test_what_v0_13_saved_by_seat_is_read_at_once_and_moved_to_the_bots_by_the_first_start(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    saved = {
        "moderator": binding("mod").model_dump(mode="json"),
        "N1": binding("one").model_dump(mode="json"),
        "N3": binding("three").model_dump(mode="json"),
        "p02": binding("two").model_dump(mode="json"),  # (v0.12.0's name of a seat)
        "N9": binding("nobody").model_dump(mode="json"),  # (a seat that does not exist at this table)
    }
    (data / "settings.json").write_text(
        json.dumps({"schema_version": 2, "default_bindings": saved, "last_session_id": None, "ui": {}, "model_templates": []}), encoding="utf-8"
    )
    settings = SettingsStore(data)
    assert sorted(settings.get_default_bindings()) == ["N1", "N3", "N9", "moderator", "p02"]  # (no bots yet: as stored)
    store = BotStore(data, seats=3, library_dir=tmp_path / "no-library")
    store.bootstrap()
    a, b, c = store.lineup()
    # bootstrap (once, when the table is set up) moved them to the bots: the file now says bot ids
    assert settings.get_bot_binding(a) == binding("one") and settings.get_bot_binding(c) == binding("three")
    migrated = raw_bindings(data)
    assert migrated[a] == saved["N1"] and migrated[c] == saved["N3"] and migrated[b] == saved["p02"]
    assert migrated["moderator"] == saved["moderator"] and migrated["N9"] == saved["N9"]
    assert "N1" not in migrated and "N3" not in migrated and "p02" not in migrated
    assert settings.get_default_bindings()["N2"] == binding("two") and settings.get_default_bindings()["N1"] == binding("one")
    # and it is idempotent: nothing is written the second time
    before = (data / "settings.json").read_bytes()
    assert settings.migrate_seat_bindings() is False
    assert (data / "settings.json").read_bytes() == before


def test_a_binding_the_bot_already_has_wins_over_a_seat_entry(table):
    store, settings, (a, b, c) = table
    payload = json.loads((store.data_dir / "settings.json").read_text(encoding="utf-8")) if (store.data_dir / "settings.json").exists() else {
        "schema_version": 2, "default_bindings": {}, "last_session_id": None, "ui": {}, "model_templates": [],
    }
    payload["default_bindings"] = {a: binding("by-bot").model_dump(mode="json"), "N1": binding("by-seat").model_dump(mode="json")}
    (store.data_dir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")
    assert settings.get_default_bindings() == {"N1": binding("by-bot")}  # (read)
    assert settings.migrate_seat_bindings() is True
    assert raw_bindings(store.data_dir) == {a: binding("by-bot").model_dump(mode="json")}  # (and the seat entry is gone)


def test_a_settings_file_that_has_other_things_in_it_keeps_them(table):
    store, settings, (a, b, c) = table
    settings.set_last_session_id("a-game")
    settings.set_ui_pref("seen", True)
    settings.save_default_bindings({"N1": binding("one")})
    settings.save_bot_binding(b, binding("two"))
    settings.rename_binding_key(b, "b-0000000b")
    settings.migrate_seat_bindings()
    assert settings.get_last_session_id() == "a-game" and settings.get_ui_pref("seen") is True
