"""Back-end model discovery (``GET /models``) with SSRF guards.

``discover_models(provider)`` is what the API layer calls. It never echoes the
API key or the upstream response body: errors carry only a status and a short,
fixed Chinese message.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.llm.endpoints import EndpointError, build_endpoints

logger = logging.getLogger(__name__)

DISCOVERY_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1_000_000
MAX_MODELS = 1000
MAX_MODEL_ID_LENGTH = 200
ANTHROPIC_VERSION = "2023-06-01"
ALLOW_PRIVATE_ENV = "ARENA_ALLOW_PRIVATE_PROVIDERS"

#: Cloud metadata endpoints that are not link-local on every platform.
_METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(a)
    for a in ("169.254.169.254", "169.254.170.2", "fd00:ec2::254", "100.100.100.200", "168.63.129.16")
)
_TRUE_VALUES = {"1", "true", "yes", "on"}

Resolver = Callable[[str, int], Awaitable[list[str]]]


class DiscoveryError(ValueError):
    """Model discovery failed. ``str(exc)`` is safe to show to the user.

    ``kind`` is one of ``invalid_url``, ``manual_endpoint`` (auto_append_path is off:
    discovery is not possible), ``blocked``, ``network``, ``timeout``,
    ``auth``, ``status``, ``redirect``, ``too_large``, ``invalid_response``.
    ``status`` is the upstream HTTP status when there was one.
    """

    def __init__(self, message: str, *, kind: str = "error", status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


def private_providers_allowed() -> bool:
    return os.environ.get(ALLOW_PRIVATE_ENV, "").strip().lower() in _TRUE_VALUES


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def parse_base_url(base_url: str) -> tuple[str, str, int, str]:
    """Validate a provider base URL; returns ``(scheme, host, port, path)``.

    Raises :class:`DiscoveryError` (kind ``invalid_url``) for anything but a plain
    http(s) URL without credentials, whitespace or control characters.
    """
    url = (base_url or "").strip()
    if not url or _has_control_chars(url) or any(ch.isspace() for ch in url):
        raise DiscoveryError("Base URL 无效：不能为空或包含空白、控制字符。", kind="invalid_url")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise DiscoveryError("Base URL 无效。", kind="invalid_url") from None
    if parts.scheme not in ("http", "https"):
        raise DiscoveryError("Base URL 必须以 http:// 或 https:// 开头。", kind="invalid_url")
    if parts.username is not None or parts.password is not None:
        raise DiscoveryError("Base URL 不能包含用户名或密码。", kind="invalid_url")
    if not parts.hostname:
        raise DiscoveryError("Base URL 缺少主机名。", kind="invalid_url")
    if parts.query or parts.fragment:
        raise DiscoveryError("Base URL 不能包含查询参数或片段。", kind="invalid_url")
    default_port = 443 if parts.scheme == "https" else 80
    return parts.scheme, parts.hostname, port or default_port, parts.path.rstrip("/")


def classify_address(address: str) -> str:
    """``"blocked"`` (never allowed), ``"private"`` (loopback / LAN) or ``"public"``."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip in _METADATA_ADDRESSES
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
    ):
        return "blocked"
    if ip.is_loopback:  # (::1 also sits inside the reserved ::/8, so test it first)
        return "private"
    if ip.is_reserved:
        return "blocked"
    if not ip.is_global:
        return "private"
    return "public"


async def _default_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        addr = info[4][0]
        if "%" in addr:  # scoped IPv6 literal
            addr = addr.split("%", 1)[0]
        if addr not in seen:
            seen.append(addr)
    return seen


async def resolve_and_check(
    host: str,
    port: int,
    *,
    allow_private: bool | None = None,
    resolver: Resolver | None = None,
) -> list[str]:
    """Resolve ``host`` and verify *every* address; returns the usable addresses.

    The caller must connect to one of these addresses (not re-resolve the name),
    which is what defeats DNS rebinding.
    """
    if allow_private is None:
        allow_private = private_providers_allowed()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            addresses = await (resolver or _default_resolver)(host, port)
        except (OSError, UnicodeError):
            raise DiscoveryError("无法解析 Base URL 的主机名。", kind="network") from None
    if not addresses:
        raise DiscoveryError("无法解析 Base URL 的主机名。", kind="network")
    for address in addresses:
        try:
            kind = classify_address(address)
        except ValueError:
            raise DiscoveryError("Base URL 解析到了无效地址。", kind="blocked") from None
        if kind == "blocked":
            raise DiscoveryError(
                "Base URL 指向被禁止的地址（链路本地 / 云元数据 / 保留地址）。", kind="blocked"
            )
        if kind == "private" and not allow_private:
            raise DiscoveryError(
                f"Base URL 指向内网或本机地址；如确需使用，请设置环境变量 {ALLOW_PRIVATE_ENV}=true。",
                kind="blocked",
            )
    return addresses


def models_url(protocol: str, base_url: str) -> tuple[str, dict[str, str]]:
    """Models endpoint (and query params) for ``protocol`` - always via ``build_endpoints``."""
    try:
        plan = build_endpoints(protocol, base_url, True)
    except EndpointError as exc:
        raise DiscoveryError(str(exc), kind="invalid_url") from None
    assert plan.models_url is not None
    params = {"limit": str(MAX_MODELS)} if protocol == "anthropic_messages" else {}
    return plan.models_url, params


