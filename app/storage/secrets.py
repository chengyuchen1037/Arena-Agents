"""The API keys: the only thing that is encrypted at rest (v0.13, decision F21).

``<data>/secrets.v1.enc`` holds ``{provider_id: api_key}`` in an AES-256-GCM envelope (:mod:`app.storage.crypto`,
purpose ``secrets.v1``).  The master key comes from ``ARENA_MASTER_KEY`` / ``ARENA_MASTER_KEY_FILE`` or, on Windows,
from a DPAPI-protected file next to the data (``master.v1.dpapi``).  Where there is none (Linux without the
variables) a key can not be saved at all: the store fails closed and never writes a key in the clear.

Everything else a provider consists of (name, protocol, address, models, prices) is plain JSON in
``providers.json`` (see :mod:`app.providers.store`); it never contains a key.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from app.storage._atomic import atomic_write
from app.storage.crypto import decrypt_json, encrypt_json
from app.storage.errors import StorageError
from app.storage.paths import get_data_dir

SECRETS_FILE = "secrets.v1.enc"
SECRETS_PURPOSE = "secrets.v1"
_SCHEMA_VERSION = 1


def _why(exc: StorageError) -> str:
    """A fixed, key-free explanation of a crypto failure (the original text is English and technical)."""
    text = str(exc)
    if "ARENA_MASTER_KEY" in text and "needs" in text:
        return "没有可用的主密钥（请设置 ARENA_MASTER_KEY 或 ARENA_MASTER_KEY_FILE）"
    if "master key" in text.lower() or "DPAPI" in text:
        return "主密钥无效或无法取得"
    return "主密钥不匹配，或密钥文件已损坏"


class SecretStore:
    """``{provider_id: api_key}`` of one data directory, encrypted with the master key."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = (data_dir or get_data_dir()).resolve()
        self._path = self._data_dir / SECRETS_FILE
        self._lock = threading.RLock()

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists() or self._path.is_symlink()

    def read_all(self) -> dict[str, str]:
        """Every stored key; ``{}`` when there is no file.  Raises :class:`StorageError` when it cannot be decrypted."""
        with self._lock:
            if self._path.is_symlink():
                raise StorageError("拒绝读取符号链接形式的密钥文件。")
            if not self._path.exists():
                return {}
            try:
                encrypted = self._path.read_bytes()
            except OSError as exc:
                raise StorageError("无法读取已保存的 API Key 文件。") from exc
            try:
                payload = decrypt_json(encrypted, SECRETS_PURPOSE, data_dir=self._data_dir)
            except StorageError as exc:
                raise StorageError(f"无法解密已保存的 API Key：{_why(exc)}。") from exc
            return self._keys_of(payload)

    def write_all(self, keys: dict[str, str]) -> None:
        """Replace the stored keys.  An empty mapping removes the file (no master key is needed for that)."""
        if not isinstance(keys, dict):
            raise TypeError("keys must be a dict")
        cleaned: dict[str, str] = {}
        for provider_id, key in keys.items():
            if not isinstance(provider_id, str) or not provider_id.strip() or not isinstance(key, str):
                raise StorageError("API Key 的格式无效，没有保存。")
            if key.strip():
                cleaned[provider_id] = key
        with self._lock:
            if self._path.is_symlink():
                raise StorageError("拒绝写入符号链接形式的密钥文件。")
            if not cleaned:
                try:
                    self._path.unlink(missing_ok=True)
                except OSError as exc:
                    raise StorageError("无法删除密钥文件。") from exc
                return
            payload: dict[str, Any] = {"schema_version": _SCHEMA_VERSION, "keys": cleaned}
            try:
                encrypted = encrypt_json(payload, SECRETS_PURPOSE, data_dir=self._data_dir)
            except StorageError as exc:
                raise StorageError(f"不能保存 API Key：{_why(exc)}。") from exc
            atomic_write(self._path, encrypted)
            if self.read_all() != cleaned:
                raise StorageError("API Key 文件写入后校验失败。")

    @staticmethod
    def _keys_of(payload: Any) -> dict[str, str]:
        keys = payload.get("keys") if isinstance(payload, dict) and payload.get("schema_version") == _SCHEMA_VERSION else None
        if not isinstance(keys, dict) or any(
            not isinstance(provider_id, str) or not isinstance(key, str) or not key for provider_id, key in keys.items()
        ):
            raise StorageError("已保存的 API Key 文件格式无效。")
        return dict(keys)


_store_lock = threading.RLock()
_secret_store: SecretStore | None = None


def get_secret_store() -> SecretStore:
    """The store of the current data directory (a new one is made when ``ARENA_DATA_DIR`` points elsewhere)."""
    global _secret_store
    with _store_lock:
        data_dir = get_data_dir()
        if _secret_store is None or _secret_store.data_dir != data_dir:
            _secret_store = SecretStore(data_dir)
        return _secret_store


def reset_secret_store() -> None:
    global _secret_store
    with _store_lock:
        _secret_store = None


__all__ = ["SECRETS_FILE", "SECRETS_PURPOSE", "SecretStore", "get_secret_store", "reset_secret_store"]
