"""The format 2 archive, export side (v0.14: docs/v0.14-plan.md §2.5, contract 13.9).

An export holds EVERY whitelisted file of the data directory - the settings, the contract, the rules and their study materials, the line-up,
every bot with every kind of file a bot has, the games with their previous copies, every log and the learning logs - and nothing that is
machine-bound, temporary, no longer used (the roster of v0.13) or unknown.  The check is made file by file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.archive import kdf
from app.archive.errors import ArchiveError
from app.archive.export import ExportOptions, export_archive
from app.archive.format import (
    EXPORT_NAME_RE,
    FORMAT_VERSION,
    KIND_MANIFEST,
    KIND_SECRETS,
    LEGACY_VERSION,
    classify,
    download_name,
    files_digest,
    kinds_for,
    parse_manifest,
)
from app.storage.bots import iter_bot_files

from .helpers import (
    GAME_A,
    GAME_B,
    KEY_1,
    KEY_2,
    LEARNING_JOBS,
    MATERIAL_SLUGS,
    PASSWORD,
    PROVIDER_1,
    PROVIDER_2,
    FakeSecrets,
    dumps,
    members_of,
    populate_v2,
)

NOW = datetime(2026, 11, 18, 0, 42, 7, tzinfo=timezone(timedelta(hours=-8)))

#: The forms of the files of ONE bot (what a bot may have): every one has to be in the export of the bot that has them all.
BOT_FILE_FORMS = [
    r"bot\.json",
    r"core/identity\.md",
    r"core/voice\.md",
    r"core/mind\.md",
    r"core/extra-[a-z0-9-]+\.md",
    r"arch/notes\.md",
    r"rules/[A-Za-z0-9_-]+/digest\.md",
    r"rules/[A-Za-z0-9_-]+/playbook\.md",
    r"rules/[A-Za-z0-9_-]+/questions\.md",
    r"people\.md",
    r"record\.json",
    r"staging/[a-z0-9-]+\.json",
]


def _export(data_dir: Path, **options: object) -> tuple[dict[str, bytes], bytes, object]:
    result = export_archive(data_dir, ExportOptions(**options), now=NOW)  # type: ignore[arg-type]
    members = members_of(result.path)
    manifest = members.pop("manifest.json")
    return members, manifest, result


# ------------------------------------------------------------------------------------------------ everything is in
def test_the_fixture_has_a_file_of_every_kind_the_format_allows(data_dir: Path) -> None:
    """(If the whitelist grows, this fails until the fixture - and so the export test below - covers the new kind.)"""
    expected = populate_v2(data_dir)
    kinds = {classify(name, version=FORMAT_VERSION)[0] for name in expected}
    assert kinds == kinds_for(FORMAT_VERSION) - {KIND_MANIFEST, KIND_SECRETS}
    bot_files = [name.split("/", 2)[2] for name in expected if re.fullmatch(r"bots/b-[0-9a-f]{8}/.+", name)]
    for form in BOT_FILE_FORMS:
        assert any(re.fullmatch(form, rel) for rel in bot_files), f"no bot has a {form}"


def test_an_export_has_every_whitelisted_file_byte_for_byte_and_nothing_else(data_dir: Path) -> None:
    expected = populate_v2(data_dir)
    members, _manifest, result = _export(data_dir)
    missing = sorted(set(expected) - set(members))
    assert not missing, f"left out of the export: {missing}"
    extra = sorted(set(members) - set(expected))
    assert not extra, f"in the export but not whitelisted: {extra}"
    for name, data in expected.items():  # one by one
        assert members[name] == data, name
    assert result.file_count == len(expected) and result.uncompressed_bytes == sum(len(data) for data in expected.values())  # type: ignore[attr-defined]


def test_the_manifest_of_format_2_counts_bots_contract_and_rules(data_dir: Path) -> None:
    expected = populate_v2(data_dir)
    members, raw, result = _export(data_dir)
    manifest = parse_manifest(raw)
    assert manifest.format_version == 2 == FORMAT_VERSION and manifest.app_version.startswith("v")
    assert manifest.created_at == "2026-11-18T00:42:07-08:00"
    wanted = {"games": 2, "providers": 2, "bots": 12, "contract": True, "rules": 1, "has_secrets": False, "has_logs": True}
    assert manifest.contents.to_manifest() == wanted == json.loads(raw)["contents"] == result.contents  # type: ignore[attr-defined]
    assert "roster_seats" not in json.loads(raw)["contents"]
    assert {f.path: f.sha256 for f in manifest.files} == {n: hashlib.sha256(d).hexdigest() for n, d in expected.items()}
    assert {f.path: f.size for f in manifest.files} == {n: len(d) for n, d in expected.items()}
    assert manifest.files_sha256 == files_digest(manifest.files)


def test_what_never_goes_into_a_format_2_archive(data_dir: Path) -> None:
    populate_v2(data_dir)
    members, _raw, _result = _export(data_dir)
    names = set(members)
    for never in (
        "secrets.v1.enc",
        "master.v1.dpapi",
        ".lock",
        "settings.json.bak",
        "roster/roster.json",  # (the roster of v0.13 became the bots)
        "roster/N1.persona.md",
        "bots/stray.txt",
        "contract/old.md",
        "games/stray.txt",
        "logs/learning/readme.txt",
        "logs/learning/sub/deep.jsonl",
    ):
        assert never not in names, never
    assert not [n for n in names if n.startswith(("backups/", "exports/", "tmp/", "roster/"))]
    assert not [n for n in names if n.endswith((".txt", ".part", ".bak"))]
    assert not [n for n in names if re.search(r"b-0000zzzz|Bad_Slug|/other\.md|/readme\.txt|/notes\.txt", n)]


def test_every_bot_of_the_table_is_exported_with_exactly_the_files_the_bot_store_lists(data_dir: Path) -> None:
    """Two independent lists of "what is a bot's file" - the store's (``iter_bot_files``) and the archive's whitelist - must agree."""
    from app.storage.bots import BotStore

    populate_v2(data_dir)
    members, _raw, _result = _export(data_dir)
    store = BotStore(data_dir)
    assert len(store.lineup()) == 12
    for bot_id in store.lineup():
        stored = {rel for rel, _path in iter_bot_files(store.bot_dir(bot_id))}
        packed = {name.split("/", 2)[2] for name in members if name.startswith(f"bots/{bot_id}/")}
        assert packed == stored, bot_id
    assert "bots/lineup.json" in members and "bots/usage.json" in members


def test_the_contract_the_rules_and_the_materials_are_exported_with_where_they_came_from(data_dir: Path) -> None:
    expected = populate_v2(data_dir)
    members, _raw, _result = _export(data_dir)
    sid = next(name.split("/")[1] for name in members if name.endswith("/scenario.md"))
    assert members["contract/contract.md"].startswith(b"---")  # (the front matter of the official contract)
    assert json.loads(members["contract/source.json"])["origin"] == "default"
    assert json.loads(members[f"rules/{sid}/source.json"])["origin"] == "default"
    for slug in MATERIAL_SLUGS:
        assert f"rules/{sid}/materials/{slug}.md" in members
    assert json.loads(members[f"rules/{sid}/materials/{MATERIAL_SLUGS[0]}.json"])["title"] == "开局笔记"  # (the sidecar with an explicit title)
    assert f"rules/{sid}/materials/{MATERIAL_SLUGS[1]}.json" not in members and f"rules/{sid}/materials/{MATERIAL_SLUGS[1]}.json" not in expected


def test_the_logs_are_always_part_of_it(data_dir: Path) -> None:
    """H36: ``include_logs`` is still accepted but ignored."""
    populate_v2(data_dir)
    for include_logs in (True, False):
        members, raw, _result = _export(data_dir, include_logs=include_logs)
        assert {n for n in members if n.startswith("logs/")} == {
            f"logs/{GAME_A}/trace-0001.jsonl",
            f"logs/{GAME_A}/trace-0002.jsonl",
            f"logs/{GAME_B}/trace-0001.jsonl",
            f"logs/{GAME_B}/trace-0002.jsonl",
            *(f"logs/learning/{job}.jsonl" for job in LEARNING_JOBS),
        }
        assert json.loads(raw)["contents"]["has_logs"] is True


def test_the_logs_of_a_game_that_could_not_be_exported_are_left_out_with_it(data_dir: Path) -> None:
    populate_v2(data_dir)
    (data_dir / "games" / GAME_B / "game.json").write_bytes(b"not json at all")  # (and it has no previous copy)
    members, _raw, result = _export(data_dir)
    assert f"games/{GAME_B}/game.json" not in members and not [n for n in members if n.startswith(f"logs/{GAME_B}/")]
    assert any(GAME_B in warning for warning in result.warnings)  # type: ignore[attr-defined]
    assert f"logs/{GAME_A}/trace-0001.jsonl" in members and any(n.startswith("logs/learning/") for n in members)


# ---------------------------------------------------------------------------------------- the previous copy of a game
def test_the_previous_copy_of_a_game_is_part_of_the_archive(data_dir: Path) -> None:
    expected = populate_v2(data_dir)
    members, _raw, result = _export(data_dir)
    assert members[f"games/{GAME_A}/game.prev.json"] == expected[f"games/{GAME_A}/game.prev.json"]
    assert f"games/{GAME_B}/game.prev.json" not in members  # (game B never had one)
    assert not result.warnings  # type: ignore[attr-defined]


def test_a_damaged_previous_copy_is_left_out_with_a_warning_and_the_export_goes_on(data_dir: Path) -> None:
    expected = populate_v2(data_dir)
    (data_dir / "games" / GAME_A / "game.prev.json").write_bytes(b"{ broken")
    members, _raw, result = _export(data_dir)
    assert f"games/{GAME_A}/game.prev.json" not in members
    assert members[f"games/{GAME_A}/game.json"] == expected[f"games/{GAME_A}/game.json"]
    assert any("备份副本" in warning and GAME_A in warning for warning in result.warnings)  # type: ignore[attr-defined]


def test_a_damaged_game_file_is_replaced_by_its_previous_copy_and_no_second_copy_is_packed(data_dir: Path) -> None:
    expected = populate_v2(data_dir)
    (data_dir / "games" / GAME_A / "game.json").write_bytes(b"\x00\x01 not a game")
    members, _raw, result = _export(data_dir)
    assert members[f"games/{GAME_A}/game.json"] == expected[f"games/{GAME_A}/game.prev.json"]
    assert f"games/{GAME_A}/game.prev.json" not in members  # (the copy that stands in is not packed twice)
    assert any("主文件已损坏" in warning for warning in result.warnings)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------------------------------------- strays
@pytest.mark.skipif(os.name == "nt", reason="creating symbolic links needs a privilege on Windows")
def test_symbolic_links_are_never_followed_inside_the_bot_folders(data_dir: Path, tmp_path: Path) -> None:  # pragma: no cover - POSIX only
    expected = populate_v2(data_dir)
    outside = tmp_path / "secret.md"
    outside.write_text("do not take me along", encoding="utf-8")
    bot = next(name.split("/")[1] for name in expected if re.fullmatch(r"bots/b-[0-9a-f]{8}/bot\.json", name))
    link = data_dir / "bots" / bot / "core" / "extra-linked.md"
    link.symlink_to(outside)
    members, _raw, result = _export(data_dir)
    assert f"bots/{bot}/core/extra-linked.md" not in members and "do not take me along".encode() not in b"".join(members.values())
    assert any("符号链接" in warning for warning in result.warnings)  # type: ignore[attr-defined]


def test_a_bot_file_that_is_not_text_is_skipped_with_a_warning(data_dir: Path) -> None:
    expected = populate_v2(data_dir)
    bot = next(name.split("/")[1] for name in expected if name.endswith("/people.md"))
    (data_dir / "bots" / bot / "people.md").write_bytes(b"\xff\xfe\x00 not utf-8")
    members, _raw, result = _export(data_dir)
    assert f"bots/{bot}/people.md" not in members
    assert any("people.md" in warning for warning in result.warnings)  # type: ignore[attr-defined]
    assert f"bots/{bot}/bot.json" in members


# ------------------------------------------------------------------------------------------------------- the keys
def test_the_keys_are_sealed_exactly_as_in_v013_whatever_the_archive_format(data_dir: Path) -> None:
    """"API Key 沿用 v0.13 的规范": optional, password, only the keys encrypted - the blob has its own version (1), not the archive's (2)."""
    populate_v2(data_dir)
    result = export_archive(
        data_dir, ExportOptions(include_keys=True, password=PASSWORD), secrets=FakeSecrets({PROVIDER_1: KEY_1, PROVIDER_2: KEY_2}), now=NOW
    )
    members = members_of(result.path)
    manifest = parse_manifest(members.pop("manifest.json"))
    blob = members.pop("secrets.enc")
    assert manifest.format_version == 2 and manifest.contents.has_secrets is True
    assert json.loads(blob)["version"] == kdf.SECRETS_FORMAT_VERSION == 1
    assert kdf.open_secrets(blob, PASSWORD, manifest.archive_id) == {PROVIDER_1: KEY_1, PROVIDER_2: KEY_2}
    assert b"sk-test" not in b"".join(members.values()), "no key anywhere else in the archive"


