"""Model bindings, persistence, restore and shutdown of the running game.

Mixed into :class:`~app.game.orchestrator.GameOrchestrator`.  The stores (provider registry,
settings, encrypted session store) are resolved lazily through ``get_*`` helpers and can be
injected per instance (``provider_store`` / ``settings_store`` / ``session_store``), which is how
the tests run without touching the real data directory.

Persistence rules (see docs/workspaces.md):

* the snapshot is built on the event loop (consistent, no ``await`` in between) and encrypted /
  written in a worker thread;
* the write is bound to the session object it snapshotted: the ``last_session_id`` pointer is only
  moved if that session is still the live one, so a slow save can never point at a replaced game;
* automatic saves are debounced, a failure is recorded on the session (``autosave_error``) and
  logged — never swallowed and never turned into overwriting the live game;
* if the last game cannot be restored, the (untouched) files are kept and automatic saving is held
  back until the user explicitly starts, creates or imports a game;
* a game saved by an older engine (schema < 3) is *not* an error state (D3): it stays in the saved
  list untouched, a fresh game is created and the user is told why.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.game.bindings import (
    BindingError,
    compute_binding_issues,
    fill_missing_from_defaults,
    normalized_binding,
)
from app.game.session import MODERATOR_ID, SESSION_SCHEMA_VERSION, BindingIssue, GameSession
from app.game.workspace_runtime import WorkspaceRuntimeMixin
from app.providers.models import ModelBinding

logger = logging.getLogger(__name__)

OLD_SAVE_NOTICE = (
    "上次的对局来自旧版本引擎（v0.11 及更早），v0.12 不兼容，已保留在存档列表中、未被改动；已为你新建了一局。"
)


class SessionRuntimeMixin(WorkspaceRuntimeMixin):
    # Class-level defaults keep partially initialized instances (tests) valid.
    provider_store: Any = None
    settings_store: Any = None
    session_store: Any = None
    _autosave_enabled: bool = False
    _autosave_dirty: bool = False
    _autosave_reason: str = ""
    _autosave_task: asyncio.Task[None] | None = None
    _autosave_delay: float = 0.5
    _persist_lock: asyncio.Lock | None = None
    _persisting: bool = False
    _session_generation: int = 0
    _recovery_pending: bool = False
    _shutting_down: bool = False
    _switching_workspace: bool = False
    recovery_error: str = ""

    # ------------------------------------------------------------------ stores ---
    def _providers(self) -> Any:
        if self.provider_store is not None:
            return self.provider_store
        from app.providers.store import get_provider_store

        return get_provider_store()

    def _settings(self) -> Any:
        if self.settings_store is not None:
            return self.settings_store
        from app.storage import settings

        return settings

    def _sessions(self) -> Any:
        if self.session_store is not None:
            return self.session_store
        from app.storage.sessions import get_session_store

        return get_session_store()

    def _known_actor_ids(self) -> set[str]:
        """The seats a new game will have (``config.yml``), plus the moderator."""
        return {MODERATOR_ID, *(str(p["id"]) for p in self.cfg.get("participants", []))}

    def _load_defaults(self) -> dict[str, ModelBinding]:
        try:
            from app.storage.settings import migrate_legacy_actor_ids

            # bindings saved by v0.12.0 under p01..p12 are the ones of N1..N12 now
            return migrate_legacy_actor_ids(dict(self._settings().get_default_bindings()), self._known_actor_ids())
        except Exception:  # noqa: BLE001 - settings problems must not block a new game
            logger.warning("Could not load default model bindings", exc_info=True)
            return {}

    # ---------------------------------------------------------------- bindings ---
    @property
    def restored_from_save(self) -> bool:
        return bool(self.session.restored_from_save)

    def get_model_binding(self, actor_id: str) -> ModelBinding | None:
        return self.session.get_model_binding(actor_id)

    def refresh_binding_issues(self) -> list[BindingIssue]:
        """Recompute (and store on the session) which bindings cannot be used right now."""
        session = self.session
        try:
            store = self._providers()
        except Exception:  # noqa: BLE001
            logger.warning("Provider store unavailable", exc_info=True)
            from app.game.bindings import required_actors

            issues = [
                BindingIssue(actor_id=a, message="Provider 配置无法读取（加密存储不可用）")
                for a in required_actors(session)
            ]
        else:
            issues = compute_binding_issues(session, store, self.cfg)
        session.binding_issues = issues
        return issues

    def ensure_bindings_ready(self) -> None:
        """Raise :class:`BindingError` ("需要重新绑定") unless every actor can call its model."""
        issues = self.refresh_binding_issues()
        if issues:
            raise BindingError(issues)

    async def update_model_bindings(
        self, mapping: dict[str, ModelBinding | dict[str, Any] | None]
    ) -> dict[str, ModelBinding]:
        """Merge *mapping* into the session's bindings and the saved defaults.

        Refused while the game runs.  Every binding is resolved against the provider store first
        (unknown provider/model raise the store's ``ProviderError`` subclasses); a value of ``None``
        removes the binding.  Nothing is applied unless everything validates and the defaults were
        persisted.
        """
        session = self.session
        if self._is_active():
            raise RuntimeError("对局进行中，不能修改模型绑定；请先暂停对局")
        self._ensure_not_switching()
        if not isinstance(mapping, dict):
            raise ValueError("bindings 必须是对象")

        allowed = {MODERATOR_ID, *(p.id for p in session.participants)}
        unknown = sorted(set(mapping) - allowed)
        if unknown:
            raise ValueError(f"未知的参与者：{', '.join(unknown)}")

        store = self._providers()
        new: dict[str, ModelBinding] = {}
        removed: list[str] = []
        for actor_id, value in mapping.items():
            if value is None:
                removed.append(actor_id)
                continue
            binding = value if isinstance(value, ModelBinding) else ModelBinding.model_validate(value)
            _, entry = store.resolve(binding)  # ProviderNotFound / ModelNotFound -> ValueError
            new[actor_id] = normalized_binding(binding, entry)

        defaults = self._load_defaults()
        defaults.update(new)
        for actor_id in removed:
            defaults.pop(actor_id, None)
        await asyncio.to_thread(self._settings().save_default_bindings, defaults)

        for actor_id in removed:
            session.model_bindings.pop(actor_id, None)
        session.model_bindings.update(new)
        self.refresh_binding_issues()
        await self.broadcast_state("bindings_updated")
        return dict(session.model_bindings)

    # -------------------------------------------------------------- persistence ---
    def schedule_autosave(self, reason: str) -> None:
        """Mark the game dirty; a debounced background task writes it."""
        if (
            not self._autosave_enabled
            or self._shutting_down
            or self._recovery_pending
            or self._switching_workspace
        ):
            return
        self._autosave_dirty = True
        self._autosave_reason = reason
        task = self._autosave_task
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._autosave_task = loop.create_task(self._autosave_worker())

    async def _autosave_worker(self) -> None:
        await asyncio.sleep(self._autosave_delay)
        while self._autosave_dirty and not self._shutting_down:
            self._autosave_dirty = False
            await self.persist_session(self._autosave_reason, raise_errors=False)
            if self._autosave_dirty:
                await asyncio.sleep(self._autosave_delay)

    async def flush_autosave(self, reason: str = "flush") -> bool:
        """Stop the debounce timer and write the current state right now."""
        task = self._autosave_task
        if task is not None and not task.done():
            if self._persisting:
                await asyncio.gather(task, return_exceptions=True)  # let the running write finish
            else:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._autosave_dirty = False
        if self._recovery_pending:
            return False
        return await self.persist_session(reason, raise_errors=False)

    def _get_persist_lock(self) -> asyncio.Lock:
        if self._persist_lock is None:
            self._persist_lock = asyncio.Lock()
        return self._persist_lock

    async def _persist_body(self, reason: str) -> tuple[GameSession, Exception | None]:
        """Snapshot + write; the caller must hold the persist lock."""
        session = self.session
        generation = self._session_generation
        self._persisting = True
        try:
            payload = self.session_payload()  # consistent snapshot, no await in between
            store = self._sessions()
            await asyncio.to_thread(store.save, session.session_id, payload)
            if session is self.session and generation == self._session_generation:
                await asyncio.to_thread(self._settings().set_last_session_id, session.session_id)
            return session, None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported by the caller, never swallowed silently
            logger.exception("Saving session %s failed (%s)", session.session_id, reason)
            return session, exc
        finally:
            self._persisting = False

    async def persist_session(
        self,
        reason: str = "manual",
        *,
        raise_errors: bool = True,
        _lock_held: bool = False,
    ) -> bool:
        """Encrypt and save the live game to the external session store.

        The consistent snapshot is taken on the event loop; the (blocking) encryption and file I/O
        run in a thread.  The ``last_session_id`` pointer moves only if the snapshotted session is
        still the live one.  Failures are logged and recorded in ``session.autosave_error``; with
        ``raise_errors`` (manual saves) they are re-raised for the caller to report.
        ``_lock_held`` is for the workspace switch, which already owns the persist lock.
        """
        if self._workspace_failed:
            # No usable workspace: saving would fall back to the legacy global directory.
            error = RuntimeError("工作文件夹不可用，无法保存；请先选择一个工作文件夹")
            if raise_errors:
                raise error
            return False
        if _lock_held:
            session, failed = await self._persist_body(reason)
        else:
            async with self._get_persist_lock():
                session, failed = await self._persist_body(reason)

        if failed is None:
            if session.autosave_error:
                session.autosave_error = ""
                if session is self.session:
                    await self.broadcast_state("autosave_error")
            return True
        session.autosave_error = f"保存对局失败（{reason}）：{_safe_error_text(failed)}"
        if session is self.session:
            await self.broadcast_state("autosave_error")
        if raise_errors:
            raise failed
        return False

    async def _restore_last_session(self) -> str | None:
        """Restore the game ``last_session_id`` points to, paused.  Returns its id, if restored."""
        try:
            last_id = await asyncio.to_thread(self._settings().get_last_session_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not read the last-session pointer")
            self._enter_recovery(None, exc)
            return None
        if not last_id:
            return None
        try:
            payload = await asyncio.to_thread(self._sessions().load, last_id)
        except Exception as exc:  # noqa: BLE001 - keep the files, degrade, never overwrite
            logger.exception("Could not restore session %s", last_id)
            self._enter_recovery(last_id, exc)
            return None
        schema = payload.get("schema_version") if isinstance(payload, dict) else None
        if schema != SESSION_SCHEMA_VERSION:
            # D3: older saves are not compatible.  Keep them, start fresh and say why.
            logger.info("Last session %s uses save format %r; not restoring (incompatible)", last_id, schema)
            self.session.restore_notice = OLD_SAVE_NOTICE
            return None
        try:
            loaded = self._validate_session_data(payload)
            await self._adopt_loaded_session(loaded, "已恢复上次对局")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not restore session %s", last_id)
            self._enter_recovery(last_id, exc)
            return None
        return loaded.session_id

    def _enter_recovery(self, session_id: str | None, exc: BaseException) -> None:
        self._recovery_pending = True
        self.recovery_error = f"{type(exc).__name__}: {_safe_error_text(exc)}"
        target = f"（{session_id}）" if session_id else ""
        self.session.restore_notice = (
            f"无法恢复上次对局{target}：{_safe_error_text(exc)}。原存档文件已保留、未被覆盖；"
            "当前为一局空白对局，在您新建/开始/导入之前不会自动保存。"
        )

    async def startup(self) -> dict[str, Any]:
        """Application start: migrate legacy data, reload config, restore the last game (paused).

        Never raises for migration/restore problems (the app must come up, e.g. in a container);
        they are logged, summarized in the returned report and shown through the session.
        """
        report: dict[str, Any] = {
            "workspace": None,
            "migration": None,
            "restored": None,
            "recovery_error": None,
        }
        if self._workspace_enabled():
            if not await self._startup_workspace(report):
                # Workspace could not be initialized: nothing is migrated, restored or saved
                # (the old data stays untouched); the user can pick a workspace to recover.
                return report
        try:
            from app.config import BASE_DIR
            from app.migration import migrate_legacy_data

            report["migration"] = await asyncio.to_thread(migrate_legacy_data, BASE_DIR, self.cfg)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Legacy data migration failed; continuing without it")
            report["migration"] = {"error": type(exc).__name__}
        try:
            self.reload_config()
        except Exception:  # noqa: BLE001 - keep the config the process started with
            logger.warning("Config reload after migration failed", exc_info=True)
        report["restored"] = await self._restore_last_session()
        if self._recovery_pending:
            report["recovery_error"] = self.recovery_error
        else:
            if report["restored"] is None:
                # Fresh game: its roster (names, avatars, persona, memory), default budget and default
                # bindings must come from the *active* workspace, which did not exist when it was built.
                try:
                    self._reload_fresh_game_from_workspace()
                except Exception:  # noqa: BLE001 - a roster problem must not stop the application from coming up
                    logger.warning("Could not load the roster of the workspace", exc_info=True)
                fill_missing_from_defaults(self.session, self._load_defaults())
            self.refresh_binding_issues()
        self._autosave_enabled = True
        return report

    async def shutdown(self) -> None:
        """Orderly stop: pause the game, flush the save, then close LLM connections."""
        if self._shutting_down:
            return
        try:
            if self._is_active():
                await self.pause("shutdown")
        except Exception:  # noqa: BLE001
            logger.exception("Pausing the game during shutdown failed")
        try:
            if self._autosave_enabled:
                await self.flush_autosave("shutdown")
        except Exception:  # noqa: BLE001
            logger.exception("Final save during shutdown failed")
        self._shutting_down = True
        aclose = getattr(self.llm, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                logger.exception("Closing the LLM client failed")
        self._release_workspace()


def _safe_error_text(exc: BaseException) -> str:
    """Error text safe to show in the UI: our own errors' messages, otherwise the class name."""
    safe: tuple[type[BaseException], ...] = (ValueError,)
    try:
        from app.storage.errors import StorageError

        safe = (StorageError, ValueError)
    except ImportError:  # storage package unavailable: only ValueErrors are shown
        pass
    if isinstance(exc, safe):
        return str(exc)[:300]
    return type(exc).__name__
