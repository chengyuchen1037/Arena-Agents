"""Archive API (v0.13, decisions F24, F26-F32): export / import ``.arenaagents`` files and the old-data notice.

    POST   /api/archive/export                     {include_keys, password?, include_logs} -> {job_id}
    GET    /api/archive/jobs/{job_id}              progress / result of one job
    GET    /api/archive/status                     is anything running, is an import pending
    GET    /api/archive/exports/{name}             download a finished export (attachment)
    POST   /api/archive/import                     the file as the request body -> {import_id, summary, ...}
    POST   /api/archive/import/{import_id}/apply   {password?, skip_keys} -> {job_id}
    DELETE /api/archive/import/{import_id}         forget the pending import
    POST   /api/notices/legacy-data:dismiss        do not show the "old data found" notice again

Every route needs the API token when one is configured; everything that changes something also needs a trusted client
(loopback, or any client with the token).  Errors are ``{"detail": "...", "code": "..."}`` with the statuses of
docs/v0.12-contracts.md §11.5.  Passwords and keys are never echoed, logged or stored here.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import ClientDisconnect

from app.archive import legacy
from app.archive.errors import ArchiveError, ArchiveLimitError, safe_text
from app.archive.export import ExportOptions
from app.archive.jobs import ArchiveManager, get_archive_manager
from app.api.security import is_local_request, max_archive_bytes, require_trusted_client, verify_api_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])

MAX_PASSWORD_REQUEST_CHARS = 1024
_FILE_NAME_ILLEGAL = re.compile(r"[\\/:*?\"<>|\x00-\x1f\x7f]")


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_keys: bool = False
    password: str | None = Field(default=None, max_length=MAX_PASSWORD_REQUEST_CHARS, repr=False)
    include_logs: bool = True


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str | None = Field(default=None, max_length=MAX_PASSWORD_REQUEST_CHARS, repr=False)
    skip_keys: bool = False


def _manager() -> ArchiveManager:
    return get_archive_manager()


def _error_response(exc: ArchiveError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.message, "code": exc.code})


def _guarded(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Turn an :class:`ArchiveError` into ``{"detail", "code"}`` with the error's own HTTP status."""

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except ArchiveError as exc:
            return _error_response(exc)

    return wrapper


def _display_name(raw: str) -> str:
    """The uploaded file's name for display only: no directories, no control characters, short."""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = _FILE_NAME_ILLEGAL.sub("_", name).strip()
    return safe_text(name, 120) or "archive.arenaagents"


# ------------------------------------------------------------------------------------------------ export
@router.post("/archive/export", status_code=202, dependencies=[Depends(require_trusted_client)])
@_guarded
async def start_export(req: ExportRequest) -> dict[str, Any]:
    """Pack the data directory in the background (the running game is saved first); progress arrives as
    ``archive.progress`` events and at ``GET /api/archive/jobs/{job_id}``."""
    job = await _manager().start_export(
        ExportOptions(include_keys=req.include_keys, password=req.password, include_logs=req.include_logs)
    )
    return {"job_id": job.id, "kind": job.kind}


@router.get("/archive/status")
@_guarded
async def archive_status(request: Request) -> dict[str, Any]:
    return _manager().status(local=is_local_request(request))


@router.get("/archive/jobs/{job_id}")
@_guarded
async def get_job(job_id: str, request: Request) -> dict[str, Any]:
    return _manager().get_job(job_id).snapshot(local=is_local_request(request))


@router.get("/archive/exports/{name}")
@_guarded
async def download_export(name: str) -> FileResponse:
    """The file of a finished export as an attachment; only ``arena-<time>.arenaagents`` inside ``data/exports/``."""
    path = _manager().export_file(name)
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


# ------------------------------------------------------------------------------------------------ import
@router.post("/archive/import", dependencies=[Depends(require_trusted_client)])
@_guarded
async def upload_archive(request: Request, name: str = Query("", max_length=300)) -> Any:
    """The archive file is the request body.  It is streamed to ``data/tmp/`` (never read into memory), checked completely
    and kept until it is applied, cancelled or expires; the answer describes it."""
    limit = max_archive_bytes()
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise ArchiveLimitError("存档文件超过了大小上限。可以用环境变量 ARENA_MAX_ARCHIVE_BYTES 调大。")
    manager = _manager()
    try:
        session = await manager.receive_upload(request.stream(), name=_display_name(name), max_bytes=limit)
    except ClientDisconnect:
        return JSONResponse(status_code=400, content={"detail": "上传被中断。", "code": "upload_interrupted"})
    return session.public(manager.import_ttl)


@router.post("/archive/import/{import_id}/apply", status_code=202, dependencies=[Depends(require_trusted_client)])
@_guarded
async def apply_import(import_id: str, req: ApplyRequest) -> dict[str, Any]:
    """Replace the current data with the uploaded archive (the old data is backed up first).  With keys in the archive
    the password is checked *here* (422 ``wrong_password``: try again) unless ``skip_keys``."""
    job = await _manager().start_apply(import_id, password=req.password, skip_keys=req.skip_keys)
    return {"job_id": job.id, "kind": job.kind}


@router.delete("/archive/import/{import_id}", dependencies=[Depends(require_trusted_client)])
@_guarded
async def cancel_import(import_id: str) -> dict[str, Any]:
    return {"ok": True, "discarded": _manager().discard_import(import_id)}


# --------------------------------------------------------------------------------------- legacy notice
@router.post("/notices/legacy-data:dismiss", dependencies=[Depends(require_trusted_client)])
async def dismiss_legacy_data() -> dict[str, bool]:
    legacy.dismiss()
    return {"ok": True}
