"""The format 2 archive, import side (v0.14: docs/v0.14-plan.md §2.5, contract 13.9; H45, H59).

* a format 2 archive replaces the whole data directory - bots, contract, rules, games, logs - and every file comes back byte for byte;
* the bots it brings must be a table that works (``BotStore.check_integrity``): a line-up that names a missing bot, has the wrong number, repeats
  a bot or a name is refused *before* anything moves; a bot whose texts are merely over a limit is imported with a warning;
* a format 1 archive (v0.13) is still read: its roster becomes bots (``BotStore.import_legacy_roster``) after ``settings.json`` and ``roster/``
  have landed, and the default bindings saved by seat move to the bots;
* a failed import puts everything back; the API keys behave exactly as in v0.13.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.archive import importer
from app.archive.errors import ArchiveError, ArchiveFormatError, ArchiveIOError, ArchivePasswordError, ArchiveVersionError
from app.archive.export import ExportOptions, export_archive
from app.archive.importer import KEEP_IN_PLACE, LEGACY_ROSTER_NOTE
from app.providers.models import ModelBinding
from app.storage.bots import BotStore
from app.storage.contract import ContractStore
from app.storage.games import GameStore
from app.storage.rules import RulesStore

from .helpers import (
    GAME_A,
    GAME_B,
    KEY_1,
    KEY_2,
    LEARNING_JOBS,
    PASSWORD,
    PROVIDER_1,
    PROVIDER_2,
    FakeSecrets,
    FileSecrets,
    dumps,
    game_payload,
    members_of,
    populate,
    populate_v2,
    tree,
    write_zip,
)

NOW = datetime(2026, 11, 18, 0, 42, 7).astimezone()
LOCAL_GAME = "cccc3333cccc"
LOCAL_PROVIDER = "prov-local"


def _no_caches() -> None:
    return None


@pytest.fixture
def v2(data_dir: Path, tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    """A format 2 archive of a fully populated data directory (``data_dir``), and what is in it (path -> bytes)."""
    expected = populate_v2(data_dir)
    path = tmp_path / "v2.arenaagents"
    path.write_bytes(export_archive(data_dir, ExportOptions(), now=NOW).path.read_bytes())
    return path, expected


@pytest.fixture
def target(tmp_path: Path) -> Path:
    """Another machine's data directory (v0.14): other bots, other games, a local provider - all of it to be replaced."""
    directory = tmp_path / "target"
    directory.mkdir()
    populate_v2(directory, games=(LOCAL_GAME,), providers=(LOCAL_PROVIDER, PROVIDER_1), last_session=LOCAL_GAME)
    FileSecrets(directory).write_all({LOCAL_PROVIDER: "sk-local-aaaaaaaaaaaa", PROVIDER_1: "sk-local-bbbbbbbbbbbb"})
    return directory


def _data(root: Path) -> dict[str, bytes]:
    """The user's data under ``root`` (what an import replaces), without the machine's key file."""
    return {
        name: data
        for name, data in tree(root).items()
        if name.split("/", 1)[0] not in KEEP_IN_PLACE and name != "secrets.v1.enc"
    }


def _run(path: Path, target: Path, **kwargs: Any) -> importer.ImportResult:
    kwargs.setdefault("secrets", FileSecrets(target))
    kwargs.setdefault("clear_caches", _no_caches)
    kwargs.setdefault("now", NOW)
    return importer.apply(path, data_dir=target, **kwargs)


def _archive(tmp_path: Path, members: dict[str, bytes], name: str = "made.arenaagents", **kwargs: Any) -> Path:
    return write_zip(tmp_path / name, members, **kwargs)


