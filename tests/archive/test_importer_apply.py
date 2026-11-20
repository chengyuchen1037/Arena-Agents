"""Importing: replacing the data directory, keys, backups and rollback (docs/archive.md "导入")."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.archive import importer
from app.archive.errors import (
    ArchiveError,
    ArchiveFormatError,
    ArchiveIOError,
    ArchiveKeysMismatchError,
    ArchivePasswordError,
    ArchiveStateError,
)
from app.archive.export import ExportOptions, export_archive
from app.archive.importer import KEEP_IN_PLACE

from .helpers import (
    KEY_1,
    KEY_2,
    PASSWORD,
    PROVIDER_1,
    PROVIDER_2,
    FakeSecrets,
    FileSecrets,
    dumps,
    mutate_archive,
    populate,
    providers_payload,
    tree,
)

NOW = datetime(2026, 11, 18, 0, 42, 7).astimezone()
LOCAL_GAME = "cccc3333cccc"
LOCAL_PROVIDER = "prov-local"


def _no_caches() -> None:
    return None


@pytest.fixture
def plain(data_dir: Path, populated: dict[str, bytes], tmp_path: Path) -> Path:
    path = tmp_path / "plain.arenaagents"
    path.write_bytes(export_archive(data_dir, ExportOptions(), now=NOW).path.read_bytes())
    return path


@pytest.fixture
def keyed(data_dir: Path, populated: dict[str, bytes], tmp_path: Path) -> Path:
    path = tmp_path / "keyed.arenaagents"
    exported = export_archive(
        data_dir, ExportOptions(include_keys=True, password=PASSWORD), secrets=FakeSecrets({PROVIDER_1: KEY_1, PROVIDER_2: KEY_2}), now=NOW
    )
    path.write_bytes(exported.path.read_bytes())
    return path


@pytest.fixture
def target(tmp_path: Path) -> Path:
    """Another machine's data directory: a different game, a local provider and the provider of the archive."""
    directory = tmp_path / "target"
    directory.mkdir()
    populate(directory, games=(LOCAL_GAME,), providers=(LOCAL_PROVIDER, PROVIDER_1), last_session=LOCAL_GAME)
    FileSecrets(directory).write_all({LOCAL_PROVIDER: "sk-local-aaaaaaaaaaaa", PROVIDER_1: "sk-local-bbbbbbbbbbbb"})
    return directory


def _data(root: Path) -> dict[str, bytes]:
    """The user's data under ``root`` (what an import replaces)."""
    return {name: data for name, data in tree(root).items() if name.split("/", 1)[0] not in KEEP_IN_PLACE}


def _run(path: Path, target: Path, **kwargs: Any) -> importer.ImportResult:
    kwargs.setdefault("secrets", FileSecrets(target))
    kwargs.setdefault("clear_caches", _no_caches)
    kwargs.setdefault("now", NOW)
    return importer.apply(path, data_dir=target, **kwargs)


# --------------------------------------------------------------------------------------- replacing
def test_apply_replaces_the_data_and_keeps_the_old_data_in_a_backup(plain: Path, target: Path, populated: dict[str, bytes]) -> None:
    before = _data(target)
    reloads: list[int] = []
    result = _run(plain, target, skip_keys=True, reload=lambda: reloads.append(1))
    after = {name: data for name, data in _data(target).items() if not name.startswith("backups/")}
    keys_file = after.pop("secrets.v1.enc", None)  # the kept local key of the shared provider (see the key tests)
    bots = {name: data for name in list(after) if name.startswith("bots/") and (data := after.pop(name)) is not None}
    assert after == populated  # (the v0.13 archive's files are all there, byte for byte ...)
    assert "bots/lineup.json" in bots and len([n for n in bots if n.endswith("/bot.json")]) == 12  # ... and its roster became the 12 bots
    assert keys_file is not None
    assert reloads == [1]
    backup = target / "backups" / result.backup
    assert result.backup == "20261118-004207"
    assert {name: data for name, data in tree(backup).items() if name != "master.v1.dpapi"} == before
    assert (backup / "master.v1.dpapi").read_bytes() == b"dpapi-wrapper"
    assert (result.games, result.providers) == (2, 2)
    assert not (target / "tmp").exists() or not any((target / "tmp").iterdir()), "the unpacked copy is removed"


