"""Authentication, origin checks and input hardening for the HTTP API.

Contract (see docs/architecture.md):

* ``ARENA_API_TOKEN`` set  -> every ``/api/*`` request needs ``X-Arena-Token``
  (``GET /api/events`` and the archive download ``GET /api/archive/exports/<name>``
  additionally accept ``?token=`` because EventSource and a plain download link
  cannot send headers).
* no token                 -> state-changing configuration endpoints are
  restricted to loopback clients.
* non-GET requests carrying an ``Origin`` header must be same-origin (Origin
  host == Host header) or listed in ``ARENA_ALLOWED_ORIGINS``.
* Provider settings (base_url / api_key / names / model ids) are validated
  here; secrets are never echoed back in errors.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import re
from collections.abc import Iterable
from urllib.parse import urlparse

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

TOKEN_HEADER = "X-Arena-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
DEFAULT_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_SECRET_CHARS = 2048
MAX_URL_CHARS = 2048
MAX_NAME_CHARS = 100
MAX_MODEL_ID_CHARS = 200

# Hostnames Starlette's TestClient / local tooling use for local clients.
_LOCAL_CLIENT_NAMES = frozenset({"localhost", "testclient"})

# Hostnames that always point at cloud metadata services.
_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata", "instance-data"})
_METADATA_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::/32"),
    ipaddress.ip_network("100.100.100.200/32"),  # Alibaba Cloud metadata
)
_AMBIGUOUS_IPV4 = re.compile(
    r"^(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+)){0,3}$", re.IGNORECASE
)
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


# --------------------------------------------------------------------------
# Token / loopback / origin
# --------------------------------------------------------------------------
def get_api_token() -> str:
    return os.environ.get("ARENA_API_TOKEN", "").strip()


def is_loopback_host(host: str | None) -> bool:
    if host is None:
        return True  # no peer address (unix socket / in-process client)
    value = host.strip().lower()
    if value in _LOCAL_CLIENT_NAMES:
        return True
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def allowed_origins() -> list[str]:
    return [item.lower().rstrip("/") for item in _split_csv(os.environ.get("ARENA_ALLOWED_ORIGINS", ""))]


def origin_allowed(origin: str, host_header: str) -> bool:
    origin_norm = origin.strip().lower().rstrip("/")
    if origin_norm in ("", "null"):
        return False
    parsed = urlparse(origin_norm)
    netloc = parsed.netloc
    if parsed.scheme in ("http", "https") and netloc and netloc == host_header.strip().lower():
        return True
    for entry in allowed_origins():
        if entry == origin_norm or (netloc and entry == netloc):
            return True
    return False


_EXPORT_DOWNLOAD = re.compile(r".*/api/archive/exports/arena-[0-9]{8}-[0-9]{6}\.arenaagents$")


def _is_events_get(request: Request) -> bool:
    return request.method == "GET" and request.url.path.rstrip("/").endswith("/api/events")


def _is_export_download(request: Request) -> bool:
    """``GET /api/archive/exports/<name>``: opened by a plain link (a download), which cannot carry a header either."""
    return request.method == "GET" and bool(_EXPORT_DOWNLOAD.fullmatch(request.url.path))


def check_origin(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is None:
        return  # curl, server-side clients, tests
    if not origin_allowed(origin, request.headers.get("host", "")):
        raise HTTPException(
            status_code=403,
            detail="Cross-origin request rejected (Origin does not match Host; "
            "add it to ARENA_ALLOWED_ORIGINS if this is intended).",
        )


def check_token(request: Request) -> bool:
    """Validate the API token. Returns True when a token is configured and valid."""

    expected = get_api_token()
    if not expected:
        return False
    supplied = request.headers.get(TOKEN_HEADER, "")
    if not supplied and (_is_events_get(request) or _is_export_download(request)):
        supplied = request.query_params.get("token", "")
    if not supplied or not hmac.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            detail=f"Missing or invalid API token (send the {TOKEN_HEADER} header).",
        )
    return True


async def verify_api_access(request: Request) -> None:
    """Router-level dependency: origin check, then token check."""

    check_origin(request)
    check_token(request)


def is_trusted_request(request: Request) -> bool:
    """The rule of :func:`require_trusted_client` as a plain yes / no, for a response that cannot be refused as a whole
    (the SSE stream is open to everybody, but only a trusted client is sent the error log, v0.13.1).

    With a token configured the router dependency (:func:`verify_api_access`) has authenticated the caller already;
    without one only a loopback peer is trusted."""

    if get_api_token():
        return True
    client = request.client
    return is_loopback_host(client.host if client else None)


async def require_trusted_client(request: Request) -> None:
    """Extra guard for configuration-changing endpoints.

    With a token configured the router dependency already authenticated the
    caller. Without one only loopback clients may use these endpoints.
    """

    if not is_trusted_request(request):
        raise HTTPException(
            status_code=403,
            detail="This endpoint is restricted to local clients unless ARENA_API_TOKEN is set.",
        )


def is_local_request(request: Request) -> bool:
    """True when the peer is a loopback address (token or not)."""

    client = request.client
    return is_loopback_host(client.host if client else None)


async def require_local_client(request: Request) -> None:
    """Strict guard: loopback only, EVEN when ARENA_API_TOKEN is set.

    Used for operations that act on the server's own filesystem: a remote caller, however
    authenticated, must never choose server paths.
    """

    if not is_local_request(request):
        raise HTTPException(
            status_code=403,
            detail="该操作只能在运行服务的本机上执行（远程客户端不能操作服务器上的文件夹）。",
        )


def warn_if_exposed() -> bool:
    """Log a warning when listening on a non-loopback address without a token."""

    host = os.environ.get("HOST", "").strip()
    if not host or get_api_token():
        return False
    if host.lower() == "localhost" or is_loopback_host(host):
        return False
    message = (
        f"SECURITY WARNING: server is configured to listen on {host!r} "
        "(non-loopback) but ARENA_API_TOKEN is not set. Anyone who can reach "
        "this port can control games and spend your API quota. Set ARENA_API_TOKEN "
        "or bind to 127.0.0.1."
    )
    logging.getLogger("uvicorn.error").warning(message)
    return True


# --------------------------------------------------------------------------
# Provider input validation
# --------------------------------------------------------------------------
def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def validate_display_text(label: str, value: str, max_chars: int = MAX_NAME_CHARS) -> str:
    """Names/model ids: printable text, no control characters, bounded length."""

    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{label} 必须是字符串。")
    if _has_control_chars(value):
        raise HTTPException(status_code=400, detail=f"{label} 含有控制字符。")
    stripped = value.strip()
    if not stripped:
        raise HTTPException(status_code=400, detail=f"{label} 不能为空。")
    if len(stripped) > max_chars:
        raise HTTPException(status_code=400, detail=f"{label} 过长（最多 {max_chars} 个字符）。")
    return stripped


def validate_secret(label: str, value: str) -> str:
    """API keys are sent in HTTP headers: printable ASCII, no whitespace/control chars."""

    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{label} 必须是字符串。")
    if _has_control_chars(value):
        raise HTTPException(status_code=400, detail=f"{label} 含有控制字符。")
    stripped = value.strip()
    if len(stripped) > MAX_SECRET_CHARS:
        raise HTTPException(status_code=400, detail=f"{label} 过长。")
    if stripped and (not stripped.isascii() or any(ch.isspace() for ch in stripped)):
        raise HTTPException(status_code=400, detail=f"{label} 只能包含不含空白的 ASCII 字符。")
    return stripped


def private_providers_allowed() -> bool:
    return os.environ.get("ARENA_ALLOW_PRIVATE_PROVIDERS", "").strip().lower() in ("1", "true", "yes", "on")


def _classify_host(host: str) -> str:
    """Return 'metadata', 'private' or 'public' for a hostname / IP literal."""

    name = host.strip().lower().rstrip(".")
    if name in _METADATA_HOSTS:
        return "metadata"
    try:
        address = ipaddress.ip_address(name.strip("[]").split("%", 1)[0])
    except ValueError:
        if _AMBIGUOUS_IPV4.fullmatch(name):
            # decimal/hex/octal IPv4 spellings such as 2130706433 or 0x7f.1
            return "metadata"
        if name == "localhost" or name.endswith(_PRIVATE_SUFFIXES) or "." not in name:
            return "private"
        return "public"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if any(address in net for net in _METADATA_NETWORKS if net.version == address.version):
        return "metadata"
    if address.is_unspecified:
        return "metadata"
    if address.is_loopback or address.is_private or address.is_link_local:
        return "private"
    if address.is_multicast or address.is_reserved:
        return "metadata"
    return "public"


def validate_provider_base_url(base_url: str, *, allow_query: bool = False) -> str:
    """Validate a Provider base_url; returns it stripped or raises 400.

    ``allow_query`` is only for manual-endpoint providers (``auto_append_path`` off), whose
    base_url is a complete message URL that may legitimately carry e.g. ``?api-version=``.
    """

    if not isinstance(base_url, str):
        raise HTTPException(status_code=400, detail="base_url 必须是字符串。")
    if _has_control_chars(base_url):
        raise HTTPException(status_code=400, detail="base_url 含有控制字符。")
    url = base_url.strip()
    if not url or len(url) > MAX_URL_CHARS or any(ch.isspace() for ch in url):
        raise HTTPException(status_code=400, detail="base_url 为空、过长或含空白字符。")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="base_url 必须是 http 或 https 地址。")
    try:
        host = parsed.hostname
        parsed.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="base_url 不是有效的 URL。") from exc
    if not host or parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise HTTPException(status_code=400, detail="base_url 不是有效的 URL（不允许包含用户名/密码）。")
    if parsed.fragment or (parsed.query and not allow_query):
        raise HTTPException(status_code=400, detail="base_url 不能包含 ? 查询参数或 # 片段。")
    host = host.lower()
    allowed = {item.lower() for item in _split_csv(os.environ.get("ARENA_ALLOWED_BASE_HOSTS", ""))}
    if allowed and host not in allowed:
        raise HTTPException(
            status_code=400,
            detail="base_url 的主机不在 ARENA_ALLOWED_BASE_HOSTS 白名单内。",
        )
    kind = _classify_host(host)
    if kind == "metadata":
        raise HTTPException(
            status_code=400,
            detail="base_url 指向云元数据/链路本地/保留地址，已被阻止。",
        )
    if kind == "private" and not private_providers_allowed():
        raise HTTPException(
            status_code=400,
            detail="base_url 指向本机或内网地址；如确需使用（如 Ollama/LM Studio），"
            "请由管理员设置 ARENA_ALLOW_PRIVATE_PROVIDERS=true。",
        )
    return url


def redact(text: str, secrets: Iterable[str]) -> str:
    """Remove secret values (and anything key-like) from an error message."""

    out = str(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, "***")
    out = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1***", out)
    out = re.sub(r"\b(sk|key|tok)-[A-Za-z0-9_-]{8,}", r"\1-***", out)
    return out[:300]


# --------------------------------------------------------------------------
# Import limits
# --------------------------------------------------------------------------
def max_archive_bytes() -> int:
    """Largest ``.arenaagents`` upload (v0.13, F30): 2 GiB unless ``ARENA_MAX_ARCHIVE_BYTES`` says otherwise."""
    raw = os.environ.get("ARENA_MAX_ARCHIVE_BYTES", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            logger.warning("Ignoring invalid ARENA_MAX_ARCHIVE_BYTES=%r", raw)
    return DEFAULT_MAX_ARCHIVE_BYTES
