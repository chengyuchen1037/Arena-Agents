from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

import yaml
from pydantic import ValidationError

from app.config import load_config
from app.engine.choice import execute_choice_requests
from app.engine.events import SUBSCRIBER_QUEUE_SIZE, SubscriberQueue, encode_event, offer_to
from app.engine.bindings import BindingError
from app.engine.history import normalize_tool_history
from app.engine.legacy_moderator import LegacyModeratorMixin, get_safe_moderator_history
from app.engine.session_migration import migrate_session_payload
from app.engine.session_runtime import AUTOSAVE_EVENTS, SessionRuntimeMixin
from app.engine.llm import LLMClient, ToolCallData
from app.engine.moderator_tools import MODERATOR_TOOL_HANDLERS
from app.engine.participant_tasks import (
    run_ask_yes_no,
    run_concurrent,
    run_interrupt,
    run_planning,
    run_speak,
    run_vote,
)
from app.engine.scenario import ScenarioDocument, ScenarioError, load_config_scenario
from app.engine.state import (
    Channel,
    GameSession,
    create_session_from_config,
    prepare_identity_seed,
)
from app.engine.toolargs import ToolArgumentError, parse_tool_args
from app.engine.tools import execute_shared_tool

logger = logging.getLogger(__name__)

__all__ = [
    "BindingError",
    "ConfigReloadError",
    "GameOrchestrator",
    "SessionLoadError",
    "get_orchestrator",
    "get_safe_moderator_history",
    "orchestrator",
]


class SessionLoadError(ValueError):
    """An imported session failed validation; the current game was left untouched."""


class ConfigReloadError(RuntimeError):
    """config.yml or the scenario could not be (re)loaded; the previous config stays active."""


