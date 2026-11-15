from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

from pydantic import ValidationError

from app.providers.models import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    ModelBinding,
    ModelEntry,
    ModelNotFound,
    ProtocolType,
    ProviderConfig,
    ProviderError,
    ProviderNotFound,
)
from app.storage._atomic import atomic_write
from app.storage.crypto import decrypt_json, encrypt_json
from app.storage.errors import StorageError
from app.storage.paths import get_data_dir

_PROVIDER_PURPOSE = "providers.v1"
_PROVIDER_FILE = "providers.v1.enc"
_PROVIDER_BACKUP_FILE = "providers.v1.backup.enc"
_SCHEMA_VERSION = 1
ENV_PROVIDER_ID = "env-deepseek"


def _required_text(value: str | None, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(f"{label} must not be empty")
    return value


def _provider_from_data(data: dict) -> ProviderConfig:
    try:
        provider = ProviderConfig.model_validate(data)
    except (ValidationError, TypeError) as exc:
        raise ProviderError("Invalid provider configuration") from exc
    if not provider.api_key.strip():
        raise ProviderError("api_key must not be empty")
    return provider


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
    """Thread-safe encrypted provider registry with a known-good backup."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = (data_dir or get_data_dir()).resolve()
        self._path = self._data_dir / _PROVIDER_FILE
        self._backup_path = self._data_dir / _PROVIDER_BACKUP_FILE
        self._lock = threading.RLock()

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def _load_registry_file(self, path: Path) -> list[ProviderConfig]:
        if path.is_symlink():
            raise StorageError("Refusing to read a symbolic-link provider registry")
        try:
            encrypted = path.read_bytes()
        except OSError as exc:
            raise StorageError("Unable to read the encrypted provider registry") from exc
        payload = decrypt_json(encrypted, _PROVIDER_PURPOSE, data_dir=self._data_dir)
        try:
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != _SCHEMA_VERSION
                or not isinstance(payload.get("providers"), list)
            ):
                raise StorageError("Invalid encrypted provider registry schema")
            providers = [ProviderConfig.model_validate(item) for item in payload["providers"]]
        except (ValidationError, TypeError) as exc:
            raise StorageError("Invalid encrypted provider registry data") from exc
        provider_ids = [provider.id for provider in providers]
        if len(provider_ids) != len(set(provider_ids)):
            raise StorageError("Duplicate provider id in encrypted registry")
        for provider in providers:
            model_ids = [model.id for model in provider.models]
            if len(model_ids) != len(set(model_ids)):
                raise StorageError("Duplicate model id in encrypted registry")
        return providers

    def _read_registry(self) -> list[ProviderConfig]:
        current_exists = self._path.exists()
        backup_exists = self._backup_path.exists()
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
        if current_error is not None or current_exists or backup_exists:
            raise StorageError("Provider registry and its backup could not be decrypted")
        return []

    def _write_registry(self, providers: list[ProviderConfig]) -> None:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "providers": [provider.model_dump(mode="json") for provider in providers],
        }
        encrypted = encrypt_json(payload, _PROVIDER_PURPOSE, data_dir=self._data_dir)

        if self._path.exists():
            try:
                self._load_registry_file(self._path)
                previous = self._path.read_bytes()
            except (OSError, StorageError):
                previous = None
            if previous is not None:
                atomic_write(self._backup_path, previous)

        atomic_write(self._path, encrypted)
        try:
            verified = decrypt_json(
                self._path.read_bytes(), _PROVIDER_PURPOSE, data_dir=self._data_dir
            )
        except OSError as exc:
            raise StorageError("Unable to verify the encrypted provider registry") from exc
        if verified != payload:
            raise StorageError("Encrypted provider registry verification failed")

    def list_providers(self) -> list[ProviderConfig]:
        with self._lock:
            providers = self._read_registry()
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
        provider = _provider_from_data(
            {
                "id": uuid.uuid4().hex,
                "name": _required_text(name, "name"),
                "protocol": protocol,
                "base_url": _required_text(base_url, "base_url"),
                "api_key": api_key,
                "models": [],
                "read_only": False,
                "source": "encrypted",
                "auto_append_path": auto_append_path,
            }
        )
        with self._lock:
            providers = self._read_registry()
            providers.append(provider)
            self._write_registry(providers)
        return provider

    def import_legacy_provider(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        model_id: str,
        input_price_per_m: float,
        output_price_per_m: float,
        context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS,
    ) -> ProviderConfig:
        """Atomically import the v0.9 DeepSeek configuration for migration."""
        model = _model_from_data(
            {
                "id": model_id,
                "input_price_per_m": input_price_per_m,
                "output_price_per_m": output_price_per_m,
                "context_window_tokens": context_window_tokens,
            }
        )
        with self._lock:
            providers = self._read_registry()
            for provider in providers:
                if (
                    provider.source == "legacy-migration"
                    and provider.protocol == "openai_chat"
                    and provider.base_url == base_url
                    and provider.api_key == api_key
                    and any(model.id == model_id for model in provider.models)
                ):
                    return provider
            provider = _provider_from_data(
                {
                    "id": uuid.uuid4().hex,
                    "name": _required_text(name, "name"),
                    "protocol": "openai_chat",
                    "base_url": _required_text(base_url, "base_url"),
                    "api_key": api_key,
                    "models": [model.model_dump(mode="json")],
                    "read_only": False,
                    "source": "legacy-migration",
                }
            )
            providers.append(provider)
            self._write_registry(providers)
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
        provider_id = _required_text(provider_id, "provider_id")
        with self._lock:
            providers = self._read_registry()
            index, current = self._find_editable(providers, provider_id)
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
            if endpoint_changed and (not isinstance(api_key, str) or not api_key.strip()):
                raise ProviderError(
                    "Changing protocol, base_url or auto_append_path requires a non-empty api_key"
                )
            new_key = current.api_key
            if isinstance(api_key, str) and api_key.strip():
                new_key = api_key
            data = current.model_dump(mode="json")
            data.update(
                {
                    "name": current.name if name is None else name,
                    "protocol": new_protocol,
                    "base_url": new_base_url,
                    "api_key": new_key,
                    "auto_append_path": new_auto_append,
                }
            )
            updated = _provider_from_data(data)
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

    def add_model(
        self,
        provider_id,
        model_id,
        input_price_per_m,
        output_price_per_m,
        context_window_tokens=DEFAULT_CONTEXT_WINDOW_TOKENS,
    ) -> ModelEntry:
        provider_id = _required_text(provider_id, "provider_id")
        model = _model_from_data(
            {
                "id": model_id,
                "input_price_per_m": input_price_per_m,
                "output_price_per_m": output_price_per_m,
                "context_window_tokens": context_window_tokens,
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
    ) -> ModelEntry:
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
    "ENV_PROVIDER_ID",
    "ModelNotFound",
    "ProviderError",
    "ProviderNotFound",
    "ProviderStore",
    "StorageError",
    "get_provider_store",
    "reset_provider_store",
]
