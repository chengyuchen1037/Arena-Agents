"""The rules API: scenarios and study materials (v0.14; docs/v0.12-contracts.md §13.7, the "files" table; H39).

    GET    /api/rules                                   {"scenarios": [RulesItem, ...]}
    GET    /api/rules/{sid}                             RulesItem + "markdown" (``?download=1``: ``<sid>-<version>.scenario.md``)
    PUT    /api/rules/{sid}                             {markdown}: replace (or add) a scenario; checked by the scenario loader first
    POST   /api/rules/{sid}:reset                       bring the default scenario back (the one ``config.yml`` names)
    GET    /api/rules/{sid}/materials/{slug}            one study material (``?download=1``: ``<sid>-<slug>.material.md``)
    PUT    /api/rules/{sid}/materials/{slug}            {markdown, title?}: add or replace one
    DELETE /api/rules/{sid}/materials/{slug}

``RulesItem`` = ``{"id", "version", "title", "origin", "imported_at", "sha256", "chars", "materials": [{"slug", "title", "chars",
"sha256"}], "problem"}`` (``problem`` - an addition - is the loader's explanation when the file in the data directory cannot be
loaded, else ``null``).  A change of a scenario or a material makes every bot's initialization ``stale`` (the hashes changed); the
orchestrator is told (``orchestrator.sources_changed("scenario" | "materials", sid)``, when it has such a method) and rebuilds an idle
game.  Errors: ``{"detail", "code", "problems"}`` - 400 for a text that cannot be saved (the scenario's ``detail`` is the loader's own
Chinese explanation), 404 for an unknown scenario or material, 413 for a text that is too large.
"""

from __future__ import annotations

import asyncio
import logging
from functools import wraps
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api._common import LazyOrchestrator, attachment, error_response, maybe_await, problems_detail, safe_filename
from app.api.security import require_trusted_client, verify_api_access
from app.storage.rules import RulesError, RulesStore, get_rules_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])
TRUSTED = [Depends(require_trusted_client)]

orchestrator: Any = LazyOrchestrator()


class ScenarioUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown: str = Field(max_length=2_000_000)


class MaterialUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown: str = Field(max_length=1_000_000)
    title: str | None = Field(default=None, max_length=400)


def _guarded(func: Any) -> Any:
    """A refusal of the store becomes ``{"detail", "code", "problems"}`` with its own HTTP status."""

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except RulesError as exc:
            return error_response(exc.status, problems_detail("", exc.problems), code=exc.code, problems=exc.problems)

    return wrapper


async def _notify(kind: str, scenario_id: str) -> None:
    """Tell the orchestrator that a scenario / its materials changed (see :mod:`app.api.contract`)."""
    try:
        hook = getattr(orchestrator, "sources_changed", None)
    except Exception:  # noqa: BLE001 - no orchestrator at all (a store test)
        return
    if callable(hook):
        try:
            await maybe_await(hook(kind, scenario_id))
        except Exception:  # noqa: BLE001
            logger.warning("sources_changed failed after a change of the %s of %s", kind, scenario_id, exc_info=True)


def _scenario_download_name(item: dict[str, Any]) -> str:
    version = safe_filename(item.get("version"), "")
    return f"{safe_filename(item['id'], 'scenario')}-{version}.scenario.md" if version else f"{safe_filename(item['id'], 'scenario')}.scenario.md"


def _store() -> RulesStore:
    return get_rules_store()


def _item_with_text(store: RulesStore, sid: str) -> dict[str, Any]:
    return {**store.item(sid), "markdown": store.scenario_text(sid)}


# ---------------------------------------------------------------------------------------------------- scenarios
@router.get("/rules")
@_guarded
async def list_rules() -> Any:
    return {"scenarios": await asyncio.to_thread(_store().items)}


@router.get("/rules/{sid}")
@_guarded
async def get_rules(sid: str, download: bool = Query(default=False)) -> Any:
    store = _store()
    data = await asyncio.to_thread(_item_with_text, store, sid)
    if download:
        return attachment(data["markdown"], _scenario_download_name(data))
    return data


@router.put("/rules/{sid}", dependencies=TRUSTED)
@_guarded
async def put_rules(sid: str, req: ScenarioUpdate) -> Any:
    """Replace the scenario ``sid`` (or add one).  The scenario loader checks it first - the same check the engine makes - and the file's
    own ``id`` has to be ``sid``: otherwise 400 with the loader's explanation and nothing written.  The materials stay."""
    store = _store()
    await asyncio.to_thread(store.set_scenario, sid, req.markdown)
    await _notify("scenario", sid)
    return await asyncio.to_thread(store.item, sid)


@router.post("/rules/{sid}:reset", dependencies=TRUSTED)
@_guarded
async def reset_rules(sid: str) -> Any:
    """Bring the default scenario back; only the default scenario has a default (400 ``no_default`` for any other).  The materials stay."""
    store = _store()
    await asyncio.to_thread(store.reset, sid)
    await _notify("scenario", sid)
    return await asyncio.to_thread(store.item, sid)


# ---------------------------------------------------------------------------------------------------- materials
@router.get("/rules/{sid}/materials/{slug}")
@_guarded
async def get_material(sid: str, slug: str, download: bool = Query(default=False)) -> Any:
    store = _store()
    data = await asyncio.to_thread(store.material, sid, slug)
    if download:
        return attachment(data["markdown"], f"{safe_filename(sid, 'scenario')}-{safe_filename(slug, 'material')}.material.md")
    return data


@router.put("/rules/{sid}/materials/{slug}", dependencies=TRUSTED)
@_guarded
async def put_material(sid: str, slug: str, req: MaterialUpdate) -> Any:
    """Add or replace one study material (the answer is the scenario's ``RulesItem``, whose ``materials`` has the new list).  The slug is
    the file's name (lower-case letters, digits and hyphens, at most 32), the optional ``title`` is what the list shows."""
    store = _store()
    await asyncio.to_thread(store.set_material, sid, slug, req.markdown, req.title)
    await _notify("materials", sid)
    return await asyncio.to_thread(store.item, sid)


@router.delete("/rules/{sid}/materials/{slug}", dependencies=TRUSTED)
@_guarded
async def delete_material(sid: str, slug: str) -> Any:
    store = _store()
    await asyncio.to_thread(store.delete_material, sid, slug)
    await _notify("materials", sid)
    return await asyncio.to_thread(store.item, sid)


__all__ = ["router"]
