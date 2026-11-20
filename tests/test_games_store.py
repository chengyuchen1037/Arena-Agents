"""``GameStore`` in v0.14 (H40; contract 13.7): the new fields of the history list, the detail of a game, the good copy for an export and the
installation of a game that came from outside (an unpacked ``.arenagame``)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from app.game.session import SUPPORTED_SCHEMA_VERSIONS
from app.storage.errors import StorageError
from app.storage.games import (
    CONFLICT_POLICIES,
    DELETE_NOTE,
    GAME_FILE,
    INDEX_FILE,
    PREVIOUS_FILE,
    READABLE_SCHEMA_VERSIONS,
    GameExistsError,
    GameNotFoundError,
    GameProtectedError,
    GameStore,
    InstalledGame,
    is_log_name,
)

ROLES = {
    "werewolf": {"faction": "wolves", "tags": ["狼人", "存活"]},
    "seer": {"faction": "good", "tags": ["预言家", "存活"]},
    "villager": {"faction": "good", "tags": ["平民", "存活"]},
    "nameless": {"faction": "good", "tags": []},
}
FACTION_NAMES = {"wolves": "狼人阵营", "good": "好人阵营"}
SEATS = [
    ("N1", "werewolf", "wolves"),
    ("N2", "seer", "good"),
    ("N3", "villager", "good"),
    ("N4", "nameless", "good"),
]


def make(session_id: str = "g1", *, schema: int = 6, status: str = "paused", seats: int = 4, **changes: Any) -> dict[str, Any]:
    """A small game document with the parts the summary and the detail read."""
    participants = [
        {"id": f"N{i}", "name": f"玩家{i}", "bot_id": f"b-{i:08x}" if schema >= 6 else "", "avatar": {"v": 1, "face": f"f{i:02d}"}}
        for i in range(1, seats + 1)
    ]
    players = [{"id": seat, "seat": seat[1:], "name": f"玩家{seat[1:]}", "role": role, "faction": faction} for seat, role, faction in SEATS][:seats]
    game: dict[str, Any] = {
        "session_id": session_id,
        "schema_version": schema,
        "title": "Arena-Agents",
        "scenario_name": "werewolf_12p",
        "scenario_title": "狼人杀",
        "status": status,
        "participants": participants,
        "created_at": "2026-11-18T08:00:00+00:00",
        "started_at": "2026-11-18T08:01:00+00:00",
        "finished_at": None,
        "review_done": [],
        "human_spans": [],
        "takeover": {"seat": None},
        "scenario_spec": {"setup": {"roles": ROLES, "faction_names": FACTION_NAMES}},
        "world": {"format": 1, "clock_now": 12.0, "state": {"winner": None, "players": players}, "ledger": []},
        "usage_stats": {"total_tokens": 10, "total_cost_usd": 0.1},
    }
    game.update(changes)
    return game


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def store(root: Path) -> GameStore:
    return GameStore(root)


def write_logs(root: Path, session_id: str, files: dict[str, bytes]) -> Path:
    folder = root / "logs" / session_id
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    return folder


def tree(path: Path) -> dict[str, bytes]:
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------------------------------------------- the constants
def test_the_readable_formats_are_the_ones_the_session_opens_and_the_note_is_the_contracts() -> None:
    assert READABLE_SCHEMA_VERSIONS == tuple(SUPPORTED_SCHEMA_VERSIONS) == (5, 6)
    assert DELETE_NOTE == "bot 已经通过复盘形成的记忆不会被删除，也无法通过删除对局撤销。"
    assert CONFLICT_POLICIES == ("ask", "overwrite", "copy")


@pytest.mark.parametrize(
    "name, expected",
    [
        ("trace-0001.jsonl", True),
        ("trace-0002.jsonl", True),
        ("trace-12345.jsonl", True),
        ("trace-001.jsonl", False),
        ("trace-000a.jsonl", False),
        ("trace-0001.jsonl.bak", False),
        ("Trace-0001.jsonl", False),
        ("trace-.jsonl", False),
        ("trace-0001.json", False),
        ("trace-０００１.jsonl", False),  # (full-width digits)
        ("notes.txt", False),
        ("", False),
        (None, False),
        (1, False),
    ],
)
def test_the_names_of_a_games_log_files(name: object, expected: bool) -> None:
    assert is_log_name(name) is expected


# ------------------------------------------------------------------------------------------------------ the summary
@pytest.mark.parametrize("schema, readable", [(5, True), (6, True), (4, False), (7, False), (0, False), (None, False), (True, False), ("6", False)])
def test_a_game_is_readable_when_its_save_format_is_one_this_version_opens(schema: object, readable: bool) -> None:
    assert GameStore.summarize(make(schema_version=schema))["readable"] is readable


def test_a_v6_game_says_how_far_its_reviews_are() -> None:
    summary = GameStore.summarize(make(status="finished", review_done=["N1", "N3"]))
    assert summary["review"] == {"done": 2, "total": 4}
    assert GameStore.summarize(make())["review"] == {"done": 0, "total": 4}
    full = GameStore.summarize(make(review_done=["N1", "N2", "N3", "N4"]))
    assert full["review"] == {"done": 4, "total": 4}


def test_the_review_count_is_tolerant() -> None:
    assert GameStore.summarize(make(review_done=["N1", "N1", "N1"]))["review"]["done"] == 1  # (a seat counts once)
    assert GameStore.summarize(make(review_done=["N1", 7, None, {"x": 1}, "N2"]))["review"]["done"] == 2
    assert GameStore.summarize(make(review_done=list("abcdefgh")))["review"] == {"done": 4, "total": 4}  # (never more than the table)
    assert GameStore.summarize(make(review_done="N1"))["review"]["done"] == 0
    assert GameStore.summarize(make(review_done=None))["review"]["done"] == 0


def test_a_v5_game_cannot_be_reviewed_any_more() -> None:
    """A game of v0.13 stays a v5 game (H58): there is no "total" to reach."""
    summary = GameStore.summarize(make(schema=5, review_done=["N1", "N2", "N3"]))
    assert summary["review"] == {"done": 3, "total": None} and summary["readable"] is True


def test_an_empty_table_has_no_total() -> None:
    assert GameStore.summarize(make(seats=0))["review"] == {"done": 0, "total": None}


def test_the_names_at_the_table() -> None:
    assert GameStore.summarize(make())["participants_brief"] == ["玩家1", "玩家2", "玩家3", "玩家4"]
    brief = GameStore.summarize(make(participants=[{"id": "N1", "name": "A"}, "junk", {"id": "N2"}, {"name": 5}, None]))["participants_brief"]
    assert brief == ["A", "", ""]
    assert GameStore.summarize(make(participants="nope"))["participants_brief"] == []
    assert GameStore.summarize({})["participants_brief"] == []


def test_a_summary_is_still_all_json() -> None:
    json.dumps(GameStore.summarize(make(review_done=["N1"])))


# ---------------------------------------------------------------------------------------------------- the listing
def test_the_listing_reads_the_size_of_the_logs_from_the_disk_every_time(store: GameStore, root: Path) -> None:
    store.save("g1", make("g1"))
    store.save("g2", make("g2"))
    assert {item["session_id"]: item["log_bytes"] for item in store.list_games()} == {"g1": 0, "g2": 0}
    folder = root / "logs" / "g1"
    folder.mkdir(parents=True)
    (folder / "trace-0001.jsonl").write_bytes(b"x" * 100)
    (folder / "trace-0002.jsonl").write_bytes(b"y" * 23)
    (folder / "sub").mkdir()
    (folder / "sub" / "deep.txt").write_bytes(b"z" * 999)  # (not a file of the folder itself)
    assert {item["session_id"]: item["log_bytes"] for item in store.list_games()} == {"g1": 123, "g2": 0}
    (folder / "trace-0001.jsonl").write_bytes(b"x" * 1000)  # (the log grows while a game is played: no stale number)
    assert {item["session_id"]: item["log_bytes"] for item in store.list_games()}["g1"] == 1023
    index = json.loads((root / "games" / INDEX_FILE).read_text(encoding="utf-8"))
    assert all("log_bytes" not in item for item in index["games"]), "the index is a cache of the game files; the logs are not part of it"


def test_the_listing_has_the_new_fields_for_games_of_both_formats(store: GameStore) -> None:
    store.save("v5", make("v5", schema=5, status="finished", review_done=["N1"]))
    store.save("v6", make("v6", status="finished", review_done=["N1", "N2"]))
    store.save("v4", make("v4", schema=4))
    games = {item["session_id"]: item for item in store.list_games()}
    assert (games["v5"]["readable"], games["v5"]["review"]) == (True, {"done": 1, "total": None})
    assert (games["v6"]["readable"], games["v6"]["review"]) == (True, {"done": 2, "total": 4})
    assert games["v4"]["readable"] is False
    assert all(item["participants_brief"] == ["玩家1", "玩家2", "玩家3", "玩家4"] for item in games.values())


def test_a_game_that_cannot_be_read_at_all_is_listed_as_unreadable(store: GameStore, root: Path) -> None:
    (root / "games" / "broken").mkdir(parents=True)
    (root / "games" / "broken" / GAME_FILE).write_bytes(b"\xff not json")
    (item,) = store.list_games()
    assert item["status"] == "unreadable" and item["readable"] is False and item["review"] == {"done": 0, "total": None}
    assert item["participants_brief"] == [] and item["log_bytes"] == 0


def test_log_bytes_of_a_game_without_a_folder_or_with_a_bad_id(store: GameStore) -> None:
    assert store.log_bytes("nothing-here") == 0
    with pytest.raises(ValueError):
        store.log_bytes("../x")


# ---------------------------------------------------------------------------------------------------- the detail
def detail(store: GameStore, game: dict[str, Any]) -> dict[str, Any]:
    return store.detail_of(game, game["session_id"])


@pytest.mark.parametrize("status", ["idle", "paused", "running", "", "whatever"])
def test_nobody_learns_an_identity_from_a_game_that_is_not_finished(store: GameStore, status: str) -> None:
    body = detail(store, make(status=status))
    assert [(p["seat"], p["role"], p["faction"]) for p in body["participants"]] == [(f"N{i}", None, None) for i in range(1, 5)]
    assert "狼人" not in json.dumps(body, ensure_ascii=False) and "wolves" not in json.dumps(body)


def test_a_finished_game_shows_who_was_what_in_the_scenarios_own_words(store: GameStore) -> None:
    body = detail(store, make(status="finished"))
    rows = [(p["seat"], p["name"], p["role"], p["faction"]) for p in body["participants"]]
    assert rows == [
        ("N1", "玩家1", "狼人", "狼人阵营"),  # (the first tag of the role, the name of the faction)
        ("N2", "玩家2", "预言家", "好人阵营"),
        ("N3", "玩家3", "平民", "好人阵营"),
        ("N4", "玩家4", "nameless", "好人阵营"),  # (a role without tags: its id)
    ]


def test_without_the_scenarios_words_the_ids_are_shown(store: GameStore) -> None:
    game = make(status="finished", scenario_spec={})
    assert [(p["role"], p["faction"]) for p in detail(store, game)["participants"]][:2] == [("werewolf", "wolves"), ("seer", "good")]


def test_a_seat_that_has_no_player_in_the_world_has_no_identity(store: GameStore) -> None:
    game = make(status="finished")
    game["world"]["state"]["players"] = game["world"]["state"]["players"][:2]
    assert [p["role"] for p in detail(store, game)["participants"]] == ["狼人", "预言家", None, None]


def test_the_review_of_each_seat(store: GameStore) -> None:
    body = detail(store, make(status="finished", review_done=["N1", "N3"]))
    assert [p["review"] for p in body["participants"]] == ["done", "pending", "done", "pending"]
    legacy = detail(store, make(schema=5, status="finished", review_done=["N2"]))
    assert [p["review"] for p in legacy["participants"]] == ["na", "done", "na", "na"], "a v5 game cannot be reviewed: only what was done is shown"
    game = make(status="finished")
    game["participants"][2]["bot_id"] = ""
    assert [p["review"] for p in detail(store, game)["participants"]] == ["pending", "pending", "na", "pending"]


def test_the_bot_and_the_avatar_of_each_seat(store: GameStore) -> None:
    body = detail(store, make())
    first = body["participants"][0]
    assert first["bot_id"] == "b-00000001" and first["avatar"] == {"v": 1, "face": "f01"} and first["name"] == "玩家1"
    assert detail(store, make(schema=5))["participants"][0]["bot_id"] == ""
    game = make()
    game["participants"][1]["avatar"] = None
    game["participants"][2]["avatar"] = {}
    game["participants"][3]["avatar"] = "nope"
    assert [p["avatar"] for p in detail(store, game)["participants"]][1:] == [None, None, None]


def test_who_played_a_seat(store: GameStore) -> None:
    spans = [
        {"seat": "N3", "from_seq": 24, "to_seq": 265},
        {"seat": "N3", "from_seq": 431.0, "to_seq": None},
        {"seat": "N2", "from_seq": "x", "to_seq": True},
        "junk",
        {"from_seq": 1},
    ]
    assert detail(store, make(human_spans=spans))["human_spans"] == [
        {"seat": "N3", "from_seq": 24, "to_seq": 265},
        {"seat": "N3", "from_seq": 431, "to_seq": None},
        {"seat": "N2", "from_seq": 0, "to_seq": None},
    ]
    assert detail(store, make())["human_spans"] == []


def test_the_detail_tolerates_a_game_that_is_mostly_missing(store: GameStore) -> None:
    body = store.detail_of({}, "g1")
    assert body == {"participants": [], "files": {"game_bytes": 0, "prev_bytes": 0, "log_bytes": 0}, "human_spans": []}
    junk = {"participants": [None, "x", {"id": "N1"}], "world": "nope", "scenario_spec": [], "status": "finished", "human_spans": "no"}
    assert store.detail_of(junk, "g1")["participants"] == [
        {"seat": "N1", "bot_id": "", "name": "", "avatar": None, "role": None, "faction": None, "review": "na"}
    ]


def test_the_sizes_of_the_files_of_a_game(store: GameStore, root: Path) -> None:
    store.save("g1", make("g1", title="一"))
    store.save("g1", make("g1", title="二二"))  # (the first copy became the previous one)
    write_logs(root, "g1", {"trace-0001.jsonl": b"a" * 50, "trace-0002.jsonl": b"b" * 7})
    body = store.detail("g1")
    assert body["files"] == {
        "game_bytes": (root / "games" / "g1" / GAME_FILE).stat().st_size,
        "prev_bytes": (root / "games" / "g1" / PREVIOUS_FILE).stat().st_size,
        "log_bytes": 57,
    }
    assert body["files"]["game_bytes"] > 0 and body["files"]["prev_bytes"] > 0
    store.save("g2", make("g2"))
    assert store.detail("g2")["files"]["prev_bytes"] == 0 and store.detail("g2")["files"]["log_bytes"] == 0


def test_the_detail_of_a_saved_game_and_its_errors(store: GameStore, root: Path) -> None:
    store.save("g1", make("g1", status="finished"))
    assert store.detail("g1")["participants"][0]["role"] == "狼人"
    with pytest.raises(GameNotFoundError):
        store.detail("nope")
    (root / "games" / "bad").mkdir()
    (root / "games" / "bad" / GAME_FILE).write_bytes(b"not json")
    with pytest.raises(StorageError):
        store.detail("bad")
    with pytest.raises(ValueError):
        store.detail("../x")


def test_the_previous_copy_stands_in_for_a_damaged_game_file(store: GameStore, root: Path) -> None:
    store.save("g1", make("g1", status="finished"))
    store.save("g1", make("g1", status="finished", title="新"))
    (root / "games" / "g1" / GAME_FILE).write_bytes(b"\x00 damaged")
    assert store.detail("g1")["participants"][1]["role"] == "预言家"


# ------------------------------------------------------------------------------------------------------ export parts
def test_the_good_copy_and_the_logs_of_a_game(store: GameStore, root: Path) -> None:
    store.save("g1", make("g1", title="好"))
    write_logs(root, "g1", {"trace-0002.jsonl": b"b\n", "trace-0001.jsonl": b"a\n"})
    folder = root / "logs" / "g1"
    (folder / "notes.txt").write_bytes(b"not a log")
    (folder / "trace-1.jsonl").write_bytes(b"too few digits")
    (folder / "sub").mkdir()
    (folder / "sub" / "trace-0003.jsonl").write_bytes(b"in a folder")
    parts = store.export_parts("g1")
    assert json.loads(parts.game_bytes)["title"] == "好" and parts.used_previous is False and parts.session_id == "g1"
    assert [name for name, _ in parts.logs] == ["trace-0001.jsonl", "trace-0002.jsonl"]
    assert [path.read_bytes() for _, path in parts.logs] == [b"a\n", b"b\n"]
    assert parts.game_bytes == (root / "games" / "g1" / GAME_FILE).read_bytes()


def test_a_game_without_logs_has_none_to_export(store: GameStore) -> None:
    store.save("g1", make("g1"))
    assert store.export_parts("g1").logs == []


def test_a_damaged_game_file_is_exported_as_its_previous_copy(store: GameStore, root: Path) -> None:
    store.save("g1", make("g1", title="一"))
    store.save("g1", make("g1", title="二"))
    (root / "games" / "g1" / GAME_FILE).write_bytes(b"{ cut")
    parts = store.export_parts("g1")
    assert parts.used_previous is True and json.loads(parts.game_bytes)["title"] == "一"


def test_nothing_to_export(store: GameStore, root: Path) -> None:
    with pytest.raises(GameNotFoundError):
        store.export_parts("nope")
    (root / "games" / "bad").mkdir(parents=True)
    (root / "games" / "bad" / GAME_FILE).write_bytes(b"x")
    (root / "games" / "bad" / PREVIOUS_FILE).write_bytes(b"y")
    with pytest.raises(StorageError, match="都无法读取"):
        store.export_parts("bad")
    with pytest.raises(ValueError):
        store.export_parts("a/b")


# --------------------------------------------------------------------------------------------------- installing
@pytest.fixture()
def incoming(tmp_path: Path) -> tuple[Path, list[tuple[str, Path]]]:
    """An unpacked game: ``game.json`` and two logs, in a scratch folder."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    game = scratch / "game.json"
    game.write_bytes(json.dumps(make("incoming", title="来自别处"), ensure_ascii=False).encode("utf-8"))
    logs = []
    for name, data in (("trace-0001.jsonl", b"one\n"), ("trace-0002.jsonl", b"two\n")):
        (scratch / name).write_bytes(data)
        logs.append((name, scratch / name))
    return game, logs


