"""``/api/games``: the history list, its detail, delete, continue, replay, export and import (docs/v0.13-plan.md F34-F36; v0.14: H40, H42,
contract 13.7).

The games live in a real :class:`GameStore` (a temporary data directory); the live game is a small stand-in for the orchestrator.  The replay itself
is tested in tests/v12/world/test_replay.py, the codec of ``.arenagame`` in tests/archive/test_arenagame.py.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from v12.world.test_replay import Run, payload_of, play

from app.api import games as games_api
from app.archive import arenagame
from app.archive.export import ExportOptions, export_archive
from app.archive.jobs import get_archive_manager, reset_archive_manager
from app.game.orchestrator import SessionLoadError
from app.game.session import GameSession, Participant
from app.storage.errors import StorageError
from app.storage.games import DELETE_NOTE, GameStore, get_game_store
from app.storage.settings import get_settings_store


def saved_game(session_id: str, **fields: Any) -> dict[str, Any]:
    """A minimal valid game payload (the model's own dump) with a few fields replaced."""
    payload = GameSession(session_id=session_id, title=fields.pop("title", "测试对局")).model_dump(mode="json")
    payload.update(fields)
    return payload


ROLES = {
    "werewolf": {"faction": "wolves", "tags": ["狼人", "存活"]},
    "seer": {"faction": "good", "tags": ["预言家", "存活"]},
}
FACTION_NAMES = {"wolves": "狼人阵营", "good": "好人阵营"}


def table(**changes: Any) -> dict[str, Any]:
    """The parts of a game that the detail reads: two seats with bots, who they were in the world and the scenario's words for it."""
    fields: dict[str, Any] = {
        "participants": [
            {"id": "N1", "seat": "1", "name": "Alice", "bot_id": "b-00000001", "avatar": {"v": 1, "face": "f01"}},
            {"id": "N2", "seat": "2", "name": "Bob", "bot_id": "b-00000002"},
        ],
        "scenario_spec": {"setup": {"roles": ROLES, "faction_names": FACTION_NAMES}},
        "world": {
            "format": 1,
            "clock_now": 5.0,
            "state": {
                "winner": None,
                "players": [
                    {"id": "N1", "seat": "1", "name": "Alice", "role": "werewolf", "faction": "wolves"},
                    {"id": "N2", "seat": "2", "name": "Bob", "role": "seer", "faction": "good"},
                ],
            },
            "ledger": [],
        },
        "review_done": ["N1"],
    }
    fields.update(changes)
    return fields


class FakeEngine(SimpleNamespace):
    """What the games router needs of the orchestrator."""

    def __init__(self, session: GameSession) -> None:
        super().__init__(
            session=session,
            flush_autosave=AsyncMock(return_value=True),
            load_game=AsyncMock(side_effect=self._load),
            new_game=AsyncMock(side_effect=self._new_game),
        )
        self.loaded: list[str] = []
        self.new_games = 0
        self.files_when_the_new_game_was_made: bool | None = None

    async def _load(self, session_id: str) -> GameSession:
        self.loaded.append(session_id)
        self.session = GameSession(session_id=session_id, status="paused")
        return self.session

    async def _new_game(self, **_kwargs: Any) -> GameSession:
        self.new_games += 1
        # (the real new_game saves the old game first and only then makes the new one: its files are still there at this moment)
        self.files_when_the_new_game_was_made = get_game_store().exists(self.session.session_id)
        self.session = GameSession(session_id=f"fresh-{self.new_games}", status="idle")
        return self.session

    def public_session(self) -> dict[str, Any]:
        return {"session_id": self.session.session_id, "status": self.session.status}

    def session_payload(self) -> dict[str, Any]:
        return self.session.model_dump(mode="json")


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    fake = FakeEngine(GameSession(session_id="live-game", status="paused"))
    monkeypatch.setattr(games_api, "orchestrator", fake)
    monkeypatch.setattr(games_api, "_DELETE_RETRY_SECONDS", 0.0)
    return fake


@pytest.fixture
def store(engine: FakeEngine) -> GameStore:
    return get_game_store()


@pytest.fixture(autouse=True)
def fresh_archive_manager() -> Iterator[None]:
    reset_archive_manager()
    yield
    reset_archive_manager()


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


def tmp_files(store: GameStore) -> list[str]:
    """What is left in ``data/tmp/``: nothing, after a request has been answered."""
    folder = store.data_dir / "tmp"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


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


def test_every_entry_has_the_fields_of_the_history_management(client: TestClient, store: GameStore) -> None:
    store.save("v6", saved_game("v6", status="finished", **table(review_done=["N1"])))
    store.save("v5", saved_game("v5", schema_version=5, **table(review_done=["N1", "N2"], participants=[{"id": "N1", "name": "Alice"}])))
    store.save("v4", saved_game("v4", schema_version=4))
    logs = store.data_dir / "logs" / "v6"
    logs.mkdir(parents=True)
    (logs / "trace-0001.jsonl").write_bytes(b"x" * 321)
    games = {g["session_id"]: g for g in client.get("/api/games").json()["games"]}
    assert {"readable", "size_bytes", "log_bytes", "review", "participants_brief"} <= set(games["v6"])
    assert (games["v6"]["readable"], games["v6"]["log_bytes"], games["v6"]["review"]) == (True, 321, {"done": 1, "total": 2})
    assert games["v6"]["participants_brief"] == ["Alice", "Bob"]
    assert games["v6"]["size_bytes"] == (store.data_dir / "games" / "v6" / "game.json").stat().st_size > 0
    assert (games["v5"]["readable"], games["v5"]["review"], games["v5"]["log_bytes"]) == (True, {"done": 2, "total": None}, 0)
    assert games["v4"]["readable"] is False


def test_a_game_that_is_not_saved_yet_is_still_listed(client: TestClient, store: GameStore) -> None:
    body = client.get("/api/games").json()
    assert [g["session_id"] for g in body["games"]] == ["live-game"] and body["games"][0]["current"] is True
    assert {"readable", "size_bytes", "log_bytes", "review", "participants_brief"} <= set(body["games"][0])
    assert body["games"][0]["log_bytes"] == 0 and body["games"][0]["readable"] is True


def test_a_game_with_a_human_is_flagged(client: TestClient, store: GameStore) -> None:
    store.save("with-human", saved_game("with-human", human_spans=[{"seat": "N3", "from_seq": 4, "to_seq": 90}]))
    games = {g["session_id"]: g for g in client.get("/api/games").json()["games"]}
    assert games["with-human"]["human_involved"] is True


# --------------------------------------------------------------------------------------------- detail


def test_the_detail_of_a_finished_game_says_who_was_what(client: TestClient, store: GameStore) -> None:
    store.save("done", saved_game("done", status="finished", human_spans=[{"seat": "N2", "from_seq": 4, "to_seq": None}], **table()))
    (store.data_dir / "logs" / "done").mkdir(parents=True)
    (store.data_dir / "logs" / "done" / "trace-0001.jsonl").write_bytes(b"x" * 40)
    response = client.get("/api/games/done/detail")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"participants", "files", "human_spans"}
    assert body["participants"] == [
        {"seat": "N1", "bot_id": "b-00000001", "name": "Alice", "avatar": {"v": 1, "face": "f01"}, "role": "狼人", "faction": "狼人阵营", "review": "done"},
        {"seat": "N2", "bot_id": "b-00000002", "name": "Bob", "avatar": None, "role": "预言家", "faction": "好人阵营", "review": "pending"},
    ]
    assert body["files"]["log_bytes"] == 40 and body["files"]["game_bytes"] > 0 and body["files"]["prev_bytes"] == 0
    assert body["human_spans"] == [{"seat": "N2", "from_seq": 4, "to_seq": None}]