def test_an_archive_without_the_keys_has_no_secrets_member(data_dir: Path) -> None:
    populate_v2(data_dir)
    members, raw, _result = _export(data_dir)
    assert "secrets.enc" not in members and json.loads(raw)["contents"]["has_secrets"] is False


# ----------------------------------------------------------------------------------------------------- the versions
def test_a_data_directory_that_is_still_a_v013_one_is_written_as_format_1(data_dir: Path) -> None:
    from .helpers import populate

    populate(data_dir)  # (a roster, no bots)
    members, raw, _result = _export(data_dir)
    assert json.loads(raw)["format_version"] == 1 and "roster/roster.json" in members
    assert not [n for n in members if n.startswith(("bots/", "contract/", "rules/"))]
    assert set(json.loads(raw)["contents"]) == {"games", "providers", "roster_seats", "has_secrets", "has_logs"}


def test_a_directory_without_the_folders_the_application_makes_is_a_v013_one_even_without_a_roster(data_dir: Path) -> None:
    """What the conversion tool for v0.12.1 workspaces stages may have no roster at all: it is a format 1 archive all the same, and its logs
    stay optional (``--no-logs``)."""
    from .helpers import game_payload

    (data_dir / "games" / GAME_A).mkdir(parents=True)
    (data_dir / "games" / GAME_A / "game.json").write_bytes(dumps(game_payload(GAME_A)))
    (data_dir / "logs" / GAME_A).mkdir(parents=True)
    (data_dir / "logs" / GAME_A / "trace-0001.jsonl").write_bytes(b"{}\n")
    members, raw, _result = _export(data_dir, include_logs=False)
    assert json.loads(raw)["format_version"] == 1 and json.loads(raw)["contents"]["has_logs"] is False
    assert set(members) == {f"games/{GAME_A}/game.json"}
    members, raw, _result = _export(data_dir, include_logs=True)
    assert f"logs/{GAME_A}/trace-0001.jsonl" in members and json.loads(raw)["contents"]["roster_seats"] == 0


