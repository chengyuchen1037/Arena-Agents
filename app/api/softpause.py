"""Soft pause (v0.13.1, docs/v0.13.1-plan.md G1-G9, contract docs/v0.12-contracts.md section 12.2).

``POST /api/control/soft-pause`` and ``POST /api/control/soft-resume``: only while a seat is taken over and the game
runs.  The rules live in :class:`app.game.softpause_runtime.SoftPauseRuntimeMixin`; a refusal is an HTTP error with a
Chinese ``detail`` (409 wrong state).  Both change what the running game does, so they need a trusted client (a token,
or a loopback caller) like every other write.  Success is ``{"ok": true, "session": PublicSession}``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.security import require_trusted_client, verify_api_access
from app.game.orchestrator import orchestrator
from app.game.softpause_runtime import SoftPauseError

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])


def http_error(exc: SoftPauseError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


@router.post("/control/soft-pause", dependencies=[Depends(require_trusted_client)])
async def post_soft_pause() -> dict[str, Any]:
    """Let the work in flight finish, then hold every agent (the clock stops now).  Already soft-paused: 200, nothing
    changes.  409: the game does not run / nobody plays a seat."""
    try:
        await orchestrator.soft_pause()
    except SoftPauseError as exc:
        raise http_error(exc) from exc
    return {"ok": True, "session": orchestrator.public_session()}


@router.post("/control/soft-resume", dependencies=[Depends(require_trusted_client)])
async def post_soft_resume() -> dict[str, Any]:
    """Let the agents go on.  409: there is no soft pause."""
    try:
        await orchestrator.soft_resume()
    except SoftPauseError as exc:
        raise http_error(exc) from exc
    return {"ok": True, "session": orchestrator.public_session()}
