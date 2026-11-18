"""The provider registry (v0.13, decision F21).

Everything about a provider except its API key is plain JSON in ``<data>/providers.json`` (name, protocol, address,
models, prices): it can be read, edited by hand and exported.  The keys live apart, encrypted, in
``<data>/secrets.v1.enc`` (:mod:`app.storage.secrets`); :class:`ProviderStore` merges them into
``ProviderConfig.api_key`` when it reads, so the rest of the program never notices the split.

* A provider can exist without a key (an imported archive without keys, F32): it shows as "not set" and cannot be
  used until a key is given.  Saving a *key* needs the master key (fail closed, see ``secrets``).
* A key that cannot be decrypted (another master key, a damaged file) leaves the registry readable: the providers
  are listed without keys and :attr:`ProviderStore.keys_error` says why; writing a new key is refused until the
  old file is dealt with, so a recoverable key file is never overwritten.
* ``providers.backup.json`` is the previous good copy of the registry.
* The environment provider (``DEEPSEEK_API_KEY``) is never stored: it is added on every read, read-only.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.providers.models import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    ModelBinding,
    ModelEntry,
    ModelNotFound,
    ProviderConfig,
    ProviderError,
    ProviderNotFound,
)
from app.storage._atomic import atomic_write
from app.storage.errors import StorageError
from app.storage.paths import get_data_dir
from app.storage.secrets import SecretStore

logger = logging.getLogger(__name__)

_PROVIDER_FILE = "providers.json"
_PROVIDER_BACKUP_FILE = "providers.backup.json"
_SCHEMA_VERSION = 1
ENV_PROVIDER_ID = "env-deepseek"


def _required_text(value: str | None, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(f"{label} must not be empty")
    return value


def _provider_from_data(data: dict) -> ProviderConfig:
    try:
        return ProviderConfig.model_validate(data)
    except (ValidationError, TypeError) as exc:
        raise ProviderError("Invalid provider configuration") from exc


def _key_of(value: Any) -> str:
    """A key as given by a caller: blank / missing means "no key"."""
    return value if isinstance(value, str) and value.strip() else ""


#: Marker for "argument not given" where ``None`` is a meaningful value (a cleared cache price).
UNSET: Any = object()


def _model_from_data(data: dict) -> ModelEntry:
    try:
        return ModelEntry.model_validate(data)
    except (ValidationError, TypeError) as exc:
        raise ProviderError("Invalid model configuration") from exc


def _environment_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ProviderError(f"Invalid {name} value") from exc


def _environment_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ProviderError(f"Invalid {name} value") from exc


def _environment_provider() -> ProviderConfig | None:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if api_key is None or not api_key.strip():
        return None
    base_url = os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    model_id = os.environ.get("DEEPSEEK_MODEL") or "deepseek-flash"
    model = _model_from_data(
        {
            "id": model_id,
            "input_price_per_m": _environment_float(
                "DEEPSEEK_INPUT_PRICE_PER_M", 0.27
            ),
            "output_price_per_m": _environment_float(
                "DEEPSEEK_OUTPUT_PRICE_PER_M", 1.10
            ),
            "context_window_tokens": _environment_int(
                "DEEPSEEK_CONTEXT_WINDOW_TOKENS", DEFAULT_CONTEXT_WINDOW_TOKENS
            ),
        }
    )
    return _provider_from_data(
        {
            "id": ENV_PROVIDER_ID,
            "name": "DeepSeek（环境变量）",
            "protocol": "openai_chat",
            "base_url": base_url,
            "api_key": api_key,
            "models": [model.model_dump(mode="json")],
            "read_only": True,
            "source": "environment",
        }
    )


class ProviderStore:
    """Thread-safe provider registry: plain JSON with a known-good backup, keys in the secret store."""

    def __init__(self, data_dir: Path | None = None, *, secrets: SecretStore | None = None) -> None:
        self._data_dir = (data_dir or get_data_dir()).resolve()
        self._path = self._data_dir / _PROVIDER_FILE
        self._backup_path = self._data_dir / _PROVIDER_BACKUP_FILE
        self._secrets = secrets if secrets is not None else SecretStore(self._data_dir)
        self._lock = threading.RLock()
        self._keys_error: str | None = None

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def keys_error(self) -> str | None:
        """Why the stored API keys could not be read the last time they were needed (``None`` when they could)."""
        return self._keys_error

    # ------------------------------------------------------------------ registry file ---
    def _load_registry_file(self, path: Path) -> list[ProviderConfig]:
        if path.is_symlink():
            raise StorageError("拒绝读取符号链接形式的 Provider 注册表。")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise StorageError("无法读取 Provider 注册表。") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != _SCHEMA_VERSION
            or not isinstance(payload.get("providers"), list)
        ):
            raise StorageError("Provider 注册表的格式无效。")
        providers: list[ProviderConfig] = []
        for item in payload["providers"]:
            if not isinstance(item, dict):
                raise StorageError("Provider 注册表的内容无效。")
            if "api_key" in item:
                # keys live in secrets.v1.enc only: one found in the plain file is never used
                logger.warning("providers.json carries an api_key for a provider; it is ignored")
            try:
                providers.append(ProviderConfig.model_validate({**item, "api_key": ""}))
            except (ValidationError, TypeError) as exc:
                raise StorageError("Provider 注册表的内容无效。") from exc
        provider_ids = [provider.id for provider in providers]
        if len(provider_ids) != len(set(provider_ids)):
            raise StorageError("Provider 注册表里有重复的 Provider id。")
        for provider in providers:
            model_ids = [model.id for model in provider.models]
            if len(model_ids) != len(set(model_ids)):
                raise StorageError("Provider 注册表里有重复的模型 id。")
        return providers

    def _read_registry(self) -> list[ProviderConfig]:
        """The stored providers (without keys): the registry, else its backup, else none yet."""
        current_exists = self._path.exists() or self._path.is_symlink()
        backup_exists = self._backup_path.exists() or self._backup_path.is_symlink()
        current_error: StorageError | None = None
        if current_exists:
            try:
                return self._load_registry_file(self._path)
            except StorageError as exc:
                current_error = exc
        if backup_exists:
            try:
                return self._load_registry_file(self._backup_path)
            except StorageError:
                pass
        elif current_error is not None:
            raise current_error  # no backup to fall back on: say what is wrong with the registry itself
        if current_error is not None or current_exists or backup_exists:
            raise StorageError("Provider 注册表和它的备份都无法读取。")
        return []

    def _write_registry(self, providers: list[ProviderConfig]) -> None:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "providers": [provider.model_dump(mode="json", exclude={"api_key"}) for provider in providers],
        }
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

        if self._path.exists():
            try:
                self._load_registry_file(self._path)
                previous = self._path.read_bytes()
            except (OSError, StorageError):
                previous = None
            if previous is not None:
                atomic_write(self._backup_path, previous)

        atomic_write(self._path, encoded)
        try:
            verified = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise StorageError("无法校验刚写入的 Provider 注册表。") from exc
        if verified != payload:
            raise StorageError("Provider 注册表写入后校验失败。")

    # -------------------------------------------------------------------------- keys ---
    def _read_keys(self) -> dict[str, str]:
        """The stored keys; unreadable ones count as none (and :attr:`keys_error` says why)."""
        try:
            keys = self._secrets.read_all()
        except StorageError as exc:
            message = str(exc)
            if message != self._keys_error:
                logger.warning("The stored API keys cannot be read: %s", message)
            self._keys_error = message
            return {}
        self._keys_error = None
        return keys

    def _save_key(self, provider_id: str, key: str, known_ids: set[str]) -> None:
        """Store ``key`` for ``provider_id`` (and drop the keys of providers that no longer exist).  Strict: a key
        file that cannot be read is an error here, never silently replaced."""
        try:
            stored = self._secrets.read_all()
        except StorageError as exc:
            raise StorageError(
                f"{exc}若主密钥已经丢失，可以删除数据目录里的 secrets.v1.enc，再重新输入各个 Provider 的 Key。"
            ) from exc
        keys = {pid: kept for pid, kept in stored.items() if pid in known_ids}
        keys[provider_id] = key
        self._secrets.write_all(keys)
        self._keys_error = None

    def _forget_key(self, provider_id: str, known_ids: set[str]) -> None:
        """Remove the key of a provider that is gone.  A key file that cannot be read is left as it is."""
        try:
            stored = self._secrets.read_all()
        except StorageError:
            logger.warning("The key of a deleted provider could not be removed (the key file is unreadable)")
            return
        keys = {pid: key for pid, key in stored.items() if pid in known_ids and pid != provider_id}
        if keys != stored:
            self._secrets.write_all(keys)

    # --------------------------------------------------------------------- providers ---
    def list_providers(self) -> list[ProviderConfig]:
        with self._lock:
            providers = self._read_registry()
            keys = self._read_keys() if providers else {}
            for provider in providers:
                provider.api_key = keys.get(provider.id, "")
            environment_provider = _environment_provider()
            if environment_provider is not None:
                providers.append(environment_provider)
            return providers

    def get_provider(self, provider_id) -> ProviderConfig:
        provider_id = _required_text(provider_id, "provider_id")
        with self._lock:
            for provider in self.list_providers():
                if provider.id == provider_id:
                    return provider
        raise ProviderNotFound("Provider not found")

    def create_provider(
        self, name, protocol, base_url, api_key, auto_append_path=True
    ) -> ProviderConfig:
        """Add a provider.  A blank ``api_key`` creates one without a key ("not set" until one is given)."""
        key = _key_of(api_key)
        provider = _provider_from_data(
            {
                "id": uuid.uuid4().hex,
                "name": _required_text(name, "name"),
                "protocol": protocol,
                "base_url": _required_text(base_url, "base_url"),
                "api_key": key,
                "models": [],
                "read_only": False,
                "source": "user",
                "auto_append_path": auto_append_path,
            }
        )
        with self._lock:
            providers = self._read_registry()
            known_ids = {existing.id for existing in providers} | {provider.id}
            if key:
                self._save_key(provider.id, key, known_ids)  # first: no master key means no provider, not a keyless one
            providers.append(provider)
            try:
                self._write_registry(providers)
            except BaseException:
                if key:
                    self._forget_key(provider.id, known_ids - {provider.id})
                raise
        return provider

    def _find_editable(
        self, providers: list[ProviderConfig], provider_id: str
    ) -> tuple[int, ProviderConfig]:
        for index, provider in enumerate(providers):
            if provider.id == provider_id:
                if provider.read_only:
                    raise ProviderError("Environment providers are read-only")
                return index, provider
        environment_provider = _environment_provider()
        if environment_provider is not None and provider_id == environment_provider.id:
            raise ProviderError("Environment providers are read-only")
        raise ProviderNotFound("Provider not found")

    def update_provider(
        self,
        provider_id,
        *,
        name=None,
        protocol=None,
        base_url=None,
        api_key=None,
        auto_append_path=None,
    ) -> ProviderConfig:
        """Change a provider.  A blank / missing ``api_key`` keeps the stored one; moving a provider that has a key to
        another endpoint (protocol, address or path rule) needs the key to be typed again, so a stored key can never
        be sent somewhere new by an edit that does not mention it."""
        provider_id = _required_text(provider_id, "provider_id")
        with self._lock:
            providers = self._read_registry()
            index, current = self._find_editable(providers, provider_id)
            stored_key = self._read_keys().get(provider_id, "")
            # A key file that exists but cannot be read may still hold this provider's key (it comes back with the right
            # master key): that counts as "has a key", so the endpoint rule below cannot be dodged while it is unreadable.
            holds_key = bool(stored_key) or (self._keys_error is not None and self._secrets.exists())
            new_protocol = current.protocol if protocol is None else protocol
            new_base_url = current.base_url if base_url is None else base_url
            new_auto_append = (
                current.auto_append_path
                if auto_append_path is None
                else auto_append_path
            )
            endpoint_changed = (
                new_protocol != current.protocol
                or new_base_url != current.base_url
                or new_auto_append != current.auto_append_path
            )
            given_key = _key_of(api_key)
            if endpoint_changed and holds_key and not given_key:
                raise ProviderError(
                    "Changing protocol, base_url or auto_append_path requires a non-empty api_key"
                )
            data = current.model_dump(mode="json", exclude={"api_key"})
            data.update(
                {
                    "name": current.name if name is None else name,
                    "protocol": new_protocol,
                    "base_url": new_base_url,
                    "api_key": given_key or stored_key,
                    "auto_append_path": new_auto_append,
                }
            )
            updated = _provider_from_data(data)
            if given_key:
                self._save_key(provider_id, given_key, {provider.id for provider in providers})
            providers[index] = updated
            self._write_registry(providers)
            return updated

    def delete_provider(self, provider_id) -> None:
        provider_id = _required_text(provider_id, "provider_id")
        with self._lock:
            providers = self._read_registry()
            index, _ = self._find_editable(providers, provider_id)
            del providers[index]
            self._write_registry(providers)
            self._forget_key(provider_id, {provider.id for provider in providers})

    def add_model(
        self,
        provider_id,
        model_id,
        input_price_per_m,
        output_price_per_m,
        context_window_tokens=DEFAULT_CONTEXT_WINDOW_TOKENS,
        *,
        cache_read_price_per_m=None,
        cache_write_price_per_m=None,
        price_source="manual",
    ) -> ModelEntry:
        provider_id = _required_text(provider_id, "provider_id")
        model = _model_from_data(
            {
                "id": model_id,
                "input_price_per_m": input_price_per_m,
                "output_price_per_m": output_price_per_m,
                "cache_read_price_per_m": cache_read_price_per_m,
                "cache_write_price_per_m": cache_write_price_per_m,
                "context_window_tokens": context_window_tokens,
                "price_source": price_source,
            }
        )
        with self._lock:
            providers = self._read_registry()
            index, provider = self._find_editable(providers, provider_id)
            if any(existing.id == model.id for existing in provider.models):
                raise ProviderError("Model id already exists; edit it instead")
            provider.models.append(model)
            providers[index] = provider
            self._write_registry(providers)
            return model

    def update_model(
        self,
        provider_id,
        model_id,
        *,
        input_price_per_m=None,
        output_price_per_m=None,
        context_window_tokens=None,
        cache_read_price_per_m=UNSET,
        cache_write_price_per_m=UNSET,
        price_source=None,
    ) -> ModelEntry:
        """Change a model.  ``UNSET`` leaves a cache price alone; ``None`` clears it (= the input price)."""
        provider_id = _required_text(provider_id, "provider_id")
        model_id = _required_text(model_id, "model_id")
        with self._lock:
            providers = self._read_registry()
            provider_index, provider = self._find_editable(providers, provider_id)
            for model_index, current in enumerate(provider.models):
                if current.id == model_id:
                    data = current.model_dump(mode="json")
                    if input_price_per_m is not None:
                        data["input_price_per_m"] = input_price_per_m
                    if output_price_per_m is not None:
                        data["output_price_per_m"] = output_price_per_m
                    if cache_read_price_per_m is not UNSET:
                        data["cache_read_price_per_m"] = cache_read_price_per_m
                    if cache_write_price_per_m is not UNSET:
                        data["cache_write_price_per_m"] = cache_write_price_per_m
                    if price_source is not None:
                        data["price_source"] = price_source
                    if context_window_tokens is not None:
                        data["context_window_tokens"] = context_window_tokens
                    updated = _model_from_data(data)
                    provider.models[model_index] = updated
                    providers[provider_index] = provider
                    self._write_registry(providers)
                    return updated
            raise ModelNotFound("Model not found")

    def remove_model(self, provider_id, model_id) -> None:
        provider_id = _required_text(provider_id, "provider_id")
        model_id = _required_text(model_id, "model_id")
        with self._lock:
            providers = self._read_registry()
            provider_index, provider = self._find_editable(providers, provider_id)
            for model_index, model in enumerate(provider.models):
                if model.id == model_id:
                    del provider.models[model_index]
                    providers[provider_index] = provider
                    self._write_registry(providers)
                    return
            raise ModelNotFound("Model not found")

    def resolve(self, binding: ModelBinding) -> tuple[ProviderConfig, ModelEntry]:
        if not isinstance(binding, ModelBinding):
            binding = ModelBinding.model_validate(binding)
        provider = self.get_provider(binding.provider_id)
        for model in provider.models:
            if model.id == binding.model_id:
                return provider, model
        raise ModelNotFound("Model not found for provider binding")


_store_lock = threading.RLock()
_provider_store: ProviderStore | None = None


def get_provider_store() -> ProviderStore:
    global _provider_store
    with _store_lock:
        data_dir = get_data_dir()
        if _provider_store is None or _provider_store.data_dir != data_dir:
            _provider_store = ProviderStore(data_dir)
        return _provider_store


def reset_provider_store() -> None:
    global _provider_store
    with _store_lock:
        _provider_store = None


__all__ = [
    "UNSET",
    "ENV_PROVIDER_ID",
    "ModelNotFound",
    "ProviderError",
    "ProviderNotFound",
    "ProviderStore",
    "StorageError",
    "get_provider_store",
    "reset_provider_store",
]
