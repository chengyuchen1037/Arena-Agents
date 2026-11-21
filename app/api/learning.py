"""The learning runs over HTTP (v0.14; docs/v0.12-contracts.md §13.7): initializing the bots and reviewing a game.

    POST /api/bots/init          {ids?: [bot id…], rebuild?: bool} -> 202 {job_id, ids, deadline_s}
    POST /api/bots/init:cancel   -> {ok}
    POST /api/game/review        {ids?: [seat id…], learn_style?: {seat id: bool}} -> {started: [seat id…], deadline_s}

Both are writes of a trusted client (a token, or a loopback caller).  The runs go on in the background; how they are doing arrives as
``bot.init.progress`` / ``review.progress`` events and in ``BotsPayload.init_job`` / ``PublicSession.review``.  A request that cannot be served
is ``{"detail": "<中文>", "code": "...", "problems"?: [Problem…]}`` with 409 (``game_active``, ``init_busy``, ``no_binding``, ``NOT_READY``,
``game_not_finished``, ``review_unsupported``), 400 (``nothing_to_do``: nobody needs an initialization), 404 (a bot that does not exist) or 422
(an invalid seat id).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.security import require_trusted_client, verify_api_access
from app.game.orchestrator import orchestrator
from app.learning.jobs import LearningRefused

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access), Depends(require_trusted_client)])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InitRequest(_Body):
    ids: list[str] | None = Field(default=None, max_length=64)
    rebuild: bool = False


class ReviewRequest(_Body):
    ids: list[str] | None = Field(default=None, max_length=64)
    learn_style: dict[str, bool] | None = None


def refusal(exc: LearningRefused) -> JSONResponse:
    """The HTTP form of a refusal of the learning jobs: ``{"detail", "code", "problems"?}``."""
    body: dict[str, Any] = {"detail": str(exc), "code": exc.code}
    if exc.problems:
        body["problems"] = exc.problems
    return JSONResponse(status_code=exc.status, content=body)


@router.post("/bots/init", status_code=202)
async def post_init(req: InitRequest | None = None) -> Any:
    """Start the initialization of the bots in ``ids`` (default: every bot that is not ready; ``rebuild``: all of them)."""
    try:
        return await orchestrator.start_init(req.ids if req else None, bool(req and req.rebuild))
    except LearningRefused as exc:
        return refusal(exc)


@router.post("/bots/init:cancel")
async def post_init_cancel() -> dict[str, Any]:
    """Stop the running initialization: every bot goes back to what it was (its files were never touched)."""
    await orchestrator.cancel_init()
    return {"ok": True}


@router.post("/game/review")
async def post_review(req: ReviewRequest | None = None) -> Any:
    """Review the finished game: every seat that has not been reviewed yet (or the seats in ``ids``) writes down what it learned, in 60 seconds."""
    try:
        return await orchestrator.start_review(req.ids if req else None, req.learn_style if req else None)
    except LearningRefused as exc:
        return refusal(exc)
