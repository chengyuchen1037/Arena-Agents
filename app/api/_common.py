"""Small helpers shared by the HTTP modules of the bots and files APIs (v0.14): error bodies, download file names (H42), the lazily
looked-up orchestrator.

* Errors are ``{"detail": "<中文>"}`` plus, when there is something to say, ``"code"`` and ``"problems"`` (one sentence per fault).
* A downloaded file's name drops ``\\ / : * ? " < > |`` and control characters and is sent twice: as ASCII for old clients and RFC 5987
  encoded (``filename*=UTF-8''...``) for Chinese names (H42).  Names inside the data directory are never made from user input.
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi.responses import JSONResponse, Response

_FILE_NAME_ILLEGAL = re.compile(r"[\\/:*?\"<>|\x00-\x1f\x7f]")


def error_response(status: int, detail: str, *, code: str | None = None, **extra: Any) -> JSONResponse:
    """``{"detail", "code"?, ...extra}`` with the HTTP ``status``."""
    body: dict[str, Any] = {"detail": detail}
    if code:
        body["code"] = code
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def problems_detail(prefix: str, problems: list[str]) -> str:
    """The one-line ``detail`` for a list of problems: the sentence itself when there is one, else a count and all of them."""
    joined = " ".join(problems)
    return f"{prefix}{joined}" if len(problems) == 1 else f"{prefix}共 {len(problems)} 处问题：{joined}"


def safe_filename(name: Any, fallback: str = "file") -> str:
    """``name`` as part of a download file name: ``\\ / : * ? " < > |`` and control characters removed, no leading / trailing dots."""
    cleaned = _FILE_NAME_ILLEGAL.sub("", str(name or "")).strip().strip(".")
    return cleaned or fallback


def content_disposition(filename: str) -> str:
    """``attachment`` with the file name in ASCII (for old clients) and in RFC 5987 form (Chinese names)."""
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", filename).replace('"', "")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def attachment(content: bytes | str, filename: str, media_type: str = "text/markdown; charset=utf-8") -> Response:
    """A file download (``Content-Disposition: attachment``)."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    return Response(content=data, media_type=media_type, headers={"Content-Disposition": content_disposition(filename)})


def stamp(moment: datetime | None = None) -> str:
    """``YYYYmmdd-HHMMSS`` (local time): the time part of a bot's or an archive's file name."""
    return (moment or datetime.now()).strftime("%Y%m%d-%H%M%S")


def day_stamp(moment: datetime | None = None) -> str:
    """``YYYYmmdd`` (local time): the date part of a game's file name."""
    return (moment or datetime.now()).strftime("%Y%m%d")


class LazyOrchestrator:
    """``app.game.orchestrator.orchestrator``, imported on first use: the API modules of the stores do not need the game package to
    load (and tests put a fake in its place by replacing the ``orchestrator`` name of the module that uses it)."""

    def __getattr__(self, name: str) -> Any:
        from app.game.orchestrator import orchestrator as real

        return getattr(real, name)


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


__all__ = [
    "LazyOrchestrator",
    "attachment",
    "content_disposition",
    "day_stamp",
    "error_response",
    "maybe_await",
    "problems_detail",
    "safe_filename",
    "stamp",
]