def test_unpacked_files_keep_their_modification_time(data_dir: Path, populated: dict[str, bytes], tmp_path: Path) -> None:
    """The history list orders and shows games by the time of their file: an import must not make them all 'now'."""
    stamp = datetime(2026, 11, 10, 12, 30, 40).timestamp()
    game = data_dir / "games" / "aaaa1111aaaa" / "game.json"
    log = data_dir / "logs" / "aaaa1111aaaa" / "trace-0001.jsonl"
    for path in (game, log):
        os.utime(path, (stamp, stamp))
    archive = tmp_path / "times.arenaagents"
    archive.write_bytes(export_archive(data_dir, ExportOptions(), now=NOW).path.read_bytes())
    fresh = tmp_path / "fresh-times"
    fresh.mkdir()
    _run(archive, fresh, secrets=FakeSecrets())
    for name in ("games/aaaa1111aaaa/game.json", "logs/aaaa1111aaaa/trace-0001.jsonl"):
        assert int((fresh / name).stat().st_mtime) == int(stamp), name


def test_what_stays_where_it_is(plain: Path, target: Path) -> None:
    lock = (target / ".lock").read_bytes()
    exports = (target / "exports" / "arena-20200101-000000.arenaagents").read_bytes()
    _run(plain, target, skip_keys=True)
    assert (target / ".lock").read_bytes() == lock
    assert (target / "master.v1.dpapi").read_bytes() == b"dpapi-wrapper"
    assert (target / "exports" / "arena-20200101-000000.arenaagents").read_bytes() == exports


def test_a_round_trip_into_an_empty_data_directory_is_byte_identical(plain: Path, tmp_path: Path, populated: dict[str, bytes]) -> None:
    empty = tmp_path / "fresh"
    empty.mkdir()
    result = _run(plain, empty, secrets=FakeSecrets())
    after = {k: v for k, v in tree(empty).items() if k != "tmp/"}
    bots = {k: after.pop(k) for k in list(after) if k.startswith("bots/")}
    assert after == populated  # (every file of the archive, byte for byte; the roster stays as it came)
    assert "bots/lineup.json" in bots and len([k for k in bots if k.endswith("/bot.json")]) == 12  # (the roster became the table of bots)
    assert result.backup is None and not (empty / "backups").exists()


def test_importing_without_any_key_store_works_when_there_are_no_keys(plain: Path, tmp_path: Path, populated: dict[str, bytes]) -> None:
    """A machine without a master key (Linux without ARENA_MASTER_KEY) can still import an archive without keys."""

    class Broken:
        def read_all(self) -> dict[str, str]:
            from app.storage.errors import StorageError

            raise StorageError("no master key")

        def write_all(self, keys: dict[str, str]) -> None:
            raise AssertionError("nothing to write")

    empty = tmp_path / "nokey"
    empty.mkdir()
    result = _run(plain, empty, secrets=Broken())
    assert result.keys_imported == 0 and result.keys_kept == 0
    assert (empty / "settings.json").read_bytes() == populated["settings.json"]


def test_an_archive_is_applied_with_the_default_cache_clearing(plain: Path, target: Path) -> None:
    result = importer.apply(plain, skip_keys=True, data_dir=target, secrets=FileSecrets(target), now=NOW)
    assert result.games == 2


# --------------------------------------------------------------------------------------------- keys
def test_keys_are_decrypted_with_the_password_and_written(keyed: Path, target: Path) -> None:
    store = FileSecrets(target)
    result = _run(keyed, target, password=PASSWORD, secrets=store)
    assert json.loads((target / "secrets.v1.enc").read_text(encoding="utf-8")) == {PROVIDER_1: KEY_1, PROVIDER_2: KEY_2}
    assert (result.keys_imported, result.keys_kept, result.providers_without_key) == (2, 0, [])
    old = json.loads((target / "backups" / result.backup / "secrets.v1.enc").read_text(encoding="utf-8"))
    assert old == {LOCAL_PROVIDER: "sk-local-aaaaaaaaaaaa", PROVIDER_1: "sk-local-bbbbbbbbbbbb"}, "the old keys stay in the backup"


