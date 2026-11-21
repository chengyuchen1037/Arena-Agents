"""The log of a learning job and the running total of what the initializations cost (v0.14, H24; contract §13.6).

* :class:`LearningLog` - ``<data>/logs/learning/<job_id>.jsonl``, one JSON object per line, written by the loop (``llm_call`` /
  ``tool_call`` / ``nudge`` / ``end``) and by the runners (``job`` / ``result``).  A line says who (``agent_id``, the bot or the seat), what
  and how long - **never a prompt and never a key**: long texts are cut, every string goes through the credential scrubber of the trace log
  (``sk-…`` keys, ``Bearer …``, ``api_key=…`` and every key registered with :meth:`LearningLog.add_secret`).  Writing never raises: a full disk
  costs log lines, not a learning run.
* :class:`LearningUsage` - ``<data>/bots/usage.json`` ``{"calls", "tokens", "cost_usd"}``: what the initializations used (they belong to no
  game, so they count against no game's budget; the review of a game counts against that game).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.runtime.trace_log import REDACTED, redact_text
from app.runtime.usage import UsageBook

logger = logging.getLogger(__name__)

__all__ = ["LearningLog", "LearningUsage"]

MAX_FIELD_CHARS = 2000
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")
_SENSITIVE_KEYS = frozenset({"api_key", "apikey", "api-key", "x-api-key", "authorization", "password", "secret", "access_token"})


class LearningLog:
    """The JSONL log of one learning job (see the module docstring)."""

    def __init__(self, directory: Path, job_id: str, *, secrets: Iterable[str] = ()) -> None:
        name = _SAFE_RE.sub("_", str(job_id)).lstrip(".") or "job"
        self.path = Path(directory) / f"{name}.jsonl"
        self._secrets: list[str] = [s for s in secrets if s]
        self.written = 0
        self.dropped = 0
        self._warned = False

    def add_secret(self, value: str) -> None:
        """Always scrub this exact string (a provider's API key that has no recognizable shape)."""
        if value and len(value) >= 6 and value not in self._secrets:
            self._secrets.append(value)

    def _scrub(self, value: Any, depth: int = 0) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            text = redact_text(value, self._secrets)
            return text if len(text) <= MAX_FIELD_CHARS else f"{text[:MAX_FIELD_CHARS]}…[已截断 {len(text) - MAX_FIELD_CHARS} 字]"
        if depth >= 6:
            return "…"
        if isinstance(value, dict):
            return {
                str(k): (REDACTED if str(k).lower() in _SENSITIVE_KEYS and v not in (None, "") else self._scrub(v, depth + 1))
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self._scrub(v, depth + 1) for v in list(value)[:200]]
        return self._scrub(str(value), depth + 1)

    def write(self, event: str, agent_id: str, **fields: Any) -> None:
        """Append one line (``TraceSink``).  Never raises."""
        try:
            record: dict[str, Any] = {"ts": datetime.now().astimezone().isoformat(timespec="milliseconds"), "event": event, "agent_id": agent_id}
            for key, value in fields.items():
                record[key if key not in record else f"field_{key}"] = value
            line = json.dumps(self._scrub(record), ensure_ascii=False, separators=(",", ":")) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "ab") as handle:  # (opened per line: no handle is left open to block moving the logs folder on Windows)
                handle.write(line.encode("utf-8"))
            self.written += 1
        except Exception as exc:  # noqa: BLE001 - a log must never end a learning run
            self.dropped += 1
            if not self._warned:
                self._warned = True
                logger.warning("learning log write failed (%s); further failures are silent", type(exc).__name__)


class LearningUsage:
    """``<data>/bots/usage.json``: ``{"calls", "tokens", "cost_usd"}`` of the initializations, accumulated over all time."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.calls = 0
        self.tokens = 0
        self.cost_usd = 0.0
        self._loaded = False
        self._dirty = False

    def load(self) -> None:
        self._loaded = True
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            self.calls = max(0, int(raw.get("calls") or 0))
            self.tokens = max(0, int(raw.get("tokens") or 0))
            self.cost_usd = max(0.0, float(raw.get("cost_usd") or 0.0))

    def add(self, resp: Any) -> None:
        """Account one model answer (tokens and cost from the price snapshot it carries, as the usage book of a game does)."""
        if not self._loaded:
            self.load()
        scratch = UsageBook()
        scratch.record_llm("learning", resp)
        self.calls += 1
        self.tokens += int(scratch.total_tokens)
        self.cost_usd = round(self.cost_usd + float(scratch.total_cost_usd), 10)
        self._dirty = True

    def view(self) -> dict[str, Any]:
        if not self._loaded:
            self.load()
        return {"calls": self.calls, "tokens": self.tokens, "cost_usd": self.cost_usd}

    def flush(self) -> None:
        """Write the totals (atomically); a failure is logged, never raised."""
        if not self._dirty:
            return
        try:
            from app.storage import botfiles

            botfiles.write_bytes(self.path, (json.dumps(self.view(), ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
            self._dirty = False
        except Exception:  # noqa: BLE001
            logger.warning("Could not write the learning usage", exc_info=True)
