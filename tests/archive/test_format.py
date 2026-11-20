"""The container format: path allow-list, manifest, checksums (docs/archive.md "格式")."""

from __future__ import annotations

import hashlib
import json

import pytest

from app.archive.errors import ArchiveFormatError, ArchiveVersionError
from app.archive.format import (
    EXPORT_NAME_RE,
    FORMAT_VERSION,
    LEGACY_VERSION,
    KIND_GAME,
    KIND_GAME_PREV,
    KIND_GAMES_INDEX,
    KIND_LOG,
    KIND_MANIFEST,
    KIND_PRICING,
    KIND_PROVIDERS,
    KIND_ROSTER_JSON,
    KIND_ROSTER_TEXT,
    KIND_SECRETS,
    KIND_SETTINGS,
    Contents,
    FileEntry,
    build_manifest,
    classify,
    files_digest,
    manifest_bytes,
    parse_manifest,
)

ALLOWED = {
    "manifest.json": (KIND_MANIFEST, None),
    "settings.json": (KIND_SETTINGS, None),
    "providers.json": (KIND_PROVIDERS, None),
    "pricing.json": (KIND_PRICING, None),
    "roster/roster.json": (KIND_ROSTER_JSON, None),
    "roster/N1.persona.md": (KIND_ROSTER_TEXT, None),
    "roster/N12.memory.md": (KIND_ROSTER_TEXT, None),
    "roster/N99.memory.md": (KIND_ROSTER_TEXT, None),
    "games/index.json": (KIND_GAMES_INDEX, None),
    "games/ab12cd34ef56/game.json": (KIND_GAME, "ab12cd34ef56"),
    "games/ab12cd34ef56/game.prev.json": (KIND_GAME_PREV, "ab12cd34ef56"),
    "games/A_b-9/game.json": (KIND_GAME, "A_b-9"),
    "logs/ab12cd34ef56/trace-0001.jsonl": (KIND_LOG, "ab12cd34ef56"),
    "logs/ab12cd34ef56/trace-12345.jsonl": (KIND_LOG, "ab12cd34ef56"),
    "secrets.enc": (KIND_SECRETS, None),
}


@pytest.mark.parametrize("name, expected", list(ALLOWED.items()))
def test_the_documented_paths_are_allowed(name: str, expected: tuple[str, str | None]) -> None:
    assert classify(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "",
        "/settings.json",
        "C:/settings.json",
        "c:settings.json",
        "\\\\server\\share\\settings.json",
        "../settings.json",
        "games/../settings.json",
        "games/x/../../settings.json",
        "./settings.json",
        "games//x/game.json",
        "games\\x\\game.json",
        "settings.json/",
        "games/",
        "games/x/",
        "logs/",
        "settings.JSON",
        "Settings.json",
        "secrets.v1.enc",
        "master.v1.dpapi",
        ".lock",
        "exports/arena-20260101-000000.arenaagents",
        "backups/x/settings.json",
        "tmp/x",
        pytest.param("settings.json\x00.png", id="nul-in-the-name"),
        pytest.param("settings.json\n", id="newline-in-the-name"),
        "roster/N0.persona.md",
        "roster/N100.persona.md",
        "roster/N1.other.md",
        "roster/extra.json",
        "roster/sub/N1.memory.md",
        "games/x/game.json.bak",
        "games/x/other.json",
        "games/x/y/game.json",
        "games/" + "a" * 65 + "/game.json",
        "games/CON/game.json",
        "games/nul/game.json",
        "games/com1/game.prev.json",
        "logs/x/trace-1.jsonl",
        "logs/x/trace-0001.json",
        "logs/x/other.txt",
        "a" * 300,
        "games/x y/game.json",
        "games/é/game.json",
    ],
)
def test_everything_else_is_rejected(name: str) -> None:
    with pytest.raises(ArchiveFormatError):
        classify(name)


def test_rejection_messages_name_the_reason_and_sanitize_the_name() -> None:
    with pytest.raises(ArchiveFormatError, match="绝对路径"):
        classify("/etc/passwd")
    with pytest.raises(ArchiveFormatError, match=r"\.\."):
        classify("games/../x")
    with pytest.raises(ArchiveFormatError, match="反斜杠"):
        classify("games\\x")
    with pytest.raises(ArchiveFormatError) as excinfo:
        classify("evil\x1b[31m.txt")
    assert "\x1b" not in str(excinfo.value)


def test_export_names_have_one_pattern() -> None:
    assert EXPORT_NAME_RE.fullmatch("arena-20261118-004200.arenaagents")
    for bad in ("arena-2026111-004200.arenaagents", "arena-20261118-004200.zip", "../arena-20261118-004200.arenaagents", "arena-20261118-004200.arenaagents.part", "x/arena-20261118-004200.arenaagents", "arena-20261118-004200.arenaagents\n"):
        assert not EXPORT_NAME_RE.fullmatch(bad), bad


