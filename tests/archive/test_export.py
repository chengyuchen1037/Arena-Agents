"""Exporting the data directory (docs/archive.md "导出")."""

from __future__ import annotations

import errno
import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.archive import export as export_module
from app.archive import importer, kdf
from app.archive.errors import ArchiveError, ArchiveFormatError, ArchiveIOError, ArchivePasswordRuleError
from app.archive.export import ExportOptions, export_archive
from app.archive.format import EXPORT_NAME_RE, files_digest, parse_manifest

from .helpers import (
    GAME_A,
    GAME_B,
    KEY_1,
    KEY_2,
    PASSWORD,
    PROVIDER_1,
    PROVIDER_2,
    FakeSecrets,
    dumps,
    game_payload,
    members_of,
    populate,
)

NOW = datetime(2026, 11, 18, 0, 42, 7, tzinfo=timezone(timedelta(hours=-8)))


def test_export_contains_exactly_the_documented_files_and_they_are_byte_identical(data_dir: Path, populated: dict[str, bytes]) -> None:
    result = export_archive(data_dir, ExportOptions(include_keys=False, include_logs=True), now=NOW)
    assert result.name == "arena-20261118-004207.arenaagents" and EXPORT_NAME_RE.fullmatch(result.name)
    assert result.path == data_dir / "exports" / result.name and result.size_bytes == result.path.stat().st_size
    members = members_of(result.path)
    manifest = members.pop("manifest.json")
    assert set(members) == set(populated), "exactly the data files: no secrets, no lock, no backups, no previous copies"
    for name, data in populated.items():
        assert members[name] == data, name
    assert not list((data_dir / "exports").glob("*.part"))
    parsed = parse_manifest(manifest)
    assert parsed.created_at == "2026-11-18T00:42:07-08:00" and parsed.app_version.startswith("v")
    assert parsed.format_version == 1  # (a v0.13 data directory - a roster and no bots - is written as a format 1 archive)
    assert parsed.contents.to_manifest() == {"games": 2, "providers": 2, "roster_seats": 2, "has_secrets": False, "has_logs": True}
    assert json.loads(manifest)["contents"] == {"games": 2, "providers": 2, "roster_seats": 2, "has_secrets": False, "has_logs": True}
    assert {f.path: f.sha256 for f in parsed.files} == {n: hashlib.sha256(d).hexdigest() for n, d in populated.items()}
    assert {f.path: f.size for f in parsed.files} == {n: len(d) for n, d in populated.items()}
    assert parsed.files_sha256 == files_digest(parsed.files)


def test_what_never_goes_into_an_archive(data_dir: Path, populated: dict[str, bytes]) -> None:
    result = export_archive(data_dir, ExportOptions(), now=NOW)
    names = set(members_of(result.path))
    for name in ("secrets.v1.enc", "master.v1.dpapi", ".lock", "games/aaaa1111aaaa/game.prev.json"):
        assert name not in names
    assert not any(n.startswith(("exports/", "backups/", "tmp/")) for n in names)
    raw = result.path.read_bytes()
    assert b"local-encrypted-keys" not in raw and b"dpapi-wrapper" not in raw


def test_the_exported_archive_passes_the_importers_inspection(data_dir: Path, populated: dict[str, bytes]) -> None:
    result = export_archive(data_dir, ExportOptions(), now=NOW)
    summary = importer.inspect(result.path)
    assert (summary.games, summary.providers, summary.roster_seats, summary.has_logs, summary.has_secrets) == (2, 2, 2, True, False)
    assert summary.file_count == len(populated) and summary.archive_id == result.archive_id
    assert {g["session_id"] for g in summary.game_list} == {GAME_A, GAME_B}
    assert summary.provider_names == [f"Provider {PROVIDER_1}", f"Provider {PROVIDER_2}"]