@pytest.mark.parametrize("status", ["idle", "paused", "running"])
def test_nobody_learns_an_identity_from_the_detail_of_a_game_that_is_not_finished(client: TestClient, store: GameStore, status: str) -> None:
    store.save("going", saved_game("going", status=status, **table()))
    response = client.get("/api/games/going/detail")
    assert [(p["role"], p["faction"]) for p in response.json()["participants"]] == [(None, None), (None, None)]
    assert "狼人" not in response.text and "werewolf" not in response.text and "wolves" not in response.text


def test_the_detail_of_a_v5_game_has_no_bots_and_no_reviews(client: TestClient, store: GameStore) -> None:
    participants = [{"id": "N1", "name": "Alice"}, {"id": "N2", "name": "Bob"}]
    store.save("old", saved_game("old", schema_version=5, status="finished", **table(participants=participants, review_done=["N2"])))
    rows = client.get("/api/games/old/detail").json()["participants"]
    assert [(p["bot_id"], p["review"]) for p in rows] == [("", "na"), ("", "done")]
    assert [p["role"] for p in rows] == ["狼人", "预言家"], "(a finished game of v0.13 shows its identities as well)"


def test_the_detail_of_the_live_game_is_what_is_on_screen_not_what_was_saved(client: TestClient, engine: FakeEngine, store: GameStore) -> None:
    store.save("live-game", saved_game("live-game", status="paused", **table(review_done=[])))  # (an older save)
    live = engine.session
    for name, value in table(review_done=["N1", "N2"]).items():
        setattr(live, name, [Participant(**item) for item in value] if name == "participants" else value)
    body = client.get("/api/games/live-game/detail").json()
    assert [p["review"] for p in body["participants"]] == ["done", "done"], "the reviews that were written after the last save are shown"
    assert [p["role"] for p in body["participants"]] == [None, None], "the live game is paused: no identity"
    live.status = "finished"
    body = client.get("/api/games/live-game/detail").json()
    assert [p["role"] for p in body["participants"]] == ["狼人", "预言家"]


