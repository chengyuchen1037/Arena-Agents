from __future__ import annotations

import json

import pytest

from app.providers.models import (
    ModelNotFound,
    ModelTemplate,
    ProviderNotFound,
)
from app.providers.store import ProviderStore
from app.storage.settings import SettingsStore


def make_provider(data_dir):
    store = ProviderStore(data_dir)
    provider = store.create_provider(
        "Templates", "openai_chat", "https://example.invalid", "template-key"
    )
    store.add_model(provider.id, "model-a", 1.25, 2.5, 64000)
    return provider


def test_templates_roundtrip_schema_two_and_no_price_or_key_copy(tmp_path) -> None:
    data_dir = tmp_path / "external"
    provider = make_provider(data_dir)
    store = SettingsStore(data_dir)
    templates = [
        ModelTemplate(
            slot=1,
            name="模板1",
            provider_id=provider.id,
            model_id="model-a",
            reasoning_depth="low",
        ),
        ModelTemplate(
            slot=3,
            name="模板3",
            provider_id=provider.id,
            model_id="model-a",
            reasoning_depth="xhigh",
        ),
    ]

    store.save_model_templates(templates)
    reopened = SettingsStore(data_dir)
    assert reopened.get_model_templates() == templates
    settings = reopened.get_settings()
    assert settings["schema_version"] == 2
    assert settings["model_templates"][0] == {
        "slot": 1,
        "name": "模板1",
        "provider_id": provider.id,
        "model_id": "model-a",
        "reasoning_depth": "low",
    }
    raw = (data_dir / "settings.json").read_text(encoding="utf-8")
    assert "1.25" not in raw
    assert "template-key" not in raw

    reopened.save_model_templates([templates[1]])
    assert reopened.get_model_templates() == [templates[1]]


def test_schema_one_settings_upgrade_on_next_write(tmp_path) -> None:
    data_dir = tmp_path / "external"
    data_dir.mkdir()
    schema_one = {
        "schema_version": 1,
        "default_bindings": {},
        "last_session_id": None,
        "ui": {"theme": "dark"},
    }
    (data_dir / "settings.json").write_text(
        json.dumps(schema_one), encoding="utf-8"
    )
    store = SettingsStore(data_dir)

    upgraded = store.get_settings()
    assert upgraded["schema_version"] == 2
    assert upgraded["model_templates"] == []
    assert upgraded["ui"] == {"theme": "dark"}
    # Reads are non-destructive; the next write persists schema 2.
    assert json.loads((data_dir / "settings.json").read_text())["schema_version"] == 1
    store.set_last_session_id("session-1")
    persisted = json.loads((data_dir / "settings.json").read_text())
    assert persisted["schema_version"] == 2
    assert persisted["model_templates"] == []


def test_template_slots_and_references_are_validated(tmp_path) -> None:
    data_dir = tmp_path / "external"
    provider = make_provider(data_dir)
    store = SettingsStore(data_dir)
    base = {
        "name": "模板",
        "provider_id": provider.id,
        "model_id": "model-a",
    }
    with pytest.raises(ValueError):
            store.save_model_templates(
                [
                    {"slot": 1, **base},
                    {"slot": 1, **base},
                ]
            )
    with pytest.raises(ValueError):
        store.save_model_templates([{"slot": 4, **base}])
    with pytest.raises(ProviderNotFound):
        store.save_model_templates(
            [{"slot": 1, **base, "provider_id": "missing"}]
        )
    with pytest.raises(ModelNotFound):
        store.save_model_templates([{"slot": 1, **base, "model_id": "missing"}])
