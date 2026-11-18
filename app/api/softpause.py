"""Soft pause (v0.13.1, docs/v0.13.1-plan.md G1-G9, contract docs/v0.12-contracts.md section 12.2).

``POST /api/control/soft-pause`` and ``POST /api/control/soft-resume``: only while a seat is taken over and the game
runs.  The rules live in :class:`app.game.softpause_runtime.SoftPauseRuntimeMixin`; a refusal is an HTTP error with a
Chinese ``detail`` (409 wrong state).

Scaffolding: the routes are written by the ``pause`` stream.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.security import verify_api_access

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])
