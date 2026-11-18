"""``GameStore`` (v0.13, F21 / F22 / F38; contract section 11.3): plain-JSON games, a previous good copy, a rebuildable index."""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

from app.storage.errors import StorageError
from app.storage.games import GAME_FILE, INDEX_FILE, PREVIOUS_FILE, UNREADABLE_STATUS, GameStore, get_game_store, reset_game_store
from app.storage.paths import get_data_dir

SUMMARY_KEYS = {
    "session_id", "title", "scenario_name", "scenario_title", "status", "winner", "created_at", "started_at",
    "finished_at", "updated_at", "duration_gt", "players", "tokens", "cost_usd", "human_involved", "schema_version",
    "size_bytes",
}


def make(session_id: str = "g1", **changes: Any) -> dict[str, Any]:
    """A small but complete v5 game document."""
    game: dict[str, Any] = {
        "session_id": session_id,
        "schema_version": 5,
        "title": "Arena-Agents",
        "scenario_name": "werewolf_12p",
        "scenario_title": "狼人杀 12 人局",
        "status": "paused",
        "participants": [{"id": f"N{i}", "name": f"玩家{i}"} for i in range(1, 13)],
        "created_at": "2026-11-18T08:00:00+00:00",
        "started_at": "2026-11-18T08:01:00+00:00",
        "finished_at": None,
        "human_spans": [],
        "takeover": {"seat": None},
        "world": {"format": 1, "clock_now": 321.0987, "state": {"winner": None}, "ledger": []},
        "usage_stats": {"total_tokens": 1234, "total_cost_usd": 0.5},
    }
    game.update(changes)
    return game


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def store(root: Path) -> GameStore:
    return GameStore(root)


def game_dir(root: Path, session_id: str = "g1") -> Path:
    return root / "games" / session_id


def set_mtime(path: Path, seconds: int) -> None:
    os.utime(path, (seconds, seconds))


# ------------------------------------------------------------------------------------- the files
def test_a_game_is_plain_readable_json_and_the_previous_good_copy_is_kept(store: GameStore, root: Path) -> None:
    first = make(title="第一版", notes="你好 alpha")
    second = make(title="第二版", notes="你好 beta")

    store.save("g1", first)
    current = game_dir(root) / GAME_FILE
    raw = current.read_bytes()
    assert "你好 alpha".encode("utf-8") in raw  # not escaped (ensure_ascii off), not encrypted
    assert b"\n" not in raw and b'": ' not in raw  # not indented
    assert json.loads(raw.decode("utf-8")) == first
    assert not (game_dir(root) / PREVIOUS_FILE).exists()  # nothing to keep yet

    store.save("g1", second)
    assert json.loads((game_dir(root) / PREVIOUS_FILE).read_text(encoding="utf-8")) == first
    assert store.load("g1") == second

    current.write_bytes(b"{corrupt")
    assert store.load("g1") == first  # a damaged game.json costs the last save, not the game


def test_a_save_of_identical_content_rewrites_nothing_and_keeps_the_older_copy(store: GameStore, root: Path) -> None:
    store.save("g1", make(title="A"))
    store.save("g1", make(title="B"))
    previous = (game_dir(root) / PREVIOUS_FILE).read_bytes()
    before = (game_dir(root) / GAME_FILE).stat()

    store.save("g1", make(title="B"))

    assert (game_dir(root) / PREVIOUS_FILE).read_bytes() == previous  # still "A": B was already the current copy
    after = (game_dir(root) / GAME_FILE).stat()
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)


def test_a_damaged_current_file_never_replaces_the_previous_good_copy(store: GameStore, root: Path) -> None:
    store.save("g1", make(version=1))
    store.save("g1", make(version=2))
    current, previous = game_dir(root) / GAME_FILE, game_dir(root) / PREVIOUS_FILE
    good_previous = previous.read_bytes()

    current.write_bytes(b"broken")
    assert store.load("g1")["version"] == 1
    store.save("g1", make(version=3))  # the broken file is NOT rotated into the previous copy
    assert previous.read_bytes() == good_previous
    assert store.load("g1")["version"] == 3

    current.write_bytes(b"broken-again")
    previous.write_bytes(b"broken-too")
    with pytest.raises(StorageError, match="都无法读取"):
        store.load("g1")
    assert current.read_bytes() == b"broken-again" and previous.read_bytes() == b"broken-too"  # nothing was repaired or erased


