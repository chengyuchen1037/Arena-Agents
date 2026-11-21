"""The error log: every model request of a game and every runtime error, newest last (v0.13.1, G32-G36).

Contract: docs/v0.12-contracts.md section 12.3.  A :class:`CallLog` is an in-memory ring (the last ``2000`` lines) of the
current game.  A line is a **call** - one HTTP attempt of a model request: ``ok``, ``retry`` (this attempt failed, the
request goes on), ``timeout`` / ``error`` (the request ended that way) or ``cancelled`` (a pause, a stop or an interrupt
cancelled it) - or an **event**: a runtime error that is not a model request (a tool that raised, an agent whose action
failed and is retried or given up, a failing timer pass).

Who writes:

* :class:`CallProbe` - one per model request, made by :class:`~app.runtime.llm_gate.GatedEngineLlm` (and by
  :class:`~app.runtime.llm_gate.LoggedLlm` around any other port): the attempts the client retries
  (``run_with_retry``'s ``on_retry``) and the end of the request;
* :class:`LoggingTrace` - wraps the agents' trace sink: every ``error`` trace event (a tool that raised, a run that
  failed - ``will_retry`` says whether it is repeated) is a line too;
* the orchestrator - an ``error`` UI event that no agent raised (a timer pass that failed).

Nothing here keeps a prompt, a model answer, a header or a key: a line holds counts, names, status codes and the
*scrubbed* text of an error (at most 500 characters, :func:`~app.runtime.trace_log.redact_text`).

Persistence is the game's trace log (``<data>/logs/<id>/trace-*.jsonl``): a failed or cancelled call, and the one-shot
calls (review, "complete it") that succeeded, are also written there as an ``llm_call`` event with ``status`` /
``purpose`` / ``error_class`` / ``http_status`` / ``message`` (a successful *turn* is written by the agent itself,
richer), so that a log can be rebuilt - lazily, by the owner of the log - after a restart or when an older game is
loaded: :meth:`CallLog.load_from_trace`.  A line written while the trace log was closed (a paused game, a review) closes
it again: an open file would keep Windows from moving the logs folder (deleting a game, importing an archive).

Everything is synchronous and runs on the event loop; no method raises into the game, and nothing that goes wrong in
here may change the outcome of a model call - not even an exception whose own text cannot be made.
"""

from __future__ import annotations

import contextlib
import logging
import math
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.runtime.ports import TraceSink
from app.runtime.trace_log import DEFAULT_SCAN_BYTES, finite_number, redact_text, scan_events_backwards, utc_iso
from app.world.types import ENGINE_ID, MODERATOR_ID, UiEvent

logger = logging.getLogger(__name__)

CAPACITY = 2000  # lines kept in memory per game
MAX_MESSAGE_CHARS = 500
#: Ids, names, classes and reasons are short; the trace of an imported game is not validated, so they are cut anyway.
MAX_FIELD_CHARS = 120
#: Of a long error text only this much is looked at for credentials (the line keeps :data:`MAX_MESSAGE_CHARS` of it).
_REDACT_WINDOW_CHARS = 8000

#: ``status`` of a line.
STATUSES = frozenset({"ok", "retry", "timeout", "error", "cancelled"})
ERROR_STATUSES = frozenset({"timeout", "error"})  # what the red badge counts
FAILED_STATUSES = frozenset({"retry", "timeout", "error"})  # what ``errors_only`` keeps
#: ``init`` (v0.14, contract 13.6): a bot's initialization run (it belongs to no game; ``actor_id`` is the bot's id).
PURPOSES = frozenset({"turn", "review", "refine", "runtime", "init"})
#: The trace events a log is rebuilt from.
TRACE_EVENTS = ("llm_call", "error", "retry", "give_up")

_UNSET: Any = object()


# ------------------------------------------------------------------------------------------------ helpers
def http_status_of(exc: BaseException) -> int | None:
    """The HTTP status an exception carries (an SDK's status error, an error event inside a stream), else ``None``.
    Never raises: an exception whose attributes misbehave simply has none."""
    try:
        for source in (exc, getattr(exc, "response", None)):
            code = getattr(source, "status_code", None)
            if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599:
                return code
    except Exception:  # noqa: BLE001 - a property that raises
        pass
    return None


