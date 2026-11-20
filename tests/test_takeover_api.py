"""The HTTP side of playing a seat (v0.13, docs/v0.12-contracts.md §11.7): ``/api/takeover*``, ``/api/seat/*`` and the
(v0.14: no re-deal any more) the start check's 409.  A small FastAPI app with just these routers over a real orchestrator with scripted agents."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.routes as routes_module
import app.api.seat as seat_module
import app.api.takeover as takeover_module
from app.api.routes import router as routes_router
from app.api.seat import router as seat_router
from app.api.takeover import router as takeover_router
from game_fakes import PLAYERS
from test_takeover_runtime import Table

IDS = [p["id"] for p in PLAYERS]
TOKEN = {"X-Arena-Token": "test-token"}


@pytest.fixture()
def table(monkeypatch: pytest.MonkeyPatch) -> Table:
    monkeypatch.delenv("ARENA_API_TOKEN", raising=False)
    t = Table(monkeypatch)
    for module in (routes_module, seat_module, takeover_module):
        monkeypatch.setattr(module, "orchestrator", t.orch)
    return t


def make_client() -> TestClient:
    application = FastAPI()
    application.include_router(routes_router)
    application.include_router(takeover_router)
    application.include_router(seat_router)
    return TestClient(application)


def test_takeover_and_release_over_http_with_their_statuses(table: Table) -> None:
    with make_client() as client:
        assert client.post("/api/takeover", json={"seat": "moderator"}).status_code == 422
        assert client.post("/api/takeover", json={"seat": "p99"}).status_code == 404
        assert client.post("/api/takeover", json={}).status_code == 422
        assert client.post("/api/takeover", json={"seat": "p02", "actor": "p03"}).status_code == 422  # no other fields

        taken = client.post("/api/takeover", json={"seat": "p02"})
        assert taken.status_code == 200 and taken.json()["ok"] is True
        session = taken.json()["session"]
        assert session["takeover"]["seat"] == "p02" and "reshuffle_available" not in session["takeover"]
        assert {p["id"]: p["state"] for p in session["participants"]}["p02"] == "human"
        busy = client.post("/api/takeover", json={"seat": "p03"})
        assert busy.status_code == 409 and "请先退出" in busy.json()["detail"]

        released = client.post("/api/takeover/release")
        assert released.status_code == 200 and released.json()["session"]["takeover"] == {"seat": None}
        assert client.post("/api/takeover/release").status_code == 200  # nobody is played: nothing happens


def test_the_seat_endpoints_refuse_until_a_seat_is_played_and_the_game_runs(table: Table) -> None:
    bodies = {
        "/api/seat/speak": {"channel_id": "dm-p02", "content": "hi"},
        "/api/seat/pass": {"channel_id": "dm-p02"},
        "/api/seat/respond": {"request_id": "r1", "value": "p01"},
        "/api/seat/ability": {"ability_id": "shout", "params": {}},
    }
    with make_client() as client:
        for path, body in bodies.items():
            nobody = client.post(path, json=body)
            assert nobody.status_code == 409 and "请先接管" in nobody.json()["detail"], path
        assert client.post("/api/takeover", json={"seat": "p02"}).status_code == 200
        for path, body in bodies.items():
            idle = client.post(path, json=body)
            assert idle.status_code == 409 and "还没有开始" in idle.json()["detail"], path


def test_a_game_is_played_over_http_and_the_actor_is_always_the_taken_over_seat(table: Table) -> None:
    orch = table.orch
    limits = {"max_messages": 2, "max_chars_per_message": 12, "max_chars_total": 50, "duration_s": 60}

    with make_client() as client:
        assert client.post("/api/takeover", json={"seat": "p02"}).status_code == 200

        async def start_and_open() -> None:
            await orch.start_or_resume()
            table.moderator.plan(
                [("create_channel", {"channel_id": "public", "name": "公共频道", "members": IDS, "type": "public"}),
                 ("set_floor_queue", {"channel_id": "public", "order": ["p02", "p01"], "limits": limits})],
                [("open_request", {"to": ["p02"], "kind": "choice", "question": "投谁？", "duration_s": 60,
                                   "options": [{"id": "p01", "label": "一号"}, {"id": "p03", "label": "三号"}]})],
            )
            await table.poke()
            for _ in range(200):
                await asyncio.sleep(0.02)
                state = orch.takeover_payload()
                if state["requests"] and state["channels"].get("public", {}).get("floor"):
                    break

        client.portal.call(start_and_open)  # type: ignore[union-attr]
        assert orch.takeover_payload()["channels"]["public"]["can_write"] is True

        stale = client.post("/api/seat/speak", json={"channel_id": "public", "content": "旧草稿", "based_on_seq": 0})
        assert stale.status_code == 200 and stale.json()["accepted"] is False and stale.json()["code"] == "STALE"
        seq = orch.takeover_payload()["seq"]
        too_long = client.post("/api/seat/speak", json={"channel_id": "public", "content": "字" * 20, "based_on_seq": seq})
        assert too_long.status_code == 200  # (v0.14, H16: an over-long speech is sent back, never cut: the floor is still the person's)
        assert too_long.json()["accepted"] is False and too_long.json()["code"] == "TOO_LONG"
        assert too_long.json()["data"] == {"chars": 20, "limit": 12}
        assert not [e for e in orch.world.ledger if e.type == "message" and e.channel_id == "public"]
        spoken = client.post("/api/seat/speak", json={"channel_id": "public", "content": "字" * 12, "based_on_seq": seq})
        assert spoken.status_code == 200
        body = spoken.json()
        assert body["accepted"] is True and body["code"] is None and not body.get("truncated")
        assert [e.actor_id for e in orch.world.ledger if e.type == "message" and e.channel_id == "public"] == ["p02"]

        # nothing in the body can name another actor
        for extra in ({"actor_id": "p03"}, {"seat": "p03"}, {"actor": "p03"}):
            assert client.post("/api/seat/speak", json={"channel_id": "public", "content": "x", **extra}).status_code == 422
        assert client.post("/api/seat/speak", json={"channel_id": "public", "content": ""}).status_code == 422
        assert client.post("/api/seat/speak", json={"channel_id": "public", "content": "x", "based_on_seq": -1}).status_code == 422
        assert client.post("/api/seat/speak", json={"content": "x"}).status_code == 422

        refused = client.post("/api/seat/speak", json={"channel_id": "nowhere", "content": "x"})
        assert refused.status_code == 200 and refused.json()["accepted"] is False and refused.json()["code"] == "UNKNOWN_CHANNEL"
        voted = client.post("/api/seat/respond", json={"request_id": "r1", "value": "p01", "reason": "直觉"})
        assert voted.status_code == 200 and voted.json()["accepted"] and voted.json()["data"]["closed"] is True
        late = client.post("/api/seat/respond", json={"request_id": "r1", "value": "p03"})
        assert late.json()["accepted"] is False and late.json()["code"] == "REQUEST_CLOSED"
        assert client.post("/api/seat/pass", json={"channel_id": "public"}).json()["accepted"] is True
        none = client.post("/api/seat/ability", json={"ability_id": "shout", "params": {"declaration": "x"}})
        assert none.status_code == 200 and none.json()["code"] in ("ABILITY_DISABLED", "FORBIDDEN")

        client.portal.call(orch.pause, "user")  # type: ignore[union-attr]
        paused = client.post("/api/seat/speak", json={"channel_id": "public", "content": "x"})
        assert paused.status_code == 409 and "已暂停" in paused.json()["detail"]
        # the seat can be given back once the game is paused; a running one is refused
        assert client.post("/api/takeover/release").status_code == 200
        client.portal.call(orch.shutdown)  # type: ignore[union-attr]


def test_the_re_deal_route_is_gone_and_a_game_that_is_not_ready_is_not_started(table: Table) -> None:
    """v0.14 (H52): the identities are dealt when the game starts; ``POST /api/control/shuffle-identities`` does not exist any more, and a start
    that does not pass the check answers 409 NOT_READY with its problems - and deals nothing."""
    orch = table.orch
    with make_client() as client:
        assert client.post("/api/takeover", json={"seat": "p03"}).status_code == 200
        assert client.post("/api/control/shuffle-identities").status_code == 404
        orch.session.model_bindings.pop("p04")  # one model is gone
        refused = client.post("/api/control/start")
        assert refused.status_code == 409 and refused.json()["code"] == "NOT_READY"
        assert [(p["scope"], p["seat"]) for p in refused.json()["problems"]] == [("binding", "p04")]
        assert "不能开局" in refused.json()["detail"]
        assert orch.session.status == "idle" and not orch.world.state.dealt
        orch.session.model_bindings["p04"] = table.orch.session.model_bindings["p01"]
        started = client.post("/api/control/start")
        assert started.status_code == 200 and started.json()["session"]["dealt"] is True
        client.portal.call(orch.shutdown)  # type: ignore[union-attr]


def test_the_write_endpoints_need_a_trusted_client(table: Table, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARENA_API_TOKEN", "test-token")
    with make_client() as client:
        for path, body in (
            ("/api/takeover", {"seat": "p02"}),
            ("/api/takeover/release", None),
            ("/api/seat/speak", {"channel_id": "public", "content": "x"}),
            ("/api/seat/pass", {"channel_id": "public"}),
            ("/api/seat/respond", {"request_id": "r1", "value": "x"}),
            ("/api/seat/ability", {"ability_id": "shout", "params": {}}),
        ):
            assert client.post(path, json=body).status_code == 401, path  # no token
        assert client.post("/api/takeover", json={"seat": "p02"}, headers=TOKEN).status_code == 200
        assert client.post("/api/takeover/release", headers=TOKEN).status_code == 200


def test_a_foreign_origin_is_refused(table: Table) -> None:
    with make_client() as client:
        refused = client.post("/api/takeover", json={"seat": "p02"}, headers={"Origin": "https://evil.example"})
        assert refused.status_code == 403
        assert table.orch.takeover_seat is None
