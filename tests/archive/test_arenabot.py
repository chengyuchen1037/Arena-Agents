""".arenabot: one bot as a file (docs/v0.14-plan.md H30, H44; contract 13.9) - the codec of ``app.archive.arenabot``.

The round trips (a bot with every kind of file, the shipped library, a real transfer between two bot stores), the shape of the manifest, and
what is refused: a path that leaves the folder, a member the format does not know, a nested archive, a zip bomb, a file that was changed after
it was made, a file of the wrong kind.  Nothing is left behind by a refusal, and no message ever carries the content of a file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import struct
import warnings
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.archive import arenabot, arenagame
from app.archive.errors import ArchiveError, ArchiveFormatError, ArchiveIOError, ArchiveLimitError, ArchiveVersionError
from app.archive.export import ExportOptions, export_archive
from app.archive.format import ArchiveLimits
from app.storage.bots import BotStore, iter_bot_files
from app.storage.games import GameStore

from .helpers import dumps, populate, populate_v2

NOW = datetime(2026, 11, 18, 0, 42, 7, tzinfo=timezone(timedelta(hours=-8)))
LIBRARY = Path(__file__).resolve().parents[2] / "bots"

#: A bot with EVERY kind of file (the forms ``BOT_FILE_RE`` allows).
BOT_FILES: dict[str, bytes] = {
    "bot.json": dumps({"format": 1, "id": "b-1a2b3c4d", "name": "Alice", "created_at": 1763600000}),
    "core/identity.md": "一个打了很多年线下局的老玩家。\n".encode("utf-8"),
    "core/voice.md": "你说话短，通常两三句话就停。\n".encode("utf-8"),
    "core/mind.md": "你先看发生了什么，再听谁说了什么。\n".encode("utf-8"),
    "core/extra-catchphrase.md": "# 口头禅\n\n「行，听你的，出了再说。」\n".encode("utf-8"),
    "arch/notes.md": "先听完再表态。\n".encode("utf-8"),
    "rules/werewolf_12p/digest.md": "夜里的信息不对称。\n".encode("utf-8"),
    "rules/werewolf_12p/playbook.md": "## 假设\n- [把握:中|支持:3|反例:1|来源:复盘|#对跳] 先看谁的查验对得上。\n".encode("utf-8"),
    "rules/werewolf_12p/questions.md": "- 首夜验谁？\n".encode("utf-8"),
    "people.md": "- Bob：发言短，站边快。\n".encode("utf-8"),
    "record.json": dumps({"games": 3, "wins": 1}),
    "staging/p-1a2b3c4d.json": dumps({"id": "p-1a2b3c4d", "section": "voice", "after": "你说话短。"}),
}


def make_bot(root: Path, files: dict[str, bytes] | None = None) -> list[tuple[str, Path]]:
    for rel, data in (files or BOT_FILES).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return iter_bot_files(root)


def tree_of(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# ------------------------------------------------------------------------------------------ a hand-made .arenabot
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build(
    target: Path,
    members: dict[str, bytes] | None = None,
    *,
    manifest: dict[str, Any] | Callable[[dict[str, Any]], None] | None = None,
    compress: int = zipfile.ZIP_DEFLATED,
    configure: Callable[[str, zipfile.ZipInfo], None] | None = None,
    list_in_manifest: bool = True,
    name: str = "Alice",
    origin: str = "user",
) -> Path:
    """A ``.arenabot`` with exactly ``members`` (member name -> bytes; default: a small valid bot) and a manifest that lists them - then
    ``manifest`` (a dict of fields to set, or a function that changes the manifest dict) and ``configure`` (changes each member's ZipInfo)
    make it wrong in some way.  Member names are written exactly as given (also on Windows, where ``ZipInfo`` would turn ``\\`` into ``/``)."""
    members = dict(members) if members is not None else {f"bot/{rel}": data for rel, data in BOT_FILES.items()}
    document: dict[str, Any] = {
        "format": "arenabot",
        "format_version": 1,
        "created_at": "2026-11-18T00:42:00-08:00",
        "app_version": "v0.14.0",
        "bot": {"name": name, "origin": origin},
        "files": [{"path": n, "size": len(d), "sha256": _sha(d)} for n, d in sorted(members.items())] if list_in_manifest else [],
    }
    if callable(manifest):
        manifest(document)
    elif manifest:
        document.update(manifest)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # (zipfile warns about a duplicate name: some tests write one on purpose)
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


def unpack(source: Path, tmp_path: Path, **kwargs: Any) -> Path:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return arenabot.unpack_arenabot(source, work, **kwargs)


# --------------------------------------------------------------------------------------------------- round trips
def test_a_bot_with_every_kind_of_file_round_trips_byte_for_byte(tmp_path: Path) -> None:
    source = tmp_path / "alice"
    files = make_bot(source)
    assert {rel for rel, _ in files} == set(BOT_FILES), "(the fixture is a bot with every kind of file)"
    target = tmp_path / "out" / "alice.arenabot"
    assert arenabot.pack_arenabot(target, name="Alice", origin="user", files=files, now=NOW) is None
    assert target.is_file() and not list(target.parent.glob("*.part"))
    staged = unpack(target, tmp_path)
    assert staged == tmp_path / "work" / "bot"
    assert tree_of(staged) == BOT_FILES


def test_the_manifest_has_exactly_the_documented_shape(tmp_path: Path) -> None:
    files = make_bot(tmp_path / "alice")
    target = tmp_path / "alice.arenabot"
    arenabot.pack_arenabot(target, name="Alice", origin="default", files=files, now=NOW)
    with zipfile.ZipFile(target) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        members = {info.filename: zf.read(info) for info in zf.infolist()}
    assert set(manifest) == {"format", "format_version", "created_at", "app_version", "bot", "files"}
    assert manifest["format"] == "arenabot" and manifest["format_version"] == 1
    assert manifest["bot"] == {"name": "Alice", "origin": "default"}
    assert manifest["created_at"] == "2026-11-18T00:42:07-08:00" and manifest["app_version"].startswith("v")
    assert [item["path"] for item in manifest["files"]] == sorted(f"bot/{rel}" for rel in BOT_FILES), "every path starts with bot/"
    assert all(set(item) == {"path", "size", "sha256"} for item in manifest["files"])
    for item in manifest["files"]:
        data = members[item["path"]]
        assert item["size"] == len(data) and item["sha256"] == _sha(data)
    assert set(members) == {"manifest.json", *(f"bot/{rel}" for rel in BOT_FILES)}, "exactly the manifest and the bot's files"


def test_the_members_are_plain_regular_files(tmp_path: Path) -> None:
    files = make_bot(tmp_path / "alice")
    target = tmp_path / "alice.arenabot"
    arenabot.pack_arenabot(target, name="Alice", origin="user", files=files)
    with zipfile.ZipFile(target) as zf:
        for info in zf.infolist():
            assert info.compress_type == zipfile.ZIP_DEFLATED and not info.flag_bits & 1
            assert stat.S_ISREG(info.external_attr >> 16) and not info.is_dir()


def test_every_bot_of_the_shipped_library_round_trips(tmp_path: Path) -> None:
    bots = sorted(p for p in LIBRARY.iterdir() if p.is_dir())
    assert len(bots) == 12
    for index, bot in enumerate(bots):
        files = iter_bot_files(bot)
        assert files, bot.name
        target = tmp_path / f"{bot.name}.arenabot"
        arenabot.pack_arenabot(target, name=bot.name, origin="default", files=files)
        work = tmp_path / f"work-{index}"
        work.mkdir()
        staged = arenabot.unpack_arenabot(target, work)
        assert tree_of(staged) == {rel: path.read_bytes() for rel, path in files}, bot.name


def test_a_real_bot_travels_from_one_bot_store_into_a_bot_of_another(data_dir: Path, tmp_path: Path) -> None:
    """Export from a data directory that has everything, import into another one: the id inside the file is ignored (the target keeps its own),
    the whole bot - name, L0 with its extra section, notes, memory, impressions, record, proposals - is the source's."""
    populate_v2(data_dir)
    source = BotStore(data_dir)
    first = source.lineup()[0]
    name = source.name_of(first)
    target_file = tmp_path / "bot.arenabot"
    arenabot.pack_arenabot(target_file, name=name, origin="user", files=source.bot_files(first))

    other_dir = tmp_path / "other"
    other = BotStore(other_dir)
    other.bootstrap()
    mine = other.bot_id_of(name)  # (the same library bot: the same name, another id)
    assert mine is not None and mine != first
    staged = unpack(target_file, tmp_path)
    other.install_bot_dir(mine, staged)

    assert other.check_integrity() == []
    sent = {rel: path.read_bytes() for rel, path in source.bot_files(first) if rel != "bot.json"}
    got = {rel: data for rel, data in tree_of(other.bot_dir(mine)).items() if rel != "bot.json"}
    assert got == sent and {"people.md", "record.json"} <= set(sent)
    assert any(re.fullmatch(r"core/extra-[a-z0-9-]+\.md", rel) for rel in sent), "(the extra section of the L0 travels, whatever its file is called)"
    assert "notes.txt" in tree_of(source.bot_dir(first)) and "notes.txt" not in got, "(a stray file in the bot's folder does not travel)"
    meta = json.loads((other.bot_dir(mine) / "bot.json").read_text(encoding="utf-8"))
    assert meta["id"] == mine and meta["name"] == name


# ------------------------------------------------------------------------------------------------------- packing
def test_an_existing_file_is_replaced_and_nothing_is_left_beside_it(tmp_path: Path) -> None:
    files = make_bot(tmp_path / "alice")
    target = tmp_path / "alice.arenabot"
    target.write_bytes(b"an older export")
    arenabot.pack_arenabot(target, name="Alice", origin="user", files=files)
    assert zipfile.is_zipfile(target) and sorted(p.name for p in tmp_path.iterdir()) == ["alice", "alice.arenabot"]


def test_a_failed_pack_leaves_neither_the_file_nor_a_part(tmp_path: Path) -> None:
    source = tmp_path / "alice"
    files = make_bot(source)
    (source / "people.md").write_bytes(b"\xff\xfe not utf-8")
    target = tmp_path / "alice.arenabot"
    with pytest.raises(ArchiveFormatError, match="people.md"):
        arenabot.pack_arenabot(target, name="Alice", origin="user", files=files)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["alice"]


def test_a_file_that_vanishes_while_packing_is_a_clear_error_and_leaves_nothing(tmp_path: Path) -> None:
    source = tmp_path / "alice"
    files = make_bot(source)
    (source / "people.md").unlink()
    with pytest.raises(ArchiveIOError, match="people.md"):
        arenabot.pack_arenabot(tmp_path / "x.arenabot", name="Alice", origin="user", files=files)
    assert not (tmp_path / "x.arenabot").exists() and not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize(
    "rel",
    [
        "evil.sh",
        "../escape.md",
        "core/other.md",
        "core/extra-Upper.md",
        "core/sub/identity.md",
        "rules/CON/playbook.md",
        "rules/x/other.md",
        "staging/P-1.json",
        "bot.json.bak",
        "/bot.json",
        "bot\\x.json",
    ],
)
def test_only_the_files_of_a_bot_can_be_packed(tmp_path: Path, rel: str) -> None:
    source = tmp_path / "alice"
    files = make_bot(source)
    stray = tmp_path / "stray.md"
    stray.write_text("x", encoding="utf-8")
    with pytest.raises(ArchiveFormatError):
        arenabot.pack_arenabot(tmp_path / "x.arenabot", name="Alice", origin="user", files=[*files, (rel, stray)])
    assert not (tmp_path / "x.arenabot").exists()


def test_a_bot_needs_its_bot_json_and_each_file_only_once(tmp_path: Path) -> None:
    files = make_bot(tmp_path / "alice")
    with pytest.raises(ArchiveFormatError, match="bot.json"):
        arenabot.pack_arenabot(tmp_path / "x.arenabot", name="Alice", origin="user", files=[f for f in files if f[0] != "bot.json"])
    with pytest.raises(ArchiveFormatError, match="两次"):
        arenabot.pack_arenabot(tmp_path / "y.arenabot", name="Alice", origin="user", files=[*files, files[1]])
    with pytest.raises(ArchiveFormatError):
        arenabot.pack_arenabot(tmp_path / "z.arenabot", name="Alice", origin="user", files=[])


def test_a_json_file_of_a_bot_must_be_an_object_and_a_text_file_must_be_text(tmp_path: Path) -> None:
    for rel, data, message in (
        ("record.json", b"[1, 2]", "不是对象"),
        ("bot.json", b"{ broken", "不是有效的 JSON"),
        ("people.md", b"line\x00break", "空字符"),
        ("core/voice.md", "中文".encode("gbk"), "UTF-8"),
    ):
        source = tmp_path / f"b-{rel.replace('/', '-')}"
        files = make_bot(source)
        (source / rel).write_bytes(data)
        with pytest.raises(ArchiveFormatError, match=message):
            arenabot.pack_arenabot(tmp_path / "x.arenabot", name="Alice", origin="user", files=files)


def test_a_link_is_never_packed(tmp_path: Path) -> None:
    source = tmp_path / "alice"
    files = make_bot(source)
    outside = tmp_path / "outside.md"
    outside.write_text("not the bot's", encoding="utf-8")
    link = source / "people.md"
    link.unlink()
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without the privilege
        pytest.skip("cannot create symbolic links here")
    with pytest.raises(ArchiveFormatError, match="普通文件"):
        arenabot.pack_arenabot(tmp_path / "x.arenabot", name="Alice", origin="user", files=files)


def test_what_is_too_big_is_refused_when_packing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "alice"
    files = make_bot(source)
    monkeypatch.setattr(arenabot, "MAX_FILE_BYTES", 40)
    with pytest.raises(ArchiveLimitError, match="太大"):
        arenabot.pack_arenabot(tmp_path / "x.arenabot", name="Alice", origin="user", files=files)
    monkeypatch.setattr(arenabot, "MAX_FILE_BYTES", 1024 * 1024)
    monkeypatch.setattr(arenabot, "MAX_BYTES", 300)
    with pytest.raises(ArchiveLimitError, match="加起来超过"):
        arenabot.pack_arenabot(tmp_path / "y.arenabot", name="Alice", origin="user", files=files)
    assert not (tmp_path / "x.arenabot").exists() and not (tmp_path / "y.arenabot").exists()


def test_the_name_in_the_manifest_is_for_people_only(tmp_path: Path) -> None:
    files = make_bot(tmp_path / "alice")
    target = tmp_path / "x.arenabot"
    arenabot.pack_arenabot(target, name="Al\x00ice\n" + "长" * 500, origin="us\x1b[31mer", files=files)
    info = arenabot.read_arenabot_manifest(target).bot
    assert "\x00" not in info.name and "\n" not in info.name and len(info.name) <= 200 and "\x1b" not in info.origin


# ------------------------------------------------------------------------------------------------------ unpacking
def test_the_default_build_helper_makes_an_acceptable_file(tmp_path: Path) -> None:
    staged = unpack(build(tmp_path / "ok.arenabot"), tmp_path)
    assert tree_of(staged) == BOT_FILES
    manifest = arenabot.read_arenabot_manifest(tmp_path / "ok.arenabot")
    assert manifest.bot.name == "Alice" and len(manifest.files) == len(BOT_FILES)


def _flip_a_byte_in_people(tmp_path: Path) -> Path:
    good = build(tmp_path / "good.arenabot")
    tampered = tmp_path / "tampered.arenabot"
    with zipfile.ZipFile(good) as source, zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as out:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == "bot/people.md":
                data = data.replace("Bob".encode("utf-8"), "Eve".encode("utf-8"))  # (the same length: the sizes still fit)
            out.writestr(info, data)
    return tampered


def test_a_file_changed_after_it_was_made_is_caught_by_its_checksum(tmp_path: Path) -> None:
    with pytest.raises(ArchiveFormatError, match="SHA-256"):
        unpack(_flip_a_byte_in_people(tmp_path), tmp_path)
    assert not (tmp_path / "work" / "bot").exists(), "nothing is left behind"


def test_a_size_that_does_not_fit_the_manifest_is_caught(tmp_path: Path) -> None:
    def lie(document: dict[str, Any]) -> None:
        document["files"][0]["size"] += 1

    with pytest.raises(ArchiveFormatError, match="大小与清单不符"):
        unpack(build(tmp_path / "lie.arenabot", manifest=lie), tmp_path)


@pytest.mark.parametrize(
    "member, reason",
    [
        ("bot/../../evil.md", r"\.\."),
        ("../evil.md", r"\.\."),
        ("bot/core/../../../x", r"\.\."),
        ("bot/./people.md", r"\.\."),
        ("/etc/passwd", "绝对路径"),
        ("C:/Windows/evil.md", "绝对路径"),
        ("\\\\server\\share\\bot\\bot.json", "绝对路径"),
        ("bot\\core\\identity.md", "反斜杠|重复"),  # (on Windows zipfile reads a backslash back as "/": a second bot/core/identity.md)
        ("bot//people.md", "空的部分"),
        ("bot/core/", "空的部分"),
        ("bot/people.md\x00.txt", "不合法|重复"),  # (zipfile cuts a name at the NUL: it reads back as a second bot/people.md)
    ],
)
def test_a_path_that_leaves_the_folder_is_refused(tmp_path: Path, member: str, reason: str) -> None:
    members = {f"bot/{rel}": data for rel, data in BOT_FILES.items()}
    members[member] = b"x"
    with pytest.raises(ArchiveFormatError, match=reason):
        unpack(build(tmp_path / "evil.arenabot", members), tmp_path)
    assert not (tmp_path / "work" / "bot").exists()
    assert not (tmp_path / "evil.md").exists() and not (tmp_path.parent / "evil.md").exists()


@pytest.mark.parametrize(
    "member",
    [
        "bot/evil.sh",
        "bot/core/other.md",
        "bot/core/extra-Upper.md",
        "bot/core/extra-.md",
        "bot/core/sub/identity.md",
        "bot/rules/x/other.md",
        "bot/rules/x/playbook.md.bak",
        "bot/staging/P-1.json",
        "bot/staging/a/b.json",
        "bot/Bot.json",
        "bot/nested.zip",
        "bot/inner.arenabot",
        "bot/bot.json.bak",
        "other/bot.json",
        "bot.json",
        "core/identity.md",
        "settings.json",
        "providers.json",
        "games/g1/game.json",
        "bots/b-1a2b3c4d/bot.json",
        "secrets.enc",
        "bot",
    ],
)
def test_a_member_the_format_does_not_know_is_refused(tmp_path: Path, member: str) -> None:
    members = {f"bot/{rel}": data for rel, data in BOT_FILES.items()}
    members[member] = b"x"
    with pytest.raises(ArchiveFormatError, match="不认识的文件"):
        unpack(build(tmp_path / "strange.arenabot", members), tmp_path)
    assert not (tmp_path / "work" / "bot").exists()


def test_a_scenario_called_like_a_windows_device_is_refused(tmp_path: Path) -> None:
    members = {f"bot/{rel}": data for rel, data in BOT_FILES.items()}
    members["bot/rules/CON/playbook.md"] = b"x"
    with pytest.raises(ArchiveFormatError, match="保留"):
        unpack(build(tmp_path / "con.arenabot", members), tmp_path)


def test_an_archive_inside_an_archive_is_refused_either_way(tmp_path: Path) -> None:
    inner = build(tmp_path / "inner.arenabot")
    # (1) the inner file as a member of a bot
    members = {f"bot/{rel}": data for rel, data in BOT_FILES.items()}
    members["bot/people.md"] = inner.read_bytes()
    with pytest.raises(ArchiveFormatError, match="UTF-8|空字符"):
        unpack(build(tmp_path / "a.arenabot", members), tmp_path)
    members["bot/inner.arenabot"] = inner.read_bytes()
    with pytest.raises(ArchiveFormatError, match="不认识的文件"):
        unpack(build(tmp_path / "b.arenabot", members), tmp_path)
    # (2) the whole file wrapped in another zip
    outer = tmp_path / "outer.arenabot"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.write(inner, "inner.arenabot")
    with pytest.raises(ArchiveFormatError):
        unpack(outer, tmp_path)
    assert not (tmp_path / "work" / "bot").exists()


def test_a_zip_bomb_is_refused_before_a_byte_is_unpacked(tmp_path: Path) -> None:
    members = {f"bot/{rel}": data for rel, data in BOT_FILES.items()}
    members["bot/people.md"] = b"a" * (30 * 1024 * 1024)  # (30 MiB of one letter: a few dozen KB packed)
    bomb = build(tmp_path / "bomb.arenabot", members)
    assert bomb.stat().st_size < 200_000
    with pytest.raises(ArchiveLimitError) as excinfo:
        unpack(bomb, tmp_path)
    assert excinfo.value.status == 413 and excinfo.value.code == "too_large"
    assert not (tmp_path / "work" / "bot").exists()


def test_too_many_small_files_are_refused(tmp_path: Path) -> None:
    members = {f"bot/{rel}": data for rel, data in BOT_FILES.items()}
    members.update({f"bot/staging/p-{n:04d}.json": b"{}" for n in range(1100)})
    with pytest.raises(ArchiveLimitError, match="条目太多"):
        unpack(build(tmp_path / "many.arenabot", members), tmp_path)


def test_the_unpacked_size_is_bounded_by_20_mib(tmp_path: Path) -> None:
    members = {f"bot/{rel}": data for rel, data in BOT_FILES.items()}
    for n in range(21):  # (21 files of exactly 1 MiB: each is allowed, together they are not)
        members[f"bot/staging/p-{n:02d}.json"] = b'{"x":"' + b"a" * (1024 * 1024 - 8) + b'"}'
    with pytest.raises(ArchiveLimitError, match="太大|上限"):
        unpack(build(tmp_path / "heavy.arenabot", members), tmp_path)


def test_a_file_that_is_itself_bigger_than_20_mib_is_refused_at_once(tmp_path: Path) -> None:
    big = tmp_path / "big.arenabot"
    big.write_bytes(os.urandom(21 * 1024 * 1024))
    with pytest.raises(ArchiveLimitError, match="20.0 MB"):
        unpack(big, tmp_path)


def test_one_file_of_a_bot_may_not_be_bigger_than_1_mib(tmp_path: Path) -> None:
    members = {f"bot/{rel}": data for rel, data in BOT_FILES.items()}
    members["bot/people.md"] = b"a" * (1024 * 1024 + 1)
    with pytest.raises(ArchiveLimitError):
        unpack(build(tmp_path / "fat.arenabot", members), tmp_path)


def test_a_bot_json_is_required(tmp_path: Path) -> None:
    members = {f"bot/{rel}": data for rel, data in BOT_FILES.items() if rel != "bot.json"}
    with pytest.raises(ArchiveFormatError, match="bot.json"):
        unpack(build(tmp_path / "nobot.arenabot", members), tmp_path)


def test_the_listing_must_be_exactly_the_members(tmp_path: Path) -> None:
    members = {f"bot/{rel}": data for rel, data in BOT_FILES.items()}

    def forget_one(document: dict[str, Any]) -> None:
        document["files"] = [item for item in document["files"] if item["path"] != "bot/people.md"]

    with pytest.raises(ArchiveFormatError, match="多出清单没有记载的文件"):
        unpack(build(tmp_path / "unlisted.arenabot", members, manifest=forget_one), tmp_path)

    def invent_one(document: dict[str, Any]) -> None:
        document["files"].append({"path": "bot/arch/notes.md.x", "size": 1, "sha256": "0" * 64})

    with pytest.raises(ArchiveFormatError):
        unpack(build(tmp_path / "invented.arenabot", members, manifest=invent_one), tmp_path)

    short = {k: v for k, v in members.items() if k != "bot/people.md"}

    def keep_it(document: dict[str, Any]) -> None:
        document["files"].append({"path": "bot/people.md", "size": 1, "sha256": "0" * 64})

    with pytest.raises(ArchiveFormatError, match="缺少清单里记载的文件"):
        unpack(build(tmp_path / "missing.arenabot", short, manifest=keep_it), tmp_path)


def test_a_manifest_cannot_list_itself_or_a_file_twice(tmp_path: Path) -> None:
    members = {f"bot/{rel}": data for rel, data in BOT_FILES.items()}

    def itself(document: dict[str, Any]) -> None:
        document["files"].append({"path": "manifest.json", "size": 1, "sha256": "0" * 64})

    with pytest.raises(ArchiveFormatError, match="自己"):
        unpack(build(tmp_path / "self.arenabot", members, manifest=itself), tmp_path)

    def twice(document: dict[str, Any]) -> None:
        document["files"].append(dict(document["files"][0]))

    with pytest.raises(ArchiveFormatError, match="重复"):
        unpack(build(tmp_path / "twice.arenabot", members, manifest=twice), tmp_path)


def test_a_name_that_only_differs_by_case_is_a_duplicate(tmp_path: Path) -> None:
    members = {f"bot/{rel}": data for rel, data in BOT_FILES.items()}
    members["bot/rules/ab/digest.md"] = b"x"
    members["bot/rules/AB/digest.md"] = b"y"  # (both are files of a bot; on Windows they would be the same file)
    with pytest.raises(ArchiveFormatError, match="重复"):
        unpack(build(tmp_path / "case.arenabot", members), tmp_path)


def test_the_same_member_twice_in_the_zip_is_refused(tmp_path: Path) -> None:
    good = build(tmp_path / "good.arenabot")
    doubled = tmp_path / "doubled.arenabot"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(good) as source, zipfile.ZipFile(doubled, "w", zipfile.ZIP_DEFLATED) as out:
            for info in source.infolist():
                out.writestr(info, source.read(info))
            out.writestr("bot/people.md", "- Eve：again\n".encode("utf-8"))
    with pytest.raises(ArchiveFormatError, match="重复"):
        unpack(doubled, tmp_path)


def _set_flag_in_central_directory(path: Path, member: str, flag: int) -> None:
    """Set a bit of the "general purpose flags" of ``member`` in the central directory of the zip ``path`` (``zipfile`` resets the flags of
    what it writes, so a flag like "encrypted" can only be put there afterwards)."""
    data = bytearray(path.read_bytes())
    position = data.find(b"PK\x01\x02")
    while position >= 0:
        name_length = struct.unpack_from("<H", data, position + 28)[0]
        if bytes(data[position + 46 : position + 46 + name_length]) == member.encode("utf-8"):
            flags = struct.unpack_from("<H", data, position + 8)[0]
            struct.pack_into("<H", data, position + 8, flags | flag)
            path.write_bytes(bytes(data))
            return
        position = data.find(b"PK\x01\x02", position + 4)
    raise AssertionError(f"no central directory entry for {member}")


def test_a_password_protected_member_is_refused(tmp_path: Path) -> None:
    locked = build(tmp_path / "locked.arenabot")
    _set_flag_in_central_directory(locked, "bot/people.md", 0x1)
    with pytest.raises(ArchiveFormatError, match="密码"):
        unpack(locked, tmp_path)
    assert not (tmp_path / "work" / "bot").exists()


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_only_stored_and_deflated_members_are_accepted(tmp_path: Path, method: int) -> None:
    with pytest.raises(ArchiveFormatError, match="压缩方式"):
        unpack(build(tmp_path / "exotic.arenabot", compress=method), tmp_path)


def test_a_symbolic_link_member_is_refused(tmp_path: Path) -> None:
    def link(member: str, info: zipfile.ZipInfo) -> None:
        if member == "bot/people.md":
            info.external_attr = (stat.S_IFLNK | 0o777) << 16

    with pytest.raises(ArchiveFormatError, match="符号链接"):
        unpack(build(tmp_path / "link.arenabot", configure=link), tmp_path)


def test_the_content_of_the_files_is_checked_and_never_echoed(tmp_path: Path) -> None:
    secret = b"sk-secret-value-0123456789"
    for rel, data, message in (
        ("bot/record.json", b'["' + secret + b'"]', "不是对象"),
        ("bot/bot.json", b'{ "api_key": "' + secret + b'"', "不是有效的 JSON"),
        ("bot/people.md", secret + b"\x00", "空字符"),
        ("bot/core/voice.md", secret + b"\xff\xfe", "UTF-8"),
    ):
        members = {f"bot/{r}": d for r, d in BOT_FILES.items()}
        members[rel] = data
        with pytest.raises(ArchiveFormatError, match=message) as excinfo:
            unpack(build(tmp_path / "bad.arenabot", members), tmp_path)
        assert "sk-secret" not in excinfo.value.message and "api_key" not in excinfo.value.message
        shutil.rmtree(tmp_path / "work", ignore_errors=True)


# --------------------------------------------------------------------------------------- what is not a .arenabot
@pytest.mark.parametrize(
    "raw",
    [b"", b"PK", b"not a zip at all, just text " * 20, os.urandom(300), b"PK\x05\x06" + b"\0" * 18],
    ids=["empty", "two-bytes", "text", "random", "empty-zip-record"],
)
def test_something_that_is_not_a_zip_is_refused(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "junk.arenabot"
    path.write_bytes(raw)
    with pytest.raises(ArchiveFormatError, match="不是有效的存档文件|文件太小|没有 manifest|没有任何数据"):
        unpack(path, tmp_path)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ArchiveFormatError, match="找不到"):
        unpack(tmp_path / "nothing.arenabot", tmp_path)


def test_a_truncated_file_is_refused(tmp_path: Path) -> None:
    good = build(tmp_path / "good.arenabot").read_bytes()
    path = tmp_path / "cut.arenabot"
    path.write_bytes(good[: len(good) // 2])
    with pytest.raises(ArchiveFormatError):
        unpack(path, tmp_path)


def test_a_zip_without_a_manifest_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "plain.arenabot"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("bot/bot.json", b"{}")
    with pytest.raises(ArchiveFormatError, match="manifest.json"):
        unpack(path, tmp_path)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.update(format="zip"), "不是 bot 文件"),
        (lambda d: d.pop("format"), "不是 bot 文件"),
        (lambda d: d.update(format_version="1"), "格式版本无效"),
        (lambda d: d.update(format_version=0), "格式版本无效"),
        (lambda d: d.update(format_version=True), "格式版本无效"),
        (lambda d: d.update(created_at="yesterday"), "不符合格式"),
        (lambda d: d.update(created_at="2026-11-18T00:42:00"), "不符合格式"),  # (no UTC offset)
        (lambda d: d.update(app_version=""), "不符合格式"),
        (lambda d: d.update(unexpected=1), "不符合格式"),
        (lambda d: d.pop("bot"), "不符合格式"),
        (lambda d: d.update(bot={"name": "A"}), "不符合格式"),
        (lambda d: d.update(bot={"name": "A", "origin": "x", "id": "b-1"}), "不符合格式"),
        (lambda d: d.update(bot={"name": "A\x00", "origin": "x"}), "不符合格式"),
        (lambda d: d.update(bot={"name": "A" * 201, "origin": "x"}), "不符合格式"),
        (lambda d: d.update(files="nope"), "不符合格式"),
        (lambda d: d["files"][0].update(sha256="ABC"), "不符合格式"),
        (lambda d: d["files"][0].update(size=-1), "不符合格式"),
        (lambda d: d["files"][0].update(extra=1), "不符合格式"),
    ],
)
def test_a_manifest_that_does_not_fit_the_format_is_refused(tmp_path: Path, mutate: Callable[[dict[str, Any]], None], message: str) -> None:
    with pytest.raises(ArchiveFormatError, match=message):
        unpack(build(tmp_path / "m.arenabot", manifest=mutate), tmp_path)


