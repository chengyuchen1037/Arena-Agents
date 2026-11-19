"""The error log is for trusted clients - on the event stream as well (v0.13.1, review of the observe stream).

``GET /api/llm/calls`` needs ``require_trusted_client`` (a token, or a loopback peer).  The ``llm.call`` events that keep an
open log panel current carry the same lines - provider, model, tokens, the text of an error - so the SSE endpoint must not
hand them to a client that may not read the REST endpoint: ``/api/events`` is open to every client that may use the API at all
(a remote spectator without a token, when none is configured), and they get the stream without ``llm.call``, live and in a
replay.  A trusted client gets it as before, with the same fields.

These tests drive the real routers over ASGI the way a server does - the response is read as it is sent, the peer address
is part of the request - because Starlette's ``TestClient`` cannot read a stream that never ends.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.calls as calls_module
import app.api.routes as routes_module
from app.api.calls import router as calls_router
from app.api.routes import router as routes_router
from app.game.orchestrator import GameOrchestrator
from app.world.types import UiEvent
from game_fakes import async_test
from test_call_log_api import make_orch

REMOTE = ("203.0.113.5", 50000)  # a peer that is not this machine
LOCAL = ("127.0.0.1", 50000)
TOKEN = "s3cret-token-value"
LOG_TEXT = "401 from the provider: key was rejected for project acme-internal"


@pytest.fixture(autouse=True)
def no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARENA_API_TOKEN", raising=False)
    monkeypatch.delenv("ARENA_ALLOWED_ORIGINS", raising=False)


def application(orch: GameOrchestrator, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """The routers of the stream and of the error log over ``orch`` (what ``app.main`` mounts, without its start-up)."""
    monkeypatch.setattr(routes_module, "orchestrator", orch)
    monkeypatch.setattr(calls_module, "orchestrator", orch)
    app = FastAPI()
    app.include_router(routes_router)
    app.include_router(calls_router)
    return app


class Stream:
    """One ``GET /api/events`` held open over ASGI: ``events`` are the data frames received so far, as they came."""

    def __init__(
        self,
        app: FastAPI,
        *,
        client: tuple[str, int] | None,
        headers: dict[str, str] | None = None,
        query: str = "",
    ) -> None:
        self.app = app
        self.scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},  # (2.3: the response listens for the disconnect itself)
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/events",
            "raw_path": b"/api/events",
            "root_path": "",
            "query_string": query.encode(),
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "client": client,
            "server": ("testserver", 80),
        }
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body = ""
        self.events: list[dict[str, Any]] = []
        self.frame_ids: list[int] = []
        self._pending = ""
        self._started = asyncio.Event()
        self._disconnect = asyncio.Event()
        self._request_sent = False
        self._task: asyncio.Task[Any] | None = None

    # --- the ASGI side
    async def _receive(self) -> dict[str, Any]:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = int(message["status"])
            self.headers = {k.decode().lower(): v.decode() for k, v in message.get("headers", [])}
            self._started.set()
        elif message["type"] == "http.response.body":
            text = message.get("body", b"").decode("utf-8")
            self.body += text
            self._feed(text)

    def _feed(self, text: str) -> None:
        self._pending += text
        while "\n\n" in self._pending:
            frame, self._pending = self._pending.split("\n\n", 1)
            data = None
            for line in frame.split("\n"):
                if line.startswith("id: "):
                    self.frame_ids.append(int(line[4:]))
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
            if data is not None:
                self.events.append(data)

    # --- the test's side
    async def __aenter__(self) -> "Stream":
        self._task = asyncio.create_task(self.app(self.scope, self._receive, self._send))
        done, _ = await asyncio.wait({self._task, asyncio.create_task(self._started.wait())}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        if self._task in done and self._task.exception() is not None:
            raise self._task.exception()  # type: ignore[misc]
        assert self.status is not None, "the response never started"
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._disconnect.set()
        assert self._task is not None
        await asyncio.wait({self._task}, timeout=10)
        assert self._task.done(), "the stream did not end after the client went away"
        if not self._task.cancelled() and self._task.exception() is not None:
            raise self._task.exception()  # type: ignore[misc]

    async def until(self, predicate: Callable[[], bool], timeout: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            assert asyncio.get_running_loop().time() < deadline, f"timed out; got {self.types()}"
            await asyncio.sleep(0.01)

    def types(self) -> list[str]:
        return [str(event["type"]) for event in self.events]

    def of(self, type_: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["type"] == type_]


def log_line(orch: GameOrchestrator, text: str = LOG_TEXT) -> dict[str, Any]:
    return orch.call_log().add(
        kind="call", status="error", actor_id="N1", purpose="turn", provider_id="acme-provider", model_id="acme-model-7",
        attempt=2, prompt_tokens=4321, http_status=401, error_class="AuthenticationError", message=text,
    )


def control(orch: GameOrchestrator, text: str) -> None:
    """An event every subscriber gets: it comes after whatever was published before it, so seeing it means they all arrived."""
    orch.bus.publish(UiEvent(type="phase.changed", data={"text": text}))


# ---------------------------------------------------------------------------------- a client that is not trusted
@async_test
async def test_a_remote_client_without_a_token_gets_the_stream_but_not_the_error_log(monkeypatch):
    orch = make_orch(monkeypatch)
    app = application(orch, monkeypatch)
    async with Stream(app, client=REMOTE) as stream:
        assert stream.status == 200 and stream.headers["content-type"].startswith("text/event-stream")
        await stream.until(lambda: stream.types() == ["snapshot"])  # (first connection: the snapshot)
        for n in range(3):
            log_line(orch, f"{LOG_TEXT} #{n}")
        control(orch, "after the lines")
        await stream.until(lambda: "phase.changed" in stream.types())
        assert stream.types() == ["snapshot", "phase.changed"]  # the stream works, the log is not in it
        assert LOG_TEXT not in stream.body and "acme-model" not in stream.body and "llm.call" not in stream.body
        assert orch.bus.last_id == 4 and orch.call_log().last_id == 3  # (the events were published: for those who may have them)
        assert [e.type for e in orch.bus._ring].count("llm.call") == 3


@async_test
async def test_a_remote_client_resuming_gets_a_replay_without_the_error_log(monkeypatch):
    orch = make_orch(monkeypatch)
    app = application(orch, monkeypatch)
    before = orch.bus.last_id
    control(orch, "one")
    log_line(orch)
    control(orch, "two")
    log_line(orch)
    async with Stream(app, client=REMOTE, headers={"Last-Event-ID": str(before)}) as stream:  # (a reconnect: no snapshot)
        await stream.until(lambda: len(stream.of("phase.changed")) == 2)
        assert stream.types() == ["phase.changed", "phase.changed"]
        assert [e["data"]["text"] for e in stream.of("phase.changed")] == ["one", "two"]
        assert stream.frame_ids == [before + 1, before + 3]  # the ids of what was left out are simply skipped
        log_line(orch, "live after the replay")
        control(orch, "three")
        await stream.until(lambda: len(stream.of("phase.changed")) == 3)
        assert "llm.call" not in stream.types() and "acme-model" not in stream.body and "live after the replay" not in stream.body
    # ... and a reconnect from the last id it saw goes on from there, still without the log
    last_seen = stream.frame_ids[-1]
    log_line(orch)
    control(orch, "four")
    async with Stream(app, client=REMOTE, headers={"Last-Event-ID": str(last_seen)}) as again:
        await again.until(lambda: len(again.of("phase.changed")) == 1)
        assert again.types() == ["phase.changed"] and again.of("phase.changed")[0]["data"]["text"] == "four"


@async_test
async def test_a_remote_client_may_be_sent_the_resync_snapshot_and_still_never_the_log(monkeypatch):
    orch = make_orch(monkeypatch)
    app = application(orch, monkeypatch)
    async with Stream(app, client=REMOTE, headers={"Last-Event-ID": "999999"}) as stream:  # an id from a previous server run
        await stream.until(lambda: stream.types() == ["snapshot"])
        log_line(orch)
        control(orch, "x")
        await stream.until(lambda: "phase.changed" in stream.types())
        assert stream.types() == ["snapshot", "phase.changed"]


@async_test
async def test_the_error_log_text_is_in_no_frame_a_remote_client_receives_when_the_log_is_busy(monkeypatch):
    orch = make_orch(monkeypatch)
    app = application(orch, monkeypatch)
    async with Stream(app, client=REMOTE) as stream:
        await stream.until(lambda: stream.types() == ["snapshot"])
        for n in range(300):  # a model call every few milliseconds of a busy game
            log_line(orch, f"{LOG_TEXT} #{n}")
            if n % 7 == 0:
                control(orch, f"c{n}")
        control(orch, "end")
        await stream.until(lambda: stream.of("phase.changed") and stream.of("phase.changed")[-1]["data"]["text"] == "end")
        assert "llm.call" not in stream.types() and LOG_TEXT not in stream.body
        assert len(stream.of("phase.changed")) == 300 // 7 + 1 + 1  # not one message was lost to the 300 lines left out


# --------------------------------------------------------------------------------------- a client that is trusted
@async_test
async def test_a_loopback_client_without_a_token_gets_the_error_log_live_with_the_same_fields(monkeypatch):
    orch = make_orch(monkeypatch)
    app = application(orch, monkeypatch)
    async with Stream(app, client=LOCAL) as stream:
        await stream.until(lambda: stream.types() == ["snapshot"])
        line = log_line(orch)
        control(orch, "after")
        await stream.until(lambda: "phase.changed" in stream.types())
        assert stream.types() == ["snapshot", "llm.call", "phase.changed"]
        (event,) = stream.of("llm.call")
        assert set(event["data"]) == {"call", "errors"} and event["data"]["errors"] == 1
        assert event["data"]["call"] == line  # the very line of the log: nothing stripped, nothing added
        assert event["data"]["call"]["message"] == LOG_TEXT and event["data"]["call"]["model_id"] == "acme-model-7"


@async_test
async def test_a_loopback_client_resuming_gets_the_error_log_in_the_replay(monkeypatch):
    orch = make_orch(monkeypatch)
    app = application(orch, monkeypatch)
    before = orch.bus.last_id
    control(orch, "one")
    first = log_line(orch)
    second = log_line(orch)
    async with Stream(app, client=LOCAL, headers={"Last-Event-ID": str(before)}) as stream:
        await stream.until(lambda: len(stream.events) == 3)
        assert stream.types() == ["phase.changed", "llm.call", "llm.call"]
        assert [e["data"]["call"]["id"] for e in stream.of("llm.call")] == [first["id"], second["id"]]
        assert stream.frame_ids == [before + 1, before + 2, before + 3]


@async_test
async def test_an_in_process_client_without_a_peer_address_is_local_and_trusted(monkeypatch):
    orch = make_orch(monkeypatch)
    app = application(orch, monkeypatch)
    async with Stream(app, client=None) as stream:
        await stream.until(lambda: stream.types() == ["snapshot"])
        log_line(orch)
        await stream.until(lambda: "llm.call" in stream.types())


@async_test
async def test_with_a_token_every_authenticated_client_gets_the_error_log_wherever_it_comes_from(monkeypatch):
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    orch = make_orch(monkeypatch)
    app = application(orch, monkeypatch)
    # (EventSource cannot send a header: the stream also takes ``?token=``)
    async with Stream(app, client=REMOTE, headers={"X-Arena-Token": TOKEN}) as by_header, Stream(
        app, client=REMOTE, query=f"token={TOKEN}"
    ) as by_query, Stream(app, client=LOCAL, headers={"X-Arena-Token": TOKEN}) as local:
        for stream in (by_header, by_query, local):
            assert stream.status == 200
            await stream.until(lambda stream=stream: stream.types() == ["snapshot"])
        log_line(orch)
        control(orch, "after")
        for stream in (by_header, by_query, local):
            await stream.until(lambda stream=stream: "phase.changed" in stream.types())
            assert stream.types() == ["snapshot", "llm.call", "phase.changed"], stream.types()
            assert stream.of("llm.call")[0]["data"]["call"]["message"] == LOG_TEXT


@async_test
async def test_with_a_token_nobody_without_it_gets_a_stream_at_all(monkeypatch):
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    orch = make_orch(monkeypatch)
    app = application(orch, monkeypatch)
    for headers, query in (({}, ""), ({"X-Arena-Token": "wrong"}, ""), ({}, "token=wrong")):
        async with Stream(app, client=REMOTE, headers=headers, query=query) as stream:
            assert stream.status == 401 and stream.events == []
        async with Stream(app, client=LOCAL, headers=headers, query=query) as stream:
            assert stream.status == 401 and LOG_TEXT not in stream.body


# --------------------------------------------------------------------- the rules of the REST endpoint did not move
def test_the_rest_rules_of_the_error_log_are_unchanged(monkeypatch):
    orch = make_orch(monkeypatch)
    app = application(orch, monkeypatch)
    line = log_line(orch)
    remote = TestClient(app, client=REMOTE)
    local = TestClient(app, client=LOCAL)
    denied = remote.get("/api/llm/calls")
    assert denied.status_code == 403 and "local clients" in denied.json()["detail"] and LOG_TEXT not in denied.text
    allowed = local.get("/api/llm/calls")
    assert allowed.status_code == 200 and allowed.json()["calls"] == [line] and allowed.json()["errors"] == 1
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    assert remote.get("/api/llm/calls").status_code == 401  # a token is required of everybody ...
    assert remote.get("/api/llm/calls", headers={"X-Arena-Token": "wrong"}).status_code == 401
    assert remote.get("/api/llm/calls", headers={"X-Arena-Token": TOKEN}).json()["calls"] == [line]  # ... and makes any client trusted
    assert local.get("/api/llm/calls", headers={"X-Arena-Token": TOKEN}).status_code == 200
    assert remote.get("/api/llm/calls?token=" + TOKEN).status_code == 401  # (only the stream takes the query token)


def test_trusted_client_is_one_rule_for_the_rest_guard_and_the_stream():
    from starlette.requests import Request

    from app.api import security

    def request(host: str | None) -> Request:
        return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": [], "client": (host, 1) if host else None})

    assert security.is_trusted_request(request("127.0.0.1")) and security.is_trusted_request(request("::1"))
    assert security.is_trusted_request(request("::ffff:127.0.0.1")) and security.is_trusted_request(request(None))
    assert security.is_trusted_request(request("testclient"))
    for remote in ("203.0.113.5", "10.0.0.7", "2001:db8::1", "example.org"):
        assert not security.is_trusted_request(request(remote)), remote


def test_the_trusted_rule_follows_the_token_setting(monkeypatch):
    from starlette.requests import Request

    from app.api import security

    remote = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": [], "client": ("203.0.113.5", 1)})
    assert security.is_trusted_request(remote) is False
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)  # (the router dependency has authenticated the caller by then)
    assert security.is_trusted_request(remote) is True
    monkeypatch.setenv("ARENA_API_TOKEN", "   ")  # a blank token counts as none
    assert security.is_trusted_request(remote) is False