def test_any_one_of_the_folders_the_application_makes_makes_it_a_format_2_directory(data_dir: Path) -> None:
    for index, folder in enumerate(("bots", "contract", "rules")):
        root = data_dir / f"d{index}"
        (root / folder).mkdir(parents=True)
        (root / "settings.json").write_bytes(dumps({"schema_version": 2}))
        _members, raw, _result = _export(root)
        assert json.loads(raw)["format_version"] == 2, folder


def test_format_1_can_still_be_asked_for_and_then_it_is_the_roster_that_is_packed(data_dir: Path) -> None:
    """(What the conversion tool for v0.12.1 workspaces makes: it stages a roster and no bots.)"""
    populate_v2(data_dir)
    members, raw, _result = _export(data_dir, format_version=LEGACY_VERSION, include_logs=False)
    manifest = json.loads(raw)
    assert manifest["format_version"] == 1 and manifest["contents"]["has_logs"] is False  # (the logs are optional in format 1)
    assert "roster/roster.json" in members and not [n for n in members if n.startswith(("bots/", "contract/", "rules/", "logs/"))]
    assert f"games/{GAME_A}/game.json" in members and f"games/{GAME_A}/game.prev.json" not in members
    assert "roster_seats" in manifest["contents"] and "bots" not in manifest["contents"]


def test_an_unknown_format_version_cannot_be_asked_for(data_dir: Path) -> None:
    populate_v2(data_dir)
    with pytest.raises(ArchiveError) as excinfo:
        export_archive(data_dir, ExportOptions(format_version=3), now=NOW)
    assert excinfo.value.status == 400
    assert not list((data_dir / "exports").glob("arena-20261118-*")), "nothing was written"


# ----------------------------------------------------------------------------------------------- names (H42)
def test_the_browser_saves_an_export_as_arena_agents_time(data_dir: Path) -> None:
    """H42: the user's file is ``arena-agents-YYYYmmdd-HHMMSS.arenaagents``; the file in ``exports/`` keeps its fixed internal name."""
    populate_v2(data_dir)
    _members, _raw, result = _export(data_dir)
    assert EXPORT_NAME_RE.fullmatch(result.name)  # type: ignore[attr-defined]
    assert download_name(result.name) == "arena-agents-20261118-004207.arenaagents"  # type: ignore[attr-defined]
    for other in ("arena-agents-20261118-004207.arenaagents", "notes.txt", "arena-2026.arenaagents", "../arena-20261118-004207.arenaagents"):
        assert download_name(other) == other
