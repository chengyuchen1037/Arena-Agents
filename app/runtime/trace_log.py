"""Plaintext JSONL trace of what the agents think and do (decisions D5 and D11).

Thoughts, tool calls (arguments and results), receipts, wake-ups, cancellations and errors are
written here and **never pushed** to the UI: no SSE event carries them, no snapshot or replay contains them.
(v0.13.1, G26: the two events of the *thinking record* - ``thought`` and ``content`` of ONE agent - can be read
on request, newest page first, through ``GET /api/agents/{id}/thoughts``, and the error log is rebuilt from the
``llm_call`` / ``error`` / ``retry`` / ``give_up`` events: see :func:`read_thoughts` and
:mod:`app.runtime.call_log`.  Both only read this file, from its end, in blocks.)
The file is plain UTF-8 JSON Lines, one event per line::

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

import contextlib
import dataclasses
import json
import logging
import math
import os
import re
import threading
from collections.abc import Collection, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_FIELD_CHARS = 50_000
REDACTED = "***"

_FILE_RE = re.compile(r"^trace-([0-9]{4,})\.jsonl$")  # (ASCII digits only: ``\d`` also takes the digits of other scripts)
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


def trace_dir(root: str | os.PathLike[str], session_id: str) -> Path:
    """``<root>/<session id>``: where the trace files of one game live (the id is made safe as a file name)."""
    safe_id = _SAFE_NAME_RE.sub("_", str(session_id)).lstrip(".") or "session"
    return Path(root) / safe_id


def trace_files(directory: str | os.PathLike[str]) -> list[tuple[int, Path]]:
    """``[(index, path)]`` of the trace files in ``directory``, oldest first (``[]``: there are none)."""
    found: list[tuple[int, Path]] = []
    try:
        for path in Path(directory).iterdir():
            match = _FILE_RE.match(path.name)
            if match:
                found.append((int(match.group(1)), path))
    except OSError:
        return []
    return sorted(found, key=lambda item: item[0])


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
        self.root = Path(dir)
        self.directory = trace_dir(self.root, session_id)
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

    def redact(self, text: str) -> str:
        """``text`` without any credential this log knows (the pass every string gets before it is written)."""
        return redact_text(text, self._secrets)

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

    @property
    def is_open(self) -> bool:
        """The log holds its file open right now (the first ``write`` opens it, :meth:`close` lets go of it).

        On Windows an open file blocks moving or deleting the logs folder, so whoever writes to a log that is *not*
        running (the call log, for a review or a "complete it" call, after a pause) closes it again when it was not
        open before - see :class:`app.runtime.call_log.CallLog`."""
        return self._handle is not None

    def close(self) -> None:
        """Close the file handle (a later ``write`` reopens it)."""
        with self._lock:
            self._drop_handle()

    def __enter__(self) -> "TraceLog":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown / forgotten close()
        try:
            self._drop_handle()
        except Exception:  # noqa: BLE001
            pass

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


# ====================================================================================== reading, newest first ==
# A trace file can reach 20 MB and a game has several: nothing below loads a file whole.  The files are read
# backwards in blocks, a line at a time; a last line without its newline (``TraceLog`` may be writing it right now,
# or a cursor cut it) is skipped.  Everything here only reads: it takes no lock and never touches the writer, and
# a file that cannot be opened (an import replaced the directory, another process holds it) counts as empty.
READ_BLOCK_BYTES = 256 * 1024
#: How much of one game's trace a single request may look at.  A 20 MB file is scanned in a fraction of a second;
#: the limit only keeps a pathological trace from occupying a worker for good (the answer then says where to go on).
DEFAULT_SCAN_BYTES = 192 * 1024 * 1024

_CURSOR_RE = re.compile(r"([0-9]{1,9}):([0-9]{1,15})")  # (ASCII digits only: ``\d`` also takes the digits of other scripts)
THOUGHT_EVENTS = ("thought", "content")
#: What a trace line may put into a short field of an answer (a model id, a name ...): the trace of an imported game is
#: not validated, and nothing in it is a reason for a huge answer.
MAX_SHORT_CHARS = 120
#: ... and into a thought: what :class:`TraceLog` itself keeps of a field (it cuts at ``DEFAULT_MAX_FIELD_CHARS`` and adds
#: a short note) with some room to spare.
MAX_TEXT_CHARS = DEFAULT_MAX_FIELD_CHARS + 1000


def format_cursor(index: int, offset: int) -> str:
    """The position of a trace line as text: ``<file index>:<byte offset of its first byte>`` (also its id)."""
    return f"{index}:{offset}"


def parse_cursor(text: str | None) -> tuple[int, int] | None:
    """``(file index, offset)`` of a cursor made by :func:`format_cursor`; ``None`` when it is not one."""
    match = _CURSOR_RE.fullmatch(str(text or "").strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def iter_lines_backwards(
    path: str | os.PathLike[str], *, end: int | None = None, block_size: int = READ_BLOCK_BYTES
) -> Iterator[tuple[int, bytes]]:
    """The complete lines of ``path`` from the last one to the first: ``(offset of its first byte, the line
    without its newline)``; blank lines are left out.

    ``end`` limits the file to its first ``end`` bytes (a cursor).  What follows the last newline before the end of
    that range is not a complete line - the writer is still busy with it, or ``end`` cut it - and is skipped.  The
    file is read in blocks of ``block_size`` bytes and every block is searched for newlines once; a line that is longer
    than a block is joined from its pieces once, when it is whole - the work is linear in the size of the file, however
    long a single line is."""
    block_size = max(16, int(block_size))
    try:
        handle = open(path, "rb")  # noqa: SIM115 - closed by the ``with`` below
    except OSError:
        return
    with handle:
        try:
            size = os.fstat(handle.fileno()).st_size
        except OSError:
            return
        # (1) where the last complete line ends: just after the last newline in front of ``end``
        scan = size if end is None else max(0, min(int(end), size))
        limit = 0
        while scan > 0:
            low = max(0, scan - block_size)
            handle.seek(low)
            chunk = handle.read(scan - low)
            cut = chunk.rfind(b"\n")
            if cut >= 0:
                limit = low + cut + 1
                break
            scan = low
        # (2) the lines, last first.  Every block lies directly in front of the ones read before it.  ``head`` holds the
        # pieces of the line that is incomplete so far (its beginning is in a block that has not been read yet), the piece
        # read last at the end of the list: the line is joined - once - when a block with a newline in it completes it.
        head: list[bytes] = []
        start = limit
        while start > 0:
            low = max(0, start - block_size)
            handle.seek(low)
            chunk = handle.read(start - low)
            if len(chunk) != start - low:  # the file shrank under us (replaced): stop
                return
            parts = chunk.split(b"\n")
            if len(parts) == 1:  # no newline in this block: all of it belongs to the line in progress
                head.append(chunk)
                start = low
                if low == 0:  # ... and that line begins at the first byte of the file
                    line = b"".join(reversed(head))
                    if line:
                        yield 0, line
                continue
            # What follows the block's last newline is the beginning of the line in progress (or nothing): it is whole now.
            tail = parts[-1]
            position = start - len(tail)
            line = tail + b"".join(reversed(head)) if head else tail
            if line:
                yield position, line
            for index in range(len(parts) - 2, 0, -1):  # the lines that lie wholly inside the block
                position -= len(parts[index]) + 1
                if parts[index]:
                    yield position, parts[index]
            start = low
            if low == 0:  # the first part is a whole line as well: it begins at the first byte of the file
                head = []
                if parts[0]:
                    yield 0, parts[0]
            else:  # ... otherwise its beginning is in the next block to the front
                head = [parts[0]]


@dataclass
class ScanStats:
    """What :func:`scan_events_backwards` did (the caller reads it after, or while, iterating)."""

    scanned: int = 0  # bytes of the lines looked at
    position: tuple[int, int] | None = None  # (file index, offset) of the oldest line looked at
    truncated: bool = False  # stopped at ``max_bytes`` with lines left


def scan_events_backwards(
    directory: str | os.PathLike[str],
    events: Collection[str],
    *,
    agent_id: str | None = None,
    before: tuple[int, int] | None = None,
    max_bytes: int = DEFAULT_SCAN_BYTES,
    block_size: int = READ_BLOCK_BYTES,
    stats: ScanStats | None = None,
) -> Iterator[tuple[int, int, dict[str, Any]]]:
    """The records of the given ``events`` (of ``agent_id``, when given) in the trace files of ``directory``,
    newest first: ``(file index, line offset, record)``.

    ``before`` = a position: only lines in front of it.  A line that is not valid JSON, or not an object, is
    ignored.  After ``max_bytes`` of lines it stops and says so in ``stats.truncated``."""
    stats = stats if stats is not None else ScanStats()
    wanted = frozenset(events)
    if not wanted:
        return
    event_re = re.compile(rb'"event"\s*:\s*"(?:' + b"|".join(re.escape(name.encode("utf-8")) for name in sorted(wanted)) + rb')"')
    agent_re = (
        re.compile(rb'"agent_id"\s*:\s*' + re.escape(json.dumps(agent_id, ensure_ascii=False).encode("utf-8")))
        if agent_id is not None
        else None
    )
    for index, path in reversed(trace_files(directory)):
        end: int | None = None
        if before is not None:
            if index > before[0]:
                continue
            if index == before[0]:
                end = before[1]
        lines = iter_lines_backwards(path, end=end, block_size=block_size)
        with contextlib.closing(lines):  # (the file is let go of at once - also when the caller stops reading early)
            for offset, line in lines:
                if stats.scanned >= max_bytes:
                    stats.truncated = True
                    return
                stats.scanned += len(line) + 1
                stats.position = (index, offset)
                if not event_re.search(line) or (agent_re is not None and not agent_re.search(line)):
                    continue  # (a cheap look at the bytes first: most lines are somebody else's)
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):  # not JSON - or nested so deeply that the parser gave up
                    continue
                if not isinstance(record, dict):
                    continue
                kind = record.get("event")
                if not isinstance(kind, str) or kind not in wanted:
                    continue  # (a hand-made line may repeat a key with a value of another type)
                if agent_id is not None and record.get("agent_id") != agent_id:
                    continue
                yield index, offset, record


def utc_iso(value: Any) -> str | None:
    """A trace ``ts`` (local time with an offset) as UTC ISO-8601 with a ``Z``; ``None`` when it cannot be read."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            moment = moment.astimezone()  # no offset: it was local time
        return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):  # not a time - or one the platform cannot convert (the year 1, say)
        return None