def test_a_game_from_outside_is_installed_with_its_logs_and_listed(
    store: GameStore, root: Path, incoming: tuple[Path, list[tuple[str, Path]]]
) -> None:
    game, logs = incoming
    result = store.install_game("incoming", game, logs)
    assert result == InstalledGame("incoming", "paused", replaced=False, copied=False)
    assert store.load("incoming")["title"] == "来自别处"
    assert tree(root / "logs" / "incoming") == {"trace-0001.jsonl": b"one\n", "trace-0002.jsonl": b"two\n"}
    (item,) = store.list_games()
    assert (item["session_id"], item["title"], item["log_bytes"], item["readable"]) == ("incoming", "来自别处", 8, True)
    assert not [p for p in (root / "games").iterdir() if p.name.startswith(".")], "no scratch folder is left behind"
    stored = (root / "games" / "incoming" / GAME_FILE).read_bytes()
    assert b"\n" not in stored and "来自别处".encode("utf-8") in stored, "plain compact JSON like every saved game"


def test_an_installed_game_is_a_good_copy_the_next_save_keeps_as_the_previous_one(
    store: GameStore, root: Path, incoming: tuple[Path, list[tuple[str, Path]]]
) -> None:
    game, logs = incoming
    store.install_game("incoming", game, logs)
    installed = (root / "games" / "incoming" / GAME_FILE).read_bytes()
    store.save("incoming", make("incoming", title="之后"))
    assert (root / "games" / "incoming" / PREVIOUS_FILE).read_bytes() == installed


