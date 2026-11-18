"""``/api/games``: the history list, delete, continue and replay (docs/v0.13-plan.md F34-F36).

The games live in a real :class:`GameStore` (a temporary data directory); the live game is a small stand-in
for the orchestrator.  The replay itself is tested in tests/v12/world/test_replay.py.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from v12.world.test_replay import Run, payload_of, play

from app.api import games as games_api
from app.game.orchestrator import SessionLoadError
from app.game.session import GameSession
from app.storage.errors import StorageError
from app.storage.games import GameStore, get_game_store


def saved_game(session_id: str, **fields: Any) -> dict[str, Any]:
    """A minimal valid game payload (the model's own dump) with a few fields replaced."""
    payload = GameSession(session_id=session_id, title=fields.pop("title", "测试对局")).model_dump(mode="json")
    payload.update(fields)
    return payload


class FakeEngine(SimpleNamespace):
    """What the games router needs of the orchestrator."""

    def __init__(self, session: GameSession) -> None:
        super().__init__(
            session=session,
            flush_autosave=AsyncMock(return_value=True),
            load_game=AsyncMock(side_effect=self._load),
        )
        self.loaded: list[str] = []

    async def _load(self, session_id: str) -> GameSession:
        self.loaded.append(session_id)
        self.session = GameSession(session_id=session_id, status="paused")
        return self.session

    def public_session(self) -> dict[str, Any]:
        return {"session_id": self.session.session_id, "status": self.session.status}

    def session_payload(self) -> dict[str, Any]:
        return self.session.model_dump(mode="json")


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    fake = FakeEngine(GameSession(session_id="live-game", status="paused"))
    monkeypatch.setattr(games_api, "orchestrator", fake)
    return fake


@pytest.fixture
def store(engine: FakeEngine) -> GameStore:
    return get_game_store()


@pytest.fixture
def client(engine: FakeEngine) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(games_api.router)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def remote(engine: FakeEngine) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(games_api.router)
    with TestClient(app, client=("203.0.113.5", 50000)) as test_client:
        yield test_client


# ----------------------------------------------------------------------------------------------- list


def test_the_list_marks_the_live_game_and_is_newest_first(client: TestClient, store: GameStore) -> None:
    store.save("old-one", saved_game("old-one", title="旧的一局", status="finished"))
    store.save("live-game", saved_game("live-game", status="paused"))
    body = client.get("/api/games").json()
    assert body["current"] == "live-game"
    ids = [g["session_id"] for g in body["games"]]
    assert set(ids) == {"old-one", "live-game"}
    assert {g["session_id"]: g["current"] for g in body["games"]} == {"old-one": False, "live-game": True}
    first = body["games"][0]
    assert {"title", "status", "winner", "duration_gt", "tokens", "cost_usd", "human_involved", "updated_at"} <= set(first)


def test_a_game_that_is_not_saved_yet_is_still_listed(client: TestClient, store: GameStore) -> None:
    body = client.get("/api/games").json()
    assert [g["session_id"] for g in body["games"]] == ["live-game"] and body["games"][0]["current"] is True


def test_a_game_with_a_human_is_flagged(client: TestClient, store: GameStore) -> None:
    store.save("with-human", saved_game("with-human", human_spans=[{"seat": "N3", "from_seq": 4, "to_seq": 90}]))
    games = {g["session_id"]: g for g in client.get("/api/games").json()["games"]}
    assert games["with-human"]["human_involved"] is True


# --------------------------------------------------------------------------------------------- delete


def test_deleting_a_game_removes_it_and_its_logs_and_leaves_the_roster_alone(
    client: TestClient, store: GameStore, tmp_path: Path
) -> None:
    store.save("doomed", saved_game("doomed"))
    logs = store.data_dir / "logs" / "doomed"
    logs.mkdir(parents=True)
    (logs / "trace-0001.jsonl").write_text('{"event":"x"}\n', encoding="utf-8")
    roster = store.data_dir / "roster"
    roster.mkdir(parents=True, exist_ok=True)
    memory = roster / "N1.memory.md"
    memory.write_text("## 战绩\n场次 3\n", encoding="utf-8")

    response = client.delete("/api/games/doomed")
    assert response.status_code == 200 and response.json()["ok"] is True
    assert not store.exists("doomed") and not logs.exists()
    assert memory.read_text(encoding="utf-8") == "## 战绩\n场次 3\n"  # an agent's memory is not part of a game
    assert "doomed" not in [g["session_id"] for g in client.get("/api/games").json()["games"]]


def test_the_live_game_cannot_be_deleted(client: TestClient, store: GameStore) -> None:
    store.save("live-game", saved_game("live-game"))
    response = client.delete("/api/games/live-game")
    assert response.status_code == 409 and "当前对局" in response.json()["detail"]
    assert store.exists("live-game")


def test_deleting_an_unknown_or_malformed_id(client: TestClient) -> None:
    assert client.delete("/api/games/nope").status_code == 404
    assert client.delete("/api/games/has.dot").status_code == 400
    assert client.delete("/api/games/CON").status_code == 400  # a Windows device name


def test_a_remote_client_cannot_delete_or_load_without_a_token(remote: TestClient, store: GameStore) -> None:
    store.save("keep", saved_game("keep"))
    assert remote.delete("/api/games/keep").status_code in (401, 403)
    assert remote.post("/api/games/keep/load").status_code in (401, 403)
    assert store.exists("keep")
    assert remote.get("/api/games").status_code in (200, 401, 403)  # reading is the ordinary API access rule


# ----------------------------------------------------------------------------------------------- load


def test_continuing_a_game_loads_it_through_the_orchestrator(client: TestClient, engine: FakeEngine, store: GameStore) -> None:
    store.save("other", saved_game("other", status="paused"))
    response = client.post("/api/games/other/load")
    assert response.status_code == 200
    assert engine.loaded == ["other"] and response.json()["session"]["session_id"] == "other"


@pytest.mark.parametrize(
    "error, status",
    [(RuntimeError("对局进行中，不能载入其他对局"), 409), (SessionLoadError("存档校验失败"), 400)],
)
def test_a_game_that_cannot_be_loaded_is_a_clear_error(
    client: TestClient, engine: FakeEngine, store: GameStore, error: Exception, status: int
) -> None:
    store.save("other", saved_game("other"))
    engine.load_game.side_effect = error
    response = client.post("/api/games/other/load")
    assert response.status_code == status and str(error) in response.json()["detail"]


def test_loading_the_live_game_or_an_unknown_one(client: TestClient, engine: FakeEngine, store: GameStore) -> None:
    store.save("live-game", saved_game("live-game"))
    assert client.post("/api/games/live-game/load").status_code == 409
    assert client.post("/api/games/ghost/load").status_code == 404
    assert engine.loaded == []


# --------------------------------------------------------------------------------------------- replay


@pytest.fixture(scope="module")
def played() -> dict[str, Any]:
    """The payload of a scripted game on the real world (a stored game of the current format)."""
    run = Run()
    play(run)
    payload = payload_of(run.world, run.spec)
    payload["session_id"] = "played-game"
    return json.loads(json.dumps(payload))


def test_a_saved_game_replays(client: TestClient, store: GameStore, played: dict[str, Any]) -> None:
    store.save("played-game", played)
    response = client.get("/api/games/played-game/replay")
    assert response.status_code == 200
    replay = response.json()
    assert replay["game"]["session_id"] == "played-game" and replay["game"]["winner"] == "红"
    assert len(replay["events"]) > 100 and replay["initial"]["status"] == "idle" and replay["duration_gt"] > 0


def test_another_game_replays_while_the_live_one_runs(client: TestClient, engine: FakeEngine, store: GameStore, played: dict[str, Any]) -> None:
    engine.session.status = "running"
    store.save("played-game", played)
    assert client.get("/api/games/played-game/replay").status_code == 200
    engine.flush_autosave.assert_not_awaited()  # only the live game is flushed first


def test_the_live_game_does_not_replay_while_it_runs_or_while_a_seat_is_played(
    client: TestClient, engine: FakeEngine, store: GameStore, played: dict[str, Any]
) -> None:
    store.save("live-game", {**played, "session_id": "live-game"})
    engine.session.status = "running"
    assert client.get("/api/games/live-game/replay").status_code == 409
    engine.session.status = "paused"
    engine.session.takeover.seat = "N3"
    assert client.get("/api/games/live-game/replay").status_code == 409
    engine.flush_autosave.assert_not_awaited()
    engine.session.takeover.seat = None
    assert client.get("/api/games/live-game/replay").status_code == 200
    engine.flush_autosave.assert_awaited_once_with("pre-replay")  # what is on screen is what is on disk


def test_replay_errors(client: TestClient, store: GameStore, played: dict[str, Any]) -> None:
    assert client.get("/api/games/ghost/replay").status_code == 404
    assert client.get("/api/games/bad.id/replay").status_code == 400
    store.save("v4-game", {**played, "session_id": "v4-game", "schema_version": 4})
    response = client.get("/api/games/v4-game/replay")
    assert response.status_code == 422 and "migrate_v0121_to_archive" in response.json()["detail"]
    store.save("broken", {**played, "session_id": "broken", "world": {"ledger": "nope"}})
    assert client.get("/api/games/broken/replay").status_code == 422


def test_a_storage_error_while_reading_is_reported_not_raised(
    client: TestClient, store: GameStore, played: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save("played-game", played)

    def boom(self: GameStore, session_id: str) -> dict:
        raise StorageError("对局文件和它的上一份副本都无法读取。")

    monkeypatch.setattr(GameStore, "load", boom)
    response = client.get("/api/games/played-game/replay")
    assert response.status_code == 422 and "无法读取" in response.json()["detail"]
