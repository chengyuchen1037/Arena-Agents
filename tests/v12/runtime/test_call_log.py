"""The error log (v0.13.1, G32-G36; contract docs/v0.12-contracts.md 12.3): the ring of lines, one probe per model
request, the trace sink that sees the agents' errors, persistence into the trace and the rebuild from it."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from app.llm.types import LLMResponse
from app.runtime import call_log as call_log_module
from app.runtime.call_log import (
    CAPACITY,
    ERROR_STATUSES,
    FAILED_STATUSES,
    MAX_FIELD_CHARS,
    CallLog,
    LoggingTrace,
    failure_status,
    failure_text,
    http_status_of,
)
from app.runtime.ports import TraceSink
from app.runtime.trace_log import TraceLog
from app.world.types import UiEvent

from .fakes import RecordingTrace

NAMES = {"a1": "Alice", "a2": "Bob", "moderator": "主持人", "engine": "系统"}
ENTRY_KEYS = [
    "id", "ts", "gt", "kind", "status", "actor_id", "actor_name", "purpose", "provider_id", "model_id", "attempt",
    "elapsed_ms", "prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens", "finish_reason",
    "http_status", "error_class", "message",
]


class Clock:
    """A clock the test moves by hand (game time or a stopwatch)."""

    def __init__(self, now: float = 10.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_log(**kwargs: Any) -> tuple[CallLog, list[UiEvent], Clock]:
    events: list[UiEvent] = []
    clock = Clock()
    log = CallLog("s1", clock=clock, names=lambda actor: NAMES.get(actor, actor), publish=events.append, **kwargs)
    return log, events, clock


def call(log: CallLog, status: str = "ok", actor: str = "a1", **fields: Any) -> dict[str, Any]:
    return log.add(kind="call", status=status, actor_id=actor, purpose=fields.pop("purpose", "turn"), **fields)


def records(trace: TraceLog) -> list[dict[str, Any]]:
    trace.close()
    return [json.loads(line) for path in trace.files() for line in path.read_text(encoding="utf-8").splitlines() if line]


# ------------------------------------------------------------------------------------------------ the ring
def test_a_line_has_exactly_the_shape_of_the_contract():
    log, _events, _clock = make_log()
    entry = call(
        log, provider_id="deepseek", model_id="deepseek-chat", attempt=1, elapsed_ms=1234, prompt_tokens=1000,
        completion_tokens=200, cache_read_tokens=0, cache_write_tokens=0, finish_reason="stop",
    )
    assert list(entry) == ENTRY_KEYS
    assert entry["id"] == 1 and entry["kind"] == "call" and entry["status"] == "ok" and entry["purpose"] == "turn"
    assert entry["actor_id"] == "a1" and entry["actor_name"] == "Alice"
    assert entry["gt"] == 10.0  # the game clock at that moment
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", entry["ts"])  # wall clock, UTC
    assert (entry["provider_id"], entry["model_id"], entry["attempt"], entry["elapsed_ms"]) == ("deepseek", "deepseek-chat", 1, 1234)
    assert (entry["prompt_tokens"], entry["completion_tokens"], entry["finish_reason"]) == (1000, 200, "stop")
    assert entry["http_status"] is None and entry["error_class"] is None and entry["message"] is None
    json.dumps(entry)  # plain JSON all the way


def test_ids_count_up_from_one_and_a_missing_clock_or_name_is_null_or_the_id():
    log = CallLog("s1")  # no clock, no names, nobody listening
    first = log.add(kind="event", status="error", actor_id="moderator", purpose="runtime", message="boom")
    second = log.add(kind="event", status="error", actor_id="who", purpose="runtime")
    assert (first["id"], second["id"]) == (1, 2) and log.last_id == 2
    assert first["gt"] is None and first["actor_name"] == "主持人" and second["actor_name"] == "who"
    assert first["attempt"] is None  # an event has no attempt
    explicit = log.add(kind="event", status="error", actor_id="who", purpose="runtime", gt=None, ts="2026-11-18T16:00:00Z")
    assert explicit["gt"] is None and explicit["ts"] == "2026-11-18T16:00:00Z"


def test_the_ring_keeps_the_newest_lines_and_the_counters_follow_it():
    log, _events, _clock = make_log(capacity=5)
    call(log, "error")
    call(log, "timeout")
    call(log, "retry")
    call(log, "ok")
    call(log, "cancelled")
    assert (log.errors, log.retries, len(log)) == (2, 1, 5)
    call(log, "ok")  # evicts the first line (an error)
    assert (log.errors, log.retries, len(log)) == (1, 1, 5)
    call(log, "ok")  # evicts the timeout
    call(log, "ok")  # evicts the retry
    assert (log.errors, log.retries, len(log)) == (0, 0, 5)
    ids = [e["id"] for e in log.entries()]
    assert ids == [4, 5, 6, 7, 8] and log.last_id == 8  # ids never repeat, even after eviction


def test_the_default_capacity_is_two_thousand_lines():
    assert CAPACITY == 2000 and CallLog("s").capacity == 2000


def test_status_sets_match_the_contract():
    assert ERROR_STATUSES == {"timeout", "error"} and FAILED_STATUSES == {"retry", "timeout", "error"}


def test_view_filters_by_id_error_status_and_returns_the_newest_when_there_are_too_many():
    log, _events, _clock = make_log()
    for status in ["ok", "retry", "error", "ok", "cancelled", "timeout", "ok"]:
        call(log, status)
    everything = log.view()
    assert [c["id"] for c in everything["calls"]] == [1, 2, 3, 4, 5, 6, 7]
    assert (everything["last_id"], everything["total"], everything["errors"], everything["retries"]) == (7, 7, 2, 1)
    assert [c["id"] for c in log.view(after=4)["calls"]] == [5, 6, 7]
    assert log.view(after=7)["calls"] == [] and log.view(after=7)["last_id"] == 7
    only = log.view(errors_only=True)
    assert [c["status"] for c in only["calls"]] == ["retry", "error", "timeout"]  # cancelled is not an error
    assert only["total"] == 7 and only["errors"] == 2  # the counters describe the ring, not the filter
    assert [c["id"] for c in log.view(limit=2)["calls"]] == [6, 7]  # the newest two, oldest first
    assert [c["id"] for c in log.view(after=1, limit=2, errors_only=True)["calls"]] == [3, 6]


def test_view_hands_out_copies():
    log, _events, _clock = make_log()
    call(log)
    log.view()["calls"][0]["status"] = "error"
    log.entries()[0]["status"] = "error"
    assert log.view()["calls"][0]["status"] == "ok" and log.errors == 0


# ------------------------------------------------------------------------------------------ llm.call events
def test_every_line_is_announced_as_an_llm_call_event_with_the_error_count():
    log, events, _clock = make_log()
    call(log, "ok")
    call(log, "error", http_status=500)
    assert [e.type for e in events] == ["llm.call", "llm.call"]
    assert set(events[1].data) == {"call", "errors"}  # one line and a number: a summary, nothing more
    assert events[1].data["errors"] == 1 and events[1].data["call"]["id"] == 2 and events[1].data["call"]["http_status"] == 500
    events[1].data["call"]["status"] = "ok"
    assert log.entries()[1]["status"] == "error"  # the event holds a copy


def test_a_publisher_that_raises_never_breaks_the_log():
    def explode(event: UiEvent) -> None:
        raise RuntimeError("bus down")

    log = CallLog("s", publish=explode)
    assert call(log)["id"] == 1 and len(log) == 1


# ----------------------------------------------------------------------------------------------- scrubbing
def test_messages_are_scrubbed_and_cut_to_500_characters(tmp_path):
    trace = TraceLog(tmp_path, "s1")
    trace.add_secret("provider-key-9876543")
    log, _events, _clock = make_log(trace=trace)
    entry = call(log, "error", message="401 for provider-key-9876543 / Bearer abcdef123456789 / sk-proj-abcdef1234567890")
    assert "provider-key" not in entry["message"] and "abcdef123456789" not in entry["message"]
    assert "sk-proj" not in entry["message"] and "***" in entry["message"]
    long = call(log, "error", message="x" * 2000)
    assert len(long["message"]) == 500 and long["message"].endswith("…")
    assert call(log, "error", message="   ")["message"] is None  # blank means none
    assert "provider-key-9876543" not in json.dumps(log.entries())


def test_without_a_trace_the_generic_credential_patterns_are_still_scrubbed():
    log = CallLog("s")
    assert "sk-" in log.add(kind="event", status="error", actor_id="a1", purpose="runtime", message="bad key sk-abcdef1234567890 sent")["message"]
    assert "abcdef1234567890" not in log.entries()[0]["message"]


# ------------------------------------------------------------------------------------ the trace of a game
def test_a_failed_call_is_written_to_the_trace_but_a_successful_turn_is_not(tmp_path):
    trace = TraceLog(tmp_path, "s1")
    log, _events, _clock = make_log(trace=trace)
    call(log, "ok", provider_id="p", model_id="m", attempt=1, prompt_tokens=5)  # the agent traces its own turns
    call(
        log, "error", attempt=3, provider_id="p", model_id="m", elapsed_ms=900, http_status=500,
        error_class="InternalServerError", message="upstream 500 sk-abcdef1234567890",
    )
    call(log, "ok", actor="a2", purpose="review", provider_id="p", model_id="m", prompt_tokens=7, completion_tokens=3, finish_reason="stop")
    call(log, "timeout", actor="moderator", purpose="refine", attempt=1)
    log.add(kind="event", status="error", actor_id="engine", purpose="runtime", message="timer pass failed")
    found = records(trace)
    assert [(r["event"], r["agent_id"], r.get("status")) for r in found] == [
        ("llm_call", "a1", "error"), ("llm_call", "a2", "ok"), ("llm_call", "moderator", "timeout"), ("error", "engine", "error"),
    ]
    failed = found[0]
    assert (failed["attempt"], failed["http_status"], failed["error_class"], failed["purpose"]) == (3, 500, "InternalServerError", "turn")
    assert (failed["provider"], failed["model"], failed["elapsed_ms"], failed["gt"]) == ("p", "m", 900, 10.0)
    assert "sk-abcdef" not in failed["message"] and "prompt_tokens" not in failed  # (no token counts for a failure)
    assert found[1]["purpose"] == "review" and found[1]["prompt_tokens"] == 7
    assert "message" in found[3] and found[3]["message"] == "timer pass failed"


def test_note_error_does_not_write_again_what_the_trace_has_already(tmp_path):
    trace = TraceLog(tmp_path, "s1")
    log, events, _clock = make_log(trace=trace)
    entry = log.note_error("a1", {"message": "RuntimeError: boom", "error_class": "RuntimeError", "will_retry": True})
    assert entry["kind"] == "event" and entry["status"] == "retry" and entry["purpose"] == "runtime"
    assert records(trace) == [] and len(events) == 1


def test_no_trace_at_all_is_fine(tmp_path):
    log = CallLog("s")
    assert call(log, "error")["status"] == "error"  # nothing to write to: the line stays in memory


def test_a_trace_made_on_demand_is_used_and_closed_when_the_games_own_one_replaces_it(tmp_path):
    made: list[TraceLog] = []

    def make() -> TraceLog:
        made.append(TraceLog(tmp_path, "s1"))
        return made[-1]

    log = CallLog("s1", make_trace=make)
    call(log, "error", message="one")
    call(log, "error", message="two")
    assert len(made) == 1  # made once, on first use
    own = TraceLog(tmp_path, "s1")
    log.attach_trace(own)  # the game started: its trace log is the one now
    call(log, "error", message="three")
    own.close()
    assert [r["message"] for r in records(own)] == ["one", "two", "three"]
    assert made[0]._handle is None  # (the one made on demand was closed)


def test_a_failing_trace_make_is_survived(tmp_path):
    def make() -> TraceLog:
        raise OSError("no disk")

    log = CallLog("s", make_trace=make)
    assert call(log, "error")["status"] == "error" and call(log, "error")["id"] == 2


# ------------------------------------------------------------------------------------- classification
def _rate_limit() -> openai.RateLimitError:
    request = httpx.Request("POST", "https://api.example.invalid/v1/chat/completions")
    return openai.RateLimitError("Rate limit reached", response=httpx.Response(429, request=request), body=None)


@pytest.mark.parametrize(
    "exc, status, http",
    [
        (TimeoutError(), "timeout", None),
        (TimeoutError("took too long"), "timeout", None),
        (httpx.ReadTimeout("slow"), "timeout", None),
        (openai.APITimeoutError(request=httpx.Request("POST", "https://x.invalid")), "timeout", None),
        (_rate_limit(), "error", 429),
        (RuntimeError("boom"), "error", None),
        (type("UpstreamError", (Exception,), {"status_code": 503})("bad gateway"), "error", 503),
    ],
)
def test_failures_are_told_apart_as_timeout_or_error_and_carry_their_http_status(exc, status, http):
    assert failure_status(exc) == status
    assert http_status_of(exc) == http


def test_the_http_status_may_sit_on_the_response_and_must_be_a_real_status():
    assert http_status_of(SimpleNamespace(response=SimpleNamespace(status_code=401))) == 401
    assert http_status_of(SimpleNamespace(status_code=True)) is None
    assert http_status_of(SimpleNamespace(status_code=99)) is None and http_status_of(SimpleNamespace(status_code=700)) is None
    assert http_status_of(ValueError("x")) is None


def test_failure_text_falls_back_to_a_sentence_for_a_bare_timeout_and_to_the_class_for_silence():
    assert "30 秒" in failure_text(TimeoutError(), timeout_s=30.0)
    assert failure_text(TimeoutError()) == "调用超时。"
    assert failure_text(RuntimeError("boom")) == "boom"
    assert failure_text(KeyError()) == "KeyError"


# --------------------------------------------------------------------------------------------- the probe
def test_a_probe_records_every_failed_attempt_and_the_end_of_the_request():
    log, events, _gt = make_log()
    watch = Clock(100.0)
    probe = log.probe("a1", provider_id="prov", model_id="m1", clock=watch)
    probe.start()
    watch.now += 2.0
    probe.on_retry(_rate_limit(), 1, 1.5)  # the first attempt took 2 s and was rejected; wait 1.5 s
    watch.now += 1.5 + 0.5  # the wait, then the second attempt takes 0.5 s ...
    probe.on_retry(RuntimeError("reset"), 2, 3.0)
    watch.now += 3.0 + 4.0
    resp = LLMResponse(
        content="SECRET ANSWER", prompt_tokens=100, completion_tokens=20, cache_read_tokens=30, cache_write_tokens=5,
        finish_reason="tool_calls", provider_id="prov", model_id="m1-2026",
    )
    probe.ok(resp)
    lines = log.entries()
    assert [(c["status"], c["attempt"], c["elapsed_ms"]) for c in lines] == [("retry", 1, 2000), ("retry", 2, 500), ("ok", 3, 4000)]
    first, second, last = lines
    assert (first["http_status"], first["error_class"], first["message"]) == (429, "RateLimitError", "Rate limit reached")
    assert (second["http_status"], second["error_class"], second["message"]) == (None, "RuntimeError", "reset")
    assert (last["prompt_tokens"], last["completion_tokens"], last["cache_read_tokens"], last["cache_write_tokens"]) == (100, 20, 30, 5)
    assert (last["finish_reason"], last["provider_id"], last["model_id"]) == ("tool_calls", "prov", "m1-2026")  # the response's own model wins
    assert last["message"] is None and "SECRET ANSWER" not in json.dumps(log.entries())  # counts, never content
    assert all(c["kind"] == "call" and c["purpose"] == "turn" and c["actor_id"] == "a1" for c in lines)
    assert [e.data["call"]["status"] for e in events] == ["retry", "retry", "ok"] and events[-1].data["errors"] == 0


def test_a_probe_that_ends_in_failure_says_timeout_or_error_with_the_attempt_it_died_in():
    log, _events, _gt = make_log()
    watch = Clock()
    probe = log.probe("a2", "review", provider_id="prov", model_id="m", clock=watch)
    probe.start()
    watch.now += 1.0
    probe.on_retry(RuntimeError("blip"), 1, 0.0)
    watch.now += 7.0
    probe.failed(TimeoutError(), timeout_s=120)
    other = log.probe("a1", "refine", clock=watch)
    other.start()
    other.failed(_rate_limit())
    retry, timed_out, error = log.entries()
    assert (retry["status"], retry["attempt"]) == ("retry", 1)
    assert (timed_out["status"], timed_out["attempt"], timed_out["elapsed_ms"], timed_out["purpose"]) == ("timeout", 2, 7000, "review")
    assert "120 秒" in timed_out["message"] and timed_out["error_class"] == "TimeoutError"
    assert (error["status"], error["http_status"], error["purpose"], error["attempt"]) == ("error", 429, "refine", 1)
    assert (error["provider_id"], error["model_id"]) == (None, None)  # no binding, no response: unknown, not invented


def test_a_cancelled_probe_is_a_cancelled_line_that_is_not_an_error():
    log, _events, _gt = make_log()
    probe = log.probe("a1")
    probe.start()
    probe.cancelled()
    (line,) = log.entries()
    assert line["status"] == "cancelled" and "取消" in line["message"] and (log.errors, log.retries) == (0, 0)


def test_the_stopwatch_of_the_first_attempt_starts_when_the_call_does_not_when_the_probe_is_made():
    log, _events, _gt = make_log()
    watch = Clock(0.0)
    probe = log.probe("a1", clock=watch)
    watch.now += 50.0  # waiting for a slot
    probe.start()
    watch.now += 0.25
    probe.ok(LLMResponse())
    assert log.entries()[0]["elapsed_ms"] == 250


def test_a_probe_never_raises_even_when_the_log_does():
    log = CallLog("s")

    def explode(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("log broken")

    log.add = explode  # type: ignore[method-assign]
    probe = log.probe("a1")
    probe.on_retry(RuntimeError("x"), 1, 0.0)
    assert probe.ok(LLMResponse()) == {} and probe.failed(RuntimeError("y")) == {} and probe.cancelled() == {}


def test_a_nonsense_attempt_number_or_delay_is_tolerated():
    log, _events, _gt = make_log()
    probe = log.probe("a1")
    probe.on_retry(RuntimeError("x"), None, "soon")  # type: ignore[arg-type]
    probe.on_retry(RuntimeError("y"), 0, None)  # type: ignore[arg-type]
    assert [c["attempt"] for c in log.entries()] == [1, 2]


# --------------------------------------------------------------------------------------- the agents' trace
def test_the_logging_trace_passes_everything_on_and_logs_the_error_events():
    inner = RecordingTrace()
    log, events, _clock = make_log()
    sink: TraceSink = LoggingTrace(inner, log)
    sink.write("wake", "a1", gen=1)
    sink.write("error", "a1", gen=2, attempt=1, message="TimeoutError: ", error_class="TimeoutError", will_retry=True, traceback="...")
    sink.write("retry", "a1", gen=2, attempt=1, delay_s=1.0)
    sink.write("error", "a2", gen=5, attempt=3, message="RuntimeError: tool speak raised ValueError: bad", will_retry=False, http_status=500)
    sink.write("tool_call", "a1", name="speak")
    assert inner.names() == ["wake", "error", "retry", "error", "tool_call"]  # nothing lost, nothing changed
    assert inner.of("error", "a1")[0]["traceback"] == "..."
    first, second = log.entries()
    assert (first["kind"], first["status"], first["purpose"], first["actor_id"], first["actor_name"]) == ("event", "retry", "runtime", "a1", "Alice")
    assert first["message"] == "TimeoutError" and first["error_class"] == "TimeoutError" and first["attempt"] is None
    assert (second["status"], second["actor_name"], second["http_status"]) == ("error", "Bob", 500)
    assert second["message"] == "RuntimeError: tool speak raised ValueError: bad"
    assert [e.data["call"]["id"] for e in events] == [1, 2] and (log.errors, log.retries) == (1, 1)


def test_the_logging_trace_reaches_what_the_inner_sink_offers_and_survives_a_broken_log(tmp_path):
    inner = TraceLog(tmp_path, "s1")
    log, _events, _clock = make_log()
    sink = LoggingTrace(inner, log)
    sink.write("wake", "a1")
    assert [p.name for p in sink.files()] == ["trace-0001.jsonl"]  # type: ignore[attr-defined]
    sink.close()  # type: ignore[attr-defined]
    log.note_error = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("log broken"))  # type: ignore[method-assign]
    sink.write("error", "a1", message="x")  # does not raise ...
    assert [r["event"] for r in records(inner)] == ["wake", "error"]  # ... and the trace still has it
    with pytest.raises(AttributeError):
        sink.nothing_like_this  # type: ignore[attr-defined]


# ------------------------------------------------------------------------------------ rebuild from a trace
def agent_call(trace: TraceSink, agent: str = "a1", **extra: Any) -> None:
    """The ``llm_call`` event an agent writes for a call that succeeded (``Agent._account_llm``)."""
    fields: dict[str, Any] = dict(
        gen=1, round=0, epoch=1, provider="prov", model="m1", prompt_tokens=100, cache_read_tokens=10, cache_write_tokens=0,
        completion_tokens=20, finish_reason="stop", tool_calls=["speak"], elapsed_ms=900, gt=10.0,
    )
    fields.update(extra)
    trace.write("llm_call", agent, **fields)


def rows_without_time(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in row.items() if k not in ("id", "ts")} for row in rows]


def test_a_rebuilt_log_shows_what_the_live_log_showed(tmp_path):
    trace = TraceLog(tmp_path, "s1")
    live, _events, _clock = make_log(trace=trace)
    tee = LoggingTrace(trace, live)

    def ok_turn(**fields: Any) -> None:  # what a successful call leaves: a line in the log and an event in the trace
        call(live, "ok", provider_id="prov", model_id="m1", prompt_tokens=100, cache_read_tokens=10, cache_write_tokens=0,
             completion_tokens=20, finish_reason="stop", elapsed_ms=900, **fields)
        agent_call(tee)

    ok_turn(attempt=1)
    call(live, "retry", provider_id="prov", model_id="m1", attempt=1, elapsed_ms=300, http_status=429, error_class="RateLimitError", message="slow down")
    call(live, "retry", provider_id="prov", model_id="m1", attempt=2, elapsed_ms=250, error_class="APIConnectionError", message="reset")
    ok_turn(attempt=3)
    call(live, "timeout", actor="a2", provider_id="prov", model_id="m2", attempt=1, elapsed_ms=120000, error_class="TimeoutError", message="调用超过 120 秒仍未返回，已放弃。")
    tee.write("error", "a2", gen=3, attempt=1, message="TimeoutError: ", error_class="TimeoutError", will_retry=True, traceback="tb")
    tee.write("retry", "a2", gen=3, attempt=1, delay_s=1.0)
    call(live, "cancelled", actor="a2", provider_id="prov", model_id="m2", attempt=1, elapsed_ms=40, message="调用被取消（暂停、停止，或被新消息打断）。")
    call(live, "error", actor="moderator", purpose="refine", provider_id="prov", model_id="m3", attempt=1, elapsed_ms=10, http_status=401, error_class="AuthenticationError", message="bad key")
    call(live, "ok", actor="a1", purpose="review", provider_id="prov", model_id="m1", attempt=1, elapsed_ms=5, prompt_tokens=7, completion_tokens=3, cache_read_tokens=0, cache_write_tokens=0, finish_reason="stop")
    tee.write("error", "a2", gen=4, attempt=3, message="RuntimeError: tool speak raised ValueError: bad", error_class="RuntimeError", will_retry=False)
    tee.write("give_up", "a2", gen=4, message="RuntimeError: tool speak raised ValueError: bad")
    live.note_runtime_error("世界的计时器处理出错，已记录日志。")
    trace.close()

    rebuilt = CallLog("s1", clock=Clock(), names=lambda actor: NAMES.get(actor, actor))
    announced: list[UiEvent] = []
    rebuilt.publish = announced.append
    size = sum(p.stat().st_size for p in trace.files())
    assert rebuilt.load_from_trace(trace.directory) == len(live.entries()) == 11
    assert rows_without_time(rebuilt.entries()) == rows_without_time(live.entries())
    assert [e["id"] for e in rebuilt.entries()] == list(range(1, 12))
    assert (rebuilt.errors, rebuilt.retries) == (live.errors, live.retries) == (4, 3)
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", e["ts"]) for e in rebuilt.entries())  # UTC, like the live ones
    assert announced == [] and sum(p.stat().st_size for p in trace.files()) == size  # nothing announced, nothing written back


def test_an_init_line_is_an_init_line_again_after_a_rebuild_from_the_trace(tmp_path):
    """v0.14: a bot's initialization is logged with ``purpose = "init"``; the log rebuilt from the trace (restart, a loaded game)
    keeps it (it used to fall back to ``turn``)."""
    trace = TraceLog(tmp_path, "s1")
    live, _events, _clock = make_log(trace=trace)
    # (a learning run has no Agent: the call log's own trace event is the only record of the call, as for a review)
    call(live, "ok", actor="a1", purpose="init", provider_id="prov", model_id="m1", attempt=1, elapsed_ms=5, prompt_tokens=7,
         completion_tokens=3, cache_read_tokens=0, cache_write_tokens=0, finish_reason="stop")
    trace.close()

    assert [e["purpose"] for e in live.entries()] == ["init"]
    rebuilt = CallLog("s1", clock=Clock(), names=lambda actor: NAMES.get(actor, actor))
    assert rebuilt.load_from_trace(trace.directory) == 1
    assert [e["purpose"] for e in rebuilt.entries()] == ["init"]


def test_an_ok_call_after_retries_gets_its_attempt_number_from_the_retries_before_it(tmp_path):
    trace = TraceLog(tmp_path, "s1")
    trace.write("llm_call", "a1", status="retry", attempt=1, provider="p", model="m", http_status=500)
    trace.write("llm_call", "a2", status="retry", attempt=1, provider="p", model="m", http_status=500)
    trace.write("llm_call", "a2", status="retry", attempt=2, provider="p", model="m", http_status=500)
    agent_call(trace, "a1")  # a1: its second attempt worked
    agent_call(trace, "a2")  # a2: its third attempt worked
    agent_call(trace, "a1")  # a fresh request: attempt 1 again
    trace.close()
    log = CallLog("s1")
    log.load_from_trace(trace.directory)
    assert [(e["actor_id"], e["status"], e["attempt"]) for e in log.entries()] == [
        ("a1", "retry", 1), ("a2", "retry", 1), ("a2", "retry", 2), ("a1", "ok", 2), ("a2", "ok", 3), ("a1", "ok", 1),
    ]


def test_a_trace_from_before_v0_13_1_gives_ok_calls_and_errors_that_say_what_the_agent_did_next(tmp_path):
    trace = TraceLog(tmp_path, "old")
    agent_call(trace, "a1", gt=None)  # (no gt, no status: v0.13.0 wrote neither)
    trace.write("error", "a1", gen=2, attempt=1, message="RuntimeError: x", traceback="tb")  # no will_retry
    trace.write("retry", "a1", gen=2, attempt=1, delay_s=1.0)
    trace.write("error", "a1", gen=2, attempt=3, message="RuntimeError: x", traceback="tb")
    trace.write("give_up", "a1", gen=2, message="RuntimeError: x")
    trace.write("error", "a2", gen=7, message="tool speak raised KeyError: 'x'")  # alone: a tool failure
    trace.write("thought", "a1", text="not a line of the log")
    trace.write("tool_call", "a1", name="speak")
    trace.close()
    log = CallLog("old")
    log.load_from_trace(trace.directory)
    assert [(e["kind"], e["status"], e["actor_id"]) for e in log.entries()] == [
        ("call", "ok", "a1"), ("event", "retry", "a1"), ("event", "error", "a1"), ("event", "error", "a2"),
    ]
    ok = log.entries()[0]
    assert (ok["gt"], ok["purpose"], ok["attempt"], ok["model_id"], ok["provider_id"]) == (None, "turn", 1, "m1", "prov")


def test_a_retry_marker_of_another_run_does_not_change_an_error(tmp_path):
    trace = TraceLog(tmp_path, "s1")
    trace.write("error", "a1", gen=2, message="first")
    trace.write("retry", "a1", gen=9, attempt=1)  # another run's marker
    trace.close()
    log = CallLog("s1")
    log.load_from_trace(trace.directory)
    assert [e["status"] for e in log.entries()] == ["error"]


def test_a_rebuild_keeps_the_newest_lines_when_the_trace_has_more_than_the_ring_holds(tmp_path):
    trace = TraceLog(tmp_path, "s1", max_bytes=2000)  # several files: the scan crosses them
    for n in range(60):
        trace.write("llm_call", "a1", status="error", attempt=1, message=f"failure {n}", provider="p", model="m")
    trace.close()
    assert len(trace.files()) > 2
    log = CallLog("s1", capacity=25)
    assert log.load_from_trace(trace.directory) == 25
    assert [e["message"] for e in log.entries()] == [f"failure {n}" for n in range(35, 60)]
    assert [e["id"] for e in log.entries()] == list(range(1, 26))  # numbered afresh from 1
    log.add(kind="event", status="error", actor_id="engine", purpose="runtime", message="next")
    assert log.last_id == 26  # and the log goes on from there


def test_a_rebuild_ignores_garbage_and_other_events_and_a_missing_directory(tmp_path):
    log = CallLog("s")
    assert log.load_from_trace(tmp_path / "nowhere") == 0
    directory = tmp_path / "s"
    directory.mkdir()
    (directory / "trace-0001.jsonl").write_bytes(
        b'not json at all\n'
        b'{"event": "llm_call", "agent_id": "a1", "status": "error", "attempt": 2, "message": "ok line"}\n'
        b'[1, 2, 3]\n'
        b'{"event": "llm_call", "agent_id": "a1", "status": "weird", "prompt_tokens": true, "purpose": ["x"]}\n'
        b'{"event": "thought", "agent_id": "a1", "text": "x"}\n'
        b'{"event": "llm_call", "agent_id": "a1", "status": "ret'  # the last line is still being written
    )
    assert log.load_from_trace(directory) == 2
    first, second = log.entries()
    assert (first["status"], first["attempt"], first["message"]) == ("error", 2, "ok line")
    assert (second["status"], second["purpose"], second["prompt_tokens"]) == ("ok", "turn", None)  # unknown values fall back


def test_the_rebuild_scrubs_again_what_it_reads(tmp_path):
    directory = tmp_path / "s"
    directory.mkdir()
    (directory / "trace-0001.jsonl").write_text(
        json.dumps({"event": "error", "agent_id": "a1", "message": "leaked sk-abcdef1234567890"}) + "\n", encoding="utf-8"
    )
    log = CallLog("s")
    log.load_from_trace(directory)
    assert "abcdef1234567890" not in json.dumps(log.entries())


def test_the_rebuild_reads_the_trace_from_its_end_and_stops_when_the_ring_is_full(tmp_path, monkeypatch):
    trace = TraceLog(tmp_path, "s1")
    for n in range(300):
        trace.write("llm_call", "a1", status="error", attempt=1, message=f"failure {n}", pad="x" * 200)
    trace.close()
    import app.runtime.trace_log as trace_module

    scanned: list[int] = []
    real = trace_module.iter_lines_backwards

    def counting(path: Any, **kwargs: Any):
        for offset, line in real(path, **kwargs):
            scanned.append(offset)
            yield offset, line

    monkeypatch.setattr(trace_module, "iter_lines_backwards", counting)
    log = CallLog("s1", capacity=10)
    log.load_from_trace(trace.directory)
    assert len(scanned) <= 12 and log.entries()[-1]["message"] == "failure 299"  # 10 lines, not 300


def test_rebuilding_does_not_depend_on_a_clock_a_name_book_or_a_listener(tmp_path):
    trace = TraceLog(tmp_path, "s1")
    trace.write("llm_call", "a1", status="timeout", attempt=1, message="late")
    trace.close()
    log = CallLog("s1")  # no clock: the game time of the line is what the trace says (nothing), not "now"
    log.load_from_trace(trace.directory)
    (line,) = log.entries()
    assert line["gt"] is None and line["actor_name"] == "a1"


def test_a_log_keeps_its_session_id_and_reads_a_directory_given_as_text_or_path(tmp_path):
    assert CallLog("abc").session_id == "abc" and CallLog(123).session_id == "123"  # type: ignore[arg-type]
    trace = TraceLog(tmp_path, "s1")
    trace.write("llm_call", "a1", status="error", attempt=1, message="x")
    trace.close()
    assert CallLog("s1").load_from_trace(str(trace.directory)) == 1
    assert CallLog("s1").load_from_trace(Path(trace.directory)) == 1


# =============================================================================== after the review (v0.13.1)
# 1. a trace log the call log had to open for a line must not stay open (Windows cannot move an open file's folder)
class LeakySink:
    """A trace sink that cannot say whether it is open: ``close`` must never be called on it by the call log."""

    def __init__(self) -> None:
        self.written: list[tuple[str, str]] = []
        self.closed = 0

    def write(self, event: str, agent_id: str, **fields: Any) -> None:
        self.written.append((event, agent_id))

    def close(self) -> None:
        self.closed += 1


def test_a_line_written_while_the_trace_was_closed_closes_it_again_so_the_folder_can_be_moved(tmp_path):
    trace = TraceLog(tmp_path, "s1")
    log, _events, _clock = make_log(trace=trace)
    assert trace.is_open is False
    call(log, "error", message="one")
    assert trace.is_open is False  # opened for this one line, closed again
    call(log, "cancelled", message="two")
    call(log, "ok", actor="a2", purpose="review", prompt_tokens=1)
    log.note_runtime_error("a timer pass failed")
    assert trace.is_open is False
    assert len(trace.files()) == 1
    moved = tmp_path / "moved"
    os.replace(trace.directory, moved)  # (an open file would make Windows refuse)
    lines = [json.loads(line) for line in (moved / "trace-0001.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["event"], r.get("status")) for r in lines] == [
        ("llm_call", "error"), ("llm_call", "cancelled"), ("llm_call", "ok"), ("error", "error"),
    ]


def test_a_trace_that_is_open_already_is_left_open_and_is_not_closed_for_every_line(tmp_path):
    trace = TraceLog(tmp_path, "s1")
    closes: list[int] = []
    real_close = trace.close

    def counting_close() -> None:
        closes.append(1)
        real_close()

    trace.close = counting_close  # type: ignore[method-assign]
    log, _events, _clock = make_log(trace=trace)
    trace.write("wake", "a1", gen=1)  # the game runs: its agents write to this trace and keep it open
    assert trace.is_open is True
    for n in range(6):
        call(log, "error", message=f"failure {n}")
    log.note_runtime_error("x")
    assert trace.is_open is True and closes == []  # the running game's trace is not touched
    trace.close()
    assert closes == [1] and trace.is_open is False  # (only the supervisor closes it, when it stops)
    call(log, "error", message="after the pause")  # a paused game: closed, so closed again
    assert closes == [1, 1] and trace.is_open is False


def test_a_trace_made_on_demand_for_a_review_holds_no_file_between_lines(tmp_path):
    made: list[TraceLog] = []

    def make() -> TraceLog:
        made.append(TraceLog(tmp_path, "s1"))
        return made[-1]

    log = CallLog("s1", make_trace=make)
    for n in range(3):
        call(log, "ok", actor=f"a{n}", purpose="review", prompt_tokens=n)
        assert len(made) == 1 and made[0].is_open is False
    call(log, "ok", purpose="refine", actor="roster")
    assert made[0].is_open is False and len(records(made[0])) == 4


def test_a_sink_that_cannot_say_whether_it_is_open_is_left_alone():
    sink = LeakySink()
    log = CallLog("s", trace=sink)
    call(log, "error", message="x")
    log.note_runtime_error("y")
    assert [event for event, _ in sink.written] == ["llm_call", "error"] and sink.closed == 0
    plain = RecordingTrace()  # (no ``close`` at all)
    assert call(CallLog("s", trace=plain), "error")["status"] == "error" and plain.names() == ["llm_call"]


def test_a_sink_whose_is_open_raises_is_taken_to_be_open_and_left_alone():
    class Odd(LeakySink):
        @property
        def is_open(self) -> bool:
            raise RuntimeError("cannot say")

    sink = Odd()
    call(CallLog("s", trace=sink), "error")
    assert sink.written == [("llm_call", "a1")] and sink.closed == 0


def test_a_trace_that_refuses_to_close_never_breaks_the_line(tmp_path):
    class Stubborn(TraceLog):
        def close(self) -> None:
            raise OSError("handle is busy")

    sink = Stubborn(tmp_path, "s1")
    log = CallLog("s1", trace=sink)
    assert call(log, "error")["status"] == "error" and call(log, "cancelled")["id"] == 2
    sink._drop_handle()  # (what the real close does: let go, so that the folder can be removed afterwards)


def test_the_rebuild_lets_go_of_the_files_when_it_stops_early(tmp_path, monkeypatch):
    trace = TraceLog(tmp_path, "s1", max_bytes=1500)
    for n in range(60):
        trace.write("llm_call", "a1", status="error", attempt=1, message=f"failure {n}", pad="x" * 100)
    trace.close()
    assert len(trace.files()) > 2
    handles: list[Any] = []
    kept: list[Any] = []  # (the test keeps every generator alive: only an explicit close lets go of a file then)
    import app.runtime.trace_log as trace_module

    real_open, real_lines, real_scan = open, trace_module.iter_lines_backwards, call_log_module.scan_events_backwards

    def tracking_open(path: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real_open(path, mode, *args, **kwargs)
        handles.append(handle)
        return handle

    def keeping_lines(*args: Any, **kwargs: Any) -> Any:
        kept.append(real_lines(*args, **kwargs))
        return kept[-1]

    def keeping_scan(*args: Any, **kwargs: Any) -> Any:
        kept.append(real_scan(*args, **kwargs))
        return kept[-1]

    monkeypatch.setattr(trace_module, "open", tracking_open, raising=False)
    monkeypatch.setattr(trace_module, "iter_lines_backwards", keeping_lines)
    monkeypatch.setattr(call_log_module, "scan_events_backwards", keeping_scan)
    log = CallLog("s1", capacity=3)
    assert log.load_from_trace(trace.directory) == 3
    assert handles and all(handle.closed for handle in handles)


# 2. nothing that goes wrong in the log may change what a model call does: the hostile values
class BadStr(Exception):
    """An exception whose own text cannot be made."""

    def __str__(self) -> str:
        raise ValueError("__str__ exploded")


class BadAttrs(Exception):
    """An exception whose HTTP attributes raise when they are looked at."""

    @property
    def status_code(self) -> int:
        raise RuntimeError("status_code exploded")

    @property
    def response(self) -> Any:
        raise RuntimeError("response exploded")


class Hostile:
    """An 'answer' whose every attribute raises."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"no {name}")