def test_a_game_without_logs_is_installed_without_a_log_folder(store: GameStore, root: Path, incoming: tuple[Path, list[tuple[str, Path]]]) -> None:
    game, _logs = incoming
    store.install_game("incoming", game)
    assert store.exists("incoming") and not (root / "logs" / "incoming").exists()


def test_a_taken_id_is_asked_about_and_nothing_changes(store: GameStore, root: Path, incoming: tuple[Path, list[tuple[str, Path]]]) -> None:
    game, logs = incoming
    store.save("incoming", make("incoming", title="本来的"))
    write_logs(root, "incoming", {"trace-0001.jsonl": b"mine\n"})
    before = tree(root)
    with pytest.raises(GameExistsError) as excinfo:
        store.install_game("incoming", game, logs)
    assert excinfo.value.session_id == "incoming" and "incoming" in str(excinfo.value)
    assert tree(root) == before
    with pytest.raises(GameExistsError):  # ("ask" is the default)
        store.install_game("incoming", game, logs, on_conflict="ask")


def test_overwriting_replaces_the_game_and_all_of_its_logs(store: GameStore, root: Path, incoming: tuple[Path, list[tuple[str, Path]]]) -> None:
    game, logs = incoming
    store.save("incoming", make("incoming", title="本来的"))
    store.save("incoming", make("incoming", title="本来的二"))  # (so there is a previous copy, too)
    write_logs(root, "incoming", {"trace-0001.jsonl": b"mine\n", "trace-0007.jsonl": b"mine too\n"})
    result = store.install_game("incoming", game, logs, on_conflict="overwrite")
    assert result == InstalledGame("incoming", "paused", replaced=True, copied=False)
    assert store.load("incoming")["title"] == "来自别处"
    assert tree(root / "logs" / "incoming") == {"trace-0001.jsonl": b"one\n", "trace-0002.jsonl": b"two\n"}, "the old logs are gone"
    assert not (root / "games" / "incoming" / PREVIOUS_FILE).exists(), "the old game's previous copy went with it"
    assert sorted(p.name for p in (root / "games").iterdir()) == ["incoming", "index.json"]