def test_json_that_is_not_an_object_counts_as_damaged(store: GameStore, root: Path) -> None:
    store.save("g1", make(version=1))
    store.save("g1", make(version=2))
    (game_dir(root) / GAME_FILE).write_text("[1, 2, 3]", encoding="utf-8")
    assert store.load("g1")["version"] == 1
    (game_dir(root) / GAME_FILE).write_bytes(b"\xff\xfe not utf-8")
    assert store.load("g1")["version"] == 1


def test_missing_games(store: GameStore) -> None:
    assert store.list_games() == [] and store.exists("nope") is False
    with pytest.raises(StorageError, match="不存在"):
        store.load("nope")
    store.delete("nope")  # idempotent


def test_ids_that_could_escape_the_folder_or_name_a_device_are_refused(store: GameStore, root: Path) -> None:
    for invalid in ("../escape", "a/b", "a\\b", ".", "..", "CON", "lpt1", "x" * 65, "", "a.b", "index.json"):
        for call in (lambda s: store.save(s, {}), store.load, store.exists, store.delete):
            with pytest.raises(ValueError):
                call(invalid)
    assert not (root / "games").exists() and not (root / "escape").exists()


def test_what_is_not_a_json_object_is_refused_and_nothing_is_written(store: GameStore, root: Path) -> None:
    store.save("g1", make(version=1))
    before = (game_dir(root) / GAME_FILE).read_bytes()
    with pytest.raises(TypeError):
        store.save("g1", ["not", "a", "dict"])  # type: ignore[arg-type]
    with pytest.raises(StorageError):
        store.save("g1", make(bad=float("nan")))
    with pytest.raises(StorageError):
        store.save("g1", make(bad=object()))
    assert (game_dir(root) / GAME_FILE).read_bytes() == before
    assert not (game_dir(root) / PREVIOUS_FILE).exists()


def test_no_temporary_files_are_left_behind(store: GameStore, root: Path) -> None:
    for version in range(5):
        store.save("g1", make(version=version))
    store.save("g2", make())
    leftovers = [p for p in (root / "games").rglob("*") if p.is_file() and p.suffix == ".tmp"]
    assert leftovers == []
    assert sorted(p.name for p in game_dir(root).iterdir()) == [GAME_FILE, PREVIOUS_FILE]


