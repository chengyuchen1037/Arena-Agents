from __future__ import annotations

import math
import uuid

import pytest

from app.providers.models import (
    ModelBinding,
    ModelNotFound,
    ProviderError,
    ProviderNotFound,
)
from app.providers.store import ENV_PROVIDER_ID, ProviderStore


def make_store(tmp_path) -> ProviderStore:
    return ProviderStore(tmp_path / "external")


def create_provider(store: ProviderStore):
    return store.create_provider(
        "Test Provider", "openai_chat", "https://example.invalid", "test-key"
    )


def test_provider_crud_encryption_backup_and_resolve(tmp_path) -> None:
    store = make_store(tmp_path)
    provider = create_provider(store)
    uuid.UUID(hex=provider.id)
    assert "test-key" not in repr(provider)
    assert store.list_providers()[0].models == []

    model = store.add_model(provider.id, "vendor/model", 0.25, 1.5, 32000)
    assert model.context_window_tokens == 32000
    updated = store.update_model(provider.id, model.id, input_price_per_m=0.5)
    assert updated.input_price_per_m == 0.5
    assert updated.output_price_per_m == 1.5

    binding = ModelBinding(provider_id=provider.id, model_id=model.id)
    resolved_provider, resolved_model = store.resolve(binding)
    assert resolved_provider.id == provider.id
    assert resolved_model == updated

    ciphertext = (tmp_path / "external" / "providers.v1.enc").read_bytes()
    assert b"test-key" not in ciphertext
    assert (tmp_path / "external" / "providers.v1.backup.enc").is_file()
    assert ProviderStore(tmp_path / "external").get_provider(provider.id).api_key == "test-key"

    renamed = store.update_provider(provider.id, name="Renamed", api_key="")
    assert renamed.name == "Renamed"
    assert renamed.api_key == "test-key"
    rotated = store.update_provider(provider.id, base_url="https://new.invalid", api_key="new-test-key")
    assert rotated.api_key == "new-test-key"

    store.remove_model(provider.id, model.id)
    with pytest.raises(ModelNotFound):
        store.resolve(binding)
    store.delete_provider(provider.id)
    with pytest.raises(ProviderNotFound):
        store.get_provider(provider.id)


def test_provider_validation_single_model_and_endpoint_key_rule(tmp_path) -> None:
    store = make_store(tmp_path)
    provider = create_provider(store)
    for invalid in (-1, math.inf, math.nan):
        with pytest.raises(ProviderError):
            store.add_model(provider.id, f"bad-{invalid}", invalid, 1)
    with pytest.raises(ProviderError):
        store.add_model(provider.id, "bad-output", 1, -0.1)

    store.add_model(provider.id, "one", 0, 0)
    with pytest.raises(ProviderError):
        store.add_model(provider.id, "one", 2, 3)
    with pytest.raises(ProviderError):
        store.update_provider(provider.id, base_url="https://other.invalid")
    with pytest.raises(ProviderError):
        store.update_provider(provider.id, protocol="anthropic_messages", api_key="")
    with pytest.raises(ProviderError):
        store.create_provider("bad", "openai_chat", "https://example.invalid", " ")


def test_environment_provider_is_read_only_and_empty_key_is_unset(tmp_path, monkeypatch) -> None:
    store = make_store(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", " ")
    assert store.list_providers() == []
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-test-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "env-model")

    env_provider = store.get_provider(ENV_PROVIDER_ID)
    assert env_provider.read_only is True
    assert env_provider.source == "environment"
    assert env_provider.models[0].id == "env-model"
    for operation in (
        lambda: store.update_provider(ENV_PROVIDER_ID, name="no"),
        lambda: store.delete_provider(ENV_PROVIDER_ID),
        lambda: store.add_model(ENV_PROVIDER_ID, "no", 1, 1),
    ):
        with pytest.raises(ProviderError):
            operation()


def test_provider_falls_back_to_known_good_backup(tmp_path) -> None:
    store = make_store(tmp_path)
    provider = create_provider(store)
    store.update_provider(provider.id, name="known-good")
    store.update_provider(provider.id, name="newest")
    current = tmp_path / "external" / "providers.v1.enc"
    current.write_bytes(b"broken-current")
    assert ProviderStore(tmp_path / "external").get_provider(provider.id).name == "known-good"

    (tmp_path / "external" / "providers.v1.backup.enc").write_bytes(b"broken-backup")
    from app.storage.errors import StorageError

    with pytest.raises(StorageError):
        ProviderStore(tmp_path / "external").list_providers()