def _headers(protocol: str, api_key: str) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "Arena-Agent/0.10"}
    if protocol == "anthropic_messages":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = ANTHROPIC_VERSION
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _pinned_url(url: str, address: str) -> str:
    parts = urlsplit(url)
    host = f"[{address}]" if ":" in address else address
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def parse_models_payload(payload: Any) -> list[str]:
    """Extract model ids from the common ``/models`` response shapes."""
    if isinstance(payload, dict):
        entries = payload.get("data")
        if not isinstance(entries, list):
            entries = payload.get("models")
    else:
        entries = payload
    if not isinstance(entries, list):
        raise DiscoveryError("上游响应格式无法识别（缺少模型列表）。", kind="invalid_response")
    ids: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, str):
            model_id = entry
        elif isinstance(entry, dict):
            raw = entry.get("id") or entry.get("name")
            model_id = raw if isinstance(raw, str) else ""
        else:
            continue
        model_id = model_id.strip()
        if (
            not model_id
            or len(model_id) > MAX_MODEL_ID_LENGTH
            or _has_control_chars(model_id)
            or model_id in seen
        ):
            continue
        seen.add(model_id)
        ids.append(model_id)
        if len(ids) >= MAX_MODELS:
            break
    return ids


def _status_error(status: int) -> DiscoveryError:
    if status in (401, 403):
        return DiscoveryError(f"认证失败（HTTP {status}），请检查 API Key。", kind="auth", status=status)
    if status == 404:
        return DiscoveryError(
            "上游不支持获取模型列表（HTTP 404），请检查 Base URL 或手动输入模型名。",
            kind="status",
            status=404,
        )
    if status == 429:
        return DiscoveryError("上游限流（HTTP 429），请稍后重试。", kind="status", status=status)
    return DiscoveryError(f"上游返回错误（HTTP {status}）。", kind="status", status=status)


async def discover_models(
    provider: Any,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
    allow_private: bool | None = None,
) -> list[str]:
    """List the model ids offered by ``provider`` (needs ``protocol/base_url/api_key``).

    Limits: 10 s total, 1 MB response, 1000 models, no redirects. The host is
    resolved here, every address is checked, and the request is sent to the
    checked address (with the original ``Host`` / SNI), so a DNS answer that
    changes between check and use cannot redirect the request.
    """
    protocol = str(getattr(provider, "protocol", "") or "")
    base_url = str(getattr(provider, "base_url", "") or "")
    api_key = str(getattr(provider, "api_key", "") or "")
    if protocol not in ("openai_chat", "openai_responses", "anthropic_messages"):
        raise DiscoveryError("不支持的协议类型。", kind="invalid_url")
    if not api_key.strip():
        raise DiscoveryError("该 Provider 尚未设置 API Key。", kind="auth")
    if _has_control_chars(api_key):
        raise DiscoveryError("API Key 含有非法字符。", kind="auth")

    if not bool(getattr(provider, "auto_append_path", True)):
        raise DiscoveryError(
            "该 Provider 已关闭“自动拼接路径”，Base URL 是完整消息地址，无法获取模型列表，请手动填写模型 ID。",
            kind="manual_endpoint",
        )
    url, params = models_url(protocol, base_url)  # validates base_url too
    scheme, host, port, _ = parse_base_url(base_url)
    addresses = await resolve_and_check(host, port, allow_private=allow_private, resolver=resolver)

    # Connect to the vetted address; keep the original name for Host header and TLS SNI.
    try:
        ipaddress.ip_address(host)
        is_literal = True
    except ValueError:
        is_literal = False
    request_url = url if is_literal else _pinned_url(url, addresses[0])
    headers = _headers(protocol, api_key)
    extensions: dict[str, Any] = {}
    if not is_literal:
        default_port = 443 if scheme == "https" else 80
        headers["Host"] = host if port == default_port else f"{host}:{port}"
        if scheme == "https":
            extensions["sni_hostname"] = host

    body = bytearray()
    try:
        async with asyncio.timeout(DISCOVERY_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(DISCOVERY_TIMEOUT_SECONDS),
            ) as client:
                async with client.stream(
                    "GET", request_url, params=params or None, headers=headers, extensions=extensions
                ) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        raise DiscoveryError(
                            f"上游返回重定向（HTTP {status}），出于安全考虑已拒绝跟随。",
                            kind="redirect",
                            status=status,
                        )
                    if status >= 400:
                        raise _status_error(status)
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                        raise DiscoveryError("上游响应过大，已拒绝读取。", kind="too_large")
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise DiscoveryError("上游响应过大，已拒绝读取。", kind="too_large")
    except DiscoveryError:
        raise
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise DiscoveryError("连接上游超时（10 秒）。", kind="timeout") from None
    except httpx.ConnectError as exc:
        reason = "TLS 证书校验失败" if "CERTIFICATE" in str(exc).upper() else "无法连接到上游"
        raise DiscoveryError(f"{reason}，请检查 Base URL。", kind="network") from None
    except httpx.HTTPError:
        raise DiscoveryError("请求上游失败，请检查 Base URL 与网络。", kind="network") from None

    try:
        payload = json.loads(bytes(body))
    except (ValueError, RecursionError):
        raise DiscoveryError("上游响应不是有效的 JSON。", kind="invalid_response") from None
    return parse_models_payload(payload)