def test_overwriting_with_a_game_that_has_no_logs_removes_the_old_logs(
    store: GameStore, root: Path, incoming: tuple[Path, list[tuple[str, Path]]]
) -> None:
    game, _logs = incoming
    store.save("incoming", make("incoming"))
    write_logs(root, "incoming", {"trace-0001.jsonl": b"mine\n"})
    store.install_game("incoming", game, [], on_conflict="overwrite")
    assert not (root / "logs" / "incoming").exists()


def test_a_copy_keeps_both_and_the_copy_has_a_new_id_inside_too(store: GameStore, root: Path, incoming: tuple[Path, list[tuple[str, Path]]]) -> None:
    game, logs = incoming
    store.save("incoming", make("incoming", title="本来的"))
    write_logs(root, "incoming", {"trace-0001.jsonl": b"mine\n"})
    result = store.install_game("incoming", game, logs, on_conflict="copy")
    assert result.copied is True and result.replaced is False and result.session_id != "incoming"
    assert len(result.session_id) == 12 and all(c in "0123456789abcdef" for c in result.session_id)
    assert store.load("incoming")["title"] == "本来的" and (root / "logs" / "incoming" / "trace-0001.jsonl").read_bytes() == b"mine\n"
    copy = store.load(result.session_id)
    assert copy["title"] == "来自别处" and copy["session_id"] == result.session_id, "the folder is the identity: the file agrees with it"
    assert tree(root / "logs" / result.session_id) == {"trace-0001.jsonl": b"one\n", "trace-0002.jsonl": b"two\n"}
    assert sorted(item["session_id"] for item in store.list_games()) == sorted(["incoming", result.session_id])