def test_the_detail_of_a_live_game_that_was_never_saved(client: TestClient, store: GameStore) -> None:
    response = client.get("/api/games/live-game/detail")
    assert response.status_code == 200 and response.json()["files"] == {"game_bytes": 0, "prev_bytes": 0, "log_bytes": 0}


def test_the_detail_errors(client: TestClient, store: GameStore, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.get("/api/games/ghost/detail").status_code == 404
    assert client.get("/api/games/bad.id/detail").status_code == 400
    assert client.get("/api/games/CON/detail").status_code == 400
    folder = store.data_dir / "games" / "broken"
    folder.mkdir(parents=True)
    (folder / "game.json").write_bytes(b"not json")
    response = client.get("/api/games/broken/detail")
    assert response.status_code == 422 and "无法读取" in response.json()["detail"]


# --------------------------------------------------------------------------------------------- delete


def test_deleting_a_game_removes_it_and_its_logs_and_leaves_the_bots_alone(client: TestClient, store: GameStore, tmp_path: Path) -> None:
    store.save("doomed", saved_game("doomed"))
    logs = store.data_dir / "logs" / "doomed"
    logs.mkdir(parents=True)
    (logs / "trace-0001.jsonl").write_text('{"event":"x"}\n', encoding="utf-8")
    memory = store.data_dir / "bots" / "b-0a0b0c0d" / "people.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("- Bob：发言短。\n", encoding="utf-8")

    response = client.delete("/api/games/doomed")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "session_id": "doomed", "note": DELETE_NOTE, "new_game": False, "new_session_id": None}
    assert not store.exists("doomed") and not logs.exists()
    assert memory.read_text(encoding="utf-8") == "- Bob：发言短。\n"  # what a bot remembers is not part of a game
    assert "doomed" not in [g["session_id"] for g in client.get("/api/games").json()["games"]]


def test_the_note_is_the_sentence_of_the_contract(client: TestClient, store: GameStore) -> None:
    store.save("doomed", saved_game("doomed"))
    assert client.delete("/api/games/doomed").json()["note"] == "bot 已经通过复盘形成的记忆不会被删除，也无法通过删除对局撤销。"


@pytest.mark.parametrize("status", ["idle", "paused", "finished"])
def test_the_live_game_can_be_deleted_a_new_blank_game_takes_its_place(
    client: TestClient, engine: FakeEngine, store: GameStore, status: str
) -> None:
    engine.session.status = status
    store.save("live-game", saved_game("live-game", status=status))
    logs = store.data_dir / "logs" / "live-game"
    logs.mkdir(parents=True)
    (logs / "trace-0001.jsonl").write_text("{}\n", encoding="utf-8")

    response = client.delete("/api/games/live-game")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "session_id": "live-game", "note": DELETE_NOTE, "new_game": True, "new_session_id": "fresh-1"}
    engine.new_game.assert_awaited_once()
    assert engine.files_when_the_new_game_was_made is True, "the new game is made first (it saves the old one), the old game is deleted after"
    assert not store.exists("live-game") and not logs.exists()
    body = client.get("/api/games").json()
    assert body["current"] == "fresh-1" and "live-game" not in [g["session_id"] for g in body["games"]]


