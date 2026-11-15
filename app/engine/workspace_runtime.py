"""Workspace-aware startup and the safe ``switch_workspace`` transaction.

A *workspace* is a user-chosen folder that holds everything private to a set of games: providers,
templates, bindings, sessions, logs and the encryption key (see ``app.storage.workspace``).  The
program-global assets (``config.yml``, ``scenarios/``, ``prompts/``) are not part of it.

The storage module is looked up lazily and can be injected per instance (``workspace_module`` and
``store_cache_clearer``), so tests run against fakes or temp directories and never touch the real
workspace.  With the workspace feature disabled (``ARENA_DISABLE_WORKSPACE``, or the module not
present) nothing in this file does anything and v0.10 behaviour is unchanged.

Switching is a transaction with these guarantees:

* refused while a game runs (``RuntimeError`` — the API answers 409);
* the current game is saved first and a failed save aborts the switch;
* the target is validated / created / locked and activated *before* anything of the running
  process changes; any failure rolls the previous workspace back, so the app is never half-switched;
* store caches are cleared and the LLM client rebuilt only after a successful activation;
* nothing is ever deleted from either workspace;
* the whole switch runs under the persist lock, so no save of the old game can land in the new
  workspace and no autosave is scheduled while it runs.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from app.engine.llm import LLMClient
from app.engine.state import create_session_from_config

logger = logging.getLogger(__name__)

_FALLBACK_MARKER = ".arena-workspace.json"


class WorkspaceRuntimeMixin:
    """Methods of ``GameOrchestrator`` that deal with the active workspace."""

    workspace_module: Any = None  # injected in tests; default: app.storage.workspace
    store_cache_clearer: Any = None  # injected in tests; default: app.storage.clear_store_caches
    _workspace_lock: asyncio.Lock | None = None
    _workspace_failed: bool = False

    # --------------------------------------------------------------- plumbing ---
    def _ws(self) -> Any:
        if self.workspace_module is not None:
            return self.workspace_module
        try:
            import app.storage.workspace as module
        except ImportError:
            return None
        return module

    def _workspace_enabled(self) -> bool:
        module = self._ws()
        if module is None:
            return False
        try:
            return bool(module.workspace_enabled())
        except Exception:  # noqa: BLE001 - an unreadable switch means "off", not a crash
            logger.warning("workspace_enabled() failed; treating the feature as disabled", exc_info=True)
            return False

    def _workspace_error(self, message: str) -> Exception:
        module = self._ws()
        error_type = getattr(module, "WorkspaceError", ValueError)
        return error_type(message)

    def _clear_store_caches(self) -> None:
        """Drop the process-wide store singletons (never the crypto test key)."""
        if self.store_cache_clearer is not None:
            self.store_cache_clearer()
            return
        try:
            from app.storage import clear_store_caches
        except ImportError:
            from app.providers.store import reset_provider_store
            from app.storage.sessions import reset_session_store
            from app.storage.settings import reset_settings_store

            reset_provider_store()
            reset_session_store()
            reset_settings_store()
            return
        clear_store_caches()

    def _get_workspace_lock(self) -> asyncio.Lock:
        if self._workspace_lock is None:
            self._workspace_lock = asyncio.Lock()
        return self._workspace_lock

    def _ensure_not_switching(self) -> None:
        """Guard for operations that must not run mid-switch or without a usable workspace."""
        if self._switching_workspace:
            raise RuntimeError("正在切换工作文件夹，请稍后再试")
        if self._workspace_failed:
            raise RuntimeError("工作文件夹不可用（初始化失败），请先选择一个工作文件夹")

    def _refuse_while_switching(self) -> None:
        if self._switching_workspace:
            raise RuntimeError("正在切换工作文件夹，请稍后再试")

    def _release_workspace(self) -> None:
        """Release the workspace lock on a clean exit (best effort)."""
        if not self._workspace_enabled():
            return
        try:
            self._ws().deactivate_workspace()
        except Exception:  # noqa: BLE001
            logger.warning("Releasing the workspace failed", exc_info=True)

    def workspace_payload(self, *, include_path: bool = True) -> dict[str, Any]:
        """Minimal workspace state for the API: enabled flag, name, id, optional path."""
        module = self._ws()
        if module is None or not self._workspace_enabled():
            return {"enabled": False}
        payload: dict[str, Any] = {"name": None, "id": None}
        try:
            active = module.get_active_workspace_payload(include_path=include_path)
            if isinstance(active, dict):
                payload.update(active)
        except Exception:  # noqa: BLE001
            logger.warning("Could not read the active workspace", exc_info=True)
        if not include_path:
            payload.pop("path", None)
        payload["enabled"] = True
        payload["degraded"] = bool(self._workspace_failed)
        payload["last_session"] = None if self._workspace_failed else self.session.session_id
        payload["status"] = "degraded" if self._workspace_failed else "ok"
        return payload

    # ---------------------------------------------------------------- startup ---
    async def _startup_workspace(self, report: dict[str, Any]) -> bool:
        """Activate the initial workspace.  Returns False when startup must stop (degraded)."""
        module = self._ws()
        try:
            info, ws_report = await asyncio.to_thread(module.ensure_initial_workspace)
        except Exception as exc:  # noqa: BLE001 - the process must come up, degraded
            logger.exception("Workspace initialization failed")
            self._enter_workspace_failure(report, exc)
            return False
        report["workspace"] = ws_report if isinstance(ws_report, dict) else {"status": "ok"}
        problem = _report_problem(ws_report)
        if problem:
            logger.error("Workspace initialization reported a problem: %s", problem)
            self._enter_workspace_failure(report, ValueError(problem))
            return False
        self._clear_store_caches()
        if self.session.status == "idle" and not self.session.records:
            # The constructor built this placeholder before any workspace existed; rebuild it so
            # its default bindings come from the active workspace.
            self._set_session(
                create_session_from_config(
                    self.cfg,
                    randomize_identities=self._shuffle_on_new_game(),
                    default_bindings=self._load_defaults(),
                )
            )
        self._workspace_failed = False
        return True

    def _enter_workspace_failure(self, report: dict[str, Any], exc: BaseException) -> None:
        from app.engine.session_runtime import _safe_error_text

        self._workspace_failed = True
        self._recovery_pending = True
        self.recovery_error = f"{type(exc).__name__}: {_safe_error_text(exc)}"
        self.session.restore_notice = (
            f"工作文件夹初始化失败：{_safe_error_text(exc)}。已有数据未被改动或删除；"
            "请选择一个可用的工作文件夹后继续。在此之前不会保存对局。"
        )
        report["workspace"] = {"error": type(exc).__name__}
        report["recovery_error"] = self.recovery_error

    # --------------------------------------------------------------- switching ---
    def _same_root(self, left: Any, right: Any) -> bool:
        try:
            return Path(left).resolve() == Path(right).resolve()
        except OSError:
            return False

    def _resolve_target(
        self, target: Path, *, initialize_existing: bool, create_if_missing: bool
    ) -> Any:
        """Open an existing workspace or (only when asked) create one.  No activation here."""
        module = self._ws()
        marker = getattr(module, "WORKSPACE_MARKER", _FALLBACK_MARKER)
        try:
            return module.open_workspace(target)
        except module.WorkspaceError as open_error:
            has_marker = (target / marker).exists()
            if has_marker or not (create_if_missing or initialize_existing):
                if has_marker:
                    raise  # a marker is present but unusable: report exactly why
                raise self._workspace_error(
                    "该文件夹还不是工作文件夹；请确认“创建/初始化”后再切换"
                ) from open_error
            return module.create_workspace(target, initialize_existing=initialize_existing)

    def _rollback_workspace(self, previous: Any) -> None:
        """Re-activate the previous workspace after a failed switch (synchronous, best effort)."""
        module = self._ws()
        try:
            current = module.get_active_workspace()
            if previous is None:
                if current is not None:
                    module.deactivate_workspace()
            elif current is None or not self._same_root(current.root, previous.root):
                module.activate_workspace(previous)
            self._clear_store_caches()
        except Exception as exc:  # noqa: BLE001 - nothing more can be done; surface as degraded
            logger.exception("Rolling back the workspace switch failed")
            self._workspace_failed = True
            self._recovery_pending = True
            self.recovery_error = f"{type(exc).__name__}: 回滚工作文件夹失败"

    async def _quiesce_autosave(self) -> None:
        self._autosave_dirty = False
        task = self._autosave_task
        if task is not None and not task.done():
            if not self._persisting:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _make_llm(self) -> Any:
        """A fresh LLM client for the new workspace (its provider cache starts empty)."""
        new = LLMClient(self.cfg, provider_store=self.provider_store)
        old = self.llm
        if getattr(old, "_effort_user_set", False):
            new.reasoning_effort = old.reasoning_effort  # a UI-chosen effort survives the switch
        return new

    async def switch_workspace(
        self,
        path: str | Path,
        *,
        initialize_existing: bool = False,
        create_if_missing: bool = False,
    ) -> dict[str, Any]:
        """Switch to another workspace folder; see the module docstring for the guarantees.

        Returns ``{"workspace": payload, "session": GameSession, "restored": id | None,
        "recovery_error": str | None, "changed": bool}``.
        Raises ``RuntimeError`` while the game runs / a switch is in progress, and the storage
        module's ``WorkspaceError`` (a ``ValueError``) for an invalid, locked or unusable target.
        """
        module = self._ws()
        if module is None or not self._workspace_enabled():
            raise ValueError("工作文件夹功能未启用")
        if self._is_active():
            raise RuntimeError("对局进行中，不能切换工作文件夹；请先暂停对局")
        if self._switching_workspace or self._get_workspace_lock().locked():
            raise RuntimeError("正在切换工作文件夹，请稍后再试")
        target = Path(path)
        if not target.is_absolute():
            raise self._workspace_error("工作文件夹必须是绝对路径")

        async with self._get_workspace_lock():
            if self._is_active():  # re-check: a start may have slipped in before we got the lock
                raise RuntimeError("对局进行中，不能切换工作文件夹；请先暂停对局")
            self._switching_workspace = True
            old_llm = self.llm
            try:
                await self._quiesce_autosave()
                async with self._get_persist_lock():
                    if self._is_active():
                        raise RuntimeError("对局进行中，不能切换工作文件夹；请先暂停对局")
                    result = await self._switch_locked(
                        module, target, initialize_existing, create_if_missing
                    )
            finally:
                self._switching_workspace = False

        if result["changed"]:
            if old_llm is not self.llm:
                aclose = getattr(old_llm, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:  # noqa: BLE001
                        logger.warning("Closing the previous LLM client failed", exc_info=True)
            await self.broadcast_state("workspace_changed")
        return result

    async def _switch_locked(
        self, module: Any, target: Path, initialize_existing: bool, create_if_missing: bool
    ) -> dict[str, Any]:
        # 1. Save the current game.  A failure aborts before anything changed.
        if not (self._recovery_pending or self._workspace_failed):
            await self.persist_session("pre-workspace-switch", raise_errors=True, _lock_held=True)

        # 2. Validate / create the target (still not active).
        info = await asyncio.to_thread(
            self._resolve_target,
            target,
            initialize_existing=initialize_existing,
            create_if_missing=create_if_missing,
        )
        previous = module.get_active_workspace()
        if previous is not None and self._same_root(info.root, previous.root):
            return {
                "workspace": self.workspace_payload(include_path=True),
                "session": self.session,
                "restored": None,
                "recovery_error": self.recovery_error or None,
                "changed": False,
            }

        # 3. Everything that can fail happens before the process state changes.
        new_llm = self._make_llm()
        try:
            await asyncio.to_thread(module.activate_workspace, info)
            self._clear_store_caches()
        except BaseException:
            self._rollback_workspace(previous)
            raise

        # 4. Committed: swap the runtime state (no awaits between the swaps).
        self.llm = new_llm
        self._workspace_failed = False
        self._recovery_pending = False
        self.recovery_error = ""
        self._autosave_dirty = False
        self._set_session(
            create_session_from_config(
                self.cfg,
                randomize_identities=self._shuffle_on_new_game(),
                default_bindings=self._load_defaults(),
            )
        )
        stats = self.session.usage_stats
        new_llm.input_price_per_m = stats.input_price_per_m
        new_llm.output_price_per_m = stats.output_price_per_m
        new_llm.context_window_tokens = stats.context_window_tokens

        # 5. The target's last game comes back paused; a damaged one is kept untouched and the
        #    workspace opens in "needs recovery" (no autosave over it), exactly like startup.
        restored = await self._restore_last_session()
        self.refresh_binding_issues()
        self._autosave_enabled = True
        return {
            "workspace": self.workspace_payload(include_path=True),
            "session": self.session,
            "restored": restored,
            "recovery_error": self.recovery_error if self._recovery_pending else None,
            "changed": True,
        }


def _report_problem(ws_report: Any) -> str:
    """A message when ``ensure_initial_workspace``'s report says the setup failed, else ''."""
    if not isinstance(ws_report, dict):
        return ""
    for key in ("error", "errors"):
        value = ws_report.get(key)
        if value:
            return str(value if not isinstance(value, (list, tuple)) else "; ".join(map(str, value)))[:300]
    if str(ws_report.get("status", "")).lower() in {"error", "failed"}:
        return "工作文件夹初始化失败"
    return ""