# --------------------------------------------------------------------------------------------------------- round trip
def test_a_round_trip_into_an_empty_data_directory_gives_back_every_file_byte_for_byte(
    v2: tuple[Path, dict[str, bytes]], tmp_path: Path
) -> None:
    path, expected = v2
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    result = _run(path, fresh, secrets=FakeSecrets())
    after = {name: data for name, data in tree(fresh).items() if name != "tmp/"}
    assert after == expected, "every whitelisted file is back, and nothing else"
    assert (result.games, result.providers, result.backup) == (2, 2, None)


def test_the_stores_read_what_an_import_brought(data_dir: Path, v2: tuple[Path, dict[str, bytes]], tmp_path: Path) -> None:
    path, _expected = v2
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _run(path, fresh, secrets=FakeSecrets())
    sources, imported = BotStore(data_dir), BotStore(fresh)
    assert imported.lineup() == sources.lineup() and len(imported.lineup()) == 12
    assert imported.check_integrity() == []
    assert imported.names() == sources.names()
    first = imported.lineup()[0]
    assert imported.core_markdown(first) == sources.core_markdown(first) and "附加：口头禅" in imported.core_markdown(first)
    assert len(imported.staging(first)) == 1
    assert ContractStore(fresh).current().sha256 == ContractStore(data_dir).current().sha256
    rules = RulesStore(fresh)
    assert rules.ids() == RulesStore(data_dir).ids() and len(rules.material_infos(rules.ids()[0])) == 2
    games = GameStore(fresh)
    assert sorted(item["session_id"] for item in games.list_games()) == sorted([GAME_A, GAME_B])
    assert (fresh / "games" / GAME_A / "game.prev.json").is_file(), "the previous copy of a game comes back as well"
    assert sorted(p.name for p in (fresh / "logs" / "learning").iterdir()) == sorted(f"{job}.jsonl" for job in LEARNING_JOBS)


def test_an_import_replaces_everything_and_the_old_data_is_in_the_backup(
    v2: tuple[Path, dict[str, bytes]], target: Path
) -> None:
    path, expected = v2
    old_lineup = (target / "bots" / "lineup.json").read_bytes()
    old_contract = (target / "contract" / "contract.md").read_bytes()
    result = _run(path, target, skip_keys=True)
    after = {name: data for name, data in _data(target).items() if not name.startswith("backups/")}
    assert after == expected, "nothing of the old bots, games or logs is left in the data directory"
    assert result.backup is not None
    backup = target / "backups" / result.backup
    assert (backup / "bots" / "lineup.json").read_bytes() == old_lineup and (backup / "games" / LOCAL_GAME / "game.json").is_file()
    assert (backup / "contract" / "contract.md").read_bytes() == old_contract
    assert (result.games, result.providers) == (2, 2)


# ----------------------------------------------------------------------------------------------------------- summaries
def test_the_summary_of_a_format_2_archive_counts_bots_contract_and_rules(v2: tuple[Path, dict[str, bytes]]) -> None:
    path, _expected = v2
    summary = importer.inspect(path)
    assert summary.format_version == 2 and summary.app_version.startswith("v")
    assert (summary.games, summary.providers, summary.bots, summary.contract, summary.rules) == (2, 2, 12, True, 1)
    assert summary.roster_seats is None and summary.has_logs is True and summary.has_secrets is False and summary.warnings == []
    body = summary.to_dict()
    assert {"bots", "contract", "rules"} <= set(body) and "roster_seats" not in body
    assert (body["bots"], body["contract"], body["rules"]) == (12, True, 1)


def test_the_summary_of_a_v013_archive_says_the_roster_becomes_bots(data_dir: Path) -> None:
    populate(data_dir)
    path = export_archive(data_dir, ExportOptions(), now=NOW).path
    summary = importer.inspect(path)
    assert summary.format_version == 1 and summary.roster_seats == 2 and summary.bots is None and summary.contract is None
    body = summary.to_dict()
    assert body["roster_seats"] == 2 and not {"bots", "contract", "rules"} & set(body)
    assert summary.warnings == [LEGACY_ROSTER_NOTE.format(seats=2)]
    assert "名册里的 2 个座位会在导入时迁移成 bot" in summary.warnings[0]


