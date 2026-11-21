"""The HTTP side of the learning runs (v0.14, contract §13.7): ``POST /api/bots/init``, ``POST /api/bots/init:cancel``, ``POST /api/game/review``.

A small FastAPI app with just this router over a real orchestrator (fake stores, a scripted model).  The orchestrator's tasks run on the loop of the
test client (``client.portal``), as they do in the real server.  Every refusal is ``{"detail", "code", "problems"?}`` with the status of the contract.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.learning as learning_module
from app.api.learning import router as learning_router
from game_fakes import async_test, make_table_orch
from learning_fakes import ScriptedLlm, by_kind, good_init
from test_call_log_api import end_at_once, play_with

IDS = ["N1", "N2", "N3", "N4"]
TOKEN = {"X-Arena-Token": "test-token"}


def make_client() -> TestClient:
    application = FastAPI()
    application.include_router(learning_router)
    return TestClient(application)


@pytest.fixture()
def orch(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("ARENA_API_TOKEN", raising=False)
    engine = make_table_orch(monkeypatch, ready=False)
    monkeypatch.setattr(learning_module, "orchestrator", engine)
    return engine


@pytest.fixture()
def client(orch: Any) -> Iterator[TestClient]:
    with make_client() as c:
        yield c
        c.portal.call(orch.shutdown)  # type: ignore[union-attr]


def until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for the condition"
        time.sleep(0.02)


def use(orch: Any, llm: ScriptedLlm) -> ScriptedLlm:
    orch.llm_port_factory = lambda o: llm
    return llm


# ------------------------------------------------------------------------------------------------------------ initialization
def test_init_answers_202_with_the_job_and_the_bots_become_ready(client: TestClient, orch: Any) -> None:
    use(orch, ScriptedLlm(good_init))
    answer = client.post("/api/bots/init")
    assert answer.status_code == 202
    body = answer.json()
    assert set(body) == {"job_id", "ids", "deadline_s"} and body["deadline_s"] == 120
    assert body["ids"] == orch._bots().lineup() and len(body["job_id"]) == 12
    until(lambda: orch.init_job_view() is None)
    assert {s["init"]["status"] for s in orch.bot_summaries()} == {"ready"}
    # a body is optional; ``ids`` picks bots; ``rebuild`` means all of them again
    nothing = client.post("/api/bots/init", json={})  # everybody is ready: nothing to do (a request for nothing, not a conflict)
    assert nothing.status_code == 400 and nothing.json()["code"] == "nothing_to_do" and "没有需要初始化的玩家" in nothing.json()["detail"]
    one = client.post("/api/bots/init", json={"ids": [body["ids"][2]]})
    assert one.status_code == 202 and one.json()["ids"] == [body["ids"][2]]
    until(lambda: orch.init_job_view() is None)
    everybody = client.post("/api/bots/init", json={"rebuild": True})
    assert everybody.status_code == 202 and everybody.json()["ids"] == body["ids"]
    until(lambda: orch.init_job_view() is None)


def test_init_is_refused_with_a_code_the_page_acts_on(client: TestClient, orch: Any) -> None:
    llm = use(orch, ScriptedLlm(good_init, gate=asyncio.Event()))
    unknown = client.post("/api/bots/init", json={"ids": ["b-00000000"]})
    assert unknown.status_code == 404 and unknown.json()["code"] == "not_found" and "没有这个玩家" in unknown.json()["detail"]
    for status in ("running", "paused"):
        orch.session.status = status
        busy = client.post("/api/bots/init")
        assert busy.status_code == 409 and busy.json()["code"] == "game_active" and "problems" not in busy.json()
    orch.session.status = "idle"
    assert client.post("/api/bots/init").status_code == 202
    until(lambda: llm.in_flight == 4)
    again = client.post("/api/bots/init")
    assert again.status_code == 409 and again.json()["code"] == "init_busy"
    assert client.post("/api/bots/init:cancel").json() == {"ok": True}
    until(lambda: orch.init_job_view() is None)
    assert client.post("/api/bots/init:cancel").json() == {"ok": True}  # nothing is running: still fine


def test_init_without_models_lists_the_problems_and_starts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARENA_API_TOKEN", raising=False)
    engine = make_table_orch(monkeypatch, ready=False, bound_all=False)
    monkeypatch.setattr(learning_module, "orchestrator", engine)
    llm = use(engine, ScriptedLlm(good_init))
    with make_client() as client:
        refused = client.post("/api/bots/init")
        body = refused.json()
        assert refused.status_code == 409 and body["code"] == "no_binding" and "还没有可用的模型" in body["detail"]
        assert [p["name"] for p in body["problems"]] == ["Alice", "Bob", "Carol", "David"]
        assert body["problems"][0]["items"][0] == {"code": "no_binding", "message": body["problems"][0]["items"][0]["message"], "fix": "bind_model"}
        assert llm.requests == [] and engine.init_job_view() is None
        client.portal.call(engine.shutdown)  # type: ignore[union-attr]


def test_init_refuses_a_contract_or_scenario_it_cannot_use_with_the_start_checks_problems(client: TestClient, orch: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.game.sources import SourceError, Sources

    def broken(self: Sources, spec: Any) -> SourceError:
        return SourceError("官方契约有问题：没有提到工具 speak。", kind="contract", code="contract_invalid")

    monkeypatch.setattr(Sources, "contract_problem", broken)
    use(orch, ScriptedLlm(good_init))
    refused = client.post("/api/bots/init")
    body = refused.json()
    assert refused.status_code == 409 and body["code"] == "NOT_READY" and body["problems"][0]["scope"] == "contract"
    assert body["problems"][0]["items"][0]["code"] == "contract_invalid" and body["problems"][0]["items"][0]["fix"] == "import_contract"


def test_the_bodies_are_checked(client: TestClient) -> None:
    assert client.post("/api/bots/init", json={"ids": ["b-00000000"], "actor": "x"}).status_code == 422  # no other fields
    assert client.post("/api/bots/init", json={"ids": "b-00000000"}).status_code == 422
    assert client.post("/api/bots/init", json={"ids": [f"b-{n:08x}" for n in range(65)]}).status_code == 422
    assert client.post("/api/bots/init", json={"rebuild": "sometimes"}).status_code == 422
    assert client.post("/api/game/review", json={"learn_style": {"N1": [1]}}).status_code == 422
    assert client.post("/api/game/review", json={"ids": ["N1"], "other": 1}).status_code == 422


def test_the_write_endpoints_need_a_trusted_client(orch: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARENA_API_TOKEN", "test-token")
    use(orch, ScriptedLlm(good_init))
    with make_client() as client:
        for path in ("/api/bots/init", "/api/bots/init:cancel", "/api/game/review"):
            assert client.post(path).status_code == 401, path  # no token
        assert client.post("/api/bots/init", headers=TOKEN).status_code == 202
        until(lambda: orch.init_job_view() is None)
        refused = client.post("/api/bots/init", headers={**TOKEN, "Origin": "https://evil.example"})
        assert refused.status_code == 403
        client.portal.call(orch.shutdown)  # type: ignore[union-attr]


# ------------------------------------------------------------------------------------------------------------------ review
def finish_a_game(client: TestClient, orch: Any) -> None:
    play_with(orch, moderator=end_at_once)
    client.portal.call(orch.start_or_resume)  # type: ignore[union-attr]
    until(lambda: orch.session.status == "finished")


def test_review_is_refused_before_the_game_is_over_and_for_a_game_of_the_old_format(client: TestClient, orch: Any) -> None:
    use(orch, ScriptedLlm(by_kind()))
    early = client.post("/api/game/review")
    assert early.status_code == 409 and early.json()["code"] == "game_not_finished" and "结束后" in early.json()["detail"]
    orch.session.status = "finished"
    orch.session.schema_version = 5
    old = client.post("/api/game/review")
    assert old.status_code == 409 and old.json()["code"] == "review_unsupported"


def test_review_answers_with_the_seats_that_started_and_every_seat_is_reviewed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARENA_API_TOKEN", raising=False)
    engine = make_table_orch(monkeypatch)
    monkeypatch.setattr(learning_module, "orchestrator", engine)
    with make_client() as client:
        finish_a_game(client, engine)
        llm = use(engine, ScriptedLlm(by_kind()))
        invalid = client.post("/api/game/review", json={"ids": ["N1", "N9"]})
        assert invalid.status_code == 422 and invalid.json()["code"] == "invalid"
        first = client.post("/api/game/review", json={"ids": ["N2", "N1"], "learn_style": {"N2": True}})
        assert first.status_code == 200 and first.json() == {"started": ["N2", "N1"], "deadline_s": 60}
        until(lambda: not engine.review_running_ids())
        assert set(llm.counts) == {"N1", "N2"} and engine.review_view()["done"] in (["N2", "N1"], ["N1", "N2"])
        rest = client.post("/api/game/review")  # no body: everything that is left
        assert rest.status_code == 200 and rest.json()["started"] == ["N3", "N4"]
        until(lambda: not engine.review_running_ids())
        again = client.post("/api/game/review")
        assert again.status_code == 200 and again.json() == {"started": [], "deadline_s": 60}
        client.portal.call(engine.shutdown)  # type: ignore[union-attr]
