from __future__ import annotations

import json
import logging
import math
import uuid
from pathlib import Path

import pytest

from app.providers.models import (
    ModelBinding,
    ModelNotFound,
    ProviderError,
    ProviderNotFound,
)
from app.providers.store import ENV_PROVIDER_ID, ProviderStore
from app.storage import crypto
from app.storage.errors import StorageError
from app.storage.secrets import SecretStore

KEY = "test-key"
SECRET_VALUE = "sk-test-SECRET-VALUE-424242"


def make_store(tmp_path) -> ProviderStore:
    return ProviderStore(tmp_path / "external")


def create_provider(store: ProviderStore):
    return store.create_provider(
        "Test Provider", "openai_chat", "https://example.invalid", KEY
    )


def registry_of(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "external" / "providers.json").read_text(encoding="utf-8"))


def no_master_key() -> None:
    def provider(_data_dir: Path) -> bytes:
        raise StorageError("Encrypted storage needs ARENA_MASTER_KEY or ARENA_MASTER_KEY_FILE on this platform")

    crypto.set_key_provider_for_tests(provider)


def test_provider_crud_plain_registry_secret_keys_backup_and_resolve(tmp_path) -> None:
    store = make_store(tmp_path)
    provider = create_provider(store)
    uuid.UUID(hex=provider.id)
    assert KEY not in repr(provider)
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

    # the registry is plain, readable JSON without any key; the key is in the encrypted file only
    data = tmp_path / "external"
    plain = (data / "providers.json").read_text(encoding="utf-8")
    assert KEY not in plain and "api_key" not in plain and "Test Provider" in plain and "vendor/model" in plain
    assert (data / "providers.backup.json").is_file()
    assert KEY.encode() not in (data / "secrets.v1.enc").read_bytes()
    assert SecretStore(data).read_all() == {provider.id: KEY}
    assert ProviderStore(data).get_provider(provider.id).api_key == KEY  # merged back in when it is read

    renamed = store.update_provider(provider.id, name="Renamed", api_key="")
    assert renamed.name == "Renamed"
    assert renamed.api_key == KEY
    rotated = store.update_provider(provider.id, base_url="https://new.invalid", api_key="new-test-key")
    assert rotated.api_key == "new-test-key"
    assert SecretStore(data).read_all() == {provider.id: "new-test-key"}

    store.remove_model(provider.id, model.id)
    with pytest.raises(ModelNotFound):
        store.resolve(binding)
    store.delete_provider(provider.id)
    with pytest.raises(ProviderNotFound):
        store.get_provider(provider.id)
    assert SecretStore(data).read_all() == {}  # the key of a deleted provider does not stay behind
    assert not (data / "secrets.v1.enc").exists()


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
        store.create_provider("", "openai_chat", "https://example.invalid", KEY)
    with pytest.raises(ProviderError):
        store.create_provider("bad", "openai_chat", " ", KEY)
    with pytest.raises(ProviderError):
        store.create_provider("bad", "carrier_pigeon", "https://example.invalid", KEY)


def test_environment_provider_is_read_only_and_empty_key_is_unset(tmp_path, monkeypatch) -> None:
    store = make_store(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", " ")
    assert store.list_providers() == []
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-test-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "env-model")

    env_provider = store.get_provider(ENV_PROVIDER_ID)
    assert env_provider.read_only is True
    assert env_provider.source == "environment"
    assert env_provider.api_key == "environment-test-key"
    assert env_provider.models[0].id == "env-model"
    for operation in (
        lambda: store.update_provider(ENV_PROVIDER_ID, name="no"),
        lambda: store.delete_provider(ENV_PROVIDER_ID),
        lambda: store.add_model(ENV_PROVIDER_ID, "no", 1, 1),
    ):
        with pytest.raises(ProviderError):
            operation()
    # it is never stored: neither in the registry nor among the keys
    assert not (tmp_path / "external" / "providers.json").exists() and not (tmp_path / "external" / "secrets.v1.enc").exists()


