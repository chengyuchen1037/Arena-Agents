"""The error log (v0.13.1, docs/v0.13.1-plan.md G32-G36, contract docs/v0.12-contracts.md section 12.3).

``GET /api/llm/calls``: every LLM call of the current game and every runtime error, newest last.

Scaffolding: the route is written by the ``observe`` stream.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.security import verify_api_access

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])
