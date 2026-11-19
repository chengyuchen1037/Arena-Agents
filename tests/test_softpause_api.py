"""The HTTP side of the soft pause (v0.13.1, docs/v0.12-contracts.md section 12.2): ``POST /api/control/soft-pause`` and
``/soft-resume``.  A small FastAPI app with just the routers involved over a real orchestrator with scripted agents."""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.routes as routes_module
import app.api.seat as seat_module
import app.api.softpause as softpause_module
import app.api.takeover as takeover_module
from app.api.routes import router as routes_router
from app.api.seat import router as seat_router
from app.api.softpause import router as softpause_router
from app.api.takeover import router as takeover_router
from test_softpause_runtime import HELD, SoftTable

TOKEN = {"X-Arena-Token": "test-token"}


@pytest.fixture()
def table(monkeypatch: pytest.MonkeyPatch) -> SoftTable:
    monkeypatch.delenv("ARENA_API_TOKEN", raising=False)
    t = SoftTable(monkeypatch)
    for module in (routes_module, seat_module, takeover_module, softpause_module):
        monkeypatch.setattr(module, "orchestrator", t.orch)
    return t


def make_client() -> TestClient:
    application = FastAPI()
    application.include_router(routes_router)
    application.include_router(takeover_router)
    application.include_router(seat_router)
    application.include_router(softpause_router)
    return TestClient(application)


def wait_until(predicate: Any, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError("condition not reached in time")
        time.sleep(0.02)


def test_the_soft_pause_is_refused_with_409_until_the_game_runs_and_a_seat_is_played(table: SoftTable) -> None:
    with make_client() as client:
        idle = client.post("/api/control/soft-pause")
        assert idle.status_code == 409 and idle.json() == {"detail": "对局没有在运行。"}
        assert client.post("/api/takeover", json={"seat": "p02"}).status_code == 200
        still_idle = client.post("/api/control/soft-pause")
        assert still_idle.status_code == 409 and "没有在运行" in still_idle.json()["detail"]
        nothing = client.post("/api/control/soft-resume")
        assert nothing.status_code == 409 and nothing.json() == {"detail": "没有处于软暂停。"}

        assert client.post("/api/takeover/release").status_code == 200
        client.portal.call(table.orch.start_or_resume)  # type: ignore[union-attr]
        nobody = client.post("/api/control/soft-pause")
        assert nobody.status_code == 409 and nobody.json() == {"detail": "只有接管座位时才能软暂停。"}
        client.portal.call(table.orch.shutdown)  # type: ignore[union-attr]


def test_soft_pause_and_soft_resume_over_http(table: SoftTable) -> None:
    orch = table.orch
    with make_client() as client:
        assert client.post("/api/takeover", json={"seat": "p02"}).status_code == 200
        client.portal.call(orch.start_or_resume)  # type: ignore[union-attr]
        running = client.get("/api/state").json()["session"]
        assert running["status"] == "running" and running["soft_pause"] is None and running["clock_running"] is True

        click = client.post("/api/control/soft-pause")
        assert click.status_code == 200 and click.json()["ok"] is True
        session = click.json()["session"]
        assert session["status"] == "running"  # not a new game state
        assert session["soft_pause"]["state"] in ("draining", "held") and session["clock_running"] is False
        wait_until(lambda: client.get("/api/state").json()["session"]["soft_pause"] == HELD)

        again = client.post("/api/control/soft-pause")  # already soft-paused: nothing happens
        assert again.status_code == 200 and again.json()["session"]["soft_pause"] == HELD

        # the person's own endpoints keep working during the pause
        assert client.post("/api/seat/pass", json={"channel_id": "dm-p02"}).status_code == 200

        resumed = client.post("/api/control/soft-resume")
        assert resumed.status_code == 200 and resumed.json()["ok"] is True
        assert resumed.json()["session"]["soft_pause"] is None and resumed.json()["session"]["clock_running"] is True
        assert client.post("/api/control/soft-resume").status_code == 409  # not soft-paused any more
        client.portal.call(orch.shutdown)  # type: ignore[union-attr]


def test_the_top_bars_pause_during_a_soft_pause_is_the_hard_pause_and_start_does_nothing(table: SoftTable) -> None:
    orch = table.orch
    orch.demo_mode = True  # (scripted agents: the start route skips the model-binding check of the real providers)
    with make_client() as client:
        assert client.post("/api/takeover", json={"seat": "p02"}).status_code == 200
        client.portal.call(orch.start_or_resume)  # type: ignore[union-attr]
        assert client.post("/api/control/soft-pause").status_code == 200
        wait_until(lambda: orch.soft_pause_payload() == HELD)

        started = client.post("/api/control/start")  # "继续" of the top bar is not available: nothing is let go
        assert started.status_code == 200 and started.json()["session"]["soft_pause"] == HELD
        assert client.post("/api/takeover/release").status_code == 409  # the game still runs

        hard = client.post("/api/control/pause")
        assert hard.status_code == 200
        session = hard.json()["session"]
        assert session["status"] == "paused" and session["soft_pause"] is None and session["clock_running"] is False
        assert client.post("/api/control/soft-resume").status_code == 409
        assert client.post("/api/control/soft-pause").status_code == 409  # a paused game does not run
        assert client.post("/api/takeover/release").status_code == 200  # (the seat can be given back now)
        client.portal.call(orch.shutdown)  # type: ignore[union-attr]


def test_the_soft_pause_endpoints_are_writes_for_a_trusted_client(table: SoftTable, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARENA_API_TOKEN", "test-token")
    with make_client() as client:
        for path in ("/api/control/soft-pause", "/api/control/soft-resume"):
            assert client.post(path).status_code == 401, path  # no token
            assert client.post(path, headers={"X-Arena-Token": "wrong"}).status_code == 401, path
        # with the token the request reaches the rules: 409 (the game does not run), not 401
        assert client.post("/api/control/soft-pause", headers=TOKEN).status_code == 409
        assert client.post("/api/control/soft-resume", headers=TOKEN).status_code == 409


def test_a_foreign_origin_is_refused_and_a_remote_caller_without_a_token_too(table: SoftTable) -> None:
    with make_client() as client:
        refused = client.post("/api/control/soft-pause", headers={"Origin": "https://evil.example"})
        assert refused.status_code == 403
    remote = TestClient(_remote_app(), client=("203.0.113.9", 50000))
    assert remote.post("/api/control/soft-pause").status_code == 403  # not a local client, no token configured
    assert remote.post("/api/control/soft-resume").status_code == 403


def _remote_app() -> FastAPI:
    application = FastAPI()
    application.include_router(softpause_router)
    return application
