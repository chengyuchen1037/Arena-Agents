"""End-to-end API tests against the REAL stores (temp data dir, test master key): plain registry, encrypted keys."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.storage import crypto, reset_stores_for_tests
from sec_api_fakes import install

NEW_KEY = "sk-real-store-key-424242"


@pytest.fixture
def real(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    for name in ("ARENA_API_TOKEN", "ARENA_ALLOWED_BASE_HOSTS", "DEEPSEEK_API_KEY", "ARENA_MASTER_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARENA_DATA_DIR", str(data_dir))
    reset_stores_for_tests()
    crypto.set_key_provider_for_tests(lambda _dir: b"\x07" * 32)
    env = install(monkeypatch, real_stores=True)
    env.data_dir = data_dir
    yield env
    reset_stores_for_tests()


@pytest.fixture
def client(real):
    return TestClient(app)


def everything_on_disk(data_dir) -> bytes:
    return b"".join(p.read_bytes() for p in data_dir.rglob("*") if p.is_file())


def test_provider_lifecycle_keeps_the_key_encrypted_at_rest_and_never_returns_it(client, real):
    created = client.post(
        "/api/providers",
        json={
            "name": "Real",
            "protocol": "anthropic_messages",
            "base_url": "https://api.anthropic.com",
            "api_key": NEW_KEY,
        },
    )
    assert created.status_code == 201
    provider_id = created.json()["id"]
    assert len(provider_id) == 32  # uuid4 hex

    added = client.post(
        f"/api/providers/{provider_id}/models",
        json={"model_id": "claude-x", "input_price_per_m": 3, "output_price_per_m": 15},
    )
    assert added.status_code == 201

    listing = client.get("/api/providers")
    assert NEW_KEY not in listing.text
    assert listing.json()["providers"][0]["models"][0]["id"] == "claude-x"

    blob = everything_on_disk(real.data_dir)
    assert blob  # something was written
    assert NEW_KEY.encode() not in blob  # the key is on disk only inside secrets.v1.enc, encrypted
    assert (real.data_dir / "secrets.v1.enc").is_file()
    registry = (real.data_dir / "providers.json").read_text(encoding="utf-8")
    assert "api.anthropic.com" in registry and "claude-x" in registry  # everything else is plain, readable config
    assert "api_key" not in registry

    # duplicates / prices handled
    dup = client.post(
        f"/api/providers/{provider_id}/models",
        json={"model_id": "claude-x", "input_price_per_m": 1, "output_price_per_m": 1},
    )
    assert dup.status_code == 409
    edited = client.patch(
        f"/api/providers/{provider_id}/models", json={"model_id": "claude-x", "input_price_per_m": 4.5}
    )
    assert edited.json()["input_price_per_m"] == 4.5

    # key rotation keeps everything else; redirect needs a key
    assert client.patch(f"/api/providers/{provider_id}", json={"base_url": "https://api.example.com"}).status_code == 400
    assert client.patch(f"/api/providers/{provider_id}", json={"api_key": NEW_KEY + "2"}).status_code == 200


def test_bindings_roundtrip_and_delete_protection_with_real_store(client, real):
    provider_id = client.post(
        "/api/providers",
        json={"name": "R", "protocol": "openai_chat", "base_url": "https://api.example.com/v1", "api_key": NEW_KEY},
    ).json()["id"]
    client.post(
        f"/api/providers/{provider_id}/models",
        json={"model_id": "m1", "input_price_per_m": 1, "output_price_per_m": 2},
    )
    put = client.put(
        "/api/settings/bindings",
        json={"bindings": {"moderator": {"provider_id": provider_id, "model_id": "m1", "reasoning_depth": "low"}}},
    )
    assert put.status_code == 200
    assert put.json()["bindings"]["moderator"]["reasoning_depth"] == "low"
    assert client.delete(f"/api/providers/{provider_id}").status_code == 409
    bad = client.put(
        "/api/settings/bindings",
        json={"bindings": {"p01": {"provider_id": provider_id, "model_id": "nope"}}},
    )
    assert bad.status_code == 400
    assert NEW_KEY not in put.text + bad.text


def test_environment_provider_is_read_only_and_listed(client, real, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-provider-key-1")
    listing = client.get("/api/providers")
    env_providers = [p for p in listing.json()["providers"] if p["read_only"]]
    assert len(env_providers) == 1
    assert "sk-env-provider-key-1" not in listing.text
    provider_id = env_providers[0]["id"]
    assert client.patch(f"/api/providers/{provider_id}", json={"name": "x"}).status_code == 403
    assert client.delete(f"/api/providers/{provider_id}").status_code == 403
    assert (
        client.post(
            f"/api/providers/{provider_id}/models",
            json={"model_id": "z", "input_price_per_m": 1, "output_price_per_m": 1},
        ).status_code
        == 403
    )


def test_a_provider_without_a_key_is_listed_as_not_set_and_a_key_can_be_added_later(client, real):
    """What an archive imported without keys leaves behind (F32): the provider is there, its key is not."""
    from app.providers.store import ProviderStore

    keyless = ProviderStore(real.data_dir).create_provider("Imported", "openai_chat", "https://api.example.com/v1", "")
    ProviderStore(real.data_dir).add_model(keyless.id, "m1", 1, 2)

    listed = client.get("/api/providers").json()["providers"]
    assert [(p["name"], p["api_key_set"]) for p in listed] == [("Imported", False)]
    bound = client.put(
        "/api/settings/bindings",
        json={"bindings": {"moderator": {"provider_id": keyless.id, "model_id": "m1", "reasoning_depth": "low"}}},
    )
    assert bound.status_code == 200
    issue = next(i for i in bound.json()["issues"] if i["actor_id"] == "moderator")
    assert issue["message"] == "Provider「Imported」未设置 API Key"

    # nothing to protect yet: it may be moved without typing a key; then the key makes it usable
    assert client.patch(f"/api/providers/{keyless.id}", json={"base_url": "https://api.other.example.com/v1"}).status_code == 200
    assert client.patch(f"/api/providers/{keyless.id}", json={"api_key": NEW_KEY}).json()["api_key_set"] is True
    after = client.get("/api/settings/bindings").json()["issues"]
    assert all(i["actor_id"] != "moderator" for i in after)
    assert NEW_KEY not in client.get("/api/providers").text
    # ... and from then on a key protects its endpoint
    assert client.patch(f"/api/providers/{keyless.id}", json={"base_url": "https://api.third.example.com/v1"}).status_code == 400


def test_storage_unavailable_surfaces_503_not_plaintext_fallback(client, real, monkeypatch):
    def no_key(_dir):
        from app.storage.errors import StorageError

        raise StorageError("主密钥不可用")

    crypto.set_key_provider_for_tests(no_key)
    created = client.post(
        "/api/providers",
        json={"name": "X", "protocol": "openai_chat", "base_url": "https://api.example.com", "api_key": NEW_KEY},
    )
    assert created.status_code == 503
    assert "主密钥" in created.json()["detail"]
    assert NEW_KEY.encode() not in everything_on_disk(real.data_dir) if real.data_dir.exists() else True
    assert not (real.data_dir / "providers.json").exists()  # no half-made provider without its key
    crypto.set_key_provider_for_tests(lambda _dir: b"\x07" * 32)
    assert client.get("/api/providers").json() == {"providers": []}


def test_a_key_that_can_no_longer_be_decrypted_does_not_take_the_providers_down(client, real):
    created = client.post(
        "/api/providers",
        json={"name": "Real", "protocol": "openai_chat", "base_url": "https://api.example.com/v1", "api_key": NEW_KEY},
    ).json()
    crypto.set_key_provider_for_tests(lambda _dir: b"\x08" * 32)  # another master key (the data was copied elsewhere)
    listing = client.get("/api/providers")
    assert listing.status_code == 200 and listing.json()["providers"][0]["api_key_set"] is False
    refused = client.patch(f"/api/providers/{created['id']}", json={"api_key": NEW_KEY + "2"})
    assert refused.status_code == 503 and "无法解密" in refused.json()["detail"] and NEW_KEY not in refused.text
    assert client.patch(f"/api/providers/{created['id']}", json={"name": "Renamed"}).status_code == 200
