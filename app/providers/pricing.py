"""Model price table ("price source"): parse, match and store the LiteLLM price list (v0.12.1, E12).

The default source is LiteLLM's ``model_prices_and_context_window.json``.  A refresh downloads it (https
only, SSRF-guarded like model discovery, size capped, no redirects) and keeps a *reduced* copy in the
data directory (``pricing.v1.json``, plain JSON: it is public data).  :meth:`PriceStore.match` then
finds the entry of a provider's model so the UI can fill in the four prices and the context window; the
user can still edit them.

Units: the source has USD **per token**, everything here is USD **per million tokens**.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.llm.discovery import DiscoveryError, _pinned_url, parse_base_url, resolve_and_check
from app.storage._atomic import atomic_write
from app.storage.errors import StorageError
from app.storage.paths import get_data_dir

logger = logging.getLogger(__name__)

DEFAULT_PRICE_SOURCE_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
PRICE_FILE = "pricing.v1.json"
MAX_SOURCE_BYTES = 20 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 30.0
MAX_ENTRIES = 50_000
_CHAT_MODES = {None, "", "chat", "responses", "completion"}

#: Substring of a Provider's host -> LiteLLM ``litellm_provider`` name.
HOST_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("deepseek", "deepseek"),
    ("openai.com", "openai"),
    ("anthropic.com", "anthropic"),
    ("openrouter.ai", "openrouter"),
    ("x.ai", "xai"),
    ("groq.com", "groq"),
    ("mistral.ai", "mistral"),
    ("googleapis.com", "gemini"),
    ("together.xyz", "together_ai"),
    ("together.ai", "together_ai"),
    ("fireworks.ai", "fireworks_ai"),
    ("moonshot", "moonshot"),
    ("dashscope", "dashscope"),
    ("siliconflow", "siliconflow"),
    ("volces.com", "volcengine"),
    ("bigmodel.cn", "zai"),
    ("z.ai", "zai"),
    ("perplexity.ai", "perplexity"),
    ("cerebras.ai", "cerebras"),
    ("deepinfra.com", "deepinfra"),
    ("nebius", "nebius"),
    ("novita.ai", "novita"),
    ("hyperbolic", "hyperbolic"),
    ("sambanova", "sambanova"),
)
PROTOCOL_PROVIDERS = {"anthropic_messages": ("anthropic",), "openai_responses": ("openai",)}

_DATE_SUFFIX = re.compile(r"-(\d{8}|\d{4}-\d{2}-\d{2}|\d{6})$")


@dataclass(frozen=True)
class PriceEntry:
    key: str
    provider: str
    input_price_per_m: float
    output_price_per_m: float
    cache_read_price_per_m: float | None
    cache_write_price_per_m: float | None
    context_window_tokens: int | None

    def suggestion(self) -> dict[str, Any]:
        """The ``suggested`` / ``match`` payload of the API (contracts section 10.3 / 10.8)."""
        return {
            "input_price_per_m": self.input_price_per_m,
            "output_price_per_m": self.output_price_per_m,
            "cache_read_price_per_m": self.cache_read_price_per_m,
            "cache_write_price_per_m": self.cache_write_price_per_m,
            "context_window_tokens": self.context_window_tokens,
            "price_source": f"litellm:{self.key}",
            "key": self.key,
        }


def _per_million(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value < 0 or value == float("inf"):
        return None
    return round(float(value) * 1_000_000, 6)


def parse_price_table(payload: Any) -> dict[str, PriceEntry]:
    """Reduce a LiteLLM price list to the chat-style entries with usable prices."""
    if not isinstance(payload, dict):
        raise ValueError("价格源内容不是 JSON 对象")
    entries: dict[str, PriceEntry] = {}
    for key, raw in payload.items():
        if not isinstance(key, str) or not isinstance(raw, dict) or key == "sample_spec":
            continue
        if raw.get("mode") not in _CHAT_MODES:
            continue
        price_in = _per_million(raw.get("input_cost_per_token"))
        price_out = _per_million(raw.get("output_cost_per_token"))
        if price_in is None or price_out is None:
            continue
        window = raw.get("max_input_tokens")
        if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
            window = raw.get("max_tokens")
        if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
            window = None
        entries[key] = PriceEntry(
            key=key,
            provider=str(raw.get("litellm_provider") or "") if isinstance(raw.get("litellm_provider"), str) else "",
            input_price_per_m=price_in,
            output_price_per_m=price_out,
            cache_read_price_per_m=_per_million(raw.get("cache_read_input_token_cost")),
            cache_write_price_per_m=_per_million(raw.get("cache_creation_input_token_cost")),
            context_window_tokens=window,
        )
        if len(entries) >= MAX_ENTRIES:
            break
    return entries


def provider_names_for(base_url: str, protocol: str) -> list[str]:
    """LiteLLM provider names a Provider most likely belongs to (by host, then by protocol)."""
    names: list[str] = []
    host = ""
    try:
        host = (parse_base_url(base_url)[1] or "").lower()
    except DiscoveryError:
        host = ""
    for needle, name in HOST_PROVIDERS:
        if needle in host and name not in names:
            names.append(name)
    for name in PROTOCOL_PROVIDERS.get(protocol, ()):
        if name not in names:
            names.append(name)
    return names


def _variants(model_id: str) -> list[str]:
    out = [model_id]
    stripped = _DATE_SUFFIX.sub("", model_id)
    if stripped and stripped != model_id:
        out.append(stripped)
    return out


class PriceTable:
    def __init__(self, entries: dict[str, PriceEntry]) -> None:
        self.entries = entries
        self._lower = {key.lower(): key for key in entries}

    def __len__(self) -> int:
        return len(self.entries)

    def match(self, model_id: str, providers: Sequence[str] = ()) -> PriceEntry | None:
        """Find the entry of ``model_id``: exact key, ``<provider>/<id>``, the id without a date suffix,
        case-insensitively, and finally a *unique* ``…/<id>`` key (or the best one of the providers)."""
        model_id = (model_id or "").strip()
        if not model_id or not self.entries:
            return None
        for variant in _variants(model_id):
            tries = [variant, *(f"{p}/{variant}" for p in providers)]
            for candidate in tries:
                if candidate in self.entries:
                    return self.entries[candidate]
            for candidate in tries:
                real = self._lower.get(candidate.lower())
                if real is not None:
                    return self.entries[real]
            suffix = "/" + variant.lower()
            found = [self.entries[key] for lower, key in self._lower.items() if lower.endswith(suffix)]
            if not found:
                continue
            preferred = [e for e in found if e.provider in providers]
            if preferred:
                return sorted(preferred, key=lambda e: (len(e.key), e.key))[0]
            if len(found) == 1:
                return found[0]
        return None


# ------------------------------------------------------------------------------------ fetching


def validate_source_url(url: str) -> str:
    """A price source must be a plain https URL (no credentials, query or fragment)."""
    value = (url or "").strip()
    if not value.lower().startswith("https://"):
        raise ValueError("价格源 URL 必须以 https:// 开头。")
    try:
        parse_base_url(value)
    except DiscoveryError as exc:
        raise ValueError(str(exc).replace("Base URL", "价格源 URL")) from None
    if len(value) > 500:
        raise ValueError("价格源 URL 过长。")
    return value


async def fetch_source(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Any = None,
    allow_private: bool | None = None,
) -> Any:
    """Download and parse the JSON of ``url`` with the same guards as model discovery."""
    url = validate_source_url(url)
    scheme, host, port, _ = parse_base_url(url)
    addresses = await resolve_and_check(host, port, allow_private=allow_private, resolver=resolver)
    try:
        ipaddress.ip_address(host)
        is_literal = True
    except ValueError:
        is_literal = False
    request_url = url if is_literal else _pinned_url(url, addresses[0])
    headers = {"Accept": "application/json", "User-Agent": "Arena-Agent/0.12"}
    extensions: dict[str, Any] = {}
    if not is_literal:
        headers["Host"] = host if port == 443 else f"{host}:{port}"
        extensions["sni_hostname"] = host

    body = bytearray()
    try:
        async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS),
            ) as client:
                async with client.stream("GET", request_url, headers=headers, extensions=extensions) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        raise DiscoveryError(f"价格源返回重定向（HTTP {status}），出于安全考虑已拒绝跟随。", kind="redirect")
                    if status >= 400:
                        raise DiscoveryError(f"价格源返回错误（HTTP {status}）。", kind="status", status=status)
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > MAX_SOURCE_BYTES:
                        raise DiscoveryError("价格源文件过大，已拒绝读取。", kind="too_large")
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_SOURCE_BYTES:
                            raise DiscoveryError("价格源文件过大，已拒绝读取。", kind="too_large")
    except DiscoveryError:
        raise
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise DiscoveryError("下载价格源超时。", kind="timeout") from None
    except httpx.HTTPError:
        raise DiscoveryError("无法连接到价格源，请检查网络或 URL。", kind="network") from None
    try:
        return json.loads(bytes(body))
    except (ValueError, RecursionError):
        raise DiscoveryError("价格源内容不是有效的 JSON。", kind="invalid_response") from None


# ------------------------------------------------------------------------------------ the store


class PriceStore:
    """The price table of one data directory (workspace): source URL, reduced entries, status."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir).resolve()
        self._path = self._data_dir / PRICE_FILE
        self._lock = threading.RLock()
        self._table: PriceTable | None = None
        self._meta: dict[str, Any] = {"url": DEFAULT_PRICE_SOURCE_URL, "fetched_at": None, "error": None}
        self._loaded = False

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    # ----------------------------------------------------------------------------- persistence
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict) or raw.get("schema") != 1:
            return
        url = raw.get("url")
        if isinstance(url, str) and url.startswith("https://"):
            self._meta["url"] = url
        fetched = raw.get("fetched_at")
        self._meta["fetched_at"] = fetched if isinstance(fetched, str) else None
        entries: dict[str, PriceEntry] = {}
        stored = raw.get("entries")
        if isinstance(stored, dict):
            for key, row in stored.items():
                if not (isinstance(key, str) and isinstance(row, list) and len(row) == 6):
                    continue
                price_in, price_out, cache_read, cache_write, window, provider = row
                if not isinstance(price_in, (int, float)) or not isinstance(price_out, (int, float)):
                    continue
                entries[key] = PriceEntry(
                    key=key,
                    provider=str(provider or ""),
                    input_price_per_m=float(price_in),
                    output_price_per_m=float(price_out),
                    cache_read_price_per_m=None if cache_read is None else float(cache_read),
                    cache_write_price_per_m=None if cache_write is None else float(cache_write),
                    context_window_tokens=int(window) if isinstance(window, int) and window > 0 else None,
                )
        if entries:
            self._table = PriceTable(entries)

    def _save(self) -> None:
        entries = self._table.entries if self._table is not None else {}
        payload = {
            "schema": 1,
            "url": self._meta["url"],
            "fetched_at": self._meta["fetched_at"],
            "entries": {
                key: [
                    e.input_price_per_m,
                    e.output_price_per_m,
                    e.cache_read_price_per_m,
                    e.cache_write_price_per_m,
                    e.context_window_tokens,
                    e.provider,
                ]
                for key, e in entries.items()
            },
        }
        try:
            atomic_write(self._path, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (OSError, StorageError):
            logger.warning("Could not save the price table", exc_info=True)

    # ----------------------------------------------------------------------------- the API
    def status(self) -> dict[str, Any]:
        with self._lock:
            self._load()
            return {
                "url": self._meta["url"],
                "default_url": DEFAULT_PRICE_SOURCE_URL,
                "fetched_at": self._meta["fetched_at"],
                "entries": len(self._table) if self._table is not None else 0,
                "error": self._meta["error"],
            }

    def set_url(self, url: str) -> dict[str, Any]:
        value = validate_source_url(url or DEFAULT_PRICE_SOURCE_URL)
        with self._lock:
            self._load()
            if value != self._meta["url"]:
                self._meta.update({"url": value, "fetched_at": None, "error": None})
                self._table = None  # prices of another source are not comparable: refresh first
                self._save()
            return self.status()

    async def refresh(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Any = None,
        allow_private: bool | None = None,
    ) -> dict[str, Any]:
        """Download the table; on failure the old table stays and ``error`` explains why."""
        with self._lock:
            self._load()
            url = self._meta["url"]
        try:
            payload = await fetch_source(url, transport=transport, resolver=resolver, allow_private=allow_private)
            entries = await asyncio.to_thread(parse_price_table, payload)
            if not entries:
                raise ValueError("价格源里没有可识别的模型价格。")
        except DiscoveryError as exc:
            return self._failed(str(exc))
        except (ValueError, TypeError) as exc:
            return self._failed(str(exc) or "价格源内容无法解析。")
        with self._lock:
            self._table = PriceTable(entries)
            self._meta["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._meta["error"] = None
            self._save()
            return self.status()

    def _failed(self, message: str) -> dict[str, Any]:
        with self._lock:
            self._meta["error"] = message
            return self.status()

    def match(self, provider: Any, model_id: str) -> PriceEntry | None:
        """The table entry of ``model_id`` as offered by ``provider`` (a ProviderConfig or any object with
        ``base_url`` / ``protocol``), or ``None`` (no table yet / no such model)."""
        with self._lock:
            self._load()
            table = self._table
        if table is None:
            return None
        names = provider_names_for(str(getattr(provider, "base_url", "") or ""), str(getattr(provider, "protocol", "") or ""))
        return table.match(model_id, names)


_store: PriceStore | None = None
_store_lock = threading.Lock()


def get_price_store() -> PriceStore:
    global _store
    with _store_lock:
        data_dir = get_data_dir()
        if _store is None or _store.data_dir != Path(data_dir).resolve():
            _store = PriceStore(data_dir)
        return _store


def reset_price_store() -> None:
    global _store
    with _store_lock:
        _store = None


__all__ = [
    "DEFAULT_PRICE_SOURCE_URL",
    "PriceEntry",
    "PriceStore",
    "PriceTable",
    "get_price_store",
    "parse_price_table",
    "provider_names_for",
    "reset_price_store",
    "validate_source_url",
]
