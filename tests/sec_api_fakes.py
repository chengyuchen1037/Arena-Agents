"""In-memory fakes for API security tests (no disk, no network, no real keys)."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

from app.api import providers as provider_api
from app.api import routes
from app.api import workspaces as workspace_api
from app.engine.state import GameSession, Participant
from app.providers.models import (
    ModelBinding,
    ModelEntry,
    ModelNotFound,
    ProviderConfig,
    ProviderError,
    ProviderNotFound,
)
from app.storage.errors import StorageError

SECRET_KEY = "sk-super-secret-value-123456"


class BoundSession(GameSession):
    """GameSession plus the v0.10 binding map (engine-core adds the real field)."""

    model_bindings: dict[str, ModelBinding] = {}


class FakeProviderStore:
    def __init__(self) -> None:
        self.providers: dict[str, ProviderConfig] = {}
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, _call: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((_call, args, kwargs))

    def seed(
        self,
        *,
        name: str = "Seed",
        protocol: str = "openai_chat",
        base_url: str = "https://api.example.com/v1",
        api_key: str = SECRET_KEY,
        models: tuple[str, ...] = ("m1",),
        read_only: bool = False,
        provider_id: str | None = None,
        auto_append_path: bool = True,
    ) -> ProviderConfig:
        provider = ProviderConfig(
            id=provider_id or uuid.uuid4().hex,
            name=name,
            protocol=protocol,
            base_url=base_url,
            api_key=api_key,
            models=[
                ModelEntry(id=m, input_price_per_m=1.0, output_price_per_m=2.0) for m in models
            ],
            read_only=read_only,
            source="env" if read_only else "encrypted",
            auto_append_path=auto_append_path,
        )
        self.providers[provider.id] = provider
        return provider

    def list_providers(self) -> list[ProviderConfig]:
        return list(self.providers.values())

    def get_provider(self, provider_id: str) -> ProviderConfig:
        try:
            return self.providers[provider_id]
        except KeyError:
            raise ProviderNotFound("Provider 不存在") from None

    def create_provider(self, name, protocol, base_url, api_key, auto_append_path=True) -> ProviderConfig:
        self._record("create_provider", name, protocol, base_url, auto_append_path=auto_append_path)
        return self.seed(
            name=name,
            protocol=protocol,
            base_url=base_url,
            api_key=api_key,
            models=(),
            auto_append_path=auto_append_path,
        )

    def update_provider(self, provider_id, **kwargs) -> ProviderConfig:
        self._record("update_provider", provider_id, **{k: v for k, v in kwargs.items() if k != "api_key"})
        provider = self.get_provider(provider_id)
        if provider.read_only:
            raise ProviderError("read only")
        endpoint_changed = any(
            key in kwargs for key in ("base_url", "protocol", "auto_append_path")
        )
        if endpoint_changed and not kwargs.get("api_key"):
            raise ProviderError("api_key required")
        updated = provider.model_copy(update=kwargs)
        self.providers[provider_id] = updated
        return updated

    def delete_provider(self, provider_id: str) -> None:
        self._record("delete_provider", provider_id)
        self.get_provider(provider_id)
        del self.providers[provider_id]

    def add_model(self, provider_id, model_id, input_price_per_m, output_price_per_m, **kwargs) -> ModelEntry:
        self._record("add_model", provider_id, model_id, input_price_per_m, output_price_per_m, **kwargs)
        provider = self.get_provider(provider_id)
        entry = ModelEntry(
            id=model_id,
            input_price_per_m=input_price_per_m,
            output_price_per_m=output_price_per_m,
            **kwargs,
        )
        provider.models.append(entry)
        return entry

    def update_model(self, provider_id, model_id, **changes) -> ModelEntry:
        self._record("update_model", provider_id, model_id, **changes)
        provider = self.get_provider(provider_id)
        for index, model in enumerate(provider.models):
            if model.id == model_id:
                provider.models[index] = model.model_copy(update=changes)
                return provider.models[index]
        raise ModelNotFound("模型不存在")

    def remove_model(self, provider_id: str, model_id: str) -> None:
        self._record("remove_model", provider_id, model_id)
        provider = self.get_provider(provider_id)
        provider.models[:] = [m for m in provider.models if m.id != model_id]

    def resolve(self, binding: ModelBinding):
        provider = self.get_provider(binding.provider_id)
        for model in provider.models:
            if model.id == binding.model_id:
                return provider, model
        raise ModelNotFound("模型不存在")


class FakeSessionStore:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.fail_with: Exception | None = None

    def list_sessions(self) -> list[dict[str, Any]]:
        if self.fail_with:
            raise self.fail_with
        return [
            {
                "session_id": sid,
                "title": "t",
                "status": "paused",
                "path": f"C:/secret/dir/{sid}/current.json.enc",
                "data_dir": "C:/secret/dir",
            }
            for sid in self.sessions
        ]

    def exists(self, session_id: str) -> bool:
        return session_id in self.sessions

    def load(self, session_id: str) -> dict[str, Any]:
        if self.fail_with:
            raise self.fail_with
        return self.sessions[session_id]

    def delete(self, session_id: str) -> None:
        if self.fail_with:
            raise self.fail_with
        self.deleted.append(session_id)
        del self.sessions[session_id]


class FakeSettingsStore:
    """The settings file's model templates, in memory (v0.11)."""

    def __init__(self) -> None:
        self.templates: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []
        self.fail_with: Exception | None = None

    def get_model_templates(self) -> list[dict[str, Any]]:
        if self.fail_with:
            raise self.fail_with
        return sorted((dict(item) for item in self.templates), key=lambda item: item.get("slot") or 0)

    def save_model_templates(self, templates) -> None:
        if self.fail_with:
            raise self.fail_with
        cleaned: list[dict[str, Any]] = []
        for item in templates:
            data = item if isinstance(item, dict) else item.model_dump(mode="json")
            cleaned.append(
                {
                    "slot": data.get("slot"),
                    "name": data.get("name"),
                    "provider_id": data.get("provider_id"),
                    "model_id": data.get("model_id"),
                    "reasoning_depth": data.get("reasoning_depth", "high"),
                }
            )
        slots = [item["slot"] for item in cleaned]
        if len(slots) != len(set(slots)):
            raise ValueError("Model template slots must be unique")
        self.templates = sorted(cleaned, key=lambda item: item["slot"])
        self.calls.append(("save_model_templates", [dict(item) for item in self.templates]))

    def seed_template(
        self,
        *,
        slot: int = 1,
        name: str = "模板1",
        provider_id: str,
        model_id: str = "m1",
        reasoning_depth: str = "high",
    ) -> dict[str, Any]:
        template = {
            "slot": slot,
            "name": name,
            "provider_id": provider_id,
            "model_id": model_id,
            "reasoning_depth": reasoning_depth,
        }
        self.templates = [t for t in self.templates if t["slot"] != slot]
        self.templates.append(template)
        self.templates.sort(key=lambda item: item["slot"])
        return template


