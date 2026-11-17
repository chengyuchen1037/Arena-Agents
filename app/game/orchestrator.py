"""The application facade of the v0.12 engine: lifecycle, persistence, spectator events.

The game itself is a :class:`~app.world.world.World` (rules-agnostic physical layer) driven by a
:class:`~app.runtime.supervisor.Supervisor` that runs one async agent (actor + worker) per player
and one for the moderator.  This class wires those together with the model bindings, the encrypted
save files, the SSE event bus and the HTTP API (``app.api``).

v0.12.1: every message injected into an agent wakes it; token use is managed by the actors (one
persistent conversation per agent, kept across pause / resume in ``self._book`` and reset for a new
game, an import or a restore), never by calling the model less.  The user's spending limit and the
roster (names, avatars, persona, memory) live in :mod:`app.game.roster_runtime`.
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
from app.game.roster_runtime import RosterRuntimeMixin
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
from app.runtime.conversation import ConversationBook
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


class GameOrchestrator(RosterRuntimeMixin, SessionRuntimeMixin):
    # Class-level defaults so partially initialized instances (tests) stay valid.
    _supervisor: Supervisor | None = None
    _reload_deferred: bool = False
    llm_port_factory: Callable[["GameOrchestrator"], Any] | None = None
    demo_mode: bool = False
    _brain: ArenaBrain | None = None
    _stall_notice: str = ""
    _roster_io_suspended: bool = False

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
        self._book = ConversationBook()
        self._brain = None
        self._stall_notice = ""
        self.session: GameSession
        self.world: World
        self.clock: GameClock
        self.usage: UsageBook
        # The first game is built before the workspace is activated (``startup()``): it must not read or
        # create roster files in whatever directory is active *now*; ``startup()`` reloads it afterwards.
        self._roster_io_suspended = True
        try:
            self._reset_game(default_bindings=None)
        finally:
            self._roster_io_suspended = False

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
    def _config_players(self) -> list[dict[str, Any]]:
        """The seats exactly as ``config.yml`` lists them (defaults; the roster overrides name / avatar / texts)."""
        players = []
        for index, item in enumerate(self.cfg.get("participants", []), start=1):
            players.append(
                {
                    "id": str(item["id"]),
                    "seat": str(item.get("seat", index)),
                    "name": str(item.get("name") or item["id"]),
                    "persona": str(item.get("persona", "")),
                    "memory": "",
                    "avatar": None,
                }
            )
        return players

    def _new_world(
        self, spec: ScenarioSpec, seed: int | None = None, players: list[dict[str, Any]] | None = None
    ) -> tuple[World, GameClock]:
        clock = GameClock()
        clock.pause()
        world = World.new_game(
            spec,
            players if players is not None else self._roster_players(),
            clock,
            seed=seed if seed is not None else random.SystemRandom().randrange(1, 2**31),
        )
        world.drain_outbox()  # nobody is listening yet; the snapshot is built from state
        world.drain_events()
        return world, clock

    def _reset_game(self, *, default_bindings: dict[str, Any] | None = None) -> None:
        """Replace the live game with a fresh, idle one (the caller guarantees nothing is running)."""
        self.spec = self.default_spec
        players = self._roster_players()
        world, clock = self._new_world(self.spec, players=players)
        defaults = self._load_defaults() if default_bindings is None else default_bindings
        allowed = {MODERATOR_ID, *(p["id"] for p in players)}
        session = GameSession(
            title=str((self.cfg.get("app") or {}).get("title", "Arena-Agents")),
            scenario_name=self.spec.id,
            scenario_title=self.spec.title,
            participants=[Participant(**p) for p in players],
            tag_styles=copy.deepcopy(self.cfg.get("tag_styles") or {}),
            scenario_spec=self.spec.model_dump(mode="json"),
            model_bindings={a: b.model_copy(deep=True) for a, b in defaults.items() if a in allowed},
            budget=self._load_budget_defaults(),
            memory_enabled=bool(getattr(self, "_memory_enabled_snapshot", True)),
            moderator_avatar=getattr(self, "_moderator_avatar_snapshot", None),
        )
        self.world, self.clock = world, clock
        self.usage = UsageBook()
        self.agent_state = {}
        self._reset_conversations()
        self._set_session(session)
        self._attach_budget()

    def _reset_conversations(self) -> None:
        """A new game / import / restore starts with no conversations (the book and the brain are the
        actors' memory of the *previous* game)."""
        self._book = ConversationBook()
        self._brain = None
        self._stall_notice = ""

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
        self.world, self.clock = self._new_world(self.spec, players=self._session_players())
        self._reset_conversations()
        await self.broadcast_state("identities_shuffled")
        return self.session

    def _session_players(self) -> list[dict[str, Any]]:
        """The seats of the *current* game (kept when only the identities are re-dealt)."""
        return [p.model_dump() for p in self.session.participants]

    # ------------------------------------------------------------------ lifecycle ---
    def _ensure_brain(self, *, fresh: bool) -> ArenaBrain:
        """The brain of this game.  It is kept across pause / resume (its prompts stay byte-identical, which
        is what the provider's prompt cache needs); ``fresh`` rebuilds it for a game that is starting, so
        the persona / memory edits made while it was still idle are taken."""
        brain = self._brain
        if fresh or brain is None or brain.world is not self.world:
            personas = {p.id: p.persona for p in self.session.participants}
            memories = (
                {p.id: p.memory for p in self.session.participants if p.memory} if self.session.memory_enabled else {}
            )
            brain = self._brain = ArenaBrain(self.world, self.spec, self.cfg, personas=personas, memories=memories)
        return brain

    def _context_window_of(self, agent_id: str) -> Callable[[], int]:
        def window() -> int:
            binding = self.session.get_model_binding(agent_id)
            return int(getattr(binding, "context_window_tokens", 0) or 0)

        return window

    def _build_runtime(self, *, fresh: bool = False) -> Supervisor:
        rt = self.cfg.get("runtime") or {}
        if self.llm_port_factory is not None:
            llm_port = self.llm_port_factory(self)
        else:
            gate = LlmGate(
                max_concurrent=int(rt.get("llm_concurrency", 0)),
                per_provider=int(rt.get("llm_concurrency_per_provider", 0)),
            )
            llm_port = GatedEngineLlm(
                self.llm,
                self.session.get_model_binding,
                gate,
                float(rt.get("llm_timeout_seconds", 120)),
            )
        brain = self._ensure_brain(fresh=fresh)
        trace = TraceLog(self._logs_root(), self.session.session_id)
        self._register_trace_secrets(trace)
        agent_config = AgentConfig(
            compact_at=float(rt.get("compact_at", 0.6)),
            trim_at=float(rt.get("trim_at", 0.9)),
        )
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
                book=self._book,
                context_window=self._context_window_of(agent_id),
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
            book=self._book,
        )

    def _register_trace_secrets(self, trace: TraceLog) -> None:
        """The trace log is plaintext: make sure no bound provider's API key can ever be written to it."""
        try:
            store = self._providers()
            seen: set[str] = set()
            for binding in self.session.model_bindings.values():
                if binding.provider_id in seen:
                    continue
                seen.add(binding.provider_id)
                try:
                    provider, _entry = store.resolve(binding)
                except Exception:  # noqa: BLE001 - an unresolved binding has no key to protect
                    continue
                key = str(getattr(provider, "api_key", "") or "")
                if key:
                    trace.add_secret(key)
        except Exception:  # noqa: BLE001 - logging hygiene must never stop a game
            logger.warning("Could not register provider keys with the trace scrubber", exc_info=True)

    def _logs_root(self) -> Path:
        from app.storage.paths import get_data_dir

        return get_data_dir() / "logs"

    async def start_or_resume(self) -> GameSession:
        self._ensure_not_switching()  # also refuses while the workspace is unusable
        if self._is_active():
            return self.session
        self.reload_config()
        # Every actor that will call a model must have a usable binding (never a silent fallback).
        self.ensure_bindings_ready()
        self._refuse_when_over_budget()
        self._recovery_pending = False
        session = self.session
        reason = {"idle": "started", "finished": "continued"}.get(self.world.state.status, "resumed")
        if reason == "started":
            # persona / memory are plain files that may have been edited outside the app while the game was idle:
            # the game starts with what they say now (a resumed game keeps the copy it started with)
            self._refresh_idle_texts()
        session.restored_from_save = False
        session.restore_notice = ""
        session.pause_reason = ""
        session.hold_reason = ""
        self._stall_notice = ""
        self._supervisor = self._build_runtime(fresh=reason == "started")
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
        self.agent_state = {k: "sleeping" for k in self.agent_state}  # a stopped game is a sleeping one

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
            session.pause_reason = why if why in ("user", "hold", "error", "stall", "budget") else "user"
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

    def _on_stall(self, reason: str | None) -> None:
        """The watchdog found the game silent (``reason``) or saw it move again (``None``).  It never pauses
        the game: the banner tells the user and the user decides."""
        self._stall_notice = reason or ""
        self._spawn(self.broadcast_state("stall_updated"))

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
                    "title": s.title,
                    "budget": self.budget_payload(),
                    "memory_enabled": s.memory_enabled,
                    "review": self.review_view(),
                    "stall_notice": self._stall_notice,
                    "clock": round(float(self.clock.now()), 3),
                },
            )
        )
        # (a finished review is part of the save: a restart must not offer to review the same game twice)
        if event_type in {"status_change", "error", "bindings_updated", "budget_updated", "review_updated"}:
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
        why = {
            "hold": "主持人暂停了对局",
            "stall": "对局长时间没有进展，已暂停",
            "error": "运行出错，已暂停",
            "budget": "已达到预算上限，已暂停",
        }.get(s.pause_reason, "已暂停")
        return f"{why}{('：' + s.hold_reason) if s.hold_reason else ''}"

    def public_session(self) -> dict[str, Any]:
        """The PublicSession v2 document (docs/v0.12-contracts.md §8.1): everything a spectator sees,
        never thoughts or tool calls."""
        s = self.session
        snap = world_snapshot(self.world)
        avatars = {p.id: p.avatar for p in s.participants}
        participants = [
            {
                **p,
                "avatar": avatars.get(str(p.get("id"))),
                "state": self.agent_state.get(str(p.get("id")), "sleeping"),
                "sleep_until": self._sleep_until(str(p.get("id"))),
            }
            for p in snap.get("players", [])
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
            "moderator": {
                "id": MODERATOR_ID,
                "name": "主持人",
                "avatar": s.moderator_avatar,
                "state": self.agent_state.get(MODERATOR_ID, "sleeping"),
                "sleep_until": self._sleep_until(MODERATOR_ID),
            },
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
            "budget": self.budget_payload(),
            "memory_enabled": s.memory_enabled,
            "review": self.review_view(),
            "stall_notice": self._stall_notice,
            "demo": bool(getattr(self, "demo_mode", False)),  # scripted bots, no models
        }

    def _sleep_until(self, agent_id: str) -> float | None:
        """When a timed ``sleep`` of the agent ends (game-clock seconds), else ``None``."""
        conversation = self._book.get(agent_id)
        until = getattr(conversation, "sleep_until", None) if conversation is not None else None
        return float(until) if isinstance(until, (int, float)) else None

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
        self._reset_conversations()  # conversations are not saved: a restored game starts them from the board
        self._set_session(loaded)
        self._attach_budget()
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