def test_logs_are_optional(data_dir: Path, populated: dict[str, bytes]) -> None:
    result = export_archive(data_dir, ExportOptions(include_logs=False), now=NOW)
    names = set(members_of(result.path))
    assert not any(n.startswith("logs/") for n in names)
    assert names - {"manifest.json"} == {n for n in populated if not n.startswith("logs/")}
    assert result.contents["has_logs"] is False


def test_big_logs_are_streamed_and_hashed(data_dir: Path) -> None:
    populate(data_dir, logs=False)
    log = data_dir / "logs" / GAME_A
    log.mkdir(parents=True)
    payload = (b'{"event":"think","text":"' + b"x" * 1000 + b'"}\n') * 4000  # about 4 MB
    (log / "trace-0001.jsonl").write_bytes(payload)
    result = export_archive(data_dir, ExportOptions(), now=NOW)
    with zipfile.ZipFile(result.path) as zf:
        info = zf.getinfo(f"logs/{GAME_A}/trace-0001.jsonl")
        assert info.file_size == len(payload) and info.compress_size < len(payload) / 10
        assert zf.read(info) == payload
    entry = next(f for f in parse_manifest(members_of(result.path)["manifest.json"]).files if f.path.endswith("trace-0001.jsonl"))
    assert entry.sha256 == hashlib.sha256(payload).hexdigest()


# ----------------------------------------------------------------------------------------------- keys
def test_keys_are_encrypted_with_the_password_and_only_for_known_providers(data_dir: Path, populated: dict[str, bytes]) -> None:
    secrets = FakeSecrets({PROVIDER_1: KEY_1, PROVIDER_2: KEY_2, "removed-provider": "sk-orphan-0123456789"})
    result = export_archive(data_dir, ExportOptions(include_keys=True, password=PASSWORD), secrets=secrets, now=NOW)
    members = members_of(result.path)
    assert "secrets.enc" in members and result.contents["has_secrets"] is True
    manifest = parse_manifest(members["manifest.json"])
    assert any(f.path == "secrets.enc" for f in manifest.files)
    assert kdf.open_secrets(members["secrets.enc"], PASSWORD, manifest.archive_id) == {PROVIDER_1: KEY_1, PROVIDER_2: KEY_2}
    sealed = kdf.open_secrets_bound(members["secrets.enc"], PASSWORD, manifest.archive_id)
    assert sealed.endpoints == {
        pid: {"protocol": "openai_chat", "base_url": "https://api.example.com", "auto_append_path": True} for pid in (PROVIDER_1, PROVIDER_2)
    }, "every key is sealed with the address of its provider"
    # no key and no password in any member or in the raw bytes of the zip
    raw = result.path.read_bytes()
    for secret in (KEY_1, KEY_2, "sk-orphan-0123456789", PASSWORD):
        assert secret.encode() not in raw
        assert all(secret.encode() not in data for data in members.values())


def test_without_include_keys_the_key_store_is_not_even_opened(data_dir: Path, populated: dict[str, bytes]) -> None:
    class Exploding:
        def read_all(self) -> dict[str, str]:
            raise AssertionError("the key store must not be read")

        def write_all(self, keys: dict[str, str]) -> None:
            raise AssertionError

    result = export_archive(data_dir, ExportOptions(include_keys=False), secrets=Exploding(), now=NOW)
    assert "secrets.enc" not in members_of(result.path)


def test_no_keys_on_this_machine_means_no_secrets_file_and_a_warning(data_dir: Path, populated: dict[str, bytes]) -> None:
    result = export_archive(data_dir, ExportOptions(include_keys=True, password=PASSWORD), secrets=FakeSecrets({}), now=NOW)
    assert "secrets.enc" not in members_of(result.path)
    assert result.contents["has_secrets"] is False
    assert any("API Key" in warning for warning in result.warnings)


def test_an_unreadable_key_store_stops_the_export_and_leaves_nothing(data_dir: Path, populated: dict[str, bytes]) -> None:
    with pytest.raises(ArchiveIOError, match="API Key"):
        export_archive(data_dir, ExportOptions(include_keys=True, password=PASSWORD), secrets=FakeSecrets(fail_read=True), now=NOW)
    assert list((data_dir / "exports").iterdir()) == [data_dir / "exports" / "arena-20200101-000000.arenaagents"]


