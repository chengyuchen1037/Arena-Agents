"""Shared helpers for importing / loading session payloads safely."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from pydantic import ValidationError

from app.engine.scenario import ScenarioDocument
from app.engine.state import GameSession


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
    try:
        GameSession.model_validate(payload)
        snapshot = payload.get("scenario_snapshot")
        if snapshot:
            ScenarioDocument.from_snapshot(snapshot)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)[:5]
        )
        raise HTTPException(status_code=400, detail=f"Invalid session data: {details}") from exc
    except (ValueError, TypeError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid session data: {exc}") from exc
    return payload
