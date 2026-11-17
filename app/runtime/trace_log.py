"""Plaintext JSONL trace of what the agents think and do (decisions D5 and D11).

Thoughts, tool calls (arguments and results), receipts, wake-ups, cancellations and errors are
written here and **never** sent to the UI.  The file is plain UTF-8 JSON Lines, one event per line::

    {"ts": "2026-11-16T23:30:18.123-08:00", "event": "tool_call", "agent_id": "a1", ...fields}

Layout: ``<dir>/<session_id>/trace-0001.jsonl``; when a file would grow past ``max_bytes`` (default
about 20 MB) the next one (``trace-0002.jsonl`` ...) is started.  Reopening the same session continues
with the newest file.  Secrets are scrubbed *before* anything is written: ``sk-...`` keys,
``Bearer ...`` tokens, ``api_key=...`` style assignments, values under sensitive dictionary keys and
every secret registered with :meth:`TraceLog.add_secret`.

Writing is synchronous and tiny (append + flush) and **never raises**: a full disk or a missing
directory costs trace lines, never a game.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import threading
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_FIELD_CHARS = 50_000
REDACTED = "***"

_FILE_RE = re.compile(r"^trace-(\d{4,})\.jsonl$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")

# Text patterns (applied to every string that is logged).
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_\-]{6,}")
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]{6,}")
_ASSIGN_RE = re.compile(
    r"""(?ix)
    \b(api[_-]?key|x-api-key|authorization)   # the name
    (["']?\s*[:=]\s*["']?)                    # separator, optional quotes
    (?!bearer\b)([^\s"',;}\]]{6,})            # the value (a Bearer value is handled above)
    """
)
# Dictionary keys whose value is always replaced.
_SENSITIVE_KEYS = frozenset(
    {"api_key", "apikey", "api-key", "x-api-key", "authorization", "password", "secret", "access_token",
     "refresh_token", "master_key"}
)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Scrub credentials from one string."""
    if not text:
        return text
    for secret in secrets:
        if secret and len(secret) >= 6 and secret in text:
            text = text.replace(secret, REDACTED)
    text = _SK_RE.sub("sk-" + REDACTED, text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _ASSIGN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    return text


class TraceLog:
    """Rolling, scrubbing JSONL writer; implements :class:`app.runtime.ports.TraceSink`."""

    def __init__(
        self,
        dir: str | os.PathLike[str],  # noqa: A002 - mirrors the documented signature
        session_id: str,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_field_chars: int = DEFAULT_MAX_FIELD_CHARS,
        secrets: Iterable[str] = (),
    ) -> None:
        safe_id = _SAFE_NAME_RE.sub("_", str(session_id)).lstrip(".") or "session"
        self.root = Path(dir)
        self.directory = self.root / safe_id
        self.session_id = str(session_id)
        self.max_bytes = max(64, int(max_bytes))
        self.max_field_chars = max(64, int(max_field_chars))
        self._secrets: list[str] = [s for s in secrets if s]
        self._lock = threading.Lock()
        self._handle: Any = None
        self._index = 0
        self._size = 0
        self.written = 0  # events written
        self.dropped = 0  # events lost to I/O errors
        self._warned = False

    # ------------------------------------------------------------------ secrets --
    def add_secret(self, value: str) -> None:
        """Always scrub this exact string (e.g. a provider API key that has no recognizable shape)."""
        if value and len(value) >= 6 and value not in self._secrets:
            self._secrets.append(value)

    # ------------------------------------------------------------------ writing --
    def write(self, event: str, agent_id: str, **fields: Any) -> None:
        """Append one event.  Never raises."""
        try:
            record: dict[str, Any] = {
                "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "event": event,
                "agent_id": agent_id,
            }
            for key, value in fields.items():
                record[key if key not in record else f"field_{key}"] = value
            line = json.dumps(self._scrub(record), ensure_ascii=False, separators=(",", ":"))
            data = (line + "\n").encode("utf-8")
        except Exception:  # noqa: BLE001 - tracing must never break a run
            self.dropped += 1
            logger.debug("could not serialize trace event %r", event, exc_info=True)
            return
        with self._lock:
            try:
                self._append(data)
                self.written += 1
            except OSError as exc:
                self.dropped += 1
                self._drop_handle()
                if not self._warned:
                    self._warned = True
                    logger.warning("trace log write failed (%s); further failures are silent", exc)

    def _append(self, data: bytes) -> None:
        if self._handle is None:
            self._open_latest()
        if self._size > 0 and self._size + len(data) > self.max_bytes:
            self._roll()
        self._handle.write(data)
        self._handle.flush()
        self._size += len(data)

    def _open_latest(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                os.chmod(self.directory, 0o700)
            except OSError:  # pragma: no cover - best effort
                pass
        indexes = sorted(
            int(m.group(1)) for p in self.directory.iterdir() if (m := _FILE_RE.match(p.name))
        )
        self._index = indexes[-1] if indexes else 1
        self._open_current()

    def _open_current(self) -> None:
        path = self.current_path
        self._handle = open(path, "ab")  # noqa: SIM115 - kept open across writes
        self._size = path.stat().st_size

    def _roll(self) -> None:
        self._drop_handle()
        self._index += 1
        self._open_current()

    def _drop_handle(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover - best effort
                pass

    # ----------------------------------------------------------------- inspecting --
    @property
    def current_path(self) -> Path:
        return self.directory / f"trace-{max(1, self._index):04d}.jsonl"

    def files(self) -> list[Path]:
        """Every trace file of this session, oldest first."""
        if not self.directory.is_dir():
            return []
        return sorted(p for p in self.directory.iterdir() if _FILE_RE.match(p.name))

    def close(self) -> None:
        """Close the file handle (a later ``write`` reopens it)."""
        with self._lock:
            self._drop_handle()

    def __enter__(self) -> "TraceLog":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ scrubbing --
    def _scrub(self, value: Any, depth: int = 0) -> Any:
        """A JSON-safe, credential-free copy of ``value``."""
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            text = redact_text(value, self._secrets)
            if len(text) > self.max_field_chars:
                text = f"{text[: self.max_field_chars]}…[已截断 {len(text) - self.max_field_chars} 字]"
            return text
        if depth >= 12:
            return "…"
        if isinstance(value, BaseModel):
            return self._scrub(value.model_dump(mode="json"), depth + 1)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return self._scrub(dataclasses.asdict(value), depth + 1)
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, item in value.items():
                name = str(key)
                if name.lower() in _SENSITIVE_KEYS and item not in (None, ""):
                    out[name] = REDACTED
                else:
                    out[name] = self._scrub(item, depth + 1)
            return out
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self._scrub(item, depth + 1) for item in value]
        if isinstance(value, bytes):
            return self._scrub(value.decode("utf-8", errors="replace"), depth + 1)
        return self._scrub(str(value), depth + 1)
