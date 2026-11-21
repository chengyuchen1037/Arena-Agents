"""Packing the data directory into a ``.arenaagents`` file (docs/archive.md "导出"; format 2: docs/v0.14-plan.md §2.5).

What goes in (``<data>`` = the data directory) - the **whole** data directory except what is machine-bound or temporary:

* ``settings.json``, ``providers.json``, ``pricing.json``;
* ``contract/`` (the official contract and where it came from) and ``rules/<scenario>/`` (the scenario, where it came from, the study
  materials with their titles);
* ``bots/lineup.json``, ``bots/usage.json`` and ``bots/<bot_id>/`` (the bot itself, its L0, notes, memory, impressions, record and the
  L0 proposals waiting for the user);
* ``games/index.json`` and every game: ``games/<id>/game.json`` and its previous good copy ``game.prev.json``;
* every log: ``logs/<id>/trace-*.jsonl`` of the games in the archive and ``logs/learning/<job>.jsonl`` (the logs are no longer optional:
  ``ExportOptions.include_logs`` is ignored for format 2);
* the API keys only as ``secrets.enc`` (encrypted with the user's password).

What never goes in: ``secrets.v1.enc`` (encrypted with this machine's key), ``master.v1.dpapi``, ``.lock``, ``backups/``, ``exports/``,
``tmp/``, the old ``roster/`` of v0.13 and anything the format does not know.  A name is only collected when
:func:`app.archive.format.classify` allows it - the allow-list is written down once, there.

A directory that is still a v0.13 one - it has none of ``bots/``, ``contract/`` and ``rules/``, which the application makes at its first start -
is written as **format 1** (a roster of seats, if it has one; the logs optional), which is what the conversion tool of v0.12.1 workspaces
makes; the importer turns that roster into bots.

The zip is written as a stream (a log may be hundreds of MB), each member is hashed while it is written, the manifest
goes last, and the file only gets its final name when it is complete (``.part`` -> ``arena-<time>.arenaagents``).
A damaged game file is skipped (and reported in ``warnings``) instead of failing the whole export; the previous
copy ``game.prev.json`` stands in for a damaged ``game.json``.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import stat
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.archive import kdf
from app.archive.errors import ArchiveError, ArchiveFormatError, ArchiveIOError, safe_text
from app.archive.format import (
    CHUNK_BYTES,
    DEFAULT_LIMITS,
    FILE_SUFFIX,
    FORMAT_VERSION,
    KIND_GAME,
    KIND_GAME_PREV,
    KIND_GAMES_INDEX,
    KIND_LEARNING_LOG,
    KIND_LOG,
    KIND_PRICING,
    KIND_PROVIDERS,
    KIND_ROSTER_JSON,
    KIND_ROSTER_TEXT,
    KIND_SETTINGS,
    LEGACY_VERSION,
    MANIFEST_NAME,
    SECRETS_NAME,
    ArchiveLimits,
    Contents,
    FileEntry,
    build_manifest,
    classify,
    is_reserved_id,
    manifest_bytes,
)
from app.archive.ports import ProgressFn, SecretsPort, default_secrets, no_progress
from app.archive.validate import Inventory, check_member, endpoint_of, load_json, providers_of
from app.archive.zipcheck import zip_info as _zip_info

logger = logging.getLogger(__name__)

_GAME_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TRACE_NAME = re.compile(r"^trace-[0-9]{4,}\.jsonl$")
_ROSTER_TEXT = re.compile(r"^N(?:[1-9]|[1-9][0-9])\.(?:persona|memory)\.md$")
_ZIP64_HINT = 1 << 30
#: The folders of a format 2 archive that hold a tree of files (the rest are single files / the games and their logs).
_TREE_DIRS = ("contract", "rules", "bots")
_TREE_MAX_DEPTH = 5  # bots/<id>/rules/<scenario>/playbook.md is the deepest allowed name

PHASE_COLLECT = "collect"
PHASE_ENCRYPT = "encrypt"
PHASE_PACK = "pack"
PHASE_FINALIZE = "finalize"
EXPORT_PHASES = (PHASE_COLLECT, PHASE_ENCRYPT, PHASE_PACK, PHASE_FINALIZE)


@dataclass(frozen=True)
class ExportOptions:
    """``include_logs`` only matters for a format 1 archive (v0.13: the logs were optional); a format 2 archive always has them (H36).
    ``format_version`` ``None`` = decided by the data directory (see the module docstring); the application only ever writes 2."""

    include_keys: bool = False
    password: str | None = field(default=None, repr=False)
    include_logs: bool = True
    format_version: int | None = None


@dataclass
class ExportResult:
    path: Path
    name: str
    size_bytes: int
    archive_id: str
    created_at: str
    app_version: str
    contents: dict[str, Any]
    file_count: int
    uncompressed_bytes: int
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Source:
    name: str
    path: Path
    kind: str
    game_id: str | None  # the "key" of :func:`classify`: a game id, a bot id or a scenario id, by kind
    size: int
    mtime: float


def app_version_label() -> str:
    """``v0.13.1`` (the application's version, always with the leading ``v``)."""
    from app.config import APP_VERSION

    return APP_VERSION if APP_VERSION.startswith("v") else f"v{APP_VERSION}"


# ------------------------------------------------------------------------------------------------ collecting


def _plain_file(path: Path) -> os.stat_result | None:
    """``stat`` of a regular file that is not a symbolic link, else ``None`` (links are never followed)."""
    try:
        info = path.lstat()
    except OSError:
        return None
    return info if stat.S_ISREG(info.st_mode) else None


def _plain_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _listing(path: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(path) as handle:
            return sorted(handle, key=lambda entry: entry.name)
    except OSError:
        return []


def decide_version(data_dir: Path, requested: int | None) -> int:
    """The format version to write: what was asked for, else 2 - except for a directory that has none of what makes a v0.14 data directory (the
    ``bots/``, the ``contract/`` and the ``rules/`` that the application makes at its first start): that one is a v0.13 one, or what the
    conversion tool for v0.12.1 workspaces stages, and is written as format 1 - its roster, if it has one, is what the importer turns into bots,
    and its logs stay optional."""
    if requested is not None:
        if requested not in (LEGACY_VERSION, FORMAT_VERSION):
            raise ArchiveError("不支持的存档格式版本。", code="bad_request", status=400)
        return requested
    if any(_plain_dir(data_dir / name) for name in _TREE_DIRS):
        return FORMAT_VERSION
    return LEGACY_VERSION


def collect_sources(
    data_dir: Path,
    *,
    include_logs: bool,
    warnings: list[str],
    logs_dir: Path | None = None,
    version: int = FORMAT_VERSION,
) -> list[_Source]:
    """The files of ``data_dir`` an archive of this ``version`` may hold, sorted by archive path.  Symbolic links are skipped (with a
    warning), unknown files are ignored.  ``logs_dir`` reads the trace logs from somewhere else than ``<data>/logs`` (the conversion
    script packs the logs of a v0.12.1 workspace without copying them).  ``include_logs`` is only honoured for ``version`` 1: the logs
    of a format 2 archive are not optional (H36)."""
    found: list[_Source] = []

    def add(name: str, path: Path, kind: str, key: str | None = None) -> None:
        info = _plain_file(path)
        if info is None:
            if path.is_symlink():
                warnings.append(f"已跳过符号链接：{safe_text(name)}")
            return
        found.append(_Source(name, path, kind, key, info.st_size, info.st_mtime))

    for name, kind in (("settings.json", KIND_SETTINGS), ("providers.json", KIND_PROVIDERS), ("pricing.json", KIND_PRICING)):
        add(name, data_dir / name, kind)

    if version == LEGACY_VERSION:
        roster = data_dir / "roster"
        if _plain_dir(roster):
            for entry in _listing(roster):
                if entry.name == "roster.json":
                    add("roster/roster.json", roster / entry.name, KIND_ROSTER_JSON)
                elif _ROSTER_TEXT.match(entry.name):
                    add(f"roster/{entry.name}", roster / entry.name, KIND_ROSTER_TEXT)
    else:
        for top in _TREE_DIRS:
            _collect_tree(data_dir / top, top, 1, add, warnings)

    games = data_dir / "games"
    if _plain_dir(games):
        for entry in _listing(games):
            if entry.name == "index.json":
                add("games/index.json", games / entry.name, KIND_GAMES_INDEX)
            elif _GAME_ID_OK.match(entry.name) and not is_reserved_id(entry.name) and _plain_dir(games / entry.name):
                add(f"games/{entry.name}/game.json", games / entry.name / "game.json", KIND_GAME, entry.name)
                add(f"games/{entry.name}/game.prev.json", games / entry.name / "game.prev.json", KIND_GAME_PREV, entry.name)

    logs = logs_dir if logs_dir is not None else data_dir / "logs"
    if (include_logs or version >= FORMAT_VERSION) and _plain_dir(logs):
        for entry in _listing(logs):
            if not (_GAME_ID_OK.match(entry.name) and _plain_dir(logs / entry.name)):
                continue
            if version >= FORMAT_VERSION and entry.name.lower() == "learning":
                for item in _listing(logs / entry.name):
                    name = f"logs/learning/{item.name}"
                    try:
                        kind, _key = classify(name, version=version)
                    except ArchiveFormatError:
                        continue
                    if kind == KIND_LEARNING_LOG:
                        add(name, logs / entry.name / item.name, kind)
                continue
            if is_reserved_id(entry.name):
                continue
            for item in _listing(logs / entry.name):
                if _TRACE_NAME.match(item.name):
                    add(f"logs/{entry.name}/{item.name}", logs / entry.name / item.name, KIND_LOG, entry.name)

    found.sort(key=lambda source: source.name)
    return found


def _collect_tree(directory: Path, prefix: str, depth: int, add: Any, warnings: list[str]) -> None:
    """Walk ``contract/``, ``rules/`` or ``bots/`` (to a bounded depth, never through a link): every file whose name the format allows is
    added with the kind and key :func:`classify` says; folders and files it does not know are ignored."""
    if depth > _TREE_MAX_DEPTH:
        return
    try:
        if directory.is_symlink():
            warnings.append(f"已跳过符号链接：{safe_text(prefix)}")
            return
    except OSError:
        return
    for entry in _listing(directory):
        name = f"{prefix}/{entry.name}"
        path = directory / entry.name
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if is_dir:
            _collect_tree(path, name, depth + 1, add, warnings)
            continue
        try:
            kind, key = classify(name, version=FORMAT_VERSION)
        except ArchiveFormatError:
            continue  # (not a file of the format: ignored, like any stray file of the data directory)
        add(name, path, kind, key)


# -------------------------------------------------------------------------------------------------- packing


class _Packer:
    def __init__(self, zf: zipfile.ZipFile, total: int, progress: ProgressFn) -> None:
        self.zf = zf
        self.total = max(total, 1)
        self.done = 0
        self.progress = progress
        self.entries: list[FileEntry] = []
        self.uncompressed = 0
        self._last_report = 0.0

    def tick(self, amount: int, *, force: bool = False) -> None:
        self.done += amount
        now = time.monotonic()
        if force or now - self._last_report >= 0.1:
            self._last_report = now
            self.progress(PHASE_PACK, min(self.done, self.total), self.total)

    def add_bytes(self, name: str, data: bytes, mtime: float) -> None:
        self.zf.writestr(_zip_info(name, mtime), data)
        self.entries.append(FileEntry(path=name, size=len(data), sha256=hashlib.sha256(data).hexdigest()))
        self.uncompressed += len(data)

    def add_stream(self, source: _Source) -> None:
        info = _zip_info(source.name, source.mtime)
        info.file_size = source.size
        digest = hashlib.sha256()
        size = 0
        with open(source.path, "rb") as reader, self.zf.open(info, "w", force_zip64=source.size >= _ZIP64_HINT) as writer:
            while True:
                chunk = reader.read(CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                writer.write(chunk)
                size += len(chunk)
                self.tick(len(chunk))
        self.entries.append(FileEntry(path=source.name, size=size, sha256=digest.hexdigest()))
        self.uncompressed += size


def _read_capped(source: _Source, limit: int) -> bytes:
    """The whole file, at most ``limit`` bytes.  Read in chunks: ``reader.read(limit + 1)`` makes Python allocate ``limit`` bytes up front
    (256 MiB for a game or a bot's file: ~90 ms on Windows, for every one of the dozens of small files of a format 2 archive)."""
    if source.size > limit:
        raise ArchiveFormatError(f"{safe_text(source.name)} 太大了（{source.size} 字节）。")
    chunks: list[bytes] = []
    total = 0
    with open(source.path, "rb") as reader:
        while True:
            chunk = reader.read(CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ArchiveFormatError(f"{safe_text(source.name)} 太大了。")
            chunks.append(chunk)
    return b"".join(chunks)


def _disk_error(exc: OSError) -> ArchiveIOError:
    if exc.errno == errno.ENOSPC:
        return ArchiveIOError("磁盘空间不足，导出已中止。")
    if exc.errno in (errno.EACCES, errno.EPERM):
        return ArchiveIOError("没有权限写入导出目录。")
    return ArchiveIOError("写入导出文件失败，请检查磁盘。")


def _unique_target(exports: Path, now: datetime) -> tuple[Path, Path]:
    for bump in range(0, 600):
        stamp = (now + timedelta(seconds=bump)).strftime("%Y%m%d-%H%M%S")
        final = exports / f"arena-{stamp}{FILE_SUFFIX}"
        part = exports / f"arena-{stamp}{FILE_SUFFIX}.part"
        if not final.exists() and not part.exists():
            return final, part
    raise ArchiveIOError("导出目录里同一时刻的文件太多，请稍后再试。")


def _read_keys(secrets: SecretsPort | None, data_dir: Path, provider_ids: set[str]) -> dict[str, str]:
    from app.storage.errors import StorageError

    store = secrets if secrets is not None else default_secrets(data_dir)
    try:
        everything = store.read_all()
    except StorageError:
        raise ArchiveIOError("无法读取本机保存的 API Key（主密钥不可用或密钥文件已损坏）。") from None
    return {pid: key for pid, key in everything.items() if pid in provider_ids and isinstance(key, str) and key}


def export_archive(
    data_dir: Path,
    options: ExportOptions | None = None,
    *,
    exports_dir: Path | None = None,
    output: Path | None = None,
    logs_dir: Path | None = None,
    secrets: SecretsPort | None = None,
    progress: ProgressFn = no_progress,
    now: datetime | None = None,
    app_version: str | None = None,
    limits: ArchiveLimits = DEFAULT_LIMITS,
) -> ExportResult:
    """Write ``<exports>/arena-<YYYYmmdd-HHMMSS>.arenaagents`` from ``data_dir`` (or exactly ``output``, which must not
    exist yet).

    Blocking (call it from a worker thread).  Raises :class:`ArchiveError` (a weak password, no readable key store,
    a full disk ...); nothing but the final file is left behind, and the final name only exists when the file is whole.
    """
    options = options or ExportOptions()
    data_dir = Path(data_dir).resolve()
    version = decide_version(data_dir, options.format_version)
    if options.include_keys:
        kdf.check_password(options.password)  # before any work: the user can fix it at once
    created = (now or datetime.now().astimezone()).replace(microsecond=0)
    if created.tzinfo is None:
        created = created.astimezone()
    exports = Path(exports_dir) if exports_dir is not None else data_dir / "exports"
    warnings: list[str] = []
    archive_id = uuid.uuid4().hex

    progress(PHASE_COLLECT, 0, 1)
    sources = collect_sources(data_dir, include_logs=options.include_logs, warnings=warnings, logs_dir=logs_dir, version=version)
    if not sources:
        raise ArchiveError("还没有任何数据可以导出。", code="empty", status=409)
    by_name = {source.name: source for source in sources}
    provider_ids: set[str] = set()
    provider_endpoints: dict[str, dict[str, Any]] = {}
    provider_bytes: bytes | None = None
    if "providers.json" in by_name:
        try:
            provider_bytes = _read_capped(by_name["providers.json"], limits.max_json_bytes)
            items = providers_of(load_json(provider_bytes, "providers.json"))  # a providers.json with a key in it stops the export
        except OSError as exc:
            raise _disk_error(exc) from None
        provider_ids = {str(item["id"]) for item in items}
        provider_endpoints = {str(item["id"]): endpoint_of(item) for item in items}
    progress(PHASE_COLLECT, 1, 1)

    secret_blob: bytes | None = None
    if options.include_keys:
        progress(PHASE_ENCRYPT, 0, 1)
        keys = _read_keys(secrets, data_dir, provider_ids)
        if keys:
            secret_blob = kdf.seal_secrets(
                keys, str(options.password), archive_id, {pid: provider_endpoints[pid] for pid in keys}
            )
        else:
            warnings.append("本机没有保存任何可导出的 API Key，存档里不含 API Key。")
        progress(PHASE_ENCRYPT, 1, 1)

    try:
        if output is not None:
            final = Path(output)
            part = final.with_name(final.name + ".part")
            if final.exists() or part.exists():
                raise ArchiveError("输出文件已经存在，请换一个文件名（不会覆盖已有文件）。", code="exists", status=409)
            final.parent.mkdir(parents=True, exist_ok=True)
        else:
            exports.mkdir(mode=0o700, parents=True, exist_ok=True)
            final, part = _unique_target(exports, created)
        inventory = Inventory()
        with open(part, "wb") as handle:
            try:
                with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
                    packer = _Packer(zf, sum(source.size for source in sources), progress)
                    _pack(packer, sources, inventory, warnings, provider_bytes, limits, version)
                    if secret_blob is not None:
                        packer.add_bytes(SECRETS_NAME, secret_blob, time.time())
                    has_logs = any(entry.path.startswith("logs/") for entry in packer.entries)
                    if version >= FORMAT_VERSION:
                        contents = Contents(
                            games=len(inventory.games),
                            providers=len(inventory.provider_ids),
                            bots=len(inventory.bot_ids),
                            contract=inventory.has_contract,
                            rules=len(inventory.scenario_ids),
                            has_secrets=secret_blob is not None,
                            has_logs=has_logs,
                        )
                    else:
                        contents = Contents(
                            games=len(inventory.games),
                            providers=len(inventory.provider_ids),
                            roster_seats=len(inventory.seat_ids),
                            has_secrets=secret_blob is not None,
                            has_logs=has_logs,
                        )
                    manifest = build_manifest(
                        archive_id=archive_id,
                        created_at=created.isoformat(timespec="seconds"),
                        app_version=app_version or app_version_label(),
                        contents=contents,
                        files=packer.entries,
                        format_version=version,
                    )
                    zf.writestr(_zip_info(MANIFEST_NAME, time.time()), manifest_bytes(manifest))
                    packer.tick(0, force=True)
                progress(PHASE_FINALIZE, 0, 1)
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                handle.close()
                _discard(part)
                raise
        os.replace(part, final)
        progress(PHASE_FINALIZE, 1, 1)
    except OSError as exc:
        raise _disk_error(exc) from None
    size = final.stat().st_size
    return ExportResult(
        path=final,
        name=final.name,
        size_bytes=size,
        archive_id=archive_id,
        created_at=manifest.created_at,
        app_version=manifest.app_version,
        contents=contents.to_manifest(),
        file_count=len(manifest.files),
        uncompressed_bytes=packer.uncompressed,
        warnings=warnings,
    )


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove the unfinished export file")


def _pack(
    packer: _Packer,
    sources: list[_Source],
    inventory: Inventory,
    warnings: list[str],
    provider_bytes: bytes | None,
    limits: ArchiveLimits,
    version: int = FORMAT_VERSION,
) -> None:
    """Write every source, checking the content first; a damaged game is left out together with its logs.

    A game is written as ``game.json`` (its main file, else - when that is damaged - the previous copy).  A format 2 archive also carries
    the previous copy as ``game.prev.json`` (H36) when the main file was fine and the previous copy is too: an unusable member would make
    the importer refuse the whole archive, so it is checked first and left out (with a warning) when it is not good."""
    groups: dict[str, dict[str, _Source]] = {}
    for source in sources:
        if source.kind in (KIND_GAME, KIND_GAME_PREV) and source.game_id:
            groups.setdefault(source.game_id, {})[source.kind] = source
    # the member that stands for a game in the sorted walk: its main file, or its previous copy when that is all there is
    triggers = {id(group.get(KIND_GAME) or group[KIND_GAME_PREV]) for group in groups.values()}
    exported: set[str] = set()

    for source in sources:
        kind, key = source.kind, source.game_id
        if kind in (KIND_GAME, KIND_GAME_PREV):
            assert key is not None
            if id(source) not in triggers:
                packer.tick(source.size)
                continue
            data, used_previous = _good_game(groups[key], inventory, warnings, limits)
            packer.tick(source.size)
            if data is not None:
                exported.add(key)
                packer.add_bytes(f"games/{key}/game.json", data, source.mtime)
                previous = groups[key].get(KIND_GAME_PREV)
                if version >= FORMAT_VERSION and previous is not None and not used_previous:
                    _add_previous_copy(packer, previous, key, inventory, warnings, limits)
            continue
        if kind == KIND_LOG:
            if key in exported:
                packer.add_stream(source)
            else:
                packer.tick(source.size)
            continue
        if kind == KIND_LEARNING_LOG:
            packer.add_stream(source)
            continue
        try:
            data = provider_bytes if kind == KIND_PROVIDERS and provider_bytes is not None else _read_capped(source, limits.max_json_bytes)
            check_member(kind, key, source.name, data, inventory)
        except ArchiveFormatError as exc:
            if kind == KIND_PROVIDERS:
                raise
            warnings.append(f"已跳过无法使用的文件：{safe_text(source.name)}（{exc.message}）")
            packer.tick(source.size)
            continue
        except OSError:
            warnings.append(f"已跳过无法读取的文件：{safe_text(source.name)}")
            packer.tick(source.size)
            continue
        packer.tick(source.size)
        packer.add_bytes(source.name, data, source.mtime)


def _add_previous_copy(
    packer: _Packer, previous: _Source, game_id: str, inventory: Inventory, warnings: list[str], limits: ArchiveLimits
) -> None:
    name = f"games/{game_id}/game.prev.json"
    try:
        data = _read_capped(previous, limits.max_json_bytes)
        check_member(KIND_GAME_PREV, game_id, name, data, Inventory())
    except (ArchiveFormatError, OSError) as exc:
        reason = exc.message if isinstance(exc, ArchiveError) else "无法读取"
        warnings.append(f"对局 {safe_text(game_id)} 的备份副本不可用，没有放进存档：{reason}")
        packer.tick(previous.size)
        return
    packer.tick(previous.size)
    packer.add_bytes(name, data, previous.mtime)


def _good_game(
    group: dict[str, _Source], inventory: Inventory, warnings: list[str], limits: ArchiveLimits
) -> tuple[bytes | None, bool]:
    """``(bytes to store as game.json, whether they are the previous copy's)``: the main file, else the previous copy, else ``None``."""
    main = group.get(KIND_GAME)
    prev = group.get(KIND_GAME_PREV)
    game_id = (main or prev).game_id  # type: ignore[union-attr]
    assert game_id is not None
    for candidate, is_prev in ((main, False), (prev, True)):
        if candidate is None:
            continue
        try:
            data = _read_capped(candidate, limits.max_json_bytes)
            probe = Inventory()
            check_member(KIND_GAME, game_id, f"games/{game_id}/game.json", data, probe)
        except (ArchiveFormatError, OSError) as exc:
            reason = exc.message if isinstance(exc, ArchiveError) else "无法读取"
            warnings.append(f"对局 {safe_text(game_id)} 的{'备份副本' if is_prev else '主文件'}不可用：{reason}")
            continue
        inventory.games.update(probe.games)
        if is_prev and main is not None:
            warnings.append(f"对局 {safe_text(game_id)} 的主文件已损坏，存档里使用了它的上一份备份副本。")
        return data, is_prev
    warnings.append(f"已跳过无法读取的对局：{safe_text(game_id)}")
    return None, False