# ----------------------------------------------------------------------------------------- manifest
def _entries() -> list[FileEntry]:
    return [
        FileEntry(path="settings.json", size=3, sha256=hashlib.sha256(b"{ }").hexdigest()),
        FileEntry(path="games/g1/game.json", size=5, sha256=hashlib.sha256(b"12345").hexdigest()),
    ]


def _manifest(**changes: object) -> dict:
    manifest = build_manifest(
        archive_id="0123456789abcdef0123456789abcdef",
        created_at="2026-11-18T00:42:00-08:00",
        app_version="v0.13.0",
        contents=Contents(games=1, providers=0, roster_seats=0, has_secrets=False, has_logs=False),
        files=_entries(),
        format_version=LEGACY_VERSION,
    )
    data = json.loads(manifest_bytes(manifest))
    data.update(changes)
    return data


def _parse(data: dict):  # type: ignore[no-untyped-def]
    return parse_manifest(json.dumps(data).encode("utf-8"))


def test_a_manifest_round_trips_and_lists_files_sorted() -> None:
    manifest = build_manifest(
        archive_id="0123456789abcdef0123456789abcdef",
        created_at="2026-11-18T00:42:00-08:00",
        app_version="v0.13.0",
        contents=Contents(games=1, providers=0, roster_seats=0, has_secrets=False, has_logs=False),
        files=_entries(),
        format_version=LEGACY_VERSION,
    )
    parsed = parse_manifest(manifest_bytes(manifest))
    assert [f.path for f in parsed.files] == ["games/g1/game.json", "settings.json"]
    assert parsed.format == "arenaagents" and parsed.format_version == LEGACY_VERSION
    assert parsed.files_sha256 == files_digest(_entries())


def test_the_file_list_digest_is_documented_canonical_json() -> None:
    """Anyone can recompute ``files_sha256``: sorted by path, ``{"path","sha256","size"}``, compact JSON, UTF-8."""
    entries = _entries()
    rows = sorted(({"path": e.path, "sha256": e.sha256, "size": e.size} for e in entries), key=lambda r: r["path"])
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert files_digest(entries) == hashlib.sha256(payload).hexdigest()
    assert files_digest(list(reversed(entries))) == files_digest(entries)
    changed = [entries[0].model_copy(update={"size": 4}), entries[1]]
    assert files_digest(changed) != files_digest(entries)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.update(format="zip"), "不是 Arena-Agents 存档"),
        (lambda d: d.pop("format"), "不是 Arena-Agents 存档"),
        (lambda d: d.update(format_version="1"), "格式版本无效"),
        (lambda d: d.update(format_version=0), "格式版本无效"),
        (lambda d: d.update(format_version=True), "格式版本无效"),
        (lambda d: d.update(archive_id="XYZ"), "不符合格式"),
        (lambda d: d.update(created_at="yesterday"), "不符合格式"),
        (lambda d: d.update(created_at="2026-11-18T00:42:00"), "不符合格式"),  # no UTC offset
        (lambda d: d.update(unexpected=1), "不符合格式"),
        (lambda d: d.update(files="nope"), "不符合格式"),
        (lambda d: d["files"][0].update(sha256="ABC"), "不符合格式"),
        (lambda d: d["files"][0].update(size=-1), "不符合格式"),
        (lambda d: d["files"][0].update(path="../evil"), "不合法|不允许|\\.\\."),
        (lambda d: d["files"].append(dict(d["files"][0])), "重复"),
        (lambda d: d["files"].append({"path": "manifest.json", "size": 1, "sha256": "0" * 64}), "自己"),
        (lambda d: d.update(files_sha256="0" * 64), "files_sha256"),
        (lambda d: d["contents"].update(has_secrets=True), "内容统计"),
        (lambda d: d["contents"].update(has_logs=True), "内容统计"),
        (lambda d: d["contents"].update(games=-1), "不符合格式"),
    ],
)
def test_a_bad_manifest_is_rejected_with_the_reason(mutate, message: str) -> None:  # type: ignore[no-untyped-def]
    data = _manifest()
    mutate(data)
    with pytest.raises(ArchiveFormatError, match=message):
        _parse(data)


def test_a_newer_format_is_reported_as_such_before_anything_else() -> None:
    data = _manifest(format_version=FORMAT_VERSION + 1)
    data["files"] = "the newer model may look quite different"
    with pytest.raises(ArchiveVersionError, match="更新的格式.*升级"):
        _parse(data)


def test_a_manifest_that_is_not_json_or_not_an_object_is_rejected() -> None:
    for raw in (b"", b"not json", b"[]", b'"x"', b"\xff\xfe", b"null"):
        with pytest.raises(ArchiveFormatError):
            parse_manifest(raw)


def test_validation_errors_never_echo_the_offending_values() -> None:
    data = _manifest(archive_id="super-secret-looking-value")
    with pytest.raises(ArchiveFormatError) as excinfo:
        _parse(data)
    assert "super-secret" not in str(excinfo.value)