# ------------------------------------------------------------------------------------------------- a v0.13 archive
def test_the_roster_of_a_v013_archive_becomes_the_table_of_bots_and_the_bindings_follow(data_dir: Path, tmp_path: Path) -> None:
    populate(data_dir)
    binding = ModelBinding(provider_id=PROVIDER_1, model_id="model-x").model_dump(mode="json")
    settings = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    settings["default_bindings"] = {"N1": binding, "moderator": binding}  # (saved by seat, as v0.13 did)
    (data_dir / "settings.json").write_bytes(dumps(settings))
    path = tmp_path / "v013.arenaagents"
    path.write_bytes(export_archive(data_dir, ExportOptions(), now=NOW).path.read_bytes())

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _run(path, fresh, secrets=FakeSecrets())
    store = BotStore(fresh)
    lineup = store.lineup()
    assert len(lineup) == 12 and store.check_integrity() == []
    assert [store.name_of(bot_id) for bot_id in lineup[:2]] == ["Alice", "Bob"], "the names of the roster, by seat"
    assert (fresh / "roster" / "roster.json").is_file(), "the old roster stays where it was"
    saved = json.loads((fresh / "settings.json").read_text(encoding="utf-8"))["default_bindings"]
    assert saved[lineup[0]] == binding and "N1" not in saved and saved["moderator"] == binding, "the binding of seat N1 is Alice's now"


def test_a_v013_archive_without_a_roster_imports_and_the_application_makes_the_bots_when_it_reloads(data_dir: Path, tmp_path: Path) -> None:
    populate(data_dir)
    exported = members_of(export_archive(data_dir, ExportOptions(), now=NOW).path)
    members = {name: data for name, data in exported.items() if not name.startswith("roster/") and name != "manifest.json"}
    path = _archive(tmp_path, members, version=1)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _run(path, fresh, secrets=FakeSecrets())
    assert not (fresh / "bots").exists() and (fresh / "games" / GAME_A / "game.json").is_file()


# ------------------------------------------------------------------------------------------- a table that must work
def _lineup(members: dict[str, bytes]) -> dict[str, Any]:
    return json.loads(members["bots/lineup.json"])


def _bot_ids(members: dict[str, bytes]) -> list[str]:
    return [name.split("/")[1] for name in sorted(members) if name.endswith("/bot.json") and name.startswith("bots/b-")]


def _with_lineup(members: dict[str, bytes], order: list[str]) -> dict[str, bytes]:
    lineup = _lineup(members)
    lineup["order"] = order
    return {**members, "bots/lineup.json": dumps(lineup)}


def _same_name(members: dict[str, bytes]) -> dict[str, bytes]:
    ids = _lineup(members)["order"]
    bot = json.loads(members[f"bots/{ids[1]}/bot.json"])
    bot["name"] = json.loads(members[f"bots/{ids[0]}/bot.json"])["name"].upper()  # (names are compared without case)
    return {**members, f"bots/{ids[1]}/bot.json": dumps(bot)}


def _without_lineup(members: dict[str, bytes]) -> dict[str, bytes]:
    return {name: data for name, data in members.items() if name != "bots/lineup.json"}