def test_an_exception_that_cannot_tell_its_text_is_still_a_line_told_by_its_class():
    log, _events, _gt = make_log()
    probe = log.probe("a1")
    probe.on_retry(BadStr("first"), 1, 0.0)
    probe.failed(BadStr("second"))
    probe.failed(BadAttrs("third"))
    retry, failed, odd = log.entries()
    assert (retry["status"], retry["error_class"], retry["message"]) == ("retry", "BadStr", "BadStr")
    assert (failed["status"], failed["error_class"], failed["message"]) == ("error", "BadStr", "BadStr")
    assert (odd["status"], odd["error_class"], odd["http_status"], odd["message"]) == ("error", "BadAttrs", None, "third")
    assert failure_text(BadStr("x")) == "BadStr" and http_status_of(BadAttrs("x")) is None


def test_an_answer_whose_attributes_raise_is_still_a_line_without_the_numbers():
    log, _events, _gt = make_log()
    probe = log.probe("a1", provider_id="prov", model_id="m-bound")
    returned = probe.ok(Hostile())
    (line,) = log.entries()
    assert returned is not None and line["status"] == "ok"
    assert (line["provider_id"], line["model_id"]) == ("prov", "m-bound")  # the binding is what is known
    assert line["prompt_tokens"] is None and line["finish_reason"] is None