def test_a_newer_format_asks_for_an_upgrade_before_anything_else_is_judged(tmp_path: Path) -> None:
    def newer(document: dict[str, Any]) -> None:
        document["format_version"] = arenabot.FORMAT_VERSION + 1
        document["files"] = "the newer model may look quite different"

    with pytest.raises(ArchiveVersionError, match="更新的格式.*升级") as excinfo:
        unpack(build(tmp_path / "newer.arenabot", manifest=newer), tmp_path)
    assert excinfo.value.code == "unsupported_version" and excinfo.value.status == 422


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'"x"', b"\xff\xfe", b"null"])
def test_a_manifest_that_is_not_a_json_object_is_refused(raw: bytes) -> None:
    with pytest.raises(ArchiveFormatError):
        arenabot.parse_bot_manifest(raw)


def test_a_total_archive_or_a_game_file_handed_in_by_mistake_is_named_for_what_it_is(data_dir: Path, tmp_path: Path) -> None:
    populate(data_dir)
    total = export_archive(data_dir, ExportOptions(), now=NOW).path
    with pytest.raises(ArchiveFormatError, match="这是总存档.*不是 bot 文件"):
        unpack(total, tmp_path)

    store = GameStore(data_dir)
    game_id = next(p.name for p in (data_dir / "games").iterdir() if p.is_dir())
    game_file = tmp_path / "g.arenagame"
    arenagame.export_game(store, game_id, game_file)
    with pytest.raises(ArchiveFormatError, match="这是对局文件.*不是 bot 文件"):
        unpack(game_file, tmp_path)
    assert not (tmp_path / "work" / "bot").exists()