@pytest.mark.parametrize(
    "damage, message",
    [
        pytest.param(lambda m: _with_lineup(m, ["b-deadbeef", *_lineup(m)["order"][1:]]), "没有对应的 bot 目录", id="names-a-missing-bot"),
        pytest.param(lambda m: _with_lineup(m, _lineup(m)["order"][:-1]), "应该是 12 个", id="eleven-bots"),
        pytest.param(lambda m: _with_lineup(m, [_lineup(m)["order"][0], *_lineup(m)["order"][:-1]]), "重复", id="a-bot-twice"),
        pytest.param(_same_name, "重名", id="a-name-twice"),
        pytest.param(_without_lineup, "缺少阵容文件", id="no-lineup"),
    ],
)
def test_a_table_that_does_not_work_is_refused_before_anything_moves(
    v2: tuple[Path, dict[str, bytes]], target: Path, tmp_path: Path, damage: Any, message: str
) -> None:
    _path, expected = v2
    path = _archive(tmp_path, damage(dict(expected)))
    importer.inspect(path)  # (the structure of the archive itself is fine: it is the bots that do not work)
    before = tree(target)
    with pytest.raises(ArchiveFormatError, match=message) as excinfo:
        _run(path, target, skip_keys=True)
    assert excinfo.value.code == "invalid_archive" and excinfo.value.status == 422 and "玩家（bot）数据有问题" in excinfo.value.message
    assert tree(target) == before, "nothing was moved, no backup and no unpacked copy was left behind"


def test_a_bot_whose_texts_are_over_a_limit_is_imported_with_a_warning(
    v2: tuple[Path, dict[str, bytes]], target: Path, tmp_path: Path
) -> None:
    """Somebody edited a file by hand: the application tolerates it and the start check says what to fix - no reason to refuse the archive."""
    _path, expected = v2
    first = _lineup(expected)["order"][0]
    members = {**expected, f"bots/{first}/core/identity.md": ("很长的设定。" * 20_000).encode("utf-8")}
    path = _archive(tmp_path, members)
    result = _run(path, target, skip_keys=True)
    assert (target / "bots" / first / "core" / "identity.md").is_file()
    assert any("字" in warning for warning in result.warnings), result.warnings


def test_an_archive_with_no_bots_at_all_is_fine_the_default_bots_are_made_on_reload(
    v2: tuple[Path, dict[str, bytes]], target: Path, tmp_path: Path
) -> None:
    _path, expected = v2
    members = {name: data for name, data in expected.items() if not name.startswith("bots/")}
    path = _archive(tmp_path, members, version=2)
    assert importer.inspect(path).bots == 0
    _run(path, target, skip_keys=True)
    assert not (target / "bots").exists() and (target / "contract" / "contract.md").is_file()


# ------------------------------------------------------------------------------------------------- what is refused
@pytest.mark.parametrize(
    "extra",
    [
        pytest.param("roster/roster.json", id="a-roster-in-format-2"),
        pytest.param("roster/N1.persona.md", id="a-roster-text-in-format-2"),
        pytest.param("bots/b-1a2b3c4d/evil.sh", id="a-script-in-a-bot"),
        pytest.param("bots/b-1a2b3c4d/core/Identity.md", id="a-name-by-case"),
        pytest.param("bots/b-1a2b3c4d/rules/CON/playbook.md", id="a-windows-device-name"),
        pytest.param("bots/B-1A2B3C4D/bot.json", id="an-uppercase-bot-id"),
        pytest.param("bots/b-1a2b3c4d/../../settings.json", id="a-path-that-leaves-the-bot"),
        pytest.param("contract/other.md", id="a-second-contract-file"),
        pytest.param("rules/CON/scenario.md", id="a-scenario-called-con"),
        pytest.param("rules/x/materials/Bad.md", id="a-bad-material-slug"),
        pytest.param("logs/learning/x.txt", id="a-learning-log-that-is-not-jsonl"),
        pytest.param("games/learning/game.json", id="a-game-called-learning"),
        pytest.param("games/g1/notes.txt", id="a-note-in-a-game"),
    ],
)
def test_a_name_the_format_does_not_allow_is_refused(v2: tuple[Path, dict[str, bytes]], tmp_path: Path, extra: str) -> None:
    _path, expected = v2
    path = _archive(tmp_path, {**expected, extra: b"x"}, version=2)
    with pytest.raises(ArchiveFormatError):
        importer.inspect(path)