def finite_number(value: Any) -> float | int | None:
    """``value`` when it is a JSON number an answer can carry: not a bool, and finite (``NaN`` / ``Infinity`` that a
    hand-made trace line may contain cannot be sent, and would turn the whole answer into a server error)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _clip(value: Any, limit: int) -> str:
    """``value`` as text of at most ``limit`` characters (``""`` for ``None``, and for a value that cannot be turned into
    text - a structure nested so deeply that even ``str()`` gives up)."""
    if isinstance(value, str):
        text = value
    elif value is None:
        text = ""
    else:
        try:
            text = str(value)
        except Exception:  # noqa: BLE001 - RecursionError and the like: an imported trace line is not trusted
            text = ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def thought_item(index: int, offset: int, record: dict[str, Any]) -> dict[str, Any]:
    """One entry of the thinking record from a ``thought`` / ``content`` trace record (docs/v0.12-contracts.md 12.4).

    ``round`` is counted from 1 here (the trace counts model calls of a run from 0).  The trace of an imported game is
    not validated: odd values become ``None``, and the texts are cut at what :class:`TraceLog` itself would keep."""
    gen = record.get("gen")
    rnd = record.get("round")
    model = record.get("model")
    return {
        "id": format_cursor(index, offset),
        "ts": utc_iso(record.get("ts")),
        "gt": finite_number(record.get("gt")),
        "gen": gen if isinstance(gen, int) and not isinstance(gen, bool) else None,
        "round": rnd + 1 if isinstance(rnd, int) and not isinstance(rnd, bool) else None,
        "kind": record.get("event"),
        "text": _clip(record.get("text") or "", MAX_TEXT_CHARS),
        "model": _clip(model, MAX_SHORT_CHARS) if model else None,
    }


def read_thoughts(
    directory: str | os.PathLike[str],
    agent_id: str,
    *,
    before: str | None = None,
    limit: int = 50,
    max_bytes: int = DEFAULT_SCAN_BYTES,
    block_size: int = READ_BLOCK_BYTES,
) -> dict[str, Any]:
    """One page of the thinking record of ``agent_id``, read from the end of the trace (docs/v0.12-contracts.md 12.4).

    Only ``thought`` events and ``content`` events of a model call WITHOUT tool calls (what the model said to itself;
    next to tool calls it is a remark on the action, and the action is what the others see) are taken.  Returns
    ``{"items": [oldest .. newest], "next_before": cursor | None, "has_more": bool}``; ``before`` is a cursor of an
    earlier answer (only entries in front of it).  Raises ``ValueError`` for a ``before`` that is not a cursor."""
    position = None
    if before:
        position = parse_cursor(before)
        if position is None:
            raise ValueError("the cursor is not valid")
    limit = max(1, int(limit))
    stats = ScanStats()
    newest_first: list[dict[str, Any]] = []
    records = scan_events_backwards(
        directory, THOUGHT_EVENTS, agent_id=agent_id, before=position, max_bytes=max_bytes, block_size=block_size,
        stats=stats,
    )
    with contextlib.closing(records):  # (stopping early must let go of the file at once: Windows cannot move an open one)
        for index, offset, record in records:
            if record.get("event") == "content" and record.get("with_tools"):
                continue
            if not str(record.get("text") or "").strip():
                continue
            newest_first.append(thought_item(index, offset, record))
            if len(newest_first) > limit:
                break  # one more than a page: there is more
    if len(newest_first) > limit:
        page = newest_first[:limit]
        return {"items": page[::-1], "next_before": page[-1]["id"], "has_more": True}
    if stats.truncated and stats.position is not None:
        # the scan was cut short with lines left: the next request goes on in front of the last line looked at
        return {"items": newest_first[::-1], "next_before": format_cursor(*stats.position), "has_more": True}
    return {"items": newest_first[::-1], "next_before": None, "has_more": False}
