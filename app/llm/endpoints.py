"""The single place where provider URLs are built.

Two modes, chosen per provider by ``auto_append_path``:

* ``True``  - ``base_url`` is a *root* address. The message URL and the
  model-discovery URL are derived from it (``<root>/v1/...``).
* ``False`` - ``base_url`` *is* the complete message endpoint. It is sent
  verbatim, model discovery is impossible, and the SDK's own path appending is
  neutralised by :class:`FixedUrlTransport`.

Nothing else in the code base may concatenate provider URLs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

PROTOCOLS = ("openai_chat", "openai_responses", "anthropic_messages")

#: Path appended to the version root for the message request, per protocol.
_REQUEST_SUFFIX = {
    "openai_chat": "/chat/completions",
    "openai_responses": "/responses",
    "anthropic_messages": "/messages",
}
_COMPLETE_ENDPOINT_SUFFIXES = tuple(_REQUEST_SUFFIX.values())
_OTHER_VERSION_RE = re.compile(r"/v(?!1$)\d+[a-z]*$", re.IGNORECASE)


class EndpointError(ValueError):
    """The base URL / protocol / mode combination is invalid. Message is user-safe."""


@dataclass(frozen=True)
class EndpointPlan:
    protocol: str
    auto_append_path: bool
    request_url: str
    models_url: str | None
    sdk_base_url: str
    warnings: tuple[str, ...] = ()


def _has_control_or_space(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 or ch.isspace() for ch in value)


def _parse(base_url: str, *, allow_query: bool) -> Any:
    url = (base_url or "").strip()
    if not url:
        raise EndpointError("Base URL 不能为空。")
    if _has_control_or_space(url):
        raise EndpointError("Base URL 不能包含空白或控制字符。")
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - validates the port
    except ValueError:
        raise EndpointError("Base URL 无效。") from None
    if parts.scheme not in ("http", "https"):
        raise EndpointError("Base URL 必须以 http:// 或 https:// 开头。")
    if parts.username is not None or parts.password is not None:
        raise EndpointError("Base URL 不能包含用户名或密码。")
    if not parts.hostname:
        raise EndpointError("Base URL 缺少主机名。")
    if parts.fragment:
        raise EndpointError("Base URL 不能包含片段（#）。")
    if parts.query and not allow_query:
        raise EndpointError("自动拼接模式下 Base URL 不能包含查询参数。")
    return parts


def _join(parts: Any, path: str) -> str:
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def build_endpoints(
    protocol: str, base_url: str, auto_append_path: bool = True
) -> EndpointPlan:
    """Compute the final request / discovery / SDK URLs for a provider."""
    if protocol not in _REQUEST_SUFFIX:
        raise EndpointError("不支持的协议类型。")
    warnings: list[str] = []

    if not auto_append_path:
        parts = _parse(base_url, allow_query=True)
        request_url = base_url.strip()
        if parts.scheme == "http":
            warnings.append("使用明文 HTTP，API Key 将以明文传输。")
        path = parts.path.rstrip("/")
        if not path.endswith(_REQUEST_SUFFIX[protocol]):
            warnings.append(
                f"关闭自动拼接时 Base URL 应是完整的消息地址，通常以 {_REQUEST_SUFFIX[protocol]} 结尾。"
            )
        return EndpointPlan(
            protocol=protocol,
            auto_append_path=False,
            request_url=request_url,
            models_url=None,
            sdk_base_url=request_url,
            warnings=tuple(warnings),
        )

    parts = _parse(base_url, allow_query=False)
    path = parts.path.rstrip("/")
    for suffix in _COMPLETE_ENDPOINT_SUFFIXES:
        if path.endswith(suffix):
            raise EndpointError(
                f"Base URL 已包含完整的接口路径（{suffix}）。"
                "请填写根地址，或关闭“自动拼接路径”后使用完整地址。"
            )
    root_path = path[: -len("/v1")] if path.endswith("/v1") else path
    version_root = _join(parts, root_path + "/v1")
    if parts.scheme == "http":
        warnings.append("使用明文 HTTP，API Key 将以明文传输。")
    if _OTHER_VERSION_RE.search(path):
        warnings.append(
            "路径以非 /v1 的版本号结尾，但仍会自动追加 /v1；"
            "若该网关的路径不是 /v1，请关闭“自动拼接路径”并填写完整地址。"
        )
    sdk_base = _join(parts, root_path) if protocol == "anthropic_messages" else version_root
    return EndpointPlan(
        protocol=protocol,
        auto_append_path=True,
        request_url=version_root + _REQUEST_SUFFIX[protocol],
        models_url=version_root + "/models",
        sdk_base_url=sdk_base,
        warnings=tuple(warnings),
    )


def endpoint_preview(protocol: str, base_url: str, auto_append_path: bool) -> dict[str, Any]:
    """JSON-able preview for the API (raises :class:`EndpointError` on invalid input)."""
    plan = build_endpoints(protocol, base_url, bool(auto_append_path))
    return {
        "auto_append_path": plan.auto_append_path,
        "request_url": plan.request_url,
        "models_url": plan.models_url,
        "warnings": list(plan.warnings),
    }


# --------------------------------------------------------------------- manual
def default_inner_transport() -> httpx.AsyncBaseTransport:
    """The real network transport (tests patch this to inject a mock)."""
    return httpx.AsyncHTTPTransport()


class FixedUrlTransport(httpx.AsyncBaseTransport):
    """Rewrites every request the SDK builds to one fixed, complete URL.

    The SDK appends its own path (``/chat/completions`` ...) to its base URL;
    this transport discards that and sends to ``request_url`` instead, keeping
    method, headers (``Host`` is updated), body and the streamed response.
    """

    def __init__(
        self, request_url: str, inner: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.request_url = httpx.URL(request_url)
        self._inner = inner

    @property
    def inner(self) -> httpx.AsyncBaseTransport:
        if self._inner is None:
            self._inner = default_inner_transport()
        return self._inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = self.request_url
        request.url = url
        request.headers["Host"] = url.netloc.decode("ascii")
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        if self._inner is not None:
            await self._inner.aclose()


def fixed_http_client(request_url: str) -> httpx.AsyncClient:
    """An ``httpx`` client for SDKs that must POST to exactly ``request_url``.

    Redirects are not followed (the key would travel with them). Per-request
    timeouts come from the SDK.
    """
    return httpx.AsyncClient(
        transport=FixedUrlTransport(request_url),
        follow_redirects=False,
        timeout=httpx.Timeout(600.0, connect=10.0),
    )
