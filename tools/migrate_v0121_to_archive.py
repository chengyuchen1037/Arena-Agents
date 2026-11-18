#!/usr/bin/env python
"""Convert a v0.12.1 workspace into a v0.13 ``.arenaagents`` archive (decision F33).

v0.13 keeps its data in ``<program folder>/data`` and has no "workspace" any more.  To bring the games, providers, roster,
settings and logs of a v0.12.1 workspace along: run this script, then open Arena-Agents -> ⚙ 设置 -> 导入存档 and pick
the file it wrote.

    python tools/migrate_v0121_to_archive.py                      # the default workspace, no API keys
    python tools/migrate_v0121_to_archive.py --workspace D:/Arena-Workspaces/Mine --output mine.arenaagents
    python tools/migrate_v0121_to_archive.py --include-keys       # asks for a password (typed twice) that encrypts the keys
    python tools/migrate_v0121_to_archive.py --no-logs            # leave the thinking logs out (much smaller)

The old workspace is only READ, never changed (nothing is written, moved or locked in it).  Its API keys and games are
encrypted with the machine key of v0.12.1: on Windows that is the DPAPI-protected ``master.v1.dpapi``, so the script has
to run as the same Windows user; elsewhere set ``ARENA_MASTER_KEY`` / ``ARENA_MASTER_KEY_FILE`` as v0.12.1 did.

Games of save format v4 (v0.12.1) are converted to v5; older games (v0.12.0 and before) and games that cannot be decrypted
are skipped and listed in the report.  The report never prints an API key.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.archive import kdf  # noqa: E402
from app.archive.errors import ArchiveError  # noqa: E402
from app.archive.export import ExportOptions, export_archive  # noqa: E402
from app.archive.legacy import detect_legacy_workspace  # noqa: E402
from app.game.session import SESSION_SCHEMA_VERSION, GameSession  # noqa: E402
from app.storage.crypto import decrypt_json  # noqa: E402
from app.storage.errors import StorageError  # noqa: E402

MARKER = ".arena-workspace.json"
DATA_DIRNAME = ".arena"
SOURCE_SCHEMA = 4
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ROSTER_FILE = re.compile(r"^(?:roster\.json|N(?:[1-9]|[1-9][0-9])\.(?:persona|memory)\.md)$")
_TRACE_FILE = re.compile(r"^trace-[0-9]{4,}\.jsonl$")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class MigrationError(Exception):
    """A problem the user has to fix; the message says how.  ``code`` is the process exit status."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Report:
    workspace: Path
    output: Path | None = None
    converted: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (game id, reason)
    providers: int = 0
    keys_included: bool = False
    keys_found: int = 0
    logs_included: bool = False
    size_bytes: int = 0
    warnings: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            "转换完成。" if self.output is not None else "没有生成存档。",
            f"  旧工作区：{self.workspace}（只读，没有改动）",
            f"  转换了 {len(self.converted)} 局对局",
        ]
        if self.skipped:
            out.append(f"  跳过了 {len(self.skipped)} 局：")
            out.extend(f"    - {game_id}：{reason}" for game_id, reason in self.skipped)
        out.append(f"  Provider：{self.providers} 个")
        if self.keys_included:
            out.append(f"  API Key：已用密码加密写入存档（{self.keys_found} 个 Provider 的 Key）")
        else:
            out.append("  API Key：未包含（导入后请在 Provider 对话框里重新填写；需要带上 Key 请加 --include-keys）")
        out.append(f"  思考日志：{'已包含' if self.logs_included else '未包含'}")
        for warning in self.warnings:
            out.append(f"  注意：{warning}")
        if self.output is not None:
            out.append(f"  输出文件：{self.output}（{_human(self.size_bytes)}）")
            out.append("下一步：启动 Arena-Agents，打开顶栏「⚙ 设置」→「导入存档」，选择这个文件。")
        return out


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


# ------------------------------------------------------------------------------------------- locating
def locate(workspace: str | os.PathLike[str] | None) -> tuple[Path, Path]:
    """``(workspace folder, its .arena data folder)``; accepts the ``.arena`` folder itself as well."""
    if workspace is None:
        found = detect_legacy_workspace()
        if found is None:
            raise MigrationError(
                "找不到 v0.12.1 的默认工作文件夹（Windows / macOS：用户文件夹下的 Arena-Agents/Default）。\n"
                "如果你的工作文件夹在别处，请用 --workspace <路径> 指定。"
            )
        workspace = found
    given = Path(workspace).expanduser()
    if given.is_symlink():
        raise MigrationError("为了安全，不处理符号链接形式的工作文件夹。")
    if not given.is_dir():
        raise MigrationError("找不到这个文件夹，请检查 --workspace 的路径。")
    root = given.resolve()
    data = root / DATA_DIRNAME
    if (root / MARKER).is_file() and data.is_dir() and not data.is_symlink():
        return root, data
    if (root / "providers.v1.enc").is_file() or (root / "sessions").is_dir():
        return root.parent, root  # the .arena folder itself
    raise MigrationError(f"这个文件夹不是 v0.12.1 的工作文件夹（里面没有 {MARKER} 和 {DATA_DIRNAME} 文件夹）。")