def failure_status(exc: BaseException) -> str:
    """``"timeout"`` for a request that ran out of time (the gate's own limit, an SDK's or a socket's), else ``"error"``."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "timeout" if any("timeout" in cls.__name__.lower() for cls in type(exc).__mro__) else "error"


def failure_text(exc: BaseException, *, timeout_s: float | None = None) -> str:
    """What to say about a failure: its own text, else (a bare timeout has none) a sentence, else its class.  An
    exception whose own text cannot be made (a ``__str__`` that raises) is told by its class."""
    try:
        text = str(exc).strip()
    except Exception:  # noqa: BLE001 - not ours to judge: the call failed, and that is what the line is for
        text = ""
    if text:
        return text
    if isinstance(exc, TimeoutError):
        return f"调用超过 {timeout_s:g} 秒仍未返回，已放弃。" if timeout_s else "调用超时。"
    return type(exc).__name__


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _whole(value: Any) -> int | None:
    """``value`` as an int when it is a number (a bool is not), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return int(value)
    except (OverflowError, ValueError):
        return None


def _text(value: Any, limit: int | None = None) -> str | None:
    """``value`` as stripped text; ``None`` when it is empty (or cannot be turned into text).  With ``limit``: at most that
    many characters (a ``…`` marks a cut)."""
    try:
        text = str(value).strip() if value is not None else ""
    except Exception:  # noqa: BLE001 - an object whose ``__str__`` raises, a structure nested too deeply: no text
        return None
    if not text:
        return None
    if limit is not None and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _short(value: Any) -> str | None:
    """:func:`_text` for the short fields of a line (ids, names, classes, reasons): at most :data:`MAX_FIELD_CHARS`."""
    return _text(value, MAX_FIELD_CHARS)


def _attr(obj: Any, name: str) -> Any:
    """``obj.name``, or ``None`` - never an exception (``obj`` is a model's answer or an error: not ours to trust)."""
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001
        return None


def _attr_text(obj: Any, name: str) -> str | None:
    """The attribute ``name`` of ``obj`` when it is text (stripped; ``None`` when empty), else ``None``."""
    value = _attr(obj, name)
    return _short(value) if isinstance(value, str) else None


def _is_open(trace: Any) -> bool:
    """Whether ``trace`` holds its file open right now.  A sink that cannot say is taken to be open: it is left alone."""
    try:
        return bool(getattr(trace, "is_open", True))
    except Exception:  # noqa: BLE001
        return True