@pytest.mark.parametrize("password", [None, "", "short", "alllowercaseletters", "NoSymbolsOrDigits"])
def test_a_weak_password_is_refused_before_anything_is_written(data_dir: Path, populated: dict[str, bytes], password: str | None) -> None:
    with pytest.raises(ArchivePasswordRuleError):
        export_archive(data_dir, ExportOptions(include_keys=True, password=password), secrets=FakeSecrets({PROVIDER_1: KEY_1}), now=NOW)
    assert [p.name for p in (data_dir / "exports").iterdir()] == ["arena-20200101-000000.arenaagents"]


def test_a_password_is_not_checked_when_keys_are_not_exported(data_dir: Path, populated: dict[str, bytes]) -> None:
    assert export_archive(data_dir, ExportOptions(include_keys=False, password="x"), now=NOW).path.is_file()


def test_the_options_never_print_the_password() -> None:
    assert PASSWORD not in repr(ExportOptions(include_keys=True, password=PASSWORD))


def test_a_provider_file_that_holds_a_key_stops_the_export(data_dir: Path, populated: dict[str, bytes]) -> None:
    providers = json.loads((data_dir / "providers.json").read_text(encoding="utf-8"))
    providers["providers"][0]["api_key"] = KEY_1
    (data_dir / "providers.json").write_bytes(dumps(providers))
    with pytest.raises(ArchiveFormatError, match="API Key"):
        export_archive(data_dir, ExportOptions(), now=NOW)
    assert not any(p.name.startswith("arena-2026") for p in (data_dir / "exports").iterdir())


# ---------------------------------------------------------------------------------- damaged data
def test_a_damaged_game_is_left_out_with_its_logs_and_reported(data_dir: Path, populated: dict[str, bytes]) -> None:
    (data_dir / "games" / GAME_B / "game.json").write_bytes(b'{"truncated": ')
    result = export_archive(data_dir, ExportOptions(), now=NOW)
    names = set(members_of(result.path))
    assert f"games/{GAME_A}/game.json" in names and f"games/{GAME_B}/game.json" not in names
    assert not any(n.startswith(f"logs/{GAME_B}/") for n in names) and any(n.startswith(f"logs/{GAME_A}/") for n in names)
    assert result.contents["games"] == 1
    assert any(GAME_B in warning for warning in result.warnings)
    assert importer.inspect(result.path).games == 1


def test_the_previous_copy_stands_in_for_a_damaged_game(data_dir: Path, populated: dict[str, bytes]) -> None:
    prev = (data_dir / "games" / GAME_A / "game.prev.json").read_bytes()
    (data_dir / "games" / GAME_A / "game.json").write_bytes(b"\x00garbage")
    result = export_archive(data_dir, ExportOptions(), now=NOW)
    assert members_of(result.path)[f"games/{GAME_A}/game.json"] == prev
    assert any("备份副本" in warning for warning in result.warnings)


def test_a_game_of_the_wrong_save_format_is_left_out(data_dir: Path, populated: dict[str, bytes]) -> None:
    old = game_payload(GAME_B)
    old["schema_version"] = 4
    (data_dir / "games" / GAME_B / "game.json").write_bytes(dumps(old))
    result = export_archive(data_dir, ExportOptions(), now=NOW)
    assert f"games/{GAME_B}/game.json" not in members_of(result.path)
    assert any("v4" in warning or GAME_B in warning for warning in result.warnings)


def test_an_unusable_config_file_is_skipped_not_fatal(data_dir: Path, populated: dict[str, bytes]) -> None:
    (data_dir / "settings.json").write_bytes(b"{ not json")
    (data_dir / "pricing.json").write_bytes(b"[1, 2]")
    result = export_archive(data_dir, ExportOptions(), now=NOW)
    names = set(members_of(result.path))
    assert "settings.json" not in names and "pricing.json" not in names and "providers.json" in names
    assert sum("已跳过无法使用的文件" in warning for warning in result.warnings) == 2


