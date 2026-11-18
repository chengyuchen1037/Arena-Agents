"""Price source API (v0.12.1, E12): where the model prices come from, refreshing them, matching a model."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app.api import providers as provider_api
from app.api.security import require_trusted_client, verify_api_access
from app.providers.pricing import get_price_store, validate_source_url

router = APIRouter(prefix="/api/pricing", dependencies=[Depends(verify_api_access)])


class PriceSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = ""  # empty = the default (LiteLLM) source


@router.get("/source")
async def get_source() -> dict[str, Any]:
    return get_price_store().status()


@router.put("/source", dependencies=[Depends(require_trusted_client)])
async def put_source(req: PriceSourceRequest) -> dict[str, Any]:
    try:
        validate_source_url(req.url) if req.url.strip() else None
        return get_price_store().set_url(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/refresh", dependencies=[Depends(require_trusted_client)])
async def refresh_source() -> dict[str, Any]:
    """Download the table now.  A failed download is *not* an HTTP error: the old table stays and the
    ``error`` field of the status explains what went wrong."""
    return await get_price_store().refresh()


@router.get("/match")
async def match_model(provider_id: str, model_id: str) -> dict[str, Any]:
    provider = provider_api._get_provider(provider_id)  # 404 / 400 like every other provider endpoint
    entry = get_price_store().match(provider, model_id)
    if entry is None:
        return {"matched": False, "key": None}
    return {"matched": True, **entry.suggestion()}