def test_a_file_of_format_2_does_not_belong_in_a_format_1_archive(data_dir: Path, tmp_path: Path) -> None:
    members = populate(data_dir)
    for name in ("bots/lineup.json", "bots/usage.json", "contract/contract.md", "rules/x/scenario.md", "logs/learning/a.jsonl"):
        with pytest.raises(ArchiveFormatError, match="格式版本"):
            importer.inspect(_archive(tmp_path, {**members, name: b"{}"}, version=1))


def test_the_counts_of_the_manifest_must_match_the_files(v2: tuple[Path, dict[str, bytes]], tmp_path: Path) -> None:
    _path, expected = v2
    for wrong in ({"bots": 11}, {"rules": 2}, {"contract": False}):
        path = _archive(tmp_path, expected, contents=wrong, name=f"wrong-{next(iter(wrong))}.arenaagents")
        with pytest.raises(ArchiveFormatError, match="内容统计"):
            importer.inspect(path)


def test_a_newer_format_asks_for_an_upgrade(v2: tuple[Path, dict[str, bytes]], tmp_path: Path) -> None:
    _path, expected = v2
    manifest = {"format": "arenaagents", "format_version": 3, "files": "whatever a newer program writes"}
    with pytest.raises(ArchiveVersionError, match="v3.*只支持到 v2"):
        importer.inspect(_archive(tmp_path, expected, manifest=manifest))


def test_a_changed_byte_in_a_bot_file_is_caught(v2: tuple[Path, dict[str, bytes]], tmp_path: Path) -> None:
    _path, expected = v2
    path = _archive(tmp_path, expected)
    tampered = tmp_path / "tampered.arenaagents"
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as out:
        for info in source.infolist():
            data = source.read(info)
            if info.filename.endswith("/people.md"):
                data = data.replace("Bob".encode("utf-8"), "Eve".encode("utf-8"))  # (the same length: only the hash notices)
            out.writestr(info.filename, data)
    with pytest.raises(ArchiveFormatError, match="SHA-256"):
        importer.inspect(tampered)


# ------------------------------------------------------------------------------------------------- the games inside
def _game_v(session_id: str, schema_version: int) -> bytes:
    payload = game_payload(session_id)
    payload["schema_version"] = schema_version
    return dumps(payload)


def test_games_of_save_format_5_and_6_are_accepted_and_others_are_not(v2: tuple[Path, dict[str, bytes]], tmp_path: Path) -> None:
    _path, expected = v2
    path = _archive(tmp_path, {**expected, f"games/{GAME_A}/game.json": _game_v(GAME_A, 5)}, name="v5.arenaagents")
    assert importer.inspect(path).games == 2
    for bad in (4, 7):
        path = _archive(tmp_path, {**expected, f"games/{GAME_A}/game.json": _game_v(GAME_A, bad)}, name=f"v{bad}.arenaagents")
        with pytest.raises(ArchiveFormatError, match=rf"v{bad}.*v5 或 v6"):
            importer.inspect(path)


