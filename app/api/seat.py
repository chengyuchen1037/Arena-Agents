"""What the person who plays a seat does (v0.13, docs/v0.12-contracts.md §11.7).

``POST /api/seat/speak | pass | respond | ability``.  The actor is ALWAYS the seat that is taken over - there is
no field for another one, and unknown fields are refused - and every action goes through the world's commit gate
exactly like an agent's tool call, so the turn, the length limits, the number of messages, the deadlines and
``STALE`` drafts are the engine's rules.  The engine's answer (accepted or refused, with its code and message)
is the response body, HTTP 200; the HTTP errors are for "nobody is played" / "the game is not running" (409) and
malformed bodies (422).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.api.security import require_trusted_client, verify_api_access
from app.api.takeover import http_error
from app.game.orchestrator import TakeoverError, orchestrator

router = APIRouter(prefix="/api/seat", dependencies=[Depends(verify_api_access), Depends(require_trusted_client)])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpeakRequest(_Body):
    channel_id: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=20000)  # over-long text is cut by the gate, not refused
    based_on_seq: int | None = Field(None, ge=0)  # what the person had seen when the draft began; omitted: now


class PassRequest(_Body):
    channel_id: str = Field(min_length=1, max_length=64)


class RespondRequest(_Body):
    request_id: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=2000)
    reason: str = Field("", max_length=1000)


class AbilityRequest(_Body):
    ability_id: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/speak")
async def seat_speak(body: SpeakRequest) -> dict[str, Any]:
    try:
        return orchestrator.seat_speak(body.channel_id, body.content, body.based_on_seq)
    except TakeoverError as exc:
        raise http_error(exc) from exc


@router.post("/pass")
async def seat_pass(body: PassRequest) -> dict[str, Any]:
    try:
        return orchestrator.seat_pass(body.channel_id)
    except TakeoverError as exc:
        raise http_error(exc) from exc


@router.post("/respond")
async def seat_respond(body: RespondRequest) -> dict[str, Any]:
    try:
        return orchestrator.seat_respond(body.request_id, body.value, body.reason)
    except TakeoverError as exc:
        raise http_error(exc) from exc


@router.post("/ability")
async def seat_ability(body: AbilityRequest) -> dict[str, Any]:
    try:
        return orchestrator.seat_ability(body.ability_id, body.params)
    except TakeoverError as exc:
        raise http_error(exc) from exc