def _encrypted_files(data: Path) -> list[Path]:
    files = [data / "providers.v1.enc", data / "providers.v1.backup.enc"]
    sessions = data / "sessions"
    if sessions.is_dir() and not sessions.is_symlink():
        for entry in sorted(sessions.iterdir()):
            files += [entry / "current.json.enc", entry / "previous.json.enc"]
    return [path for path in files if path.is_file()]


def ensure_master_key(data: Path) -> None:
    """Fail early and clearly when nothing could unlock the old data.  Never creates a key file in the old workspace."""
    if not _encrypted_files(data):
        return
    if os.environ.get("ARENA_MASTER_KEY", "").strip() or os.environ.get("ARENA_MASTER_KEY_FILE", "").strip():
        return
    if os.name == "nt":
        if (data / "master.v1.dpapi").is_file():
            return
        raise MigrationError(
            "旧工作区里没有主密钥文件 master.v1.dpapi，加密的数据无法解开。\n"
            "如果 v0.12.1 当时用的是环境变量 ARENA_MASTER_KEY / ARENA_MASTER_KEY_FILE，请在运行本脚本前设置同样的值。"
        )
    raise MigrationError(
        "需要 v0.12.1 用的主密钥才能解开旧数据：请先设置环境变量 ARENA_MASTER_KEY（或 ARENA_MASTER_KEY_FILE），值与 v0.12.1 的相同。"
    )


def _key_problem(exc: BaseException) -> bool:
    """A failure of the master key itself (DPAPI refused, no key) rather than of one damaged file."""
    text = str(exc)
    return any(word in text for word in ("DPAPI", "master key", "ARENA_MASTER_KEY"))


# -------------------------------------------------------------------------------------- reading legacy
def _decrypt(data: Path, name: str, purpose: str) -> Any:
    path = data / name
    if path.is_symlink():
        raise StorageError("symbolic link")
    return decrypt_json(path.read_bytes(), purpose, data_dir=data)