# ----------------------------------------------------------------------------------------------- the target folder
def test_a_folder_that_is_already_there_is_never_removed(tmp_path: Path) -> None:
    """What a failed unpacking removes is only what it made itself: a ``bot`` folder that was there before is left alone."""
    work = tmp_path / "work"
    (work / "bot").mkdir(parents=True)
    (work / "bot" / "precious.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(ArchiveIOError, match="已经有"):
        arenabot.unpack_arenabot(build(tmp_path / "ok.arenabot"), work)
    assert (work / "bot" / "precious.txt").read_text(encoding="utf-8") == "mine"


def test_the_work_folder_is_made_when_it_is_missing(tmp_path: Path) -> None:
    staged = arenabot.unpack_arenabot(build(tmp_path / "ok.arenabot"), tmp_path / "new" / "deep")
    assert staged == tmp_path / "new" / "deep" / "bot" and tree_of(staged) == BOT_FILES


def test_the_limits_can_be_tightened_by_the_caller(tmp_path: Path) -> None:
    tight = ArchiveLimits(max_entries=5, max_file_bytes=1024, max_total_bytes=4096, max_manifest_bytes=4096, max_json_bytes=1024)
    with pytest.raises(ArchiveLimitError):
        unpack(build(tmp_path / "ok.arenabot"), tmp_path, limits=tight)


def test_every_refusal_is_an_archive_error_with_a_status_a_code_and_a_chinese_message(tmp_path: Path) -> None:
    cases: list[Path] = []
    junk = tmp_path / "junk.arenabot"
    junk.write_bytes(b"not a zip")
    cases.append(junk)
    cases.append(_flip_a_byte_in_people(tmp_path))
    cases.append(build(tmp_path / "strange.arenabot", {"bot/bot.json": b"{}", "bot/evil.sh": b"x"}))
    cases.append(build(tmp_path / "nobot.arenabot", {"bot/people.md": b"x"}))
    for index, case in enumerate(cases):
        work = tmp_path / f"w{index}"
        work.mkdir()
        with pytest.raises(ArchiveError) as excinfo:
            arenabot.unpack_arenabot(case, work)
        error = excinfo.value
        assert isinstance(error.status, int) and error.code and any("\u4e00" <= ch <= "\u9fff" for ch in error.message), (case.name, error)
        assert not (work / "bot").exists()
