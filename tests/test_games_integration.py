"""The history of games against the REAL orchestrator and the REAL game store (docs/v0.13-plan.md F34-F36).

``tests/test_games_api.py`` tests the router with a stand-in for the orchestrator.  Here the live game is the real
:class:`GameOrchestrator` (real world, runtime, brain; scripted agents, nothing calls a model) whose games are
written by the real :class:`GameStore` into the temporary data directory of the test: a game is played, saved,
listed, replayed, replaced by a new one, loaded again and deleted - through the HTTP routes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from game_fakes import PLAYERS
from test_takeover_runtime import OPEN_PUBLIC, Table, wait_for

import app.api.games as games_module
import app.api.routes as routes_module
from app.api.games import router as games_router
from app.api.routes import router as routes_router
from app.storage.games import get_game_store

IDS = [p["id"] for p in PLAYERS]


@pytest.fixture()
def table(monkeypatch: pytest.MonkeyPatch) -> Table:
    monkeypatch.delenv("ARENA_API_TOKEN", raising=False)
    t = Table(monkeypatch)
    t.orch.game_store = get_game_store()  # (the fake store of the helper is replaced by the real one: plain JSON files)
    for module in (routes_module, games_module):
        monkeypatch.setattr(module, "orchestrator", t.orch)
    return t


def make_client() -> TestClient:
    application = FastAPI()
    application.include_router(routes_router)
    application.include_router(games_router)
    return TestClient(application)


def play_a_little(client: TestClient, table: Table, *, words: str = "欢迎来到本局。") -> None:
    """Start the game, let the moderator open the public channel and say something, then pause it."""
    orch = table.orch

    async def scenario() -> None:
        await orch.start_or_resume()
        await table.run([*OPEN_PUBLIC, ("send_message", {"channel_id": "public", "content": words})])
        await wait_for(lambda: any(e.type == "message" and e.channel_id == "public" for e in orch.world.ledger))
        await orch.pause("user")
        await orch.flush_autosave("test")

    client.portal.call(scenario)  # type: ignore[union-attr]


def messages_of(replay: dict) -> list[str]:
    return [ev["data"]["content"] for ev in replay["events"] if ev["type"] == "message.committed"]


def test_a_played_game_is_saved_listed_replayed_replaced_loaded_and_deleted(table: Table) -> None:
    orch = table.orch
    store = get_game_store()
    first = orch.session.session_id

    with make_client() as client:
        # ---- a game that has not started yet is listed, too (it is live; nothing of it is on disk before the first save)
        body = client.get("/api/games").json()
        assert body["current"] == first and [g["session_id"] for g in body["games"]] == [first]
        assert body["games"][0]["current"] is True and body["games"][0]["status"] == "idle"

        # ---- a game is played and paused: it is saved as plain JSON, and the list shows it as the live one
        play_a_little(client, table, words="第一局的第一句。")
        game_file = store.data_dir / "games" / first / "game.json"
        saved = json.loads(game_file.read_text(encoding="utf-8"))  # (plain, readable text: nothing encrypted)
        assert saved["session_id"] == first and saved["schema_version"] == 5 and saved["status"] == "paused"
        assert saved["started_at"] and saved["created_at"]
        games = client.get("/api/games").json()["games"]
        assert [g["session_id"] for g in games] == [first]
        assert games[0]["current"] is True and games[0]["status"] == "paused" and games[0]["human_involved"] is False
        assert games[0]["players"] == 4 and games[0]["duration_gt"] >= 0

        # ---- the paused live game can be replayed: the spectator events of what happened, nothing else
        replay = client.get(f"/api/games/{first}/replay")
        assert replay.status_code == 200
        data = replay.json()
        assert data["game"]["session_id"] == first and data["initial"]["session_id"] == first
        assert data["initial"]["status"] == "idle" and data["events"] and data["duration_gt"] >= 0
        assert messages_of(data) == ["第一局的第一句。"]
        assert all(set(ev) == {"seq", "gt", "type", "data"} for ev in data["events"])
        assert not any(ev["type"].startswith(("agent.", "budget.", "review.")) for ev in data["events"])

        # ---- ... but not while it runs (a replay shows everything) - the answer is a clear 409, nothing changes
        client.portal.call(orch.start_or_resume)  # type: ignore[union-attr]
        running = client.get(f"/api/games/{first}/replay")
        assert running.status_code == 409 and "暂停" in running.json()["detail"]
        # nothing can be loaded or deleted either way while it runs
        assert client.post("/api/games/nope/load").status_code == 404
        client.portal.call(orch.pause, "user")  # type: ignore[union-attr]

        # ---- a new game replaces it; the old one stays in the list (saved when it was replaced)
        created = client.post("/api/control/new")
        assert created.status_code == 200
        second = created.json()["session"]["session_id"]
        assert second != first
        client.portal.call(orch.flush_autosave, "test")  # type: ignore[union-attr]
        listed = {g["session_id"]: g for g in client.get("/api/games").json()["games"]}
        assert set(listed) == {first, second}
        assert listed[second]["current"] is True and listed[first]["current"] is False
        assert client.get("/api/games").json()["current"] == second

        # the live (idle) game cannot be deleted or loaded again; the first game can be replayed from the list
        assert client.delete(f"/api/games/{second}").status_code == 409
        assert client.post(f"/api/games/{second}/load").status_code == 409
        again = client.get(f"/api/games/{first}/replay").json()
        assert messages_of(again) == ["第一局的第一句。"], "the old game replays exactly as before"

        # ---- continue the first game: it becomes the live one, paused, with its ledger; the second is saved first
        loaded = client.post(f"/api/games/{first}/load")
        assert loaded.status_code == 200
        session = loaded.json()["session"]
        assert session["session_id"] == first and session["status"] == "paused"
        assert orch.session.session_id == first
        assert any(e.type == "message" and e.channel_id == "public" for e in orch.world.ledger), "its ledger came back"
        assert store.exists(second), "the game that was replaced is kept"
        assert client.get("/api/games").json()["current"] == first
        assert orch.session.status == "paused", "loading never starts a game"

        # ---- delete the other game: its file and its logs go, the live game and the roster stay
        logs = store.data_dir / "logs" / second
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "trace-0001.jsonl").write_text("{}\n", encoding="utf-8")
        roster_before = {p.name for p in (store.data_dir / "roster").glob("*")} if (store.data_dir / "roster").exists() else set()
        removed = client.delete(f"/api/games/{second}")
        assert removed.status_code == 200 and removed.json() == {"ok": True, "session_id": second}
        assert not (store.data_dir / "games" / second).exists() and not logs.exists()
        assert [g["session_id"] for g in client.get("/api/games").json()["games"]] == [first]
        roster_after = {p.name for p in (store.data_dir / "roster").glob("*")} if (store.data_dir / "roster").exists() else set()
        assert roster_after == roster_before, "deleting a game never touches the roster (persona and memory)"
        assert client.delete(f"/api/games/{second}").status_code == 404
        client.portal.call(orch.shutdown)  # type: ignore[union-attr]


def test_a_game_that_started_cannot_be_replaced_while_it_runs_and_a_failed_load_changes_nothing(table: Table) -> None:
    orch = table.orch
    store = get_game_store()
    with make_client() as client:
        play_a_little(client, table)
        first = orch.session.session_id
        client.post("/api/control/new")
        second = orch.session.session_id
        client.portal.call(orch.start_or_resume)  # type: ignore[union-attr]
        # the second game runs: nothing else may be loaded
        refused = client.post(f"/api/games/{first}/load")
        assert refused.status_code == 409 and "暂停" in refused.json()["detail"]
        assert orch.session.session_id == second
        client.portal.call(orch.pause, "user")  # type: ignore[union-attr]

        # a game file this version cannot open: a clear 400, the live game is untouched
        broken = json.loads((store.data_dir / "games" / first / "game.json").read_text(encoding="utf-8"))
        broken["schema_version"] = 4
        (store.data_dir / "games" / first / "game.json").write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        old = client.post(f"/api/games/{first}/load")
        assert old.status_code == 400
        assert "v0.12.1" in old.json()["detail"] and "migrate_v0121_to_archive" in old.json()["detail"]
        assert "新建了一局" not in old.json()["detail"], "no game is created by a refused load"
        assert orch.session.session_id == second and orch.session.status == "paused"
        # ... and it is not replayable either, with the same pointer to the conversion script
        replay = client.get(f"/api/games/{first}/replay")
        assert replay.status_code == 422 and "migrate_v0121_to_archive" in replay.json()["detail"]
        # it is still in the list (as what it is) and can be deleted
        listed = {g["session_id"]: g for g in client.get("/api/games").json()["games"]}
        assert first in listed
        assert client.delete(f"/api/games/{first}").status_code == 200
        client.portal.call(orch.shutdown)  # type: ignore[union-attr]


def test_a_game_a_person_played_is_flagged_and_cannot_be_replayed_while_the_seat_is_played(table: Table) -> None:
    orch = table.orch
    with make_client() as client:
        client.portal.call(orch.takeover, "p02")  # type: ignore[union-attr]
        play_a_little(client, table)
        live = orch.session.session_id
        # the live game, paused, with a seat played: no replay (it would show everything to the person at the table)
        blocked = client.get(f"/api/games/{live}/replay")
        assert blocked.status_code == 409 and "接管" in blocked.json()["detail"]
        assert client.get("/api/games").json()["games"][0]["human_involved"] is True
        # giving the seat back opens the replay again, and the game stays flagged: a person played a stretch of it
        client.portal.call(orch.release_takeover)  # type: ignore[union-attr]
        client.portal.call(orch.flush_autosave, "test")  # type: ignore[union-attr]
        assert client.get(f"/api/games/{live}/replay").status_code == 200
        assert client.get("/api/games").json()["games"][0]["human_involved"] is True
        client.portal.call(orch.shutdown)  # type: ignore[union-attr]


def test_the_history_endpoints_are_protected_like_every_write(table: Table, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARENA_API_TOKEN", "test-token")
    with make_client() as client:
        assert client.get("/api/games").status_code == 401
        assert client.get("/api/games", headers={"X-Arena-Token": "test-token"}).status_code == 200
        assert client.delete("/api/games/whatever", headers={"X-Arena-Token": "test-token"}).status_code == 404
        assert client.delete("/api/games/whatever").status_code == 401
        assert client.post("/api/games/whatever/load").status_code == 401
        assert client.get("/api/games/whatever/replay").status_code == 401
