"""The path allow-list and the manifest of format 2 (docs/v0.14-plan.md §2.5), and what format 1 still allows."""

from __future__ import annotations

import hashlib
import json

import pytest

from app.archive.errors import ArchiveFormatError, ArchiveVersionError
from app.archive.format import (
    FORMAT_VERSION,
    KIND_BOT_FILE,
    KIND_BOTS_LINEUP,
    KIND_BOTS_USAGE,
    KIND_CONTRACT,
    KIND_CONTRACT_SOURCE,
    KIND_GAME,
    KIND_GAME_PREV,
    KIND_GAMES_INDEX,
    KIND_LEARNING_LOG,
    KIND_LOG,
    KIND_MANIFEST,
    KIND_PRICING,
    KIND_PROVIDERS,
    KIND_ROSTER_JSON,
    KIND_ROSTER_TEXT,
    KIND_RULES_MATERIAL,
    KIND_RULES_MATERIAL_META,
    KIND_RULES_SCENARIO,
    KIND_RULES_SOURCE,
    KIND_SECRETS,
    KIND_SETTINGS,
    LEGACY_VERSION,
    SUPPORTED_VERSIONS,
    Contents,
    FileEntry,
    build_manifest,
    check_member_name,
    classify,
    is_reserved_id,
    kinds_for,
    manifest_bytes,
    parse_manifest,
)

BOT = "b-1a2b3c4d"


# ----------------------------------------------------------------------------------------------------------- names
@pytest.mark.parametrize(
    "name, kind, key",
    [
        ("manifest.json", KIND_MANIFEST, None),
        ("settings.json", KIND_SETTINGS, None),
        ("providers.json", KIND_PROVIDERS, None),
        ("pricing.json", KIND_PRICING, None),
        ("secrets.enc", KIND_SECRETS, None),
        ("contract/contract.md", KIND_CONTRACT, None),
        ("contract/source.json", KIND_CONTRACT_SOURCE, None),
        ("rules/werewolf_12p/scenario.md", KIND_RULES_SCENARIO, "werewolf_12p"),
        ("rules/werewolf_12p/source.json", KIND_RULES_SOURCE, "werewolf_12p"),
        ("rules/werewolf_12p/materials/opening-notes.md", KIND_RULES_MATERIAL, "werewolf_12p"),
        ("rules/werewolf_12p/materials/opening-notes.json", KIND_RULES_MATERIAL_META, "werewolf_12p"),
        ("bots/lineup.json", KIND_BOTS_LINEUP, None),
        ("bots/usage.json", KIND_BOTS_USAGE, None),
        (f"bots/{BOT}/bot.json", KIND_BOT_FILE, BOT),
        (f"bots/{BOT}/core/identity.md", KIND_BOT_FILE, BOT),
        (f"bots/{BOT}/core/voice.md", KIND_BOT_FILE, BOT),
        (f"bots/{BOT}/core/mind.md", KIND_BOT_FILE, BOT),
        (f"bots/{BOT}/core/extra-catchphrase.md", KIND_BOT_FILE, BOT),
        (f"bots/{BOT}/arch/notes.md", KIND_BOT_FILE, BOT),
        (f"bots/{BOT}/rules/werewolf_12p/digest.md", KIND_BOT_FILE, BOT),
        (f"bots/{BOT}/rules/werewolf_12p/playbook.md", KIND_BOT_FILE, BOT),
        (f"bots/{BOT}/rules/werewolf_12p/questions.md", KIND_BOT_FILE, BOT),
        (f"bots/{BOT}/people.md", KIND_BOT_FILE, BOT),
        (f"bots/{BOT}/record.json", KIND_BOT_FILE, BOT),
        (f"bots/{BOT}/staging/p-1a2b3c4d.json", KIND_BOT_FILE, BOT),
        ("games/index.json", KIND_GAMES_INDEX, None),
        ("games/aaaa1111aaaa/game.json", KIND_GAME, "aaaa1111aaaa"),
        ("games/aaaa1111aaaa/game.prev.json", KIND_GAME_PREV, "aaaa1111aaaa"),
        ("logs/aaaa1111aaaa/trace-0001.jsonl", KIND_LOG, "aaaa1111aaaa"),
        ("logs/aaaa1111aaaa/trace-12345.jsonl", KIND_LOG, "aaaa1111aaaa"),
        ("logs/learning/init-20261118-0001.jsonl", KIND_LEARNING_LOG, None),
    ],
)
def test_every_name_of_format_2_has_its_kind(name: str, kind: str, key: str | None) -> None:
    assert classify(name, version=2) == (kind, key)
    assert classify(name) == (kind, key)  # (the zip is scanned before its manifest says which format it is)


