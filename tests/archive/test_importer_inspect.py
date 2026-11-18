"""Checking an archive before anything is imported (docs/archive.md "校验").  Every case here must be refused with a
reason, and none of them may touch the data directory."""

from __future__ import annotations

import json
import stat
import struct
import warnings
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.archive import importer
from app.archive.errors import ArchiveError, ArchiveFormatError, ArchiveLimitError, ArchiveVersionError
from app.archive.export import ExportOptions, export_archive
from app.archive.format import ArchiveLimits

from .helpers import (
    GAME_A,
    GAME_B,
    KEY_1,
    PASSWORD,
    PROVIDER_1,
    PROVIDER_2,
    FakeSecrets,
    dumps,
    game_payload,
    members_of,
    mutate_archive,
    populate,
    providers_payload,
    tree,
    write_zip,
)

NOW = datetime(2026, 11, 18, 0, 42, 7, tzinfo=timezone(timedelta(hours=-8)))


@pytest.fixture
def archive(data_dir: Path, populated: dict[str, bytes], tmp_path: Path) -> Path:
    """A good archive without keys; ``populated`` is its content."""
    exported = export_archive(data_dir, ExportOptions(include_keys=False), now=NOW).path
    target = tmp_path / "good.arenaagents"
    target.write_bytes(exported.read_bytes())
    return target


@pytest.fixture
def keyed(data_dir: Path, populated: dict[str, bytes], tmp_path: Path) -> Path:
    exported = export_archive(
        data_dir, ExportOptions(include_keys=True, password=PASSWORD), secrets=FakeSecrets({PROVIDER_1: KEY_1}), now=NOW
    ).path
    target = tmp_path / "keyed.arenaagents"
    target.write_bytes(exported.read_bytes())
    return target


def _tampered(source: Path, tmp_path: Path, mutate: Any, *, fix: bool = False, name: str = "tampered.arenaagents") -> Path:
    return mutate_archive(source, tmp_path / name, mutate, fix_manifest=fix)


# ----------------------------------------------------------------------------------------------- good
def test_inspect_describes_the_archive(archive: Path) -> None:
    summary = importer.inspect(archive)
    assert (summary.games, summary.providers, summary.roster_seats) == (2, 2, 2)
    assert (summary.has_secrets, summary.has_logs) == (False, True)
    assert summary.format_version == 1 and summary.app_version.startswith("v") and summary.created_at.startswith("2026-11-18T00:42:07")
    assert summary.size_bytes == archive.stat().st_size and summary.uncompressed_bytes > 0 and summary.file_count > 8
    listed = {g["session_id"]: g for g in summary.game_list}
    assert listed[GAME_B]["status"] == "finished" and listed[GAME_B]["winner"] == "villagers" and listed[GAME_A]["status"] == "paused"
    as_dict = summary.to_dict()
    json.dumps(as_dict)  # it goes straight into an HTTP response
    assert as_dict["provider_names"] and "api_key" not in json.dumps(as_dict)


def test_a_keyed_archive_says_so_without_a_password(keyed: Path) -> None:
    assert importer.inspect(keyed).has_secrets is True


def test_inspect_writes_nothing(archive: Path, data_dir: Path) -> None:
    before = tree(data_dir)
    importer.inspect(archive)
    assert tree(data_dir) == before


def test_inspect_reports_progress_in_bytes(archive: Path) -> None:
    seen: list[tuple[str, int, int]] = []
    importer.inspect(archive, progress=lambda p, d, t: seen.append((p, d, t)))
    assert {p for p, _, _ in seen} == {"verify"} and seen[-1][1] == seen[-1][2] > 0
    assert [d for _, d, _ in seen] == sorted(d for _, d, _ in seen)


def test_an_archive_may_lack_optional_members(tmp_path: Path) -> None:
    only = write_zip(tmp_path / "small.arenaagents", {"settings.json": dumps({"schema_version": 2, "default_bindings": {}, "last_session_id": None, "ui": {}, "model_templates": []})})
    summary = importer.inspect(only)
    assert (summary.games, summary.providers, summary.has_logs) == (0, 0, False)


# ------------------------------------------------------------------------------- not an archive at all
def test_things_that_are_not_archives_are_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    text = tmp_path / "text"
    text.write_bytes(b"hello " * 100)
    a_dir = tmp_path / "adir"
    a_dir.mkdir()
    for path in (empty, text, a_dir, tmp_path / "missing"):
        with pytest.raises(ArchiveFormatError):
            importer.inspect(path)