def test_concurrent_saves_leave_valid_files_and_a_valid_index(store: GameStore, root: Path) -> None:
    errors: list[BaseException] = []

    def worker(session_id: str) -> None:
        try:
            for version in range(15):
                store.save(session_id, make(session_id, version=version))
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"g{i}",)) for i in range(4)] + [threading.Thread(target=worker, args=("g0",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not errors
    assert sorted(item["session_id"] for item in store.list_games()) == ["g0", "g1", "g2", "g3"]
    for i in range(4):
        assert store.load(f"g{i}")["version"] == 14
    assert json.loads((root / "games" / INDEX_FILE).read_text(encoding="utf-8"))["schema_version"] == 1


# ------------------------------------------------------------------------------------- the summary
def test_the_summary_has_exactly_the_contract_fields(store: GameStore, root: Path) -> None:
    store.save("g1", make())
    (summary,) = store.list_games()
    assert set(summary) == SUMMARY_KEYS
    assert summary["session_id"] == "g1" and summary["title"] == "Arena-Agents"
    assert (summary["scenario_name"], summary["scenario_title"]) == ("werewolf_12p", "狼人杀 12 人局")
    assert (summary["status"], summary["winner"], summary["players"], summary["schema_version"]) == ("paused", None, 12, 5)
    assert (summary["created_at"], summary["started_at"], summary["finished_at"]) == ("2026-11-18T08:00:00+00:00", "2026-11-18T08:01:00+00:00", None)
    assert summary["duration_gt"] == 321.099 and summary["tokens"] == 1234 and summary["cost_usd"] == 0.5
    assert summary["human_involved"] is False
    assert summary["size_bytes"] == (game_dir(root) / GAME_FILE).stat().st_size
    assert summary["updated_at"].endswith("+00:00")  # the file's time, UTC
    json.dumps(summary)  # all JSON


def test_the_summary_reads_the_game_the_way_the_history_list_needs_it() -> None:
    summarize = GameStore.summarize
    running = summarize(make(status="running"))
    assert running["status"] == "paused"  # the process ended mid-game: it comes back paused
    assert summarize(make(status="idle"))["status"] == "idle" and summarize(make(status="finished"))["status"] == "finished"
    won = summarize(make(status="finished", world={"clock_now": 99.5, "state": {"winner": "好人阵营"}}, finished_at="2026-11-18T09:00:00+00:00"))
    assert (won["winner"], won["duration_gt"], won["finished_at"]) == ("好人阵营", 99.5, "2026-11-18T09:00:00+00:00")
    human = summarize(make(human_spans=[{"seat": "N3", "from_seq": 0, "to_seq": None}]))
    assert human["human_involved"] is True
    parts = summarize(make(usage_stats={"prompt_tokens": 10, "cache_read_tokens": 20, "cache_write_tokens": 30, "completion_tokens": 40}))
    assert parts["tokens"] == 100  # without the computed total the four counters add up
    given = summarize(make(), size_bytes=77, updated_at="2026-01-01T00:00:00+00:00")
    assert (given["size_bytes"], given["updated_at"]) == (77, "2026-01-01T00:00:00+00:00")


def test_a_game_of_another_version_or_a_partial_one_still_has_a_summary() -> None:
    old = GameStore.summarize({"session_id": "old", "schema_version": 4, "status": "paused", "records": [{"id": 1}]})
    assert set(old) == SUMMARY_KEYS and old["schema_version"] == 4 and old["players"] == 0 and old["tokens"] == 0
    empty = GameStore.summarize({})
    assert set(empty) == SUMMARY_KEYS and empty["schema_version"] is None and empty["status"] == "paused"
    junk = GameStore.summarize({"world": "x", "usage_stats": [1], "participants": "nope", "human_spans": None, "schema_version": True})
    assert junk["players"] == 0 and junk["schema_version"] is None and junk["human_involved"] is False


def test_a_real_game_of_the_orchestrator_has_a_summary(monkeypatch: pytest.MonkeyPatch, store: GameStore) -> None:
    from game_fakes import real_orch

    payload = real_orch(monkeypatch).session_payload()
    store.save(payload["session_id"], payload)
    (summary,) = store.list_games()
    assert set(summary) == SUMMARY_KEYS
    assert summary["status"] == "idle" and summary["players"] == 4 and summary["schema_version"] == 5
    assert summary["scenario_name"] == "minimal_test" and summary["created_at"]


# ------------------------------------------------------------------------------------- the index
def test_the_index_is_updated_by_every_save_and_lists_the_newest_first(store: GameStore, root: Path) -> None:
    store.save("old", make("old"))
    store.save("mid", make("mid"))
    store.save("new", make("new"))
    set_mtime(game_dir(root, "old") / GAME_FILE, 1_700_000_000)
    set_mtime(game_dir(root, "mid") / GAME_FILE, 1_700_100_000)
    set_mtime(game_dir(root, "new") / GAME_FILE, 1_700_200_000)

    index = json.loads((root / "games" / INDEX_FILE).read_text(encoding="utf-8"))
    assert index["schema_version"] == 1 and {item["session_id"] for item in index["games"]} == {"old", "mid", "new"}
    # the files got new times: the list notices (the index no longer matches) and rebuilds itself
    assert [item["session_id"] for item in store.list_games()] == ["new", "mid", "old"]
    assert [item["session_id"] for item in store.list_games()] == ["new", "mid", "old"]  # (and the rebuilt index is stable)

    store.save("old", make("old", title="再次保存"))  # saved again: the newest
    assert store.list_games()[0]["session_id"] == "old" and store.list_games()[0]["title"] == "再次保存"


def test_a_missing_or_damaged_index_is_rebuilt_from_the_game_files(store: GameStore, root: Path) -> None:
    store.save("a", make("a", title="甲"))
    store.save("b", make("b", title="乙"))
    expected = store.list_games()
    index = root / "games" / INDEX_FILE

    index.unlink()
    assert store.list_games() == expected and index.is_file()  # rebuilt and written again

    for damage in (b"not json", b"[]", b'{"schema_version": 99, "games": []}', b'{"schema_version": 1, "games": "x"}',
                   b'{"schema_version": 1, "games": [{"title": "no id"}]}'):
        index.write_bytes(damage)
        assert store.list_games() == expected
        assert json.loads(index.read_text(encoding="utf-8"))["games"], damage


def test_an_index_that_does_not_match_the_folders_is_rebuilt(store: GameStore, root: Path) -> None:
    store.save("a", make("a"))
    store.save("b", make("b"))

    shutil.rmtree(game_dir(root, "b"))  # a game folder disappeared behind the index's back
    assert [item["session_id"] for item in store.list_games()] == ["a"]

    (root / "games" / "c").mkdir()
    (root / "games" / "c" / GAME_FILE).write_text(json.dumps(make("c", title="手工放进来的")), encoding="utf-8")  # one appeared
    assert {item["session_id"]: item["title"] for item in store.list_games()} == {"a": "Arena-Agents", "c": "手工放进来的"}

    (root / "games" / "c" / GAME_FILE).write_text(json.dumps(make("c", title="换了内容")), encoding="utf-8")  # same id, other content
    assert {item["session_id"]: item["title"] for item in store.list_games()}["c"] == "换了内容"


def test_a_game_file_replaced_outside_the_app_changes_its_summary(store: GameStore, root: Path) -> None:
    store.save("a", make("a", status="paused"))
    assert store.list_games()[0]["status"] == "paused"
    (game_dir(root, "a") / GAME_FILE).write_text(json.dumps(make("a", status="finished", world={"clock_now": 5, "state": {"winner": "狼人阵营"}})), encoding="utf-8")
    (summary,) = store.list_games()
    assert (summary["status"], summary["winner"]) == ("finished", "狼人阵营")


def test_the_index_keeps_working_when_the_game_file_is_damaged_but_the_previous_copy_is_good(store: GameStore, root: Path) -> None:
    store.save("a", make("a", title="好的"))
    store.save("a", make("a", title="更新的"))
    (game_dir(root, "a") / GAME_FILE).write_bytes(b"{broken")
    (summary,) = store.list_games()
    assert summary["title"] == "好的" and summary["status"] != UNREADABLE_STATUS  # described by the previous copy
    assert store.load("a")["title"] == "好的"


def test_unreadable_games_are_listed_so_they_can_be_deleted(store: GameStore, root: Path) -> None:
    store.save("good", make("good"))
    (root / "games" / "bad").mkdir()
    (root / "games" / "bad" / GAME_FILE).write_bytes(b"\x00\x01 not json")
    summaries = {item["session_id"]: item for item in store.list_games()}
    assert summaries["bad"]["status"] == UNREADABLE_STATUS and summaries["bad"]["schema_version"] is None
    assert summaries["good"]["status"] == "paused"
    with pytest.raises(StorageError):
        store.load("bad")
    store.delete("bad")
    assert [item["session_id"] for item in store.list_games()] == ["good"]


def test_a_game_of_an_older_engine_is_listed_with_its_schema_version(store: GameStore) -> None:
    store.save("v4", {"session_id": "v4", "schema_version": 4, "status": "paused", "records": []})
    (summary,) = store.list_games()
    assert summary["schema_version"] == 4 and summary["status"] == "paused"


def test_folders_that_are_not_games_are_ignored(store: GameStore, root: Path) -> None:
    store.save("ok", make("ok"))
    (root / "games" / "not valid!").mkdir()
    (root / "games" / "not valid!" / GAME_FILE).write_text("{}", encoding="utf-8")
    (root / "games" / "empty").mkdir()  # no game file inside
    (root / "games" / "stray.txt").write_text("x", encoding="utf-8")
    assert [item["session_id"] for item in store.list_games()] == ["ok"]


def test_the_summary_names_the_folder_even_when_the_file_says_otherwise(store: GameStore, root: Path) -> None:
    (root / "games" / "folder").mkdir(parents=True)
    (root / "games" / "folder" / GAME_FILE).write_text(json.dumps(make("someone-else")), encoding="utf-8")
    assert [item["session_id"] for item in store.list_games()] == ["folder"]


# ------------------------------------------------------------------------------------- deleting
def test_delete_removes_the_game_its_logs_and_its_index_entry_and_nothing_else(store: GameStore, root: Path) -> None:
    store.save("keep", make("keep"))
    store.save("gone", make("gone"))
    for session_id in ("keep", "gone"):
        logs = root / "logs" / session_id
        logs.mkdir(parents=True)
        (logs / "trace-0001.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "roster").mkdir()
    (root / "roster" / "roster.json").write_text("{}", encoding="utf-8")
    (root / "settings.json").write_text("{}", encoding="utf-8")

    store.delete("gone")

    assert not game_dir(root, "gone").exists() and not (root / "logs" / "gone").exists()
    assert (root / "logs" / "keep" / "trace-0001.jsonl").is_file() and store.load("keep")["session_id"] == "keep"
    assert (root / "roster" / "roster.json").read_text(encoding="utf-8") == "{}"  # the roster is not touched
    assert (root / "settings.json").is_file()
    assert [item["session_id"] for item in json.loads((root / "games" / INDEX_FILE).read_text(encoding="utf-8"))["games"]] == ["keep"]
    assert store.exists("gone") is False and store.exists("keep") is True
    store.delete("gone")  # again: nothing to do


def test_a_game_that_was_deleted_and_saved_again_comes_back_clean(store: GameStore, root: Path) -> None:
    store.save("g", make("g", version=1))
    store.save("g", make("g", version=2))
    store.delete("g")
    store.save("g", make("g", version=3))
    assert store.load("g")["version"] == 3 and not (game_dir(root, "g") / PREVIOUS_FILE).exists()


# ------------------------------------------------------------------------------------- symbolic links
def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not permitted here")


def test_a_symlinked_game_folder_is_never_followed(store: GameStore, root: Path, tmp_path: Path) -> None:
    store.save("real", make("real"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / GAME_FILE).write_text(json.dumps(make("link")), encoding="utf-8")
    _symlink(root / "games" / "link", outside)

    for call in (lambda: store.save("link", make("link")), lambda: store.load("link"), lambda: store.exists("link"), lambda: store.delete("link")):
        with pytest.raises(StorageError, match="符号链接"):
            call()
    assert [item["session_id"] for item in store.list_games()] == ["real"]  # skipped when listing
    assert (outside / GAME_FILE).is_file()  # nothing outside was touched or deleted


def test_a_symlinked_game_file_is_never_read_or_replaced(store: GameStore, root: Path, tmp_path: Path) -> None:
    store.save("g", make("g", version=1))
    store.save("g", make("g", version=2))
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"stolen": True}), encoding="utf-8")
    (game_dir(root, "g") / GAME_FILE).unlink()
    _symlink(game_dir(root, "g") / GAME_FILE, secret)

    assert store.load("g")["version"] == 1  # the link is skipped, the real previous copy is used
    with pytest.raises(StorageError):
        store.save("g", make("g", version=3))  # it is not replaced behind a link either
    assert json.loads(secret.read_text(encoding="utf-8")) == {"stolen": True}


def test_a_symlinked_games_folder_is_refused(root: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root.mkdir()
    _symlink(root / "games", elsewhere)
    store = GameStore(root)
    for call in (lambda: store.save("g", make()), lambda: store.load("g"), store.list_games, store.rebuild_index):
        with pytest.raises(StorageError, match="符号链接"):
            call()
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("name", ["games", "link", GAME_FILE])
def test_the_symlink_guards_hold_even_where_links_cannot_be_created(store: GameStore, root: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """The three real-link tests above are skipped without the privilege (Windows); this drives the same guards with
    a pretended link."""
    store.save("link", make("link", version=1))
    store.save("link", make("link", version=2))
    real = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == name or real(self))
    if name == GAME_FILE:
        assert store.load("link")["version"] == 1  # the "linked" current file is skipped: the previous copy is used
        with pytest.raises(StorageError):
            store.save("link", make("link", version=3))
        return
    for call in (lambda: store.save("link", make("link")), lambda: store.load("link"), lambda: store.exists("link"), lambda: store.delete("link")):
        with pytest.raises(StorageError, match="符号链接"):
            call()
    if name == "games":
        for call in (store.list_games, store.rebuild_index):
            with pytest.raises(StorageError, match="符号链接"):
                call()
    else:
        assert store.list_games() == []  # a linked folder is skipped when listing
    monkeypatch.setattr(Path, "is_symlink", real)
    assert store.load("link")["version"] == 2  # and nothing was changed on the way


# ------------------------------------------------------------------------------------- singletons
def test_the_module_level_store_follows_the_data_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.storage.games as games

    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setenv("ARENA_DATA_DIR", str(first))
    reset_game_store()
    store = get_game_store()
    assert store is get_game_store() and store.data_dir == first.resolve()
    games.save("g", make("g"))
    assert games.exists("g") and games.load("g")["session_id"] == "g" and [x["session_id"] for x in games.list_games()] == ["g"]

    monkeypatch.setenv("ARENA_DATA_DIR", str(second))
    assert get_game_store().data_dir == second.resolve() and games.exists("g") is False
    monkeypatch.setenv("ARENA_DATA_DIR", str(first))
    games.delete("g")
    assert games.list_games() == []
    assert get_data_dir() == first.resolve()


def test_looking_never_creates_anything(tmp_path: Path) -> None:
    root = tmp_path / "untouched"
    store = GameStore(root)
    assert store.list_games() == [] and store.rebuild_index() == [] and store.exists("g") is False
    store.delete("g")
    with pytest.raises(StorageError):
        store.load("g")
    assert not root.exists()
