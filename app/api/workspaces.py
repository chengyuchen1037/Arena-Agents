"""Workspace API (v0.11): list, native folder picker and the guarded switch.

A *workspace* is the user-chosen folder that holds providers, templates, bindings, sessions,
logs and the encryption key (see ``app.storage.workspace``).  This module is only the HTTP
face of it:

* ``GET  /api/workspaces``              - enabled flag, current workspace, recent list;
* ``POST /api/workspaces/pick-native-folder`` - open the OS folder dialog (loopback only);
* ``POST /api/workspaces/select``       - hand the folder to ``orchestrator.switch_workspace``.

Security rules:

* every route sits behind ``verify_api_access`` (token + Origin);
* picking and switching act on the server's own filesystem, so they are strictly loopback
  (``require_local_client``) - a remote client must never choose a server directory, even
  when it holds a valid token;
* read responses for non-loopback clients never contain absolute paths.

The storage module is imported lazily: if a deployment has no workspace support (older
storage layer), read endpoints report ``enabled: false`` and the write endpoints answer a
clear 503 instead of a 500.  Tests inject ``workspace_module``; the real module is only
imported when it is not injected.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.api.security import (
    is_local_request,
    require_local_client,
    verify_api_access,
)
from app.game.orchestrator import orchestrator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])

MAX_PATH_CHARS = 4096

#: Test injection point: set to a fake module; ``None`` means "import the real one".
workspace_module: Any = None


# --------------------------------------------------------------------------
# Module access + error mapping
# --------------------------------------------------------------------------
def _load_workspace_module() -> Any:
    """The real storage module, or None when this build has no workspace support."""

    try:
        import app.storage.workspace as module
    except ImportError:  # pragma: no cover - exercised with an injected missing module
        return None
    return module


def _ws() -> Any:
    return workspace_module if workspace_module is not None else _load_workspace_module()


def _module_enabled(module: Any) -> bool:
    try:
        return bool(module.workspace_enabled())
    except Exception:  # noqa: BLE001 - "cannot tell" must not take the API down
        logger.warning("workspace_enabled() failed", exc_info=True)
        return False


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="工作文件夹功能不可用：当前存储层未提供 workspace 支持，请检查安装或配置。",
    )


def _disabled() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="工作文件夹功能未启用（服务端设置了 ARENA_DISABLE_WORKSPACE），不能在界面切换工作文件夹。",
    )


def _clean_path(raw: Any) -> str:
    """Validate the folder string sent by the (local) client; the storage layer judges it."""

    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="工作文件夹路径必须是字符串。")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise HTTPException(status_code=400, detail="工作文件夹路径含有控制字符。")
    value = raw.strip()
    if not value:
        raise HTTPException(status_code=400, detail="工作文件夹路径不能为空。")
    if len(value) > MAX_PATH_CHARS:
        raise HTTPException(status_code=400, detail="工作文件夹路径过长。")
    return value


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


# --------------------------------------------------------------------------
# Payload helpers
# --------------------------------------------------------------------------
def _is_path_key(key: Any) -> bool:
    name = str(key).lower()
    return "path" in name or "dir" in name or name in ("root", "folder")


def _without_paths(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop filesystem locations from a read payload (remote clients must never see them)."""

    return {key: value for key, value in mapping.items() if not _is_path_key(key)}


def _normalise_workspace_payload(payload: Any, *, include_path: bool) -> dict[str, Any]:
    """Flatten the workspace payload variants into the stable API summary shape.

    ``app.storage.workspace.get_active_workspace_payload`` returns
    ``{"enabled": bool, "current": {"id", "name", "created_at", "path"?} | None}`` while the
    engine's ``workspace_payload()`` adds ``last_session`` / ``status`` / ``degraded``.  Older
    builds returned a flat dict.  This tolerates all three so the HTTP contract stays flat:
    ``{enabled, id, name, created_at, path?, last_session, status, degraded}``.
    """

    source = payload if isinstance(payload, dict) else {}
    if "current" in source:
        nested = source.get("current")
        out: dict[str, Any] = dict(nested) if isinstance(nested, dict) else {}
        # Flat fields win only when the nested object did not carry them.
        for key in ("id", "name", "created_at", "path", "last_session", "status"):
            if key not in out and source.get(key) is not None:
                out[key] = source[key]
        if "enabled" in source:
            out = {"enabled": bool(source["enabled"]), **out}
    else:
        out = dict(source)
    for key in ("last_session", "status", "degraded"):
        if key in source:
            out[key] = source[key]
    if not include_path:
        out = _without_paths(out)
    return out


def workspace_state(*, include_path: bool, engine: Any = None) -> dict[str, Any]:
    """The workspace summary used by ``GET /api/state`` and ``GET /api/workspaces``.

    Uses the engine's payload (the orchestrator knows the session/status too), normalises it
    and strips every absolute path unless ``include_path`` is true (loopback callers only).
    """

    target = orchestrator if engine is None else engine
    payload: dict[str, Any] = {"enabled": False}
    getter = getattr(target, "workspace_payload", None)
    if getter is not None:
        try:
            candidate = getter(include_path=include_path)
        except Exception:  # noqa: BLE001 - workspace problems must never break /state
            logger.warning("Could not read the workspace payload", exc_info=True)
            candidate = None
        if isinstance(candidate, dict):
            payload = _normalise_workspace_payload(candidate, include_path=include_path)
    if "enabled" not in payload:
        module = _ws()
        payload["enabled"] = bool(module is not None and _module_enabled(module))
    return payload