@pytest.mark.parametrize("delay", [float("nan"), float("inf"), -5.0, "soon", None])
def test_a_nonsense_wait_before_a_repeat_is_ignored_and_the_next_attempt_is_timed_from_now(delay):
    log, _events, _gt = make_log()
    watch = Clock(0.0)
    probe = log.probe("a1", clock=watch)
    probe.start()
    probe.on_retry(RuntimeError("x"), 1, delay)  # type: ignore[arg-type]
    watch.now += 2.0
    probe.ok(LLMResponse())
    assert [c["status"] for c in log.entries()] == ["retry", "ok"] and log.entries()[1]["elapsed_ms"] == 2000


def test_a_probe_whose_clock_raises_or_runs_backwards_never_reaches_the_caller():
    log, _events, _gt = make_log()
    watch = Clock(100.0)
    probe = log.probe("a1", clock=watch)
    probe.start()
    watch.now = 50.0  # (a clock that jumped back: the time is 0, not negative)
    probe.ok(LLMResponse())
    assert log.entries()[0]["elapsed_ms"] == 0

    def broken() -> float:
        raise RuntimeError("clock broken")

    probe._clock = broken  # type: ignore[assignment]
    probe.start()
    assert probe.ok(LLMResponse()) == {} and probe.failed(RuntimeError("x")) == {} and probe.cancelled() == {}
    probe.on_retry(RuntimeError("y"), 1, 1.0)  # (and the hook of the client's retry)
    assert len(log) == 1


