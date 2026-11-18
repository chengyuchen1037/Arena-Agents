"""Taking a seat over and giving it back (v0.13, docs/v0.12-contracts.md §11.7).

``POST /api/takeover {seat}`` and ``POST /api/takeover/release``.  Both change who plays a seat, so they need a
trusted client (a token, or a loopback caller) like every other write.  The rules (only a player seat, one at a
time, only while the game has not started or is paused) live in the orchestrator; a refusal is an HTTP error with a
Chinese ``detail``: 404 unknown seat, 422 not a player seat, 409 the game is in the wrong state.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.security import require_trusted_client, verify_api_access
from app.game.orchestrator import TakeoverError, orchestrator

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])


class TakeoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seat: str = Field(min_length=1, max_length=64)


def http_error(exc: TakeoverError) -> HTTPException:
    """The HTTP form of a refusal of the takeover machinery (the same for the seat endpoints and the re-deal)."""
    return HTTPException(status_code=exc.status, detail=str(exc))


@router.post("/takeover", dependencies=[Depends(require_trusted_client)])
async def post_takeover(body: TakeoverRequest) -> dict[str, Any]:
    try:
        await orchestrator.takeover(body.seat)
    except TakeoverError as exc:
        raise http_error(exc) from exc
    return {"ok": True, "session": orchestrator.public_session()}


@router.post("/takeover/release", dependencies=[Depends(require_trusted_client)])
async def post_takeover_release() -> dict[str, Any]:
    try:
        await orchestrator.release_takeover()
    except TakeoverError as exc:
        raise http_error(exc) from exc
    return {"ok": True, "session": orchestrator.public_session()}