def recent_workspaces(*, include_path: bool) -> list[dict[str, Any]]:
    module = _ws()
    if module is None:
        return []
    lister = getattr(module, "list_recent_workspaces", None)
    if lister is None:
        return []
    try:
        items = lister() or []
    except Exception:  # noqa: BLE001 - the recent list is a convenience, never fatal
        logger.warning("Could not list recent workspaces", exc_info=True)
        return []
    result: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            cleaned = dict(item)
        else:
            cleaned = {"name": str(item)}
        if not include_path:
            cleaned = _without_paths(cleaned)
        result.append(cleaned)
    return result


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------
class WorkspaceSelectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    initialize_existing: bool = False
    create_if_missing: bool = False


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@router.get("/workspaces")
async def list_workspaces(request: Request) -> dict[str, Any]:
    """The current workspace, the recent list and whether this client may pick one.

    Read-only: a remote client only ever sees names and ids, never an absolute path.
    """

    local = is_local_request(request)
    module = _ws()
    enabled = module is not None and _module_enabled(module)
    if not enabled:
        return {"enabled": False, "current": None, "recent": [], "native_picker": False}

    summary = workspace_state(include_path=local)
    current = {key: value for key, value in summary.items() if key != "enabled"}
    result: dict[str, Any] = {
        "enabled": True,
        "current": current or None,
        "recent": recent_workspaces(include_path=local),
        # The native dialog opens on the server's desktop, so only a local client may ask.
        "native_picker": local,
    }
    if local:
        try:
            result["default_path"] = str(module.get_default_workspace_path())
        except Exception:  # noqa: BLE001 - optional convenience field
            logger.warning("Could not read the default workspace path", exc_info=True)
    return result


@router.post(
    "/workspaces/pick-native-folder", dependencies=[Depends(require_local_client)]
)
async def pick_native_folder() -> dict[str, Any]:
    """Open the OS folder chooser on the server's desktop and return the chosen path.

    Loopback only (even with a token): a remote caller cannot browse server directories.
    Nothing is switched here; the client sends the result to ``/workspaces/select``.
    """

    module = _ws()
    if module is None:
        raise _unavailable()
    if not _module_enabled(module):
        raise _disabled()
    picker = getattr(module, "pick_native_folder", None)
    if picker is None:
        raise _unavailable()
    try:
        # The dialog blocks until the user answers; never block the event loop (SSE).
        path = await asyncio.to_thread(picker)
    except (ValueError, OSError) as exc:
        # WorkspaceError is a ValueError: "no GUI" / "cancelled" - the UI falls back to
        # typing the path, so this is a user-facing 400 rather than a server error.
        raise HTTPException(
            status_code=400,
            detail=str(exc) or "未能打开系统文件夹选择框，请手动输入路径。",
        ) from exc
    except Exception:  # noqa: BLE001 - a GUI failure is an environment issue, not a bug
        logger.warning("Native folder picker failed", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="无法打开系统文件夹选择框（没有可用的图形界面），请手动输入路径。",
        ) from None
    return {"path": str(path)}


@router.post("/workspaces/select", dependencies=[Depends(require_local_client)])
async def select_workspace(req: WorkspaceSelectRequest) -> dict[str, Any]:
    """Switch the running application to another workspace folder.

    The engine owns the transaction (save first, validate/activate target, roll back on
    failure); this endpoint only validates the input and maps the outcome:

    * a game is running / a switch is in progress -> 409 (nothing changed);
    * invalid, locked or non-workspace target     -> 400 (the current workspace stays);
    * workspace support missing                   -> 503.
    """

    module = _ws()
    if module is None:
        raise _unavailable()
    if not _module_enabled(module):
        raise _disabled()
    path = _clean_path(req.path)
    switcher = getattr(orchestrator, "switch_workspace", None)
    if switcher is None:
        raise _unavailable()
    try:
        result = await _maybe_await(
            switcher(
                path,
                initialize_existing=req.initialize_existing,
                create_if_missing=req.create_if_missing,
            )
        )
    except RuntimeError as exc:
        # Running game / switch in progress: explicitly "nothing was changed".
        raise HTTPException(
            status_code=409,
            detail=str(exc) or "对局进行中，不能切换工作文件夹；请先暂停对局。",
        ) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc) or "无法切换到该工作文件夹；当前工作文件夹未改变。",
        ) from exc
    workspace_payload = result.get("workspace") if isinstance(result, dict) else None
    if isinstance(workspace_payload, dict):
        workspace_payload = _normalise_workspace_payload(workspace_payload, include_path=True)
    else:
        workspace_payload = workspace_state(include_path=True)
    session = result.get("session") if isinstance(result, dict) else None
    if session is None:
        session = getattr(orchestrator, "session", None)
    session_payload: Any = None
    if session is not None:
        try:
            from app.api.routes import _public_session

            session_payload = _public_session(session)
        except Exception:  # noqa: BLE001 - never fail a successful switch on serialisation
            logger.warning("Could not serialise the session after a workspace switch", exc_info=True)
            session_payload = session.model_dump() if hasattr(session, "model_dump") else None
    # This route is loopback-only, so the payload may include the chosen absolute path.
    return {"workspace": workspace_payload, "session": session_payload}


__all__ = [
    "MAX_PATH_CHARS",
    "WorkspaceSelectRequest",
    "list_workspaces",
    "pick_native_folder",
    "recent_workspaces",
    "router",
    "select_workspace",
    "workspace_module",
    "workspace_state",
]