def test_a_game_that_is_being_played_cannot_be_deleted(client: TestClient, engine: FakeEngine, store: GameStore) -> None:
    store.save("live-game", saved_game("live-game"))
    engine.session.status = "running"
    response = client.delete("/api/games/live-game")
    assert response.status_code == 409 and response.json()["detail"] == "进行中的对局不能删除，请先暂停。"
    assert store.exists("live-game")
    engine.new_game.assert_not_awaited()


def test_a_seat_that_is_played_by_a_person_does_not_stop_the_deletion_of_a_paused_game(client: TestClient, engine: FakeEngine, store: GameStore) -> None:
    """(The orchestrator's ``new_game`` ends the takeover; the router has nothing to add.)"""
    store.save("live-game", saved_game("live-game", status="paused"))
    engine.session.takeover.seat = "N3"
    response = client.delete("/api/games/live-game")
    assert response.status_code == 200 and response.json()["new_game"] is True
    assert not store.exists("live-game")


def test_a_new_game_that_is_refused_deletes_nothing(client: TestClient, engine: FakeEngine, store: GameStore) -> None:
    store.save("live-game", saved_game("live-game"))
    engine.new_game.side_effect = RuntimeError("赛后复盘正在进行，请等它结束后再新建对局。")
    response = client.delete("/api/games/live-game")
    assert response.status_code == 409 and response.json()["detail"] == "赛后复盘正在进行，请等它结束后再新建对局。"
    assert store.exists("live-game") and engine.session.session_id == "live-game"


def test_a_live_game_that_was_never_saved_can_be_deleted_too(client: TestClient, engine: FakeEngine, store: GameStore) -> None:
    assert not store.exists("live-game")
    response = client.delete("/api/games/live-game")
    assert response.status_code == 200 and response.json()["new_game"] is True and response.json()["new_session_id"] == "fresh-1"


def test_the_game_the_next_start_would_bring_back_is_forgotten_with_it(client: TestClient, store: GameStore) -> None:
    """Otherwise the next start would say "the last game's file no longer exists" about a game the user deleted on purpose."""
    settings = get_settings_store()
    store.save("live-game", saved_game("live-game"))
    settings.set_last_session_id("live-game")
    assert client.delete("/api/games/live-game").status_code == 200
    assert settings.get_last_session_id() is None

    store.save("other", saved_game("other"))
    store.save("kept", saved_game("kept"))
    settings.set_last_session_id("kept")
    assert client.delete("/api/games/other").status_code == 200
    assert settings.get_last_session_id() == "kept", "only the pointer to the deleted game is cleared"
    assert client.delete("/api/games/kept").status_code == 200
    assert settings.get_last_session_id() is None