def test_the_environment_provider_works_without_any_master_key(tmp_path, monkeypatch) -> None:
    no_master_key()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-test-key")
    store = make_store(tmp_path)
    assert [p.id for p in store.list_providers()] == [ENV_PROVIDER_ID]
    assert store.resolve(ModelBinding(provider_id=ENV_PROVIDER_ID, model_id="deepseek-flash"))[0].api_key == "environment-test-key"


def test_provider_falls_back_to_known_good_backup(tmp_path) -> None:
    store = make_store(tmp_path)
    provider = create_provider(store)
    store.update_provider(provider.id, name="known-good")
    store.update_provider(provider.id, name="newest")
    current = tmp_path / "external" / "providers.json"
    current.write_bytes(b"broken-current")
    reopened = ProviderStore(tmp_path / "external").get_provider(provider.id)
    assert reopened.name == "known-good" and reopened.api_key == KEY  # (the key does not depend on the registry copy)

    (tmp_path / "external" / "providers.backup.json").write_bytes(b"broken-backup")
    with pytest.raises(StorageError):
        ProviderStore(tmp_path / "external").list_providers()
    assert current.read_bytes() == b"broken-current"  # nothing was repaired or erased


def test_a_registry_of_the_wrong_shape_is_a_storage_error(tmp_path) -> None:
    data = tmp_path / "external"
    data.mkdir()
    for payload in (
        [], {"schema_version": 9, "providers": []}, {"schema_version": 1}, {"schema_version": 1, "providers": {}},
        {"schema_version": 1, "providers": ["x"]}, {"schema_version": 1, "providers": [{"id": "only"}]},
    ):
        (data / "providers.json").write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(StorageError):
            ProviderStore(data).list_providers()
    duplicate = {"id": "a", "name": "n", "protocol": "openai_chat", "base_url": "https://example.invalid", "models": []}
    (data / "providers.json").write_text(json.dumps({"schema_version": 1, "providers": [duplicate, duplicate]}), encoding="utf-8")
    with pytest.raises(StorageError, match="重复"):
        ProviderStore(data).list_providers()


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
    data_dir = tmp_path / "external"
    store = ProviderStore(data_dir)
    provider = create_provider(store)
    path = data_dir / "providers.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["providers"][0]["auto_append_path"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = ProviderStore(data_dir).get_provider(provider.id)
    assert reopened.auto_append_path is True


# ------------------------------------------------------------------------------ v0.13: keys apart
def test_the_registry_never_holds_a_key_and_a_key_found_in_it_is_ignored(tmp_path, caplog) -> None:
    data = tmp_path / "external"
    store = ProviderStore(data)
    provider = store.create_provider("P", "openai_chat", "https://example.invalid", SECRET_VALUE)
    for name in ("providers.json", "providers.backup.json"):
        path = data / name
        if path.exists():
            assert SECRET_VALUE not in path.read_text(encoding="utf-8")
    assert not any(SECRET_VALUE.encode() in p.read_bytes() for p in data.rglob("*") if p.is_file() and p.name != "master.v1.dpapi" and p.name != ".lock" and p.suffix != ".enc")

    # somebody pastes a key into the plain file: it is neither used nor kept
    path = data / "providers.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["providers"][0]["api_key"] = "sk-pasted-by-hand-000000"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        reopened = ProviderStore(data).get_provider(provider.id)
    assert reopened.api_key == SECRET_VALUE  # the stored key, not the pasted one
    assert "sk-pasted-by-hand-000000" not in caplog.text
    store.update_provider(provider.id, name="touched")
    assert "sk-pasted-by-hand-000000" not in path.read_text(encoding="utf-8")  # the next write drops it