# 5. what an imported trace says is not trusted to be small, or to be a number
def test_the_short_fields_of_a_line_are_cut_at_120_characters_and_the_message_at_500():
    huge = "z" * 5000
    log = CallLog("s")
    entry = log.add(
        kind="call", status="error", actor_id=huge, actor_name=huge, purpose="turn", provider_id=huge, model_id=huge,
        finish_reason=huge, error_class=huge, message=huge,
    )
    for key in ("actor_id", "actor_name", "provider_id", "model_id", "finish_reason", "error_class"):
        assert len(entry[key]) == MAX_FIELD_CHARS == 120 and entry[key].endswith("…"), key
    assert len(entry["message"]) == 500 and entry["message"].endswith("…")
    named = CallLog("s", names=lambda actor: huge).add(kind="event", status="error", actor_id="a1", purpose="runtime")
    assert len(named["actor_name"]) == 120 and named["actor_id"] == "a1"
    short = log.add(kind="call", status="ok", actor_id="N3", purpose="turn", provider_id="  deepseek  ", model_id="")
    assert (short["actor_id"], short["actor_name"], short["provider_id"], short["model_id"]) == ("N3", "N3", "deepseek", None)
    exact = log.add(kind="call", status="ok", actor_id="a" * 120, purpose="turn")
    assert exact["actor_id"] == "a" * 120  # (what fits is not touched)
    odd = log.add(kind="call", status="ok", actor_id="N1", purpose="turn", provider_id=BadStr("x"), model_id=["a", ["b"]], message=BadStr("y"))
    assert (odd["provider_id"], odd["model_id"], odd["message"]) == (None, "['a', ['b']]", None)  # (no text to be had: none, not a crash)


