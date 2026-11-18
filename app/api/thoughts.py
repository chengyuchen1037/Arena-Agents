"""The thinking record of an agent (v0.13.1, docs/v0.13.1-plan.md G21-G26, contract docs/v0.12-contracts.md 12.4).

``GET /api/agents/{agent_id}/thoughts``: what the agent thought (never its tool calls), read from the game's trace
log, newest page first.

Scaffolding: the route is written by the ``observe`` stream.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.security import verify_api_access

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])
