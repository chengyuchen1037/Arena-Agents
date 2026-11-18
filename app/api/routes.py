from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api import providers as provider_api
from app.api.security import require_trusted_client, verify_api_access
from app.config import APP_VERSION
from app.game.orchestrator import TakeoverError, orchestrator
from app.game.session import GameSession
from app.providers.models import ModelBinding, ProviderError
from app.providers.store import get_provider_store
from app.storage.errors import StorageError

logger = logging.getLogger(__name__)

# All /api/* routes require the API token (when configured) and a same-origin
# Origin header on state-changing requests. /api/health lives in app.main.
router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])


class NewGameRequest(BaseModel):
    randomize_identities: bool | None = None


def _public_session(session: GameSession | None = None) -> dict[str, Any]:
    """PublicSession v3 of the live game (spectator view: no thoughts, no tool calls)."""
    return orchestrator.public_session()


def _safe_binding_issues() -> list[dict[str, str]]:
    try:
        return provider_api.binding_issues()
    except (ProviderError, StorageError):
        return []
    except Exception:  # noqa: BLE001 - /state must stay available
        logger.warning("Could not compute model binding issues", exc_info=True)
        return []


def _legacy_data(request: Request | None) -> dict[str, Any]:
    """v0.13 (F24): is there v0.12.1 data to convert?  Never raises: ``/state`` must stay available."""
    from app.archive.legacy import legacy_data_state

    try:
        return legacy_data_state(request)
    except Exception:  # noqa: BLE001
        logger.warning("Could not check for v0.12.1 data", exc_info=True)
        return {"found": False, "dismissed": False}


@router.get("/state")
async def get_state(request: Request = None) -> dict[str, Any]:  # type: ignore[assignment]
    llm = orchestrator.llm
    return {
        # Application version, independent of the (possibly older) restored session.
        "app_version": APP_VERSION,
        "llm_configured": llm.is_configured(),
        "model": getattr(llm, "default_model", ""),
        "base_url": getattr(llm, "base_url", ""),
        "reasoning_effort": getattr(llm, "reasoning_effort", ""),
        # True when the game was restored from the autosave at startup (paused).
        "restored": bool(
            getattr(orchestrator, "restored_from_save", False)
            or getattr(orchestrator.session, "restored_from_save", False)
        ),
        "binding_issues": _safe_binding_issues(),
        # v0.13 (F24): the notice "old data found, run the conversion script" (path only for a client on this machine)
        "legacy_data": _legacy_data(request),
        "session": _public_session(orchestrator.session),
    }


@router.get("/config/llm")
async def get_llm_config() -> dict[str, Any]:
    """Compatibility view derived from the moderator's model binding.

    Never reveals key material; the full picture lives in /providers and
    /settings/bindings.
    """

    result: dict[str, Any] = {
        "configured": False,
        "api_key_set": False,
        "base_url": "",
        "model": "",
        "protocol": "",
        "provider_id": "",
        "reasoning_effort": "",
        "input_price_per_m": None,
        "output_price_per_m": None,
        "context_window_tokens": 1_000_000,
    }
    binding = provider_api.effective_binding("moderator")
    if binding is not None:
        result["reasoning_effort"] = str(provider_api.serialize_binding(binding).get("reasoning_depth") or "")
        try:
            resolved = binding if isinstance(binding, ModelBinding) else ModelBinding.model_validate(binding)
            provider, model = get_provider_store().resolve(resolved)
        except (ProviderError, StorageError, ValueError):
            provider = model = resolved = None
        if provider is not None and model is not None:
            result.update(
                configured=True,
                api_key_set=bool(provider.api_key),
                base_url=provider.base_url,
                model=model.id,
                protocol=provider.protocol,
                provider_id=provider.id,
                input_price_per_m=model.input_price_per_m,
                output_price_per_m=model.output_price_per_m,
                context_window_tokens=resolved.context_window_tokens,
            )
    try:
        extra = provider_api.bindings_payload()
        result["bindings"] = extra["bindings"]
        result["providers"] = extra["providers"]
    except (ProviderError, StorageError):
        pass
    return result


@router.post("/config/llm", dependencies=[Depends(require_trusted_client)])
async def update_llm_config() -> dict[str, Any]:
    # v0.10: Provider settings live in the encrypted store; this endpoint no
    # longer touches environment variables or .env.
    raise HTTPException(
        status_code=410,
        detail="该接口已移除（v0.10）：请使用 /api/providers 管理 Provider 与模型，使用 /api/settings/bindings 绑定模型。",
    )


@router.get("/events")
async def sse_events(request: Request) -> StreamingResponse:
    """SSE v2: incremental, numbered events with ``Last-Event-ID`` resume (contracts §8.2)."""
    last = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    bus = orchestrator.bus
    # Subscribe and (if needed) build the snapshot with no ``await`` in between: the snapshot and
    # the queued events are exactly consecutive.
    replay, queue = bus.subscribe(last)
    first = _snapshot_message() if replay is None else None

    async def event_generator():
        try:
            if first is not None:
                yield first
            else:
                for event in replay or []:
                    yield event.sse()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if queue.lost_important:
                    # A slow client lost a non-superseding event: drop the backlog and resync.
                    queue.lost_important = False
                    while not queue.empty():
                        queue.get_nowait()
                    yield _snapshot_message()
                    continue
                yield event.sse()
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _snapshot_message() -> str:
    body = {"id": orchestrator.bus.last_id, "type": "snapshot", "data": {"session": orchestrator.public_session()}}
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"id: {orchestrator.bus.last_id}\ndata: {payload}\n\n"


@router.post("/control/new")
async def control_new_game(req: NewGameRequest | None = None) -> dict[str, Any]:
    # None -> orchestrator falls back to the scenario's shuffle_on_new_game.
    randomize = req.randomize_identities if req else None
    try:
        session = await orchestrator.new_game(randomize_identities=randomize)
    except (RuntimeError, ValueError) as exc:  # ConfigReloadError / ScenarioError
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "session": _public_session(session)}


@router.post("/control/shuffle-identities")
async def control_shuffle_identities() -> dict[str, Any]:
    try:
        session = await orchestrator.shuffle_identities()
        return {"ok": True, "session": _public_session(session)}
    except TakeoverError as exc:  # v0.13 (F5): the game has started - never re-dealt again
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _require_complete_bindings() -> None:
    """Refuse to start/resume while any actor lacks a usable model binding."""

    if getattr(orchestrator, "get_model_binding", None) is None:
        return  # engine without binding support (legacy/unit tests)
    if getattr(orchestrator, "demo_mode", False):
        return  # scripted bots call no model
    issues = _safe_binding_issues()
    if issues:
        shown = "；".join(f"{i['actor_id']}：{i['message']}" for i in issues[:6])
        if len(issues) > 6:
            shown += f"…（共 {len(issues)} 项）"
        raise HTTPException(
            status_code=409,
            detail=f"无法开始：需要先为所有角色绑定可用的模型（{shown}）。请在 Provider/模型绑定中重新选择。",
        )


@router.post("/control/start")
async def control_start() -> dict[str, Any]:
    _require_complete_bindings()
    try:
        session = await orchestrator.start_or_resume()
        return {"ok": True, "session": _public_session(session)}
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/control/pause")
async def control_pause() -> dict[str, Any]:
    session = await orchestrator.request_pause()
    return {"ok": True, "session": _public_session(session)}


@router.post("/control/force-pause")
async def control_force_pause() -> dict[str, Any]:
    session = await orchestrator.force_pause()
    return {"ok": True, "session": _public_session(session)}