@pytest.mark.parametrize(
    "name",
    [
        f"bots/{BOT}/core/extra-.md",  # (an empty slug)
        f"bots/{BOT}/core/extra-{'a' * 40}.md",  # (a slug longer than 32)
        f"bots/{BOT}/core/extra-Upper.md",
        f"bots/{BOT}/core/identity.md.bak",
        f"bots/{BOT}/core/sub/identity.md",
        f"bots/{BOT}/staging/P-1.json",
        f"bots/{BOT}/staging/p-1.md",
        f"bots/{BOT}/arch/other.md",
        f"bots/{BOT}/people.json",
        f"bots/{BOT}/rules/werewolf_12p/other.md",
        f"bots/{BOT}/rules/-bad/digest.md",  # (a scenario id starts with a letter or a digit)
        f"bots/{BOT}/rules/a/b/digest.md",
        f"bots/{BOT}/notes.txt",
        "bots/b-1A2B3C4D/bot.json",
        "bots/b-1a2b3c4/bot.json",
        "bots/b-1a2b3c4de/bot.json",
        "bots/bot.json",
        "bots/lineup.json.bak",
        "contract/old.md",
        "contract/contract.md/x",
        "rules/-bad/scenario.md",
        "rules/x/scenario.json",
        "rules/x/materials/y/z.md",
        "rules/x/materials/Bad.md",
        "rules/x/materials/.md",
        "logs/learning/x.txt",
        "logs/learning/sub/x.jsonl",
        "logs/learning/" + "a" * 65 + ".jsonl",
        "logs/x/other.txt",
        "games/g1/notes.txt",
        "games/g1/sub/game.json",
        "secrets.v1.enc",
        "master.v1.dpapi",
        ".lock",
        "backups/20260101-000000/settings.json",
        "exports/arena-20260101-000000.arenaagents",
        "tmp/x",
    ],
)
def test_everything_else_is_refused(name: str) -> None:
    with pytest.raises(ArchiveFormatError):
        classify(name)


@pytest.mark.parametrize(
    "name",
    ["CON", "con", "NUL", "Aux", "COM1", "LPT9", "learning", "LEARNING"],
)
def test_ids_that_cannot_be_a_folder_are_reserved(name: str) -> None:
    assert is_reserved_id(name)
    paths = [f"games/{name}/game.json", f"games/{name}/game.prev.json"]
    if name != "learning":  # (``logs/learning/trace-0001.jsonl`` is the log of a learning job called "trace-0001": that folder is not a game's)
        paths.append(f"logs/{name}/trace-0001.jsonl")
    for path in paths:
        with pytest.raises(ArchiveFormatError, match="保留"):
            classify(path)
    assert classify("logs/learning/trace-0001.jsonl")[0] == KIND_LEARNING_LOG


def test_other_ids_are_not_reserved() -> None:
    for name in ("aaaa1111aaaa", "learning2", "my-game", "CONSOLE", "COM10"):
        assert not is_reserved_id(name)


@pytest.mark.parametrize("scenario", ["CON", "nul", "COM3"])
def test_a_scenario_or_a_bots_memory_of_one_cannot_be_called_like_a_windows_device(scenario: str) -> None:
    for name in (
        f"rules/{scenario}/scenario.md",
        f"rules/{scenario}/source.json",
        f"rules/{scenario}/materials/a.md",
        f"bots/{BOT}/rules/{scenario}/playbook.md",
    ):
        with pytest.raises(ArchiveFormatError, match="保留"):
            classify(name)


def test_a_name_of_the_wrong_format_version_is_refused_with_that_reason() -> None:
    for name in ("roster/roster.json", "roster/N1.persona.md", "roster/N12.memory.md"):
        assert classify(name, version=1)[0] in (KIND_ROSTER_JSON, KIND_ROSTER_TEXT)
        with pytest.raises(ArchiveFormatError, match=r"格式版本（v2）"):
            classify(name, version=2)
    for name in ("contract/contract.md", "rules/x/scenario.md", "bots/lineup.json", f"bots/{BOT}/bot.json", "logs/learning/a.jsonl"):
        classify(name, version=2)
        with pytest.raises(ArchiveFormatError, match=r"格式版本（v1）"):
            classify(name, version=1)
    for name in ("settings.json", "providers.json", "games/g/game.json", "games/g/game.prev.json", "logs/g/trace-0001.jsonl", "secrets.enc"):
        classify(name, version=1)
        classify(name, version=2)


