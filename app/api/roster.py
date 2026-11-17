"""Roster, memory, budget and review API (v0.12.1; contracts sections 10.4 and 10.8).

Everything that changes something needs a trusted client (loopback, or the API token); the bodies are
small JSON objects except the memory pack, which is a raw zip.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from app.api.security import require_trusted_client, verify_api_access
from app.api.session_io import read_limited_body
from app.game.orchestrator import orchestrator
from app.storage.errors import StorageError
from app.storage.roster import MAX_ZIP_BYTES, RosterError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])


class RosterUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    avatar: dict[str, Any] | None = None
    persona: str | None = None
    memory: str | None = None


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
    """Roster problems are the caller's fault (400); a busy game is a conflict (409); storage is a 503."""
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


@router.get("/roster/memory:export")
async def export_memory() -> Response:
    try:
        data = orchestrator.export_memory()
    except (RosterError, StorageError) as exc:
        raise _fail(exc) from exc
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="arena-memory.zip"', "Cache-Control": "no-store"},
    )


@router.post("/roster/memory:import", dependencies=[Depends(require_trusted_client)])
async def import_memory(request: Request, dry_run: bool = False) -> dict[str, Any]:
    body = await read_limited_body(request, MAX_ZIP_BYTES)
    try:
        return await orchestrator.import_memory(body, dry_run=dry_run)
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
    try:
        return {"seats": await orchestrator.regenerate_avatars()}
    except (RosterError, StorageError, RuntimeError) as exc:
        raise _fail(exc) from exc


@router.put("/roster/{agent_id}", dependencies=[Depends(require_trusted_client)])
async def put_roster_entry(agent_id: str, req: RosterUpdate) -> dict[str, Any]:
    try:
        return await orchestrator.update_roster_entry(
            agent_id, name=req.name, avatar=req.avatar, persona=req.persona, memory=req.memory
        )
    except (RosterError, StorageError, RuntimeError) as exc:
        raise _fail(exc) from exc


@router.post("/roster/{agent_id}/avatar:regenerate", dependencies=[Depends(require_trusted_client)])
async def regenerate_avatar(agent_id: str) -> dict[str, Any]:
    try:
        return (await orchestrator.regenerate_avatars(agent_id))[0]
    except (RosterError, StorageError, RuntimeError) as exc:
        raise _fail(exc) from exc


@router.post("/roster/{agent_id}/refine", dependencies=[Depends(require_trusted_client)])
async def refine(agent_id: str, req: RefineRequest) -> dict[str, Any]:
    """Expand the user's notes with the moderator's model.  Returns a draft; nothing is saved."""
    try:
        return {"draft": await orchestrator.refine_text(agent_id, req.kind, req.text)}
    except (RosterError, StorageError, RuntimeError) as exc:
        raise _fail(exc) from exc
    except Exception as exc:  # noqa: BLE001 - provider / network problems of the model call
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
