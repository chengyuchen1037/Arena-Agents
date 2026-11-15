"""Behaviour shared by all protocol adapters: retry, client cache, stream view."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable

import httpx
import openai

from app.llm import types as _types
from app.llm.types import LLMResponse, estimate_text_tokens

try:  # the Anthropic SDK is optional at import time
    import anthropic as _anthropic
except ImportError:  # pragma: no cover - exercised only without the dependency
    _anthropic = None

logger = logging.getLogger(__name__)

DEFAULT_RETRY_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BASE_DELAY = 0.5
DEFAULT_RETRY_MAX_DELAY = 8.0
DEFAULT_RETRY_JITTER = 0.25
DEFAULT_RETRY_AFTER_CAP = 60.0

_RETRYABLE_STATUS = frozenset({408, 429})

_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (openai.APIConnectionError,)
_STATUS_ERRORS: tuple[type[BaseException], ...] = (openai.APIStatusError,)
if _anthropic is not None:
    _CONNECTION_ERRORS += (_anthropic.APIConnectionError,)
    _STATUS_ERRORS += (_anthropic.APIStatusError,)


def as_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class UpstreamStreamError(Exception):
    """An error event delivered *inside* a stream (Responses / Anthropic ``error``)."""

    def __init__(self, message: str, *, retryable: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status_code = status_code


def retry_after_seconds(exc: BaseException) -> float | None:
    """Seconds the server asked us to wait (``Retry-After`` / ``retry-after-ms``)."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw_ms = headers.get("retry-after-ms")
        if raw_ms is not None:
            ms = float(raw_ms)
            if math.isfinite(ms) and ms >= 0:
                return ms / 1000.0
        raw = headers.get("retry-after")
    except (TypeError, ValueError, AttributeError):
        return None
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        try:
            when = parsedate_to_datetime(str(raw))
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def is_retryable_error(exc: BaseException) -> bool:
    """Connection errors, timeouts, 408/429 and 5xx are transient; other 4xx are not."""
    if isinstance(exc, UpstreamStreamError):
        return exc.retryable
    if isinstance(exc, _STATUS_ERRORS):
        status = int(getattr(exc, "status_code", 0) or 0)
        return status in _RETRYABLE_STATUS or status >= 500
    if isinstance(exc, _CONNECTION_ERRORS):  # includes the SDKs' timeout errors
        return True
    # Raw httpx failures can surface while iterating a stream.
    return isinstance(exc, httpx.TransportError)