def test_a_provider_without_a_key_is_listed_and_can_be_given_one_later(tmp_path) -> None:
    data = tmp_path / "external"
    store = ProviderStore(data)
    provider = store.create_provider("No key yet", "openai_chat", "https://example.invalid", "")
    assert provider.api_key == "" and store.get_provider(provider.id).api_key == ""
    assert not (data / "secrets.v1.enc").exists()  # nothing to encrypt
    store.add_model(provider.id, "m", 1, 2)

    moved = store.update_provider(provider.id, base_url="https://moved.invalid")  # nothing to protect: no key needed
    assert moved.base_url == "https://moved.invalid" and moved.api_key == ""

    keyed = store.update_provider(provider.id, api_key=SECRET_VALUE)
    assert keyed.api_key == SECRET_VALUE and ProviderStore(data).get_provider(provider.id).api_key == SECRET_VALUE
    assert SecretStore(data).read_all() == {provider.id: SECRET_VALUE}
    with pytest.raises(ProviderError):  # ... and now the rule applies
        store.update_provider(provider.id, base_url="https://elsewhere.invalid")


def test_without_a_master_key_a_provider_can_be_saved_but_not_its_key(tmp_path) -> None:
    no_master_key()
    data = tmp_path / "external"
    store = ProviderStore(data)

    keyless = store.create_provider("Keyless", "openai_chat", "https://example.invalid", "")
    assert [p.name for p in ProviderStore(data).list_providers()] == ["Keyless"]

    with pytest.raises(StorageError, match="ARENA_MASTER_KEY"):
        store.create_provider("With key", "openai_chat", "https://example.invalid", SECRET_VALUE)
    with pytest.raises(StorageError, match="ARENA_MASTER_KEY"):
        store.update_provider(keyless.id, api_key=SECRET_VALUE)

    # failing closed: no key anywhere, no half-created provider, nothing in the clear
    assert [p.name for p in ProviderStore(data).list_providers()] == ["Keyless"]
    assert not (data / "secrets.v1.enc").exists()
    assert not any(SECRET_VALUE.encode() in p.read_bytes() for p in data.rglob("*") if p.is_file())


def test_a_key_file_that_cannot_be_decrypted_leaves_the_registry_readable_and_is_never_overwritten(tmp_path, monkeypatch, caplog) -> None:
    data = tmp_path / "external"
    store = ProviderStore(data)
    provider = store.create_provider("P", "openai_chat", "https://example.invalid", SECRET_VALUE)
    store.add_model(provider.id, "m", 1, 2)
    good = (data / "secrets.v1.enc").read_bytes()

    monkeypatch.setenv("ARENA_MASTER_KEY", "WlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlo")  # another master key
    crypto.reset_crypto_for_tests()
    with caplog.at_level(logging.WARNING):
        reopened = ProviderStore(data)
        listed = reopened.list_providers()
    assert [p.name for p in listed] == ["P"] and listed[0].api_key == ""  # listed, "not set"
    assert reopened.keys_error and "无法解密" in reopened.keys_error
    assert SECRET_VALUE not in caplog.text and SECRET_VALUE not in reopened.keys_error

    # editing what needs no key still works; saving a key is refused, and the old key file stays as it is
    assert reopened.update_provider(provider.id, name="renamed").name == "renamed"
    reopened.add_model(provider.id, "m2", 1, 2)
    with pytest.raises(StorageError, match="无法解密"):
        reopened.update_provider(provider.id, api_key="sk-a-new-key-000000")
    with pytest.raises(StorageError, match="无法解密"):
        reopened.create_provider("Q", "openai_chat", "https://example.invalid", "sk-a-new-key-000000")
    assert (data / "secrets.v1.enc").read_bytes() == good
    assert [p.name for p in reopened.list_providers()] == ["renamed"]  # (the refused create left nothing behind)
    with pytest.raises(StorageError, match="删除数据目录里的 secrets.v1.enc"):  # ... and the way out is said in the message
        reopened.update_provider(provider.id, api_key="sk-a-new-key-000000")

    # while the stored key cannot be read it still counts as a key: the endpoint rule cannot be dodged by an edit
    with pytest.raises(ProviderError, match="api_key"):
        reopened.update_provider(provider.id, base_url="https://elsewhere.invalid")
    assert reopened.get_provider(provider.id).base_url == "https://example.invalid"

    reopened.delete_provider(provider.id)  # deleting never needs the keys
    assert (data / "secrets.v1.enc").read_bytes() == good  # (and cannot touch what it cannot read)