def _let_go(trace: Any) -> None:
    """Close the file of ``trace`` (a later ``write`` reopens it); never raises."""
    close = getattr(trace, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - logging hygiene must never break a call
            logger.debug("could not close a trace log", exc_info=True)


# ------------------------------------------------------------------------------------------------ the log
class CallLog:
    """The call / error log of ONE game (``session_id``); see the module docstring.

    ``clock() -> float | None`` is the game clock (a line's ``gt``), ``names(actor_id) -> str`` gives the name of
    an actor, ``publish(UiEvent)`` receives the ``llm.call`` event of every line that is added, ``trace`` is where
    failed calls are written (``make_trace()`` makes it on first use when there is none yet)."""

    def __init__(
        self,
        session_id: str,
        *,
        capacity: int = CAPACITY,
        clock: Callable[[], float | None] | None = None,
        names: Callable[[str], str] | None = None,
        publish: Callable[[UiEvent], None] | None = None,
        trace: TraceSink | None = None,
        make_trace: Callable[[], TraceSink | None] | None = None,
    ) -> None:
        self.session_id = str(session_id)
        self.capacity = max(1, int(capacity))
        self.clock = clock
        self.names = names
        self.publish = publish
        self.trace = trace
        self.make_trace = make_trace
        self._owns_trace = False  # ``trace`` was made by ``make_trace`` (and is closed when another one replaces it)
        self._entries: deque[dict[str, Any]] = deque()
        self._next_id = 1
        self.errors = 0  # ``timeout`` + ``error`` lines in the ring
        self.retries = 0  # ``retry`` lines in the ring

    # ---------------------------------------------------------------------------------------- reading
    def __len__(self) -> int:
        return len(self._entries)

    @property
    def last_id(self) -> int:
        """The id of the newest line (``0`` while there is none)."""
        return self._next_id - 1

    def entries(self) -> list[dict[str, Any]]:
        """Copies of every line, oldest first."""
        return [dict(entry) for entry in self._entries]

    def view(self, *, after: int = 0, limit: int = 500, errors_only: bool = False) -> dict[str, Any]:
        """The answer of ``GET /api/llm/calls``: the lines with an id above ``after`` (the newest ``limit`` of them
        when there are more), only the failed ones with ``errors_only``, oldest first."""
        rows = [
            entry
            for entry in self._entries
            if entry["id"] > after and (not errors_only or entry["status"] in FAILED_STATUSES)
        ]
        if limit > 0 and len(rows) > limit:
            rows = rows[-limit:]
        return {
            "calls": [dict(entry) for entry in rows],
            "last_id": self.last_id,
            "total": len(self._entries),
            "errors": self.errors,
            "retries": self.retries,
        }

    # --------------------------------------------------------------------------------------- recording
    def probe(
        self,
        actor_id: str,
        purpose: str = "turn",
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> "CallProbe":
        """The recorder of one model request made by ``actor_id`` (``purpose``: ``turn`` / ``review`` / ``refine``)."""
        return CallProbe(self, actor_id, purpose, provider_id=provider_id, model_id=model_id, clock=clock)

    def add(
        self,
        *,
        kind: str,
        status: str,
        actor_id: str,
        purpose: str,
        provider_id: str | None = None,
        model_id: str | None = None,
        attempt: int | None = None,
        elapsed_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        finish_reason: str | None = None,
        http_status: int | None = None,
        error_class: str | None = None,
        message: str | None = None,
        gt: Any = _UNSET,
        ts: str | None = None,
        actor_name: str | None = None,
        persist: bool = True,
        announce: bool = True,
    ) -> dict[str, Any]:
        """Append one line (``CallEntry``, contract 12.3) and return it.

        ``gt`` defaults to the game clock now (pass ``None`` for "unknown"), ``ts`` to the wall clock now.  ``persist``
        writes a failed call (and any line the trace does not know yet) into the trace log; ``announce`` publishes the
        ``llm.call`` event.  A rebuild passes ``False`` for both.  The short fields (ids, names, classes, the finish
        reason) are cut at :data:`MAX_FIELD_CHARS` characters, the message at :data:`MAX_MESSAGE_CHARS` (after the
        credentials are scrubbed from it): what an imported trace says is not trusted to be small."""
        actor = _short(actor_id) or ""
        entry: dict[str, Any] = {
            "id": 0,
            "ts": ts or _utc_now_iso(),
            "gt": self.game_time() if gt is _UNSET else finite_number(gt),
            "kind": kind,
            "status": status,
            "actor_id": actor,
            "actor_name": _short(actor_name) or _short(self._name(actor_id)) or actor,
            "purpose": purpose,
            "provider_id": _short(provider_id),
            "model_id": _short(model_id),
            "attempt": _whole(attempt),
            "elapsed_ms": _whole(elapsed_ms),
            "prompt_tokens": _whole(prompt_tokens),
            "completion_tokens": _whole(completion_tokens),
            "cache_read_tokens": _whole(cache_read_tokens),
            "cache_write_tokens": _whole(cache_write_tokens),
            "finish_reason": _short(finish_reason),
            "http_status": _whole(http_status),
            "error_class": _short(error_class),
            "message": self._message(message),
        }
        self._append(entry)
        if persist:
            self._persist(entry)
        if announce:
            self._announce(entry)
        return entry

    def note_error(self, actor_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        """An ``error`` trace event of an agent (:class:`LoggingTrace`) becomes a line: ``retry`` when ``will_retry``
        (the agent repeats its action), else ``error``.  The trace holds it already: nothing is written again."""
        text = _text(fields.get("message"), _REDACT_WINDOW_CHARS)
        text = re.sub(r":\s*$", "", text) if text else text  # ("TimeoutError: " - the exception had no text)
        return self.add(
            kind="event",
            status="retry" if fields.get("will_retry") else "error",
            actor_id=actor_id,
            purpose="runtime",
            message=text,
            error_class=fields.get("error_class"),
            http_status=fields.get("http_status"),
            gt=fields["gt"] if "gt" in fields else _UNSET,  # (the game time the trace event carries, if it does)
            persist=False,
        )

    def note_runtime_error(self, message: str, *, actor_id: str = ENGINE_ID) -> dict[str, Any]:
        """A runtime error nobody traced (a timer pass of the world that failed): a line, and a trace ``error`` event."""
        return self.add(kind="event", status="error", actor_id=actor_id, purpose="runtime", message=message)

    def _append(self, entry: dict[str, Any]) -> None:
        entry["id"] = self._next_id
        self._next_id += 1
        self._entries.append(entry)
        self._count(entry, 1)
        while len(self._entries) > self.capacity:
            self._count(self._entries.popleft(), -1)

    def _count(self, entry: dict[str, Any], sign: int) -> None:
        if entry["status"] in ERROR_STATUSES:
            self.errors += sign
        elif entry["status"] == "retry":
            self.retries += sign

    def _announce(self, entry: dict[str, Any]) -> None:
        if self.publish is None:
            return
        try:
            self.publish(UiEvent(type="llm.call", data={"call": dict(entry), "errors": self.errors}))
        except Exception:  # noqa: BLE001 - the UI side must never break a model call
            logger.debug("could not publish an llm.call event", exc_info=True)

    # ------------------------------------------------------------------------------------- the trace
    def attach_trace(self, trace: TraceSink | None) -> None:
        """Write what the log persists into ``trace`` (the trace log of the running game) from now on."""
        old, owned = self.trace, self._owns_trace
        self.trace, self._owns_trace = trace, False
        if owned and old is not None and old is not trace:
            _let_go(old)

    def _trace(self) -> TraceSink | None:
        if self.trace is None and self.make_trace is not None:
            try:
                self.trace = self.make_trace()
                self._owns_trace = self.trace is not None
            except Exception:  # noqa: BLE001 - no trace log: the lines stay in memory only
                logger.warning("Could not open the trace log of the call log", exc_info=True)
                self.make_trace = None
        return self.trace

    def _persist(self, entry: dict[str, Any]) -> None:
        """Write a line into the trace unless the trace has it already: a successful turn is traced by its agent.

        A trace that was closed when the line came (a paused game - ``Supervisor.stop()`` lets go of its file - or one
        made just for a review) is closed again right after: whoever opened it for the sake of this one line must not
        leave it open, because on Windows an open file blocks moving or deleting the logs folder.  The trace of a game
        that is running, and so open already, is left as it is."""
        if entry["kind"] == "call" and entry["status"] == "ok" and entry["purpose"] == "turn":
            return
        trace = self._trace()
        if trace is None:
            return
        if entry["kind"] == "call":
            event = "llm_call"
            fields: dict[str, Any] = {
                "status": entry["status"],
                "purpose": entry["purpose"],
                "attempt": entry["attempt"],
                "provider": entry["provider_id"],
                "model": entry["model_id"],
                "elapsed_ms": entry["elapsed_ms"],
                "prompt_tokens": entry["prompt_tokens"],
                "completion_tokens": entry["completion_tokens"],
                "cache_read_tokens": entry["cache_read_tokens"],
                "cache_write_tokens": entry["cache_write_tokens"],
                "finish_reason": entry["finish_reason"],
                "http_status": entry["http_status"],
                "error_class": entry["error_class"],
                "message": entry["message"],
                "gt": entry["gt"],
            }
        else:
            event = "error"
            fields = {
                "status": entry["status"],
                "message": entry["message"],
                "error_class": entry["error_class"],
                "http_status": entry["http_status"],
                "gt": entry["gt"],
            }
        was_open = _is_open(trace)
        try:
            trace.write(event, entry["actor_id"], **{k: v for k, v in fields.items() if v is not None})
        except Exception:  # noqa: BLE001 - a broken trace must never break a call
            logger.debug("could not write a call to the trace", exc_info=True)
        finally:
            if not was_open:
                _let_go(trace)

    # ------------------------------------------------------------------------------------ the details
    def game_time(self) -> float | None:
        """The game clock now in seconds (``None``: there is no clock to ask, or it gave no usable number)."""
        if self.clock is None:
            return None
        try:
            now = self.clock()
            if now is None:
                return None
            seconds = float(now)
            return round(seconds, 3) if math.isfinite(seconds) else None
        except Exception:  # noqa: BLE001 - no clock to ask
            return None

    def _name(self, actor_id: str) -> str:
        if self.names is not None:
            try:
                name = self.names(actor_id)
                if name:
                    return str(name)
            except Exception:  # noqa: BLE001
                pass
        return {MODERATOR_ID: "主持人", ENGINE_ID: "系统"}.get(actor_id, actor_id)

    def _message(self, text: Any) -> str | None:
        """``text`` without credentials and at most :data:`MAX_MESSAGE_CHARS` long."""
        text = _text(text, _REDACT_WINDOW_CHARS)  # (a bounded look: what could be shown is the first few hundred characters)
        if text is None:
            return None
        trace = self._trace()
        redact = getattr(trace, "redact", None)
        text = redact(text) if callable(redact) else redact_text(text)
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[: MAX_MESSAGE_CHARS - 1] + "…"
        return text

    # ------------------------------------------------------------------------------------- rebuilding
    def load_from_trace(self, directory: str | Path, *, max_bytes: int = DEFAULT_SCAN_BYTES) -> int:
        """Fill this log from the trace files in ``directory`` (the game's ``logs/<id>/``): the newest lines first,
        read from the end of the files, until the ring is full.  Returns how many lines the log holds afterwards.

        Lines come from the ``llm_call`` events (``status`` says what happened; the ones an agent wrote for a
        successful call have none: ``ok``) and the ``error`` events (``retry`` / ``give_up`` say whether the agent
        went on).  Nothing is written back or announced."""
        newest_first: list[dict[str, Any]] = []
        lines = 0
        records = scan_events_backwards(directory, TRACE_EVENTS, max_bytes=max_bytes)
        with contextlib.closing(records):  # (stopping early must let go of the file at once: Windows cannot move an open one)
            for _index, _offset, record in records:
                newest_first.append(record)
                if record.get("event") in ("llm_call", "error"):
                    lines += 1
                    if lines >= self.capacity:
                        break
        rows = self._rows_from_records(newest_first[::-1])
        for row in rows[-self.capacity :]:
            self.add(persist=False, announce=False, **row)
        return len(self._entries)

    @staticmethod
    def _rows_from_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The lines of trace ``records`` (oldest first); the arguments of :meth:`add`."""
        rows: list[dict[str, Any]] = []
        failed_attempts: dict[str, int] = {}  # actor -> the number of the last failed attempt of its request
        last_error: dict[str, tuple[dict[str, Any], Any]] = {}  # actor -> (the line of its last error, its gen)
        for record in records:
            event = record.get("event")
            actor = str(record.get("agent_id") or "")
            ts = utc_iso(record.get("ts"))
            if event == "llm_call":
                status = record.get("status")
                status = status if isinstance(status, str) and status in STATUSES else "ok"
                attempt = _whole(record.get("attempt")) or failed_attempts.get(actor, 0) + 1
                if status == "retry":
                    failed_attempts[actor] = attempt
                else:
                    failed_attempts.pop(actor, None)
                purpose = record.get("purpose")
                rows.append(
                    {
                        "kind": "call",
                        "status": status,
                        "actor_id": actor,
                        "purpose": purpose if isinstance(purpose, str) and purpose in PURPOSES else "turn",
                        "provider_id": record.get("provider") or record.get("provider_id"),
                        "model_id": record.get("model") or record.get("model_id"),
                        "attempt": attempt,
                        "elapsed_ms": _whole(record.get("elapsed_ms")),
                        "prompt_tokens": _whole(record.get("prompt_tokens")),
                        "completion_tokens": _whole(record.get("completion_tokens")),
                        "cache_read_tokens": _whole(record.get("cache_read_tokens")),
                        "cache_write_tokens": _whole(record.get("cache_write_tokens")),
                        "finish_reason": record.get("finish_reason"),
                        "http_status": _whole(record.get("http_status")),
                        "error_class": record.get("error_class"),
                        "message": record.get("message"),
                        "gt": finite_number(record.get("gt")),
                        "ts": ts,
                    }
                )
            elif event == "error":
                text = _text(record.get("message"), _REDACT_WINDOW_CHARS)
                row = {
                    "kind": "event",
                    "status": "retry" if record.get("will_retry") or record.get("status") == "retry" else "error",
                    "actor_id": actor,
                    "purpose": "runtime",
                    "message": re.sub(r":\s*$", "", text) if text else text,
                    "error_class": record.get("error_class"),
                    "http_status": _whole(record.get("http_status")),
                    "gt": finite_number(record.get("gt")),
                    "ts": ts,
                }
                rows.append(row)
                last_error[actor] = (row, record.get("gen"))
            elif event in ("retry", "give_up"):
                # (older traces: the ``error`` event does not say what the agent did next - the next one does)
                found = last_error.pop(actor, None)
                if found is not None and found[1] == record.get("gen"):
                    found[0]["status"] = "retry" if event == "retry" else "error"
        return rows


# ------------------------------------------------------------------------------------- one request
class CallProbe:
    """The record of ONE model request while it runs: every attempt that fails and is repeated is a ``retry``
    line (:meth:`on_retry`, fed by the client), the end of the request is the last line of it (:meth:`ok`,
    :meth:`failed`, :meth:`cancelled`).  ``elapsed_ms`` is the time of that attempt (the wait before a repeated
    attempt is not part of it)."""

    def __init__(
        self,
        log: CallLog,
        actor_id: str,
        purpose: str = "turn",
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.log = log
        self.actor_id = actor_id
        self.purpose = purpose
        self.provider_id = provider_id
        self.model_id = model_id
        self.attempt = 1  # the number of the attempt that is in flight
        self._clock = clock
        self._attempt_started = clock()

    # Every method below is written so that NOTHING that goes wrong in it can reach the caller: the gate calls them
    # from inside an ``except`` block, and an exception raised there would replace the one of the model call - the
    # agent would retry (or give up) for the wrong reason - and from the client's retry hook, where it would end the
    # retry.  The arguments are worked out inside the guard too: an exception whose ``__str__`` raises, an answer whose
    # attributes raise, a clock that raises.
    def start(self) -> None:
        """The request really begins now (a slot was granted): the stopwatch of the first attempt starts."""
        try:
            self._attempt_started = self._clock()
        except Exception:  # noqa: BLE001
            logger.debug("the clock of a call probe failed", exc_info=True)

    def _elapsed_ms(self, now: float) -> int:
        milliseconds = (now - self._attempt_started) * 1000
        return max(0, int(milliseconds)) if math.isfinite(milliseconds) else 0

    def _line(self, status: str, **fields: Any) -> dict[str, Any]:
        fields.setdefault("provider_id", self.provider_id)
        fields.setdefault("model_id", self.model_id)
        try:
            return self.log.add(
                kind="call",
                status=status,
                actor_id=self.actor_id,
                purpose=self.purpose,
                attempt=self.attempt,
                elapsed_ms=self._elapsed_ms(self._clock()),
                **fields,
            )
        except Exception:  # noqa: BLE001 - writing the log must never change the outcome of a model call
            logger.warning("Could not record a model call in the call log", exc_info=True)
            return {}

    def on_retry(self, exc: BaseException, attempt: int | None = None, delay: float = 0.0) -> None:
        """An attempt failed and the client repeats it after ``delay`` seconds (``run_with_retry``'s ``on_retry``)."""
        try:
            number = _whole(attempt)
            if number is not None and number >= 1:
                self.attempt = max(self.attempt, number)
            self._line(
                "retry",
                http_status=http_status_of(exc),
                error_class=type(exc).__name__,
                message=failure_text(exc),
            )
            self.attempt += 1
            try:
                wait = float(delay or 0.0)
            except (TypeError, ValueError):
                wait = 0.0
            self._attempt_started = self._clock() + (wait if math.isfinite(wait) and wait > 0 else 0.0)
        except Exception:  # noqa: BLE001 - the retry goes on, logged or not
            logger.warning("Could not record a repeated model call in the call log", exc_info=True)

    def ok(self, resp: Any) -> dict[str, Any]:
        """The request returned ``resp``: tokens and finish reason are counts, never content."""
        try:
            return self._line(
                "ok",
                provider_id=_attr_text(resp, "provider_id") or self.provider_id,
                model_id=_attr_text(resp, "model_id") or self.model_id,
                prompt_tokens=_whole(_attr(resp, "prompt_tokens")),
                completion_tokens=_whole(_attr(resp, "completion_tokens")),
                cache_read_tokens=_whole(_attr(resp, "cache_read_tokens")),
                cache_write_tokens=_whole(_attr(resp, "cache_write_tokens")),
                finish_reason=_attr_text(resp, "finish_reason"),
            )
        except Exception:  # noqa: BLE001 - the answer is returned, logged or not
            logger.warning("Could not record a model answer in the call log", exc_info=True)
            return {}

    def failed(self, exc: BaseException, *, timeout_s: float | None = None) -> dict[str, Any]:
        """The request ended with ``exc``: ``timeout`` when it ran out of time, else ``error``."""
        try:
            return self._line(
                failure_status(exc),
                http_status=http_status_of(exc),
                error_class=type(exc).__name__,
                message=failure_text(exc, timeout_s=timeout_s),
            )
        except Exception:  # noqa: BLE001 - ``exc`` is raised again by the caller, logged or not
            logger.warning("Could not record a failed model call in the call log", exc_info=True)
            return {}

    def cancelled(self) -> dict[str, Any]:
        """The request was cancelled (a hard pause or a stop, or an interrupt that made its answer useless)."""
        try:
            return self._line("cancelled", message="调用被取消（暂停、停止，或被新消息打断）。")
        except Exception:  # noqa: BLE001 - the cancellation goes on, logged or not
            logger.warning("Could not record a cancelled model call in the call log", exc_info=True)
            return {}


# ------------------------------------------------------------------------------------- the agents' trace
class LoggingTrace:
    """A trace sink that passes every event on to ``inner`` and shows the ``error`` events to the call log.

    The agents (and the supervisor) of a game write to it instead of to the trace log itself; whatever the
    inner sink offers besides ``write`` (``close``, ``files`` ...) is still reachable."""

    def __init__(self, inner: TraceSink, call_log: CallLog) -> None:
        self.inner = inner
        self.call_log = call_log

    def write(self, event: str, agent_id: str, **fields: Any) -> None:
        if event == "error" and "gt" not in fields:
            fields["gt"] = self.call_log.game_time()  # when it happened on the game clock: the line shows it, the trace keeps it
        self.inner.write(event, agent_id, **fields)
        if event == "error":
            try:
                self.call_log.note_error(agent_id, fields)
            except Exception:  # noqa: BLE001 - the log must never break a run
                logger.debug("could not log an error event", exc_info=True)

    def __getattr__(self, name: str) -> Any:
        if name in ("inner", "call_log"):  # (not set yet: a copy or an unpickle - do not recurse)
            raise AttributeError(name)
        return getattr(self.inner, name)