def test_a_copy_of_a_game_whose_id_is_free_is_just_the_game(store: GameStore, incoming: tuple[Path, list[tuple[str, Path]]]) -> None:
    game, logs = incoming
    assert store.install_game("incoming", game, logs, on_conflict="copy") == InstalledGame("incoming", "paused", replaced=False, copied=False)
    assert store.install_game("incoming", game, logs, on_conflict="overwrite").replaced is True


def test_the_live_game_is_protected_even_when_it_was_never_saved(store: GameStore, root: Path, incoming: tuple[Path, list[tuple[str, Path]]]) -> None:
    game, logs = incoming
    assert not store.exists("incoming")
    with pytest.raises(GameExistsError):
        store.install_game("incoming", game, logs, protected=["incoming"])
    with pytest.raises(GameProtectedError, match="当前对局") as excinfo:
        store.install_game("incoming", game, logs, on_conflict="overwrite", protected=["incoming"])
    assert excinfo.value.session_id == "incoming"
    assert not (root / "games").exists() or not (root / "games" / "incoming").exists(), "nothing was installed"
    copy = store.install_game("incoming", game, logs, on_conflict="copy", protected=["incoming"])
    assert copy.copied is True and copy.session_id != "incoming" and not store.exists("incoming")
    other = store.install_game("elsewhere", game, logs, protected=["incoming"])  # (only the protected id is special)
    assert other.session_id == "elsewhere"


