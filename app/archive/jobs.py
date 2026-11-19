"""Background export / import jobs and the import session (docs/archive.md "任务与进度").

One archive operation runs at a time (an export, an import's upload + check, or an import's apply).  Progress goes to
every browser as an ``archive.progress`` event on the SSE bus (docs/v0.12-contracts.md §11.4):

    {"job_id", "kind": "export" | "import", "phase", "done", "total", "status": "running" | "done" | "error",
     "message"?, "code"?, "result"?}

The blocking work (packing, unpacking, hashing, scrypt, renaming) runs in worker threads; only the bookkeeping, the
events and the orchestrator calls happen on the event loop.  Neither passwords nor keys appear in a job, an event, a log
line or an error message.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.archive import importer, kdf
from app.archive.errors import (
    ArchiveBusyError,
    ArchiveError,
    ArchiveFormatError,
    ArchiveIOError,
    ArchiveKeysMismatchError,
    ArchiveLimitError,
    ArchiveNotFoundError,
    ArchivePasswordError,
    ArchiveStateError,
)
from app.archive.export import ExportOptions, export_archive
from app.archive.format import DEFAULT_LIMITS, EXPORT_NAME_RE, ArchiveLimits
from app.archive.ports import EnginePort, OrchestratorAdapter, SecretsPort, default_secrets

logger = logging.getLogger(__name__)

PHASE_FLUSH = "flush"
PHASE_UPLOAD_CHECK = importer.PHASE_VERIFY
EVENT_TYPE = "archive.progress"
IMPORT_TTL_SECONDS = 2 * 60 * 60
MAX_PASSWORD_ATTEMPTS = 10
MAX_JOBS_KEPT = 20
_UPLOAD_BUFFER_BYTES = 1024 * 1024
_REPORT_INTERVAL = 0.15
_STALE_PART_SECONDS = 24 * 60 * 60
GAME_RUNNING_MESSAGE = "对局正在运行：请先暂停对局，再导入存档。"


def _user_text(exc: BaseException) -> str | None:
    """The text of an exception when it was written for the user (the application's own Chinese sentences, such as
    "a review is running"), else ``None``: anything else (a library error, a path, English) is never shown."""
    text = str(exc)
    return text if 0 < len(text) <= 200 and any("\u4e00" <= ch <= "\u9fff" for ch in text) else None


@dataclass
class ArchiveJob:
    id: str
    kind: str  # "export" | "import"
    status: str = "running"  # "running" | "done" | "error"
    phase: str = "start"
    done: int = 0
    total: int = 0
    message: str = ""
    code: str = ""
    result: dict[str, Any] | None = None  # the whole result (the export's ``path`` included): only for this machine
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    _last_report: float = field(default=0.0, repr=False)

    def public_result(self) -> dict[str, Any] | None:
        """The result without absolute server paths (it goes to every connected browser)."""
        if self.result is None:
            return None
        return {key: value for key, value in self.result.items() if key != "path"}

    def event_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "job_id": self.id,
            "kind": self.kind,
            "phase": self.phase,
            "done": self.done,
            "total": self.total,
            "status": self.status,
        }
        if self.message:
            data["message"] = self.message
        if self.code:
            data["code"] = self.code
        if self.status == "done" and self.result is not None:
            data["result"] = self.public_result()
        return data

    def snapshot(self, *, local: bool) -> dict[str, Any]:
        """``GET /api/archive/jobs/{id}``: the event shape, with the absolute path for a client on this machine."""
        data = self.event_data()
        if self.status == "done" and self.result is not None:
            data["result"] = dict(self.result) if local else self.public_result()
        return data


@dataclass
class ImportSession:
    """An uploaded, checked archive waiting for the user's decision."""

    id: str
    path: Path
    summary: importer.ArchiveSummary
    name: str
    size_bytes: int
    touched: float
    attempts: int = 0

    def public(self, ttl_left: float) -> dict[str, Any]:
        return {
            "import_id": self.id,
            "name": self.name,
            "size_bytes": self.size_bytes,
            "summary": self.summary.to_dict(),
            "expires_in": max(0, int(ttl_left)),
        }


