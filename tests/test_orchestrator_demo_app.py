"""The whole application in demo mode, from the outside (v0.14, R4): ``ARENA_DEMO=1``, the real ``app.main`` with its lifespan, the real HTTP routes.

A first start of the demo comes up with every card ready (the scripted bots initialize without a model, H25), starts a game through the start check, plays
it to the end - the identities appear only when it starts, H52 -, reviews it, makes a new game, and initializes everybody again.  No port is opened (the
test client talks to the application directly); no model is called.  The data directory is the test's temporary one.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.game.orchestrator as orchestrator_module
import game_fakes
from app.config import load_config
from app.game.demo import install_demo
from app.game.orchestrator import GameOrchestrator


def until(predicate: Callable[[], bool], timeout: float = 120.0, what: str = "the condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from app.main import app

    monkeypatch.delenv("ARENA_API_TOKEN", raising=False)
    monkeypatch.setenv("ARENA_DEMO", "1")
    cfg = load_config()
    cfg["runtime"] = {**cfg["runtime"], "tick_interval": 0.05, "watchdog": 60}
    game_fakes.patch_config(monkeypatch, cfg)
    engine = GameOrchestrator()
    install_demo(engine, delay=0.0, seed=7, prepare=False)  # (``get_orchestrator`` does the same, with the delay that makes a demo watchable)
    monkeypatch.setattr(orchestrator_module, "_instance", engine)
    with TestClient(app) as test_client:  # the lifespan: the data directory is locked and ``startup()`` makes every demo bot ready
        yield test_client


def state(client: TestClient) -> dict[str, Any]:
    body = client.get("/api/state").json()
    assert set(body) >= {"app_version", "session"}
    return body["session"]


def test_the_demo_starts_with_every_bot_ready_and_nothing_dealt(client: TestClient) -> None:
    session = state(client)
    assert session["demo"] is True and session["status"] == "idle" and session["dealt"] is False and session["schema_version"] == 6
    assert session["system_status"] == {"code": "before_game", "text": "对局前", "reason": ""}
    assert all(p["tags"] == [] for p in session["participants"]) and len(session["participants"]) == 12  # nothing about an identity before the start
    assert {p["init_status"] for p in session["participants"]} == {"ready"} and all(p["bot_id"] for p in session["participants"])
    bots = client.get("/api/bots").json()
    assert bots["ready"] == {"ok": True, "problems": []} and len(bots["bots"]) == 12 and bots["init_job"] is None and bots["memory_enabled"] is True
    assert {b["init"]["status"] for b in bots["bots"]} == {"ready"} and {b["binding"]["label"] for b in bots["bots"]} == {"演示模式"}
    assert [b["seat"] for b in bots["bots"]] == [f"N{i}" for i in range(1, 13)] and bots["limits"]["init_timeout_s"] == 120 and bots["defaults"]
    assert client.get("/api/roster").status_code == 404  # (H55: the roster is gone)
    assert client.post("/api/control/shuffle-identities").status_code == 404  # (H52: and so is the re-deal)


def test_a_demo_game_is_started_played_reviewed_and_the_bots_are_initialized_again(client: TestClient) -> None:
    started = client.post("/api/control/start")
    assert started.status_code == 200 and started.json()["session"]["dealt"] is True and started.json()["session"]["system_status"]["code"] == "in_game"
    until(lambda: state(client)["status"] in ("finished", "paused"), what="the demo game to end")
    session = state(client)
    assert session["status"] == "finished" and session["winner"] and session["dealt"] is True
    assert all(p["tags"] for p in session["participants"]) and session["system_status"] == {"code": "finished", "text": "已结束", "reason": ""}
    assert session["review"]["available"] is True and session["review"]["supported"] is True and session["review"]["done"] == []

    # ---- the review: every seat, once
    review = client.post("/api/game/review")
    assert review.status_code == 200 and review.json()["deadline_s"] == 60 and len(review.json()["started"]) == 12
    until(lambda: len(state(client)["review"]["done"]) == 12 and not state(client)["review"]["running"], what="the reviews")
    assert client.post("/api/game/review").json()["started"] == []  # (once per seat and game)
    bots = client.get("/api/bots").json()
    assert all("场次 1" in b["memory_stats"]["record"] for b in bots["bots"]) and all(b["memory_stats"]["people"] == 11 for b in bots["bots"])
    detail = client.get(f"/api/bots/{bots['bots'][0]['id']}").json()
    assert detail["seat"] == "N1" and detail["init"]["status"] == "ready"

    # ---- a new game; everybody is initialized again (a revision, in demo mode with the same fixed answers)
    new = client.post("/api/control/new")
    assert new.status_code == 200 and new.json()["session"]["dealt"] is False and new.json()["session"]["review"]["done"] == []
    first = client.post("/api/bots/init", json={"rebuild": True})
    assert first.status_code == 202 and len(first.json()["ids"]) == 12 and first.json()["deadline_s"] == 120
    until(lambda: client.get("/api/bots").json()["init_job"] is None, what="the initialization")
    bots = client.get("/api/bots").json()
    assert {b["init"]["status"] for b in bots["bots"]} == {"ready"} and bots["learning_usage"]["calls"] >= 12 * 4
    assert client.post("/api/bots/init").status_code == 400  # nothing is left to do
    assert client.post("/api/control/start").status_code == 200  # and the next game starts
    client.post("/api/control/pause")


def test_a_start_that_is_not_ready_is_a_409_with_its_problems_and_nothing_is_dealt(client: TestClient) -> None:
    bots = client.get("/api/bots").json()
    cleared = client.post("/api/bots/memory:clear", json={"ids": [bots["bots"][4]["id"]]})  # Eve forgot everything: not initialized any more
    assert cleared.status_code == 200
    refused = client.post("/api/control/start")
    body = refused.json()
    assert refused.status_code == 409 and body["code"] == "NOT_READY" and "Eve" in body["detail"]
    (problem,) = body["problems"]
    assert problem["scope"] == "bot" and problem["name"] == "Eve" and problem["seat"] == "N5" and problem["items"][0]["code"] == "not_initialized"
    assert problem["items"][0]["fix"] == "init"
    session = state(client)
    assert session["status"] == "idle" and session["dealt"] is False and all(p["tags"] == [] for p in session["participants"])
    assert session["system_status"]["code"] == "uninitialized" and session["bots"]["ready"]["ok"] is False
    # initialize just that bot, and the game starts
    assert client.post("/api/bots/init", json={"ids": [problem["bot_id"]]}).status_code == 202
    until(lambda: client.get("/api/bots").json()["init_job"] is None, what="the initialization")
    assert client.post("/api/control/start").status_code == 200
    client.post("/api/control/pause")


def test_the_card_of_a_seat_is_not_changed_while_its_bot_is_being_initialized(client: TestClient) -> None:
    """The bots API refuses to change a bot an initialization is working on (409 ``init_busy``): the hook the orchestrator gives it."""
    import asyncio

    engine = orchestrator_module._instance
    assert engine is not None
    bot = engine._bots().lineup()[0]
    engine.init_running_ids = lambda: {bot}  # type: ignore[method-assign]  # (a job is running for it)
    engine.init_busy_ids = lambda: {bot}  # type: ignore[method-assign]
    refused = client.post("/api/bots/memory:clear", json={"ids": [bot]})
    assert refused.status_code == 409 and refused.json()["code"] == "init_busy"
    del engine.init_running_ids, engine.init_busy_ids
    assert asyncio.iscoroutinefunction(engine.start_init) and asyncio.iscoroutinefunction(engine.complete_oneshot)


def test_get_orchestrator_builds_the_demo_the_environment_asks_for_and_startup_makes_its_bots_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production path of ``ARENA_DEMO=1``: ``get_orchestrator()`` installs the scripted demo (before the data directory is locked it cannot touch the
    bots: ``prepare=False``), and the application's lifespan - ``startup()`` - gives every default bot its fixed memory."""
    from app.main import app

    monkeypatch.delenv("ARENA_API_TOKEN", raising=False)
    monkeypatch.setenv("ARENA_DEMO", "1")
    cfg = load_config()
    cfg["runtime"] = {**cfg["runtime"], "tick_interval": 0.05, "watchdog": 60}
    game_fakes.patch_config(monkeypatch, cfg)
    monkeypatch.setattr(orchestrator_module, "_instance", None)  # (no orchestrator yet: the lifespan makes the one the environment asks for)
    with TestClient(app) as test_client:
        engine = orchestrator_module.get_orchestrator()
        assert engine.demo_mode is True and engine.llm_port_factory is not None
        session = state(test_client)
        assert session["demo"] is True and session["status"] == "idle" and session["dealt"] is False
        assert session["system_status"]["code"] == "before_game" and {p["init_status"] for p in session["participants"]} == {"ready"}
        assert test_client.get("/api/bots").json()["ready"] == {"ok": True, "problems": []}
        assert test_client.post("/api/control/start").status_code == 200  # ... and it starts: no model, no key, nothing to bind
        assert state(test_client)["dealt"] is True
        test_client.post("/api/control/pause")
