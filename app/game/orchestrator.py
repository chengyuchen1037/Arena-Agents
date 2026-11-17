"""The application facade of the v0.12 engine: lifecycle, persistence, spectator events.

The game itself is a :class:`~app.world.world.World` (rules-agnostic physical layer) driven by a
:class:`~app.runtime.supervisor.Supervisor` that runs one async agent (actor + worker) per player
and one for the moderator.  This class wires those together with the model bindings, the encrypted
save files, the SSE event bus and the HTTP API (``app.api``).
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import random
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.agents.brain import ArenaBrain
from app.config import load_config
from app.engine.llm import LLMClient
from app.game.bindings import BindingError
from app.game.session import (
    APP_ENGINE_VERSION,
    MODERATOR_ID,
    SESSION_SCHEMA_VERSION,
    GameSession,
    Participant,
)
from app.game.session_runtime import OLD_SAVE_NOTICE, SessionRuntimeMixin
from app.runtime.agent import Agent, AgentConfig
from app.runtime.bus import EventBus
from app.runtime.clock import GameClock
from app.runtime.llm_gate import GatedEngineLlm, LlmGate
from app.runtime.supervisor import Supervisor, SupervisorConfig
from app.runtime.trace_log import TraceLog
from app.runtime.usage import UsageBook
from app.world.loader import load_scenario_v2
from app.world.projection import world_snapshot
from app.world.spec import ScenarioError, ScenarioSpec
from app.world.types import Envelope, UiEvent
from app.world.world import World

logger = logging.getLogger(__name__)

__all__ = [
    "BindingError",
    "ConfigReloadError",
    "GameOrchestrator",
    "SessionLoadError",
    "get_orchestrator",
    "orchestrator",
]

#: UI events that mark a state change worth saving (debounced; metrics / status chatter is not).
AUTOSAVE_EVENT_TYPES = frozenset(
    {
        "message.committed",
        "game.status",
        "request.closed",
        "challenge.resolved",
        "phase.changed",
        "player.updated",
        "floor.changed",
        "ability.used",
        "channel.created",
        "channel.updated",
        "dashboard.updated",
    }
)


class SessionLoadError(ValueError):
    """An imported session failed validation; the current game was left untouched."""


class ConfigReloadError(RuntimeError):
    """config.yml or the scenario could not be (re)loaded; the previous config stays active."""


class _CallableSink:
    """Adapts a plain function to the runtime's ``EventSink`` protocol."""

    def __init__(self, func: Callable[[UiEvent], None]) -> None:
        self._func = func

    def publish(self, event: UiEvent) -> None:
        self._func(event)


def load_config_spec(cfg: dict[str, Any]) -> ScenarioSpec:
    scenario_cfg = cfg.get("scenario") or {}
    path = scenario_cfg.get("path") or scenario_cfg.get("document")
    if not path:
        raise ScenarioError("config.scenario.path is not set")
    return load_scenario_v2(str(path))