def test_keys_of_providers_that_are_gone_are_pruned_on_the_next_key_write(tmp_path) -> None:
    data = tmp_path / "external"
    store = ProviderStore(data)
    keep = store.create_provider("Keep", "openai_chat", "https://example.invalid", "keep-key")
    SecretStore(data).write_all({keep.id: "keep-key", "ghost-provider": "ghost-key"})
    other = store.create_provider("Other", "openai_chat", "https://example.invalid", "other-key")
    assert SecretStore(data).read_all() == {keep.id: "keep-key", other.id: "other-key"}


def test_a_failed_registry_write_does_not_leave_the_new_key_behind(tmp_path, monkeypatch) -> None:
    data = tmp_path / "external"
    store = ProviderStore(data)
    first = store.create_provider("First", "openai_chat", "https://example.invalid", "first-key")

    def failing_write(self, providers):
        raise StorageError("disk full")

    monkeypatch.setattr(ProviderStore, "_write_registry", failing_write)
    with pytest.raises(StorageError, match="disk full"):
        store.create_provider("Second", "openai_chat", "https://example.invalid", "second-key")
    assert SecretStore(data).read_all() == {first.id: "first-key"}


def test_the_registry_file_is_written_atomically_and_leaves_no_temporary_files(tmp_path) -> None:
    store = make_store(tmp_path)
    provider = create_provider(store)
    for index in range(5):
        store.add_model(provider.id, f"m{index}", 1, 2)
    names = sorted(p.name for p in (tmp_path / "external").iterdir())
    assert names == ["providers.backup.json", "providers.json", "secrets.v1.enc"]
    assert registry_of(tmp_path)["schema_version"] == 1


def test_a_symlinked_registry_is_refused(tmp_path, monkeypatch) -> None:
    store = make_store(tmp_path)
    create_provider(store)
    real = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "providers.json" or real(self))
    with pytest.raises(StorageError):
        ProviderStore(tmp_path / "external").list_providers()
    with pytest.raises(StorageError):
        store.create_provider("x", "openai_chat", "https://example.invalid", "")


# ------------------------------------------------------------------------------ v0.12.1: four prices
def test_a_registry_written_before_the_cache_prices_existed_still_loads_and_bills_like_before(tmp_path) -> None:
    data_dir = tmp_path / "external"
    old_registry = {
        "schema_version": 1,
        "providers": [
            {
                "id": uuid.uuid4().hex, "name": "Old", "protocol": "openai_chat", "base_url": "https://example.invalid",
                "read_only": False, "source": "encrypted", "auto_append_path": True,
                "models": [{"id": "m", "input_price_per_m": 0.5, "output_price_per_m": 2.0, "context_window_tokens": 64000}],
            }
        ],
    }
    data_dir.mkdir()
    (data_dir / "providers.json").write_text(json.dumps(old_registry), encoding="utf-8")

    store = ProviderStore(data_dir)
    model = store.list_providers()[0].models[0]
    assert (model.cache_read_price_per_m, model.cache_write_price_per_m, model.price_source) == (None, None, "manual")
    assert model.price_snapshot() == {  # an empty cache price is billed like an input token: nothing changes for old data
        "input_price_per_m": 0.5, "output_price_per_m": 2.0, "cache_read_price_per_m": 0.5, "cache_write_price_per_m": 0.5,
    }
    assert store.list_providers()[0].source == "encrypted"  # an old label is kept as it is
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