class GameOrchestrator(SessionRuntimeMixin, LegacyModeratorMixin):
    """Manages session state, SSE subscribers and the scenario-driven async loop."""

    # Class-level defaults so partially initialized instances (tests) stay valid.
    _loop_task: asyncio.Task[None] | None = None
    _pause_requested: bool = False
    _last_stream_broadcast: float = 0.0
    _last_tool_data: Any = None
    _last_tool_error: str | None = None
    _reload_deferred: bool = False

    def __init__(self) -> None:
        self.cfg = load_config()
        self.scenario = load_config_scenario(self.cfg)
        if self.scenario:
            self.cfg["_scenario_snapshot"] = self.scenario.snapshot()
        self.llm = LLMClient(self.cfg)
        self.session: GameSession = create_session_from_config(
            self.cfg,
            randomize_identities=self._shuffle_on_new_game(),
        )
        self._subscribers: set[asyncio.Queue[Any]] = set()
        self._loop_task = None
        self._pause_requested = False
        self._last_stream_broadcast = 0.0
        self._last_tool_data = None
        self._last_tool_error = None
        self._reload_deferred = False

    # ------------------------------------------------------------------ config ---
    def _shuffle_on_new_game(self) -> bool:
        identity_cfg = (
            self.scenario.definition.get("identity_setup", {})
            if self.scenario
            else self.cfg.get("identity_setup", {})
        )
        return bool(identity_cfg.get("shuffle_on_new_game", True))

    def _is_active(self) -> bool:
        return (
            self._loop_task is not None and not self._loop_task.done()
        ) or self.session.status == "running"

    def reload_config(self) -> bool:
        """Re-read config.yml / the scenario / env.  Returns False when deferred.

        A running game keeps the llm/cfg/scenario it started with; the reload is
        postponed to the next start/new game.  The new configuration is built first and
        only swapped in when it loaded completely, so a broken file cannot half-apply.
        """
        if self._is_active():
            self._reload_deferred = True
            return False
        try:
            cfg = load_config()
            scenario = load_config_scenario(cfg)
        except (FileNotFoundError, ScenarioError, yaml.YAMLError, OSError, ValueError) as exc:
            raise ConfigReloadError(f"配置加载失败：{exc}") from exc
        if scenario:
            cfg["_scenario_snapshot"] = scenario.snapshot()
        else:
            cfg.pop("_scenario_snapshot", None)

        previous_effort = getattr(self.llm, "reasoning_effort", None)
        previous_default = (
            self.cfg.get("llm", {}).get("thinking", {}).get("reasoning_effort", "high")
        )
        self.cfg = cfg
        self.scenario = scenario
        reconfigure = getattr(self.llm, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(cfg)
        else:
            self.llm = LLMClient(cfg)
            # Keep a reasoning effort the user changed in the UI across reloads.
            if previous_effort and previous_effort != previous_default:
                self.llm.reasoning_effort = previous_effort
        stats = self.session.usage_stats
        if stats:
            self.llm.input_price_per_m = stats.input_price_per_m
            self.llm.output_price_per_m = stats.output_price_per_m
            self.llm.context_window_tokens = stats.context_window_tokens
        self._reload_deferred = False
        return True

    # --------------------------------------------------------------- broadcast ---
    def subscribe(self) -> asyncio.Queue[Any]:
        queue: asyncio.Queue[Any] = SubscriberQueue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, q: asyncio.Queue[Any]) -> None:
        self._subscribers.discard(q)

    async def broadcast_state(self, event_type: str = "state_update") -> None:
        if event_type in AUTOSAVE_EVENTS:
            self.schedule_autosave(event_type)  # debounced; streaming chunks never save
        if not self._subscribers:
            return  # nobody listening: do not pay for serialization
        payload = encode_event(event_type, self.session)  # serialized once for all subscribers
        dead: list[asyncio.Queue[Any]] = []
        for queue in list(self._subscribers):
            try:
                offer_to(queue, payload)
            except Exception:  # noqa: BLE001 - a broken subscriber must not break the game
                logger.debug("Dropping broken SSE subscriber", exc_info=True)
                dead.append(queue)
        for queue in dead:
            self._subscribers.discard(queue)

    async def _throttled_stream_broadcast(self) -> None:
        now = time.monotonic()
        if now - self._last_stream_broadcast >= 0.12:
            self._last_stream_broadcast = now
            await self.broadcast_state("stream_chunk")

    # ----------------------------------------------------------- game lifecycle ---
    async def new_game(self, *, randomize_identities: bool | None = None) -> GameSession:
        """Start a fresh game; ``None`` falls back to the scenario's ``shuffle_on_new_game``."""
        self._refuse_while_switching()
        await self.force_pause(silent=True)
        self.reload_config()
        randomize = (
            self._shuffle_on_new_game() if randomize_identities is None else randomize_identities
        )
        self._recovery_pending = False  # an explicit new game ends the "needs recovery" hold
        self._set_session(
            create_session_from_config(
                self.cfg,
                randomize_identities=randomize,
                default_bindings=self._load_defaults(),
            )
        )
        self.refresh_binding_issues()
        await self.broadcast_state("new_game")
        return self.session

    async def shuffle_identities(self) -> GameSession:
        if self.session.status != "idle":
            raise RuntimeError("仅未开始的新对局可以刷新身份；请先新建对局。")
        self.reload_config()
        if self.scenario:
            self.session.scenario_snapshot = self.scenario.snapshot()
            self.session.scenario_name = self.scenario.scenario_id
            self.session.scenario_runtime = {}
            self.session.prompt_cache.clear()
        assignments = prepare_identity_seed(self.session, self.cfg, randomize=True)
        self.session.moderator_messages.clear()
        summary = "、".join(
            f"{p.name}({assignments.get(p.id, {}).get('role', '')})"
            for p in self.session.participants
        )
        self.session.add_record(
            stream="engine",
            kind="note",
            actor_id="moderator",
            content=f"已重新生成开局身份随机分配种子（待主持人开局通过工具赋予标签）：{summary}",
        )
        await self.broadcast_state("identities_shuffled")
        return self.session

    @staticmethod
    def _validate_session_data(data: Any) -> GameSession:
        """Validate an imported payload completely, without touching any live state."""
        if not isinstance(data, dict):
            raise SessionLoadError("存档必须是 JSON 对象")
        try:
            loaded = GameSession.model_validate(migrate_session_payload(data))
            if loaded.scenario_snapshot:
                ScenarioDocument.from_snapshot(loaded.scenario_snapshot)
            loaded.moderator_messages, history_report = normalize_tool_history(
                loaded.moderator_messages
            )
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in exc.errors(include_input=False, include_url=False)[:5]
            )
            raise SessionLoadError(f"存档校验失败：{problems}") from exc
        except ScenarioError as exc:
            raise SessionLoadError(f"存档中的剧本无效：{exc}") from exc
        except (TypeError, KeyError, AttributeError, ValueError) as exc:
            raise SessionLoadError(f"存档结构无效：{type(exc).__name__}: {exc}") from exc
        if any(history_report.values()):
            logger.warning("Repaired imported moderator tool history: %s", history_report)
        return loaded

    async def load_session_data(self, data: dict[str, Any]) -> GameSession:
        """Replace the current game with an imported one.

        Validate first; on failure (:class:`SessionLoadError`, a ``ValueError``) the
        running game is not paused, reset or otherwise modified.
        """
        self._refuse_while_switching()
        loaded = self._validate_session_data(data)
        await self.force_pause(silent=True)
        try:
            self.reload_config()
        except ConfigReloadError:
            logger.warning("Config reload failed during import; keeping the current config", exc_info=True)
        self._recovery_pending = False
        self._adopt_loaded_session(loaded, "已导入存档")
        await self.broadcast_state("session_loaded")
        return self.session

    async def start_or_resume(self) -> GameSession:
        self._ensure_not_switching()  # also refuses while the workspace is unusable
        self.reload_config()  # deferred (False) while a loop is still alive
        if self.session.status == "finished":
            raise RuntimeError("当前对局已结束，请点击「新建」开启新对局。")
        if self._loop_task and not self._loop_task.done():
            self._pause_requested = False
            self.session.status = "running"
            await self.broadcast_state("status_change")
            return self.session

        # Every actor that will call a model must have a usable binding (never a silent fallback).
        self.ensure_bindings_ready()
        self._recovery_pending = False
        self.session.restored_from_save = False
        self.session.restore_notice = ""
        self._pause_requested = False
        self.session.status = "running"
        self.session.set_generating("moderator", "主持人 · 启动编排中")
        self.session.set_moderator_state(True, "启动中 · 准备加载规则与工具")
        await self.broadcast_state("status_change")
        self._loop_task = asyncio.create_task(
            self._run_scenario_loop() if self.session.scenario_snapshot else self._run_loop()
        )
        return self.session

    async def request_pause(self) -> GameSession:
        self._pause_requested = True
        if not self._loop_task or self._loop_task.done():
            self.session.status = "paused"
            self.session.clear_all_generating()
            self.session.active_status_text = "已暂停"
            self.session.set_moderator_state(False, "已暂停")
        else:
            self.session.active_status_text = "等待当前步骤完成后暂停..."
            self.session.set_moderator_state(True, "等待当前原子步骤完成后暂停...")
        await self.broadcast_state("status_change")
        return self.session

    async def force_pause(self, *, silent: bool = False) -> GameSession:
        self._pause_requested = True
        task = self._loop_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - the loop already reported its own failure
                logger.exception("Game loop raised while being force-paused")
        self._loop_task = None
        self.session.streaming_draft = None
        self.session.clear_all_generating()
        if self.session.status != "finished":
            self.session.status = "paused" if not silent else "idle"
            self.session.active_status_text = "已强制暂停" if not silent else "等待开局"
            self.session.set_moderator_state(
                False, "已强制暂停" if not silent else "待命 · 等待开局"
            )
        if not silent:
            await self.broadcast_state("status_change")
        return self.session

    # -------------------------------------------------------------- run loops ---
    async def _guarded_loop(self, body: Callable[[], Awaitable[None]], log_message: str) -> None:
        """Run *body* with the shared pause / cancel / failure epilogue."""
        session = self.session  # a replaced session must never be touched by this loop
        try:
            await body()
            if session.status == "running" and self._pause_requested:
                session.status = "paused"
                session.clear_all_generating()
                session.active_status_text = "已暂停"
                session.set_moderator_state(False, "已暂停")
                await self.broadcast_state("status_change")
        except asyncio.CancelledError:
            session.streaming_draft = None
            session.clear_all_generating()
            session.active_status_text = "已强制暂停"
            session.set_moderator_state(False, "已强制暂停")
            raise
        except Exception as exc:  # noqa: BLE001 - report and pause instead of crashing the task
            logger.exception(log_message)
            session.streaming_draft = None
            session.status = "paused"
            session.clear_all_generating()
            session.active_status_text = f"运行异常已暂停: {exc}"
            session.set_moderator_state(False, f"异常暂停: {exc}")
            session.add_record(
                stream="engine",
                kind="note",
                actor_id="moderator",
                content=f"[引擎异常] {exc}",
            )
            await self.broadcast_state("error")

    async def _run_scenario_loop(self) -> None:
        """Run the snapshotted executable Markdown scenario without a free-form rules arbiter."""
        from app.engine.director import ScenarioDirector

        async def body() -> None:
            await ScenarioDirector(self).run()

        await self._guarded_loop(body, "Scenario loop error")

    # ------------------------------------------------------------ tool dispatch ---
    def _tool_error(
        self,
        name: str,
        args: dict[str, Any],
        message: str,
        channel_id: str | None = None,
    ) -> tuple[str, None]:
        """Report a failed tool: logged, returned to the caller, and flagged for the director."""
        self._last_tool_error = message
        self._log_tool(name, args, message, channel_id)
        return message, None

    async def _execute_moderator_tool(self, tc: ToolCallData) -> tuple[str, str | None]:
        """Execute a single moderator tool call. Returns (tool_result, interrupt_event_or_None)."""
        name = tc.name
        self._last_tool_error = None
        args, arg_error = parse_tool_args(tc)
        if arg_error:
            return self._tool_error(name, {}, f"错误：工具 {name} 的参数无效：{arg_error}")
        channel = args.get("channel_id")
        channel = channel if isinstance(channel, str) and channel else None

        shared_res = execute_shared_tool("moderator", name, args, self.session, self.cfg)
        if shared_res is not None:
            if shared_res.startswith('{"ok": false'):
                self._last_tool_error = shared_res
            self._log_tool(name, args, shared_res, None)
            await self.broadcast_state("tool_executed")
            return shared_res, None

        handler = MODERATOR_TOOL_HANDLERS.get(name)
        if handler is None:
            return self._tool_error(name, args, f"未知工具：{name}", channel)
        try:
            return await handler(self, name, args)
        except ToolArgumentError as exc:
            return self._tool_error(name, args, f"错误：工具 {name} 的参数无效：{exc}", channel)

    async def _handle_request_choices(self, args: dict[str, Any]) -> tuple[str, str | None]:
        try:
            message, data = await execute_choice_requests(self, args)
        except ValueError as exc:
            return self._tool_error("request_choices", args, f"错误：{exc}")
        self._last_tool_data = data
        return message, None

    async def _run_participant_speak(
        self, player_id: str, channel_id: str, instruction: str
    ) -> tuple[str, str | None]:
        return await run_speak(self, player_id, channel_id, instruction)

    async def _handle_request_players_concurrent(self, args: dict[str, Any]) -> tuple[str, str | None]:
        return await run_concurrent(self, "request_players_concurrent", args)

    async def _handle_ask_yes_or_no(self, args: dict[str, Any]) -> tuple[str, str | None]:
        return await run_ask_yes_no(self, args)

    async def _handle_request_players_strategic_planning(
        self, args: dict[str, Any]
    ) -> tuple[str, str | None]:
        return await run_planning(self, args)

    async def _handle_request_players_vote(self, args: dict[str, Any]) -> tuple[str, str | None]:
        return await run_vote(self, args)

    async def _handle_request_player_interrupt(self, args: dict[str, Any]) -> tuple[str, str | None]:
        return await run_interrupt(self, args)

    # ------------------------------------------------------------------ logging ---
    def _log_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_result: str,
        channel_id: str | None,
        action_id: str | None = None,
        action_type: str | None = None,
    ) -> None:
        self.session.add_record(
            stream="engine",
            channel_id=channel_id,
            kind="tool_call",
            actor_id="moderator",
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=tool_result,
            content=f"{tool_name}({json.dumps(tool_args, ensure_ascii=False)}) -> {tool_result}",
            action_id=action_id or self.session.active_action_id,
            action_type=action_type or self.session.active_action_type or "moderator",
        )

    def _log_participant_tool(
        self,
        player_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_result: str,
        channel_id: str | None,
        action_id: str | None = None,
        action_type: str | None = None,
    ) -> None:
        from app.engine.participant_loop import log_participant_tool

        log_participant_tool(
            self.session,
            player_id,
            tool_name,
            tool_args,
            tool_result,
            channel_id,
            action_id=action_id,
            action_type=action_type,
        )

    def _ensure_player_dm_channel(self, player_id: str) -> str:
        """Ensure a private 1-on-1 DM channel between `moderator` and `player_id` exists."""
        for ch in self.session.channels:
            if ch.type == "dm" and player_id in ch.members and len(ch.members) <= 2:
                if "moderator" not in ch.members:
                    ch.members.insert(0, "moderator")
                ch.status = "open"
                return ch.id

        dm_cid = f"dm-{player_id}"
        existing = self.session.get_channel(dm_cid)
        if existing:
            if "moderator" not in existing.members:
                existing.members.insert(0, "moderator")
            if player_id not in existing.members:
                existing.members.append(player_id)
            existing.status = "open"
            return dm_cid

        participant = self.session.get_participant(player_id)
        pname = participant.name if participant else player_id
        self.session.channels.append(
            Channel(
                id=dm_cid,
                name=f"{pname} 私聊",
                type="dm",
                status="open",
                members=["moderator", player_id],
            )
        )
        return dm_cid


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
