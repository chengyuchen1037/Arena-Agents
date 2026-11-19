"""``SecretStore`` (v0.13, F21; contract section 11.2): the API keys are the only thing encrypted at rest."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import pytest

from app.storage import crypto
from app.storage.errors import StorageError
from app.storage.secrets import SECRETS_FILE, SecretStore, get_secret_store, reset_secret_store

KEY_A = "sk-test-AAAA-secret-value-111111"
KEY_B = "sk-test-BBBB-secret-value-222222"


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def store(root: Path) -> SecretStore:
    return SecretStore(root)


def no_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine without any master key (Linux without ARENA_MASTER_KEY / ARENA_MASTER_KEY_FILE)."""

    def provider(_data_dir: Path) -> bytes:
        raise StorageError("Encrypted storage needs ARENA_MASTER_KEY or ARENA_MASTER_KEY_FILE on this platform")

    crypto.set_key_provider_for_tests(provider)


def other_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARENA_MASTER_KEY", base64.urlsafe_b64encode(b"Z" * 32).decode("ascii"))
    crypto.reset_crypto_for_tests()


def test_keys_round_trip_and_the_file_holds_no_plaintext(store: SecretStore, root: Path) -> None:
    assert store.read_all() == {}  # no file yet
    store.write_all({"prov-a": KEY_A, "prov-b": KEY_B})

    raw = (root / SECRETS_FILE).read_bytes()
    assert KEY_A.encode() not in raw and KEY_B.encode() not in raw and b"prov-a" not in raw  # not even the ids
    assert json.loads(raw)["purpose"] == "secrets.v1"
    assert SecretStore(root).read_all() == {"prov-a": KEY_A, "prov-b": KEY_B}

    store.write_all({"prov-b": KEY_B})  # a replacement, not a merge
    assert store.read_all() == {"prov-b": KEY_B}
    assert [p.name for p in root.iterdir()] == [SECRETS_FILE]  # no temp files, no backup copies


def test_an_empty_mapping_removes_the_file_and_needs_no_master_key(store: SecretStore, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.write_all({"prov-a": KEY_A})
    assert (root / SECRETS_FILE).exists()
    no_master_key(monkeypatch)
    store.write_all({})
    assert not (root / SECRETS_FILE).exists() and store.read_all() == {}
    store.write_all({})  # nothing to remove: fine
    store.write_all({"prov-a": "", "prov-b": "   "})  # blank keys are no keys
    assert not (root / SECRETS_FILE).exists()


def test_without_a_master_key_a_key_cannot_be_saved_and_nothing_is_written(store: SecretStore, root: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    no_master_key(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(StorageError) as raised:
            store.write_all({"prov-a": KEY_A})
    assert "ARENA_MASTER_KEY" in str(raised.value) and KEY_A not in str(raised.value) and KEY_A not in caplog.text
    assert not root.exists() or [p for p in root.rglob("*") if p.is_file()] == []  # nothing in the clear, nothing at all
    assert store.read_all() == {}  # reading "no keys" needs no master key either


def test_a_file_that_cannot_be_decrypted_is_an_error_never_an_empty_result(store: SecretStore, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.write_all({"prov-a": KEY_A})
    good = (root / SECRETS_FILE).read_bytes()

    other_master_key(monkeypatch)  # another master key
    with pytest.raises(StorageError, match="无法解密") as wrong_key:
        store.read_all()
    assert KEY_A not in str(wrong_key.value)
    monkeypatch.setenv("ARENA_MASTER_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode("ascii"))
    crypto.reset_crypto_for_tests()
    assert store.read_all() == {"prov-a": KEY_A}

    envelope = json.loads(good)
    flipped = dict(envelope, ciphertext=("A" if envelope["ciphertext"][0] != "A" else "B") + envelope["ciphertext"][1:])
    for damaged in (json.dumps(flipped).encode(), good[: len(good) // 2], b"", b"not json", b"[]"):
        (root / SECRETS_FILE).write_bytes(damaged)
        with pytest.raises(StorageError):
            store.read_all()


def test_an_envelope_made_for_another_purpose_is_refused(store: SecretStore, root: Path) -> None:
    root.mkdir()
    (root / SECRETS_FILE).write_bytes(crypto.encrypt_json({"schema_version": 1, "keys": {"p": KEY_A}}, "providers.v1", data_dir=root))
    with pytest.raises(StorageError):
        store.read_all()  # the same master key, but not the secrets purpose


@pytest.mark.parametrize(
    "payload",
    [[], {"schema_version": 2, "keys": {"p": KEY_A}}, {"schema_version": 1}, {"schema_version": 1, "keys": []},
     {"schema_version": 1, "keys": {"p": 1}}, {"schema_version": 1, "keys": {"p": ""}}, {"keys": {"p": KEY_A}}],
)
def test_a_decrypted_document_of_the_wrong_shape_is_refused(store: SecretStore, root: Path, payload) -> None:
    root.mkdir()
    (root / SECRETS_FILE).write_bytes(crypto.encrypt_json(payload, "secrets.v1", data_dir=root))
    with pytest.raises(StorageError, match="格式无效"):
        store.read_all()


def test_bad_arguments_are_refused_before_anything_is_written(store: SecretStore, root: Path) -> None:
    with pytest.raises(TypeError):
        store.write_all([("p", KEY_A)])  # type: ignore[arg-type]
    for bad in ({"": KEY_A}, {"  ": KEY_A}, {"p": None}, {"p": 5}, {7: KEY_A}):
        with pytest.raises(StorageError):
            store.write_all(bad)  # type: ignore[arg-type]
    assert not root.exists()


def test_a_failed_write_keeps_the_old_keys(store: SecretStore, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.write_all({"prov-a": KEY_A})
    before = (root / SECRETS_FILE).read_bytes()
    no_master_key(monkeypatch)
    with pytest.raises(StorageError):
        store.write_all({"prov-a": KEY_B})
    assert (root / SECRETS_FILE).read_bytes() == before


def test_a_symlinked_key_file_is_refused(store: SecretStore, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.write_all({"prov-a": KEY_A})
    real = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == SECRETS_FILE or real(self))
    with pytest.raises(StorageError, match="符号链接"):
        store.read_all()
    with pytest.raises(StorageError, match="符号链接"):
        store.write_all({"prov-a": KEY_B})
    with pytest.raises(StorageError, match="符号链接"):
        store.write_all({})
    monkeypatch.setattr(Path, "is_symlink", real)
    assert store.read_all() == {"prov-a": KEY_A}


def test_the_module_level_store_follows_the_data_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setenv("ARENA_DATA_DIR", str(first))
    reset_secret_store()
    store = get_secret_store()
    assert store is get_secret_store() and store.data_dir == first.resolve() and store.path == first.resolve() / SECRETS_FILE
    store.write_all({"p": KEY_A})
    monkeypatch.setenv("ARENA_DATA_DIR", str(second))
    assert get_secret_store().data_dir == second.resolve() and get_secret_store().read_all() == {}
    monkeypatch.setenv("ARENA_DATA_DIR", str(first))
    assert get_secret_store().read_all() == {"p": KEY_A}
    assert get_secret_store().exists()


def test_the_key_is_never_part_of_what_is_logged(store: SecretStore, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        store.write_all({"prov-a": KEY_A})
        store.read_all()
    assert KEY_A not in caplog.text
