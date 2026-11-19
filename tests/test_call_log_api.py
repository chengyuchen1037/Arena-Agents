"""The error log of a game (v0.13.1, G32-G36; contract docs/v0.12-contracts.md 12.3) on the real orchestrator: every model call and
every runtime error is a line, the lines are announced as ``llm.call`` events, ``GET /api/llm/calls`` serves them to trusted
clients, and a game that is loaded later gets its log back from its trace.  Scripted agents; nothing calls a model."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.calls as calls_module
import app.game.orchestrator as orchestrator_module
from app.game.orchestrator import AUTOSAVE_EVENT_TYPES, GameOrchestrator
from app.game.takeover_runtime import SEAT_QUIET_EVENTS
from app.llm.types import LLMResponse
from app.main import app as real_app
from app.runtime.bus import ORDINARY_TYPES
from app.runtime.call_log import CallLog
from app.runtime.trace_log import TraceLog, trace_dir
from app.storage.roster import RosterError
from app.world.types import MODERATOR_ID, UiEvent
from game_fakes import CFG, PROVIDER, FakeGames, FakeProviders, FakeSettings, async_test, bound
from v12.bots import BotLlm, Call, simple_player

IDS = ["N1", "N2", "N3", "N4"]
NAMES = ["Alice", "Bob", "Carol", "David"]
CALL_KEYS = [
    "id", "ts", "gt", "kind", "status", "actor_id", "actor_name", "purpose", "provider_id", "model_id", "attempt",
    "elapsed_ms", "prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens", "finish_reason",
    "http_status", "error_class", "message",
]
CFG4: dict[str, Any] = {
    **copy.deepcopy(CFG),
    "participants": [{"id": pid, "seat": str(i), "name": name} for i, (pid, name) in enumerate(zip(IDS, NAMES), start=1)],
}
TOKEN = {"X-Arena-Token": "test-token"}


# ------------------------------------------------------------------------------------------------ helpers
def make_orch(monkeypatch: pytest.MonkeyPatch) -> GameOrchestrator:
    monkeypatch.setattr(orchestrator_module, "load_config", lambda *a, **k: copy.deepcopy(CFG4))
    orch = GameOrchestrator()
    orch.provider_store = FakeProviders(PROVIDER)
    orch.settings_store = FakeSettings({a: bound() for a in [MODERATOR_ID, *IDS]})
    orch.game_store = FakeGames()
    orch._autosave_delay = 0.01
    orch._reset_game(default_bindings=orch._load_defaults())
    orch.refresh_binding_issues()
    return orch


@pytest.fixture(autouse=True)
def fast_agent_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent whose action failed waits a few milliseconds, not a second, before it tries again."""
    real = orchestrator_module.AgentConfig
    monkeypatch.setattr(orchestrator_module, "AgentConfig", lambda **kw: real(retry_backoff_s=0.01, retry_backoff_max_s=0.02, **kw))


class FlakyLlm(BotLlm):
    """Scripted agents; the model of the agents in ``fail`` raises (a callable decides what and when)."""

    def __init__(self, world: Any, policies: dict[str, Any], fail: dict[str, Any] | None = None) -> None:
        super().__init__(world, policies)
        self.fail = fail or {}
        self.entered: asyncio.Event = asyncio.Event()

    async def complete(self, agent_id: str, messages: list[dict[str, Any]], tools: Any, *, max_tokens: int | None = None) -> LLMResponse:
        failure = self.fail.get(agent_id)
        if failure is not None:
            if isinstance(failure, BaseException):
                raise failure
            await failure(self)  # (a coroutine function: hangs, or fails later)
        return await super().complete(agent_id, messages, tools, max_tokens=max_tokens)


def chatty(world: Any, me: str) -> list[Call]:
    """Opens a free discussion once; the players then answer each other: continuous activity."""
    if world.channel("public") is not None:
        return []
    return [
        ("create_channel", {"channel_id": "public", "name": "公共频道", "members": IDS, "type": "public"}),
        ("open_floor", {"channel_id": "public", "mode": "free", "limits": {"max_messages_per_member": 4}}),
    ]