def status_code_of(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    return int(code) if isinstance(code, int) else None


def is_status_error(exc: BaseException) -> bool:
    return isinstance(exc, _STATUS_ERRORS)


def error_text(exc: BaseException) -> str:
    """Lower-cased description of an API error (for matching rejected parameters)."""
    return f"{getattr(exc, 'message', '')} {getattr(exc, 'body', '')}".lower()


REDACTED = "***"


def redact_exception(exc: BaseException, secret: str) -> None:
    """Scrub ``secret`` (an API key) from an exception's text, in place.

    Some gateways echo the offending key in 401/403 bodies; the exception text
    ends up in logs and UI records, so it must not carry the key.
    """
    if not secret or len(secret) < 6:
        return
    try:
        exc.args = tuple(a.replace(secret, REDACTED) if isinstance(a, str) else a for a in exc.args)
        message = getattr(exc, "message", None)
        if isinstance(message, str) and secret in message:
            exc.message = message.replace(secret, REDACTED)  # type: ignore[attr-defined]
        body = getattr(exc, "body", None)
        if body is not None and secret in repr(body):
            exc.body = json.loads(json.dumps(body, default=str).replace(secret, REDACTED))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - scrubbing must never mask the real error
        logger.debug("could not redact exception text", exc_info=True)


@dataclass
class RetryPolicy:
    max_attempts: int = DEFAULT_RETRY_MAX_ATTEMPTS
    base_delay: float = DEFAULT_RETRY_BASE_DELAY
    max_delay: float = DEFAULT_RETRY_MAX_DELAY
    jitter: float = DEFAULT_RETRY_JITTER
    retry_after_cap: float = DEFAULT_RETRY_AFTER_CAP

    @classmethod
    def from_cfg(cls, retry_cfg: dict[str, Any] | None) -> "RetryPolicy":
        retry_cfg = retry_cfg or {}
        return cls(
            max_attempts=max(1, as_int(retry_cfg.get("max_attempts"), DEFAULT_RETRY_MAX_ATTEMPTS)),
            base_delay=max(0.0, as_float(retry_cfg.get("base_delay_seconds"), DEFAULT_RETRY_BASE_DELAY)),
            max_delay=max(0.0, as_float(retry_cfg.get("max_delay_seconds"), DEFAULT_RETRY_MAX_DELAY)),
            jitter=min(1.0, max(0.0, as_float(retry_cfg.get("jitter"), DEFAULT_RETRY_JITTER))),
            retry_after_cap=max(
                0.0, as_float(retry_cfg.get("max_retry_after_seconds"), DEFAULT_RETRY_AFTER_CAP)
            ),
        )

    def delay(
        self,
        failures: int,
        retry_after: float | None = None,
        rand: Callable[[], float] = random.random,
    ) -> float:
        """Delay before retry number ``failures`` (1-based): exponential + jitter.

        ``jitter`` is the maximum fraction shaved off the exponential delay, so the
        result never exceeds ``max_delay``. A server ``Retry-After`` raises the delay
        to at least that value (itself capped by ``retry_after_cap``).
        """
        exponent = max(0, failures - 1)
        base = min(self.max_delay, self.base_delay * (2 ** min(exponent, 30)))
        delay = base * (1.0 - self.jitter * rand())
        if retry_after is not None:
            delay = max(delay, min(retry_after, self.retry_after_cap))
        return delay


@dataclass
class StreamProgress:
    """Set once a stream has produced anything visible (so it must not be retried)."""

    emitted: bool = False


async def run_with_retry(
    attempt: Callable[[StreamProgress], Awaitable[LLMResponse]],
    *,
    policy: RetryPolicy,
    downgrade: Callable[[BaseException], bool] | None = None,
    rand: Callable[[], float] = random.random,
    label: str = "LLM",
) -> LLMResponse:
    """Run ``attempt`` with transient-error retries.

    * ``downgrade(exc)`` may edit the request and return True to retry at once
      without spending the retry budget (parameter rejected by the upstream);
    * retries only happen while the stream has produced nothing visible.
    """
    failures = 0
    while True:
        progress = StreamProgress()
        try:
            return await attempt(progress)
        except Exception as exc:  # noqa: BLE001 - classified below
            if downgrade is not None and downgrade(exc):
                continue
            if (
                progress.emitted
                or not is_retryable_error(exc)
                or failures + 1 >= policy.max_attempts
            ):
                raise
            failures += 1
            delay = policy.delay(failures, retry_after_seconds(exc), rand)
            # Log the error class and status only: upstream messages can echo credentials.
            logger.warning(
                "%s request failed (%s, status=%s); retry %d/%d in %.2fs",
                label,
                type(exc).__name__,
                status_code_of(exc),
                failures,
                policy.max_attempts - 1,
                delay,
            )
            await asyncio.sleep(delay)


# --------------------------------------------------------------------- clients
class ClientHandle:
    """A shared SDK client plus bookkeeping for safe retirement."""

    __slots__ = ("client", "key", "loop", "inflight", "retired", "closed", "slot")

    def __init__(
        self,
        client: Any,
        key: tuple[Any, ...],
        loop: asyncio.AbstractEventLoop,
        slot: str = "",
    ) -> None:
        self.client = client
        self.key = key
        self.loop = loop
        self.slot = slot
        self.inflight = 0
        self.retired = False
        self.closed = False


class ClientCache:
    """One live SDK client per slot (provider). A slot is rebuilt only when its key changes.

    Retired clients are closed once their in-flight requests have finished, so a
    key rotation never breaks a running stream.
    """

    def __init__(self) -> None:
        self._handles: dict[str, ClientHandle] = {}

    def __len__(self) -> int:
        return len(self._handles)

    def get(self, slot: str) -> ClientHandle | None:
        return self._handles.get(slot)

    async def acquire(
        self, slot: str, key: tuple[Any, ...], create: Callable[[], Any]
    ) -> ClientHandle:
        loop = asyncio.get_running_loop()
        handle = self._handles.get(slot)
        stale: ClientHandle | None = None
        if handle is not None and (handle.key != key or handle.loop is not loop):
            stale = handle
            handle = None
        if handle is None:
            handle = ClientHandle(create(), key, loop, slot)
            self._handles[slot] = handle
        handle.inflight += 1
        if stale is not None:
            await self.retire(stale)
        return handle

    async def release(self, handle: ClientHandle) -> None:
        handle.inflight -= 1
        if handle.retired and handle.inflight <= 0:
            await self._close(handle)

    async def retire(self, handle: ClientHandle) -> None:
        """Close ``handle`` now, or as soon as its in-flight requests finish."""
        handle.retired = True
        if handle.inflight <= 0:
            await self._close(handle)

    async def retire_slot(self, slot: str) -> None:
        handle = self._handles.pop(slot, None)
        if handle is not None:
            await self.retire(handle)

    @staticmethod
    async def _close(handle: ClientHandle) -> None:
        if handle.closed:
            return
        handle.closed = True
        try:
            await handle.client.close()
        except Exception:  # noqa: BLE001 - closing must never break a request
            logger.debug("Failed to close retired LLM client", exc_info=True)

    async def aclose(self) -> None:
        handles, self._handles = list(self._handles.values()), {}
        for handle in handles:
            await self.retire(handle)


# ----------------------------------------------------------------- stream view
# Markers that make the (comparatively expensive) inline-think / DSML pass
# necessary. ``_MARKER_OVERLAP`` is len("<think>") - 1: enough to catch a marker
# split across two stream chunks.
_THINK_MARKERS = ("<think>", "DSML")
_MARKER_OVERLAP = 6


class StreamAccumulator:
    """Cumulative (thought, content) view of a stream, built without rescanning.

    * pieces are appended to lists and only joined when a snapshot is needed
      (never, if there is no ``on_chunk`` callback);
    * the ``<think>`` / DSML pass (:func:`split_inline_think`) only runs once a
      marker has actually been seen in the stream; before that the view is just
      the stripped native thought / content, which is exactly what
      ``split_inline_think`` returns for marker-free text;
    * snapshots are cached while nothing changed.
    """

    __slots__ = (
        "_raw",
        "_raw_pending",
        "_native",
        "_native_pending",
        "_tail",
        "_marker_seen",
        "_snapshot",
    )

    def __init__(self) -> None:
        self._raw = ""
        self._raw_pending: list[str] = []
        self._native = ""
        self._native_pending: list[str] = []
        self._tail = ""
        self._marker_seen = False
        self._snapshot: tuple[str, str] | None = ("", "")

    @property
    def marker_seen(self) -> bool:
        return self._marker_seen

    def add_content(self, piece: str) -> None:
        if not piece:
            return
        self._raw_pending.append(piece)
        self._snapshot = None
        if not self._marker_seen:
            window = self._tail + piece
            if any(marker in window for marker in _THINK_MARKERS):
                self._marker_seen = True
            else:
                self._tail = window[-_MARKER_OVERLAP:]

    def add_thought(self, piece: str) -> None:
        if not piece:
            return
        self._native_pending.append(piece)
        self._snapshot = None

    @property
    def raw(self) -> str:
        if self._raw_pending:
            self._raw += "".join(self._raw_pending)
            self._raw_pending.clear()
        return self._raw

    @property
    def native(self) -> str:
        if self._native_pending:
            self._native += "".join(self._native_pending)
            self._native_pending.clear()
        return self._native

    def snapshot(self) -> tuple[str, str]:
        if self._snapshot is None:
            if self._marker_seen:
                # looked up on the module so tests can observe how often it runs
                self._snapshot = _types.split_inline_think(self.raw, self.native)
            else:
                self._snapshot = (self.native.strip(), self.raw.strip())
        return self._snapshot

    def final(self) -> tuple[str, str, list[Any]]:
        """``(thought, content, dsml_tool_calls)`` once the stream is complete."""
        cleaned_raw, dsml_tools = _types.extract_dsml_tool_calls(self.raw)
        thought, content = _types.split_inline_think(cleaned_raw, self.native)
        return thought, content, dsml_tools


def estimate_prompt_tokens(*parts: Any) -> int:
    blob = "".join(json.dumps(p, ensure_ascii=False, default=str) for p in parts if p)
    return estimate_text_tokens(blob)


def estimate_completion_tokens(resp: LLMResponse) -> int:
    out_blob = (
        resp.thought
        + resp.content
        + "".join(tc.name + tc.arguments_json for tc in resp.tool_calls)
    )
    return estimate_text_tokens(out_blob)
