""".arenagame: one saved game as a file (docs/v0.14-plan.md H40; contract 13.9) - the codec of ``app.archive.arenagame``.

A game and its logs go out and come in again; only save formats 5 and 6 are accepted; a game id that is taken is asked about (``ask``), overwritten
(``overwrite``, the game *and* its logs) or kept under a new id (``copy``); the live game is never overwritten.  Everything that is not exactly
that - a path that leaves the folder, a member the format does not know, a file changed after it was made, a zip bomb, a game that does not load -
is refused with a clear reason and leaves nothing behind.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import warnings
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.archive import arenabot, arenagame
from app.archive.errors import (
    ArchiveError,
    ArchiveExistsError,
    ArchiveFormatError,
    ArchiveIOError,
    ArchiveLimitError,
    ArchiveVersionError,
)
from app.archive.export import ExportOptions, export_archive
from app.archive.format import ArchiveLimits
from app.storage.errors import StorageError
from app.storage.games import GAME_FILE, PREVIOUS_FILE, GameNotFoundError, GameStore

from .helpers import dumps, game_payload, populate

NOW = datetime(2026, 11, 18, 0, 42, 7, tzinfo=timezone(timedelta(hours=-8)))
GAME = "aaaa1111aaaa"
OTHER = "bbbb2222bbbb"


def game_bytes(session_id: str = GAME, *, schema: int = 6, status: str = "paused", **changes: Any) -> bytes:
    payload = game_payload(session_id, status=status)
    payload["schema_version"] = schema
    payload.update(changes)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def logs_of(session_id: str, store_root: Path) -> dict[str, bytes]:
    folder = store_root / "logs" / session_id
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())} if folder.is_dir() else {}


def seed(root: Path, session_id: str = GAME, *, schema: int = 6, logs: dict[str, bytes] | None = None, **changes: Any) -> GameStore:
    """A game store in ``root`` holding one game, with logs."""
    store = GameStore(root)
    store.save(session_id, json.loads(game_bytes(session_id, schema=schema, **changes)))
    folder = root / "logs" / session_id
    folder.mkdir(parents=True, exist_ok=True)
    if logs is None:
        logs = {"trace-0001.jsonl": '{"event":"a","text":"你好"}\n'.encode("utf-8") * 40, "trace-0002.jsonl": b'{"event":"b"}\n'}
    for name, data in logs.items():
        (folder / name).write_bytes(data)
    return store


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build(
    target: Path,
    members: dict[str, bytes] | None = None,
    *,
    session_id: str = GAME,
    manifest: dict[str, Any] | Callable[[dict[str, Any]], None] | None = None,
    compress: int = zipfile.ZIP_DEFLATED,
    configure: Callable[[str, zipfile.ZipInfo], None] | None = None,
) -> Path:
    """A ``.arenagame`` with exactly ``members`` (member name -> bytes; default: the game ``GAME`` and one log) and a manifest that lists
    them; ``manifest`` and ``configure`` make it wrong in some way.  Member names are written exactly as given."""
    members = dict(members) if members is not None else {
        f"games/{session_id}/game.json": game_bytes(session_id),
        f"logs/{session_id}/trace-0001.jsonl": b'{"event":"a"}\n',
    }
    document: dict[str, Any] = {
        "format": "arenagame",
        "format_version": 1,
        "session_id": session_id,
        "created_at": "2026-11-18T00:42:00-08:00",
        "app_version": "v0.14.0",
        "files": [{"path": n, "size": len(d), "sha256": _sha(d)} for n, d in sorted(members.items())],
    }
    if callable(manifest):
        manifest(document)
    elif manifest:
        document.update(manifest)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(target, "w", compression=compress) as zf:
            for member, data in members.items():
                info = zipfile.ZipInfo("x", date_time=(2026, 11, 18, 0, 0, 0))
                info.filename = member
                info.compress_type = compress
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                if configure is not None:
                    configure(member, info)
                zf.writestr(info, data)
            zf.writestr("manifest.json", json.dumps(document, ensure_ascii=False))
    return target


def unpack(source: Path, tmp_path: Path, **kwargs: Any) -> arenagame.UnpackedGame:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return arenagame.unpack_arenagame(source, work, **kwargs)


# ------------------------------------------------------------------------------------------------------ round trips
@pytest.mark.parametrize("schema", [5, 6])
def test_a_game_and_its_logs_go_out_and_come_in_again(tmp_path: Path, schema: int) -> None:
    source = seed(tmp_path / "a", schema=schema)
    exported = arenagame.export_game(source, GAME, tmp_path / "out" / "g.arenagame", now=NOW)
    assert exported.path.is_file() and exported.session_id == GAME and exported.log_files == 2 and exported.used_previous is False
    assert exported.size_bytes == exported.path.stat().st_size and exported.warnings == []

    target = GameStore(tmp_path / "b")
    work = tmp_path / "work"
    work.mkdir()
    installed = arenagame.import_arenagame(target, exported.path, work)
    assert (installed.session_id, installed.replaced, installed.copied) == (GAME, False, False)
    assert (tmp_path / "b" / "games" / GAME / GAME_FILE).read_bytes() == (tmp_path / "a" / "games" / GAME / GAME_FILE).read_bytes()
    assert logs_of(GAME, tmp_path / "b") == logs_of(GAME, tmp_path / "a") and len(logs_of(GAME, tmp_path / "b")) == 2
    (item,) = target.list_games()
    assert (item["session_id"], item["schema_version"], item["readable"]) == (GAME, schema, True)


def test_the_file_is_exactly_the_manifest_the_game_and_its_logs(tmp_path: Path) -> None:
    source = seed(tmp_path / "a")
    source.save(GAME, json.loads(game_bytes(GAME, title="新")))  # (so there is a game.prev.json, which must not go along)
    (tmp_path / "a" / "logs" / GAME / "notes.txt").write_bytes(b"not a log")
    (tmp_path / "a" / "logs" / GAME / "trace-1.jsonl").write_bytes(b"too few digits")
    assert (tmp_path / "a" / "games" / GAME / PREVIOUS_FILE).is_file()
    target = tmp_path / "g.arenagame"
    arenagame.export_game(source, GAME, target, now=NOW)
    with zipfile.ZipFile(target) as zf:
        names = sorted(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
        members = {info.filename: zf.read(info) for info in zf.infolist()}
    assert names == sorted(["manifest.json", f"games/{GAME}/game.json", f"logs/{GAME}/trace-0001.jsonl", f"logs/{GAME}/trace-0002.jsonl"])
    assert set(manifest) == {"format", "format_version", "session_id", "created_at", "app_version", "files"}
    assert (manifest["format"], manifest["format_version"], manifest["session_id"]) == ("arenagame", 1, GAME)
    assert manifest["created_at"] == "2026-11-18T00:42:07-08:00" and manifest["app_version"].startswith("v")
    assert [item["path"] for item in manifest["files"]] == sorted(n for n in names if n != "manifest.json")
    for item in manifest["files"]:
        assert set(item) == {"path", "size", "sha256"} and item["size"] == len(members[item["path"]]) and item["sha256"] == _sha(members[item["path"]])
    assert json.loads(members[f"games/{GAME}/game.json"])["title"] == "新", "the good copy: the latest one"


def test_the_members_are_plain_regular_files_and_the_name_is_not_taken_before_the_file_is_complete(tmp_path: Path) -> None:
    source = seed(tmp_path / "a")
    target = tmp_path / "out" / "g.arenagame"
    arenagame.export_game(source, GAME, target)
    assert [p.name for p in target.parent.iterdir()] == ["g.arenagame"]
    with zipfile.ZipFile(target) as zf:
        for info in zf.infolist():
            assert info.compress_type == zipfile.ZIP_DEFLATED and stat.S_ISREG(info.external_attr >> 16) and not info.flag_bits & 1


def test_a_big_log_is_streamed_and_comes_back_whole(tmp_path: Path) -> None:
    # (lines like a real trace: each has something unique in it - a perfectly repetitive log would deflate ~1000:1, which the importer rightly
    # takes for a zip bomb)
    big = b"".join(
        json.dumps({"event": "tool_call", "n": i, "text": "x" * 100, "h": hashlib.sha256(str(i).encode()).hexdigest()}).encode("utf-8") + b"\n"
        for i in range(12_000)
    )
    assert len(big) > 2 * 1024 * 1024
    source = seed(tmp_path / "a", logs={"trace-0001.jsonl": big, "trace-0002.jsonl": b"tail\n"})
    target = tmp_path / "g.arenagame"
    arenagame.export_game(source, GAME, target)
    assert target.stat().st_size < len(big) // 2, "(the log is compressed)"
    unpacked = unpack(target, tmp_path)
    assert (unpacked.logs[0][1]).read_bytes() == big and unpacked.logs[1][1].read_bytes() == b"tail\n"
    assert [name for name, _ in unpacked.logs] == ["trace-0001.jsonl", "trace-0002.jsonl"]


def test_a_game_without_logs_round_trips(tmp_path: Path) -> None:
    source = seed(tmp_path / "a", logs={})
    target = tmp_path / "g.arenagame"
    assert arenagame.export_game(source, GAME, target).log_files == 0
    unpacked = unpack(target, tmp_path)
    assert unpacked.logs == [] and unpacked.game_file.read_bytes() == (tmp_path / "a" / "games" / GAME / GAME_FILE).read_bytes()


def test_unpacking_describes_the_game(tmp_path: Path) -> None:
    source = seed(tmp_path / "a", status="finished")
    target = tmp_path / "g.arenagame"
    arenagame.export_game(source, GAME, target, now=NOW)
    unpacked = unpack(target, tmp_path)
    assert unpacked.session_id == GAME and unpacked.facts["status"] == "finished" and unpacked.facts["session_id"] == GAME
    assert unpacked.created_at == "2026-11-18T00:42:07-08:00" and unpacked.app_version.startswith("v") and unpacked.uncompressed_bytes > 0
    assert unpacked.game_file == tmp_path / "work" / "game" / "game.json"
    assert all(path.parent == tmp_path / "work" / "game" / "logs" for _name, path in unpacked.logs)


def test_a_real_v5_game_of_v013_survives_the_trip(tmp_path: Path) -> None:
    """The game of the demo data (a real v0.13 game of 12 players, 430 KB) - when it is there: the trip changes nothing."""
    demo = Path(__file__).resolve().parents[2] / ".demo-data" / "real" / "games"
    candidates = sorted(demo.glob("*/game.json")) if demo.is_dir() else []
    if not candidates:
        pytest.skip("no demo game here")
    payload = json.loads(candidates[0].read_text(encoding="utf-8"))
    store = GameStore(tmp_path / "a")
    store.save(payload["session_id"], payload)
    target = tmp_path / "real.arenagame"
    arenagame.export_game(store, payload["session_id"], target)
    other = GameStore(tmp_path / "b")
    work = tmp_path / "work"
    work.mkdir()
    arenagame.import_arenagame(other, target, work)
    assert other.load(payload["session_id"]) == store.load(payload["session_id"])


# --------------------------------------------------------------------------------------------------------- exporting
def test_a_damaged_game_file_is_exported_as_its_previous_copy_with_a_warning(tmp_path: Path) -> None:
    source = seed(tmp_path / "a")
    source.save(GAME, json.loads(game_bytes(GAME, title="二")))
    (tmp_path / "a" / "games" / GAME / GAME_FILE).write_bytes(b"{ cut off")
    result = arenagame.export_game(source, GAME, tmp_path / "g.arenagame")
    assert result.used_previous is True and any("备份副本" in warning for warning in result.warnings)
    assert json.loads(unpack(result.path, tmp_path).game_file.read_bytes())["title"] != "二"


def test_a_game_that_is_not_there_or_not_readable(tmp_path: Path) -> None:
    store = GameStore(tmp_path / "a")
    with pytest.raises(GameNotFoundError):
        arenagame.export_game(store, GAME, tmp_path / "g.arenagame")
    (tmp_path / "a" / "games" / GAME).mkdir(parents=True)
    (tmp_path / "a" / "games" / GAME / GAME_FILE).write_bytes(b"\xff not json")
    with pytest.raises(StorageError):
        arenagame.export_game(store, GAME, tmp_path / "g.arenagame")
    assert not (tmp_path / "g.arenagame").exists()


@pytest.mark.parametrize("schema", [4, 7, 0])
def test_only_save_formats_5_and_6_can_be_exported(tmp_path: Path, schema: int) -> None:
    root = tmp_path / "a"
    GameStore(root).save(GAME, json.loads(game_bytes(GAME, schema=schema)))
    with pytest.raises(ArchiveFormatError, match=rf"v{schema}.*v5 或 v6") as excinfo:
        arenagame.export_game(GameStore(root), GAME, tmp_path / "g.arenagame")
    assert excinfo.value.status == 422 and excinfo.value.code == "invalid_archive"
    assert not (tmp_path / "g.arenagame").exists() and not list(tmp_path.glob("*.part"))


def test_a_game_that_does_not_load_cannot_be_exported(tmp_path: Path) -> None:
    root = tmp_path / "a"
    GameStore(root).save(GAME, json.loads(game_bytes(GAME, participants="not a list")))
    with pytest.raises(ArchiveFormatError, match="结构校验"):
        arenagame.export_game(GameStore(root), GAME, tmp_path / "g.arenagame")
    GameStore(root).save(OTHER, json.loads(game_bytes(GAME)))  # (a file that says another id than its folder)
    with pytest.raises(ArchiveFormatError, match="编号与目录名不一致"):
        arenagame.export_game(GameStore(root), OTHER, tmp_path / "h.arenagame")


@pytest.mark.parametrize("session_id", ["", "../x", "a/b", "CON", "learning", "a" * 65, "a b"])
def test_only_a_valid_game_id_can_be_packed(tmp_path: Path, session_id: str) -> None:
    with pytest.raises(ArchiveFormatError, match="无效的对局 id"):
        arenagame.pack_arenagame(tmp_path / "g.arenagame", session_id=session_id, game=game_bytes(GAME))
    assert not list(tmp_path.iterdir())


def test_only_trace_logs_can_be_packed_and_never_a_link_or_a_folder(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    log.write_bytes(b"x\n")
    for name in ("notes.txt", "../trace-0001.jsonl", "trace-1.jsonl", "sub/trace-0001.jsonl", "game.prev.json"):
        with pytest.raises(ArchiveFormatError):
            arenagame.pack_arenagame(tmp_path / "g.arenagame", session_id=GAME, game=game_bytes(GAME), logs=[(name, log)])
    with pytest.raises(ArchiveFormatError, match="普通文件"):
        arenagame.pack_arenagame(tmp_path / "g.arenagame", session_id=GAME, game=game_bytes(GAME), logs=[("trace-0001.jsonl", tmp_path)])
    with pytest.raises(ArchiveFormatError):  # (the same log twice)
        arenagame.pack_arenagame(
            tmp_path / "g.arenagame", session_id=GAME, game=game_bytes(GAME), logs=[("trace-0001.jsonl", log), ("trace-0001.jsonl", log)]
        )
    assert not (tmp_path / "g.arenagame").exists() and not list(tmp_path.glob("*.part"))


def test_a_log_that_vanishes_while_packing_leaves_nothing(tmp_path: Path) -> None:
    with pytest.raises(ArchiveIOError, match="trace-0001"):
        arenagame.pack_arenagame(
            tmp_path / "g.arenagame", session_id=GAME, game=game_bytes(GAME), logs=[("trace-0001.jsonl", tmp_path / "gone.jsonl")]
        )
    assert not (tmp_path / "g.arenagame").exists() and not list(tmp_path.glob("*.part"))


def test_a_game_bigger_than_the_limit_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arenagame, "MAX_GAME_BYTES", 100)
    with pytest.raises(ArchiveLimitError, match="太大"):
        arenagame.pack_arenagame(tmp_path / "g.arenagame", session_id=GAME, game=game_bytes(GAME))
    assert not (tmp_path / "g.arenagame").exists()


def test_an_existing_file_is_replaced_atomically(tmp_path: Path) -> None:
    source = seed(tmp_path / "a")
    target = tmp_path / "g.arenagame"
    target.write_bytes(b"an older export")
    arenagame.export_game(source, GAME, target)
    assert zipfile.is_zipfile(target) and sorted(p.name for p in tmp_path.iterdir()) == ["a", "g.arenagame"]


# ------------------------------------------------------------------------------------------------------- unpacking
def test_the_default_build_helper_makes_an_acceptable_file(tmp_path: Path) -> None:
    unpacked = unpack(build(tmp_path / "ok.arenagame"), tmp_path)
    assert unpacked.session_id == GAME and [name for name, _ in unpacked.logs] == ["trace-0001.jsonl"]


def _flip_a_byte(tmp_path: Path, member: str) -> Path:
    good = build(tmp_path / "good.arenagame")
    tampered = tmp_path / "tampered.arenagame"
    with zipfile.ZipFile(good) as source, zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as out:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == member:
                data = data.replace(b"a", b"b", 1)  # (the same length: only the checksum notices)
            out.writestr(info, data)
    return tampered


@pytest.mark.parametrize("member", [f"games/{GAME}/game.json", f"logs/{GAME}/trace-0001.jsonl"])
def test_a_file_changed_after_it_was_made_is_caught_by_its_checksum(tmp_path: Path, member: str) -> None:
    with pytest.raises(ArchiveFormatError, match="SHA-256"):
        unpack(_flip_a_byte(tmp_path, member), tmp_path)
    assert not (tmp_path / "work" / "game").exists()


def test_a_size_that_does_not_fit_the_manifest_is_caught(tmp_path: Path) -> None:
    def lie(document: dict[str, Any]) -> None:
        document["files"][0]["size"] += 1

    with pytest.raises(ArchiveFormatError, match="大小与清单不符"):
        unpack(build(tmp_path / "lie.arenagame", manifest=lie), tmp_path)


@pytest.mark.parametrize(
    "member, reason",
    [
        (f"games/{GAME}/../../evil.json", r"\.\."),
        ("../evil.md", r"\.\."),
        (f"logs/{GAME}/../../x.jsonl", r"\.\."),
        ("/etc/passwd", "绝对路径"),
        ("C:/Windows/evil.md", "绝对路径"),
        (f"games\\{GAME}\\game.json", "反斜杠|重复"),  # (on Windows zipfile reads a backslash back as "/")
        (f"games/{GAME}/", "空的部分"),
        (f"logs//{GAME}/trace-0002.jsonl", "空的部分"),
    ],
)
def test_a_path_that_leaves_the_folder_is_refused(tmp_path: Path, member: str, reason: str) -> None:
    members = {f"games/{GAME}/game.json": game_bytes(GAME), member: b"x"}
    with pytest.raises(ArchiveFormatError, match=reason):
        unpack(build(tmp_path / "evil.arenagame", members), tmp_path)
    assert not (tmp_path / "work" / "game").exists() and not (tmp_path / "evil.md").exists() and not (tmp_path.parent / "evil.md").exists()


@pytest.mark.parametrize(
    "member",
    [
        f"games/{GAME}/game.prev.json",  # (the previous copy does not go into a .arenagame)
        f"games/{GAME}/notes.txt",
        f"games/{GAME}/sub/game.json",
        "games/index.json",
        f"logs/{GAME}/other.txt",
        f"logs/{GAME}/trace-1.jsonl",
        f"logs/{GAME}/inner.zip",
        f"logs/{GAME}/trace-0002.jsonl.bak",
        "logs/learning/init-1.jsonl",
        "settings.json",
        "providers.json",
        "secrets.enc",
        "bots/lineup.json",
        "contract/contract.md",
        "roster/roster.json",
        "game.json",
        "bot/bot.json",
        "x.arenagame",
    ],
)
def test_a_member_the_format_does_not_know_is_refused(tmp_path: Path, member: str) -> None:
    members = {f"games/{GAME}/game.json": game_bytes(GAME), member: b"x"}
    with pytest.raises(ArchiveFormatError, match="不认识的文件|不该有的文件|格式版本"):
        unpack(build(tmp_path / "strange.arenagame", members), tmp_path)
    assert not (tmp_path / "work" / "game").exists()


def test_another_game_inside_the_file_is_refused(tmp_path: Path) -> None:
    for extra in (f"games/{OTHER}/game.json", f"logs/{OTHER}/trace-0001.jsonl"):
        members = {f"games/{GAME}/game.json": game_bytes(GAME), extra: game_bytes(OTHER)}
        with pytest.raises(ArchiveFormatError, match="不属于清单里的对局"):
            unpack(build(tmp_path / "two.arenagame", members), tmp_path)


def test_the_game_itself_has_to_be_there_once(tmp_path: Path) -> None:
    with pytest.raises(ArchiveFormatError, match="没有对局本身"):
        unpack(build(tmp_path / "nogame.arenagame", {f"logs/{GAME}/trace-0001.jsonl": b"x\n"}), tmp_path)

    def twice(document: dict[str, Any]) -> None:
        document["files"].append(dict(document["files"][0]))

    with pytest.raises(ArchiveFormatError, match="重复"):
        unpack(build(tmp_path / "twice.arenagame", manifest=twice), tmp_path)

    def itself(document: dict[str, Any]) -> None:
        document["files"].append({"path": "manifest.json", "size": 1, "sha256": "0" * 64})

    with pytest.raises(ArchiveFormatError, match="自己"):
        unpack(build(tmp_path / "self.arenagame", manifest=itself), tmp_path)


def test_the_listing_must_be_exactly_the_members(tmp_path: Path) -> None:
    def forget_the_log(document: dict[str, Any]) -> None:
        document["files"] = [item for item in document["files"] if item["path"].startswith("games/")]

    with pytest.raises(ArchiveFormatError, match="多出清单没有记载的文件"):
        unpack(build(tmp_path / "unlisted.arenagame", manifest=forget_the_log), tmp_path)

    def invent(document: dict[str, Any]) -> None:
        document["files"].append({"path": f"logs/{GAME}/trace-0009.jsonl", "size": 1, "sha256": "0" * 64})

    with pytest.raises(ArchiveFormatError, match="缺少清单里记载的文件"):
        unpack(build(tmp_path / "missing.arenagame", manifest=invent), tmp_path)


def test_the_same_member_twice_or_by_case_is_refused(tmp_path: Path) -> None:
    good = build(tmp_path / "good.arenagame")
    doubled = tmp_path / "doubled.arenagame"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(good) as source, zipfile.ZipFile(doubled, "w", zipfile.ZIP_DEFLATED) as out:
            for info in source.infolist():
                out.writestr(info, source.read(info))
            out.writestr(f"logs/{GAME}/trace-0001.jsonl", b"again\n")
    with pytest.raises(ArchiveFormatError, match="重复"):
        unpack(doubled, tmp_path)
    upper = GAME.upper()
    members = {f"games/{GAME}/game.json": game_bytes(GAME), f"games/{upper}/game.json": game_bytes(GAME)}
    with pytest.raises(ArchiveFormatError, match="不属于|重复"):
        unpack(build(tmp_path / "case.arenagame", members), tmp_path)


@pytest.mark.parametrize("session_id", ["../x", "CON", "learning", "a" * 65, "", "a b", "é"])
def test_a_manifest_with_an_id_that_cannot_be_a_folder_is_refused(tmp_path: Path, session_id: str) -> None:
    with pytest.raises(ArchiveFormatError):
        unpack(build(tmp_path / "id.arenagame", manifest={"session_id": session_id}), tmp_path)


def test_the_game_inside_is_checked_like_a_game_the_application_loads(tmp_path: Path) -> None:
    secret = "sk-secret-value-0123456789"
    cases = [
        (game_bytes(GAME, schema=4), "v4.*v5 或 v6"),
        (game_bytes(GAME, schema=7), "v7.*v5 或 v6"),
        (json.dumps({"session_id": GAME}).encode("utf-8"), "需要 v5 或 v6"),
        (game_bytes(GAME, participants="not a list", note=secret), "结构校验"),
        (game_bytes(OTHER), "编号与目录名不一致"),
        (b"{ not json " + secret.encode(), "不是有效的 JSON"),
        (b"[1, 2]", "不是对局文件"),
        (b"\xff\xfe\x00", "不是有效的 JSON"),
    ]
    for index, (data, reason) in enumerate(cases):
        members = {f"games/{GAME}/game.json": data}
        with pytest.raises(ArchiveFormatError, match=reason) as excinfo:
            unpack(build(tmp_path / f"bad-{index}.arenagame", members), tmp_path)
        assert secret not in excinfo.value.message, "the content of a file never appears in a message"
        assert excinfo.value.status == 422 and excinfo.value.code == "invalid_archive"
        shutil.rmtree(tmp_path / "work", ignore_errors=True)


def test_a_zip_bomb_is_refused_before_a_byte_is_unpacked(tmp_path: Path) -> None:
    members = {f"games/{GAME}/game.json": game_bytes(GAME), f"logs/{GAME}/trace-0001.jsonl": b"a" * (64 * 1024 * 1024)}
    bomb = build(tmp_path / "bomb.arenagame", members)
    assert bomb.stat().st_size < 200_000
    with pytest.raises(ArchiveLimitError, match="压缩炸弹") as excinfo:
        unpack(bomb, tmp_path)
    assert excinfo.value.status == 413 and excinfo.value.code == "too_large" and not (tmp_path / "work" / "game").exists()


def test_too_many_files_and_too_much_data_are_refused_by_the_limits(tmp_path: Path) -> None:
    members = {f"games/{GAME}/game.json": game_bytes(GAME), **{f"logs/{GAME}/trace-{n:04d}.jsonl": b"x\n" for n in range(1, 8)}}
    path = build(tmp_path / "many.arenagame", members)
    with pytest.raises(ArchiveLimitError, match="条目太多"):
        unpack(path, tmp_path, limits=ArchiveLimits(max_entries=5))
    with pytest.raises(ArchiveLimitError, match="太大"):
        unpack(path, tmp_path, limits=ArchiveLimits(max_file_bytes=100))
    with pytest.raises(ArchiveLimitError, match="解压后太大"):
        unpack(path, tmp_path, limits=ArchiveLimits(max_total_bytes=1000))
    assert not (tmp_path / "work" / "game").exists()


def test_a_game_file_over_the_limit_is_refused_by_the_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arenagame, "MAX_GAME_BYTES", 100)
    with pytest.raises(ArchiveLimitError, match="对局文件太大"):
        unpack(build(tmp_path / "fat.arenagame"), tmp_path)


def test_not_enough_disk_space_is_reported_before_unpacking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Usage:
        free = 1024

    monkeypatch.setattr(arenagame.shutil, "disk_usage", lambda _path: Usage())
    with pytest.raises(ArchiveIOError, match="磁盘空间不足"):
        unpack(build(tmp_path / "ok.arenagame"), tmp_path)
    assert not (tmp_path / "work" / "game").exists()


def test_a_password_protected_member_an_exotic_compression_and_a_link_are_refused(tmp_path: Path) -> None:
    def link(member: str, info: zipfile.ZipInfo) -> None:
        if member.endswith("trace-0001.jsonl"):
            info.external_attr = (stat.S_IFLNK | 0o777) << 16

    with pytest.raises(ArchiveFormatError, match="符号链接"):
        unpack(build(tmp_path / "link.arenagame", configure=link), tmp_path)
    with pytest.raises(ArchiveFormatError, match="压缩方式"):
        unpack(build(tmp_path / "exotic.arenagame", compress=zipfile.ZIP_LZMA), tmp_path)


# --------------------------------------------------------------------------------------- what is not a .arenagame
@pytest.mark.parametrize("raw", [b"", b"PK", b"not a zip at all " * 30, os.urandom(300)], ids=["empty", "two-bytes", "text", "random"])
def test_something_that_is_not_a_zip_is_refused(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "junk.arenagame"
    path.write_bytes(raw)
    with pytest.raises(ArchiveFormatError, match="不是有效的存档文件|文件太小"):
        unpack(path, tmp_path)


def test_a_missing_or_truncated_file_and_a_zip_without_a_manifest(tmp_path: Path) -> None:
    with pytest.raises(ArchiveFormatError, match="找不到"):
        unpack(tmp_path / "nothing.arenagame", tmp_path)
    good = build(tmp_path / "good.arenagame").read_bytes()
    cut = tmp_path / "cut.arenagame"
    cut.write_bytes(good[: len(good) // 2])
    with pytest.raises(ArchiveFormatError):
        unpack(cut, tmp_path)
    plain = tmp_path / "plain.arenagame"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr(f"games/{GAME}/game.json", game_bytes(GAME))
    with pytest.raises(ArchiveFormatError, match="manifest.json"):
        unpack(plain, tmp_path)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.update(format="zip"), "不是对局文件"),
        (lambda d: d.pop("format"), "不是对局文件"),
        (lambda d: d.update(format_version="1"), "格式版本无效"),
        (lambda d: d.update(format_version=0), "格式版本无效"),
        (lambda d: d.update(format_version=True), "格式版本无效"),
        (lambda d: d.pop("session_id"), "不符合格式"),
        (lambda d: d.update(created_at="yesterday"), "不符合格式"),
        (lambda d: d.update(created_at="2026-11-18T00:42:00"), "不符合格式"),
        (lambda d: d.update(app_version=""), "不符合格式"),
        (lambda d: d.update(unexpected=1), "不符合格式"),
        (lambda d: d.update(files="nope"), "不符合格式"),
        (lambda d: d["files"][0].update(sha256="ABC"), "不符合格式"),
        (lambda d: d["files"][0].update(size=-1), "不符合格式"),
    ],
)
def test_a_manifest_that_does_not_fit_the_format_is_refused(tmp_path: Path, mutate: Callable[[dict[str, Any]], None], message: str) -> None:
    with pytest.raises(ArchiveFormatError, match=message):
        unpack(build(tmp_path / "m.arenagame", manifest=mutate), tmp_path)


def test_a_newer_format_asks_for_an_upgrade(tmp_path: Path) -> None:
    def newer(document: dict[str, Any]) -> None:
        document["format_version"] = arenagame.FORMAT_VERSION + 1
        document["files"] = "the newer model may look quite different"

    with pytest.raises(ArchiveVersionError, match="更新的格式.*升级") as excinfo:
        unpack(build(tmp_path / "newer.arenagame", manifest=newer), tmp_path)
    assert excinfo.value.code == "unsupported_version"


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'"x"', b"\xff\xfe", b"null"])
def test_a_manifest_that_is_not_a_json_object_is_refused(raw: bytes) -> None:
    with pytest.raises(ArchiveFormatError):
        arenagame.parse_game_manifest(raw)


def test_a_total_archive_or_a_bot_file_handed_in_by_mistake_is_named_for_what_it_is(data_dir: Path, tmp_path: Path) -> None:
    populate(data_dir)
    total = export_archive(data_dir, ExportOptions(), now=NOW).path
    with pytest.raises(ArchiveFormatError, match="这是总存档.*不是对局文件"):
        unpack(total, tmp_path)
    bot = tmp_path / "alice.arenabot"
    source = tmp_path / "alice"
    (source / "core").mkdir(parents=True)
    (source / "bot.json").write_bytes(dumps({"id": "b-1a2b3c4d", "name": "Alice"}))
    (source / "core" / "identity.md").write_text("设定\n", encoding="utf-8")
    arenabot.pack_arenabot(bot, name="Alice", origin="user", files=[("bot.json", source / "bot.json"), ("core/identity.md", source / "core" / "identity.md")])
    with pytest.raises(ArchiveFormatError, match="这是 bot 文件.*不是对局文件"):
        unpack(bot, tmp_path)


def test_a_folder_that_is_already_there_is_never_removed(tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / "game").mkdir(parents=True)
    (work / "game" / "precious.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(ArchiveIOError, match="已经有"):
        arenagame.unpack_arenagame(build(tmp_path / "ok.arenagame"), work)
    assert (work / "game" / "precious.txt").read_text(encoding="utf-8") == "mine"


# ---------------------------------------------------------------------------------------------- importing: conflicts
@pytest.fixture()
def exported(tmp_path: Path) -> Path:
    source = seed(tmp_path / "source", status="finished")
    return arenagame.export_game(source, GAME, tmp_path / "game.arenagame").path


def run_import(target: GameStore, file: Path, tmp_path: Path, **kwargs: Any) -> Any:
    work = tmp_path / "import-work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    return arenagame.import_arenagame(target, file, work, **kwargs)


def test_a_taken_id_is_asked_about_with_a_409_exists_that_names_the_game(tmp_path: Path, exported: Path) -> None:
    target = seed(tmp_path / "t", status="paused")
    before = {p.relative_to(tmp_path / "t"): p.read_bytes() for p in (tmp_path / "t").rglob("*") if p.is_file()}
    with pytest.raises(ArchiveExistsError) as excinfo:
        run_import(target, exported, tmp_path)
    error = excinfo.value
    assert (error.status, error.code, error.session_id) == (409, "exists", GAME) and GAME in error.message
    assert {p.relative_to(tmp_path / "t"): p.read_bytes() for p in (tmp_path / "t").rglob("*") if p.is_file()} == before


def test_overwriting_replaces_the_game_and_its_logs(tmp_path: Path, exported: Path) -> None:
    target = seed(tmp_path / "t", status="paused", logs={"trace-0001.jsonl": b"old\n", "trace-0009.jsonl": b"older\n"})
    result = run_import(target, exported, tmp_path, on_conflict="overwrite")
    assert (result.session_id, result.replaced, result.copied, result.status) == (GAME, True, False, "finished")
    assert target.load(GAME)["status"] == "finished"
    assert sorted(logs_of(GAME, tmp_path / "t")) == ["trace-0001.jsonl", "trace-0002.jsonl"], "the old logs are gone, the new ones are there"
    assert logs_of(GAME, tmp_path / "t") == logs_of(GAME, tmp_path / "source")


def test_a_copy_keeps_both_games(tmp_path: Path, exported: Path) -> None:
    target = seed(tmp_path / "t", status="paused", logs={"trace-0001.jsonl": b"mine\n"})
    result = run_import(target, exported, tmp_path, on_conflict="copy")
    assert result.copied is True and result.session_id != GAME
    assert target.load(GAME)["status"] == "paused" and logs_of(GAME, tmp_path / "t") == {"trace-0001.jsonl": b"mine\n"}
    copy = target.load(result.session_id)
    assert copy["status"] == "finished" and copy["session_id"] == result.session_id
    assert logs_of(result.session_id, tmp_path / "t") == logs_of(GAME, tmp_path / "source")


def test_a_free_id_just_installs_whatever_the_policy(tmp_path: Path, exported: Path) -> None:
    for policy in ("ask", "overwrite", "copy"):
        target = GameStore(tmp_path / f"t-{policy}")
        result = run_import(target, exported, tmp_path, on_conflict=policy)
        assert (result.session_id, result.replaced, result.copied) == (GAME, False, False)


def test_the_live_game_is_never_overwritten_even_when_it_was_never_saved(tmp_path: Path, exported: Path) -> None:
    target = GameStore(tmp_path / "t")
    with pytest.raises(ArchiveExistsError):
        run_import(target, exported, tmp_path, protected=[GAME])
    with pytest.raises(ArchiveError) as excinfo:
        run_import(target, exported, tmp_path, on_conflict="overwrite", protected=[GAME])
    assert (excinfo.value.status, excinfo.value.code) == (409, "current") and "当前对局" in excinfo.value.message
    assert not target.exists(GAME)
    result = run_import(target, exported, tmp_path, on_conflict="copy", protected=[GAME])
    assert result.copied is True and result.session_id != GAME and not target.exists(GAME)


def test_a_failure_while_installing_is_an_io_error_and_changes_nothing(tmp_path: Path, exported: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = seed(tmp_path / "t", status="paused")
    before = {p.relative_to(tmp_path / "t"): p.read_bytes() for p in (tmp_path / "t").rglob("*") if p.is_file()}

    def broken(source: Path, destination: Path) -> None:
        raise StorageError("无法移动对局文件夹（可能被其他程序占用）。")

    monkeypatch.setattr(GameStore, "_rename", staticmethod(broken))
    with pytest.raises(ArchiveIOError, match="无法移动对局文件夹") as excinfo:
        run_import(target, exported, tmp_path, on_conflict="overwrite")
    assert excinfo.value.status == 500 and excinfo.value.code == "io_error"
    monkeypatch.undo()
    assert {p.relative_to(tmp_path / "t"): p.read_bytes() for p in (tmp_path / "t").rglob("*") if p.is_file()} == before
    assert not [p for p in (tmp_path / "t" / "games").iterdir() if p.name.startswith(".")]


def test_a_file_that_is_refused_installs_nothing(tmp_path: Path) -> None:
    target = GameStore(tmp_path / "t")
    with pytest.raises(ArchiveFormatError):
        run_import(target, _flip_a_byte(tmp_path, f"games/{GAME}/game.json"), tmp_path)
    assert not (tmp_path / "t").exists() or not list((tmp_path / "t").rglob("*"))
    assert target.list_games() == []


def test_a_bad_policy_is_refused(tmp_path: Path, exported: Path) -> None:
    with pytest.raises(ValueError, match="on_conflict"):
        run_import(GameStore(tmp_path / "t"), exported, tmp_path, on_conflict="merge")


def test_every_refusal_is_an_archive_error_with_a_status_a_code_and_a_chinese_message(tmp_path: Path) -> None:
    junk = tmp_path / "junk.arenagame"
    junk.write_bytes(b"not a zip")
    cases = [
        junk,
        _flip_a_byte(tmp_path, f"games/{GAME}/game.json"),
        build(tmp_path / "strange.arenagame", {f"games/{GAME}/game.json": game_bytes(GAME), "settings.json": b"{}"}),
        build(tmp_path / "old.arenagame", {f"games/{GAME}/game.json": game_bytes(GAME, schema=4)}),
    ]
    for index, case in enumerate(cases):
        work = tmp_path / f"w{index}"
        work.mkdir()
        with pytest.raises(ArchiveError) as excinfo:
            arenagame.unpack_arenagame(case, work)
        error = excinfo.value
        assert isinstance(error.status, int) and error.code and any("\u4e00" <= ch <= "\u9fff" for ch in error.message), (case.name, error)
        assert not (work / "game").exists()