class ArchiveManager:
    """Owns the running job, the finished ones and the pending import."""

    def __init__(
        self,
        *,
        engine: EnginePort | None = None,
        data_dir: Callable[[], Path] | None = None,
        secrets: Callable[[Path], SecretsPort] | None = None,
        limits: ArchiveLimits = DEFAULT_LIMITS,
        import_ttl: float = IMPORT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._data_dir_fn = data_dir
        self._secrets_fn = secrets
        self.limits = limits
        self.import_ttl = import_ttl
        self._clock = clock
        self._busy: str | None = None
        self._jobs: OrderedDict[str, ArchiveJob] = OrderedDict()
        self._session: ImportSession | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._cleaned = False

    # ------------------------------------------------------------------------------------------ plumbing
    @property
    def engine(self) -> EnginePort:
        if self._engine is None:
            self._engine = OrchestratorAdapter()
        return self._engine

    def data_dir(self) -> Path:
        if self._data_dir_fn is not None:
            return Path(self._data_dir_fn())
        from app.storage.paths import get_data_dir

        return get_data_dir()

    def _secrets(self, data_dir: Path) -> SecretsPort:
        return self._secrets_fn(data_dir) if self._secrets_fn is not None else default_secrets(data_dir)

    def exports_dir(self) -> Path:
        return self.data_dir() / "exports"

    def tmp_dir(self) -> Path:
        return self.data_dir() / "tmp"

    def _publish(self, job: ArchiveJob) -> None:
        from app.world.types import UiEvent

        try:
            self.engine.bus.publish(UiEvent(type=EVENT_TYPE, data=job.event_data()))
        except Exception:  # noqa: BLE001 - progress is a courtesy; the job must not die of a bus problem
            logger.debug("Could not publish the archive progress event", exc_info=True)

    def _set_progress(self, job: ArchiveJob, phase: str, done: int, total: int, *, force: bool = False) -> None:
        changed_phase = phase != job.phase
        job.phase, job.done, job.total = phase, int(done), int(total)
        now = time.monotonic()
        if force or changed_phase or done >= total or now - job._last_report >= _REPORT_INTERVAL:
            job._last_report = now
            self._publish(job)

    def _thread_progress(self, job: ArchiveJob, loop: asyncio.AbstractEventLoop) -> Callable[[str, int, int], None]:
        def report(phase: str, done: int, total: int) -> None:
            loop.call_soon_threadsafe(self._set_progress, job, phase, done, total)

        return report

    def _new_job(self, kind: str) -> ArchiveJob:
        job = ArchiveJob(id=uuid.uuid4().hex[:16], kind=kind)
        self._jobs[job.id] = job
        while len(self._jobs) > MAX_JOBS_KEPT:
            self._jobs.popitem(last=False)
        return job

    def _finish(self, job: ArchiveJob, *, result: dict[str, Any] | None = None, error: ArchiveError | None = None) -> None:
        job.finished_at = time.time()
        if error is not None:
            job.status, job.message, job.code = "error", error.message, error.code
        else:
            job.status, job.result = "done", result
        self._publish(job)

    def _fail_internal(self, job: ArchiveJob, what: str) -> None:
        logger.exception("Archive %s failed", what)
        self._finish(job, error=ArchiveError(f"{what}失败：发生了内部错误（详情见服务端日志）。", code="internal", status=500))

    def _require_free(self, token: str) -> None:
        if self._busy is not None:
            raise ArchiveBusyError("已有一个导出或导入任务正在进行，请等它结束。")
        self._busy = token

    def _release(self, token: str) -> None:
        if self._busy == token:
            self._busy = None

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _ensure_idle(self) -> None:
        if self.engine.is_game_active():
            raise ArchiveStateError(GAME_RUNNING_MESSAGE)

    def cleanup_stale(self) -> None:
        """Remove what a previous process left in ``tmp/`` (uploads, unpacked data) and ``exports/`` (unfinished ``.part``)."""
        self._cleaned = True
        tmp = self.tmp_dir()
        keep = {self._session.path.name} if self._session is not None else set()
        if tmp.is_dir():
            for entry in tmp.iterdir():
                if entry.name in keep or not entry.name.startswith("import-"):
                    continue
                try:
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                except OSError:
                    logger.warning("Could not remove the stale temporary item %s", entry.name)
        exports = self.exports_dir()
        if exports.is_dir():
            for entry in exports.glob("arena-*.arenaagents.part"):
                try:
                    if time.time() - entry.stat().st_mtime > _STALE_PART_SECONDS:
                        entry.unlink()
                except OSError:
                    pass

    def _ensure_clean(self) -> None:
        if not self._cleaned:
            try:
                self.cleanup_stale()
            except Exception:  # noqa: BLE001
                logger.debug("Cleaning stale archive files failed", exc_info=True)

    # ----------------------------------------------------------------------------------------- queries
    def get_job(self, job_id: str) -> ArchiveJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise ArchiveNotFoundError("找不到这个任务（可能已经过期，或服务重启过）。")
        return job

    def status(self, *, local: bool) -> dict[str, Any]:
        self._purge_expired()
        active = next((job for job in reversed(self._jobs.values()) if job.status == "running"), None)
        session = self._session
        return {
            "busy": self._busy is not None,
            "job": active.snapshot(local=local) if active is not None else None,
            "import": session.public(self.import_ttl - (self._clock() - session.touched)) if session is not None else None,
            "game_active": self.engine.is_game_active(),
        }

    def export_file(self, name: str) -> Path:
        """The path of a finished export for download; only the names this program writes, only inside ``exports/``."""
        if not isinstance(name, str) or not EXPORT_NAME_RE.fullmatch(name):
            raise ArchiveNotFoundError("找不到这个导出文件。")
        exports = self.exports_dir()
        path = exports / name
        try:
            if path.is_symlink() or not path.is_file() or path.resolve().parent != exports.resolve():
                raise ArchiveNotFoundError("找不到这个导出文件。")
        except OSError:
            raise ArchiveNotFoundError("找不到这个导出文件。") from None
        return path

    # ------------------------------------------------------------------------------------------ export
    async def start_export(self, options: ExportOptions) -> ArchiveJob:
        if options.include_keys:
            kdf.check_password(options.password)  # a weak password is answered at once, not by a failed job
        self._ensure_clean()
        self._require_free("export")
        job = self._new_job("export")
        self._set_progress(job, PHASE_FLUSH, 0, 1, force=True)
        self._spawn(self._run_export(job, options))
        return job

    async def _run_export(self, job: ArchiveJob, options: ExportOptions) -> None:
        loop = asyncio.get_running_loop()
        try:
            warnings: list[str] = []
            try:
                saved = await self.engine.flush_autosave("pre-export")
            except Exception:  # noqa: BLE001 - exporting what is on disk beats not exporting
                logger.warning("Saving the current game before the export failed", exc_info=True)
                saved = False
            if saved is False:
                warnings.append("当前对局没能先保存，存档里可能不含它的最新内容。")
            data_dir = self.data_dir()
            result = await asyncio.to_thread(
                export_archive,
                data_dir,
                options,
                secrets=self._secrets(data_dir) if options.include_keys else None,
                progress=self._thread_progress(job, loop),
            )
            payload = {
                "name": result.name,
                "path": str(result.path),
                "display_path": f"{data_dir.name}/exports/{result.name}",
                "size_bytes": result.size_bytes,
                "archive_id": result.archive_id,
                "created_at": result.created_at,
                "app_version": result.app_version,
                "contents": result.contents,
                "file_count": result.file_count,
                "uncompressed_bytes": result.uncompressed_bytes,
                "warnings": [*warnings, *result.warnings],
            }
            self._finish(job, result=payload)
        except ArchiveError as exc:
            self._finish(job, error=exc)
        except Exception:  # noqa: BLE001
            self._fail_internal(job, "导出")
        finally:
            self._release("export")

    # ------------------------------------------------------------------------------- import: upload
    def _purge_expired(self) -> None:
        session = self._session
        if session is not None and self._clock() - session.touched > self.import_ttl:
            self.discard_import(session.id)

    def _get_session(self, import_id: str) -> ImportSession:
        self._purge_expired()
        session = self._session
        if session is None or session.id != import_id:
            raise ArchiveNotFoundError("这次导入已经过期或被取消，请重新选择存档文件。")
        session.touched = self._clock()
        return session

    def discard_import(self, import_id: str | None = None) -> bool:
        """Forget the pending import and delete its uploaded file; ``False`` when there was none (or another one)."""
        session = self._session
        if session is None or (import_id is not None and session.id != import_id):
            return False
        self._session = None
        with contextlib.suppress(OSError):
            session.path.unlink(missing_ok=True)
        return True

    async def receive_upload(
        self,
        chunks: AsyncIterator[bytes],
        *,
        name: str,
        max_bytes: int,
    ) -> ImportSession:
        """Stream an uploaded archive to ``tmp/``, check it completely and keep it as the pending import."""
        self._ensure_clean()
        self._require_free("upload")
        part: Path | None = None
        try:
            self._ensure_idle()
            self.discard_import()
            tmp = self.tmp_dir()
            tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
            import_id = uuid.uuid4().hex[:16]
            final = tmp / f"import-{import_id}.arenaagents"
            part = tmp / f"import-{import_id}.part"
            size = await self._write_upload(chunks, part, max_bytes)
            await asyncio.to_thread(os.replace, part, final)
            part = None
            job = ArchiveJob(id=import_id, kind="import")
            loop = asyncio.get_running_loop()
            report = self._thread_progress(job, loop)
            try:
                summary = await asyncio.to_thread(importer.inspect, final, limits=self.limits, progress=report)
            except BaseException:
                with contextlib.suppress(OSError):
                    final.unlink(missing_ok=True)
                raise
            session = ImportSession(id=import_id, path=final, summary=summary, name=name, size_bytes=size, touched=self._clock())
            self._session = session
            return session
        finally:
            if part is not None:
                with contextlib.suppress(OSError):
                    part.unlink(missing_ok=True)
            self._release("upload")

    async def _write_upload(self, chunks: AsyncIterator[bytes], part: Path, max_bytes: int) -> int:
        size = 0
        pending = bytearray()
        handle = await asyncio.to_thread(open, part, "wb")
        try:
            async for chunk in chunks:
                size += len(chunk)
                if size > max_bytes:
                    raise ArchiveLimitError(
                        f"存档文件超过了大小上限（{importer.human_bytes(max_bytes)}）。可以用环境变量 ARENA_MAX_ARCHIVE_BYTES 调大。"
                    )
                pending += chunk
                if len(pending) >= _UPLOAD_BUFFER_BYTES:
                    data, pending = bytes(pending), bytearray()
                    await asyncio.to_thread(handle.write, data)
            if pending:
                await asyncio.to_thread(handle.write, bytes(pending))
            await asyncio.to_thread(handle.flush)
            await asyncio.to_thread(os.fsync, handle.fileno())
        except OSError:
            raise ArchiveIOError("保存上传的文件失败：磁盘空间不足或没有写入权限。") from None
        finally:
            await asyncio.to_thread(handle.close)
        if size == 0:
            raise ArchiveFormatError("上传的文件是空的。")
        return size

    # ------------------------------------------------------------------------------- import: apply
    async def start_apply(self, import_id: str, *, password: str | None, skip_keys: bool) -> ArchiveJob:
        """Check the password (when the archive holds keys and they are wanted), then start the import job.

        A wrong password is answered here, synchronously (:class:`ArchivePasswordError`, the session stays so the user can
        try again); everything else happens in the job."""
        session = self._get_session(import_id)
        self._ensure_idle()
        self._require_free("import")
        keys: dict[str, str] | None = None
        try:
            if session.summary.has_secrets and not skip_keys:
                if not password:
                    raise ArchivePasswordError("这份存档含有 API Key：请输入密码，或者选择跳过 API Key。")
                if session.attempts >= MAX_PASSWORD_ATTEMPTS:
                    self.discard_import(session.id)
                    raise ArchiveError("密码错误次数过多，请重新选择存档文件再试。", code="too_many_attempts", status=429)
                try:
                    keys = await asyncio.to_thread(importer.decrypt_archive_keys, session.path, password, limits=self.limits)
                except ArchivePasswordError:
                    session.attempts += 1
                    raise
                except ArchiveKeysMismatchError:
                    raise  # the file is fine, only its keys are not trusted: the user may import without them
                except ArchiveFormatError:
                    self.discard_import(session.id)  # the uploaded file changed or broke: nothing to retry with
                    raise
            job = self._new_job("import")
            self._set_progress(job, importer.PHASE_VERIFY, 0, 1, force=True)
        except BaseException:
            self._release("import")
            raise
        self._spawn(self._run_import(job, session, keys, skip_keys))
        return job

    async def _run_import(self, job: ArchiveJob, session: ImportSession, keys: dict[str, str] | None, skip_keys: bool) -> None:
        loop = asyncio.get_running_loop()
        report = self._thread_progress(job, loop)
        discard = False
        try:
            data_dir = self.data_dir()
            prepared = await asyncio.to_thread(
                importer.prepare, session.path, data_dir, keys=keys, skip_keys=skip_keys, limits=self.limits, progress=report
            )
            self._ensure_idle()
            engine = self.engine
            try:
                await engine.flush_autosave("pre-import")
            except Exception:  # noqa: BLE001 - the data directory is about to be backed up whole anyway
                logger.warning("Saving the current game before the import failed", exc_info=True)

            def reload() -> Any:
                try:
                    return asyncio.run_coroutine_threadsafe(engine.reload_from_data_dir(), loop).result()
                except RuntimeError as exc:
                    text = _user_text(exc)
                    if text is None:
                        raise  # a foreign error: the job ends as an internal one, without its text
                    # "a game is running" / "a review is running": the orchestrator refuses in its own words
                    raise ArchiveStateError(text) from None

            try:
                async with engine.persistence_suspended():
                    result = await asyncio.to_thread(
                        importer.commit,
                        prepared,
                        data_dir,
                        secrets=self._secrets_for_commit(data_dir),
                        reload=reload,
                        ensure_idle=self._ensure_idle,
                        progress=report,
                    )
            except BaseException:
                shutil.rmtree(prepared.work_dir, ignore_errors=True)
                raise
            discard = True
            self._finish(job, result=result.to_dict())
        except ArchiveFormatError as exc:
            discard = True  # the file itself is bad: nothing to retry with
            self._finish(job, error=exc)
        except ArchiveError as exc:
            self._finish(job, error=exc)
        except Exception:  # noqa: BLE001
            self._fail_internal(job, "导入")
        finally:
            if discard:
                self.discard_import(session.id)
            self._release("import")

    def _secrets_for_commit(self, data_dir: Path) -> SecretsPort | None:
        """The key store for the swap; ``None`` lets the importer create it only when it has keys to write or read."""
        if self._secrets_fn is not None:
            return self._secrets_fn(data_dir)
        return None


# ------------------------------------------------------------------------------------------------ singleton
_manager: ArchiveManager | None = None


def get_archive_manager() -> ArchiveManager:
    global _manager
    if _manager is None:
        _manager = ArchiveManager()
    return _manager


def set_archive_manager(manager: ArchiveManager | None) -> None:
    """Install (or, with ``None``, drop) the process-wide manager; tests inject one with a fake engine."""
    global _manager
    _manager = manager


def reset_archive_manager() -> None:
    set_archive_manager(None)
