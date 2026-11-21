"""Reading a zip file safely: the checks every Arena-Agents container shares (``.arenaagents``, ``.arenabot``, ``.arenagame``).

A container is a zip whose members are exactly the files listed in its ``manifest.json`` (plus the manifest itself).  This module knows
nothing about *what* a particular container may hold - the member-name allow-list and the manifest model are handed in by the format
(:func:`app.archive.format.classify` / :func:`~app.archive.format.parse_manifest` for the total archive, the ones of
:mod:`app.archive.arenabot` / :mod:`app.archive.arenagame` for the small ones).  What it does is the same for all of them:

* the declared number of entries is read from the end-of-central-directory record *before* the zip module allocates an object per entry
  (a hostile file can declare tens of millions);
* every member name goes through the format's allow-list; duplicates (also by case) are refused; no password-protected members, no
  compression but stored / deflate, no symbolic links or special files;
* sizes are checked against :class:`~app.archive.format.ArchiveLimits` - per file, in total, the compression ratio (zip bombs) and members
  that overlap - both against what the headers *declare* and, while the data is read, against what actually comes out;
* a member is read with its size and SHA-256 checked against the manifest as the bytes go by, and never reads more than the manifest says.

Everything here is blocking: run it in a worker thread.  No file content is ever put into an exception message.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import struct
import time
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from app.archive.errors import ArchiveError, ArchiveFormatError, ArchiveIOError, ArchiveLimitError, safe_text
from app.archive.format import CHUNK_BYTES, ArchiveLimits, FileEntry

MANIFEST_NAME = "manifest.json"
ALLOWED_COMPRESSION = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
DECOMPRESS_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError)


#: What each container is called to the user, by the ``format`` its manifest declares (for "this is not the kind of file I expected").
FORMAT_LABELS = {
    "arenaagents": "总存档（.arenaagents）",
    "arenabot": "bot 文件（.arenabot）",
    "arenagame": "对局文件（.arenagame）",
}
_SNIFF_MANIFEST_BYTES = 1024 * 1024


def sniff_format(path: Path) -> str | None:
    """The ``format`` the manifest of a zip declares (``"arenaagents"``, ``"arenabot"``, ``"arenagame"`` ...), or ``None`` when that cannot be
    told: not a zip, no manifest, one that is too big or not JSON.  It reads at most 1 MiB and believes nothing else about the file - it only
    lets the importers say "this is a 对局文件, not a bot 文件" instead of "unknown file: games/x/game.json"."""
    try:
        size = Path(path).stat().st_size
        if size < 22:
            return None
        with open(path, "rb") as handle:
            entries, _cd = declared_entries(handle, size)
        if entries > 50_000:
            return None
        with zipfile.ZipFile(path) as zf:
            info = zf.getinfo(MANIFEST_NAME)
            if info.file_size > _SNIFF_MANIFEST_BYTES:
                return None
            data = json.loads(zf.read(info).decode("utf-8"))
    except (OSError, ArchiveError, KeyError, ValueError, RecursionError, UnicodeError, struct.error, OverflowError, *DECOMPRESS_ERRORS):
        return None
    declared = data.get("format") if isinstance(data, dict) else None
    return declared if isinstance(declared, str) and len(declared) <= 40 else None


def wrong_kind_message(found: str | None, *, expected: str) -> str | None:
    """The sentence to say when a file of format ``found`` is handed to the importer of ``expected`` (``None``: nothing to say - it is the
    right kind, or it cannot be told)."""
    if found is None or found == expected:
        return None
    wanted = _spaced(FORMAT_LABELS.get(expected, expected))
    if found in FORMAT_LABELS:
        return f"这是{_spaced(FORMAT_LABELS[found])}，不是{wanted}。请选择正确的文件。"
    return f"这不是{wanted}（它的格式名是 {safe_text(found, 40)}）。"


def _spaced(label: str) -> str:
    """``label`` with a space in front when it begins with a Latin letter (``不是 bot 文件``, but ``不是对局文件``)."""
    return " " + label if label[:1].isascii() else label


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def declared_entries(handle: BinaryIO, file_size: int) -> tuple[int, int]:
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


class OpenedZip:
    """A zip whose structure, member names and manifest have been checked (no member has been read yet).

    ``classify(name)`` raises :class:`ArchiveFormatError` for a member name the format does not allow (it is called for every member,
    the manifest included); ``parse_manifest(raw)`` returns the manifest model - anything with ``.files`` (a list of
    :class:`~app.archive.format.FileEntry`) - or raises.  After ``with OpenedZip(...) as opened`` the attributes ``manifest``, ``infos``
    (member name -> ``ZipInfo``), ``size_bytes`` (of the zip) and ``uncompressed_bytes`` (what the manifest lists) are available."""

    def __init__(
        self,
        path: Path,
        limits: ArchiveLimits,
        *,
        classify: Callable[[str], Any],
        parse_manifest: Callable[[bytes], Any],
    ) -> None:
        self.path = Path(path)
        self.limits = limits
        self._classify = classify
        self._parse_manifest = parse_manifest
        self.zf: zipfile.ZipFile | None = None
        self.manifest: Any = None
        self.infos: dict[str, zipfile.ZipInfo] = {}
        self.size_bytes = 0
        self.uncompressed_bytes = 0

    def __enter__(self) -> "OpenedZip":
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
                entries, cd_size = declared_entries(handle, self.size_bytes)
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
            self._classify(name)
            if name in self.infos or name.lower() in seen_lower:
                raise ArchiveFormatError(f"存档里有重复的文件：{safe_text(name)}")
            seen_lower.add(name.lower())
            if info.flag_bits & 0x1 or info.flag_bits & 0x40:
                raise ArchiveFormatError(f"存档里的文件被 zip 密码加密了：{safe_text(name)}")
            if info.compress_type not in ALLOWED_COMPRESSION:
                raise ArchiveFormatError(f"存档里的文件用了不支持的压缩方式：{safe_text(name)}")
            mode = info.external_attr >> 16
            # Unix ``ZipFile.writestr(name, ...)`` stores permission bits without an explicit S_IFREG.  No file-type
            # bits is an ordinary file; reject only an explicit non-regular type (and the DOS directory flag).
            file_type = stat.S_IFMT(mode) if info.create_system == 3 else 0
            if file_type not in (0, stat.S_IFREG) or info.external_attr & 0x10:
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
        raw = read_all(self.zf, info, self.limits.max_manifest_bytes)
        self.manifest = self._parse_manifest(raw)
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


def read_all(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    """One member read into memory, at most ``limit`` bytes (:class:`ArchiveLimitError` beyond)."""
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
    except ArchiveError:
        raise
    except DECOMPRESS_ERRORS:
        raise ArchiveFormatError(f"{safe_text(info.filename)} 已损坏（压缩数据或 CRC 校验失败）。") from None
    except OSError:
        raise ArchiveIOError("读取存档文件失败。") from None
    return b"".join(chunks)


def stream_member(
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
    except DECOMPRESS_ERRORS:
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


def restore_mtime(target: Path, info: zipfile.ZipInfo) -> None:
    """Give an unpacked file the modification time it had when it was exported (the zip stores it, to the second, as
    local time): the history list orders and shows games by it.  Best effort."""
    try:
        stamp = time.mktime((*info.date_time, 0, 0, -1))
        os.utime(target, (stamp, stamp))
    except (OverflowError, ValueError, OSError):
        pass


def zip_info(name: str, mtime: float) -> zipfile.ZipInfo:
    """A regular, deflated, 0644 member (Unix attributes, so an extractor sees "regular file")."""
    stamp = time.localtime(max(mtime, 315532800))  # the zip format cannot hold years before 1980
    info = zipfile.ZipInfo(name, date_time=stamp[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


__all__ = [
    "ALLOWED_COMPRESSION",
    "DECOMPRESS_ERRORS",
    "FORMAT_LABELS",
    "MANIFEST_NAME",
    "OpenedZip",
    "declared_entries",
    "human_bytes",
    "read_all",
    "restore_mtime",
    "sniff_format",
    "stream_member",
    "wrong_kind_message",
    "zip_info",
]
