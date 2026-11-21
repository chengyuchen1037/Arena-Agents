"""``/api/budget``: the spending limit of the game that is on screen (contract 10.4), against a real orchestrator.

(These cases lived in ``tests/test_roster_api.py`` with the roster API, which v0.14 removed (H55); the budget API itself did not change.)"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from game_fakes import make_table_orch

from app.api import routes as routes_api
from app.main import app

TOKEN = "s3cret-token-value"


@pytest.fixture
def orch(monkeypatch):
    orchestrator = make_table_orch(monkeypatch)
    # (the process-wide singleton may already exist, built by an earlier test: this test's game is the one that is served)
    monkeypatch.setattr(routes_api, "orchestrator", orchestrator)
    return orchestrator


@pytest.fixture
def client(orch):
    with TestClient(app) as c:
        yield c


def detail(response) -> str:
    body = response.json()
    assert set(body) == {"detail"} and isinstance(body["detail"], str) and body["detail"]
    return body["detail"]


def test_the_budget_can_be_read_and_set(client):
    assert client.get("/api/budget").json() == {
        "max_tokens": None, "max_cost_usd": None, "force_stop": False, "used_tokens": 0, "used_cost_usd": 0.0, "progress": 0.0, "state": "ok",
    }
    put = client.put("/api/budget", json={"max_tokens": 200000, "max_cost_usd": 3, "force_stop": True})
    assert put.status_code == 200 and put.json()["max_tokens"] == 200000 and put.json()["force_stop"] is True
    assert client.get("/api/budget").json()["max_cost_usd"] == 3.0
    cleared = client.put("/api/budget", json={"max_tokens": None, "max_cost_usd": "", "force_stop": False})
    assert cleared.json()["max_tokens"] is None and cleared.json()["max_cost_usd"] is None


def test_the_limit_is_part_of_the_session_the_clients_see(client):
    client.put("/api/budget", json={"max_tokens": 5000, "force_stop": True})
    assert client.get("/api/state").json()["session"]["budget"]["max_tokens"] == 5000


@pytest.mark.parametrize("body", [{"max_tokens": -5}, {"max_cost_usd": "lots"}, {"force_stop": "maybe"}, {"unknown": 1}, {"max_tokens": 10**13}])
def test_a_bad_budget_is_a_422_with_a_plain_message(client, body):
    response = client.put("/api/budget", json=body)
    assert response.status_code == 422 and "预算设置无效" in detail(response)
    assert client.get("/api/budget").json()["max_tokens"] is None


def test_a_budget_body_must_be_an_object(client):
    for content in (b"[]", b"7", b"not json"):
        response = client.put("/api/budget", content=content, headers={"content-type": "application/json"})
        assert response.status_code == 400 and "JSON 对象" in detail(response)


def test_the_budget_needs_the_token_to_be_changed_but_not_to_be_read_on_this_machine(client, monkeypatch):
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    assert client.put("/api/budget", json={"max_tokens": 5}).status_code == 401
    assert client.get("/api/budget").status_code == 401
    assert client.get("/api/budget", headers={"X-Arena-Token": TOKEN}).status_code == 200
    assert client.put("/api/budget", json={"max_tokens": 5}, headers={"X-Arena-Token": TOKEN}).status_code == 200


def test_a_remote_client_may_read_but_not_change_the_budget_without_a_token(orch):
    with TestClient(app, client=("203.0.113.5", 50000)) as remote:
        assert remote.get("/api/budget").status_code == 200
        assert remote.put("/api/budget", json={"max_tokens": 5}).status_code == 403
