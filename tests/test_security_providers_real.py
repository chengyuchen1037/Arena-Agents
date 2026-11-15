"""End-to-end API tests against the REAL encrypted stores (temp data dir, test master key)."""

from __future__ import annotations

import json

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


def test_provider_lifecycle_is_encrypted_at_rest_and_key_never_returned(client, real):
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
    assert NEW_KEY.encode() not in blob
    assert b"api.anthropic.com" not in blob  # even the URL is inside the ciphertext

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


def test_session_store_list_load_delete_through_api(client, real):
    from app.storage.sessions import get_session_store

    store = get_session_store()
    good = json.loads(real.session.model_dump_json())
    good["session_id"] = "stored-1"
    store.save("stored-1", good)
    store.save("stored-2", {**good, "session_id": "stored-2"})

    listing = client.get("/api/sessions").json()["sessions"]
    assert {s["session_id"] for s in listing} == {"stored-1", "stored-2"}
    assert all("path" not in key for s in listing for key in s)
    assert str(real.data_dir) not in json.dumps(listing)

    assert client.post("/api/sessions/stored-1/load").status_code == 200
    real.orch.load_session_data.assert_awaited_once()

    assert client.delete("/api/sessions/stored-2").json() == {"ok": True}
    assert not store.exists("stored-2")
    assert client.delete("/api/sessions/stored-2").status_code == 404
    # current game cannot be deleted
    store.save("sec-test", {**good, "session_id": "sec-test"})
    assert client.delete("/api/sessions/sec-test").status_code == 409


@pytest.mark.parametrize("bad", ["CON", "nul", "COM1", "aux"])
def test_reserved_windows_names_are_clean_400s(client, real, bad):
    assert client.delete(f"/api/sessions/{bad}").status_code == 400
    assert client.post(f"/api/sessions/{bad}/load").status_code == 400


def test_import_with_reserved_session_id_is_rejected_before_replacing(client, real):
    payload = json.loads(real.session.model_dump_json())
    payload["session_id"] = "CON"
    response = client.post("/api/session/import", content=json.dumps(payload).encode())
    assert response.status_code == 400
    real.orch.load_session_data.assert_not_awaited()


def test_import_collision_with_real_store_gets_a_new_id(client, real):
    from app.storage.sessions import get_session_store

    payload = json.loads(real.session.model_dump_json())
    payload["session_id"] = "taken-1"
    get_session_store().save("taken-1", payload)
    response = client.post("/api/session/import", content=json.dumps(payload).encode())
    assert response.status_code == 200
    assert response.json()["renamed_from"] == "taken-1"
    assert real.orch.load_session_data.await_args.args[0]["session_id"] != "taken-1"


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