def test_a_deletion_that_does_not_work_at_once_is_tried_again(
    client: TestClient, store: GameStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows a log file the process has only just let go of can still be busy for a moment."""
    store.save("stubborn", saved_game("stubborn"))
    real = GameStore.delete
    calls = {"n": 0}

    def flaky(self: GameStore, session_id: str) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise StorageError("无法删除对局文件。")
        real(self, session_id)

    monkeypatch.setattr(GameStore, "delete", flaky)
    assert client.delete("/api/games/stubborn").status_code == 200
    assert calls["n"] == 3 and not store.exists("stubborn")


@pytest.mark.parametrize("live", [False, True])
def test_a_game_whose_files_cannot_be_deleted_is_a_clear_409(
    client: TestClient, engine: FakeEngine, store: GameStore, monkeypatch: pytest.MonkeyPatch, live: bool
) -> None:
    game_id = "live-game" if live else "stubborn"
    store.save(game_id, saved_game(game_id))

    def locked(self: GameStore, session_id: str) -> None:
        raise StorageError("无法删除对局文件。")

    monkeypatch.setattr(GameStore, "delete", locked)
    response = client.delete(f"/api/games/{game_id}")
    body = response.json()
    assert response.status_code == 409 and body["code"] == "in_use" and "暂时无法删除" in body["detail"]
    assert body["new_game"] is live and body["new_session_id"] == ("fresh-1" if live else None)
    assert ("新的空白对局已经建好" in body["detail"]) is live
    assert store.exists(game_id)


def test_deleting_an_unknown_or_malformed_id(client: TestClient) -> None:
    assert client.delete("/api/games/nope").status_code == 404
    assert client.delete("/api/games/has.dot").status_code == 400
    assert client.delete("/api/games/CON").status_code == 400  # a Windows device name


def test_a_remote_client_cannot_change_anything_without_a_token(remote: TestClient, store: GameStore, tmp_path: Path) -> None:
    store.save("keep", saved_game("keep"))
    assert remote.delete("/api/games/keep").status_code in (401, 403)
    assert remote.post("/api/games/keep/load").status_code in (401, 403)
    assert remote.post("/api/games/import", content=b"x").status_code in (401, 403)
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


def test_a_v5_and_a_v6_game_both_replay(client: TestClient, store: GameStore, played: dict[str, Any]) -> None:
    store.save("v5-game", {**played, "session_id": "v5-game", "schema_version": 5})
    store.save("v6-game", {**played, "session_id": "v6-game", "schema_version": 6})
    assert client.get("/api/games/v5-game/replay").status_code == 200
    assert client.get("/api/games/v6-game/replay").status_code == 200


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


# --------------------------------------------------------------------------------------------- export


def zip_of(response: Any) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(response.content))


def test_a_game_is_exported_as_an_arenagame_named_after_its_id_and_the_day(
    client: TestClient, store: GameStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(games_api, "day_stamp", lambda: "20261118")
    store.save("done", saved_game("done", status="finished", title="一局"))
    logs = store.data_dir / "logs" / "done"
    logs.mkdir(parents=True)
    (logs / "trace-0001.jsonl").write_bytes(b'{"event":"a"}\n')
    response = client.get("/api/games/done/export")
    assert response.status_code == 200
    assert response.headers["content-disposition"] == "attachment; filename=\"game-done-20261118.arenagame\"; filename*=UTF-8''game-done-20261118.arenagame"
    assert response.headers["content-type"].startswith("application/octet-stream")
    archive = zip_of(response)
    assert sorted(archive.namelist()) == ["games/done/game.json", "logs/done/trace-0001.jsonl", "manifest.json"]
    manifest = json.loads(archive.read("manifest.json"))
    assert (manifest["format"], manifest["format_version"], manifest["session_id"]) == ("arenagame", 1, "done")
    assert json.loads(archive.read("games/done/game.json"))["title"] == "一局"
    assert tmp_files(store) == [], "the file made for the download is removed once it has been sent"


def test_the_name_of_the_file_has_the_day_of_today_by_default(client: TestClient, store: GameStore) -> None:
    store.save("done", saved_game("done"))
    disposition = client.get("/api/games/done/export").headers["content-disposition"]
    assert f"game-done-{datetime.now():%Y%m%d}.arenagame" in disposition


def test_the_previous_copy_does_not_go_into_the_file(client: TestClient, store: GameStore) -> None:
    store.save("done", saved_game("done", title="一"))
    store.save("done", saved_game("done", title="二"))
    assert (store.data_dir / "games" / "done" / "game.prev.json").is_file()
    assert "games/done/game.prev.json" not in zip_of(client.get("/api/games/done/export")).namelist()


def test_a_paused_live_game_is_saved_first_so_the_file_is_what_the_screen_shows(client: TestClient, engine: FakeEngine, store: GameStore) -> None:
    store.save("live-game", saved_game("live-game", title="存过的"))
    engine.session.status = "paused"
    response = client.get("/api/games/live-game/export")
    assert response.status_code == 200
    engine.flush_autosave.assert_awaited_once_with("pre-export")


def test_the_live_game_cannot_be_exported_while_it_runs_or_while_a_seat_is_played(client: TestClient, engine: FakeEngine, store: GameStore) -> None:
    """The file holds everything, identities included - exactly why the replay of such a game is refused as well."""
    store.save("live-game", saved_game("live-game"))
    engine.session.status = "running"
    response = client.get("/api/games/live-game/export")
    assert response.status_code == 409 and "请先暂停" in response.json()["detail"]
    engine.session.status = "paused"
    engine.session.takeover.seat = "N3"
    assert client.get("/api/games/live-game/export").status_code == 409
    engine.flush_autosave.assert_not_awaited()
    assert tmp_files(store) == []


def test_another_game_can_be_exported_while_the_live_one_runs(client: TestClient, engine: FakeEngine, store: GameStore) -> None:
    engine.session.status = "running"
    store.save("done", saved_game("done", status="finished"))
    assert client.get("/api/games/done/export").status_code == 200
    engine.flush_autosave.assert_not_awaited()


def test_a_game_of_v013_can_be_exported_too(client: TestClient, store: GameStore) -> None:
    store.save("old", saved_game("old", schema_version=5))
    assert json.loads(zip_of(client.get("/api/games/old/export")).read("games/old/game.json"))["schema_version"] == 5


def test_a_game_that_cannot_be_exported_is_a_clear_error(client: TestClient, store: GameStore) -> None:
    assert client.get("/api/games/ghost/export").status_code == 404
    assert client.get("/api/games/bad.id/export").status_code == 400
    store.save("old", saved_game("old", schema_version=4))
    response = client.get("/api/games/old/export")
    assert response.status_code == 422 and response.json()["code"] == "invalid_archive" and "v4" in response.json()["detail"]
    folder = store.data_dir / "games" / "broken"
    folder.mkdir(parents=True)
    (folder / "game.json").write_bytes(b"not json")
    response = client.get("/api/games/broken/export")
    assert response.status_code == 422 and "无法读取" in response.json()["detail"]
    assert tmp_files(store) == [], "nothing is left behind by a refusal"


# --------------------------------------------------------------------------------------------- import


def arenagame_bytes(tmp_path: Path, session_id: str = "imported", *, schema: int = 6, status: str = "finished", **fields: Any) -> bytes:
    """A ``.arenagame`` made by another machine (another data directory): the game ``session_id`` and one log."""
    source = GameStore(tmp_path / "source-data")
    source.save(session_id, saved_game(session_id, status=status, schema_version=schema, **fields))
    logs = tmp_path / "source-data" / "logs" / session_id
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "trace-0001.jsonl").write_bytes(b'{"event":"imported"}\n')
    target = tmp_path / f"{session_id}.arenagame"
    arenagame.export_game(source, session_id, target)
    return target.read_bytes()


def post_import(client: TestClient, data: bytes | Iterator[bytes], **params: str) -> Any:
    return client.post("/api/games/import", content=data, params={"name": "mine.arenagame", **params})


@pytest.mark.parametrize("schema", [5, 6])
def test_a_game_file_is_added_to_the_history(client: TestClient, store: GameStore, tmp_path: Path, schema: int) -> None:
    response = post_import(client, arenagame_bytes(tmp_path, schema=schema, **table()))
    assert response.status_code == 200
    assert response.json() == {"session_id": "imported", "status": "finished", "replaced": False, "copied": False}
    assert store.load("imported")["schema_version"] == schema
    assert (store.data_dir / "logs" / "imported" / "trace-0001.jsonl").read_bytes() == b'{"event":"imported"}\n'
    games = {g["session_id"]: g for g in client.get("/api/games").json()["games"]}
    assert games["imported"]["readable"] is True and games["imported"]["log_bytes"] == len(b'{"event":"imported"}\n')
    assert tmp_files(store) == [], "the upload and the unpacked copy are removed"


def test_an_exported_game_can_be_imported_again_after_it_was_deleted(client: TestClient, store: GameStore) -> None:
    store.save("done", saved_game("done", status="finished", title="一局"))
    exported = client.get("/api/games/done/export").content
    assert client.delete("/api/games/done").status_code == 200 and not store.exists("done")
    response = post_import(client, exported)
    assert response.status_code == 200 and response.json()["session_id"] == "done"
    assert store.load("done")["title"] == "一局"


def test_a_taken_id_is_a_409_that_names_the_game_and_changes_nothing(client: TestClient, store: GameStore, tmp_path: Path) -> None:
    store.save("imported", saved_game("imported", title="本来的"))
    before = (store.data_dir / "games" / "imported" / "game.json").read_bytes()
    for params in ({}, {"on_conflict": "ask"}):
        response = post_import(client, arenagame_bytes(tmp_path, title="来的"), **params)
        assert response.status_code == 409
        assert response.json() == {"detail": response.json()["detail"], "code": "exists", "session_id": "imported"}
        assert "imported" in response.json()["detail"]
    assert (store.data_dir / "games" / "imported" / "game.json").read_bytes() == before
    assert tmp_files(store) == []


def test_overwriting_replaces_the_game_and_its_logs(client: TestClient, store: GameStore, tmp_path: Path) -> None:
    store.save("imported", saved_game("imported", title="本来的"))
    old_logs = store.data_dir / "logs" / "imported"
    old_logs.mkdir(parents=True)
    (old_logs / "trace-0009.jsonl").write_bytes(b"old\n")
    response = post_import(client, arenagame_bytes(tmp_path, title="来的"), on_conflict="overwrite")
    assert response.status_code == 200 and response.json()["replaced"] is True and response.json()["copied"] is False
    assert store.load("imported")["title"] == "来的"
    assert sorted(p.name for p in old_logs.iterdir()) == ["trace-0001.jsonl"]


def test_a_copy_keeps_both_games(client: TestClient, store: GameStore, tmp_path: Path) -> None:
    store.save("imported", saved_game("imported", title="本来的"))
    response = post_import(client, arenagame_bytes(tmp_path, title="来的"), on_conflict="copy")
    body = response.json()
    assert response.status_code == 200 and body["copied"] is True and body["replaced"] is False and body["session_id"] != "imported"
    assert store.load("imported")["title"] == "本来的" and store.load(body["session_id"])["title"] == "来的"
    assert store.load(body["session_id"])["session_id"] == body["session_id"]


def test_the_live_game_is_never_overwritten_by_an_import(client: TestClient, engine: FakeEngine, store: GameStore, tmp_path: Path) -> None:
    data = arenagame_bytes(tmp_path, "live-game", title="来的")
    assert not store.exists("live-game"), "(the live game has not been saved yet: it is protected all the same)"
    response = post_import(client, data)
    assert response.status_code == 409 and response.json()["code"] == "exists" and response.json()["session_id"] == "live-game"
    response = post_import(client, data, on_conflict="overwrite")
    assert response.status_code == 409 and response.json()["code"] == "current" and "当前对局" in response.json()["detail"]
    assert not store.exists("live-game")
    response = post_import(client, data, on_conflict="copy")
    assert response.status_code == 200 and response.json()["copied"] is True and response.json()["session_id"] != "live-game"
    assert engine.session.session_id == "live-game", "the live game itself was not touched"


def test_a_game_can_be_added_while_another_one_is_being_played(client: TestClient, engine: FakeEngine, store: GameStore, tmp_path: Path) -> None:
    engine.session.status = "running"
    assert post_import(client, arenagame_bytes(tmp_path)).status_code == 200


def test_the_policy_must_be_one_of_the_three(client: TestClient, store: GameStore, tmp_path: Path) -> None:
    response = post_import(client, arenagame_bytes(tmp_path), on_conflict="merge")
    assert response.status_code == 400 and response.json()["code"] == "bad_request" and "ask、overwrite 或 copy" in response.json()["detail"]
    assert not store.exists("imported")


@pytest.mark.parametrize(
    "data, reason",
    [
        (b"", "空的|没有收到"),
        (b"not a zip at all " * 20, "不是有效的存档文件"),
        (b"PK\x03\x04" + b"\0" * 30, "不是有效的存档文件|文件太小"),
    ],
    ids=["empty", "text", "broken-zip"],
)
def test_a_file_that_is_not_a_game_file_is_a_422(client: TestClient, store: GameStore, data: bytes, reason: str) -> None:
    response = post_import(client, data)
    assert response.status_code == 422 and response.json()["code"] == "invalid_archive"
    assert any(part in response.json()["detail"] for part in reason.split("|")), response.json()
    assert store.list_games() == [] and tmp_files(store) == []


def handmade(session_id: str, members: dict[str, bytes]) -> bytes:
    """A ``.arenagame`` put together by hand (what the exporter would refuse to make): the given members and a manifest that lists them."""
    files = [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in sorted(members.items())]
    manifest = {
        "format": "arenagame",
        "format_version": 1,
        "session_id": session_id,
        "created_at": "2026-11-18T00:42:00-08:00",
        "app_version": "v0.14.0",
        "files": files,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        archive.writestr("manifest.json", json.dumps(manifest))
    return buffer.getvalue()


def game_member(session_id: str, **fields: Any) -> dict[str, bytes]:
    return {f"games/{session_id}/game.json": json.dumps(saved_game(session_id, **fields)).encode("utf-8")}


def test_a_file_that_was_changed_after_it_was_made_is_refused(client: TestClient, store: GameStore, tmp_path: Path) -> None:
    good = arenagame_bytes(tmp_path)
    changed = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(good)) as source, zipfile.ZipFile(changed, "w", zipfile.ZIP_DEFLATED) as out:
        for info in source.infolist():
            data = source.read(info)
            if info.filename.startswith("logs/"):
                data = data.replace(b"imported", b"IMPORTED")  # (the same length: only the checksum notices)
            out.writestr(info, data)
    response = post_import(client, changed.getvalue())
    assert response.status_code == 422 and response.json()["code"] == "invalid_archive" and "SHA-256" in response.json()["detail"]
    assert store.list_games() == [] and tmp_files(store) == []


def test_a_game_of_v0121_is_refused_with_the_way_to_convert_it(client: TestClient, store: GameStore) -> None:
    response = post_import(client, handmade("oldie", game_member("oldie", schema_version=4)))
    assert response.status_code == 422 and response.json()["code"] == "invalid_archive"
    assert "v4" in response.json()["detail"] and "v5 或 v6" in response.json()["detail"] and "migrate_v0121_to_archive" in response.json()["detail"]
    assert not store.exists("oldie")


def test_a_file_with_something_else_inside_is_refused(client: TestClient, store: GameStore) -> None:
    for extra in ("settings.json", "games/oldie/game.prev.json", "games/other/game.json", "../evil.md"):
        members = {**game_member("oldie"), extra: b"{}"}
        response = post_import(client, handmade("oldie", members))
        assert response.status_code == 422 and response.json()["code"] == "invalid_archive", extra
    assert store.list_games() == [] and tmp_files(store) == []


def test_a_total_archive_handed_in_by_mistake_is_named_for_what_it_is(client: TestClient, store: GameStore, tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "settings.json").write_text("{}", encoding="utf-8")
    total = export_archive(tmp_path / "x", ExportOptions()).path.read_bytes()
    response = post_import(client, total)
    assert response.status_code == 422 and "这是总存档" in response.json()["detail"] and "不是对局文件" in response.json()["detail"]


def test_a_file_over_the_size_limit_is_a_413_whether_or_not_its_size_was_announced(
    client: TestClient, store: GameStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = arenagame_bytes(tmp_path)
    monkeypatch.setattr(games_api, "max_archive_bytes", lambda: 100)
    assert len(data) > 100
    announced = post_import(client, data)
    assert announced.status_code == 413 and announced.json()["code"] == "too_large" and "ARENA_MAX_ARCHIVE_BYTES" in announced.json()["detail"]

    def chunks() -> Iterator[bytes]:  # (no Content-Length: the limit is enforced while the body arrives)
        for index in range(0, len(data), 64):
            yield data[index : index + 64]

    streamed = post_import(client, chunks())
    assert streamed.status_code == 413 and streamed.json()["code"] == "too_large"
    assert store.list_games() == [] and tmp_files(store) == []


def test_a_game_cannot_be_added_while_an_archive_is_being_exported_or_imported(client: TestClient, store: GameStore, tmp_path: Path) -> None:
    data = arenagame_bytes(tmp_path)
    with get_archive_manager().exclusive("export"):
        response = post_import(client, data)
    assert response.status_code == 409 and response.json()["code"] == "busy"
    assert store.list_games() == []
    assert post_import(client, data).status_code == 200, "(the slot is free again as soon as the other operation is over)"


def test_the_slot_is_released_when_an_import_fails(client: TestClient, store: GameStore) -> None:
    assert post_import(client, b"junk").status_code == 422
    manager = get_archive_manager()
    with manager.exclusive("again"):
        pass  # (would raise ArchiveBusyError if the failed import had kept the slot)
