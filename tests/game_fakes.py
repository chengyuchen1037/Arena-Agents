"""Shared fakes for the v0.12 orchestrator tests: in-memory stores + a real orchestrator wired to them.

The orchestrator under test is the *real* :class:`GameOrchestrator` (real world, real runtime); only
the storage (providers / settings / games) is replaced, and the scenario is the small
``minimal_test`` one so a game finishes in seconds with scripted agents (``tests/v12/bots.py``).
"""

from __future__ import annotations

import asyncio
import copy
import functools
from collections.abc import Callable
from typing import Any

import pytest

import app.game.orchestrator as orchestrator_module
from app.game.orchestrator import GameOrchestrator
from app.providers.models import (
    ModelBinding,
    ModelEntry,
    ModelNotFound,
    ProviderConfig,
    ProviderNotFound,
    StorageError,
)
from app.storage.games import GameNotFoundError, GameStore
from app.world.types import MODERATOR_ID
from v12.bots import BotLlm, Call, simple_player

PLAYERS = [{"id": f"p0{i}", "seat": f"S{i}", "name": f"N{i}", "persona": ""} for i in range(1, 5)]
CFG: dict[str, Any] = {
    "app": {"title": "测试"},
    "scenario": {"path": "scenarios/minimal_test.md"},
    "participants": PLAYERS,
    "tag_styles": {},
    "llm": {},
    "knowledge_base": {"docs_dir": "knowledge"},
    "runtime": {"tick_interval": 0.05, "watchdog": 30, "llm_timeout_seconds": 10},
}
PROVIDER = ProviderConfig(
    id="prov", name="prov", protocol="openai_chat", base_url="https://example.invalid/v1", api_key="k",
    models=[
        ModelEntry(id="small", input_price_per_m=1.0, output_price_per_m=2.0, context_window_tokens=8000),
        ModelEntry(id="big", input_price_per_m=3.0, output_price_per_m=6.0),
    ],
)
BINDING = ModelBinding(provider_id="prov", model_id="big", reasoning_depth="high")


def async_test(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class FakeGames:
    """An in-memory ``GameStore`` (``orch.game_store``): what is saved is in ``data``, every save in ``saves``."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}
        self.saves: list[str] = []
        self.fail_save: str | None = None
        self.on_save: Callable[[str, dict[str, Any]], None] | None = None

    def save(self, session_id: str, payload: dict[str, Any]) -> None:
        if self.on_save:
            self.on_save(session_id, payload)
        if self.fail_save:
            raise StorageError(self.fail_save)
        self.saves.append(session_id)
        self.data[session_id] = copy.deepcopy(payload)

    def load(self, session_id: str) -> dict[str, Any]:
        if session_id not in self.data:
            raise GameNotFoundError("对局不存在。")
        return copy.deepcopy(self.data[session_id])

    def exists(self, session_id: str) -> bool:
        return session_id in self.data

    def delete(self, session_id: str) -> None:
        self.data.pop(session_id, None)

    def list_games(self) -> list[dict[str, Any]]:
        return [GameStore.summarize(payload) | {"session_id": sid} for sid, payload in self.data.items()]


FakeSessions = FakeGames  # the v0.12 name


class FakeSettings:
    def __init__(self, defaults: dict[str, ModelBinding] | None = None) -> None:
        self.defaults = dict(defaults or {})
        self.last_id: str | None = None
        self.fail_save_defaults = False
        self.budget_defaults: dict[str, Any] = {}

    def get_default_bindings(self) -> dict[str, ModelBinding]:
        return dict(self.defaults)

    def save_default_bindings(self, bindings: dict[str, ModelBinding]) -> None:
        if self.fail_save_defaults:
            raise RuntimeError("disk full")
        self.defaults = dict(bindings)

    def get_budget_defaults(self) -> dict[str, Any]:
        return dict(self.budget_defaults)

    def save_budget_defaults(self, budget: dict[str, Any]) -> None:
        self.budget_defaults = dict(budget)

    def get_last_session_id(self) -> str | None:
        return self.last_id

    def set_last_session_id(self, session_id: str) -> None:
        self.last_id = session_id


class FakeProviders:
    """Just the part of ProviderStore the engine uses."""

    def __init__(self, *providers: ProviderConfig) -> None:
        self.providers = {p.id: p for p in providers}

    def resolve(self, binding: ModelBinding) -> tuple[ProviderConfig, ModelEntry]:
        provider = self.providers.get(binding.provider_id)
        if provider is None:
            raise ProviderNotFound(binding.provider_id)
        for entry in provider.models:
            if entry.id == binding.model_id:
                return provider, entry
        raise ModelNotFound(binding.model_id)


def real_orch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    defaults: dict[str, ModelBinding] | None = None,
    providers: tuple[ProviderConfig, ...] = (PROVIDER,),
) -> GameOrchestrator:
    monkeypatch.setattr(orchestrator_module, "load_config", lambda *a, **k: copy.deepcopy(CFG))
    orch = GameOrchestrator()
    orch.provider_store = FakeProviders(*providers)
    orch.settings_store = FakeSettings(defaults)
    orch.game_store = FakeGames()
    orch._autosave_delay = 0.01
    # The constructor ran against the real (isolated) stores; rebuild the fresh game the way startup()
    # does, so its default bindings come from the stores injected above.
    orch._reset_game(default_bindings=orch._load_defaults())
    orch.refresh_binding_issues()
    return orch


def bots_for(orch: GameOrchestrator, moderator_policy: Callable[[Any, str], list[Call]]) -> None:
    """Make ``orch`` play with scripted agents (no network) and skip the provider binding check."""
    orch.ensure_bindings_ready = lambda: None  # type: ignore[method-assign]
    orch.llm_port_factory = lambda o: BotLlm(
        o.world, {MODERATOR_ID: moderator_policy, **{p["id"]: simple_player for p in PLAYERS}}
    )


def idle_moderator(world: Any, me: str) -> list[Call]:
    return []  # always ``wait``: the game stays running until somebody stops it


def bound(model: str = "big", pid: str = "prov", depth: str = "high", window: int = 1_000_000) -> ModelBinding:
    return ModelBinding(provider_id=pid, model_id=model, reasoning_depth=depth, context_window_tokens=window)


def all_bound() -> dict[str, ModelBinding]:
    return {actor: BINDING for actor in [MODERATOR_ID, *(p["id"] for p in PLAYERS)]}