class GameOrchestrator(SessionRuntimeMixin):
    # Class-level defaults so partially initialized instances (tests) stay valid.
    _supervisor: Supervisor | None = None
    _reload_deferred: bool = False
    llm_port_factory: Callable[["GameOrchestrator"], Any] | None = None

    def __init__(self) -> None:
        self.cfg = load_config()
        self.default_spec = load_config_spec(self.cfg)
        self.spec = self.default_spec
        self.llm = LLMClient(self.cfg)
        self.bus = EventBus()
        self.agent_state: dict[str, str] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._supervisor = None
        self._reload_deferred = False
        self.session: GameSession
        self.world: World
        self.clock: GameClock
        self.usage: UsageBook
        self._reset_game(default_bindings=None)

    # ------------------------------------------------------------------ config ---
    def _is_active(self) -> bool:
        return self._supervisor is not None or self.session.status == "running"

    def reload_config(self) -> bool:
        """Re-read config.yml / the scenario.  Returns False when deferred (a game is running).

        The running (or restored) game keeps the scenario it started with (it is stored in the
        session); the reload only affects the next new game.  The new configuration is built first
        and swapped in only when it loaded completely.
        """
        if self._is_active():
            self._reload_deferred = True
            return False
        try:
            cfg = load_config()
            spec = load_config_spec(cfg)
        except (FileNotFoundError, ScenarioError, yaml.YAMLError, OSError, ValueError) as exc:
            raise ConfigReloadError(f"配置加载失败：{exc}") from exc
        self.cfg = cfg
        self.default_spec = spec
        reconfigure = getattr(self.llm, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(cfg)
        else:
            self.llm = LLMClient(cfg)
        self._reload_deferred = False
        return True

    # ------------------------------------------------------------- game creation ---
    def _players(self) -> list[dict[str, str]]:
        players = []
        for item in self.cfg.get("participants", []):
            players.append(
                {
                    "id": str(item["id"]),
                    "seat": str(item.get("seat", str(item["id"]).upper())),
                    "name": str(item["name"]),
                    "persona": str(item.get("persona", "")),
                }
            )
        return players

    def _new_world(self, spec: ScenarioSpec, seed: int | None = None) -> tuple[World, GameClock]:
        clock = GameClock()
        clock.pause()
        world = World.new_game(
            spec, self._players(), clock, seed=seed if seed is not None else random.SystemRandom().randrange(1, 2**31)
        )
        world.drain_outbox()  # nobody is listening yet; the snapshot is built from state
        world.drain_events()
        return world, clock

    def _reset_game(self, *, default_bindings: dict[str, Any] | None = None) -> None:
        """Replace the live game with a fresh, idle one (the caller guarantees nothing is running)."""
        self.spec = self.default_spec
        world, clock = self._new_world(self.spec)
        players = self._players()
        defaults = self._load_defaults() if default_bindings is None else default_bindings
        allowed = {MODERATOR_ID, *(p["id"] for p in players)}
        session = GameSession(
            title=str((self.cfg.get("app") or {}).get("title", "Arena-Agents")),
            scenario_name=self.spec.id,
            scenario_title=self.spec.title,
            participants=[Participant(**p) for p in players],
            tag_styles=copy.deepcopy(self.cfg.get("tag_styles") or {}),
            scenario_spec=self.spec.model_dump(mode="json"),
            execution_depth=str((self.cfg.get("llm") or {}).get("execution_depth", "medium")),  # type: ignore[arg-type]
            model_bindings={a: b.model_copy(deep=True) for a, b in defaults.items() if a in allowed},
        )
        self.world, self.clock = world, clock
        self.usage = UsageBook()
        self.agent_state = {}
        self._set_session(session)

    async def new_game(self, *, randomize_identities: bool | None = None) -> GameSession:
        """Start a fresh game (identities are always dealt anew)."""
        self._refuse_while_switching()
        if self._autosave_enabled and not self._recovery_pending and self.world.seq > 0:
            await self.flush_autosave("pre-new-game")
        await self._halt()
        self.reload_config()
        self._recovery_pending = False
        self._reset_game(default_bindings=self._load_defaults())
        self.refresh_binding_issues()
        await self.broadcast_state("new_game")
        return self.session

    async def shuffle_identities(self) -> GameSession:
        """Re-deal the identities of a game that has not started."""
        self._ensure_not_switching()  # never swap the world under a workspace switch / without a workspace
        if self.session.status != "idle":
            raise RuntimeError("仅未开始的新对局可以刷新身份；请先新建对局。")
        self.world, self.clock = self._new_world(self.spec)
        await self.broadcast_state("identities_shuffled")
        return self.session

    # ------------------------------------------------------------------ lifecycle ---
    def _build_runtime(self) -> Supervisor:
        rt = self.cfg.get("runtime") or {}
        gate = LlmGate(
            max_concurrent=int(rt.get("llm_concurrency", 6)),
            per_provider=int(rt.get("llm_concurrency_per_provider", 4)),
        )
        if self.llm_port_factory is not None:
            llm_port = self.llm_port_factory(self)
        else:
            llm_port = GatedEngineLlm(
                self.llm,
                self.session.get_model_binding,
                gate,
                float(rt.get("llm_timeout_seconds", 120)),
            )
        personas = {p.id: p.persona for p in self.session.participants}
        brain = ArenaBrain(self.world, self.spec, self.cfg, personas=personas)
        trace = TraceLog(self._logs_root(), self.session.session_id)
        agent_config = AgentConfig(
            debounce_s=float(rt.get("debounce", 1.5)),
            execution_depth=self.session.execution_depth,
        )
        if isinstance(rt.get("max_rounds"), dict):
            agent_config.max_rounds = {
                kind: {str(k): int(v) for k, v in table.items()} for kind, table in rt["max_rounds"].items()
            }
        self._agent_config = agent_config
        sink = _CallableSink(self._publish)
        agents = [
            Agent(
                agent_id,
                "moderator" if agent_id == MODERATOR_ID else "player",
                self.world,
                brain,
                llm_port,
                self.usage,
                trace,
                sink,
                agent_config,
            )
            for agent_id in [MODERATOR_ID, *(p.id for p in self.session.participants)]
        ]
        supervisor_config = SupervisorConfig(
            tick_interval=float(rt.get("tick_interval", 0.1)),
            watchdog_s=float(rt.get("watchdog", 20)),
            max_watchdog_wakes=int(rt.get("max_watchdog_wakes", 3)),
        )
        return Supervisor(
            self.world,
            agents,
            sink,
            self._on_engine,
            supervisor_config,
            on_stall=self._on_stall,
            usage=self.usage,
            trace=trace,
        )

    def _logs_root(self) -> Path:
        from app.storage.paths import get_data_dir

        return get_data_dir() / "logs"

    def set_execution_depth(self, depth: str) -> None:
        """Change the execution depth; a running game picks it up on its agents' next run."""
        self.session.execution_depth = depth  # type: ignore[assignment]
        agent_config = getattr(self, "_agent_config", None)
        if agent_config is not None:
            agent_config.execution_depth = depth

    async def start_or_resume(self) -> GameSession:
        self._ensure_not_switching()  # also refuses while the workspace is unusable
        if self._is_active():
            return self.session
        self.reload_config()
        # Every actor that will call a model must have a usable binding (never a silent fallback).
        self.ensure_bindings_ready()
        self._recovery_pending = False
        session = self.session
        reason = {"idle": "started", "finished": "continued"}.get(self.world.state.status, "resumed")
        session.restored_from_save = False
        session.restore_notice = ""
        session.pause_reason = ""
        session.hold_reason = ""
        self._supervisor = self._build_runtime()
        self.clock.resume()
        self.world.set_running(True, reason)
        session.status = "running"
        wake = Envelope(kind="notice", priority="urgent", payload={"event": f"game.{reason}"})
        await self._supervisor.start({MODERATOR_ID: [wake]})
        await self.broadcast_state("status_change")
        return session

    async def _halt(self, why: str = "halted") -> None:
        """Stop every agent and freeze the game clock / commit gate (no status change)."""
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            await supervisor.stop()
        if self.world.running:
            self.world.set_running(False, why)
        self.clock.pause()
        self._flush_world()
        self.agent_state = {k: "stopped" for k in self.agent_state}

    def _flush_world(self) -> None:
        """Publish spectator events the world queued while nobody was draining it."""
        self.world.drain_outbox()
        for event in self.world.drain_events():
            self._publish(event)

    async def pause(self, why: str = "user", *, hold_reason: str = "") -> GameSession:
        await self._halt(why)
        session = self.session
        if self.world.state.status == "finished":
            session.status = "finished"
        else:
            session.status = "paused"
            session.pause_reason = why if why in ("user", "hold", "error", "stall") else "user"
            session.hold_reason = hold_reason
        await self.broadcast_state("status_change")
        return session

    async def request_pause(self) -> GameSession:
        return await self.pause("user")

    async def force_pause(self, *, silent: bool = False) -> GameSession:
        """Kept for API compatibility: with async agents there is no atomic step to wait for."""
        if silent:
            await self._halt()
            return self.session
        return await self.pause("user")

    # --------------------------------------------------------- runtime callbacks ---
    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)

        def done(finished: asyncio.Task[Any]) -> None:
            self._tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                logger.error("Background task failed", exc_info=finished.exception())

        task.add_done_callback(done)

    def _on_engine(self, envelope: Envelope) -> None:
        event = envelope.payload.get("event")
        if event == "hold_game":
            self._spawn(self.pause("hold", hold_reason=str(envelope.payload.get("reason", ""))))
        elif event == "end_game":
            self._spawn(self.pause("finish"))

    def _on_stall(self, reason: str) -> None:
        self._spawn(self.pause("stall", hold_reason=reason or "对局长时间没有进展，已自动暂停。"))

    def _publish(self, event: UiEvent) -> None:
        if event.type == "agent.status":
            self.agent_state[str(event.data.get("id"))] = str(event.data.get("state", "idle"))
        if event.type in AUTOSAVE_EVENT_TYPES:
            self.schedule_autosave(event.type)
        self.bus.publish(event)

    async def broadcast_state(self, event_type: str = "state_update") -> None:
        """Tell spectators that session-level state changed (the world publishes its own events)."""
        if event_type in {"new_game", "session_loaded", "identities_shuffled", "workspace_changed"}:
            if event_type == "workspace_changed":
                self.bus.publish(UiEvent(type="workspace.changed", data={}))
            self.bus.publish(UiEvent(type="snapshot", data={"session": self.public_session()}))
            if event_type != "identities_shuffled":
                self.schedule_autosave(event_type)
            return
        s = self.session
        self.bus.publish(
            UiEvent(
                type="session.updated",
                data={
                    "status": s.status,
                    "pause_reason": s.pause_reason,
                    "hold_reason": s.hold_reason,
                    "winner": self.world.state.winner,
                    "summary": self.world.state.summary,
                    "active_status_text": self._status_text(),
                    "model_bindings": {k: v.model_dump(mode="json") for k, v in s.model_bindings.items()},
                    "binding_issues": [i.model_dump() for i in s.binding_issues],
                    "restored_from_save": s.restored_from_save,
                    "restore_notice": s.restore_notice,
                    "autosave_error": s.autosave_error,
                    "execution_depth": s.execution_depth,
                    "title": s.title,
                },
            )
        )
        if event_type in {"status_change", "error", "bindings_updated", "execution_depth_updated"}:
            self.schedule_autosave(event_type)

    # ------------------------------------------------------------------ snapshots ---
    def _status_text(self) -> str:
        s = self.session
        if s.status == "idle":
            return "未开始 · 点击「开始」由主持人开局"
        if s.status == "running":
            return "对局进行中"
        if s.status == "finished":
            winner = self.world.state.winner
            return f"对局已结束{(' · ' + winner + ' 获胜') if winner else ''}（可点击「继续」恢复）"
        why = {"hold": "主持人暂停了对局", "stall": "对局长时间没有进展，已自动暂停", "error": "运行出错，已暂停"}.get(
            s.pause_reason, "已暂停"
        )
        return f"{why}{('：' + s.hold_reason) if s.hold_reason else ''}"

    def public_session(self) -> dict[str, Any]:
        """The PublicSession v2 document (docs/v0.12-contracts.md §8.1): everything a spectator sees,
        never thoughts or tool calls."""
        s = self.session
        snap = world_snapshot(self.world)
        participants = [
            {**p, "state": self.agent_state.get(str(p.get("id")), "idle")} for p in snap.get("players", [])
        ]
        return {
            "session_id": s.session_id,
            "schema_version": SESSION_SCHEMA_VERSION,
            "version": APP_ENGINE_VERSION,
            "title": s.title,
            "scenario_name": s.scenario_name,
            "scenario_title": s.scenario_title,
            "status": s.status,
            "pause_reason": s.pause_reason,
            "hold_reason": s.hold_reason,
            "winner": snap.get("winner"),
            "summary": snap.get("summary", ""),
            "phase_text": snap.get("phase") or "未开始 · 等待主持人开局",
            "active_status_text": self._status_text(),
            "seq": snap.get("seq", 0),
            "event_id": self.bus.last_id,
            "clock": snap.get("now", 0.0),
            "participants": participants,
            "moderator": {"id": MODERATOR_ID, "name": "主持人", "state": self.agent_state.get(MODERATOR_ID, "idle")},
            "channels": snap.get("channels", []),
            "messages": snap.get("messages", []),
            "requests": snap.get("requests", []),
            "tickets": snap.get("tickets", []),
            "dashboard": snap.get("dashboard", {"factions": [], "tag_stats": []}),
            "public_tags": snap.get("public_tags", []),
            "tag_styles": s.tag_styles,
            "ui": snap.get("ui", {}),
            "usage_stats": self.usage.model_dump(mode="json"),
            "model_bindings": {k: v.model_dump(mode="json") for k, v in s.model_bindings.items()},
            "binding_issues": [i.model_dump() for i in s.binding_issues],
            "restored_from_save": s.restored_from_save,
            "restore_notice": s.restore_notice,
            "autosave_error": s.autosave_error,
            "execution_depth": s.execution_depth,
        }

    # ---------------------------------------------------------------- persistence ---
    def session_payload(self) -> dict[str, Any]:
        """A consistent, JSON-serializable snapshot of the whole game (no ``await`` inside)."""
        s = self.session
        s.world = self.world.dump()
        s.usage_stats = self.usage.model_dump(mode="json")
        s.scenario_spec = self.spec.model_dump(mode="json")
        return s.model_dump(mode="json")

    @staticmethod
    def _validate_session_data(data: Any) -> GameSession:
        """Validate an imported payload completely, without touching any live state."""
        if not isinstance(data, dict):
            raise SessionLoadError("存档必须是 JSON 对象")
        if data.get("schema_version") != SESSION_SCHEMA_VERSION:
            raise SessionLoadError(OLD_SAVE_NOTICE)
        try:
            loaded = GameSession.model_validate(data)
            spec = ScenarioSpec.model_validate(loaded.scenario_spec)
            clock = GameClock(start=float((loaded.world or {}).get("clock_now", 0.0)))
            World.load(copy.deepcopy(loaded.world), spec, clock)  # throwaway: proves the world loads
            UsageBook.model_validate(loaded.usage_stats or {})
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in exc.errors(include_input=False, include_url=False)[:5]
            )
            raise SessionLoadError(f"存档校验失败：{problems}") from exc
        except (TypeError, KeyError, AttributeError, ValueError) as exc:
            raise SessionLoadError(f"存档结构无效：{type(exc).__name__}: {exc}") from exc
        return loaded

    async def _adopt_loaded_session(self, loaded: GameSession, notice_prefix: str) -> None:
        """Install a validated session as the live game: paused, bound, flagged as restored."""
        spec = ScenarioSpec.model_validate(loaded.scenario_spec)
        clock = GameClock(start=float((loaded.world or {}).get("clock_now", 0.0)))
        clock.pause()
        world = World.load(copy.deepcopy(loaded.world), spec, clock)
        if world.state.status == "running":
            world.state.status = "paused"
        usage = UsageBook.model_validate(loaded.usage_stats or {})
        loaded.status = {"idle": "idle", "finished": "finished"}.get(world.state.status, "paused")  # type: ignore[assignment]
        suffix = {"finished": "（已结束）", "idle": ""}.get(loaded.status, "（已暂停）")
        loaded.restored_from_save = True
        loaded.restore_notice = f"{notice_prefix}{suffix}"
        from app.game.bindings import fill_missing_from_defaults

        fill_missing_from_defaults(loaded, self._load_defaults())
        self.spec, self.clock, self.world, self.usage = spec, clock, world, usage
        self.agent_state = {}
        self._set_session(loaded)
        self.refresh_binding_issues()

    async def load_session_data(self, data: dict[str, Any]) -> GameSession:
        """Replace the current game with an imported one.

        Validate first; on failure (:class:`SessionLoadError`, a ``ValueError``) the running game is
        not paused, reset or otherwise modified.
        """
        self._refuse_while_switching()
        loaded = self._validate_session_data(data)
        await self._halt()
        try:
            self.reload_config()
        except ConfigReloadError:
            logger.warning("Config reload failed during import; keeping the current config", exc_info=True)
        self._recovery_pending = False
        await self._adopt_loaded_session(loaded, "已导入存档")
        await self.broadcast_state("session_loaded")
        return self.session

    def _set_session_hook(self) -> None:  # pragma: no cover - documentation only
        """``_set_session`` (mixin) bumps ``_session_generation`` so a slow save never moves the
        last-session pointer to a replaced game."""

    def _set_session(self, session: GameSession) -> None:
        self._session_generation += 1
        self.session = session


# ---------------------------------------------------------------------------
# Process-wide singleton, created lazily so a broken config.yml / scenario does not
# make merely *importing* the application fail.  ``orchestrator`` behaves like the
# instance itself; call ``get_orchestrator()`` at startup to surface config errors early.
# ---------------------------------------------------------------------------

_instance: GameOrchestrator | None = None


def get_orchestrator() -> GameOrchestrator:
    global _instance
    if _instance is None:
        _instance = GameOrchestrator()
        if os.environ.get("ARENA_DEMO", "").strip() not in ("", "0", "false", "no"):
            from app.game.demo import install_demo

            install_demo(_instance)
    return _instance


class _LazyOrchestrator:
    def __getattr__(self, name: str) -> Any:
        return getattr(get_orchestrator(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(get_orchestrator(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(get_orchestrator(), name)

    def __repr__(self) -> str:
        return f"<lazy orchestrator {'ready' if _instance else 'pending'}>"


orchestrator: GameOrchestrator = _LazyOrchestrator()  # type: ignore[assignment]