def test_a_protected_game_that_is_on_disk_is_not_overwritten_either(store: GameStore, root: Path, incoming: tuple[Path, list[tuple[str, Path]]]) -> None:
    game, logs = incoming
    store.save("incoming", make("incoming", title="正在玩的"))
    before = tree(root)
    with pytest.raises(GameProtectedError):
        store.install_game("incoming", game, logs, on_conflict="overwrite", protected=("incoming",))
    assert tree(root) == before


@pytest.mark.parametrize("failing_rename", [1, 2, 3, 4])
def test_a_failure_while_swapping_leaves_everything_as_it_was(
    store: GameStore, root: Path, incoming: tuple[Path, list[tuple[str, Path]]], monkeypatch: pytest.MonkeyPatch, failing_rename: int
) -> None:
    game, logs = incoming
    store.save("incoming", make("incoming", title="本来的"))
    write_logs(root, "incoming", {"trace-0001.jsonl": b"mine\n"})
    store.list_games()  # (the index is written)
    before = tree(root)
    real = GameStore._rename
    calls = {"n": 0}

    def flaky(source: Path, target: Path) -> None:
        calls["n"] += 1
        if calls["n"] == failing_rename:
            raise StorageError("cannot move this folder")
        real(source, target)

    monkeypatch.setattr(GameStore, "_rename", staticmethod(flaky))
    with pytest.raises(StorageError, match="cannot move"):
        store.install_game("incoming", game, logs, on_conflict="overwrite")
    monkeypatch.setattr(GameStore, "_rename", staticmethod(real))
    assert tree(root) == before, f"rename #{failing_rename} failed: the data directory is not as it was"
    assert not [p for p in (root / "games").iterdir() if p.name.startswith(".")]
    assert not [p for p in (root / "logs").iterdir() if p.name.startswith(".")]
    assert store.load("incoming")["title"] == "本来的"


