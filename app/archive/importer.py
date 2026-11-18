"""Reading a ``.arenaagents``: checking it, unpacking it safely and swapping it in (docs/archive.md "导入").

Three steps, each usable on its own:

``inspect(path)``
    Everything that can be known without a password and without touching the data directory: the zip structure, the
    member names (allow-list), the manifest, the file list against the real members, every checksum and every file's
    content (each game has to be a valid save-format-v5 game).  Raises :class:`ArchiveFormatError` with the reason.

``prepare(path, ...)``
    Decrypts the keys (when asked to), unpacks every member into ``<data>/tmp/import-<id>/stage`` *verifying it again*,
    validates the unpacked data and returns a :class:`PreparedImport`.  The data directory is not touched.

``commit(prepared, ...)``
    The only step that changes the data directory: the current data is moved to ``<data>/backups/<time>/``, the unpacked
    data is moved in, the keys are written, the caches are cleared and the application reloads.  Any failure puts the old
    data back.  ``apply`` is ``prepare`` + ``commit``.

Everything here is blocking: run it in a worker thread.  No password, key or file content is ever logged or put into an
exception message.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import struct
import time
import uuid
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from app.archive.errors import (
    ArchiveError,
    ArchiveFormatError,
    ArchiveIOError,
    ArchiveKeysMismatchError,
    ArchiveLimitError,
    ArchivePasswordError,
    ArchiveStateError,
    safe_text,
)
from app.archive.format import (
    CHUNK_BYTES,
    DEFAULT_LIMITS,
    JSON_KINDS,
    KIND_MANIFEST,
    KIND_ROSTER_TEXT,
    KIND_SECRETS,
    KIND_SETTINGS,
    MANIFEST_NAME,
    SECRETS_NAME,
    ArchiveLimits,
    FileEntry,
    Manifest,
    classify,
    parse_manifest,
)
from app.archive.kdf import open_secrets_bound
from app.archive.ports import ProgressFn, SecretsPort, default_secrets, no_progress
from app.archive.validate import Inventory, check_member, endpoint_of, providers_of

logger = logging.getLogger(__name__)

PHASE_VERIFY = "verify"
PHASE_VALIDATE = "validate"
PHASE_BACKUP = "backup"
PHASE_REPLACE = "replace"
PHASE_KEYS = "keys"
PHASE_RELOAD = "reload"
PHASE_FINALIZE = "finalize"
IMPORT_PHASES = (PHASE_VERIFY, PHASE_VALIDATE, PHASE_BACKUP, PHASE_REPLACE, PHASE_KEYS, PHASE_RELOAD, PHASE_FINALIZE)

#: Top-level entries of the data directory an import never moves: the process lock (held open, cannot be renamed on
#: Windows), the machine's master key, and the folders the archive tooling itself lives in.
KEEP_IN_PLACE = frozenset({".lock", "master.v1.dpapi", "backups", "exports", "tmp"})
KEEP_BACKUPS = 3
_BACKUP_NAME = re.compile(r"^[0-9]{8}-[0-9]{6}(?:-[0-9]+)?$")
_ALLOWED_COMPRESSION = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
_MAX_GAMES_LISTED = 100
_DECOMPRESS_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError)


# ------------------------------------------------------------------------------------------------ summary


@dataclass
class ArchiveSummary:
    """What the user is shown before an import (and what the API returns): facts of the archive, never its content."""

    archive_id: str
    format_version: int
    app_version: str
    created_at: str
    size_bytes: int
    uncompressed_bytes: int
    file_count: int
    games: int
    providers: int
    roster_seats: int
    has_secrets: bool
    has_logs: bool
    provider_names: list[str] = field(default_factory=list)
    game_list: list[dict[str, Any]] = field(default_factory=list)
    games_listed: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive_id": self.archive_id,
            "format_version": self.format_version,
            "app_version": self.app_version,
            "created_at": self.created_at,
            "size_bytes": self.size_bytes,
            "uncompressed_bytes": self.uncompressed_bytes,
            "file_count": self.file_count,
            "games": self.games,
            "providers": self.providers,
            "roster_seats": self.roster_seats,
            "has_secrets": self.has_secrets,
            "has_logs": self.has_logs,
            "provider_names": list(self.provider_names),
            "game_list": [dict(item) for item in self.game_list],
            "games_listed": self.games_listed,
            "warnings": list(self.warnings),
        }


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


# ------------------------------------------------------------------------------------- opening an archive


def _declared_entries(handle: BinaryIO, file_size: int) -> tuple[int, int]:
    """``(entry count, central directory size)`` as the end-of-central-directory record claims, read *before* the zip
    module gets to allocate an object per entry (a hostile file can declare tens of millions)."""
    tail_len = min(file_size, 22 + 65535)
    handle.seek(file_size - tail_len)
    tail = handle.read(tail_len)
    pos = tail.rfind(b"PK\x05\x06")
    if pos < 0 or pos + 22 > len(tail):
        raise ArchiveFormatError("这不是有效的存档文件（不是 zip，或者文件不完整）。")
    _sig, _disk, _cd_disk, _entries_here, entries, cd_size, cd_offset, _comment = struct.unpack("<4sHHHHIIH", tail[pos : pos + 22])
    if entries == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        locator = pos - 20
        if locator < 0 or tail[locator : locator + 4] != b"PK\x06\x07":
            raise ArchiveFormatError("这不是有效的存档文件（zip64 记录不完整）。")
        _sig, _disk, record_offset, _disks = struct.unpack("<4sIQI", tail[locator : locator + 20])
        if record_offset + 56 > file_size:
            raise ArchiveFormatError("这不是有效的存档文件（zip64 记录越界）。")
        handle.seek(record_offset)
        record = handle.read(56)
        if len(record) < 56 or record[:4] != b"PK\x06\x06":
            raise ArchiveFormatError("这不是有效的存档文件（zip64 记录损坏）。")
        fields = struct.unpack("<4sQHHIIQQQQ", record)
        entries, cd_size = fields[7], fields[8]
    return int(entries), int(cd_size)


class _Opened:
    """An archive whose structure, member names and manifest have been checked (no member has been read yet)."""

    def __init__(self, path: Path, limits: ArchiveLimits) -> None:
        self.path = Path(path)
        self.limits = limits
        self.zf: zipfile.ZipFile | None = None
        self.manifest: Manifest
        self.infos: dict[str, zipfile.ZipInfo] = {}
        self.size_bytes = 0
        self.uncompressed_bytes = 0

    def __enter__(self) -> "_Opened":
        try:
            self._open()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self.zf is not None:
            try:
                self.zf.close()
            except Exception:  # noqa: BLE001
                pass
            self.zf = None

    # ------------------------------------------------------------------------------------------------
    def _open(self) -> None:
        limits = self.limits
        try:
            info = self.path.lstat()
        except OSError:
            raise ArchiveFormatError("找不到要导入的存档文件。") from None
        if not stat.S_ISREG(info.st_mode):
            raise ArchiveFormatError("要导入的存档必须是普通文件。")
        self.size_bytes = info.st_size
        if self.size_bytes < 22:
            raise ArchiveFormatError("这不是有效的存档文件（文件太小）。")
        try:
            with open(self.path, "rb") as handle:
                entries, cd_size = _declared_entries(handle, self.size_bytes)
        except OSError:
            raise ArchiveIOError("读取存档文件失败。") from None
        if entries > limits.max_entries or cd_size > limits.max_entries * 400:
            raise ArchiveLimitError(f"存档里的文件条目太多（声明了 {entries} 条，最多 {limits.max_entries} 条）。")
        try:
            self.zf = zipfile.ZipFile(self.path, "r")
        except (zipfile.BadZipFile, ValueError, struct.error, OverflowError, UnicodeError):
            raise ArchiveFormatError("这不是有效的存档文件（不是 zip，或者文件已损坏）。") from None
        except OSError:
            raise ArchiveIOError("读取存档文件失败。") from None
        self._scan()
        self._read_manifest()

    def _scan(self) -> None:
        assert self.zf is not None
        limits = self.limits
        infos = self.zf.infolist()
        if len(infos) > limits.max_entries:
            raise ArchiveLimitError(f"存档里的文件条目太多（{len(infos)} 条，最多 {limits.max_entries} 条）。")
        seen_lower: set[str] = set()
        total = 0
        packed = 0
        for info in infos:
            name = info.filename
            classify(name)
            if name in self.infos or name.lower() in seen_lower:
                raise ArchiveFormatError(f"存档里有重复的文件：{safe_text(name)}")
            seen_lower.add(name.lower())
            if info.flag_bits & 0x1 or info.flag_bits & 0x40:
                raise ArchiveFormatError(f"存档里的文件被 zip 密码加密了：{safe_text(name)}")
            if info.compress_type not in _ALLOWED_COMPRESSION:
                raise ArchiveFormatError(f"存档里的文件用了不支持的压缩方式：{safe_text(name)}")
            mode = info.external_attr >> 16
            if (info.create_system == 3 and mode and not stat.S_ISREG(mode)) or info.external_attr & 0x10:
                raise ArchiveFormatError(f"存档里有符号链接或特殊文件：{safe_text(name)}")
            if info.file_size > limits.max_file_bytes:
                raise ArchiveLimitError(f"存档里的文件太大：{safe_text(name)}")
            if info.file_size > limits.ratio_floor_bytes and info.file_size > max(info.compress_size, 1) * limits.max_ratio:
                raise ArchiveLimitError(f"存档里的文件压缩比异常（疑似压缩炸弹）：{safe_text(name)}")
            total += info.file_size
            packed += info.compress_size
            if total > limits.max_total_bytes:
                raise ArchiveLimitError(f"存档解压后太大（超过 {human_bytes(limits.max_total_bytes)}）。")
            self.infos[name] = info
        if packed > self.size_bytes:
            raise ArchiveFormatError("存档里的文件互相重叠，文件已损坏。")
        self.uncompressed_bytes = total

    def _read_manifest(self) -> None:
        assert self.zf is not None
        info = self.infos.get(MANIFEST_NAME)
        if info is None:
            raise ArchiveFormatError("存档里没有 manifest.json。")
        if info.file_size > self.limits.max_manifest_bytes:
            raise ArchiveLimitError("manifest.json 太大。")
        raw = _read_all(self.zf, info, self.limits.max_manifest_bytes)
        self.manifest = parse_manifest(raw)
        listed = {entry.path for entry in self.manifest.files}
        actual = set(self.infos) - {MANIFEST_NAME}
        if listed - actual:
            raise ArchiveFormatError(f"存档缺少清单里记载的文件：{safe_text(sorted(listed - actual)[0])}")
        if actual - listed:
            raise ArchiveFormatError(f"存档里多出清单没有记载的文件：{safe_text(sorted(actual - listed)[0])}")
        for entry in self.manifest.files:
            if self.infos[entry.path].file_size != entry.size:
                raise ArchiveFormatError(f"{safe_text(entry.path)} 的大小与清单不符。")
        if not self.manifest.files:
            raise ArchiveFormatError("存档里没有任何数据。")
        self.uncompressed_bytes = sum(entry.size for entry in self.manifest.files)


def _read_all(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    try:
        with zf.open(info, "r") as stream:
            while True:
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise ArchiveLimitError(f"{safe_text(info.filename)} 太大。")
                chunks.append(chunk)
    except _DECOMPRESS_ERRORS:
        raise ArchiveFormatError(f"{safe_text(info.filename)} 已损坏（压缩数据或 CRC 校验失败）。") from None
    except OSError:
        raise ArchiveIOError("读取存档文件失败。") from None
    return b"".join(chunks)


def _stream_member(
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    entry: FileEntry,
    *,
    keep: bool,
    sink: BinaryIO | None,
    on_bytes: Callable[[int], None],
) -> bytes | None:
    """Read one member, checking its size and SHA-256 against the manifest as the bytes go by; optionally keep the
    bytes (for content checks) and/or write them to ``sink``.  Never reads more than the manifest says."""
    digest = hashlib.sha256()
    size = 0
    chunks: list[bytes] = []
    try:
        with zf.open(info, "r") as stream:
            while True:
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > entry.size:
                    raise ArchiveFormatError(f"{safe_text(entry.path)} 比清单记载的大：存档已损坏或被改动。")
                digest.update(chunk)
                if sink is not None:
                    sink.write(chunk)
                if keep:
                    chunks.append(chunk)
                on_bytes(len(chunk))
    except ArchiveError:
        raise
    except _DECOMPRESS_ERRORS:
        raise ArchiveFormatError(f"{safe_text(entry.path)} 已损坏（压缩数据或 CRC 校验失败）。") from None
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise ArchiveIOError("磁盘空间不足，解压已中止。") from None
        raise ArchiveIOError("读取存档或写入临时文件失败。") from None
    if size != entry.size:
        raise ArchiveFormatError(f"{safe_text(entry.path)} 的大小与清单不符：存档已损坏或被改动。")
    if digest.hexdigest() != entry.sha256:
        raise ArchiveFormatError(f"{safe_text(entry.path)} 的 SHA-256 校验值与清单不符：存档已损坏或被改动。")
    return b"".join(chunks) if keep else None


def _walk(
    opened: _Opened,
    *,
    stage: Path | None,
    progress: ProgressFn,
    skip_secrets: bool = False,
) -> Inventory:
    """Verify (and, with ``stage``, unpack) every member; check each member's content; return what was found."""
    assert opened.zf is not None
    limits = opened.limits
    entries = [entry for entry in opened.manifest.files if not (skip_secrets and entry.path == SECRETS_NAME)]
    total = max(sum(entry.size for entry in entries), 1)
    done = 0
    inventory = Inventory()

    def on_bytes(count: int) -> None:
        nonlocal done
        done += count
        progress(PHASE_VERIFY, min(done, total), total)

    progress(PHASE_VERIFY, 0, total)
    for entry in entries:
        kind, game_id = classify(entry.path)
        info = opened.infos[entry.path]
        keep = kind in JSON_KINDS or kind in (KIND_ROSTER_TEXT, KIND_SECRETS)
        if keep:
            cap = limits.max_secrets_bytes if kind == KIND_SECRETS else limits.max_json_bytes
            if entry.size > cap:
                raise ArchiveLimitError(f"{safe_text(entry.path)} 太大，无法检查。")
        if stage is not None and kind != KIND_SECRETS:
            target = stage.joinpath(*entry.path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                target.parent.chmod(0o700)
            with open(target, "xb") as sink:
                data = _stream_member(opened.zf, info, entry, keep=keep, sink=sink, on_bytes=on_bytes)
            if os.name != "nt":
                target.chmod(0o600)
            _restore_mtime(target, info)
        else:
            data = _stream_member(opened.zf, info, entry, keep=keep, sink=None, on_bytes=on_bytes)
        check_member(kind, game_id, entry.path, data if keep else None, inventory)
    progress(PHASE_VERIFY, total, total)
    return inventory


def _restore_mtime(target: Path, info: zipfile.ZipInfo) -> None:
    """Give an unpacked file the modification time it had when it was exported (the zip stores it, to the second, as
    local time): the history list orders and shows games by it.  Best effort."""
    try:
        stamp = time.mktime((*info.date_time, 0, 0, -1))
        os.utime(target, (stamp, stamp))
    except (OverflowError, ValueError, OSError):
        pass


def _crosscheck(manifest: Manifest, inventory: Inventory) -> None:
    contents = manifest.contents
    if (
        contents.games != len(inventory.games)
        or contents.providers != len(inventory.provider_ids)
        or contents.roster_seats != len(inventory.seat_ids)
    ):
        raise ArchiveFormatError("清单的内容统计（对局数、Provider 数、名册座位数）与实际内容不符。")
    for game_id in inventory.prev_games - set(inventory.games):
        raise ArchiveFormatError(f"对局 {safe_text(game_id)} 只有备份副本，没有 game.json。")


def _summary(opened: _Opened, inventory: Inventory) -> ArchiveSummary:
    manifest = opened.manifest
    games = sorted(inventory.games.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return ArchiveSummary(
        archive_id=manifest.archive_id,
        format_version=manifest.format_version,
        app_version=manifest.app_version,
        created_at=manifest.created_at,
        size_bytes=opened.size_bytes,
        uncompressed_bytes=opened.uncompressed_bytes,
        file_count=len(manifest.files),
        games=len(inventory.games),
        providers=len(inventory.provider_ids),
        roster_seats=len(inventory.seat_ids),
        has_secrets=manifest.contents.has_secrets,
        has_logs=manifest.contents.has_logs,
        provider_names=list(inventory.provider_names),
        game_list=games[:_MAX_GAMES_LISTED],
        games_listed=min(len(games), _MAX_GAMES_LISTED),
    )


# ------------------------------------------------------------------------------------------------ inspect


def inspect(path: Path, *, limits: ArchiveLimits = DEFAULT_LIMITS, progress: ProgressFn = no_progress) -> ArchiveSummary:
    """Validate an archive completely (no password needed) and describe it; nothing is written anywhere."""
    with _Opened(path, limits) as opened:
        inventory = _walk(opened, stage=None, progress=progress)
        _crosscheck(opened.manifest, inventory)
        return _summary(opened, inventory)


def _member_bytes(opened: "_Opened", name: str, limit: int) -> bytes | None:
    """One member read into memory with its size and SHA-256 checked (``None`` when the archive has no such member)."""
    entry = next((item for item in opened.manifest.files if item.path == name), None)
    if entry is None:
        return None
    if entry.size > limit:
        raise ArchiveLimitError(f"{safe_text(name)} 太大，无法检查。")
    assert opened.zf is not None
    return _stream_member(opened.zf, opened.infos[name], entry, keep=True, sink=None, on_bytes=lambda _n: None)


def _open_bound_keys(opened: "_Opened", password: str) -> dict[str, str]:
    """Decrypt the keys of an opened archive and make sure they are still bound to the addresses they were made for:
    ``providers.json`` is not encrypted, so a changed ``base_url`` would send the owner's key somewhere else."""
    blob = _member_bytes(opened, SECRETS_NAME, opened.limits.max_secrets_bytes)
    if blob is None:
        raise ArchiveFormatError("这份存档不含 API Key。")
    sealed = open_secrets_bound(blob, password, opened.manifest.archive_id)
    raw = _member_bytes(opened, "providers.json", opened.limits.max_json_bytes)
    try:
        items = providers_of(json.loads(raw.decode("utf-8")), "providers.json") if raw is not None else []
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ArchiveFormatError("providers.json 不是有效的 JSON。") from None
    current = {str(item["id"]): item for item in items}
    for provider_id in sorted(sealed.keys):
        item = current.get(provider_id)
        # a key whose provider is not in the archive is harmless (it is dropped on import); one whose address changed is not
        if item is not None and sealed.endpoints.get(provider_id) != endpoint_of(item):
            raise ArchiveKeysMismatchError(
                f"存档里 Provider「{safe_text(item.get('name') or provider_id, 40)}」的协议或地址与加密 API Key 时不一致："
                "存档在导出之后被改动过。为了安全，不导入 API Key；可以选择「跳过 API Key」导入其余内容，再在 Provider 里重新填写 Key。"
            )
    return sealed.keys


def decrypt_archive_keys(path: Path, password: str, *, limits: ArchiveLimits = DEFAULT_LIMITS) -> dict[str, str]:
    """The API keys of an archive, decrypted with ``password`` (:class:`ArchivePasswordError` when it is wrong,
    :class:`ArchiveKeysMismatchError` when ``providers.json`` no longer names the addresses they were sealed for).

    Reads only the manifest, ``secrets.enc`` and ``providers.json`` (whose checksums are verified first), so it answers
    in about a second even for a huge archive: the UI asks for the password before it starts the import job."""
    with _Opened(path, limits) as opened:
        return _open_bound_keys(opened, password)


# ----------------------------------------------------------------------------------------------- prepare


@dataclass
class PreparedImport:
    """An archive that has been unpacked and verified into a staging directory, ready to be swapped in."""

    summary: ArchiveSummary
    stage_dir: Path
    work_dir: Path
    keys: dict[str, str] | None = field(default=None, repr=False)  # decrypted API keys (None: none to import)
    provider_ids: list[str] = field(default_factory=list)
    provider_names: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _fix_last_session(stage: Path, game_ids: set[str], warnings: list[str]) -> None:
    """``settings.json`` must not point at a game the archive does not hold: the application would start in its
    "could not restore the last game" protection mode (no autosave)."""
    path = stage / "settings.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    last = data.get("last_session_id") if isinstance(data, dict) else None
    if not last or last in game_ids:
        return
    data["last_session_id"] = None
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    warnings.append("存档里没有「上次对局」所指的对局，已清除这条记录（导入后会从一局新的空白对局开始）。")


def _check_settings(stage: Path) -> None:
    """The real settings reader must accept the unpacked ``settings.json`` (it is stricter than a JSON check)."""
    if not (stage / "settings.json").is_file():
        return
    from app.storage.errors import StorageError
    from app.storage.settings import SettingsStore

    try:
        SettingsStore(stage).get_settings()
    except StorageError:
        raise ArchiveFormatError("settings.json 没有通过校验：它的内容不是本程序能读取的设置。") from None
    except (ValueError, OSError):
        raise ArchiveFormatError("settings.json 没有通过校验。") from None


def prepare(
    path: Path,
    data_dir: Path,
    *,
    password: str | None = None,
    skip_keys: bool = False,
    keys: dict[str, str] | None = None,
    limits: ArchiveLimits = DEFAULT_LIMITS,
    progress: ProgressFn = no_progress,
    work_dir: Path | None = None,
) -> PreparedImport:
    """Unpack and verify an archive into ``<data>/tmp/import-<id>/stage``; the data directory itself is not changed.

    ``keys`` are already decrypted keys (the API checks the password before it starts the job); otherwise ``password``
    decrypts them.  With ``skip_keys`` (or an archive without keys) none are imported.
    """
    data_dir = Path(data_dir).resolve()
    work = Path(work_dir) if work_dir is not None else data_dir / "tmp" / f"import-{uuid.uuid4().hex[:12]}"
    stage = work / "stage"
    try:
        with _Opened(path, limits) as opened:
            manifest = opened.manifest
            import_keys: dict[str, str] | None = None
            if manifest.contents.has_secrets and not skip_keys:
                if keys is not None:
                    import_keys = dict(keys)
                elif password is not None:
                    import_keys = _open_bound_keys(opened, password)
                else:
                    raise ArchivePasswordError("这份存档含有 API Key：请输入密码，或者选择跳过 API Key。")
            work.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(work).free
            need = opened.uncompressed_bytes + limits.min_free_margin_bytes
            if free < need:
                raise ArchiveIOError(f"磁盘空间不足：解压需要约 {human_bytes(opened.uncompressed_bytes)}，可用 {human_bytes(free)}。")
            stage.mkdir(parents=True, exist_ok=True)
            inventory = _walk(opened, stage=stage, progress=progress, skip_secrets=True)
            _crosscheck(manifest, inventory)
            progress(PHASE_VALIDATE, 0, 1)
            warnings: list[str] = []
            _fix_last_session(stage, set(inventory.games), warnings)
            _check_settings(stage)
            progress(PHASE_VALIDATE, 1, 1)
            summary = _summary(opened, inventory)
            if import_keys is not None:
                import_keys = {pid: key for pid, key in import_keys.items() if pid in set(inventory.provider_ids)}
            return PreparedImport(
                summary=summary,
                stage_dir=stage,
                work_dir=work,
                keys=import_keys,
                provider_ids=list(inventory.provider_ids),
                provider_names=dict(zip(inventory.provider_ids, inventory.provider_names)),
                warnings=warnings,
            )
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


# ------------------------------------------------------------------------------------------------ commit


def _move(source: Path, target: Path) -> None:
    """Rename ``source`` to ``target`` (a file or a whole directory); retried briefly because on Windows an indexer or
    a virus scanner may hold a folder for a moment."""
    last: OSError | None = None
    for attempt in range(4):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(0.15 * (attempt + 1))
        except OSError as exc:
            if exc.errno == errno.EXDEV:  # the data directory spans file systems: fall back to copy + delete
                shutil.move(str(source), str(target))
                return
            last = exc
            break
    logger.warning("Could not move %s (%s)", safe_text(source.name), type(last).__name__)
    raise ArchiveIOError(f"无法移动数据目录里的「{safe_text(source.name)}」（可能被其他程序占用）。")


def _listing(path: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(path) as handle:
            return sorted(handle, key=lambda entry: entry.name)
    except OSError:
        return []


def _fresh_dir(parent: Path, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%d-%H%M%S")
    for index in range(100):
        candidate = parent / (stamp if index == 0 else f"{stamp}-{index}")
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise ArchiveIOError("备份目录里同一时刻的文件夹太多。")


def prune_backups(data_dir: Path, keep: int = KEEP_BACKUPS) -> list[str]:
    """Delete the oldest ``backups/<time>/`` folders so that ``keep`` remain; returns the names removed."""
    backups = Path(data_dir) / "backups"
    names = sorted(entry.name for entry in _listing(backups) if entry.is_dir(follow_symlinks=False) and _BACKUP_NAME.match(entry.name))
    removed: list[str] = []
    for name in names[: max(0, len(names) - keep)]:
        try:
            shutil.rmtree(backups / name)
            removed.append(name)
        except OSError:
            logger.warning("Could not delete the old backup %s", name)
    return removed


@dataclass
class ImportResult:
    backup: str | None  # name of the folder under ``backups/`` that holds the previous data (None: there was none)
    games: int
    providers: int
    keys_imported: int
    keys_kept: int  # local keys kept for providers with the same id (the archive's keys were skipped / missing)
    providers_without_key: list[str]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backup": self.backup,
            "games": self.games,
            "providers": self.providers,
            "keys_imported": self.keys_imported,
            "keys_kept": self.keys_kept,
            "providers_without_key": list(self.providers_without_key),
            "warnings": list(self.warnings),
        }


class ImportTransaction:
    """Swap the prepared data into the data directory; undo it completely on any failure."""

    def __init__(
        self,
        data_dir: Path,
        prepared: PreparedImport,
        *,
        secrets: SecretsPort | None = None,
        now: datetime | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.prepared = prepared
        self._secrets = secrets
        self._now = now or datetime.now()
        self.backup_dir: Path | None = None
        self.changed = False  # the data directory has been touched (a rollback has something to undo)
        self._moved: list[str] = []  # names moved from the data directory into the backup
        self._placed: list[str] = []  # names moved from the stage into the data directory
        self._keys_written = False
        self.keys_imported = 0
        self.keys_kept = 0
        self.providers_without_key: list[str] = []

    @property
    def secrets(self) -> SecretsPort:
        if self._secrets is None:
            self._secrets = default_secrets(self.data_dir)
        return self._secrets

    # ------------------------------------------------------------------------------------------------
    def _old_keys(self) -> dict[str, str]:
        """The keys of this machine, read *before* anything moves (F32: they stay for providers with the same id)."""
        try:
            return {pid: key for pid, key in self.secrets.read_all().items() if isinstance(key, str) and key}
        except Exception as exc:  # noqa: BLE001 - unreadable keys (no master key, damaged file) must not block an import
            logger.warning("The existing API keys could not be read (%s); none will be kept", type(exc).__name__)
            return {}

    def swap(self, progress: ProgressFn = no_progress) -> None:
        """Back the current data up, move the new data in, write the keys.  Raises on failure (call :meth:`rollback`)."""
        prepared = self.prepared
        provider_ids = set(prepared.provider_ids)
        keep_local = {pid: key for pid, key in self._old_keys().items() if pid in provider_ids}
        final = dict(keep_local)
        imported = {pid: key for pid, key in (prepared.keys or {}).items() if pid in provider_ids}
        final.update(imported)

        progress(PHASE_BACKUP, 0, 1)
        current = [entry for entry in _listing(self.data_dir) if entry.name not in KEEP_IN_PLACE]
        self.changed = True
        if current:
            self.backup_dir = _fresh_dir(self.data_dir / "backups", self._now)
            for entry in current:
                _move(Path(entry.path), self.backup_dir / entry.name)
                self._moved.append(entry.name)
            wrapper = self.data_dir / "master.v1.dpapi"
            if wrapper.is_file():
                shutil.copy2(wrapper, self.backup_dir / "master.v1.dpapi")
        progress(PHASE_BACKUP, 1, 1)

        progress(PHASE_REPLACE, 0, 1)
        staged = _listing(prepared.stage_dir)
        for index, entry in enumerate(staged, start=1):
            _move(Path(entry.path), self.data_dir / entry.name)
            self._placed.append(entry.name)
            progress(PHASE_REPLACE, index, max(len(staged), 1))
        progress(PHASE_REPLACE, max(len(staged), 1), max(len(staged), 1))

        progress(PHASE_KEYS, 0, 1)
        if final:
            self._keys_written = True  # even a half-finished write must be undone
            self.secrets.write_all(final)
        self.keys_imported = len(imported)
        self.keys_kept = len([pid for pid in keep_local if pid not in imported])
        self.providers_without_key = [prepared.provider_names.get(pid, pid) for pid in prepared.provider_ids if pid not in final]
        progress(PHASE_KEYS, 1, 1)

    def rollback(self) -> None:
        """Put the previous data back exactly as it was (best effort per entry; problems are logged, not raised)."""
        trash = self.prepared.work_dir / "rolled-back"
        try:
            trash.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        evict = list(self._placed) + (["secrets.v1.enc"] if self._keys_written else [])
        for name in reversed(evict):
            target = self.data_dir / name
            if not (target.exists() or target.is_symlink()):
                continue
            try:
                _move(target, trash / name)
            except ArchiveError:
                try:
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                except OSError:
                    logger.error("Rollback could not remove %s", safe_text(name))
        for name in reversed(self._moved):
            assert self.backup_dir is not None
            try:
                _move(self.backup_dir / name, self.data_dir / name)
            except ArchiveError:
                logger.error("Rollback could not restore %s; the previous data is in %s", safe_text(name), safe_text(self.backup_dir.name))
        if self.backup_dir is not None:
            try:
                (self.backup_dir / "master.v1.dpapi").unlink(missing_ok=True)
                self.backup_dir.rmdir()  # only succeeds when everything was restored
            except OSError:
                pass
        self._moved.clear()
        self._placed.clear()
        self._keys_written = False

    def finish(self) -> None:
        shutil.rmtree(self.prepared.work_dir, ignore_errors=True)
        prune_backups(self.data_dir)


def commit(
    prepared: PreparedImport,
    data_dir: Path,
    *,
    secrets: SecretsPort | None = None,
    reload: Callable[[], Any] | None = None,
    ensure_idle: Callable[[], None] | None = None,
    progress: ProgressFn = no_progress,
    now: datetime | None = None,
    clear_caches: Callable[[], None] | None = None,
) -> ImportResult:
    """Swap ``prepared`` into ``data_dir`` (backup first) and reload the application; any failure restores the old data
    and reloads that.  ``reload`` is called after the swap (it must raise when the new data cannot be used)."""
    if clear_caches is None:
        from app.storage import clear_store_caches as clear_caches
    transaction = ImportTransaction(data_dir, prepared, secrets=secrets, now=now)
    try:
        if ensure_idle is not None:
            ensure_idle()
        transaction.swap(progress)
        clear_caches()
        progress(PHASE_RELOAD, 0, 1)
        if reload is not None:
            reload()
        progress(PHASE_RELOAD, 1, 1)
    except BaseException:
        transaction.rollback()
        if transaction.changed:  # nothing to bring back when the failure came before the first move
            try:
                clear_caches()
                if reload is not None:
                    reload()  # bring the in-memory state back to the restored data
            except Exception:  # noqa: BLE001 - the original error is what the user needs to see
                logger.exception("Reloading the restored data after a failed import failed")
        shutil.rmtree(prepared.work_dir, ignore_errors=True)
        raise
    progress(PHASE_FINALIZE, 0, 1)
    transaction.finish()
    progress(PHASE_FINALIZE, 1, 1)
    return ImportResult(
        backup=transaction.backup_dir.name if transaction.backup_dir is not None else None,
        games=prepared.summary.games,
        providers=prepared.summary.providers,
        keys_imported=transaction.keys_imported,
        keys_kept=transaction.keys_kept,
        providers_without_key=transaction.providers_without_key,
        warnings=list(prepared.warnings),
    )


def apply(
    path: Path,
    password: str | None = None,
    skip_keys: bool = False,
    *,
    data_dir: Path | None = None,
    secrets: SecretsPort | None = None,
    reload: Callable[[], Any] | None = None,
    ensure_idle: Callable[[], None] | None = None,
    keys: dict[str, str] | None = None,
    limits: ArchiveLimits = DEFAULT_LIMITS,
    progress: ProgressFn = no_progress,
    now: datetime | None = None,
    clear_caches: Callable[[], None] | None = None,
) -> ImportResult:
    """``prepare`` + ``commit``: replace the whole data directory with the archive's content.  The previous data is moved
    to ``backups/<time>/`` first and put back if anything fails."""
    if data_dir is None:
        from app.storage.paths import get_data_dir

        data_dir = get_data_dir()
    prepared = prepare(path, data_dir, password=password, skip_keys=skip_keys, keys=keys, limits=limits, progress=progress)
    return commit(
        prepared,
        data_dir,
        secrets=secrets,
        reload=reload,
        ensure_idle=ensure_idle,
        progress=progress,
        now=now,
        clear_caches=clear_caches,
    )


__all__ = [
    "IMPORT_PHASES",
    "KEEP_BACKUPS",
    "KEEP_IN_PLACE",
    "ArchiveSummary",
    "ImportResult",
    "ImportTransaction",
    "PreparedImport",
    "apply",
    "commit",
    "decrypt_archive_keys",
    "human_bytes",
    "inspect",
    "prepare",
    "prune_backups",
]