def read_providers(data: Path, report: Report) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The provider registry without the keys, and the keys: ``([provider...], {provider_id: api_key})``."""
    last: BaseException | None = None
    for name in ("providers.v1.enc", "providers.v1.backup.enc"):
        if not (data / name).is_file():
            continue
        try:
            payload = _decrypt(data, name, "providers.v1")
        except (StorageError, OSError, ValueError) as exc:
            if _key_problem(exc):
                raise MigrationError(_key_message(exc)) from None
            last = exc
            continue
        providers = payload.get("providers") if isinstance(payload, dict) else None
        if not isinstance(providers, list):
            last = ValueError("registry")
            continue
        clean: list[dict[str, Any]] = []
        keys: dict[str, str] = {}
        for item in providers:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            if item.get("read_only") or item.get("source") == "environment":
                continue  # the environment provider is made from DEEPSEEK_* variables, never stored
            provider = dict(item)
            key = provider.pop("api_key", "")
            if isinstance(key, str) and key.strip():
                keys[provider["id"]] = key
            clean.append(provider)
        if name != "providers.v1.enc":
            report.warnings.append("Provider 注册表的主文件无法读取，改用了它的备份副本。")
        return clean, keys
    if last is not None:
        report.warnings.append("Provider 注册表无法解密（主密钥不匹配或文件已损坏），存档里没有 Provider。")
    return [], {}


def _key_message(exc: BaseException) -> str:
    if os.name == "nt":
        return (
            "无法用当前的 Windows 用户解开旧工作区的主密钥。请用创建这个工作区的同一个 Windows 用户运行本脚本，"
            "或者设置环境变量 ARENA_MASTER_KEY（与 v0.12.1 相同）。"
        )
    return "无法取得旧工作区的主密钥：请检查 ARENA_MASTER_KEY / ARENA_MASTER_KEY_FILE 是否与 v0.12.1 使用的相同。"


def _to_utc(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc).isoformat()


def convert_game(payload: dict[str, Any], *, fallback_ts: str | None = None) -> dict[str, Any]:
    """A v0.12.1 game (save format 4) as a v0.13 game (format 5): ``takeover``, ``human_spans``, ``created_at``,
    ``started_at`` and ``finished_at`` are added (taken from the ledger's timestamps).  Raises ``ValueError`` (with a
    message for the report) when the game cannot be converted."""
    schema = payload.get("schema_version")
    if schema == SESSION_SCHEMA_VERSION:
        return dict(payload)
    if schema != SOURCE_SCHEMA:
        raise ValueError(f"存档格式 v{schema}，只能转换 v{SOURCE_SCHEMA}（v0.12.1）的对局")
    world = payload.get("world")
    ledger = world.get("ledger") if isinstance(world, dict) else None
    if not isinstance(ledger, list):
        raise ValueError("对局里没有账本")
    created = started = finished = None
    for entry in ledger:
        if not isinstance(entry, dict):
            continue
        ts = _to_utc(entry.get("ts"))
        if created is None:
            created = ts
        if entry.get("type") != "game.status":
            continue
        status = (entry.get("data") or {}).get("status") if isinstance(entry.get("data"), dict) else None
        if status == "running":
            if started is None:
                started = ts
            finished = None  # continued after a verdict
        elif status == "finished":
            finished = ts
    converted = dict(payload)
    converted.update(
        schema_version=SESSION_SCHEMA_VERSION,
        takeover={"seat": None},
        human_spans=[],
        created_at=created or fallback_ts,
        started_at=started,
        finished_at=finished,
    )
    try:
        GameSession.model_validate(converted)
    except Exception:  # noqa: BLE001 - pydantic can echo values: say only that it does not fit
        raise ValueError("对局内容不符合 v5 的格式") from None
    return converted


def read_games(data: Path, report: Report) -> dict[str, dict[str, Any]]:
    """``{game id: v5 payload}`` for every game that could be read and converted; the others go to the report."""
    sessions = data / "sessions"
    games: dict[str, dict[str, Any]] = {}
    if not sessions.is_dir() or sessions.is_symlink():
        return games
    for entry in sorted(sessions.iterdir(), key=lambda item: item.name):
        game_id = entry.name
        if entry.is_symlink() or not entry.is_dir():
            continue
        if not _SESSION_ID.fullmatch(game_id) or game_id.upper() in _RESERVED:
            report.skipped.append((game_id[:40], "文件夹名不是合法的对局编号"))
            continue
        payload: Any = None
        reason = "解密失败（文件损坏，或主密钥不匹配）"
        for filename in ("current.json.enc", "previous.json.enc"):
            if not (entry / filename).is_file():
                continue
            try:
                payload = _decrypt(data, f"sessions/{game_id}/{filename}", f"session.v1:{game_id}")
            except (StorageError, OSError, ValueError) as exc:
                if _key_problem(exc):
                    raise MigrationError(_key_message(exc)) from None
                payload = None
                continue
            if isinstance(payload, dict):
                if filename != "current.json.enc":
                    report.warnings.append(f"对局 {game_id} 的主文件无法读取，使用了上一份副本。")
                break
            payload = None
        if payload is None:
            report.skipped.append((game_id, reason))
            continue
        if payload.get("session_id") != game_id:
            report.skipped.append((game_id, "文件里的对局编号与文件夹名不一致"))
            continue
        try:
            mtime = max((entry / name).stat().st_mtime for name in ("current.json.enc", "previous.json.enc") if (entry / name).is_file())
            games[game_id] = convert_game(payload, fallback_ts=datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat())
        except ValueError as exc:
            report.skipped.append((game_id, str(exc)))
    return games


# ------------------------------------------------------------------------------------------- building
def _plain_files(directory: Path, pattern: re.Pattern[str]) -> list[Path]:
    if not directory.is_dir() or directory.is_symlink():
        return []
    return [p for p in sorted(directory.iterdir()) if pattern.fullmatch(p.name) and p.is_file() and not p.is_symlink()]


def _write_json(path: Path, value: Any, *, indent: int | None = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=indent, separators=None if indent else (",", ":")), encoding="utf-8")


def _stage_settings(data: Path, stage: Path, game_ids: set[str], report: Report) -> None:
    source = data / "settings.json"
    if not source.is_file() or source.is_symlink():
        return
    try:
        settings = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        report.warnings.append("settings.json 无法读取，已跳过（默认模型绑定、模板和预算默认值不会带过来）。")
        return
    if settings.get("last_session_id") not in game_ids:
        settings["last_session_id"] = None
    _write_json(stage / "settings.json", settings, indent=None)
    from app.storage.settings import SettingsStore

    try:
        SettingsStore(stage).get_settings()
    except Exception:  # noqa: BLE001
        (stage / "settings.json").unlink(missing_ok=True)
        report.warnings.append("settings.json 的内容本版本无法使用，已跳过（默认模型绑定、模板和预算默认值不会带过来）。")


def _stage_pricing(data: Path, stage: Path, report: Report) -> None:
    source = data / "pricing.v1.json"
    if not source.is_file() or source.is_symlink():
        return
    try:
        if not isinstance(json.loads(source.read_text(encoding="utf-8")), dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        report.warnings.append("价格表无法读取，已跳过（导入后可在 Provider 对话框里「刷新价格表」）。")
        return
    shutil.copyfile(source, stage / "pricing.json")


class _MemoryKeys:
    """The keys of the old workspace as the key store the exporter reads."""

    def __init__(self, keys: dict[str, str]) -> None:
        self._keys = dict(keys)

    def read_all(self) -> dict[str, str]:
        return dict(self._keys)

    def write_all(self, keys: dict[str, str]) -> None:  # pragma: no cover - the exporter never writes
        raise AssertionError("read-only")


def build_archive(
    workspace: Path,
    data: Path,
    output: Path,
    *,
    password: str | None,
    include_logs: bool,
    report: Report,
) -> Report:
    ensure_master_key(data)
    providers, keys = read_providers(data, report)
    games = read_games(data, report)
    report.providers = len(providers)
    report.keys_found = len(keys)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".arena-convert-", dir=output.parent))
    try:
        for game_id, payload in games.items():
            _write_json(stage / "games" / game_id / "game.json", payload, indent=None)
        if providers:
            _write_json(stage / "providers.json", {"schema_version": 1, "providers": providers})
        _stage_settings(data, stage, set(games), report)
        _stage_pricing(data, stage, report)
        for source in _plain_files(data / "roster", _ROSTER_FILE):
            (stage / "roster").mkdir(exist_ok=True)
            shutil.copyfile(source, stage / "roster" / source.name)
        if not any(stage.iterdir()):
            raise MigrationError("旧工作区里没有可以转换的内容（没有对局、Provider、设置或名册）。")
        include_keys = password is not None
        if include_keys and not keys:
            report.warnings.append("旧工作区里没有找到任何 API Key，存档里不含 API Key。")
        result = export_archive(
            stage,
            ExportOptions(include_keys=include_keys and bool(keys), password=password, include_logs=include_logs),
            output=output,
            logs_dir=data / "logs",
            secrets=_MemoryKeys(keys),
        )
    except ArchiveError as exc:
        raise MigrationError(f"生成存档失败：{exc.message}") from None
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    report.output = result.path
    report.size_bytes = result.size_bytes
    report.keys_included = bool(result.contents.get("has_secrets"))
    report.logs_included = bool(result.contents.get("has_logs"))
    report.converted = sorted(games)
    report.warnings.extend(w for w in result.warnings if w not in report.warnings)
    return report


# ----------------------------------------------------------------------------------------------- CLI
def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def default_output() -> Path:
    return Path.cwd() / f"arena-v0121-{datetime.now().strftime('%Y%m%d-%H%M%S')}.arenaagents"


def ask_password(getpass_fn: Callable[[str], str], out: TextIO) -> str:
    """The archive password, typed twice, three tries; the rule is the one the settings dialog enforces."""
    print(f"API Key 会用密码加密后写进存档。密码规则：{kdf.PASSWORD_RULE_TEXT}。忘记密码就无法恢复 Key。", file=out)
    for _ in range(3):
        first = getpass_fn("设置存档密码：")
        problem = kdf.password_problem(first)
        if problem:
            print(f"  {problem}", file=out)
            continue
        if getpass_fn("再输入一遍：") != first:
            print("  两次输入的密码不一致。", file=out)
            continue
        return first
    raise MigrationError("密码没有设置成功，已退出（没有生成任何文件）。", code=2)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", help="the v0.12.1 workspace folder (default: the default one of this platform)")
    parser.add_argument("--output", help="the .arenaagents file to write (default: ./arena-v0121-<time>.arenaagents; never overwritten)")
    parser.add_argument("--include-keys", action="store_true", help="also put the API keys in, encrypted with a password you type (twice)")
    parser.add_argument("--no-logs", action="store_true", help="leave the thinking logs out of the archive")
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = parse_args(argv)
    try:
        workspace, data = locate(args.workspace)
        output = Path(args.output).expanduser() if args.output else default_output()
        output = output.resolve()
        if _inside(output, workspace) or _inside(output, data):
            raise MigrationError("输出文件不能放在旧工作区里面（旧工作区只读，不会被改动）。请用 --output 指定别处。", code=2)
        if output.exists():
            raise MigrationError("输出文件已经存在，不会覆盖它。请用 --output 换一个文件名。", code=2)
        if output.suffix != ".arenaagents":
            print("注意：存档文件的后缀应该是 .arenaagents。", file=out)
        password = ask_password(getpass_fn, out) if args.include_keys else None
        report = build_archive(workspace, data, output, password=password, include_logs=not args.no_logs, report=Report(workspace))
    except MigrationError as exc:
        print(f"出错：{exc}", file=err)
        return exc.code
    for line in report.lines():
        print(line, file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
