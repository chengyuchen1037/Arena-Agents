"""The history of games: list, delete, continue and replay (docs/v0.13-plan.md F34-F36, contract section 11.8).

``/api/games`` is what replaced the old "sessions" window.  Every saved game is one entry of the list; the
game that is live right now is marked ``current`` and can be neither deleted nor loaded again.  Deleting a
game removes the game and its logs only - the roster (names, avatars, persona and memory) is kept apart, so
"deleting a game does not touch an agent's memory" is true by construction.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.security import require_trusted_client, verify_api_access
from app.game.orchestrator import SessionLoadError, orchestrator
from app.game.replay import build_replay
from app.game.session import SESSION_SCHEMA_VERSION
from app.storage.errors import StorageError
from app.storage.games import GameStore, get_game_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])


def _checked_id(session_id: str) -> str:
    """The id of a game, or 400: nothing but ``[A-Za-z0-9_-]`` (and no Windows device name) ever reaches the disk."""
    from app.storage._validation import validate_session_id

    try:
        return validate_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="无效的对局 id。") from exc


def _current_id() -> str:
    return str(getattr(orchestrator.session, "session_id", ""))


def _live_summary() -> dict[str, Any]:
    """The summary of the live game, for the moment it is not on disk yet (a game created a second ago)."""
    summary = GameStore.summarize(
        orchestrator.session_payload(), updated_at=datetime.now(timezone.utc).isoformat()
    )
    summary["session_id"] = _current_id()
    return summary


@router.get("/games")
async def list_games() -> dict[str, Any]:
    """``{"games": [summary, ...], "current": "<id>"}``, newest first; the live game is always in the list."""
    games = await asyncio.to_thread(get_game_store().list_games)
    current = _current_id()
    items = [{**game, "current": game.get("session_id") == current} for game in games]
    if current and not any(item["current"] for item in items):
        items.insert(0, {**_live_summary(), "current": True})
    return {"games": items, "current": current}


@router.delete("/games/{session_id}", dependencies=[Depends(require_trusted_client)])
async def delete_game(session_id: str) -> dict[str, Any]:
    """Delete a saved game and its logs.  The live game cannot be deleted; the roster is never touched."""
    session_id = _checked_id(session_id)
    if session_id == _current_id():
        raise HTTPException(status_code=409, detail="当前对局不能删除：请先新建或载入另一局。")
    store = get_game_store()
    if not await asyncio.to_thread(store.exists, session_id):
        raise HTTPException(status_code=404, detail="对局不存在。")
    await asyncio.to_thread(store.delete, session_id)
    return {"ok": True, "session_id": session_id}


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
    if payload.get("schema_version") != SESSION_SCHEMA_VERSION:
        raise HTTPException(
            status_code=422,
            detail="这局来自旧版本，不能重放：请用 tools/migrate_v0121_to_archive.py 转换后导入。",
        )
    try:
        return await asyncio.to_thread(build_replay, payload)
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("Replay of game %s failed: %s", session_id, type(exc).__name__, exc_info=True)
        raise HTTPException(status_code=422, detail="这局的账本无法重放（存档内容不完整或已损坏）。") from exc