def test_the_kinds_of_each_version() -> None:
    assert SUPPORTED_VERSIONS == (1, 2) and FORMAT_VERSION == 2 and LEGACY_VERSION == 1
    v1, v2 = kinds_for(1), kinds_for(2)
    assert {KIND_ROSTER_JSON, KIND_ROSTER_TEXT} <= v1 and not {KIND_BOT_FILE, KIND_CONTRACT, KIND_RULES_SCENARIO, KIND_LEARNING_LOG} & v1
    assert {KIND_BOT_FILE, KIND_CONTRACT, KIND_RULES_SCENARIO, KIND_LEARNING_LOG, KIND_BOTS_LINEUP} <= v2
    assert not {KIND_ROSTER_JSON, KIND_ROSTER_TEXT} & v2
    assert {KIND_MANIFEST, KIND_SETTINGS, KIND_PROVIDERS, KIND_PRICING, KIND_GAME, KIND_GAME_PREV, KIND_LOG, KIND_SECRETS} <= v1 & v2


@pytest.mark.parametrize(
    "name, reason",
    [
        ("", "空的文件名"),
        ("/etc/passwd", "绝对路径"),
        ("C:/Windows/x", "绝对路径"),
        ("\\\\server\\share\\x", "绝对路径"),
        ("bots\\x", "反斜杠"),
        ("bots/../x", r"\.\."),
        ("./x", r"\.\."),
        ("bots//x", "空的部分"),
        ("bots/", "空的部分"),
        ("a\x00b", "不合法"),
        ("a\x1bb", "不合法"),
        ("x" * 300, "不合法"),
    ],
)
def test_a_member_name_must_first_be_a_safe_relative_path(name: str, reason: str) -> None:
    with pytest.raises(ArchiveFormatError, match=reason):
        check_member_name(name)
    with pytest.raises(ArchiveFormatError):
        classify(name)


def test_a_safe_name_passes_unchanged() -> None:
    for name in ("manifest.json", "a/b/c.md", "名字/文件.md"):
        assert check_member_name(name) == name
    with pytest.raises(ArchiveFormatError):
        check_member_name(None)
    with pytest.raises(ArchiveFormatError):
        check_member_name(3)


# -------------------------------------------------------------------------------------------------------- manifest
def _files() -> list[FileEntry]:
    return [
        FileEntry(path="settings.json", size=3, sha256=hashlib.sha256(b"{ }").hexdigest()),
        FileEntry(path="contract/contract.md", size=1, sha256=hashlib.sha256(b"x").hexdigest()),
        FileEntry(path=f"bots/{BOT}/bot.json", size=2, sha256=hashlib.sha256(b"{}").hexdigest()),
        FileEntry(path="rules/werewolf_12p/scenario.md", size=1, sha256=hashlib.sha256(b"y").hexdigest()),
    ]


def _contents(**changes: object) -> Contents:
    values: dict = {"games": 0, "providers": 0, "bots": 1, "contract": True, "rules": 1, "has_secrets": False, "has_logs": False}
    values.update(changes)
    return Contents(**values)


def _manifest_dict(**changes: object) -> dict:
    manifest = build_manifest(
        archive_id="0123456789abcdef0123456789abcdef",
        created_at="2026-11-18T00:42:00-08:00",
        app_version="v0.14.0",
        contents=_contents(),
        files=_files(),
    )
    data = json.loads(manifest_bytes(manifest))
    data.update(changes)
    return data


def _parse(data: dict):  # type: ignore[no-untyped-def]
    return parse_manifest(json.dumps(data).encode("utf-8"))


def test_a_format_2_manifest_round_trips() -> None:
    manifest = _parse(_manifest_dict())
    assert manifest.format_version == 2 and manifest.contents.to_manifest() == {
        "games": 0, "providers": 0, "bots": 1, "contract": True, "rules": 1, "has_secrets": False, "has_logs": False,
    }
    assert [f.path for f in manifest.files] == sorted(f.path for f in _files())


