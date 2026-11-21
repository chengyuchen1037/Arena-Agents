"""The history of games: list, detail, delete, continue, replay, export and import (docs/v0.13-plan.md F34-F36; v0.14: H40, H42, contract 13.7).

``/api/games`` is what replaced the old "sessions" window.  Every saved game is one entry of the list; the game that is live right now is
marked ``current``.  What the "对局管理" of the settings dialog needs:

    GET    /api/games                    every game (newest first) with ``readable`` / ``size_bytes`` / ``log_bytes`` / ``review`` /
                                         ``participants_brief``
    GET    /api/games/{id}/detail        the expanded row: the seats (bot, identity - only of a finished game -, review), file sizes, who played
    DELETE /api/games/{id}               delete a game and its logs (the bots' memory is never touched: ``note``); deleting the live game
                                         makes a new blank one
    POST   /api/games/{id}/load          continue a saved game
    GET    /api/games/{id}/replay        the spectator view of what happened
    GET    /api/games/{id}/export        the game and its logs as ``game-<id>-YYYYmmdd.arenagame``
    POST   /api/games/import             a ``.arenagame`` as the request body (``?on_conflict=ask|overwrite|copy``)

Deleting a game removes the game and its logs only: what a bot remembers is kept apart (``bots/``) and a review writes it directly, so "deleting a
game does not touch a bot's memory" is true by construction.  Errors are ``{"detail": "<中文>"}``, with a ``code`` where the dialog branches on it.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import secrets
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect

from app.api._common import content_disposition, day_stamp, error_response
from app.api.security import max_archive_bytes, require_trusted_client, verify_api_access
from app.archive import arenagame
from app.archive.errors import ArchiveError, ArchiveExistsError, ArchiveIOError, ArchiveLimitError
from app.archive.jobs import get_archive_manager
from app.archive.zipcheck import human_bytes
from app.game.orchestrator import SessionLoadError, orchestrator
from app.game.replay import build_replay
from app.game.session import SUPPORTED_SCHEMA_VERSIONS
from app.storage.errors import StorageError
from app.storage.games import CONFLICT_POLICIES, DELETE_NOTE, GameNotFoundError, GameStore, get_game_store
from app.storage.paths import get_data_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])

#: How often (and how long) a deletion is tried again: on Windows a log file the process has only just let go of can still be busy for a moment.
_DELETE_ATTEMPTS = 4
_DELETE_RETRY_SECONDS = 0.25  # (times the number of the attempt)


def _checked_id(session_id: str) -> str:
    """The id of a game, or 400: nothing but ``[A-Za-z0-9_-]`` (and no Windows device name) ever reaches the disk."""
    from app.storage._validation import validate_session_id

    try:
        return validate_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="无效的对局 id。") from exc


def _current_id() -> str:
    return str(getattr(orchestrator.session, "session_id", ""))


def _tmp_dir() -> Path:
    from app.storage._atomic import ensure_private_directory

    path = get_data_dir() / "tmp"
    ensure_private_directory(path)
    return path


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("could not delete %s", path)


def _live_summary() -> dict[str, Any]:
    """The summary of the live game, for the moment it is not on disk yet (a game created a second ago)."""
    summary = GameStore.summarize(orchestrator.session_payload(), updated_at=datetime.now(timezone.utc).isoformat())
    summary["session_id"] = _current_id()
    summary["log_bytes"] = get_game_store().log_bytes(summary["session_id"])
    return summary


def _archive_error(exc: ArchiveError) -> JSONResponse:
    """``{"detail", "code"}`` with the error's own HTTP status (``session_id`` too for a game that already exists)."""
    extra = {"session_id": exc.session_id} if isinstance(exc, ArchiveExistsError) else {}
    return error_response(exc.status, exc.message, code=exc.code, **extra)