class FakeWorkspaceModule:
    """Just enough of ``app.storage.workspace`` (contract signatures) for API tests."""

    WorkspaceError = type("WorkspaceError", (ValueError,), {})

    root = Path("C:/fake-arena-workspaces")

    def __init__(self, *, enabled: bool = False) -> None:
        self._enabled = enabled
        self.name = "Test Workspace"
        self.id = "ws-test"
        self.path = str(self.root / "Test")
        self.last_session = "sec-test"
        self.status = "ok"
        self.default_path = str(self.root / "Default")
        self.recent: list[dict[str, Any]] = [
            {
                "name": "Test Workspace",
                "id": "ws-test",
                "last_opened": "2026-01-01T00:00:00",
                "path": self.path,
            },
            {
                "name": "Other Workspace",
                "id": "ws-other",
                "last_opened": "2025-12-01T00:00:00",
                "path": str(self.root / "Other"),
            },
        ]
        self.picker_result: str | None = str(self.root / "Picked")
        self.picker_error: Exception | None = None
        self.enabled_error: Exception | None = None
        self.payload_error: Exception | None = None
        self.recent_error: Exception | None = None

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def workspace_enabled(self) -> bool:
        if self.enabled_error:
            raise self.enabled_error
        return self._enabled

    def active_payload(self, *, include_path: bool) -> dict[str, Any]:
        if self.payload_error:
            raise self.payload_error
        payload: dict[str, Any] = {
            "enabled": True,
            "name": self.name,
            "id": self.id,
            "last_session": self.last_session,
            "status": self.status,
        }
        if include_path:
            payload["path"] = self.path
        return payload

    def get_active_workspace_payload(self, *, include_path: bool) -> dict[str, Any]:
        return self.active_payload(include_path=include_path)

    def list_recent_workspaces(self) -> list[dict[str, Any]]:
        if self.recent_error:
            raise self.recent_error
        return [dict(item) for item in self.recent]

    def get_default_workspace_path(self) -> Path:
        return Path(self.default_path)

    def pick_native_folder(self) -> Path:
        if self.picker_error:
            raise self.picker_error
        assert self.picker_result is not None
        return Path(self.picker_result)


