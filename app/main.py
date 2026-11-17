from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.providers import router as providers_router
from app.api.routes import router as api_router
from app.api.security import warn_if_exposed
from app.api.workspaces import router as workspaces_router
from app.config import APP_VERSION, BASE_DIR
from app.game.orchestrator import get_orchestrator
from app.storage.errors import StorageError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.degraded = False
    try:
        # Fail fast on config/scenario errors instead of on the first request.
        orchestrator = get_orchestrator()
        # Migrate legacy data, restore the last game (paused) and enable autosave.
        # startup() reports problems instead of raising; a failed restore keeps the
        # files untouched and leaves the app in a "needs recovery" state.
        startup = getattr(orchestrator, "startup", None)
        if startup is not None:
            report = await startup()
            if isinstance(report, dict) and report.get("recovery_error"):
                application.state.degraded = True
                logger.error("Last session could not be restored; recovery needed (see session.restore_notice)")
    except StorageError:
        # Encrypted storage unavailable (missing master key / data dir): do not
        # crash-loop the container. Stay up in a degraded state; storage-backed
        # endpoints answer 503 with a clear reason and nothing is written in plaintext.
        application.state.degraded = True
        logger.error("Startup storage failure; running in degraded mode", exc_info=True)
    # Startup check: warn loudly when exposed beyond loopback without a token.
    warn_if_exposed()
    try:
        yield
    finally:
        # Graceful shutdown: pause, flush the autosave, close LLM clients.
        try:
            await get_orchestrator().shutdown()
        except Exception:  # noqa: BLE001 - never block process exit
            logger.exception("Orchestrator shutdown failed")


app = FastAPI(
    title="Arena-Agents",
    description="由可执行 Markdown 剧本驱动的通用多智能体博弈沙盒",
    version=APP_VERSION,
    lifespan=lifespan,
)


def _validation_detail(exc: RequestValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "query"))
        msg = str(err.get("msg", "invalid value"))
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "Invalid request"


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Never echo the offending input (it may contain an API key).
    return JSONResponse(status_code=422, content={"detail": _validation_detail(exc)})


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail: Any = exc.detail
    if not isinstance(detail, str):
        detail = str(detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": detail},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(StorageError)
async def storage_error_handler(request: Request, exc: StorageError) -> JSONResponse:
    logger.error("Storage error on %s %s: %s", request.method, request.url.path, type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": str(exc) or "存储不可用。"})


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.middleware("http")
async def disable_static_cache(request: Request, call_next) -> Response:
    response: Response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Unauthenticated liveness probe; intentionally exposes nothing sensitive."""

    status = "degraded" if getattr(app.state, "degraded", False) else "ok"
    return {"status": status, "version": APP_VERSION}


app.include_router(api_router)
app.include_router(providers_router)
app.include_router(workspaces_router)

STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(
        str(STATIC_DIR / "index.html"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
