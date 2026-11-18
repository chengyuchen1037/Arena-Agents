"""Shared helpers for importing / loading session payloads safely."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from pydantic import ValidationError

from app.game.session import SESSION_SCHEMA_VERSION, GameSession
from app.game.session_runtime import OLD_SAVE_NOTICE
from app.world.spec import ScenarioSpec


async def read_limited_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise HTTPException(status_code=413, detail=f"Import payload exceeds the {limit} byte limit.")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header.") from None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail=f"Import payload exceeds the {limit} byte limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def validate_import_payload(payload: Any) -> dict[str, Any]:
    """Fully validate an imported session without touching the live game."""

    if isinstance(payload, dict) and isinstance(payload.get("session"), dict):
        payload = payload["session"]
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Import payload must be a JSON object.")
    if payload.get("schema_version") != SESSION_SCHEMA_VERSION:
        # D3: saves written by an older engine are not compatible.
        raise HTTPException(status_code=400, detail=OLD_SAVE_NOTICE)
    try:
        loaded = GameSession.model_validate(payload)
        ScenarioSpec.model_validate(loaded.scenario_spec)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)[:5]
        )
        raise HTTPException(status_code=400, detail=f"Invalid session data: {details}") from exc
    except (ValueError, TypeError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid session data: {exc}") from exc
    return payload
