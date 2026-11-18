"""Packing the data directory into a ``.arenaagents`` file (docs/archive.md "导出").

What goes in (``<data>`` = the data directory): ``settings.json``, ``providers.json``, ``pricing.json``, ``roster/``,
``games/index.json``, ``games/<id>/game.json`` and - optionally - ``logs/<id>/trace-*.jsonl``; the API keys only as
``secrets.enc`` (encrypted with the user's password).  What never goes in: ``secrets.v1.enc`` (encrypted with this
machine's key), ``master.v1.dpapi``, ``.lock``, ``backups/``, ``exports/``, ``tmp/`` and ``game.prev.json``.

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
    KIND_GAME,
    KIND_GAME_PREV,
    KIND_GAMES_INDEX,
    KIND_LOG,
    KIND_PRICING,
    KIND_PROVIDERS,
    KIND_ROSTER_JSON,
    KIND_ROSTER_TEXT,
    KIND_SETTINGS,
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

logger = logging.getLogger(__name__)

_GAME_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TRACE_NAME = re.compile(r"^trace-[0-9]{4,}\.jsonl$")
_ROSTER_TEXT = re.compile(r"^N(?:[1-9]|[1-9][0-9])\.(?:persona|memory)\.md$")
_ZIP64_HINT = 1 << 30

PHASE_COLLECT = "collect"
PHASE_ENCRYPT = "encrypt"
PHASE_PACK = "pack"
PHASE_FINALIZE = "finalize"
EXPORT_PHASES = (PHASE_COLLECT, PHASE_ENCRYPT, PHASE_PACK, PHASE_FINALIZE)


@dataclass(frozen=True)
class ExportOptions:
    include_keys: bool = False
    password: str | None = field(default=None, repr=False)
    include_logs: bool = True


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
    game_id: str | None
    size: int
    mtime: float


def app_version_label() -> str:
    """``v0.13.0`` (the application's version, always with the leading ``v``)."""
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


def collect_sources(
    data_dir: Path, *, include_logs: bool, warnings: list[str], logs_dir: Path | None = None
) -> list[_Source]:
    """The files of ``data_dir`` an archive may hold, sorted by archive path.  Symbolic links are skipped (with a
    warning), unknown files are ignored.  ``logs_dir`` reads the trace logs from somewhere else than ``<data>/logs``
    (the conversion script packs the logs of a v0.12.1 workspace without copying them)."""
    found: list[_Source] = []

    def add(name: str, path: Path, kind: str, game_id: str | None = None) -> None:
        info = _plain_file(path)
        if info is None:
            if path.is_symlink():
                warnings.append(f"已跳过符号链接：{safe_text(name)}")
            return
        found.append(_Source(name, path, kind, game_id, info.st_size, info.st_mtime))

    for name, kind in (("settings.json", KIND_SETTINGS), ("providers.json", KIND_PROVIDERS), ("pricing.json", KIND_PRICING)):
        add(name, data_dir / name, kind)

    roster = data_dir / "roster"
    if _plain_dir(roster):
        for entry in _listing(roster):
            if entry.name == "roster.json":
                add("roster/roster.json", roster / entry.name, KIND_ROSTER_JSON)
            elif _ROSTER_TEXT.match(entry.name):
                add(f"roster/{entry.name}", roster / entry.name, KIND_ROSTER_TEXT)

    games = data_dir / "games"
    if _plain_dir(games):
        for entry in _listing(games):
            if entry.name == "index.json":
                add("games/index.json", games / entry.name, KIND_GAMES_INDEX)
            elif _GAME_ID_OK.match(entry.name) and not is_reserved_id(entry.name) and _plain_dir(games / entry.name):
                add(f"games/{entry.name}/game.json", games / entry.name / "game.json", KIND_GAME, entry.name)
                add(f"games/{entry.name}/game.prev.json", games / entry.name / "game.prev.json", KIND_GAME_PREV, entry.name)

    logs = logs_dir if logs_dir is not None else data_dir / "logs"
    if include_logs and _plain_dir(logs):
        for entry in _listing(logs):
            if not (_GAME_ID_OK.match(entry.name) and not is_reserved_id(entry.name) and _plain_dir(logs / entry.name)):
                continue
            for item in _listing(logs / entry.name):
                if _TRACE_NAME.match(item.name):
                    add(f"logs/{entry.name}/{item.name}", logs / entry.name / item.name, KIND_LOG, entry.name)

    found.sort(key=lambda source: source.name)
    return found


# -------------------------------------------------------------------------------------------------- packing


def _zip_info(name: str, mtime: float) -> zipfile.ZipInfo:
    stamp = time.localtime(max(mtime, 315532800))  # the zip format cannot hold years before 1980
    info = zipfile.ZipInfo(name, date_time=stamp[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3  # Unix: lets the extractor see "regular file, 0644"
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


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
    if source.size > limit:
        raise ArchiveFormatError(f"{safe_text(source.name)} 太大了（{source.size} 字节）。")
    with open(source.path, "rb") as reader:
        data = reader.read(limit + 1)
    if len(data) > limit:
        raise ArchiveFormatError(f"{safe_text(source.name)} 太大了。")
    return data


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
    if options.include_keys:
        kdf.check_password(options.password)  # before any work: the user can fix it at once
    created = (now or datetime.now().astimezone()).replace(microsecond=0)
    if created.tzinfo is None:
        created = created.astimezone()
    exports = Path(exports_dir) if exports_dir is not None else data_dir / "exports"
    warnings: list[str] = []
    archive_id = uuid.uuid4().hex

    progress(PHASE_COLLECT, 0, 1)
    sources = collect_sources(data_dir, include_logs=options.include_logs, warnings=warnings, logs_dir=logs_dir)
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
                    _pack(packer, sources, inventory, warnings, provider_bytes, limits)
                    if secret_blob is not None:
                        packer.add_bytes(SECRETS_NAME, secret_blob, time.time())
                    contents = Contents(
                        games=len(inventory.games),
                        providers=len(inventory.provider_ids),
                        roster_seats=len(inventory.seat_ids),
                        has_secrets=secret_blob is not None,
                        has_logs=any(entry.path.startswith("logs/") for entry in packer.entries),
                    )
                    manifest = build_manifest(
                        archive_id=archive_id,
                        created_at=created.isoformat(timespec="seconds"),
                        app_version=app_version or app_version_label(),
                        contents=contents,
                        files=packer.entries,
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
        contents=contents.model_dump(),
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
) -> None:
    """Write every source, checking the content first; a damaged game is left out together with its logs."""
    groups: dict[str, dict[str, _Source]] = {}
    for source in sources:
        if source.kind in (KIND_GAME, KIND_GAME_PREV) and source.game_id:
            groups.setdefault(source.game_id, {})[source.kind] = source
    # the member that stands for a game in the sorted walk: its main file, or its previous copy when that is all there is
    triggers = {id(group.get(KIND_GAME) or group[KIND_GAME_PREV]) for group in groups.values()}
    exported: set[str] = set()

    for source in sources:
        kind, game_id = classify(source.name)
        if kind in (KIND_GAME, KIND_GAME_PREV):
            assert game_id is not None
            if id(source) not in triggers:
                packer.tick(source.size)
                continue
            data = _good_game(groups[game_id], inventory, warnings, limits)
            packer.tick(source.size)
            if data is not None:
                exported.add(game_id)
                packer.add_bytes(f"games/{game_id}/game.json", data, source.mtime)
            continue
        if kind == KIND_LOG:
            if game_id in exported:
                packer.add_stream(source)
            else:
                packer.tick(source.size)
            continue
        try:
            data = provider_bytes if kind == KIND_PROVIDERS and provider_bytes is not None else _read_capped(source, limits.max_json_bytes)
            check_member(kind, game_id, source.name, data, inventory)
        except ArchiveFormatError as exc:
            if kind == KIND_PROVIDERS:
                raise
            warnings.append(f"已跳过无法使用的文件：{safe_text(source.name)}（{exc.message}）")
            packer.tick(source.size)
            continue
        packer.tick(source.size)
        packer.add_bytes(source.name, data, source.mtime)


def _good_game(group: dict[str, _Source], inventory: Inventory, warnings: list[str], limits: ArchiveLimits) -> bytes | None:
    """The bytes to store as ``game.json`` of one game: its main file, else the previous copy, else ``None``."""
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
        return data
    warnings.append(f"已跳过无法读取的对局：{safe_text(game_id)}")
    return None