def test_a_game_time_that_is_not_a_finite_number_is_unknown_and_every_line_can_be_sent_as_json():
    log = CallLog("s", clock=lambda: float("nan"))
    assert call(log)["gt"] is None
    assert call(log, gt=float("inf"))["gt"] is None and call(log, gt=float("-inf"))["gt"] is None
    assert call(log, gt=True)["gt"] is None and call(log, gt="12")["gt"] is None and call(log, gt=7)["gt"] == 7
    assert CallLog("s", clock=lambda: 2.5).game_time() == 2.5 and CallLog("s", clock=lambda: None).game_time() is None
    json.dumps(log.entries(), allow_nan=False)  # what the API does with the lines


def test_a_trace_made_by_hand_cannot_break_or_bloat_the_rebuild(tmp_path):
    directory = tmp_path / "s"
    directory.mkdir()
    huge = "H" * 200_000
    lines = [
        b'{"event":"llm_call","agent_id":"a1","status":"error","gt":NaN,"elapsed_ms":Infinity,"attempt":NaN,"message":"nan values"}',
        b'{"event":"error","agent_id":"a1","gt":-Infinity,"message":"x"}',
        json.dumps(
            {"event": "llm_call", "agent_id": huge, "status": "error", "provider": huge, "model": huge, "error_class": huge,
             "finish_reason": huge, "message": huge, "ts": "0001-01-01T00:00:00+05:00"}
        ).encode(),
        b'{"event":"llm_call","agent_id":"a2","status":"error","message":"deep","x":' + b"[" * 100_000 + b"]" * 100_000 + b"}",
        b'{"event":"llm_call","event":["x"],"agent_id":"a3"}',  # (a repeated key: the last value wins, and it is no event)
        b'{"event":"error","agent_id":{"a":1},"message":["m"],"will_retry":"yes"}',
        b'{"event":"llm_call","agent_id":"a4","status":["retry"],"purpose":{"p":1}}',
        b'{"event":"llm_call","agent_id":"a5","status":"error","message":"fine"}',
    ]
    (directory / "trace-0001.jsonl").write_bytes(b"\n".join(lines) + b"\n")
    log = CallLog("s")
    assert log.load_from_trace(directory) == 6  # the line nested too deeply for the parser and the one without an event are gone
    first, second, long, odd, fallback, fine = log.entries()
    assert (first["gt"], first["elapsed_ms"], first["attempt"], first["message"]) == (None, None, 1, "nan values")
    assert second["gt"] is None
    for key in ("actor_id", "actor_name", "provider_id", "model_id", "error_class", "finish_reason"):
        assert len(long[key]) <= 120, key
    assert len(long["message"]) == 500 and long["ts"].endswith("Z")  # (a time that cannot be converted: now, not a crash)
    assert odd["kind"] == "event" and odd["status"] == "retry" and odd["message"] == "['m']"
    assert (fallback["status"], fallback["purpose"]) == ("ok", "turn")  # unknown values fall back
    assert fine["message"] == "fine"
    text = json.dumps(log.entries(), allow_nan=False)
    assert len(text) < 8000  # (not the 200 000-character values it was given, several times over)