def test_pre_decrypted_keys_can_be_handed_in(keyed: Path, target: Path) -> None:
    _run(keyed, target, keys={PROVIDER_1: "sk-from-the-api-layer"})
    assert json.loads((target / "secrets.v1.enc").read_text(encoding="utf-8")) == {PROVIDER_1: "sk-from-the-api-layer"}


def test_skipping_the_keys_keeps_local_keys_of_providers_with_the_same_id(keyed: Path, target: Path) -> None:
    result = _run(keyed, target, skip_keys=True)
    assert json.loads((target / "secrets.v1.enc").read_text(encoding="utf-8")) == {PROVIDER_1: "sk-local-bbbbbbbbbbbb"}
    assert (result.keys_imported, result.keys_kept) == (0, 1)
    assert result.providers_without_key == [f"Provider {PROVIDER_2}"], "the other provider shows 'no API key'"


def test_an_archive_without_keys_also_keeps_the_local_keys_of_the_same_providers(plain: Path, target: Path) -> None:
    result = _run(plain, target)
    assert json.loads((target / "secrets.v1.enc").read_text(encoding="utf-8")) == {PROVIDER_1: "sk-local-bbbbbbbbbbbb"}
    assert result.keys_kept == 1


def test_the_key_store_is_not_written_when_there_is_nothing_to_write(plain: Path, tmp_path: Path) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    store = FakeSecrets({})
    _run(plain, fresh, secrets=store)
    assert store.writes == []


def test_a_wrong_password_changes_nothing_and_can_be_retried(keyed: Path, target: Path) -> None:
    before = tree(target)
    store = FileSecrets(target)
    with pytest.raises(ArchivePasswordError, match="密码错误或 API Key 部分已损坏"):
        _run(keyed, target, password="Wrong-Horse-Battery-9", secrets=store)
    assert tree(target) == before and store.writes == 0
    assert _run(keyed, target, password=PASSWORD, secrets=store).keys_imported == 2


def test_a_keyed_archive_needs_the_password_or_the_skip(keyed: Path, target: Path) -> None:
    before = tree(target)
    with pytest.raises(ArchivePasswordError, match="密码.*跳过"):
        _run(keyed, target)
    assert tree(target) == before


def test_decrypt_archive_keys_checks_the_password_without_importing(keyed: Path, plain: Path, target: Path) -> None:
    assert importer.decrypt_archive_keys(keyed, PASSWORD) == {PROVIDER_1: KEY_1, PROVIDER_2: KEY_2}
    with pytest.raises(ArchivePasswordError):
        importer.decrypt_archive_keys(keyed, "Wrong-Horse-Battery-9")
    with pytest.raises(ArchiveFormatError, match="不含 API Key"):
        importer.decrypt_archive_keys(plain, PASSWORD)


