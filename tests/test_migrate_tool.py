"""tools/migrate_v0121_to_archive.py: a v0.12.1 workspace becomes a v0.13 archive (decision F33).

The old workspace is built with the real ``app.storage.crypto`` (the test master key of conftest): encrypted provider
registry and games (save format v4 / v3), roster, settings, price table, trace logs."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import migrate_v0121_to_archive as tool  # noqa: E402

from app.archive import importer, legacy  # noqa: E402
from app.archive.errors import ArchivePasswordError  # noqa: E402
from app.game.session import GameSession  # noqa: E402
from app.storage.crypto import encrypt_json  # noqa: E402
from tests.archive.helpers import FakeSecrets, game_payload  # noqa: E402

GAME_RUNNING = "v4game00001"  # started, then paused
GAME_DONE = "v4game00002"  # finished
GAME_CONTINUED = "v4game00003"  # finished, then continued
GAME_OLD = "v3game00004"  # v0.12.0: cannot be converted
GAME_BROKEN = "bad0game0005"  # damaged ciphertext
KEY_1 = "sk-legacy-one-0123456789abcdef"
KEY_2 = "sk-legacy-two-0123456789abcdef"
PASSWORD = "Correct-Horse-Battery-9"


def entry(seq: int, ts: str, kind: str = "message", data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"seq": seq, "ts": ts, "gt": float(seq), "type": kind, "actor_id": "engine", "channel_id": None, "visible_to_ids": [], "data": data or {}}


def status(seq: int, ts: str, value: str) -> dict[str, Any]:
    return entry(seq, ts, "game.status", {"status": value, "reason": "x"})


def v4_game(game_id: str, ledger: list[dict[str, Any]], *, game_status: str = "paused") -> dict[str, Any]:
    payload = game_payload(game_id, status=game_status)
    for field in ("takeover", "human_spans", "created_at", "started_at", "finished_at"):
        payload.pop(field)
    payload["schema_version"] = 4
    payload["world"]["ledger"] = ledger
    return payload


def make_workspace(root: Path, *, extra_provider: bool = True) -> Path:
    """``<root>/Arena-Agents/Default`` as v0.12.1 left it; returns the workspace folder."""
    workspace = root / "Arena-Agents" / "Default"
    data = workspace / ".arena"
    data.mkdir(parents=True)
    (workspace / ".arena-workspace.json").write_text(json.dumps({"schema_version": 1, "workspace_id": "w1", "name": "Default", "created_at": "2026-11-01T00:00:00+00:00"}), encoding="utf-8")
    (data / "workspace.lock").write_bytes(b"\0")
    (data / "master.v1.dpapi").write_bytes(b"not-a-real-dpapi-wrapper")
    providers = [
        {"id": "p1", "name": "DeepSeek", "protocol": "openai_chat", "base_url": "https://api.deepseek.com", "api_key": KEY_1, "models": [{"id": "m1", "input_price_per_m": 0.3, "output_price_per_m": 1.1, "cache_read_price_per_m": None, "cache_write_price_per_m": None, "context_window_tokens": 128000, "price_source": "manual"}], "read_only": False, "source": "encrypted", "auto_append_path": True},
        {"id": "env-deepseek", "name": "DeepSeek（环境变量）", "protocol": "openai_chat", "base_url": "https://api.deepseek.com", "api_key": "sk-from-the-environment-0000", "models": [], "read_only": True, "source": "environment", "auto_append_path": True},
    ]
    if extra_provider:
        providers.append({"id": "p2", "name": "Claude", "protocol": "anthropic_messages", "base_url": "https://api.anthropic.com", "api_key": KEY_2, "models": [], "read_only": False, "source": "encrypted", "auto_append_path": True})
    registry = {"schema_version": 1, "providers": providers}
    (data / "providers.v1.enc").write_bytes(encrypt_json(registry, "providers.v1", data_dir=data))
    (data / "providers.v1.backup.enc").write_bytes(encrypt_json(registry, "providers.v1", data_dir=data))
    (data / "settings.json").write_text(json.dumps({"schema_version": 2, "default_bindings": {}, "last_session_id": GAME_RUNNING, "ui": {"budget_defaults": {"max_tokens": 5000}}, "model_templates": []}), encoding="utf-8")
    (data / "pricing.v1.json").write_text(json.dumps({"schema": 1, "url": "https://example.com/prices.json", "fetched_at": None, "entries": {}}), encoding="utf-8")
    (data / "roster").mkdir()
    (data / "roster" / "roster.json").write_text(json.dumps({"memory_enabled": True, "seats": {"N1": {"name": "小明"}}}, ensure_ascii=False), encoding="utf-8")
    (data / "roster" / "N1.persona.md").write_text("说话简短。\n", encoding="utf-8")
    (data / "roster" / "N2.memory.md").write_text("## 战绩\n- 赢过一次\n", encoding="utf-8")
    (data / "roster" / "notes.txt").write_text("not part of the roster", encoding="utf-8")

    games = {
        GAME_RUNNING: v4_game(GAME_RUNNING, [entry(1, "2026-11-10T08:00:00.100-08:00"), status(2, "2026-11-10T08:05:00.000-08:00", "running"), status(3, "2026-11-10T08:30:00.000-08:00", "paused")]),
        GAME_DONE: v4_game(GAME_DONE, [entry(1, "2026-11-11T09:00:00+00:00"), status(2, "2026-11-11T09:01:00+00:00", "running"), status(3, "2026-11-11T09:40:00+00:00", "finished")], game_status="finished"),
        GAME_CONTINUED: v4_game(GAME_CONTINUED, [entry(1, "2026-11-12T10:00:00+00:00"), status(2, "2026-11-12T10:01:00+00:00", "running"), status(3, "2026-11-12T10:30:00+00:00", "finished"), status(4, "2026-11-12T11:00:00+00:00", "running")], game_status="running"),
    }
    old = v4_game(GAME_OLD, [entry(1, "2026-10-01T00:00:00+00:00")])
    old["schema_version"] = 3
    games[GAME_OLD] = old
    for game_id, payload in games.items():
        folder = data / "sessions" / game_id
        folder.mkdir(parents=True)
        blob = encrypt_json(payload, f"session.v1:{game_id}", data_dir=data)
        (folder / "current.json.enc").write_bytes(blob)
        (folder / "previous.json.enc").write_bytes(blob)
        (data / "logs" / game_id).mkdir(parents=True)
        (data / "logs" / game_id / "trace-0001.jsonl").write_bytes((json.dumps({"event": "think", "game": game_id}) + "\n").encode() * 20)
    broken = data / "sessions" / GAME_BROKEN
    broken.mkdir()
    (broken / "current.json.enc").write_bytes(b'{"format":"arena-agent-aes-gcm","version":1}')
    (data / "exports").mkdir()
    (data / "exports" / "old-export.json").write_text("{}", encoding="utf-8")
    return workspace


def fingerprint(root: Path) -> dict[str, tuple[str, int]]:
    """Everything under ``root`` -> (sha256 of the bytes, mtime in ns); directories too (an extra / missing one shows)."""
    found: dict[str, tuple[str, int]] = {}
    for current, dirs, files in os.walk(root):
        for name in dirs:
            path = Path(current) / name
            found[path.relative_to(root).as_posix() + "/"] = ("", path.stat().st_mtime_ns)
        for name in files:
            path = Path(current) / name
            found[path.relative_to(root).as_posix()] = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    return found


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return make_workspace(tmp_path / "old-home")


def run_tool(*argv: str, passwords: list[str] | None = None) -> tuple[int, str, str]:
    """``main`` with captured output; ``passwords`` are what getpass will return in turn."""
    answers = iter(passwords or [])
    out, err = io.StringIO(), io.StringIO()
    code = tool.main(list(argv), getpass_fn=lambda prompt: next(answers), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def convert(workspace: Path, output: Path, *flags: str, passwords: list[str] | None = None) -> tuple[int, str]:
    code, text, err = run_tool("--workspace", str(workspace), "--output", str(output), *flags, passwords=passwords)
    return code, text + err


# ---------------------------------------------------------------------------------- the game conversion
def test_a_v4_game_gets_the_v5_fields_from_its_ledger() -> None:
    ledger = [entry(1, "2026-11-10T08:00:00.100-08:00"), status(2, "2026-11-10T08:05:00-08:00", "running"), status(3, "2026-11-10T08:30:00-08:00", "finished")]
    converted = tool.convert_game(v4_game("g1", ledger, game_status="finished"))
    assert converted["schema_version"] == 5 and converted["takeover"] == {"seat": None} and converted["human_spans"] == []
    assert converted["created_at"] == "2026-11-10T16:00:00.100000+00:00", "UTC, from the first ledger entry"
    assert converted["started_at"] == "2026-11-10T16:05:00+00:00"
    assert converted["finished_at"] == "2026-11-10T16:30:00+00:00"
    assert GameSession.model_validate(converted).session_id == "g1"


def test_a_game_that_was_continued_after_its_verdict_has_no_finish_time() -> None:
    ledger = [entry(1, "2026-11-12T10:00:00+00:00"), status(2, "2026-11-12T10:01:00+00:00", "running"), status(3, "2026-11-12T10:30:00+00:00", "finished"), status(4, "2026-11-12T11:00:00+00:00", "running")]
    converted = tool.convert_game(v4_game("g1", ledger, game_status="running"))
    assert converted["started_at"] == "2026-11-12T10:01:00+00:00" and converted["finished_at"] is None


def test_a_game_that_never_started_has_only_a_creation_time() -> None:
    converted = tool.convert_game(v4_game("g1", [entry(1, "2026-11-12T10:00:00+00:00")], game_status="idle"))
    assert converted["started_at"] is None and converted["finished_at"] is None and converted["created_at"]


def test_an_empty_ledger_falls_back_to_the_file_time() -> None:
    converted = tool.convert_game(v4_game("g1", []), fallback_ts="2026-01-02T03:04:05+00:00")
    assert converted["created_at"] == "2026-01-02T03:04:05+00:00" and converted["started_at"] is None


@pytest.mark.parametrize("schema", [1, 2, 3, 7, None, "4"])
def test_other_save_formats_are_not_converted(schema: Any) -> None:
    payload = v4_game("g1", [])
    payload["schema_version"] = schema
    with pytest.raises(ValueError, match="只能转换 v4"):
        tool.convert_game(payload)


def test_a_game_that_does_not_fit_the_model_is_refused_without_echoing_it() -> None:
    payload = v4_game("g1", [entry(1, "2026-11-12T10:00:00+00:00")])
    payload["status"] = "super-secret-looking-status"
    with pytest.raises(ValueError, match="不符合 v5") as excinfo:
        tool.convert_game(payload)
    assert "super-secret" not in str(excinfo.value)


def test_a_ledger_less_game_is_refused() -> None:
    payload = v4_game("g1", [])
    payload["world"] = {"format": 1}
    with pytest.raises(ValueError, match="账本"):
        tool.convert_game(payload)


# ------------------------------------------------------------------------------------- the conversion
def test_the_converted_archive_imports_and_holds_everything(workspace: Path, tmp_path: Path) -> None:
    output = tmp_path / "out" / "converted.arenaagents"
    code, text = convert(workspace, output)
    assert code == 0 and output.is_file(), text
    summary = importer.inspect(output)
    assert (summary.games, summary.providers, summary.has_secrets, summary.has_logs) == (3, 2, False, True)
    assert summary.roster_seats == 2

    target = tmp_path / "new-data"
    target.mkdir()
    result = importer.apply(output, skip_keys=True, data_dir=target, secrets=FakeSecrets(), clear_caches=lambda: None)
    assert result.games == 3 and result.providers == 2
    for game_id in (GAME_RUNNING, GAME_DONE, GAME_CONTINUED):
        game = GameSession.model_validate(json.loads((target / "games" / game_id / "game.json").read_text(encoding="utf-8")))
        assert game.schema_version == 5 and game.takeover.seat is None and game.created_at
        assert (target / "logs" / game_id / "trace-0001.jsonl").read_bytes().startswith(b'{"event": "think"')
    assert not (target / "games" / GAME_OLD).exists() and not (target / "games" / GAME_BROKEN).exists()
    done = json.loads((target / "games" / GAME_DONE / "game.json").read_text(encoding="utf-8"))
    assert done["finished_at"] == "2026-11-11T09:40:00+00:00" and done["status"] == "finished"
    settings = json.loads((target / "settings.json").read_text(encoding="utf-8"))
    assert settings["last_session_id"] == GAME_RUNNING and settings["ui"] == {"budget_defaults": {"max_tokens": 5000}}
    source = workspace / ".arena"
    assert (target / "pricing.json").read_bytes() == (source / "pricing.v1.json").read_bytes()
    assert (target / "roster" / "N1.persona.md").read_bytes() == (source / "roster" / "N1.persona.md").read_bytes()
    assert (target / "roster" / "N2.memory.md").read_bytes() == (source / "roster" / "N2.memory.md").read_bytes()
    assert (target / "roster" / "roster.json").read_bytes() == (source / "roster" / "roster.json").read_bytes()
    assert not (target / "roster" / "notes.txt").exists()
    providers = json.loads((target / "providers.json").read_text(encoding="utf-8"))
    assert providers["schema_version"] == 1 and [p["id"] for p in providers["providers"]] == ["p1", "p2"], "the environment provider is not stored"
    assert all("api_key" not in p for p in providers["providers"])
    assert providers["providers"][0]["models"][0]["id"] == "m1" and providers["providers"][0]["source"] == "encrypted"


def test_the_old_workspace_is_not_touched_at_all(workspace: Path, tmp_path: Path) -> None:
    before = fingerprint(workspace)
    code, _ = convert(workspace, tmp_path / "out.arenaagents", "--include-keys", passwords=[PASSWORD, PASSWORD])
    assert code == 0
    assert fingerprint(workspace) == before, "every file, every folder, every timestamp of the old workspace is as it was"


def test_no_key_and_no_plain_secret_is_in_an_archive_without_keys(workspace: Path, tmp_path: Path) -> None:
    output = tmp_path / "out.arenaagents"
    code, text = convert(workspace, output)
    assert code == 0
    import zipfile

    with zipfile.ZipFile(output) as zf:
        everything = b"".join(zf.read(name) for name in zf.namelist())
    for secret in (KEY_1, KEY_2, "sk-from-the-environment-0000"):
        assert secret.encode() not in everything and secret not in text
    assert "未包含" in text and "--include-keys" in text


def test_include_keys_encrypts_them_with_the_typed_password(workspace: Path, tmp_path: Path) -> None:
    output = tmp_path / "keyed.arenaagents"
    code, text = convert(workspace, output, "--include-keys", passwords=[PASSWORD, PASSWORD])
    assert code == 0 and "已用密码加密" in text
    for secret in (KEY_1, KEY_2, PASSWORD):
        assert secret not in text and secret.encode() not in output.read_bytes()
    assert importer.inspect(output).has_secrets is True
    assert importer.decrypt_archive_keys(output, PASSWORD) == {"p1": KEY_1, "p2": KEY_2}
    with pytest.raises(ArchivePasswordError):
        importer.decrypt_archive_keys(output, "Wrong-Horse-Battery-9")
    target = tmp_path / "t"
    target.mkdir()
    secrets = FakeSecrets()
    result = importer.apply(output, PASSWORD, data_dir=target, secrets=secrets, clear_caches=lambda: None)
    assert result.keys_imported == 2 and secrets.keys == {"p1": KEY_1, "p2": KEY_2}


def test_a_weak_or_mismatched_password_asks_again_and_gives_up_after_three_tries(workspace: Path, tmp_path: Path) -> None:
    output = tmp_path / "again.arenaagents"
    # try 1: too weak (one answer); try 2: typed twice, different; try 3: typed twice, the same
    code, text = convert(workspace, output, "--include-keys", passwords=["weak", "Strong-Pass-123!", "different-Pass-123!", "Strong-Pass-123!", "Strong-Pass-123!"])
    assert code == 0 and "至少 12 位" in text and "不一致" in text and output.is_file()
    assert importer.decrypt_archive_keys(output, "Strong-Pass-123!")

    failed = tmp_path / "failed.arenaagents"
    code, text = convert(workspace, failed, "--include-keys", passwords=["a", "b", "c"])
    assert code == 2 and not failed.exists()


def test_the_logs_can_be_left_out(workspace: Path, tmp_path: Path) -> None:
    with_logs = tmp_path / "a.arenaagents"
    without = tmp_path / "b.arenaagents"
    assert convert(workspace, with_logs)[0] == 0 and convert(workspace, without, "--no-logs")[0] == 0
    assert importer.inspect(with_logs).has_logs is True and importer.inspect(without).has_logs is False
    assert without.stat().st_size < with_logs.stat().st_size


def test_old_and_damaged_games_are_skipped_and_reported(workspace: Path, tmp_path: Path) -> None:
    code, text = convert(workspace, tmp_path / "out.arenaagents")
    assert code == 0
    assert "转换了 3 局对局" in text and "跳过了 2 局" in text
    assert f"{GAME_OLD}：存档格式 v3" in text and f"{GAME_BROKEN}：解密失败" in text
    assert str(tmp_path / "out.arenaagents") in text and "KB" in text or "MB" in text


def test_the_previous_copy_is_used_when_the_current_one_is_damaged(workspace: Path, tmp_path: Path) -> None:
    (workspace / ".arena" / "sessions" / GAME_DONE / "current.json.enc").write_bytes(b"garbage")
    # a file that is only touched in the copy, so the source stays byte-identical for the next assertion
    code, text = convert(workspace, tmp_path / "out.arenaagents")
    assert code == 0 and "转换了 3 局对局" in text and f"对局 {GAME_DONE} 的主文件无法读取" in text


def test_the_provider_backup_is_used_when_the_registry_is_damaged(workspace: Path, tmp_path: Path) -> None:
    (workspace / ".arena" / "providers.v1.enc").write_bytes(b"garbage")
    output = tmp_path / "out.arenaagents"
    code, text = convert(workspace, output, "--include-keys", passwords=[PASSWORD, PASSWORD])
    assert code == 0 and "备份副本" in text
    assert importer.decrypt_archive_keys(output, PASSWORD) == {"p1": KEY_1, "p2": KEY_2}


def test_a_settings_pointer_to_a_game_that_is_not_converted_is_cleared(workspace: Path, tmp_path: Path) -> None:
    settings_path = workspace / ".arena" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["last_session_id"] = GAME_OLD
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    output = tmp_path / "out.arenaagents"
    assert convert(workspace, output)[0] == 0
    target = tmp_path / "t"
    target.mkdir()
    importer.apply(output, skip_keys=True, data_dir=target, secrets=FakeSecrets(), clear_caches=lambda: None)
    assert json.loads((target / "settings.json").read_text(encoding="utf-8"))["last_session_id"] is None


def test_settings_the_application_cannot_use_are_left_out_with_a_warning(workspace: Path, tmp_path: Path) -> None:
    (workspace / ".arena" / "settings.json").write_text(json.dumps({"schema_version": 9}), encoding="utf-8")
    output = tmp_path / "out.arenaagents"
    code, text = convert(workspace, output)
    assert code == 0 and "settings.json" in text
    assert importer.inspect(output).games == 3


def test_a_workspace_with_nothing_encrypted_needs_no_master_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARENA_MASTER_KEY", raising=False)
    workspace = tmp_path / "w"
    data = workspace / ".arena"
    data.mkdir(parents=True)
    (workspace / ".arena-workspace.json").write_text("{}", encoding="utf-8")
    (data / "roster").mkdir()
    (data / "roster" / "N1.persona.md").write_text("x\n", encoding="utf-8")
    code, text = convert(workspace, tmp_path / "out.arenaagents")
    assert code == 0 and "转换了 0 局对局" in text


# ---------------------------------------------------------------------------------------- failures
def test_without_the_master_key_the_message_says_what_to_do_and_nothing_is_created(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARENA_MASTER_KEY", raising=False)
    monkeypatch.delenv("ARENA_MASTER_KEY_FILE", raising=False)
    if os.name == "nt":
        (workspace / ".arena" / "master.v1.dpapi").unlink()  # the key file of v0.12.1 is gone
    before = fingerprint(workspace)
    output = tmp_path / "out.arenaagents"
    code, out, err = run_tool("--workspace", str(workspace), "--output", str(output))
    assert code == 1 and not output.exists()
    assert "ARENA_MASTER_KEY" in err and ("master.v1.dpapi" in err or os.name != "nt")
    assert fingerprint(workspace) == before, "a missing key must not make the script create one in the old workspace"


def test_a_key_that_does_not_match_skips_what_it_cannot_read(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARENA_MASTER_KEY", base64.urlsafe_b64encode(bytes(range(1, 33))).decode("ascii"))
    output = tmp_path / "out.arenaagents"
    code, text = convert(workspace, output)
    assert code == 0 and "转换了 0 局对局" in text and "解密失败" in text and "Provider 注册表无法解密" in text
    assert importer.inspect(output).games == 0, "settings, roster and price table still come along"


def test_the_output_must_not_be_inside_the_old_workspace_nor_overwrite_a_file(workspace: Path, tmp_path: Path) -> None:
    code, text, _ = run_tool("--workspace", str(workspace), "--output", str(workspace / "out.arenaagents"))
    assert code == 2 and not (workspace / "out.arenaagents").exists()
    existing = tmp_path / "exists.arenaagents"
    existing.write_bytes(b"precious")
    code, text, _ = run_tool("--workspace", str(workspace), "--output", str(existing))
    assert code == 2 and existing.read_bytes() == b"precious"


def test_the_default_output_is_in_the_current_folder(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    here = tmp_path / "cwd"
    here.mkdir()
    monkeypatch.chdir(here)
    code, text, _ = run_tool("--workspace", str(workspace))
    assert code == 0
    [made] = list(here.glob("arena-v0121-*.arenaagents"))
    assert importer.inspect(made).games == 3 and str(made) in text


def test_the_default_workspace_is_found_where_v0121_put_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    make_workspace(home)
    monkeypatch.setattr(legacy, "_home", lambda: home)
    monkeypatch.setattr(legacy.platform, "system", lambda: "Windows")
    code, text, _ = run_tool("--output", str(tmp_path / "found.arenaagents"))
    assert code == 0 and "转换了 3 局对局" in text


def test_without_a_default_workspace_the_message_points_to_the_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(legacy, "_home", lambda: tmp_path / "nothing-here")
    monkeypatch.setattr(legacy.platform, "system", lambda: "Windows")
    code, _, err = run_tool("--output", str(tmp_path / "x.arenaagents"))
    assert code == 1 and "--workspace" in err


def test_the_arena_folder_itself_is_accepted(workspace: Path, tmp_path: Path) -> None:
    output = tmp_path / "out.arenaagents"
    code, _ = convert(workspace / ".arena", output)
    assert code == 0 and importer.inspect(output).games == 3


def test_a_folder_that_is_not_a_workspace_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    code, _, err = run_tool("--workspace", str(empty), "--output", str(tmp_path / "o.arenaagents"))
    assert code == 1 and "不是 v0.12.1 的工作文件夹" in err
    assert run_tool("--workspace", str(tmp_path / "missing"), "--output", str(tmp_path / "o.arenaagents"))[0] == 1


def test_a_symbolic_link_workspace_is_refused(workspace: Path, tmp_path: Path) -> None:
    link = tmp_path / "link"
    try:
        link.symlink_to(workspace, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symbolic links")
    code, _, err = run_tool("--workspace", str(link), "--output", str(tmp_path / "o.arenaagents"))
    assert code == 1 and "符号链接" in err


def test_a_workspace_with_nothing_to_convert_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "w"
    (workspace / ".arena").mkdir(parents=True)
    (workspace / ".arena-workspace.json").write_text("{}", encoding="utf-8")
    code, _, err = run_tool("--workspace", str(workspace), "--output", str(tmp_path / "o.arenaagents"))
    assert code == 1 and "没有可以转换的内容" in err and not (tmp_path / "o.arenaagents").exists()
    assert not list(tmp_path.glob(".arena-convert-*")), "the scratch folder is removed"


def test_the_scratch_folder_is_removed(workspace: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "outdir"
    assert convert(workspace, out_dir / "o.arenaagents")[0] == 0
    assert [p.name for p in out_dir.iterdir()] == ["o.arenaagents"]


# ----------------------------------------------------------------------------------------- as a script
def test_the_script_runs_as_a_program(workspace: Path, tmp_path: Path) -> None:
    help_text = subprocess.run([sys.executable, str(ROOT / "tools" / "migrate_v0121_to_archive.py"), "--help"], capture_output=True, text=True, encoding="utf-8", cwd=tmp_path, env={**os.environ, "PYTHONUTF8": "1"})
    assert help_text.returncode == 0 and "--include-keys" in help_text.stdout and "--no-logs" in help_text.stdout
    output = tmp_path / "from-script.arenaagents"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "migrate_v0121_to_archive.py"), "--workspace", str(workspace), "--output", str(output)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert completed.returncode == 0, completed.stderr
    assert "转换了 3 局对局" in completed.stdout and output.is_file()
    assert KEY_1 not in completed.stdout + completed.stderr