def test_what_is_refused_changes_nothing(store: GameStore, root: Path, incoming: tuple[Path, list[tuple[str, Path]]], tmp_path: Path) -> None:
    game, logs = incoming
    with pytest.raises(StorageError, match="日志文件的名字"):
        store.install_game("incoming", game, [("notes.txt", logs[0][1])])
    with pytest.raises(StorageError, match="日志文件的名字"):
        store.install_game("incoming", game, [("../trace-0001.jsonl", logs[0][1])])
    with pytest.raises(ValueError):
        store.install_game("../escape", game, logs)
    with pytest.raises(ValueError):
        store.install_game("CON", game, logs)
    with pytest.raises(StorageError, match="保留"):
        store.install_game("learning", game, logs)  # (``logs/learning/`` is where the logs of the initializations and reviews live)
    with pytest.raises(ValueError, match="on_conflict"):
        store.install_game("incoming", game, logs, on_conflict="merge")
    not_json = tmp_path / "scratch" / "bad.json"
    not_json.write_bytes(b"not json")
    with pytest.raises(StorageError):
        store.install_game("incoming", not_json, logs)
    array = tmp_path / "scratch" / "array.json"
    array.write_bytes(b"[1, 2]")
    with pytest.raises(StorageError, match="JSON 对象"):
        store.install_game("incoming", array, logs)
    with pytest.raises(StorageError, match="无法读取"):
        store.install_game("incoming", tmp_path / "nothing.json", logs)
    assert not (root / "games").exists() and store.list_games() == [], "nothing was installed, not even a folder"


def test_an_install_is_safe_to_run_from_several_threads_at_once(store: GameStore, root: Path, incoming: tuple[Path, list[tuple[str, Path]]]) -> None:
    game, logs = incoming
    results: list[Any] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(store.install_game("incoming", game, logs, on_conflict="copy"))
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not errors
    ids = sorted(result.session_id for result in results)
    assert len(set(ids)) == 4 and sum(result.copied for result in results) == 3
    assert sorted(item["session_id"] for item in store.list_games()) == ids


@pytest.mark.skipif(os.name == "nt", reason="creating symbolic links needs a privilege on Windows")
def test_a_linked_logs_folder_is_not_followed(store: GameStore, root: Path, tmp_path: Path) -> None:  # pragma: no cover - POSIX only
    store.save("g1", make("g1"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "trace-0001.jsonl").write_bytes(b"not this game's")
    (root / "logs").mkdir()
    (root / "logs" / "g1").symlink_to(elsewhere, target_is_directory=True)
    assert store.log_bytes("g1") == 0 and store.export_parts("g1").logs == []