def test_unknown_files_in_the_data_dir_are_ignored(data_dir: Path, populated: dict[str, bytes]) -> None:
    (data_dir / "notes.txt").write_text("mine")
    (data_dir / "roster" / "stray.bin").write_bytes(b"x")
    (data_dir / "logs" / GAME_A / "notes.txt").write_text("x")
    (data_dir / "games" / "not a valid id").mkdir()
    (data_dir / "games" / "not a valid id" / "game.json").write_bytes(b"{}")
    names = set(members_of(export_archive(data_dir, ExportOptions(), now=NOW).path))
    assert names - {"manifest.json"} == set(populated)


def test_symbolic_links_are_never_followed(data_dir: Path, populated: dict[str, bytes], tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_bytes(b'{"schema_version": 2, "default_bindings": {}, "last_session_id": null, "ui": {}, "model_templates": []}')
    (data_dir / "pricing.json").unlink()
    try:
        (data_dir / "pricing.json").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symbolic links")
    result = export_archive(data_dir, ExportOptions(), now=NOW)
    assert "pricing.json" not in members_of(result.path)
    assert any("符号链接" in warning for warning in result.warnings)


# ----------------------------------------------------------------------------------- mechanics
def test_progress_walks_through_the_phases_and_ends_complete(data_dir: Path, populated: dict[str, bytes]) -> None:
    seen: list[tuple[str, int, int]] = []
    export_archive(
        data_dir,
        ExportOptions(include_keys=True, password=PASSWORD),
        secrets=FakeSecrets({PROVIDER_1: KEY_1}),
        progress=lambda phase, done, total: seen.append((phase, done, total)),
        now=NOW,
    )
    order: list[str] = []
    for phase, _done, _total in seen:
        if not order or order[-1] != phase:
            order.append(phase)
    assert order == ["collect", "encrypt", "pack", "finalize"]
    packs = [(done, total) for phase, done, total in seen if phase == "pack"]
    assert packs[-1][0] == packs[-1][1] and all(a[0] <= b[0] for a, b in zip(packs, packs[1:]))
    assert seen[-1] == ("finalize", 1, 1)


def test_a_failure_while_packing_leaves_no_partial_file(data_dir: Path, populated: dict[str, bytes], monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(export_module, "_pack", explode)
    with pytest.raises(ArchiveIOError, match="磁盘空间不足"):
        export_archive(data_dir, ExportOptions(), now=NOW)
    assert [p.name for p in (data_dir / "exports").iterdir()] == ["arena-20200101-000000.arenaagents"]


def test_two_exports_in_the_same_second_get_two_names(data_dir: Path, populated: dict[str, bytes]) -> None:
    first = export_archive(data_dir, ExportOptions(), now=NOW)
    second = export_archive(data_dir, ExportOptions(), now=NOW)
    assert first.name != second.name and EXPORT_NAME_RE.fullmatch(second.name)
    assert first.path.is_file() and second.path.is_file()


def test_an_empty_data_directory_has_nothing_to_export(data_dir: Path) -> None:
    with pytest.raises(ArchiveError, match="没有任何数据"):
        export_archive(data_dir, ExportOptions(), now=NOW)
    assert not (data_dir / "exports").exists() or not list((data_dir / "exports").iterdir())


def test_the_zip_has_regular_files_with_forward_slash_names(data_dir: Path, populated: dict[str, bytes]) -> None:
    import stat

    with zipfile.ZipFile(export_archive(data_dir, ExportOptions(), now=NOW).path) as zf:
        for info in zf.infolist():
            assert "\\" not in info.filename and not info.filename.startswith("/") and ".." not in info.filename.split("/")
            assert stat.S_ISREG(info.external_attr >> 16) and info.flag_bits & 0x1 == 0
        assert zf.infolist()[-1].filename == "manifest.json", "the manifest is written last"