def test_the_contents_of_the_other_format_are_not_written() -> None:
    v2 = json.loads(manifest_bytes(build_manifest(
        archive_id="0123456789abcdef0123456789abcdef", created_at="2026-11-18T00:42:00-08:00", app_version="v0.14.0", contents=_contents(), files=_files(),
    )))
    assert "roster_seats" not in v2["contents"] and {"bots", "contract", "rules"} <= set(v2["contents"])
    v1 = json.loads(manifest_bytes(build_manifest(
        archive_id="0123456789abcdef0123456789abcdef", created_at="2026-11-18T00:42:00-08:00", app_version="v0.13.0",
        contents=Contents(games=0, providers=0, roster_seats=2, has_secrets=False, has_logs=False),
        files=[FileEntry(path="settings.json", size=3, sha256=hashlib.sha256(b"{ }").hexdigest())], format_version=1,
    )))
    assert v1["contents"] == {"games": 0, "providers": 0, "roster_seats": 2, "has_secrets": False, "has_logs": False}


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d["contents"].update(roster_seats=2), "不符合格式"),  # (the counts of format 1 in a format 2 manifest)
        (lambda d: d["contents"].pop("bots"), "不符合格式"),
        (lambda d: d["contents"].pop("contract"), "不符合格式"),
        (lambda d: d["contents"].pop("rules"), "不符合格式"),
        (lambda d: d["contents"].update(bots=-1), "不符合格式"),
        (lambda d: d["contents"].update(bots=2), "内容统计"),
        (lambda d: d["contents"].update(rules=0), "内容统计"),
        (lambda d: d["contents"].update(contract=False), "内容统计"),
        (lambda d: d["contents"].update(has_logs=True), "内容统计"),
        (lambda d: d["contents"].update(has_secrets=True), "内容统计"),
        (lambda d: d.update(format_version=0), "格式版本无效"),
        (lambda d: d.update(format_version=True), "格式版本无效"),
        (lambda d: d.update(format_version="2"), "格式版本无效"),
        (lambda d: d["files"].append({"path": "roster/roster.json", "size": 1, "sha256": "0" * 64}), "格式版本"),
        (lambda d: d["files"].append({"path": f"bots/{BOT}/evil.sh", "size": 1, "sha256": "0" * 64}), "不认识"),
    ],
)
def test_a_format_2_manifest_that_does_not_fit_its_files_is_refused(mutate, message: str) -> None:  # type: ignore[no-untyped-def]
    data = _manifest_dict()
    mutate(data)
    data["files_sha256"] = hashlib.sha256(b"unused").hexdigest() if message == "格式版本无效" else _digest_of(data)
    with pytest.raises(ArchiveFormatError, match=message):
        _parse(data)


def _digest_of(data: dict) -> str:
    """``files_sha256`` of the (possibly changed) file list, so that the check under test is the one that fails, not the digest."""
    from app.archive.format import files_digest

    try:
        return files_digest(data["files"])
    except (KeyError, TypeError, ValueError):
        return "0" * 64


def test_a_format_1_manifest_with_the_counts_of_format_2_is_refused() -> None:
    data = _manifest_dict(format_version=1)  # (bots / contract / rules in a manifest that says it is format 1)
    with pytest.raises(ArchiveFormatError, match="不符合格式"):
        _parse(data)


def test_a_format_1_manifest_cannot_list_a_file_of_format_2() -> None:
    manifest = build_manifest(
        archive_id="0123456789abcdef0123456789abcdef",
        created_at="2026-11-18T00:42:00-08:00",
        app_version="v0.13.0",
        contents=Contents(games=0, providers=0, roster_seats=0, has_secrets=False, has_logs=False),
        files=[FileEntry(path="contract/contract.md", size=1, sha256=hashlib.sha256(b"x").hexdigest())],
        format_version=1,
    )
    with pytest.raises(ArchiveFormatError, match="格式版本（v1）"):
        parse_manifest(manifest_bytes(manifest))


def test_a_manifest_newer_than_this_program_is_reported_as_such() -> None:
    data = _manifest_dict(format_version=FORMAT_VERSION + 1)
    data["files"] = "whatever a newer program writes"
    with pytest.raises(ArchiveVersionError, match=f"v{FORMAT_VERSION + 1}.*只支持到 v{FORMAT_VERSION}"):
        _parse(data)