# --------------------------------------------------------------------------------------------------------- rollback
def _count_moves(path: Path, target: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    probe = tmp_path / "probe"
    shutil.copytree(target, probe, ignore=shutil.ignore_patterns(".lock"))
    calls = {"n": 0}
    real = importer._move

    def counting(source: Path, destination: Path) -> None:
        calls["n"] += 1
        real(source, destination)

    monkeypatch.setattr(importer, "_move", counting)
    _run(path, probe, skip_keys=True)
    monkeypatch.setattr(importer, "_move", real)
    return calls["n"]


def test_a_failure_while_moving_puts_a_format_2_data_directory_back(
    v2: tuple[Path, dict[str, bytes]], target: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _expected = v2
    moves = _count_moves(path, target, tmp_path, monkeypatch)
    assert moves >= 10, "the old entries into the backup, the new ones into place: bots, contract, rules and the rest are all moved"
    real_move = importer._move
    for failing_call in sorted({1, moves // 2, moves - 1, moves}):
        before = tree(target)
        calls = {"n": 0}

        def flaky(source: Path, destination: Path, failing_call: int = failing_call, calls: dict = calls) -> None:
            calls["n"] += 1
            if calls["n"] == failing_call:
                raise ArchiveIOError("cannot move this one")
            real_move(source, destination)

        monkeypatch.setattr(importer, "_move", flaky)
        reloads: list[int] = []
        with pytest.raises(ArchiveIOError, match="cannot move"):
            _run(path, target, skip_keys=True, reload=lambda: reloads.append(1))
        monkeypatch.setattr(importer, "_move", real_move)
        assert tree(target) == before, f"move #{failing_call} of {moves} failed: the data directory is not as it was"
        assert reloads == [1]


def test_a_failing_reload_restores_the_bots_and_the_rest_and_reloads_again(v2: tuple[Path, dict[str, bytes]], target: Path) -> None:
    path, _expected = v2
    before = tree(target)
    calls: list[int] = []

    def reload() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the application cannot use this data")

    with pytest.raises(RuntimeError, match="cannot use"):
        _run(path, target, skip_keys=True, reload=reload)
    assert tree(target) == before and len(calls) == 2
    assert BotStore(target).check_integrity() == [], "the old table of bots is back and works"


# --------------------------------------------------------------------------------------------------------- the keys
@pytest.fixture
def keyed(data_dir: Path, tmp_path: Path) -> Path:
    populate_v2(data_dir)
    path = tmp_path / "keyed.arenaagents"
    exported = export_archive(
        data_dir, ExportOptions(include_keys=True, password=PASSWORD), secrets=FakeSecrets({PROVIDER_1: KEY_1, PROVIDER_2: KEY_2}), now=NOW
    )
    path.write_bytes(exported.path.read_bytes())
    return path


def test_the_keys_of_a_format_2_archive_are_decrypted_with_the_password_and_written(keyed: Path, tmp_path: Path) -> None:
    summary = importer.inspect(keyed)
    assert summary.format_version == 2 and summary.has_secrets is True
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    store = FileSecrets(fresh)
    result = _run(keyed, fresh, password=PASSWORD, secrets=store)
    assert result.keys_imported == 2 and store.read_all() == {PROVIDER_1: KEY_1, PROVIDER_2: KEY_2}
    assert BotStore(fresh).check_integrity() == []


def test_a_wrong_password_changes_nothing_and_can_be_retried(keyed: Path, target: Path) -> None:
    before = tree(target)
    with pytest.raises(ArchivePasswordError):
        _run(keyed, target, password="Wrong-Horse-Battery-9")
    assert tree(target) == before
    result = _run(keyed, target, password=PASSWORD)
    assert result.keys_imported == 2


def test_the_keys_can_be_skipped_and_the_local_keys_of_the_same_providers_stay(keyed: Path, target: Path) -> None:
    result = _run(keyed, target, skip_keys=True)
    assert result.keys_imported == 0 and result.keys_kept == 1  # (PROVIDER_1 is in both: its local key stays)
    assert FileSecrets(target).read_all() == {PROVIDER_1: "sk-local-bbbbbbbbbbbb"}


def test_an_archive_without_keys_needs_no_password(v2: tuple[Path, dict[str, bytes]], tmp_path: Path) -> None:
    path, _expected = v2
    assert importer.inspect(path).has_secrets is False
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert _run(path, fresh, secrets=FakeSecrets()).keys_imported == 0


def test_every_failure_of_an_import_is_an_archive_error_with_a_code(v2: tuple[Path, dict[str, bytes]], target: Path, tmp_path: Path) -> None:
    _path, expected = v2
    path = _archive(tmp_path, _without_lineup(dict(expected)))
    with pytest.raises(ArchiveError) as excinfo:
        _run(path, target, skip_keys=True)
    assert excinfo.value.code and excinfo.value.status and excinfo.value.message
