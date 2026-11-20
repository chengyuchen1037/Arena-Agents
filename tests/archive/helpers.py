"""Builders shared by the archive tests: a populated data directory, hand-made (and tampered) archives, fakes."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Callable

from app.archive.format import FORMAT_NAME, FORMAT_VERSION, Contents, FileEntry, build_manifest, manifest_bytes
from app.game.session import GameSession

GAME_A = "aaaa1111aaaa"
GAME_B = "bbbb2222bbbb"
PROVIDER_1 = "prov-one"
PROVIDER_2 = "prov-two"
KEY_1 = "sk-test-one-0123456789abcdef"
KEY_2 = "sk-test-two-0123456789abcdef"
PASSWORD = "Correct-Horse-Battery-9"


def game_payload(session_id: str, *, status: str = "paused", title: str = "Arena-Agents", winner: str | None = None) -> dict[str, Any]:
    session = GameSession(session_id=session_id, title=title, status=status, scenario_name="werewolf_12p", scenario_title="狼人杀 12 人局")
    payload = session.model_dump(mode="json")
    payload["world"] = {
        "format": 1,
        "clock_now": 12.5,
        "state": {"status": status, "winner": winner, "players": []},
        "ledger": [{"seq": 1, "ts": "2026-11-18T00:00:00+00:00", "gt": 0.0, "type": "message", "actor_id": "moderator", "data": {"text": "你好"}}],
        "modules": {},
    }
    return payload


def providers_payload(*ids: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "providers": [
            {
                "id": pid,
                "name": f"Provider {pid}",
                "protocol": "openai_chat",
                "base_url": "https://api.example.com",
                "models": [
                    {
                        "id": "model-x",
                        "input_price_per_m": 1.0,
                        "output_price_per_m": 2.0,
                        "cache_read_price_per_m": None,
                        "cache_write_price_per_m": None,
                        "context_window_tokens": 128000,
                        "price_source": "manual",
                    }
                ],
                "read_only": False,
                "source": "encrypted",
                "auto_append_path": True,
            }
            for pid in ids
        ],
    }


def dumps(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8")


def populate(
    data_dir: Path,
    *,
    games: tuple[str, ...] = (GAME_A, GAME_B),
    logs: bool = True,
    providers: tuple[str, ...] = (PROVIDER_1, PROVIDER_2),
    last_session: str | None = GAME_A,
    noise: bool = True,
) -> dict[str, bytes]:
    """Write a v0.13 data directory; returns ``{archive path: bytes}`` of what an export must contain."""
    expected: dict[str, bytes] = {}

    def put(name: str, data: bytes) -> None:
        target = data_dir.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        expected[name] = data

    settings = {
        "schema_version": 2,
        "default_bindings": {},
        "last_session_id": last_session,
        "ui": {"budget_defaults": {"max_tokens": 1000}},
        "model_templates": [],
    }
    put("settings.json", dumps(settings))
    put("providers.json", dumps(providers_payload(*providers)))
    put("pricing.json", dumps({"schema": 1, "url": "https://example.com/p.json", "fetched_at": None, "entries": {}}))
    put("roster/roster.json", dumps({"memory_enabled": True, "seats": {"N1": {"name": "Alice"}, "N2": {"name": "Bob"}}}))
    put("roster/N1.persona.md", "说话简短。\n".encode("utf-8"))
    put("roster/N2.memory.md", "## 印象\n- N1 Alice：发言短，站边快。\n\n## 经验\n- 谨慎\n".encode("utf-8"))  # (v0.13.1: impressions and lessons)
    for index, game_id in enumerate(games):
        put(f"games/{game_id}/game.json", dumps(game_payload(game_id, status="finished" if index else "paused", winner="villagers" if index else None)))
        if logs:
            put(f"logs/{game_id}/trace-0001.jsonl", (json.dumps({"event": "tool_call", "n": index}) + "\n").encode("utf-8") * 50)
            put(f"logs/{game_id}/trace-0002.jsonl", b'{"event":"x"}\n')
    put("games/index.json", dumps([{"session_id": game_id} for game_id in games]))
    if noise:
        (data_dir / "secrets.v1.enc").write_bytes(b"local-encrypted-keys")
        (data_dir / "master.v1.dpapi").write_bytes(b"dpapi-wrapper")
        (data_dir / ".lock").write_bytes(b"\0")
        (data_dir / "exports").mkdir(exist_ok=True)
        (data_dir / "exports" / "arena-20200101-000000.arenaagents").write_bytes(b"old export")
        (data_dir / "backups").mkdir(exist_ok=True)
        (data_dir / "tmp").mkdir(exist_ok=True)
        # a previous copy of a game is data, but never goes into an archive
        (data_dir / "games" / games[0] / "game.prev.json").write_bytes(dumps(game_payload(games[0])))
    return expected


LEARNING_JOBS = ("init-20261118-0001", "review-20261118-0002")
MATERIAL_SLUGS = ("opening-notes", "second-material")


def populate_v2(
    data_dir: Path,
    *,
    games: tuple[str, ...] = (GAME_A, GAME_B),
    providers: tuple[str, ...] = (PROVIDER_1, PROVIDER_2),
    last_session: str | None = GAME_A,
    noise: bool = True,
) -> dict[str, bytes]:
    """Write a v0.14 data directory through the real stores - the contract, the rules with two study materials, the 12 bots of the default
    library (the first of them with EVERY kind of file a bot has), the line-up, the games (game A also with a previous copy), the logs, the
    learning logs - and returns ``{archive path: bytes}`` of everything an export must contain: one entry per whitelisted file, nothing else.

    With ``noise`` it also leaves what an export must NOT take along (machine-bound files, the old roster, temporary folders, strays inside the
    folders that are exported)."""
    from app.storage.bots import BotStore, iter_bot_files
    from app.storage.contract import ContractStore
    from app.storage.rules import RulesStore

    expected: dict[str, bytes] = {}

    def put(name: str, data: bytes) -> None:
        target = data_dir.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        expected[name] = data

    def keep(name: str) -> None:
        """A file the stores wrote: it must be in the export, byte for byte."""
        expected[name] = data_dir.joinpath(*name.split("/")).read_bytes()

    settings = {
        "schema_version": 2,
        "default_bindings": {},
        "last_session_id": last_session,
        "ui": {"budget_defaults": {"max_tokens": 1000}},
        "model_templates": [],
    }
    put("settings.json", dumps(settings))
    put("providers.json", dumps(providers_payload(*providers)))
    put("pricing.json", dumps({"schema": 1, "url": "https://example.com/p.json", "fetched_at": None, "entries": {}}))

    # ---- the official contract and the rules (first run: copied from the program's own files)
    ContractStore(data_dir).ensure_installed()
    keep("contract/contract.md")
    keep("contract/source.json")
    rules = RulesStore(data_dir)
    rules.ensure_installed()
    sid = rules.ids()[0]
    keep(f"rules/{sid}/scenario.md")
    keep(f"rules/{sid}/source.json")
    rules.set_material(sid, MATERIAL_SLUGS[0], "# 开局笔记\n\n先看谁的发言和票型对得上。\n", title="开局笔记")
    rules.set_material(sid, MATERIAL_SLUGS[1], "# 第二份材料\n\n一份没有写标题的旁文件的材料。\n")
    for slug in MATERIAL_SLUGS:
        keep(f"rules/{sid}/materials/{slug}.md")
    keep(f"rules/{sid}/materials/{MATERIAL_SLUGS[0]}.json")  # (the sidecar that holds an explicit title)

    # ---- the bots: the default library, the first one with every kind of file
    store = BotStore(data_dir)
    store.bootstrap()
    first = store.lineup()[0]
    merged = store.core_markdown(first).rstrip("\n") + "\n\n# 附加：口头禅\n「行，听你的，出了再说。」\n"
    store.set_core_markdown(first, merged)
    playbook = "## 假设\n- [把握:中|支持:3|反例:1|来源:复盘 2026-11-20|#对跳] 两个人都说自己是预言家时，先看谁的查验和之后的投票对得上。\n"
    for part, scenario, text in (
        ("arch_notes", "", "先听完再表态。\n"),
        ("digest", sid, "规则要点：夜里的信息不对称。\n"),
        ("playbook", sid, playbook),
        ("questions", sid, "- 首夜验谁有没有道理？\n"),
        ("people", "", "- Bob：发言短，站边快。\n"),
    ):
        result = store.write_part(first, part, text, scenario)
        assert result.ok, (part, result)
    store.add_record(first, "平民", True)
    store.stage_core(first, "voice", "你说话短，通常两三句就停。", "复盘发现发言太长", "复盘 2026-11-20")
    put("bots/usage.json", dumps({"schema_version": 1, "totals": {"calls": 3, "tokens": 1200}}))
    keep("bots/lineup.json")
    for bot_id in store.lineup():
        for rel, path in iter_bot_files(store.bot_dir(bot_id)):
            expected[f"bots/{bot_id}/{rel}"] = path.read_bytes()

    # ---- the games and their logs
    for index, game_id in enumerate(games):
        put(f"games/{game_id}/game.json", dumps(game_payload(game_id, status="finished" if index else "paused", winner="villagers" if index else None)))
        put(f"logs/{game_id}/trace-0001.jsonl", (json.dumps({"event": "tool_call", "n": index}) + "\n").encode("utf-8") * 50)
        put(f"logs/{game_id}/trace-0002.jsonl", b'{"event":"x"}\n')
    put(f"games/{games[0]}/game.prev.json", dumps(game_payload(games[0], status="idle")))  # (v0.14: the previous good copy is part of the archive)
    put("games/index.json", dumps([{"session_id": game_id} for game_id in games]))
    for job in LEARNING_JOBS:
        put(f"logs/learning/{job}.jsonl", (json.dumps({"event": "learning", "job": job}) + "\n").encode("utf-8") * 5)

    if noise:
        (data_dir / "secrets.v1.enc").write_bytes(b"local-encrypted-keys")
        (data_dir / "master.v1.dpapi").write_bytes(b"dpapi-wrapper")
        (data_dir / ".lock").write_bytes(b"\0")
        (data_dir / "exports").mkdir(exist_ok=True)
        (data_dir / "exports" / "arena-20200101-000000.arenaagents").write_bytes(b"old export")
        (data_dir / "backups").mkdir(exist_ok=True)
        (data_dir / "backups" / "20200101-000000").mkdir(exist_ok=True)
        (data_dir / "backups" / "20200101-000000" / "settings.json").write_bytes(b"{}")
        (data_dir / "tmp").mkdir(exist_ok=True)
        (data_dir / "tmp" / "import-abc.part").write_bytes(b"half")
        # the v0.13 roster, still lying around after the migration: bots are what an archive of v0.14 holds
        put_noise = {
            "roster/roster.json": b'{"seats": {}}',
            "roster/N1.persona.md": "旧的性格。\n".encode("utf-8"),
            "settings.json.bak": b"{}",
            "bots/stray.txt": b"x",
            f"bots/{first}/notes.txt": b"x",
            f"bots/{first}/core/other.md": b"x",
            f"bots/{first}/rules/{sid}/readme.txt": b"x",
            "bots/b-0000zzzz/bot.json": b"{}",  # (not a bot id)
            "contract/old.md": b"x",
            f"rules/{sid}/readme.txt": b"x",
            f"rules/{sid}/materials/notes.txt": b"x",
            f"rules/{sid}/materials/Bad_Slug.md": b"x",
            "games/stray.txt": b"x",
            f"games/{games[0]}/notes.txt": b"x",
            f"logs/{games[0]}/other.txt": b"x",
            "logs/learning/readme.txt": b"x",
            "logs/learning/sub/deep.jsonl": b"{}\n",
        }
        for name, data in put_noise.items():
            target = data_dir.joinpath(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return expected


def tree(root: Path, *, skip: tuple[str, ...] = ()) -> dict[str, bytes]:
    """Every file under ``root`` (relative posix path -> bytes), for exact before / after comparisons."""
    found: dict[str, bytes] = {}
    for current, dirs, files in os.walk(root):
        for name in files:
            path = Path(current) / name
            rel = path.relative_to(root).as_posix()
            if rel.split("/", 1)[0] in skip:
                continue
            if rel == ".lock":
                # the process lock of a running application: held open (and locked) on Windows, never read
                found[rel] = b""
                continue
            found[rel] = path.read_bytes()
        for name in dirs:
            rel = (Path(current) / name).relative_to(root).as_posix()
            if rel.split("/", 1)[0] in skip:
                continue
            if not any((Path(current) / name).iterdir()):
                found[rel + "/"] = b""
    return found


# ---------------------------------------------------------------------------------- hand-made archives


def members_of(archive: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(archive) as zf:
        return {info.filename: zf.read(info) for info in zf.infolist()}


def _roster_seat_ids(members: dict[str, bytes]) -> set[str]:
    ids = {name.split("/")[1].split(".")[0] for name in members if name.startswith("roster/N")}
    raw = members.get("roster/roster.json")
    if raw:
        seats = json.loads(raw).get("seats", {})
        ids |= {key for key in seats if re.fullmatch(r"N(?:[1-9]|[1-9][0-9])", key)}
    return ids


def _is_v2(names: list[str]) -> bool:
    return any(name.startswith(("bots/", "contract/", "rules/")) or name.startswith("logs/learning/") for name in names)


def write_zip(
    target: Path,
    members: dict[str, bytes],
    *,
    manifest: bool | dict[str, Any] = True,
    contents: dict[str, Any] | None = None,
    configure: Callable[[zipfile.ZipFile], None] | None = None,
    compress: int = zipfile.ZIP_DEFLATED,
    version: int | None = None,
) -> Path:
    """Write an archive with exactly ``members`` (plus a matching manifest, or the given raw manifest dict).

    ``version`` is the format of the manifest; ``None`` = 2 when a member only format 2 has (``bots/``, ``contract/``, ``rules/``,
    ``logs/learning/``) is there, else 1 (the v0.13 archives most tests are about)."""
    names = [name for name in members if name != "manifest.json"]
    files = [FileEntry(path=name, size=len(members[name]), sha256=hashlib.sha256(members[name]).hexdigest()) for name in names]
    version = version if version is not None else (2 if _is_v2(names) else 1)
    with zipfile.ZipFile(target, "w", compression=compress) as zf:
        for name in names:
            zf.writestr(name, members[name])
        if manifest is True:
            kinds: dict[str, Any] = {
                "games": len([n for n in names if n.startswith("games/") and n.endswith("/game.json")]),
                "providers": 0,
                "has_secrets": "secrets.enc" in names,
                "has_logs": any(n.startswith("logs/") for n in names),
            }
            if "providers.json" in members:
                kinds["providers"] = len(json.loads(members["providers.json"]).get("providers", []))
            if version >= 2:
                kinds["bots"] = len([n for n in names if re.fullmatch(r"bots/b-[0-9a-f]{8}/bot\.json", n)])
                kinds["rules"] = len([n for n in names if re.fullmatch(r"rules/[^/]+/scenario\.md", n)])
                kinds["contract"] = "contract/contract.md" in names
            else:
                kinds["roster_seats"] = len(_roster_seat_ids(members))
            if contents:
                kinds.update(contents)
            built = build_manifest(
                archive_id="0123456789abcdef0123456789abcdef",
                created_at="2026-11-18T00:42:00-08:00",
                app_version="v0.14.0" if version >= 2 else "v0.13.0",
                contents=Contents(**kinds),
                files=files,
                format_version=version,
            )
            zf.writestr("manifest.json", manifest_bytes(built))
        elif isinstance(manifest, dict):
            zf.writestr("manifest.json", json.dumps(manifest))
        if configure is not None:
            configure(zf)
    return target


def mutate_archive(
    source: Path,
    target: Path,
    mutate: Callable[[dict[str, bytes], dict[str, Any]], None],
    *,
    fix_manifest: bool = False,
) -> Path:
    """Copy ``source`` to ``target`` after ``mutate(members, manifest_dict)`` changed the members and / or the manifest.

    With ``fix_manifest`` the manifest's list and digest are recomputed from the members (a forger who knows the format);
    otherwise it is written as the mutation left it."""
    members = members_of(source)
    manifest = json.loads(members.pop("manifest.json"))
    mutate(members, manifest)
    if fix_manifest:
        entries = [FileEntry(path=name, size=len(data), sha256=hashlib.sha256(data).hexdigest()) for name, data in members.items()]
        manifest["files"] = [entry.model_dump() for entry in sorted(entries, key=lambda e: e.path)]
        from app.archive.format import files_digest

        manifest["files_sha256"] = files_digest(entries)
        manifest["contents"]["has_secrets"] = "secrets.enc" in members
        manifest["contents"]["has_logs"] = any(name.startswith("logs/") for name in members)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
        zf.writestr("manifest.json", json.dumps(manifest, indent=1))
    return target


# ------------------------------------------------------------------------------------------------ fakes


class FakeSecrets:
    """An in-memory ``SecretStore``."""

    def __init__(self, keys: dict[str, str] | None = None, *, fail_write: bool = False, fail_read: bool = False) -> None:
        self.keys = dict(keys or {})
        self.fail_write = fail_write
        self.fail_read = fail_read
        self.writes: list[dict[str, str]] = []

    def read_all(self) -> dict[str, str]:
        if self.fail_read:
            from app.storage.errors import StorageError

            raise StorageError("no master key")
        return dict(self.keys)

    def write_all(self, keys: dict[str, str]) -> None:
        if self.fail_write:
            from app.storage.errors import StorageError

            raise StorageError("cannot write")
        self.writes.append(dict(keys))
        self.keys = dict(keys)


class FileSecrets:
    """A key store that keeps ``secrets.v1.enc`` (plain JSON here) in a data directory, like the real ``SecretStore``."""

    def __init__(self, data_dir: Path, *, fail_after_write: bool = False) -> None:
        self.path = Path(data_dir) / "secrets.v1.enc"
        self.fail_after_write = fail_after_write
        self.writes = 0

    def read_all(self) -> dict[str, str]:
        from app.storage.errors import StorageError

        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError:
            raise StorageError("the key file cannot be decrypted") from None

    def write_all(self, keys: dict[str, str]) -> None:
        self.writes += 1
        self.path.write_text(json.dumps(keys), encoding="utf-8")
        if self.fail_after_write:
            from app.storage.errors import StorageError

            raise StorageError("disk failure after the write")


class FakeBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def publish(self, event: Any) -> int:
        self.events.append(event)
        return len(self.events)


class FakeEngine:
    """The slice of the orchestrator the jobs use."""

    def __init__(self) -> None:
        self.bus = FakeBus()
        self.active = False
        self.flushes: list[str] = []
        self.reloads = 0
        self.fail_reload = False
        self.suspended_depth = 0
        self.suspended_entered = 0
        self.flush_result = True

    def is_game_active(self) -> bool:
        return self.active

    async def flush_autosave(self, reason: str) -> bool:
        self.flushes.append(reason)
        return self.flush_result

    async def reload_from_data_dir(self) -> Any:
        self.reloads += 1
        if self.fail_reload and self.reloads == 1:
            raise RuntimeError("cannot reload")
        return None

    @contextlib.asynccontextmanager
    async def persistence_suspended(self):  # type: ignore[no-untyped-def]
        self.suspended_depth += 1
        self.suspended_entered += 1
        try:
            yield
        finally:
            self.suspended_depth -= 1

    def archive_events(self) -> list[dict[str, Any]]:
        return [event.data for event in self.bus.events if event.type == "archive.progress"]


__all__ = [
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "GAME_A",
    "GAME_B",
    "KEY_1",
    "KEY_2",
    "LEARNING_JOBS",
    "MATERIAL_SLUGS",
    "PASSWORD",
    "PROVIDER_1",
    "PROVIDER_2",
    "FakeBus",
    "FakeEngine",
    "FakeSecrets",
    "FileSecrets",
    "dumps",
    "game_payload",
    "members_of",
    "mutate_archive",
    "populate",
    "populate_v2",
    "providers_payload",
    "tree",
    "write_zip",
]
