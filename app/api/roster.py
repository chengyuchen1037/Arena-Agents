"""Roster, memory, budget and review API (v0.12.1; contracts sections 10.4 and 10.8).

Everything that changes something needs a trusted client (loopback, or the API token); the bodies are
small JSON objects.  (The memory pack - a zip of the persona / memory files - is gone in v0.13: the only way to
take the memory somewhere else is the archive export.)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError

from app.api.security import require_trusted_client, verify_api_access
from app.game.orchestrator import orchestrator
from app.storage.errors import StorageError
from app.storage.roster import RosterError, RosterInputError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])


class MemoryPartsUpdate(BaseModel):
    """The parts of a memory a ``PUT`` changes (contract 12.5); what is left out stays as it is."""

    model_config = ConfigDict(extra="forbid")

    impressions: dict[str, str] | None = None  # another seat's id -> impression ("" = none)
    experience: list[str] | None = None  # the whole list of lessons
    legacy: str | None = None  # the old text waiting to be tidied up ("" deletes it)
    record: str | None = None  # accepted so that a client can send back what it read; the record is the engine's: ignored


class RosterUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    avatar: dict[str, Any] | None = None
    persona: str | None = None
    memory: str | None = None
    memory_parts: MemoryPartsUpdate | None = None


class RefineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    text: str


class ClearRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_persona: bool = False


class MemoryEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str] | None = None


def _fail(exc: Exception) -> HTTPException:
    """Roster problems are the caller's fault (400; 422 when the request itself makes no sense: both ``memory`` and
    ``memory_parts``, an impression of oneself or of a seat that does not exist); a busy game is a conflict (409);
    storage is a 503."""
    if isinstance(exc, RosterInputError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, RosterError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, StorageError):
        logger.error("Roster storage failed: %s", type(exc).__name__)
        return HTTPException(status_code=503, detail="名册文件无法读写，请检查数据目录。")
    if isinstance(exc, RuntimeError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc) or "请求无效。")


# ---------------------------------------------------------------------------------------- roster
@router.get("/roster")
async def get_roster() -> dict[str, Any]:
    try:
        return orchestrator.roster_payload()
    except (RosterError, StorageError) as exc:
        raise _fail(exc) from exc


@router.put("/roster/memory-enabled", dependencies=[Depends(require_trusted_client)])
async def put_memory_enabled(req: MemoryEnabledRequest) -> dict[str, Any]:
    try:
        return await orchestrator.set_memory_enabled(req.enabled)
    except (RosterError, StorageError, RuntimeError) as exc:
        raise _fail(exc) from exc


@router.post("/roster/memory:clear", dependencies=[Depends(require_trusted_client)])
async def clear_memory(req: ClearRequest | None = None) -> dict[str, Any]:
    try:
        return await orchestrator.clear_memory(include_persona=bool(req and req.include_persona))
    except (RosterError, StorageError, RuntimeError) as exc:
        raise _fail(exc) from exc


@router.post("/roster/avatars:regenerate", dependencies=[Depends(require_trusted_client)])
async def regenerate_all_avatars() -> dict[str, Any]:
    """New avatars for all twelve seats *and the moderator* -> ``{"seats": [...], "moderator": {id,name,avatar}}``."""
    try:
        return await orchestrator.regenerate_avatars()
    except (RosterError, StorageError, RuntimeError) as exc:
        raise _fail(exc) from exc


@router.put("/roster/{agent_id}", dependencies=[Depends(require_trusted_client)])
async def put_roster_entry(agent_id: str, req: RosterUpdate) -> dict[str, Any]:
    """``persona`` too long -> 400; ``memory`` (the whole text) or ``memory_parts`` (only the parts to change), not both
    (422); a part over its limit -> 400 naming it; an impression of oneself / of an unknown seat -> 422."""
    parts = req.memory_parts.model_dump(exclude_none=True) if req.memory_parts is not None else None
    if parts is not None:
        parts.pop("record", None)
    try:
        return await orchestrator.update_roster_entry(
            agent_id, name=req.name, avatar=req.avatar, persona=req.persona, memory=req.memory, memory_parts=parts
        )
    except (RosterError, StorageError, RuntimeError) as exc:
        raise _fail(exc) from exc


@router.post("/roster/{agent_id}/avatar:regenerate", dependencies=[Depends(require_trusted_client)])
async def regenerate_avatar(agent_id: str) -> dict[str, Any]:
    """New avatar for one seat, or for the moderator (``agent_id = "moderator"``) -> that entry."""
    try:
        return await orchestrator.regenerate_avatars(agent_id)
    except (RosterError, StorageError, RuntimeError) as exc:
        raise _fail(exc) from exc


@router.post("/roster/{agent_id}/refine", dependencies=[Depends(require_trusted_client)])
async def refine(agent_id: str, req: RefineRequest) -> dict[str, Any]:
    """Turn the user's notes into a persona (the fixed format) or a memory (impressions and lessons) with the moderator's
    model.  Returns ``{"draft": …}`` (``memory`` also ``"memory_parts"``); nothing is saved."""
    try:
        return await orchestrator.refine_entry(agent_id, req.kind, req.text)
    except (RosterError, StorageError) as exc:  # our own refusals; anything else comes from the model call
        raise _fail(exc) from exc
    except Exception as exc:  # noqa: BLE001 - provider / network problems of the model call: never echoed
        logger.warning("Refine failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="调用主持人模型失败，请检查模型绑定和网络后重试。") from exc


# ---------------------------------------------------------------------------------------- review
@router.post("/game/review", dependencies=[Depends(require_trusted_client)])
async def review(req: ReviewRequest | None = None) -> dict[str, Any]:
    try:
        return {"started": await orchestrator.start_review(req.ids if req else None)}
    except (RosterError, StorageError, RuntimeError) as exc:
        raise _fail(exc) from exc


# ---------------------------------------------------------------------------------------- budget
@router.get("/budget")
async def get_budget() -> dict[str, Any]:
    return orchestrator.budget_payload()


@router.put("/budget", dependencies=[Depends(require_trusted_client)])
async def put_budget(request: Request) -> dict[str, Any]:
    try:
        raw = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象。") from None
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象。")
    try:
        return await orchestrator.set_budget(raw)
    except ValidationError as exc:
        detail = "；".join(
            f"{'.'.join(str(p) for p in err['loc'])}：{err['msg']}" for err in exc.errors(include_input=False, include_url=False)[:4]
        )
        raise HTTPException(status_code=422, detail=f"预算设置无效：{detail}") from exc