def test_a_changed_secrets_file_fails_like_a_wrong_password(keyed: Path, tmp_path: Path, target: Path) -> None:
    def flip(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
        header = json.loads(members["secrets.enc"])
        header["ciphertext"] = header["ciphertext"][:-6] + "AAAAAA"
        members["secrets.enc"] = json.dumps(header).encode()

    forged = mutate_archive(keyed, tmp_path / "forged.arenaagents", flip, fix_manifest=True)  # checksums recomputed: only GCM can notice
    before = tree(target)
    with pytest.raises(ArchivePasswordError, match="密码错误或 API Key 部分已损坏"):
        _run(forged, target, password=PASSWORD)
    assert tree(target) == before
    assert _run(forged, target, skip_keys=True).games == 2, "the data is importable without the keys"


# ------------------------------------------------------------- keys are bound to their addresses
def _retarget(provider_id: str, **changes: Any) -> Any:
    def mutate(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
        providers = json.loads(members["providers.json"])
        for item in providers["providers"]:
            if item["id"] == provider_id:
                item.update(changes)
        members["providers.json"] = dumps(providers)

    return mutate


@pytest.mark.parametrize(
    "change",
    [
        {"base_url": "https://evil.example.com"},
        {"base_url": "https://api.example.com/"},  # even a trailing slash: the address is compared exactly
        {"protocol": "anthropic_messages"},
        {"auto_append_path": False},
    ],
    ids=["base_url", "base_url-slash", "protocol", "auto_append_path"],
)
def test_a_changed_provider_address_stops_the_keys_even_with_the_right_password(keyed: Path, tmp_path: Path, target: Path, change: dict) -> None:
    """providers.json is not encrypted: whoever edits it (and recomputes the checksums) could point a provider at his own
    server and let the owner's password open the key for it.  The keys carry the address they were sealed for."""
    forged = mutate_archive(keyed, tmp_path / "forged.arenaagents", _retarget(PROVIDER_1, **change), fix_manifest=True)
    assert importer.inspect(forged).games == 2, "(a perfectly valid archive: only the keys are in doubt)"
    with pytest.raises(ArchiveKeysMismatchError, match="不一致.*不导入 API Key") as excinfo:
        importer.decrypt_archive_keys(forged, PASSWORD)
    assert excinfo.value.code == "keys_mismatch" and excinfo.value.status == 422
    assert KEY_1 not in str(excinfo.value) and "evil" not in str(excinfo.value)
    before = tree(target)
    with pytest.raises(ArchiveKeysMismatchError):
        _run(forged, target, password=PASSWORD)
    assert tree(target) == before, "nothing was changed"
    # the rest of the archive can still be imported by skipping the keys
    result = _run(forged, target, skip_keys=True)
    assert result.keys_imported == 0 and result.games == 2


def test_changes_that_do_not_move_a_key_are_fine(keyed: Path, tmp_path: Path, target: Path) -> None:
    def harmless(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
        _retarget(PROVIDER_1, name="Renamed", models=[])(members, manifest)  # a name and the model list are not addresses

    renamed = mutate_archive(keyed, tmp_path / "renamed.arenaagents", harmless, fix_manifest=True)
    assert importer.decrypt_archive_keys(renamed, PASSWORD) == {PROVIDER_1: KEY_1, PROVIDER_2: KEY_2}
    assert _run(renamed, target, password=PASSWORD).keys_imported == 2


def test_a_key_of_a_provider_the_archive_no_longer_lists_is_dropped_without_a_fuss(keyed: Path, tmp_path: Path, target: Path) -> None:
    def drop_second(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
        members["providers.json"] = dumps(providers_payload(PROVIDER_1))

    trimmed = mutate_archive(keyed, tmp_path / "trimmed.arenaagents", drop_second, fix_manifest=True)
    with pytest.raises(ArchiveFormatError, match="内容统计"):  # (the manifest counts two providers: a forger has to fix that too)
        importer.inspect(trimmed)

    def drop_and_recount(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
        drop_second(members, manifest)
        manifest["contents"]["providers"] = 1

    consistent = mutate_archive(keyed, tmp_path / "consistent.arenaagents", drop_and_recount, fix_manifest=True)
    result = _run(consistent, target, password=PASSWORD)
    assert result.keys_imported == 1 and json.loads((target / "secrets.v1.enc").read_text(encoding="utf-8")) == {PROVIDER_1: KEY_1}


def test_keys_sealed_without_addresses_are_not_trusted(tmp_path: Path, data_dir: Path, populated: dict[str, bytes], target: Path) -> None:
    """A secrets.enc that carries no addresses (not made by this program's exporter) binds nothing: fail closed."""
    from app.archive import kdf
    from app.archive.format import FORMAT_NAME  # noqa: F401

    good = export_archive(data_dir, ExportOptions(include_keys=True, password=PASSWORD), secrets=FakeSecrets({PROVIDER_1: KEY_1}), now=NOW).path

    def unbound(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
        members["secrets.enc"] = kdf.seal_secrets({PROVIDER_1: KEY_1}, PASSWORD, manifest["archive_id"])

    forged = mutate_archive(good, tmp_path / "unbound.arenaagents", unbound, fix_manifest=True)
    with pytest.raises(ArchiveKeysMismatchError):
        importer.decrypt_archive_keys(forged, PASSWORD)


# ------------------------------------------------------------------------------------------ backups
def test_only_the_three_newest_backups_are_kept(plain: Path, target: Path) -> None:
    names = []
    for index in range(5):
        result = _run(plain, target, skip_keys=True, now=NOW + timedelta(minutes=index))
        names.append(result.backup)
    kept = sorted(p.name for p in (target / "backups").iterdir())
    assert kept == sorted(names)[-3:] and len(kept) == 3


def test_two_imports_in_the_same_second_get_two_backups(plain: Path, target: Path) -> None:
    first = _run(plain, target, skip_keys=True).backup
    second = _run(plain, target, skip_keys=True).backup
    assert first != second and {first, second} <= {p.name for p in (target / "backups").iterdir()}


def test_pruning_only_touches_folders_named_like_backups(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    for name in ("20260101-000000", "20260102-000000", "20260103-000000", "20260104-000000", "notes", "20260105-000000-1"):
        (backups / name).mkdir(parents=True)
    (backups / "readme.txt").write_text("x")
    removed = importer.prune_backups(tmp_path, keep=3)
    assert removed == ["20260101-000000", "20260102-000000"]
    assert sorted(p.name for p in backups.iterdir()) == ["20260103-000000", "20260104-000000", "20260105-000000-1", "notes", "readme.txt"]


# ----------------------------------------------------------------------------------------- rollback
FORWARD_MOVES = 14  # 7 entries of the old data into the backup + 7 of the new data into place (the 6 of the archive and the bots/ made of its roster)


@pytest.mark.parametrize("failing_call", range(1, FORWARD_MOVES + 1))
def test_a_failure_while_moving_puts_everything_back(plain: Path, target: Path, monkeypatch: pytest.MonkeyPatch, failing_call: int) -> None:
    before = tree(target)
    real_move = importer._move
    calls = {"n": 0}

    def flaky(source: Path, destination: Path) -> None:
        calls["n"] += 1
        if calls["n"] == failing_call:
            raise ArchiveIOError("cannot move this one")
        real_move(source, destination)

    monkeypatch.setattr(importer, "_move", flaky)
    reloads: list[int] = []
    with pytest.raises(ArchiveIOError, match="cannot move"):
        _run(plain, target, skip_keys=True, reload=lambda: reloads.append(1))
    assert tree(target) == before, f"move #{failing_call} failed: the data directory is not as it was"
    assert not list((target / "backups").iterdir()), "no empty backup folder is left behind"
    assert reloads == [1], "the application is told about the restored data"


def test_a_failure_writing_the_keys_removes_the_new_key_file_and_restores_the_old_one(keyed: Path, target: Path) -> None:
    before = tree(target)
    store = FileSecrets(target, fail_after_write=True)
    with pytest.raises(Exception, match="disk failure"):
        _run(keyed, target, password=PASSWORD, secrets=store)
    assert tree(target) == before and store.writes == 1


def test_a_failing_reload_restores_the_old_data_and_reloads_again(plain: Path, target: Path) -> None:
    before = tree(target)
    calls: list[int] = []
    cleared: list[int] = []

    def reload() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the application cannot use this data")

    with pytest.raises(RuntimeError, match="cannot use"):
        _run(plain, target, skip_keys=True, reload=reload, clear_caches=lambda: cleared.append(1))
    assert tree(target) == before
    assert len(calls) == 2, "reloaded once with the new data (failed) and once with the restored data"
    assert len(cleared) == 2, "the caches are cleared after the swap and again after the restore"


def test_a_running_game_stops_the_import_before_anything_moves(plain: Path, target: Path) -> None:
    before = tree(target)
    reloads: list[int] = []

    def busy() -> None:
        raise ArchiveStateError("对局正在运行")

    with pytest.raises(ArchiveStateError):
        _run(plain, target, skip_keys=True, ensure_idle=busy, reload=lambda: reloads.append(1))
    assert tree(target) == before and reloads == []


def test_nothing_is_left_in_tmp_after_a_failed_import(plain: Path, target: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importer, "_move", lambda *a: (_ for _ in ()).throw(ArchiveIOError("boom")))
    with pytest.raises(ArchiveIOError):
        _run(plain, target, skip_keys=True)
    assert [p for p in (target / "tmp").iterdir()] == []


# --------------------------------------------------------------------------- what is checked on the way
def test_an_archive_that_changes_between_inspection_and_apply_is_caught(plain: Path, target: Path) -> None:
    importer.inspect(plain)
    raw = bytearray(plain.read_bytes())
    # flip a byte of a stored-compressed-data region: the member's checksum no longer matches
    import zipfile

    with zipfile.ZipFile(plain) as zf:
        info = zf.getinfo("settings.json")
        offset = info.header_offset + 30 + len(info.filename) + len(info.extra) + 3
    raw[offset] ^= 0x01
    plain.write_bytes(bytes(raw))
    before = tree(target)
    with pytest.raises(ArchiveFormatError):
        _run(plain, target, skip_keys=True)
    assert tree(target) == before


def test_settings_the_application_cannot_read_are_refused_before_the_swap(plain: Path, target: Path, tmp_path: Path) -> None:
    def bad_settings(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
        members["settings.json"] = dumps({"schema_version": 7, "whatever": True})

    forged = mutate_archive(plain, tmp_path / "bad-settings.arenaagents", bad_settings, fix_manifest=True)
    assert importer.inspect(forged).games == 2  # structurally a fine archive ...
    before = tree(target)
    with pytest.raises(ArchiveFormatError, match="settings.json"):  # ... that the settings reader refuses
        _run(forged, target, skip_keys=True)
    assert tree(target) == before


def test_the_last_game_pointer_never_points_at_a_game_that_is_not_there(data_dir: Path, tmp_path: Path, target: Path) -> None:
    other = tmp_path / "other-source"
    other.mkdir()
    populate(other, last_session="ffff0000ffff")  # the pointer names a game the export will not contain
    archive = tmp_path / "pointer.arenaagents"
    archive.write_bytes(export_archive(other, ExportOptions(), now=NOW).path.read_bytes())
    result = _run(archive, target, skip_keys=True)
    settings = json.loads((target / "settings.json").read_text(encoding="utf-8"))
    assert settings["last_session_id"] is None and settings["ui"] == {"budget_defaults": {"max_tokens": 1000}}
    assert any("上次对局" in warning for warning in result.warnings)


def test_a_valid_last_game_pointer_is_left_alone(plain: Path, target: Path, populated: dict[str, bytes]) -> None:
    _run(plain, target, skip_keys=True)
    assert (target / "settings.json").read_bytes() == populated["settings.json"]


def test_not_enough_disk_space_is_reported_before_unpacking(plain: Path, target: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Usage:
        total, used, free = 100, 100, 0

    monkeypatch.setattr(shutil, "disk_usage", lambda path: Usage)
    before = tree(target)
    with pytest.raises(ArchiveIOError, match="磁盘空间不足"):
        _run(plain, target, skip_keys=True)
    assert tree(target) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_unpacked_data_is_private_on_posix(plain: Path, tmp_path: Path) -> None:
    fresh = tmp_path / "fresh-mode"
    fresh.mkdir()
    _run(plain, fresh, secrets=FakeSecrets())
    assert (fresh / "settings.json").stat().st_mode & 0o077 == 0
    assert (fresh / "games").stat().st_mode & 0o077 == 0


def test_every_failure_is_an_archive_error_with_a_code_and_a_safe_message(keyed: Path, target: Path) -> None:
    with pytest.raises(ArchiveError) as excinfo:
        _run(keyed, target, skip_keys=False, password="Wrong-Horse-Battery-9")
    error = excinfo.value
    assert (error.code, error.status) == ("wrong_password", 422)
    assert "Wrong-Horse" not in str(error) and KEY_1 not in str(error) and PASSWORD not in str(error)