class FakeEnv(SimpleNamespace):
    """Bundle of the fakes installed for one test."""


def install(monkeypatch, *, real_stores: bool = False) -> FakeEnv:
    session = BoundSession(
        session_id="sec-test",
        participants=[
            Participant(id="p01", seat="1", name="Alex"),
            Participant(id="p02", seat="2", name="Blair"),
        ],
    )
    llm = SimpleNamespace(
        api_key="",
        base_url="",
        default_model="",
        reasoning_effort="high",
        is_configured=lambda: True,
    )
    defaults: dict[str, ModelBinding] = {}
    providers = FakeProviderStore()
    sessions = FakeSessionStore()
    settings = FakeSettingsStore()
    workspace = FakeWorkspaceModule()
    discover = AsyncMock(return_value=["m1", "m2"])

    orch = SimpleNamespace(
        llm=llm,
        session=session,
        restored_from_save=False,
        broadcast_state=AsyncMock(return_value=None),
        request_pause=AsyncMock(return_value=session),
        force_pause=AsyncMock(return_value=session),
        new_game=AsyncMock(return_value=session),
        start_or_resume=AsyncMock(return_value=session),
        load_session_data=AsyncMock(return_value=session),
        persist_session=AsyncMock(return_value=None),
        shutdown=AsyncMock(return_value=None),
        startup=AsyncMock(return_value={"recovery_error": None}),
        reload_config=Mock(),
    )

    def get_model_binding(actor_id: str):
        return orch.session.model_bindings.get(actor_id) or defaults.get(actor_id)

    def update_model_bindings(mapping):
        if orch.session.status == "running":
            raise RuntimeError("对局进行中，不能修改绑定")
        orch.session.model_bindings = {**orch.session.model_bindings, **mapping}

    def workspace_payload(*, include_path: bool = True):
        if not workspace.workspace_enabled():
            return {"enabled": False}
        return workspace.active_payload(include_path=include_path)

    async def switch_workspace(path, *, initialize_existing=False, create_if_missing=False):
        workspace.path = str(path)
        return {
            "workspace": workspace.active_payload(include_path=True),
            "session": session,
            "restored": None,
            "recovery_error": None,
            "changed": True,
        }

    orch.get_model_binding = get_model_binding
    orch.update_model_bindings = Mock(side_effect=update_model_bindings)
    orch.workspace_payload = Mock(side_effect=workspace_payload)
    orch.switch_workspace = AsyncMock(side_effect=switch_workspace)

    for module in (routes, provider_api, workspace_api):
        monkeypatch.setattr(module, "orchestrator", orch)
    monkeypatch.setattr(provider_api, "discover_models", discover)
    monkeypatch.setattr(workspace_api, "workspace_module", workspace)
    if not real_stores:
        for module in (routes, provider_api):
            monkeypatch.setattr(module, "get_session_store", lambda: sessions, raising=False)
        monkeypatch.setattr(provider_api, "get_provider_store", lambda: providers)
        monkeypatch.setattr(routes, "get_provider_store", lambda: providers)
        monkeypatch.setattr(provider_api, "get_default_bindings", lambda: dict(defaults))
        monkeypatch.setattr(provider_api, "_settings_store", lambda: settings)

    return FakeEnv(
        orch=orch,
        session=session,
        providers=providers,
        sessions=sessions,
        settings=settings,
        workspace=workspace,
        defaults=defaults,
        discover=discover,
    )


def bind(provider_id: str, model_id: str = "m1", depth: str = "high") -> ModelBinding:
    return ModelBinding(provider_id=provider_id, model_id=model_id, reasoning_depth=depth)


__all__ = [
    "SECRET_KEY",
    "BoundSession",
    "FakeEnv",
    "FakeProviderStore",
    "FakeSessionStore",
    "FakeSettingsStore",
    "FakeWorkspaceModule",
    "StorageError",
    "bind",
    "install",
]
