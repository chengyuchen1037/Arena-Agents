from __future__ import annotations

import pytest

from app.storage.errors import StorageError
from app.storage.sessions import SessionStore


def test_session_round_trip_previous_backup_and_metadata(tmp_path) -> None:
    store = SessionStore(tmp_path / "external")
    first = {"session_id": "game_1", "private_history": [{"text": "alpha"}]}
    second = {"session_id": "game_1", "private_history": [{"text": "beta"}]}

    store.save("game_1", first)
    current = tmp_path / "external" / "sessions" / "game_1" / "current.json.enc"
    assert b"alpha" not in current.read_bytes()
    store.save("game_1", second)

    previous = current.with_name("previous.json.enc")
    assert previous.is_file()
    assert store.load("game_1") == second
    current.write_bytes(b"corrupt-current")
    assert store.load("game_1") == first

    metadata = store.list_sessions()
    assert len(metadata) == 1
    assert metadata[0]["session_id"] == "game_1"
    assert set(metadata[0]) == {"session_id", "updated_at", "size_bytes"}
    assert "private_history" not in str(metadata)
    assert str(tmp_path) not in str(metadata)

    store.delete("game_1")
    assert not store.exists("game_1")


def test_session_rejects_traversal_reserved_names_and_ciphertext_swap(tmp_path) -> None:
    store = SessionStore(tmp_path / "external")
    for invalid in ("../escape", "a/b", ".", "CON", "lpt1", "x" * 65, ""):
        with pytest.raises(ValueError):
            store.save(invalid, {})

    store.save("one", {"value": 1})
    store.save("two", {"value": 2})
    root = tmp_path / "external" / "sessions"
    (root / "two" / "current.json.enc").write_bytes(
        (root / "one" / "current.json.enc").read_bytes()
    )
    with pytest.raises(StorageError):
        store.load("two")


def test_session_failure_preserves_current_and_previous(tmp_path) -> None:
    store = SessionStore(tmp_path / "external")
    store.save("game", {"version": 1})
    store.save("game", {"version": 2})
    session_dir = tmp_path / "external" / "sessions" / "game"
    current = session_dir / "current.json.enc"
    previous = session_dir / "previous.json.enc"
    old_previous = previous.read_bytes()

    current.write_bytes(b"broken")
    assert store.load("game") == {"version": 1}
    store.save("game", {"version": 3})
    assert previous.read_bytes() == old_previous
    assert store.load("game") == {"version": 3}

    current.write_bytes(b"broken-again")
    previous.write_bytes(b"broken-too")
    with pytest.raises(StorageError):
        store.load("game")
    assert current.read_bytes() == b"broken-again"
    assert previous.read_bytes() == b"broken-too"