def play_with(orch: GameOrchestrator, fail: dict[str, Any] | None = None, moderator: Any = chatty) -> list[FlakyLlm]:
    made: list[FlakyLlm] = []

    def factory(o: GameOrchestrator) -> FlakyLlm:
        bot = FlakyLlm(o.world, {MODERATOR_ID: moderator, **{pid: simple_player for pid in IDS}}, fail)
        made.append(bot)
        return bot

    orch.ensure_bindings_ready = lambda: None  # type: ignore[method-assign]
    orch.llm_port_factory = factory
    return made


async def wait_for(predicate: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for the condition")
        await asyncio.sleep(0.02)


def events_of(orch: GameOrchestrator, *types: str) -> list[UiEvent]:
    replay, _ = orch.bus.subscribe("0")
    return [e for e in (replay or []) if e.type in types]


@contextlib.contextmanager
def make_client(orch: GameOrchestrator, monkeypatch: pytest.MonkeyPatch, *, remote: bool = False) -> Iterator[TestClient]:
    """A client of the real application (every router of ``app.main``).  It is not entered as a context - that would run
    the application's own start-up and build its own orchestrator - the endpoint under test talks to ``orch``."""
    monkeypatch.setattr(calls_module, "orchestrator", orch)
    yield TestClient(real_app, client=("203.0.113.5", 50000)) if remote else TestClient(real_app)


@pytest.fixture(autouse=True)
def no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARENA_API_TOKEN", raising=False)


# ------------------------------------------------------------------------------------- the endpoint
def test_an_empty_log_answers_with_the_contract_shape(monkeypatch):
    orch = make_orch(monkeypatch)
    with make_client(orch, monkeypatch) as client:
        response = client.get("/api/llm/calls")
    assert response.status_code == 200
    assert response.json() == {"calls": [], "last_id": 0, "total": 0, "errors": 0, "retries": 0}


def test_the_endpoint_serves_the_lines_of_the_live_game_with_its_filters(monkeypatch):
    orch = make_orch(monkeypatch)
    log = orch.call_log()
    for status in ["ok", "retry", "error", "ok", "cancelled", "timeout"]:
        log.add(kind="call", status=status, actor_id="N1", purpose="turn", attempt=1, message="x" if status != "ok" else None)
    with make_client(orch, monkeypatch) as client:
        everything = client.get("/api/llm/calls").json()
        assert [c["id"] for c in everything["calls"]] == [1, 2, 3, 4, 5, 6]
        assert (everything["last_id"], everything["total"], everything["errors"], everything["retries"]) == (6, 6, 2, 1)
        assert all(list(c) == CALL_KEYS for c in everything["calls"])
        assert [c["id"] for c in client.get("/api/llm/calls?after=4").json()["calls"]] == [5, 6]
        assert [c["status"] for c in client.get("/api/llm/calls?errors_only=true").json()["calls"]] == ["retry", "error", "timeout"]
        assert [c["id"] for c in client.get("/api/llm/calls?limit=2").json()["calls"]] == [5, 6]  # the newest two
        assert client.get("/api/llm/calls?after=6").json()["calls"] == []
        assert client.get("/api/llm/calls?errors_only=false&limit=2000&after=0").status_code == 200


@pytest.mark.parametrize("query", ["after=-1", "after=abc", "limit=0", "limit=2001", "limit=x", "errors_only=maybe"])
def test_bad_query_values_are_a_422_not_a_crash(monkeypatch, query):
    orch = make_orch(monkeypatch)
    with make_client(orch, monkeypatch) as client:
        assert client.get(f"/api/llm/calls?{query}").status_code == 422


def test_only_a_trusted_client_may_read_the_log(monkeypatch):
    orch = make_orch(monkeypatch)
    with make_client(orch, monkeypatch, remote=True) as remote:
        denied = remote.get("/api/llm/calls")
        assert denied.status_code == 403 and "local" in denied.json()["detail"]
    monkeypatch.setenv("ARENA_API_TOKEN", "test-token")
    with make_client(orch, monkeypatch, remote=True) as remote:
        assert remote.get("/api/llm/calls").status_code == 401
        assert remote.get("/api/llm/calls", headers={"X-Arena-Token": "wrong"}).status_code == 401
        assert remote.get("/api/llm/calls", headers=TOKEN).status_code == 200  # a token makes any client trusted
    with make_client(orch, monkeypatch) as local:
        assert local.get("/api/llm/calls").status_code == 401  # ... and is then required of everybody


@async_test
async def test_the_log_can_be_read_while_a_seat_is_taken_over(monkeypatch):
    orch = make_orch(monkeypatch)
    await orch.takeover("N2")
    orch.call_log().add(kind="call", status="ok", actor_id="N1", purpose="turn")
    assert orch.call_log_payload()["total"] == 1  # (only metadata: nothing a player must not see)
    with make_client(orch, monkeypatch) as client:
        assert client.get("/api/llm/calls").status_code == 200


# -------------------------------------------------------------------------------------- the event
def test_llm_call_is_an_ordinary_event_that_changes_neither_the_save_nor_the_seat_state():
    assert "llm.call" in ORDINARY_TYPES  # a slow client may lose it: it asks GET /api/llm/calls?after=
    assert "llm.call" not in AUTOSAVE_EVENT_TYPES  # no save because of a line of the log
    assert "llm.call" in SEAT_QUIET_EVENTS  # ... and no recomputation of the state of the seat a person plays


@async_test
async def test_a_line_is_published_as_llm_call_through_the_orchestrator_without_waking_the_seat_push(monkeypatch):
    orch = make_orch(monkeypatch)
    await orch.takeover("N2")
    orch._takeover_push_scheduled = False
    before = orch.bus.last_id
    orch.call_log().add(kind="call", status="error", actor_id="N1", purpose="turn", message="boom")
    (event,) = [e for e in orch.bus._ring if e.id > before]
    assert event.type == "llm.call" and set(event.data) == {"call", "errors"} and event.data["errors"] == 1
    assert event.data["call"]["actor_name"] == "Alice" and event.data["call"]["status"] == "error"
    assert orch._takeover_push_scheduled is False  # nothing the person plays is affected: no push was scheduled


# ---------------------------------------------------------------------------- a game that is played
@async_test
async def test_a_played_game_is_a_log_of_ok_calls_with_names_game_time_tokens_and_no_content(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch)
    await orch.start_or_resume()
    log = orch.call_log()
    await wait_for(lambda: len(log) >= 12)
    await orch.pause()
    lines = log.entries()
    assert {c["kind"] for c in lines} == {"call"} and {c["status"] for c in lines} == {"ok"}
    assert {c["actor_id"] for c in lines} >= {MODERATOR_ID, "N1"}
    names = {c["actor_id"]: c["actor_name"] for c in lines}
    assert names[MODERATOR_ID] == "主持人" and names["N1"] == "Alice"
    first = next(c for c in lines if c["actor_id"] == "N1")
    assert (first["purpose"], first["attempt"], first["provider_id"], first["model_id"]) == ("turn", 1, "bot", "scripted")
    assert (first["prompt_tokens"], first["completion_tokens"], first["finish_reason"]) == (120, 30, "tool_calls")
    assert isinstance(first["gt"], float) and isinstance(first["elapsed_ms"], int)
    text = json.dumps(lines, ensure_ascii=False)
    assert "（脚本）" not in text and "我是 N" not in text  # neither the scripted thought nor what the agents said
    published = events_of(orch, "llm.call")
    assert len(published) == log.last_id and [e.data["call"]["id"] for e in published] == list(range(1, log.last_id + 1))


@async_test
async def test_a_model_that_keeps_failing_shows_every_call_every_retry_and_the_give_up(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch, fail={"N3": RuntimeError("upstream 503 for sk-abcdef1234567890 (Bearer abcdefgh12345678)")})
    await orch.start_or_resume()
    log = orch.call_log()
    await wait_for(lambda: any(c["kind"] == "event" and c["status"] == "error" and c["actor_id"] == "N3" for c in log.entries()))
    await orch.pause()
    carol = [c for c in log.entries() if c["actor_id"] == "N3"]
    assert [(c["kind"], c["status"]) for c in carol[:6]] == [
        ("call", "error"), ("event", "retry"), ("call", "error"), ("event", "retry"), ("call", "error"), ("event", "error"),
    ]
    failed = carol[0]
    assert (failed["error_class"], failed["purpose"], failed["attempt"], failed["actor_name"]) == ("RuntimeError", "turn", 1, "Carol")
    assert failed["prompt_tokens"] is None and "upstream 503" in failed["message"]
    assert "abcdef1234567890" not in json.dumps(orch.call_log_payload()) and "abcdefgh12345678" not in json.dumps(orch.call_log_payload())
    event = carol[5]
    assert (event["purpose"], event["attempt"], event["error_class"]) == ("runtime", None, "RuntimeError")
    # the toast of an agent that gave up is unchanged - one event - and is not a second line of the log
    toasts = [e for e in events_of(orch, "error") if e.data.get("agent_id") == "N3"]
    assert len(toasts) == 1 and "Carol" in toasts[0].data["message"]
    assert len([c for c in log.entries() if c["kind"] == "event" and c["status"] == "error"]) == 1
    # the badge counts what is over (an error, a timeout), a retry is only a retry
    summary = log.view(errors_only=True)
    assert summary["errors"] == 4 and summary["retries"] == 2  # three failed calls + the give-up; two repeated runs
    assert {c["actor_id"] for c in summary["calls"]} == {"N3"}  # the others were fine


@async_test
async def test_a_hard_pause_cancels_the_call_in_flight_and_that_is_a_cancelled_line_not_an_error(monkeypatch):
    orch = make_orch(monkeypatch)
    started = asyncio.Event()

    async def hang(bot: FlakyLlm) -> None:
        started.set()
        await asyncio.sleep(60)

    play_with(orch, fail={"N2": hang})
    await orch.start_or_resume()
    await asyncio.wait_for(started.wait(), 10)
    await orch.pause()
    lines = [c for c in orch.call_log().entries() if c["actor_id"] == "N2"]
    assert [c["status"] for c in lines] == ["cancelled"] and "取消" in lines[0]["message"]
    assert orch.call_log().errors == 0  # a pause is not a failure


@async_test
async def test_a_timer_error_of_the_runtime_and_a_give_up_toast_are_told_apart(monkeypatch):
    orch = make_orch(monkeypatch)
    # (what the supervisor publishes when a timer pass of the world failed: no agent in it)
    orch._publish(UiEvent(type="error", data={"message": "世界的计时器处理出错，已记录日志。"}))
    # (what an agent publishes when it gave its action up: its own trace has told the log already)
    orch._publish(UiEvent(type="error", data={"message": "Carol（N3）的本次行动失败，已放弃：x", "agent_id": "N3"}))
    (line,) = orch.call_log().entries()
    assert (line["kind"], line["status"], line["purpose"], line["actor_id"], line["actor_name"]) == ("event", "error", "runtime", "engine", "系统")
    assert line["message"] == "世界的计时器处理出错，已记录日志。" and line["attempt"] is None
    # it is in the trace, too: a restart brings it back
    assert any(json.loads(row)["agent_id"] == "engine" for p in (trace_dir(orch._logs_root(), orch.session.session_id)).glob("trace-*.jsonl") for row in p.read_text(encoding="utf-8").splitlines())
    assert [e.type for e in events_of(orch, "error", "llm.call")] == ["error", "llm.call", "error"]  # the toast events are untouched


# ----------------------------------------------------------------------- one game, one log; a rebuild
@async_test
async def test_the_log_belongs_to_its_game_a_new_game_starts_empty_and_a_loaded_one_gets_its_log_from_the_trace(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch, fail={"N3": RuntimeError("down")})
    await orch.start_or_resume()
    await wait_for(lambda: orch.call_log().errors >= 4)
    await orch.pause()
    first_id = orch.session.session_id
    log = orch.call_log()
    assert log.session_id == first_id and orch.call_log() is log  # one log per game, kept while the game is live
    failures = [c for c in log.entries() if c["status"] in ("error", "retry", "timeout")]
    assert failures
    await orch.persist_session("test")

    await orch.new_game()
    assert orch.session.session_id != first_id
    fresh = orch.call_log()
    assert fresh is not log and fresh.session_id == orch.session.session_id and len(fresh) == 0  # a new game: an empty log
    assert orch.call_log_payload() == {"calls": [], "last_id": 0, "total": 0, "errors": 0, "retries": 0}

    await orch.load_game(first_id)  # the old game is the live one again: its log is made from its trace, on first use
    rebuilt = orch.call_log()
    assert rebuilt.session_id == first_id and rebuilt is not log
    rebuilt_failures = [c for c in rebuilt.entries() if c["status"] in ("error", "retry", "timeout")]
    drop = ("id", "ts")
    assert [{k: v for k, v in c.items() if k not in drop} for c in rebuilt_failures] == [
        {k: v for k, v in c.items() if k not in drop} for c in failures
    ]
    assert [c["id"] for c in rebuilt.entries()] == list(range(1, len(rebuilt) + 1))
    assert rebuilt.errors == log.errors and rebuilt.retries == log.retries
    with make_client(orch, monkeypatch) as client:
        assert client.get("/api/llm/calls?errors_only=true").json()["errors"] == log.errors


@async_test
async def test_a_game_installed_again_under_the_same_id_gets_its_log_rebuilt_from_the_files_it_now_has(monkeypatch):
    orch = make_orch(monkeypatch)
    log = orch.call_log()
    log.add(kind="call", status="error", actor_id="N1", purpose="turn", message="written to the trace")
    assert orch.call_log() is log  # nothing changed: the same log
    orch._set_session(orch.session)  # what an import that replaced the files does: the same id, other files
    again = orch.call_log()
    assert again is not log and [c["message"] for c in again.entries()] == ["written to the trace"]


@async_test
async def test_the_key_of_a_provider_never_reaches_the_log_the_events_or_the_trace(monkeypatch):
    orch = make_orch(monkeypatch)
    key = "provider-key-9876543210"
    orch.provider_store = FakeProviders(PROVIDER.model_copy(update={"api_key": key}))
    play_with(orch, fail={"N3": RuntimeError(f"401: {key} rejected (Authorization: Bearer abcdefgh12345678)")})
    await orch.start_or_resume()
    await wait_for(lambda: any(c["kind"] == "event" and c["status"] == "error" for c in orch.call_log().entries()))
    await orch.pause()
    errors = [c for c in orch.call_log().entries() if c["actor_id"] == "N3"]
    assert errors and all(key not in (c["message"] or "") for c in errors) and any("***" in (c["message"] or "") for c in errors)
    announced = " ".join(e.json() for e in events_of(orch, "llm.call"))
    files = "".join(p.read_text(encoding="utf-8") for p in trace_dir(orch._logs_root(), orch.session.session_id).glob("trace-*.jsonl"))
    for text in (json.dumps(orch.call_log_payload()), announced, files):
        assert key not in text and "abcdefgh12345678" not in text


@async_test
async def test_a_game_without_a_trace_has_an_empty_log_and_a_broken_trace_does_not_break_the_request(monkeypatch):
    orch = make_orch(monkeypatch)
    assert orch.call_log_payload()["total"] == 0  # no trace directory at all
    directory = trace_dir(orch._logs_root(), orch.session.session_id)
    directory.mkdir(parents=True)
    (directory / "trace-0001.jsonl").write_bytes(b"\xff\xfe garbage\n{not json}\n")
    orch._call_log_obj = None  # (as after a restart)
    assert orch.call_log_payload()["total"] == 0

    def explode(self: CallLog, directory: Any, **kwargs: Any) -> int:
        raise RuntimeError("trace unreadable")

    monkeypatch.setattr(CallLog, "load_from_trace", explode)
    orch._call_log_obj = None
    assert orch.call_log_payload()["total"] == 0  # the log starts empty; the request still works


# ------------------------------------------------------------------------------------ one-shot calls
class TextLlm:
    """A one-shot port (review, "complete it for me"): plain text, and a failure for some agents."""

    def __init__(self, text: str = "答案", fail_for: tuple[str, ...] = ()) -> None:
        self.text, self.fail_for = text, fail_for

    async def complete(self, agent_id: str, messages: list[dict[str, Any]], tools: Any, *, max_tokens: int | None = None) -> LLMResponse:
        if agent_id in self.fail_for:
            raise RuntimeError("upstream exploded sk-secret-1234567")
        return LLMResponse(content=self.text, prompt_tokens=50, completion_tokens=20, provider_id="bot", model_id="scripted", finish_reason="stop")


def end_at_once(world: Any, me: str) -> list[Call]:
    return [("end_game", {"winner": "红队", "summary": "测试"})] if world.state.status != "finished" else []


@async_test
async def test_completing_a_text_and_reviewing_a_game_are_lines_of_the_log_with_their_own_purposes(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch, moderator=end_at_once)
    await orch.start_or_resume()
    await wait_for(lambda: orch.session.status == "finished")
    before = orch.call_log().last_id

    orch.llm_port_factory = lambda o: TextLlm("【定位】慢条斯理的冷笑话爱好者。\n【说话风格】\n- 句子短，爱讲冷笑话。")
    await orch.refine_text("N1", "persona", "爱说冷笑话")
    (refine,) = [c for c in orch.call_log().entries() if c["id"] > before]
    assert (refine["purpose"], refine["actor_id"], refine["actor_name"], refine["status"]) == ("refine", "roster", "名册", "ok")
    assert (refine["provider_id"], refine["model_id"], refine["prompt_tokens"]) == ("bot", "scripted", 50)

    orch.llm_port_factory = lambda o: TextLlm("我的复盘", fail_for=("N2",))
    mark = orch.call_log().last_id
    await orch.start_review()
    await wait_for(lambda: not orch._review_running)
    reviews = {c["actor_id"]: c for c in orch.call_log().entries() if c["id"] > mark}
    assert set(reviews) == set(IDS) and {c["purpose"] for c in reviews.values()} == {"review"}
    assert reviews["N1"]["status"] == "ok" and reviews["N1"]["actor_name"] == "Alice"
    failed = reviews["N2"]
    assert (failed["status"], failed["error_class"]) == ("error", "RuntimeError") and "sk-secret" not in failed["message"]
    assert (failed["provider_id"], failed["model_id"]) == ("prov", "big")  # nothing answered: the seat's own binding
    # the one-shot lines are written to the trace of the game (the agents wrote nothing for them) and survive a rebuild
    orch._call_log_obj = None
    rebuilt = {(c["purpose"], c["actor_id"], c["status"]) for c in orch.call_log().entries() if c["purpose"] in ("refine", "review")}
    assert ("refine", "roster", "ok") in rebuilt and ("review", "N2", "error") in rebuilt and ("review", "N1", "ok") in rebuilt
    assert "sk-secret" not in json.dumps(orch.call_log().entries())


@async_test
async def test_a_call_log_line_is_never_a_prompt_or_an_answer(monkeypatch):
    orch = make_orch(monkeypatch)
    orch.llm_port_factory = lambda o: TextLlm("GEHEIME ANTWORT")
    with contextlib.suppress(RosterError):  # (the answer is not in the format of a persona: no draft - the call was made all the same)
        await orch.refine_text("N1", "persona", "GEHEIMER PROMPT-INHALT")
    payload = json.dumps(orch.call_log_payload(), ensure_ascii=False)
    assert "GEHEIM" not in payload and orch.call_log().last_id == 1


# ------------------------------------------- no trace file stays open (review: Windows could not move the logs folder)
# A line the log writes while the game is not running (a "complete it" call, a review) opens the trace of the game for that
# one line; leaving it open made moving or deleting the ``logs`` folder fail with WinError 5 - an archive import, deleting a
# game from the history.  (On Linux an open file does not stop ``os.replace``; the ``is_open`` assertions are the same there.)
PERSONA = "【定位】慢条斯理的冷笑话爱好者。\n【说话风格】\n- 句子短，爱讲冷笑话。"


def assert_logs_folder_can_be_moved(orch: GameOrchestrator) -> None:
    logs = orch._logs_root()
    assert logs.is_dir()
    moved = logs.parent / "logs-moved"
    os.replace(logs, moved)  # (the data directory's logs folder, as an import replaces it)
    assert moved.is_dir() and not logs.exists()


@async_test
async def test_a_refine_in_an_idle_game_holds_no_trace_file_open_and_the_logs_folder_can_be_moved(monkeypatch):
    orch = make_orch(monkeypatch)
    orch.llm_port_factory = lambda o: TextLlm(PERSONA)
    await orch.refine_text("N1", "persona", "x")
    log = orch.call_log()
    assert len(log) == 1 and log.trace is not None and log.trace.is_open is False  # (the trace was made for this one line)
    assert (trace_dir(orch._logs_root(), orch.session.session_id) / "trace-0001.jsonl").is_file()  # the line is on disk ...
    assert_logs_folder_can_be_moved(orch)  # ... and nothing holds the file


@async_test
async def test_after_a_pause_a_refine_does_not_leave_the_game_trace_open_again(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch)
    await orch.start_or_resume()
    await wait_for(lambda: len(orch.call_log()) >= 6)
    await orch.pause()
    trace = orch.call_log().trace
    assert isinstance(trace, TraceLog) and trace.is_open is False  # (``Supervisor.stop()`` let go of it)
    orch.llm_port_factory = lambda o: TextLlm(PERSONA)
    await orch.refine_text("N1", "persona", "x")
    assert orch.call_log().trace is trace and trace.is_open is False  # one line later it is closed again
    assert orch.call_log().entries()[-1]["purpose"] == "refine"
    assert_logs_folder_can_be_moved(orch)


@async_test
async def test_a_review_of_a_finished_game_leaves_no_trace_file_open_either(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch, moderator=end_at_once)
    await orch.start_or_resume()
    await wait_for(lambda: orch.session.status == "finished")
    trace = orch.call_log().trace
    assert isinstance(trace, TraceLog) and trace.is_open is False
    orch.llm_port_factory = lambda o: TextLlm("我的复盘", fail_for=("N2",))  # (three reviews succeed as calls, one fails)
    mark = orch.call_log().last_id
    await orch.start_review()
    await wait_for(lambda: not orch._review_running)
    lines = [c for c in orch.call_log().entries() if c["id"] > mark and c["purpose"] == "review"]
    assert sorted(c["status"] for c in lines) == ["error", "ok", "ok", "ok"]
    assert orch.call_log().trace is trace and trace.is_open is False
    assert_logs_folder_can_be_moved(orch)


@async_test
async def test_the_trace_of_a_running_game_is_not_closed_for_every_line_only_by_the_pause(monkeypatch):
    closed: list[int] = []
    real_close = TraceLog.close

    def counting_close(self: TraceLog) -> None:
        closed.append(id(self))
        real_close(self)

    monkeypatch.setattr(TraceLog, "close", counting_close)
    orch = make_orch(monkeypatch)
    play_with(orch, fail={"N3": RuntimeError("down")})
    await orch.start_or_resume()
    await wait_for(lambda: orch.call_log().errors >= 4)  # three failed calls and a give-up: lines the log writes to the trace
    trace = orch.call_log().trace
    assert isinstance(trace, TraceLog) and trace.is_open is True  # the game runs: its trace is open, as before
    assert id(trace) not in closed  # not closed - and so not reopened - for one of those lines
    persisted = [c for c in orch.call_log().entries() if c["status"] in ("error", "retry")]
    assert len(persisted) >= 6
    await orch.pause()
    assert trace.is_open is False and closed.count(id(trace)) == 1  # one close: the supervisor's, when it stopped
    assert_logs_folder_can_be_moved(orch)


@async_test
async def test_reading_the_thoughts_and_rebuilding_the_log_leave_no_file_open_either(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch, fail={"N3": RuntimeError("down")})
    await orch.start_or_resume()
    await wait_for(lambda: orch.call_log().errors >= 4)
    await orch.pause()
    page = await orch.agent_thoughts("N1", limit=1)  # (a page that leaves most of the trace unread)
    assert len(page["items"]) <= 1
    orch._call_log_obj = None  # (as after a restart: the next look rebuilds the log from the trace)
    assert orch.call_log_payload()["errors"] >= 4
    assert_logs_folder_can_be_moved(orch)