def test_a_truncated_archive_is_refused(archive: Path, tmp_path: Path) -> None:
    raw = archive.read_bytes()
    for keep in (len(raw) // 2, len(raw) - 10, 30):
        cut = tmp_path / f"cut{keep}.arenaagents"
        cut.write_bytes(raw[:keep])
        with pytest.raises(ArchiveFormatError, match="不是有效的存档"):
            importer.inspect(cut)


def test_an_archive_with_a_flipped_byte_is_refused(tmp_path: Path) -> None:
    stored = write_zip(
        tmp_path / "stored.arenaagents",
        {"settings.json": dumps({"schema_version": 2, "default_bindings": {}, "last_session_id": None, "ui": {}, "model_templates": []}), "pricing.json": dumps({"x": "y" * 400})},
        compress=zipfile.ZIP_STORED,
    )
    with zipfile.ZipFile(stored) as zf:
        info = zf.getinfo("pricing.json")
        offset = info.header_offset + 30 + len(info.filename) + len(info.extra) + 100
    raw = bytearray(stored.read_bytes())
    raw[offset] ^= 0x01
    stored.write_bytes(bytes(raw))
    with pytest.raises(ArchiveFormatError, match="pricing.json"):
        importer.inspect(stored)


# --------------------------------------------------------------------------------- content tampering
def _sha_mismatch(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
    data = bytearray(members["settings.json"])
    data[10] = data[10] ^ 0x01 if data[10] not in (0x20, 0x0A) else ord("x")
    members["settings.json"] = bytes(data)


def _one_byte_longer(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
    members["settings.json"] += b" "


def _drop_member(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
    del members["pricing.json"]


def _extra_member(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
    members["roster/N5.persona.md"] = "多出来的文件".encode("utf-8")


def _drop_manifest_entry(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
    manifest["files"] = [f for f in manifest["files"] if f["path"] != "pricing.json"]


def _bad_digest(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
    manifest["files_sha256"] = "0" * 64


def _wrong_size(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
    manifest["files"][0]["size"] += 1


def _wrong_counts(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
    manifest["contents"]["games"] = 5


@pytest.mark.parametrize(
    "mutate, fix, message",
    [
        (_sha_mismatch, False, "SHA-256"),
        (_one_byte_longer, False, "大小"),
        (_drop_member, False, "缺少"),
        (_extra_member, False, "多出"),
        (_drop_manifest_entry, False, "files_sha256|多出"),
        (_bad_digest, False, "files_sha256"),
        (_wrong_size, False, "files_sha256|大小"),
        (_wrong_counts, True, "内容统计"),
    ],
    ids=["byte-changed", "size-changed", "member-missing", "member-added", "manifest-entry-dropped", "digest", "size-lie", "counts"],
)
def test_changed_archives_are_refused(archive: Path, tmp_path: Path, mutate: Any, fix: bool, message: str) -> None:
    with pytest.raises(ArchiveFormatError, match=message):
        importer.inspect(_tampered(archive, tmp_path, mutate, fix=fix))


def test_a_forger_who_recomputes_the_checksums_is_stopped_only_by_content_checks(archive: Path, tmp_path: Path) -> None:
    """SHA-256 detects damage, it cannot stop somebody who rewrites the manifest too (docs/archive.md "能力边界")."""

    def rewrite(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
        members["pricing.json"] = dumps({"schema": 1, "entries": {"evil": [0, 0, None, None, None, "x"]}})

    forged = _tampered(archive, tmp_path, rewrite, fix=True)
    assert importer.inspect(forged).games == 2  # accepted: it is a perfectly valid archive of other data


# ---------------------------------------------------------------------------------------- bad names
def _zip_with_member(tmp_path: Path, name: str, data: bytes = b"{}") -> Path:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return write_zip(tmp_path / "evil.arenaagents", {"settings.json": b"{}", name: data})


@pytest.mark.parametrize(
    "name",
    [
        "../evil.txt",
        "games/../../evil.txt",
        "/etc/passwd",
        "C:/Windows/evil.txt",
        "..\\evil.txt",
        "games//game.json",
        "unknown.txt",
        "secrets.v1.enc",
        "master.v1.dpapi",
        ".lock",
        "backups/x/settings.json",
        "exports/a.arenaagents",
        "games/CON/game.json",
        "logs/x/../../y/trace-0001.jsonl",
        "games/",
    ],
)
def test_unsafe_or_unknown_member_names_are_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(ArchiveFormatError):
        importer.inspect(_zip_with_member(tmp_path, name))


def test_a_symbolic_link_member_is_refused(tmp_path: Path) -> None:
    def link(zf: zipfile.ZipFile) -> None:
        info = zipfile.ZipInfo("pricing.json")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "settings.json")

    path = write_zip(tmp_path / "link.arenaagents", {"settings.json": b"{}"}, configure=link)
    with pytest.raises(ArchiveFormatError, match="符号链接"):
        importer.inspect(path)


def test_duplicate_members_are_refused(tmp_path: Path) -> None:
    def twice(zf: zipfile.ZipFile) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            zf.writestr("settings.json", b'{"second": true}')

    path = write_zip(tmp_path / "dup.arenaagents", {"settings.json": b"{}"}, configure=twice)
    with pytest.raises(ArchiveFormatError, match="重复"):
        importer.inspect(path)


def test_members_that_differ_only_by_case_are_refused(tmp_path: Path) -> None:
    path = write_zip(tmp_path / "case.arenaagents", {"games/AbC/game.json": b"{}", "games/abc/game.json": b"{}"}, manifest=False)
    with pytest.raises(ArchiveFormatError):
        importer.inspect(path)


def _set_central_flag(path: Path, member: str, bits: int) -> None:
    """Set general-purpose flag bits of ``member`` in the central directory (``ZipFile.writestr`` resets them)."""
    raw = bytearray(path.read_bytes())
    position = 0
    while True:
        position = raw.find(b"PK\x01\x02", position)
        assert position >= 0, "member not found"
        name_len, extra_len, comment_len = struct.unpack("<HHH", raw[position + 28 : position + 34])
        name = bytes(raw[position + 46 : position + 46 + name_len]).decode("ascii")
        if name == member:
            flags = struct.unpack("<H", raw[position + 8 : position + 10])[0] | bits
            raw[position + 8 : position + 10] = struct.pack("<H", flags)
            path.write_bytes(bytes(raw))
            return
        position += 46 + name_len + extra_len + comment_len


def test_zip_encrypted_and_unusual_compression_are_refused(tmp_path: Path) -> None:
    path = write_zip(tmp_path / "enc.arenaagents", {"settings.json": b"{}", "pricing.json": b"{}"})
    _set_central_flag(path, "pricing.json", 0x1)
    with pytest.raises(ArchiveFormatError, match="密码加密"):
        importer.inspect(path)

    def bzip(zf: zipfile.ZipFile) -> None:
        zf.writestr(zipfile.ZipInfo("pricing.json"), b"{}", compress_type=zipfile.ZIP_BZIP2)

    path = write_zip(tmp_path / "bz.arenaagents", {"settings.json": b"{}"}, configure=bzip)
    with pytest.raises(ArchiveFormatError, match="压缩方式"):
        importer.inspect(path)


# ------------------------------------------------------------------------------------- the manifest
def test_an_archive_without_a_manifest_is_refused(tmp_path: Path) -> None:
    path = write_zip(tmp_path / "bare.arenaagents", {"settings.json": b"{}"}, manifest=False)
    with pytest.raises(ArchiveFormatError, match="manifest.json"):
        importer.inspect(path)


def test_an_archive_with_an_empty_manifest_list_is_refused(tmp_path: Path) -> None:
    path = write_zip(tmp_path / "none.arenaagents", {})
    with pytest.raises(ArchiveFormatError, match="没有任何数据"):
        importer.inspect(path)


def test_another_programs_zip_is_not_an_archive(tmp_path: Path) -> None:
    path = tmp_path / "other.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format": "something-else"}))
    with pytest.raises(ArchiveFormatError, match="不是 Arena-Agents 存档"):
        importer.inspect(path)


def test_a_newer_archive_format_asks_for_an_upgrade(archive: Path, tmp_path: Path) -> None:
    def newer(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
        manifest["format_version"] = 2
        manifest["something_new"] = {"a": 1}

    with pytest.raises(ArchiveVersionError, match="升级"):
        importer.inspect(_tampered(archive, tmp_path, newer))


# ---------------------------------------------------------------------------------------- the content
def _put_game(payload: dict[str, Any], game_id: str = GAME_A) -> Any:
    def mutate(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
        members[f"games/{game_id}/game.json"] = dumps(payload)

    return mutate


def test_a_v4_game_is_refused_with_the_hint_about_the_conversion_script(archive: Path, tmp_path: Path) -> None:
    old = game_payload(GAME_A)
    old["schema_version"] = 4
    with pytest.raises(ArchiveFormatError, match="v4.*migrate_v0121_to_archive"):
        importer.inspect(_tampered(archive, tmp_path, _put_game(old), fix=True))


def test_a_game_must_be_a_valid_session(archive: Path, tmp_path: Path) -> None:
    broken = game_payload(GAME_A)
    broken["status"] = "bogus-status"
    with pytest.raises(ArchiveFormatError, match="结构校验"):
        importer.inspect(_tampered(archive, tmp_path, _put_game(broken), fix=True))
    wrong_id = game_payload("zzzz9999zzzz")
    with pytest.raises(ArchiveFormatError, match="编号"):
        importer.inspect(_tampered(archive, tmp_path, _put_game(wrong_id), fix=True, name="t2.arenaagents"))


def test_a_game_file_that_is_not_json_is_refused(archive: Path, tmp_path: Path) -> None:
    def garbage(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
        members[f"games/{GAME_A}/game.json"] = b"{ truncated"

    with pytest.raises(ArchiveFormatError, match="JSON"):
        importer.inspect(_tampered(archive, tmp_path, garbage, fix=True))


def test_a_previous_copy_without_its_game_is_refused(archive: Path, tmp_path: Path) -> None:
    def orphan(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
        members["games/cccc3333cccc/game.prev.json"] = dumps(game_payload("cccc3333cccc"))

    with pytest.raises(ArchiveFormatError, match="备份副本"):
        importer.inspect(_tampered(archive, tmp_path, orphan, fix=True))


def test_a_previous_copy_is_accepted_when_it_is_valid(archive: Path, tmp_path: Path) -> None:
    def with_prev(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
        members[f"games/{GAME_A}/game.prev.json"] = dumps(game_payload(GAME_A))

    assert importer.inspect(_tampered(archive, tmp_path, with_prev, fix=True)).games == 2


def test_an_api_key_in_the_clear_is_refused(archive: Path, tmp_path: Path) -> None:
    def leak(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
        providers = providers_payload(PROVIDER_1, PROVIDER_2)
        providers["providers"][0]["api_key"] = KEY_1
        members["providers.json"] = dumps(providers)

    with pytest.raises(ArchiveFormatError, match="API Key") as excinfo:
        importer.inspect(_tampered(archive, tmp_path, leak, fix=True))
    assert KEY_1 not in str(excinfo.value)


def test_duplicate_provider_ids_are_refused(archive: Path, tmp_path: Path) -> None:
    def twice(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
        members["providers.json"] = dumps(providers_payload(PROVIDER_1, PROVIDER_1))

    with pytest.raises(ArchiveFormatError, match="重复"):
        importer.inspect(_tampered(archive, tmp_path, twice, fix=True))


def test_a_malformed_secrets_file_is_refused_without_a_password(keyed: Path, tmp_path: Path) -> None:
    def garbage(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
        members["secrets.enc"] = b'{"format": "arenaagents"}'

    with pytest.raises(ArchiveFormatError, match="secrets.enc"):
        importer.inspect(_tampered(keyed, tmp_path, garbage, fix=True))


# ------------------------------------------------------------------------------------------- limits
def test_too_many_entries_are_refused(archive: Path) -> None:
    with pytest.raises(ArchiveLimitError, match="条目太多"):
        importer.inspect(archive, limits=ArchiveLimits(max_entries=5))


def test_the_declared_entry_count_is_checked_before_the_zip_module_allocates_anything(archive: Path, tmp_path: Path) -> None:
    raw = bytearray(archive.read_bytes())
    end = raw.rfind(b"PK\x05\x06")
    raw[end + 8 : end + 12] = struct.pack("<HH", 60000, 60000)  # "60 000 entries"
    lying = tmp_path / "lying.arenaagents"
    lying.write_bytes(bytes(raw))
    with pytest.raises(ArchiveLimitError, match="条目太多"):
        importer.inspect(lying)


def test_a_huge_unpacked_size_is_refused(archive: Path) -> None:
    with pytest.raises(ArchiveLimitError, match="太大"):
        importer.inspect(archive, limits=ArchiveLimits(max_total_bytes=500))
    with pytest.raises(ArchiveLimitError, match="太大"):
        importer.inspect(archive, limits=ArchiveLimits(max_file_bytes=100))


def test_a_compression_bomb_is_refused(tmp_path: Path) -> None:
    bomb = write_zip(tmp_path / "bomb.arenaagents", {f"logs/{GAME_A}/trace-0001.jsonl": b"0" * (8 * 1024 * 1024)})
    assert bomb.stat().st_size < 100 * 1024
    with pytest.raises(ArchiveLimitError, match="压缩炸弹"):
        importer.inspect(bomb)


def test_the_size_of_a_member_that_has_to_be_parsed_is_capped(archive: Path) -> None:
    with pytest.raises(ArchiveLimitError, match="太大"):
        importer.inspect(archive, limits=ArchiveLimits(max_json_bytes=200))


def test_all_the_refusals_are_archive_errors_with_a_code(archive: Path, tmp_path: Path) -> None:
    with pytest.raises(ArchiveError) as excinfo:
        importer.inspect(_tampered(archive, tmp_path, _sha_mismatch))
    assert excinfo.value.code == "invalid_archive" and excinfo.value.status == 422
    with pytest.raises(ArchiveError) as excinfo:
        importer.inspect(archive, limits=ArchiveLimits(max_entries=5))
    assert excinfo.value.code == "too_large" and excinfo.value.status == 413
