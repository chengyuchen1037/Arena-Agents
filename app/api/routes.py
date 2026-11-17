from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.api import providers as provider_api
from app.api import workspaces as workspace_api
from app.api.security import (
    is_local_request,
    max_import_bytes,
    require_trusted_client,
    verify_api_access,
)
from app.api.session_io import read_limited_body, validate_import_payload
from app.config import APP_VERSION
from app.game.orchestrator import SessionLoadError, orchestrator
from app.game.session import SESSION_ID_PATTERN, GameSession
from app.providers.models import ModelBinding, ProviderError
from app.providers.store import get_provider_store
from app.storage.errors import StorageError
from app.storage.sessions import get_session_store

logger = logging.getLogger(__name__)

# All /api/* routes require the API token (when configured) and a same-origin
# Origin header on state-changing requests. /api/health lives in app.main.
router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])


class NewGameRequest(BaseModel):
    randomize_identities: bool | None = None


class ExecutionDepthUpdateRequest(BaseModel):
    depth: str | None = None
    execution_depth: str | None = None


def _public_session(session: GameSession | None = None) -> dict[str, Any]:
    """PublicSession v2 of the live game (spectator view: no thoughts, no tool calls)."""
    return orchestrator.public_session()


def _safe_binding_issues() -> list[dict[str, str]]:
    try:
        return provider_api.binding_issues()
    except (ProviderError, StorageError):
        return []
    except Exception:  # noqa: BLE001 - /state must stay available
        logger.warning("Could not compute model binding issues", exc_info=True)
        return []


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
        # v0.11: which workspace is active. Remote readers only get name/id (no path).
        "workspace": _workspace_summary(request),
        "session": _public_session(orchestrator.session),
    }


def _workspace_summary(request: Request | None) -> dict[str, Any]:
    try:
        include_path = True if request is None else is_local_request(request)
        return workspace_api.workspace_state(include_path=include_path, engine=orchestrator)
    except Exception:  # noqa: BLE001 - /state must stay available even if the workspace breaks
        logger.warning("Could not read the active workspace", exc_info=True)
        return {"enabled": False}


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
        "execution_depth": getattr(orchestrator.session, "execution_depth", "medium"),
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


@router.post("/session/execution-depth")
async def set_execution_depth(req: ExecutionDepthUpdateRequest) -> dict[str, Any]:
    depth = (req.execution_depth or req.depth or "").strip().lower()
    if depth in ("low", "medium", "high", "xhigh"):
        orchestrator.set_execution_depth(depth)
        await orchestrator.broadcast_state("execution_depth_updated")
        return {"ok": True, "execution_depth": orchestrator.session.execution_depth}
    raise HTTPException(status_code=400, detail="Invalid execution depth. Must be low, medium, high, or xhigh.")


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
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _require_complete_bindings() -> None:
    """Refuse to start/resume while any actor lacks a usable model binding."""

    if getattr(orchestrator, "get_model_binding", None) is None:
        return  # engine without binding support (legacy/unit tests)
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


@router.post("/session/save")
async def save_session() -> JSONResponse:
    session_id = str(orchestrator.session.session_id)
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        # Should be impossible (validated by GameSession); defence in depth.
        raise HTTPException(status_code=400, detail="Invalid session_id; refusing to save.")
    # The durable copy goes to the encrypted external SessionStore (written by
    # the orchestrator). Nothing is written next to the source code any more.
    try:
        await orchestrator.persist_session("manual")
    except StorageError as exc:
        logger.error("Manual save failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail=f"保存失败：{exc}") from exc
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"arena_session_{ts}_{session_id}.json"
    # `session` is the user's own full save for an explicit browser download;
    # server paths are never returned.
    return JSONResponse(
        content={
            "ok": True,
            "filename": filename,
            "session": orchestrator.session_payload(),
        }
    )


def _unique_import_id(payload: dict[str, Any]) -> str | None:
    """Return the original id when it collided with a stored session (payload gets a fresh id)."""

    original = str(payload.get("session_id", ""))
    try:
        collides = bool(original) and get_session_store().exists(original)
    except StorageError:
        return None
    except ValueError as exc:  # e.g. Windows reserved names (CON, NUL, ...)
        raise HTTPException(status_code=400, detail="无效的 session_id（保留名称或非法字符）。") from exc
    if not collides:
        return None
    payload["session_id"] = uuid.uuid4().hex[:12]
    return original


@router.post("/session/import", dependencies=[Depends(require_trusted_client)])
async def import_session(request: Request) -> dict[str, Any]:
    raw = await read_limited_body(request, max_import_bytes())
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail="Import payload is not valid JSON.") from exc
    session_payload = validate_import_payload(data)
    # Never overwrite an existing stored game: colliding ids get a fresh id.
    renamed_from = _unique_import_id(session_payload)
    try:
        session = await orchestrator.load_session_data(session_payload)
    except ValueError as exc:  # engine-side validation (ValidationError/ScenarioError)
        raise HTTPException(status_code=400, detail=f"Invalid session data: {exc}") from exc
    result: dict[str, Any] = {"ok": True, "session": _public_session(session)}
    if renamed_from:
        result["renamed_from"] = renamed_from
    return result


@router.post("/sessions/{session_id}/load", dependencies=[Depends(require_trusted_client)])
async def load_stored_session(session_id: str) -> dict[str, Any]:
    provider_api.check_session_id(session_id)
    if session_id == str(getattr(orchestrator.session, "session_id", "")):
        # Persisting the live game first would overwrite the very copy being loaded.
        raise HTTPException(status_code=409, detail="该存档就是当前对局，无需加载。")
    store = get_session_store()
    try:
        if not store.exists(session_id):
            raise HTTPException(status_code=404, detail="存档不存在。")
        payload = store.load(session_id)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=f"读取存档失败：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="无效的 session_id（保留名称或非法字符）。") from exc
    session_payload = validate_import_payload(payload)

    # The UI promises "the current game is saved first": do it explicitly, and abort
    # (leaving the live game untouched) if that save fails.
    try:
        saved = await orchestrator.persist_session("pre-load")
    except Exception as exc:  # noqa: BLE001 - any failure must abort the load
        logger.error("Pre-load save failed: %s", type(exc).__name__)
        # Only storage errors carry user-safe text; anything else may contain paths.
        reason = f"（{exc}）" if isinstance(exc, StorageError) else ""
        raise HTTPException(
            status_code=503,
            detail=f"加载已取消：无法先保存当前对局{reason}。当前对局未被改动。",
        ) from exc
    if saved is False:
        raise HTTPException(
            status_code=503,
            detail="加载已取消：无法先保存当前对局。当前对局未被改动。",
        )
    try:
        session = await orchestrator.load_session_data(session_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid session data: {exc}") from exc
    return {"ok": True, "session": _public_session(session)}
