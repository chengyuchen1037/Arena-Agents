from __future__ import annotations

import base64
import json
import os
import stat

import pytest

from app.storage.crypto import (
    decrypt_data,
    decrypt_json,
    encrypt_data,
    encrypt_json,
    reset_crypto_for_tests,
)


def test_aes_gcm_round_trip_tamper_truncation_and_wrong_key(
    tmp_path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    plaintext = b"provider-secret-and-session-state"
    encrypted = encrypt_data(plaintext, "providers.v1", data_dir=data_dir)

    assert plaintext not in encrypted
    assert decrypt_data(encrypted, "providers.v1", data_dir=data_dir) == plaintext
    with pytest.raises(Exception) as wrong_purpose:
        decrypt_data(encrypted, "session.v1:other", data_dir=data_dir)
    assert wrong_purpose.type.__name__ == "StorageError"

    envelope = json.loads(encrypted)
    ciphertext = envelope["ciphertext"]
    envelope["ciphertext"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
    with pytest.raises(Exception) as tampered:
        decrypt_data(
            json.dumps(envelope).encode(), "providers.v1", data_dir=data_dir
        )
    assert tampered.type.__name__ == "StorageError"

    with pytest.raises(Exception) as truncated:
        decrypt_data(encrypted[:24], "providers.v1", data_dir=data_dir)
    assert truncated.type.__name__ == "StorageError"

    monkeypatch.setenv(
        "ARENA_MASTER_KEY", base64.urlsafe_b64encode(b"Z" * 32).decode("ascii")
    )
    with pytest.raises(Exception) as wrong_key:
        decrypt_data(encrypted, "providers.v1", data_dir=data_dir)
    assert wrong_key.type.__name__ == "StorageError"


def test_json_encryption_and_native_key_source(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ARENA_MASTER_KEY", raising=False)
    monkeypatch.delenv("ARENA_MASTER_KEY_FILE", raising=False)
    reset_crypto_for_tests()
    data_dir = tmp_path / "native-key-data"
    payload = {"text": "你好", "number": 3}

    if os.name == "nt":
        encrypted = encrypt_json(payload, "test.native", data_dir=data_dir)
        assert (data_dir / "master.v1.dpapi").is_file()
        assert decrypt_json(encrypted, "test.native", data_dir=data_dir) == payload
    else:
        from app.storage.errors import StorageError

        with pytest.raises(StorageError):
            encrypt_json(payload, "test.native", data_dir=data_dir)
        assert not list(data_dir.glob("*")) if data_dir.exists() else True


def test_master_key_file_accepts_base64url(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "secret" / "master.key"
    key_file.parent.mkdir()
    key_file.write_text(
        base64.urlsafe_b64encode(bytes(reversed(range(32)))).decode("ascii").rstrip("="),
        encoding="ascii",
    )
    monkeypatch.delenv("ARENA_MASTER_KEY", raising=False)
    monkeypatch.setenv("ARENA_MASTER_KEY_FILE", str(key_file))
    encrypted = encrypt_data(b"file-key", "test.file", data_dir=tmp_path / "data")
    assert decrypt_data(encrypted, "test.file", data_dir=tmp_path / "data") == b"file-key"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_ciphertext_permissions_are_private(tmp_path) -> None:
    data_dir = tmp_path / "private"
    encrypted = encrypt_json({"a": 1}, "test.permissions", data_dir=data_dir)
    target = data_dir / "payload.enc"
    from app.storage._atomic import atomic_write

    atomic_write(target, encrypted)
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
