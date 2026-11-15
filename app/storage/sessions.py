from __future__ import annotations

import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.storage._atomic import atomic_write, ensure_private_directory
from app.storage._validation import validate_session_id
from app.storage.crypto import decrypt_json, encrypt_json
from app.storage.errors import StorageError
from app.storage.paths import get_data_dir

_CURRENT_FILE = "current.json.enc"
_PREVIOUS_FILE = "previous.json.enc"


def _purpose(session_id: str) -> str:
    return f"session.v1:{session_id}"


class SessionStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = (data_dir or get_data_dir()).resolve()
        self._sessions_dir = self._data_dir / "sessions"
        self._lock = threading.RLock()

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def _session_dir(self, session_id: str) -> Path:
        validate_session_id(session_id)
        return self._sessions_dir / session_id

    def _load_file(self, session_id: str, path: Path) -> dict:
        if path.is_symlink():
            raise StorageError("Refusing to read a symbolic-link session file")
        try:
            encrypted = path.read_bytes()
        except OSError as exc:
            raise StorageError("Unable to read the encrypted session") from exc
        payload = decrypt_json(encrypted, _purpose(session_id), data_dir=self._data_dir)
        if not isinstance(payload, dict):
            raise StorageError("Encrypted session payload must be a JSON object")
        return payload

    def save(self, session_id: str, payload: dict) -> None:
        if not isinstance(payload, dict):
            raise TypeError("session payload must be a dict")
        session_dir = self._session_dir(session_id)
        with self._lock:
            ensure_private_directory(self._sessions_dir)
            ensure_private_directory(session_dir)
            current_path = session_dir / _CURRENT_FILE
            previous_path = session_dir / _PREVIOUS_FILE
            encrypted = encrypt_json(
                payload, _purpose(session_id), data_dir=self._data_dir
            )

            if current_path.exists():
                try:
                    self._load_file(session_id, current_path)
                    previous = current_path.read_bytes()
                except (OSError, StorageError):
                    previous = None
                if previous is not None:
                    atomic_write(previous_path, previous)

            atomic_write(current_path, encrypted)
            if self._load_file(session_id, current_path) != payload:
                raise StorageError("Encrypted session verification failed")

    def load(self, session_id: str) -> dict:
        session_dir = self._session_dir(session_id)
        with self._lock:
            if self._sessions_dir.is_symlink() or session_dir.is_symlink():
                raise StorageError("Refusing to read a symbolic-link session directory")
            found = False
            for filename in (_CURRENT_FILE, _PREVIOUS_FILE):
                path = session_dir / filename
                if not path.exists():
                    continue
                found = True
                try:
                    return self._load_file(session_id, path)
                except StorageError:
                    continue
            if found:
                raise StorageError("Session and its previous backup could not be decrypted")
            raise StorageError("Session not found")

    def exists(self, session_id: str) -> bool:
        session_dir = self._session_dir(session_id)
        with self._lock:
            if self._sessions_dir.is_symlink() or session_dir.is_symlink():
                raise StorageError("Refusing to inspect a symbolic-link session directory")
            return (session_dir / _CURRENT_FILE).exists() or (
                session_dir / _PREVIOUS_FILE
            ).exists()

    def list_sessions(self) -> list[dict]:
        with self._lock:
            if not self._sessions_dir.exists():
                return []
            if self._sessions_dir.is_symlink():
                raise StorageError("Refusing to read a symbolic-link sessions directory")
            try:
                directories = list(self._sessions_dir.iterdir())
            except OSError as exc:
                raise StorageError("Unable to list external sessions") from exc
            sessions: list[tuple[float, dict]] = []
            for session_dir in directories:
                if session_dir.is_symlink() or not session_dir.is_dir():
                    continue
                try:
                    validate_session_id(session_dir.name)
                except ValueError:
                    continue
                current = session_dir / _CURRENT_FILE
                previous = session_dir / _PREVIOUS_FILE
                candidate = current if current.exists() else previous
                if not candidate.exists() or candidate.is_symlink():
                    continue
                try:
                    stat = candidate.stat()
                except OSError:
                    continue
                metadata = {
                    "session_id": session_dir.name,
                    "updated_at": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(),
                    "size_bytes": stat.st_size,
                }
                sessions.append((stat.st_mtime, metadata))
            sessions.sort(key=lambda item: (-item[0], item[1]["session_id"]))
            return [metadata for _, metadata in sessions]

    def delete(self, session_id: str) -> None:
        session_dir = self._session_dir(session_id)
        with self._lock:
            if not session_dir.exists():
                return
            if session_dir.is_symlink():
                raise StorageError("Refusing to delete a symbolic-link session directory")
            try:
                shutil.rmtree(session_dir)
            except OSError as exc:
                raise StorageError("Unable to delete the external session") from exc


_store_lock = threading.RLock()
_session_store: SessionStore | None = None


def get_session_store() -> SessionStore:
    global _session_store
    with _store_lock:
        data_dir = get_data_dir()
        if _session_store is None or _session_store.data_dir != data_dir:
            _session_store = SessionStore(data_dir)
        return _session_store


def reset_session_store() -> None:
    global _session_store
    with _store_lock:
        _session_store = None


def save(session_id: str, payload: dict) -> None:
    get_session_store().save(session_id, payload)


def load(session_id: str) -> dict:
    return get_session_store().load(session_id)


def exists(session_id: str) -> bool:
    return get_session_store().exists(session_id)


def list_sessions() -> list[dict]:
    return get_session_store().list_sessions()


def delete(session_id: str) -> None:
    get_session_store().delete(session_id)


__all__ = [
    "SessionStore",
    "delete",
    "exists",
    "get_session_store",
    "list_sessions",
    "load",
    "reset_session_store",
    "save",
]
