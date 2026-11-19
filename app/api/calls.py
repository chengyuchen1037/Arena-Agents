"""The error log (v0.13.1, docs/v0.13.1-plan.md G32-G36, contract docs/v0.12-contracts.md section 12.3).

``GET /api/llm/calls``: every LLM call of the current game and every runtime error, newest last.  Only metadata - which
actor, which model, how it ended, how long, how many tokens, the scrubbed text of an error; never a prompt, an answer, a
header or a key - and still only for a trusted client (a token, or a loopback caller).  It can be read while a seat is
taken over.  The log itself lives in :mod:`app.game.observe_runtime`; the ``llm.call`` event keeps an open panel current.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api.security import require_trusted_client, verify_api_access
from app.game.orchestrator import orchestrator

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])


@router.get("/llm/calls", dependencies=[Depends(require_trusted_client)])
async def get_llm_calls(
    after: int = Query(0, ge=0, description="only the lines with an id above this one"),
    limit: int = Query(500, ge=1, le=2000, description="at most this many lines (the newest ones)"),
    errors_only: bool = Query(False, description="only retries, timeouts and errors"),
) -> dict[str, Any]:
    """``{"calls": [...], "last_id", "total", "errors", "retries"}``: the lines (oldest first) with an id above
    ``after`` - the newest ``limit`` of them when there are more - of the live game."""
    return orchestrator.call_log_payload(after=after, limit=limit, errors_only=errors_only)
