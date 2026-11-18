"""Model bindings, persistence, restore and shutdown of the running game.

Mixed into :class:`~app.game.orchestrator.GameOrchestrator`.  The stores (provider registry,
settings, game store) are resolved lazily through ``get_*`` helpers and can be injected per instance
(``provider_store`` / ``settings_store`` / ``game_store``), which is how the tests run without
touching the real data directory.

Persistence rules (see docs/storage.md):

* the snapshot is built on the event loop (consistent, no ``await`` in between) and written in a worker
  thread;
* the write is bound to the session object it snapshotted: the ``last_session_id`` pointer is only
  moved if that session is still the live one, so a slow save can never point at a replaced game;
* automatic saves are debounced, a failure is recorded on the session (``autosave_error``) and
  logged — never swallowed and never turned into overwriting the live game;
* if the last game cannot be restored, the (untouched) files are kept and automatic saving is held
  back until the user explicitly starts or creates a game;
* a game saved by an older engine (schema < 5, i.e. v0.12.1 or older) is *not* an error state (D3): it
  stays untouched, a fresh game is created and the user is told why;
* while the data directory is replaced as a whole (an archive import) nothing may be saved:
  :meth:`SessionRuntimeMixin.persistence_suspended`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from app.engine.llm import LLMClient
from app.game.bindings import (
    BindingError,
    compute_binding_issues,
    fill_missing_from_defaults,
    normalized_binding,
)
from app.game.session import MODERATOR_ID, SESSION_SCHEMA_VERSION, BindingIssue, GameSession
from app.providers.models import ModelBinding
from app.storage.datadir import DataDirLockedError, ensure_data_dir, release_data_dir
from app.storage.errors import StorageError
from app.storage.games import GameNotFoundError

logger = logging.getLogger(__name__)

OLD_SAVE_NOTICE = (
    "上次的对局来自旧版本引擎（v0.12.1 及更早），v0.13 不兼容，已保留在原位置、未被改动；"
    "请用 tools/migrate_v0121_to_archive.py 转换后导入。已为你新建了一局。"
)
#: The same fact for a game the user asks to open (history "继续"): no new game is created then.
OLD_SAVE_LOAD_NOTICE = (
    "这局来自旧版本引擎（v0.12.1 及更早），v0.13 不兼容，不能载入；原文件没有被改动。"
    "请用 tools/migrate_v0121_to_archive.py 转换后导入。"
)


class SessionRuntimeMixin:
    # Class-level defaults keep partially initialized instances (tests) valid.
    provider_store: Any = None
    settings_store: Any = None
    game_store: Any = None  # injectable (tests); ``None`` = the data directory's GameStore
    _persistence_holds: int = 0
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

    def _games(self) -> Any:
        if self.game_store is not None:
            return self.game_store
        from app.storage.games import get_game_store

        return get_game_store()

    @property
    def session_store(self) -> Any:
        """The v0.12 name of ``game_store`` (older tests and callers still inject the store under it)."""
        return self.game_store

    @session_store.setter
    def session_store(self, store: Any) -> None:
        self.game_store = store

    def _clear_store_caches(self) -> None:
        """Drop the process-wide store singletons (never the crypto test key)."""
        from app.storage import clear_store_caches

        clear_store_caches()

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
                BindingIssue(actor_id=a, message="Provider 配置无法读取（存储不可用）")
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
        if not self._autosave_enabled or self._shutting_down or self._recovery_pending or self._persistence_holds:
            return  # (while persistence is suspended the request is dropped, not queued)
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

    async def _quiesce_autosave(self) -> None:
        """Cancel the pending debounce timer and wait for a write that is already running."""
        self._autosave_dirty = False
        task = self._autosave_task
        if task is not None and not task.done():
            if not self._persisting:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

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

    @contextlib.asynccontextmanager
    async def persistence_suspended(self) -> AsyncIterator[None]:
        """Hold off every save of the live game while the data directory is replaced as a whole (an archive import).

        On entry the debounce timer is cancelled and a write that is already running is waited for; until the block
        ends the persist lock is held, ``schedule_autosave`` drops its requests (nothing is queued: the game in memory
        is replaced anyway) and ``persist_session`` refuses.  Re-entrant: :meth:`reload_from_data_dir` can be called
        inside the block (and takes the block itself when it is called outside).
        """
        if self._persistence_holds:
            self._persistence_holds += 1
            try:
                yield
            finally:
                self._persistence_holds -= 1
            return
        await self._quiesce_autosave()
        async with self._get_persist_lock():
            self._persistence_holds = 1
            try:
                yield
            finally:
                self._persistence_holds = 0

    async def _persist_body(self, reason: str) -> tuple[GameSession, Exception | None]:
        """Snapshot + write; the caller must hold the persist lock."""
        session = self.session
        generation = self._session_generation
        self._persisting = True
        try:
            payload = self.session_payload()  # consistent snapshot, no await in between
            store = self._games()
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
        """Save the live game to the game store.

        The consistent snapshot is taken on the event loop; the (blocking) file I/O runs in a thread.
        The ``last_session_id`` pointer moves only if the snapshotted session is still the live one.
        Failures are logged and recorded in ``session.autosave_error``; with ``raise_errors`` (explicit
        saves) they are re-raised for the caller to report.  ``_lock_held`` is for callers that already
        own the persist lock.  Nothing is saved while :meth:`persistence_suspended` is in force.
        """
        if self._persistence_holds:
            if raise_errors:
                raise RuntimeError("数据目录正在被替换，暂时不能保存对局。")
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
            payload = await asyncio.to_thread(self._games().load, last_id)
        except GameNotFoundError:
            # The file is gone (deleted by hand, left out of an imported archive): nothing is there to be
            # overwritten, so this is no reason to hold the autosave back.  Start a fresh game and say so.
            logger.info("The last game %s no longer exists; starting a fresh one", last_id)
            self.session.restore_notice = "上次的对局文件已经不存在，已为你新建了一局。"
            return None
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
            "当前为一局空白对局，在您新建/开始之前不会自动保存。"
        )

    # ----------------------------------------------------- other games / a replaced data directory ---
    async def load_game(self, session_id: str) -> GameSession:
        """Make the saved game ``session_id`` the live one, paused (F34 "继续").

        The current game is saved first and a failed save cancels the load (``StorageError``, nothing changed).  Raises
        ``ValueError`` for an invalid id, ``RuntimeError`` while a game runs, ``GameNotFoundError`` (a ``StorageError``)
        for an unknown game and ``SessionLoadError`` (a ``ValueError``) when the file is not a game this version can
        open.  Loading the game that is already live changes nothing.
        """
        from app.game.orchestrator import SessionLoadError
        from app.storage._validation import validate_session_id

        validate_session_id(session_id)
        self._refuse_while_running("载入其他对局")
        if session_id == self.session.session_id:
            return self.session
        payload = await asyncio.to_thread(self._games().load, session_id)
        loaded = self._validate_session_data(payload)
        if loaded.session_id != session_id:
            raise SessionLoadError("对局文件里的 id 与它所在的文件夹不一致，不能载入。")
        if self._autosave_enabled and not self._recovery_pending:
            await self.persist_session("pre-load-game")  # raises when it cannot: the live game stays untouched
        self._refuse_while_running("载入其他对局")  # (the save above was an await: a start may have slipped in)
        return await self._replace_game(loaded, "已载入对局")

    async def reload_from_data_dir(self) -> GameSession:
        """The data directory was replaced as a whole (an archive import): read everything again.

        Store caches are dropped, the LLM client is rebuilt (the providers and their keys may have changed), the
        roster, settings and bindings are read afresh and the game ``last_session_id`` points to is restored, paused
        (a fresh game when there is none).  Nothing of the previous game is saved: whoever replaced the directory
        saved it before.  Refused while a game runs.  Safe to call inside :meth:`persistence_suspended`, which it
        takes itself otherwise, so no save of the old game can land in the new directory.
        """
        self._refuse_while_running("重新载入数据目录")
        if self._review_running:  # type: ignore[attr-defined]
            raise RuntimeError("赛后复盘正在进行，请等它结束后再重新载入数据目录。")
        async with self.persistence_suspended():
            self._autosave_dirty = False
            self._clear_store_caches()
            old_llm = self.llm
            self.llm = self._rebuild_llm()
            self._recovery_pending = False
            self.recovery_error = ""
            try:
                self.reload_config()
            except Exception:  # noqa: BLE001 - keep the config the process started with
                logger.warning("Config reload after replacing the data directory failed", exc_info=True)
            self._reset_game(default_bindings=self._load_defaults())
            restored = await self._restore_last_session()
            if restored is None and not self._recovery_pending:
                fill_missing_from_defaults(self.session, self._load_defaults())
            self.refresh_binding_issues()
        aclose = getattr(old_llm, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                logger.warning("Closing the previous LLM client failed", exc_info=True)
        await self.broadcast_state("session_loaded")
        return self.session

    def _refuse_while_running(self, what: str) -> None:
        if self._is_active():
            raise RuntimeError(f"对局进行中，不能{what}；请先暂停对局。")

    def _rebuild_llm(self) -> Any:
        """A fresh LLM client (its provider cache starts empty); a reasoning effort chosen in the UI is kept."""
        new = LLMClient(self.cfg, provider_store=self.provider_store)
        old = self.llm
        if getattr(old, "_effort_user_set", False):
            new.reasoning_effort = old.reasoning_effort
        return new

    async def startup(self) -> dict[str, Any]:
        """Application start: lock the data directory, reload config, restore the last game (paused).

        Never raises for restore problems (the app must come up, e.g. in a container); they are
        logged, summarized in the returned report and shown through the session.  The one exception
        is a data directory that another process holds (:class:`DataDirLockedError`): two servers
        must not share it, so the application refuses to start.
        """
        report: dict[str, Any] = {"restored": None, "recovery_error": None}
        try:
            await asyncio.to_thread(ensure_data_dir)
        except DataDirLockedError:
            raise
        except StorageError as exc:
            logger.exception("The data directory is not usable")
            self._recovery_pending = True
            self.recovery_error = f"{type(exc).__name__}: {_safe_error_text(exc)}"
            self.session.restore_notice = (
                f"数据目录不可用：{_safe_error_text(exc)}。在修复之前不会保存对局，已有数据未被改动。"
            )
            report["recovery_error"] = self.recovery_error
            return report
        try:
            self.reload_config()
        except Exception:  # noqa: BLE001 - keep the config the process started with
            logger.warning("Config reload at startup failed", exc_info=True)
        # The first game was built in the constructor, before the data directory was locked and readable:
        # take the roster and the defaults from it now (same dealt identities, nothing is saved).
        report["restored"] = await self._restore_last_session()
        if self._recovery_pending:
            report["recovery_error"] = self.recovery_error
        else:
            if report["restored"] is None:
                try:
                    self._reload_fresh_game_from_data_dir()
                except Exception:  # noqa: BLE001 - a roster problem must not stop the application from coming up
                    logger.warning("Could not load the roster from the data directory", exc_info=True)
                fill_missing_from_defaults(self.session, self._load_defaults())
            self.refresh_binding_issues()
        self._autosave_enabled = True
        return report

    async def shutdown(self) -> None:
        """Orderly stop: pause the game, flush the save, close LLM connections, release the data directory."""
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
        try:
            release_data_dir()
        except Exception:  # noqa: BLE001 - never block process exit
            logger.warning("Releasing the data directory failed", exc_info=True)


def _safe_error_text(exc: BaseException) -> str:
    """Error text safe to show in the UI: our own errors' messages, otherwise the class name."""
    if isinstance(exc, (StorageError, ValueError)):
        return str(exc)[:300]
    return type(exc).__name__