def test_provider_auto_append_path_defaults_persists_and_requires_key(tmp_path) -> None:
    store = make_store(tmp_path)
    default_provider = create_provider(store)
    manual = store.create_provider(
        "Manual",
        "openai_responses",
        "https://example.invalid/custom/responses",
        "manual-key",
        auto_append_path=False,
    )
    assert default_provider.auto_append_path is True
    assert manual.auto_append_path is False
    assert ProviderStore(tmp_path / "external").get_provider(manual.id).auto_append_path is False

    with pytest.raises(ProviderError):
        store.update_provider(default_provider.id, auto_append_path=False)
    changed = store.update_provider(
        default_provider.id,
        auto_append_path=False,
        api_key="replacement-key",
    )
    assert changed.auto_append_path is False
    assert changed.api_key == "replacement-key"
    unchanged = store.update_provider(
        default_provider.id, name="name-only", api_key=""
    )
    assert unchanged.auto_append_path is False


def test_old_provider_payload_defaults_auto_append_path_to_true(tmp_path) -> None:
    from app.storage.crypto import decrypt_json, encrypt_json

    data_dir = tmp_path / "external"
    store = ProviderStore(data_dir)
    provider = create_provider(store)
    path = data_dir / "providers.v1.enc"
    payload = decrypt_json(path.read_bytes(), "providers.v1", data_dir=data_dir)
    del payload["providers"][0]["auto_append_path"]
    path.write_bytes(encrypt_json(payload, "providers.v1", data_dir=data_dir))

    reopened = ProviderStore(data_dir).get_provider(provider.id)
    assert reopened.auto_append_path is True


# ------------------------------------------------------------------------------ v0.12.1: four prices
def test_a_registry_written_before_the_cache_prices_existed_still_loads_and_bills_like_before(tmp_path) -> None:
    from app.storage.crypto import encrypt_json

    data_dir = tmp_path / "external"
    old_registry = {
        "schema_version": 1,
        "providers": [
            {
                "id": uuid.uuid4().hex, "name": "Old", "protocol": "openai_chat", "base_url": "https://example.invalid",
                "api_key": "old-key", "read_only": False, "source": "encrypted", "auto_append_path": True,
                "models": [{"id": "m", "input_price_per_m": 0.5, "output_price_per_m": 2.0, "context_window_tokens": 64000}],
            }
        ],
    }
    data_dir.mkdir()
    (data_dir / "providers.v1.enc").write_bytes(encrypt_json(old_registry, "providers.v1", data_dir=data_dir))

    store = ProviderStore(data_dir)
    model = store.list_providers()[0].models[0]
    assert (model.cache_read_price_per_m, model.cache_write_price_per_m, model.price_source) == (None, None, "manual")
    assert model.price_snapshot() == {  # an empty cache price is billed like an input token: nothing changes for old data
        "input_price_per_m": 0.5, "output_price_per_m": 2.0, "cache_read_price_per_m": 0.5, "cache_write_price_per_m": 0.5,
    }
    # the first edit rewrites the registry in the new shape
    store.update_model(store.list_providers()[0].id, "m", cache_read_price_per_m=0.05, price_source="litellm:m")
    again = ProviderStore(data_dir).list_providers()[0].models[0]
    assert (again.cache_read_price_per_m, again.cache_write_price_per_m, again.price_source) == (0.05, None, "litellm:m")


def test_cache_prices_can_be_set_cleared_and_left_alone(tmp_path) -> None:
    store = make_store(tmp_path)
    provider = create_provider(store)
    model = store.add_model(provider.id, "m", 1.0, 4.0, cache_read_price_per_m=0.1, cache_write_price_per_m=1.25, price_source="litellm:m")
    assert (model.cache_read_price_per_m, model.cache_write_price_per_m, model.price_source) == (0.1, 1.25, "litellm:m")
    untouched = store.update_model(provider.id, "m", output_price_per_m=5.0)
    assert (untouched.cache_read_price_per_m, untouched.cache_write_price_per_m) == (0.1, 1.25)  # not given: left alone
    cleared = store.update_model(provider.id, "m", cache_read_price_per_m=None)
    assert cleared.cache_read_price_per_m is None and cleared.cache_write_price_per_m == 1.25  # None: back to the input price
    with pytest.raises(ProviderError):
        store.update_model(provider.id, "m", cache_write_price_per_m=-1.0)