# ----------------------------------------------------------------------------------------------------------------- list
@router.get("/games")
async def list_games() -> dict[str, Any]:
    """``{"games": [summary, ...], "current": "<id>"}``, newest first; the live game is always in the list.

    A summary has the fields of v0.13 and ``readable`` (this version can open its save format: v5 or v6), ``size_bytes`` (``game.json``),
    ``log_bytes`` (its logs), ``review`` (``{"done": n, "total": 12 | null}`` - ``null`` for a v5 game, which cannot be reviewed) and
    ``participants_brief`` (the names at the table)."""
    games = await asyncio.to_thread(get_game_store().list_games)
    current = _current_id()
    items = [{**game, "current": game.get("session_id") == current} for game in games]
    if current and not any(item["current"] for item in items):
        items.insert(0, {**_live_summary(), "current": True})
    return {"games": items, "current": current}


@router.get("/games/{session_id}/detail")
async def game_detail(session_id: str) -> dict[str, Any]:
    """The expanded row of the history list: ``{"participants": [{"seat", "bot_id", "name", "avatar", "role", "faction", "review"}],
    "files": {"game_bytes", "prev_bytes", "log_bytes"}, "human_spans": [...]}``.  ``role`` / ``faction`` are ``null`` for a game that is not
    finished: nobody learns an identity from the history of a game that is still being played.  The live game is described as it is
    on screen, not as it was last saved."""
    session_id = _checked_id(session_id)
    store = get_game_store()
    if session_id == _current_id():
        payload = orchestrator.session_payload()  # (a snapshot of the screen's own state: taken here, on the event loop)
        return await asyncio.to_thread(store.detail_of, payload, session_id)
    try:
        return await asyncio.to_thread(store.detail, session_id)
    except GameNotFoundError as exc:
        raise HTTPException(status_code=404, detail="对局不存在。") from exc
    except StorageError as exc:
        raise HTTPException(status_code=422, detail=f"无法读取这局的存档：{exc}") from exc


# --------------------------------------------------------------------------------------------------------------- delete
def _forget_last_session(session_id: str) -> None:
    """The settings remember which game to bring back at the next start: it must not be one that is gone (the next start would say that "the
    last game's file no longer exists" about a game the user deleted on purpose)."""
    try:
        from app.storage.settings import get_settings_store

        settings = get_settings_store()
        if settings.get_last_session_id() == session_id:
            settings.set_last_session_id(None)
    except Exception:  # noqa: BLE001 - a pointer that cannot be cleared is only a notice at the next start
        logger.warning("Could not clear the last-session pointer of a deleted game", exc_info=True)


async def _delete_files(store: GameStore, session_id: str) -> None:
    """Delete the game's files; a few tries, because a file the process has only just let go of can be busy for a moment on Windows.
    :class:`StorageError` when it still does not work."""
    for attempt in range(_DELETE_ATTEMPTS):
        try:
            await asyncio.to_thread(store.delete, session_id)
            return
        except StorageError:
            if attempt == _DELETE_ATTEMPTS - 1:
                raise
            gc.collect()  # (a log handle that is only closed when its object is collected)
            await asyncio.sleep(_DELETE_RETRY_SECONDS * (attempt + 1))


@router.delete("/games/{session_id}", dependencies=[Depends(require_trusted_client)])
async def delete_game(session_id: str) -> Any:
    """Delete a game and its logs.  What a bot remembered of it stays (``note``: the dialog shows the same sentence).

    The live game can be deleted unless it is being played (409): a new blank game is made first (:meth:`new_game`: it stops the agents, ends
    a takeover and refuses while a review or an initialization is running) and then the old game is deleted - the answer says so with
    ``"new_game": true`` and the new game's id (``new_session_id``)."""
    session_id = _checked_id(session_id)
    store = get_game_store()
    live = session_id == _current_id()
    if live:
        if orchestrator.session.status == "running":
            raise HTTPException(status_code=409, detail="进行中的对局不能删除，请先暂停。")
    elif not await asyncio.to_thread(store.exists, session_id):
        raise HTTPException(status_code=404, detail="对局不存在。")
    new_id: str | None = None
    if live:
        try:
            await orchestrator.new_game()
        except RuntimeError as exc:  # a review / an initialization is running, or the game has started in the meantime
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        new_id = _current_id()
    try:
        await _delete_files(store, session_id)
    except StorageError:
        suffix = "新的空白对局已经建好，但" if live else ""
        return error_response(
            409,
            f"{suffix}这局的文件暂时无法删除（可能被其他程序占用），请稍后再试。",
            code="in_use",
            new_game=live,
            new_session_id=new_id,
        )
    await asyncio.to_thread(_forget_last_session, session_id)
    return {"ok": True, "session_id": session_id, "note": DELETE_NOTE, "new_game": live, "new_session_id": new_id}


