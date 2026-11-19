"""The thinking record of an agent (v0.13.1, docs/v0.13.1-plan.md G21-G26, contract docs/v0.12-contracts.md 12.4).

``GET /api/agents/{agent_id}/thoughts``: what the agent thought (never its tool calls), read from the game's trace
log, newest page first.  The thoughts are not pushed anywhere (no event, no snapshot, no replay: D5 still stands for
those); this is the one way to read them, and only for a trusted client (a token, or a loopback caller).  While a seat
is taken over and the game is not over, the other agents' thoughts are closed (409, G25).  The reading is in
:mod:`app.game.observe_runtime`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.security import require_trusted_client, verify_api_access
from app.game.observe_runtime import ObserveError
from app.game.orchestrator import orchestrator

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])


@router.get("/agents/{agent_id}/thoughts", dependencies=[Depends(require_trusted_client)])
async def get_agent_thoughts(
    agent_id: str,
    before: str | None = Query(None, max_length=64, description="the cursor of an earlier answer (older entries only)"),
    limit: int = Query(50, ge=1, le=200, description="entries per page"),
) -> dict[str, Any]:
    """``{"agent_id", "name", "items": [oldest .. newest], "next_before", "has_more"}``; 404 for an id that is
    no agent of this game, 409 while another seat is played and the game is not over."""
    try:
        return await orchestrator.agent_thoughts(agent_id, before, limit)
    except ObserveError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