# ------------------------------------------------------------------------------------------------------- load / replay
@router.post("/games/{session_id}/load", dependencies=[Depends(require_trusted_client)])
async def load_game(session_id: str) -> dict[str, Any]:
    """Continue a saved game: the live game is saved first, the chosen one comes back *paused*."""
    session_id = _checked_id(session_id)
    if session_id == _current_id():
        raise HTTPException(status_code=409, detail="这就是当前对局，无需载入。")
    store = get_game_store()
    if not await asyncio.to_thread(store.exists, session_id):
        raise HTTPException(status_code=404, detail="对局不存在。")
    try:
        await orchestrator.load_game(session_id)
    except SessionLoadError as exc:  # a game this version cannot open: the live game is untouched
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:  # the game runs / the live game could not be saved first
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "session": orchestrator.public_session()}


@router.get("/games/{session_id}/replay")
async def replay_game(session_id: str) -> dict[str, Any]:
    """The replay of a saved game (:func:`app.game.replay.build_replay`): the spectator view of what happened.

    The live game cannot be replayed while it runs or while somebody plays a seat: the replay shows everything
    (the god view), which would spoil the game for the person at the table.  Any other saved game can.
    """
    session_id = _checked_id(session_id)
    live = session_id == _current_id()
    if live:
        if orchestrator.session.status == "running" or getattr(orchestrator.session.takeover, "seat", None):
            raise HTTPException(
                status_code=409,
                detail="对局进行中或正在接管一个座位，不能重放当前对局：请先暂停并退出接管。",
            )
        await orchestrator.flush_autosave("pre-replay")  # the stored copy must be what the screen shows
    store = get_game_store()
    if not await asyncio.to_thread(store.exists, session_id):
        raise HTTPException(status_code=404, detail="对局不存在。")
    try:
        payload = await asyncio.to_thread(store.load, session_id)
    except StorageError as exc:
        raise HTTPException(status_code=422, detail=f"无法读取这局的存档：{exc}") from exc
    if payload.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:  # (v0.14: v6, and v5 - a game of v0.13 - replays as before)
        raise HTTPException(
            status_code=422,
            detail="这局来自旧版本，不能重放：请用 tools/migrate_v0121_to_archive.py 转换后导入。",
        )
    try:
        return await asyncio.to_thread(build_replay, payload)
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("Replay of game %s failed: %s", session_id, type(exc).__name__, exc_info=True)
        raise HTTPException(status_code=422, detail="这局的账本无法重放（存档内容不完整或已损坏）。") from exc


# ----------------------------------------------------------------------------------------------------- export / import
@router.get("/games/{session_id}/export")
async def export_game_file(session_id: str) -> Any:
    """The game and its logs as an attachment ``game-<id>-YYYYmmdd.arenagame`` (a zip; ``game.prev.json`` is not part of it).

    Like the replay - the file holds everything, identities included - the live game cannot be exported while it runs or while somebody plays a
    seat (409); a paused or finished live game is saved first, so the file is what the screen shows.  A game this version cannot open (an old save
    format) cannot be exported (422)."""
    session_id = _checked_id(session_id)
    store = get_game_store()
    if session_id == _current_id():
        if orchestrator.session.status == "running" or getattr(orchestrator.session.takeover, "seat", None):
            raise HTTPException(
                status_code=409,
                detail="对局进行中或正在接管一个座位，不能导出当前对局：请先暂停并退出接管。",
            )
        await orchestrator.flush_autosave("pre-export")
    if not await asyncio.to_thread(store.exists, session_id):
        raise HTTPException(status_code=404, detail="对局不存在。")
    target = _tmp_dir() / f"export-game-{secrets.token_hex(6)}{arenagame.FILE_SUFFIX}"
    try:
        await asyncio.to_thread(arenagame.export_game, store, session_id, target)
    except GameNotFoundError as exc:
        _unlink(target)
        raise HTTPException(status_code=404, detail="对局不存在。") from exc
    except ArchiveError as exc:
        _unlink(target)
        return _archive_error(exc)
    except StorageError as exc:
        _unlink(target)
        raise HTTPException(status_code=422, detail=f"无法读取这局的存档：{exc}") from exc
    except BaseException:
        _unlink(target)
        raise
    return FileResponse(
        target,
        media_type="application/octet-stream",
        headers={"Content-Disposition": content_disposition(f"game-{session_id}-{day_stamp()}{arenagame.FILE_SUFFIX}")},
        background=BackgroundTask(_unlink, target),
    )


async def _receive_upload(request: Request, limit: int) -> Path:
    """The request body, streamed to a temporary file in ``data/tmp/`` (never held in memory), at most ``limit`` bytes."""
    message = f"对局文件超过了大小上限（{human_bytes(limit)}）。可以用环境变量 ARENA_MAX_ARCHIVE_BYTES 调大。"
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise ArchiveLimitError(message)
    path = _tmp_dir() / f"import-game-{secrets.token_hex(6)}.upload"
    size = 0
    try:
        with open(path, "wb") as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise ArchiveLimitError(message)
                await asyncio.to_thread(stream.write, chunk)
    except OSError:
        _unlink(path)
        raise ArchiveIOError("保存上传的文件失败：磁盘空间不足或没有写入权限。") from None
    except BaseException:
        _unlink(path)
        raise
    if size == 0:
        _unlink(path)
        raise ArchiveError("没有收到文件内容。", code="invalid_archive", status=422)
    return path


@router.post("/games/import", dependencies=[Depends(require_trusted_client)])
async def import_game_file(
    request: Request,
    name: str = Query("", max_length=300),  # (the file's name as the browser knew it: for display only, never used)
    on_conflict: str = Query("ask"),
) -> Any:
    """Add the game in the ``.arenagame`` file that is the request body to the history: ``{"session_id", "status", "replaced", "copied"}``.

    ``on_conflict`` says what to do with a game that has the same id: ``ask`` (the default) answers 409 ``exists`` - with the id - and changes
    nothing, ``overwrite`` replaces the game and its logs, ``copy`` keeps both (the imported one gets a new id).  The live game is never
    overwritten (409 ``current``).  The file is checked completely (names, sizes, SHA-256, save format 5 / 6, the game itself): 422 when it is
    not acceptable, 413 when it is too big.  Not while an archive is being exported or imported (409 ``busy``)."""
    if on_conflict not in CONFLICT_POLICIES:
        return error_response(400, "on_conflict 只能是 ask、overwrite 或 copy。", code="bad_request")
    store = get_game_store()
    current = _current_id()
    try:
        with get_archive_manager().exclusive("game-import"):
            try:
                upload = await _receive_upload(request, max_archive_bytes())
            except ClientDisconnect:
                return error_response(400, "上传被中断。", code="upload_interrupted")
            workdir = Path(tempfile.mkdtemp(prefix="import-game-", dir=_tmp_dir()))
            try:
                installed = await asyncio.to_thread(
                    arenagame.import_arenagame,
                    store,
                    upload,
                    workdir,
                    on_conflict=on_conflict,
                    protected=(current,) if current else (),
                )
            finally:
                _unlink(upload)
                shutil.rmtree(workdir, ignore_errors=True)
    except ArchiveError as exc:
        return _archive_error(exc)
    return {"session_id": installed.session_id, "status": installed.status, "replaced": installed.replaced, "copied": installed.copied}
