"""``.arenabot``: one bot as a file (docs/v0.14-plan.md H30, H44, H45; contract section 13.9).

A ``.arenabot`` is a zip file whose members are ``manifest.json`` and the files of one bot directory under ``bot/``::

    manifest.json     {"format": "arenabot", "format_version": 1, "created_at", "app_version",
                       "bot": {"name", "origin"}, "files": [{"path": "bot/core/identity.md", "size", "sha256"}, ...]}
    bot/bot.json  bot/core/identity.md  voice.md  mind.md  extra-<slug>.md  bot/arch/notes.md
    bot/rules/<scenario>/digest.md  playbook.md  questions.md  bot/people.md  bot/record.json  bot/staging/<proposal>.json

It is exactly the whitelist of :func:`app.storage.bots.iter_bot_files` (:data:`app.archive.format.BOT_FILE_RE`): nothing else is
packed, and nothing else is unpacked, however harmless it looks.  The container is read the way every container of this package is
(:mod:`app.archive.zipcheck`): member names are safe relative paths, every file is checked against the manifest's size and SHA-256 as it
is unpacked, a zip bomb cannot unpack to more than the manifest says, and nothing is written outside ``<workdir>/bot``.

* :func:`pack_arenabot` writes the file (atomically: ``.part`` then a rename);
* :func:`unpack_arenabot` checks it and unpacks the ``bot/`` directory into ``workdir``; what the bot *means* (its name, the limits of its L0,
  the format of its memory files) is :meth:`app.storage.bots.BotStore.install_bot_dir`'s business, which also ignores the ``id`` in
  ``bot.json`` (the target bot keeps its own id).

Both are blocking: run them in a worker thread.  File content is never put into an exception message.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import zipfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.archive.errors import ArchiveError, ArchiveFormatError, ArchiveIOError, ArchiveLimitError, ArchiveVersionError, safe_text
from app.archive.format import (
    BOT_FILE_RE,
    ArchiveLimits,
    FileEntry,
    check_bot_relative,
    check_member_name,
)
from app.archive.validate import MAX_TEXT_BYTES, check_bot_file
from app.archive.zipcheck import (
    MANIFEST_NAME,
    OpenedZip,
    human_bytes,
    sniff_format,
    stream_member,
    wrong_kind_message,
    zip_info,
)

logger = logging.getLogger(__name__)

FORMAT_NAME = "arenabot"
FORMAT_VERSION = 1
FILE_SUFFIX = ".arenabot"
BOT_PREFIX = "bot/"
BOT_JSON = "bot.json"
#: A ``.arenabot`` file, and what it unpacks to, is at most this big (contract 13.9); one file of a bot at most :data:`MAX_FILE_BYTES`.
MAX_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = MAX_TEXT_BYTES
MAX_ENTRIES = 1000
LIMITS = ArchiveLimits(
    max_entries=MAX_ENTRIES + 1,  # the manifest, too
    max_file_bytes=MAX_FILE_BYTES,
    max_total_bytes=MAX_BYTES,
    max_manifest_bytes=2 * 1024 * 1024,
    max_json_bytes=MAX_FILE_BYTES,
    min_free_margin_bytes=16 * 1024 * 1024,
)
_NAME_CHARS = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# --------------------------------------------------------------------------------------------------------- manifest
class BotInfo(BaseModel):
    """What the manifest says of the bot: for people (a preview, a log line) - never a path, never an id."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=_NAME_CHARS)
    origin: str = Field(max_length=64)

    @field_validator("name", "origin")
    @classmethod
    def _printable(cls, value: str) -> str:
        if _CONTROL.search(value):
            raise ValueError("control characters")
        return value


class BotManifest(BaseModel):
    """``manifest.json`` of a ``.arenabot``."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["arenabot"]
    format_version: int = Field(ge=1)
    created_at: str
    app_version: str = Field(min_length=1, max_length=64)
    bot: BotInfo
    files: list[FileEntry]

    @field_validator("created_at")
    @classmethod
    def _created_at(cls, value: str) -> str:
        if datetime.fromisoformat(value).tzinfo is None:  # ValueError -> a validation error
            raise ValueError("created_at must carry a UTC offset")
        return value

    @field_validator("app_version")
    @classmethod
    def _app_version(cls, value: str) -> str:
        if _CONTROL.search(value):
            raise ValueError("app_version holds control characters")
        return value


def classify_bot_member(name: object) -> str:
    """The manifest's name for the manifest, else the file's path inside the bot's directory (``"core/identity.md"``); every other name
    is refused with its reason.  The one place that says which member names a ``.arenabot`` may have."""
    name = check_member_name(name)
    if name == MANIFEST_NAME:
        return name
    if name.startswith(BOT_PREFIX) and BOT_FILE_RE.fullmatch(name[len(BOT_PREFIX) :]):
        rel = name[len(BOT_PREFIX) :]
        check_bot_relative(rel, name)
        return rel
    raise ArchiveFormatError(f"这个 bot 文件里有不认识的文件：{safe_text(name)}")


def parse_bot_manifest(raw: bytes) -> BotManifest:
    """Parse and check ``manifest.json``: JSON, the format name, the version (a newer one is reported as such, before anything else is judged),
    the model, every listed path (all of them files of a bot, none twice - also not by case - and ``bot.json`` among them) and the total size."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ArchiveFormatError("manifest.json 不是有效的 JSON。") from None
    if not isinstance(data, dict):
        raise ArchiveFormatError("manifest.json 的内容不是一个对象。")
    found = data.get("format")
    if found != FORMAT_NAME:
        raise ArchiveFormatError(
            wrong_kind_message(found if isinstance(found, str) else None, expected=FORMAT_NAME)
            or "这不是 bot 文件（manifest.json 里没有格式名 arenabot）。"
        )
    version = data.get("format_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ArchiveFormatError("这个 bot 文件的格式版本无效。")
    if version > FORMAT_VERSION:
        raise ArchiveVersionError(
            f"这个 bot 文件使用更新的格式（v{version}），当前程序只支持到 v{FORMAT_VERSION}。请先把 Arena-Agents 升级到较新的版本再导入。"
        )
    try:
        manifest = BotManifest.model_validate(data)
    except ValidationError as exc:
        where = ", ".join(sorted({".".join(str(p) for p in err["loc"]) or "manifest" for err in exc.errors()[:5]}))
        raise ArchiveFormatError(f"manifest.json 的内容不符合格式（{safe_text(where, 120)}）。") from None
    seen: set[str] = set()
    seen_lower: set[str] = set()
    for entry in manifest.files:
        rel = classify_bot_member(entry.path)
        if rel == MANIFEST_NAME:
            raise ArchiveFormatError("清单不应该列出 manifest.json 自己。")
        if entry.path in seen:
            raise ArchiveFormatError(f"清单里的文件重复：{safe_text(entry.path)}")
        if entry.path.lower() in seen_lower:
            raise ArchiveFormatError(f"清单里有只差大小写的路径：{safe_text(entry.path)}")
        seen.add(entry.path)
        seen_lower.add(entry.path.lower())
        if entry.size > MAX_FILE_BYTES:
            raise ArchiveLimitError(f"{safe_text(entry.path)} 太大了（单个文件最多 {human_bytes(MAX_FILE_BYTES)}）。")
    if len(manifest.files) > MAX_ENTRIES:
        raise ArchiveLimitError("这个 bot 文件里的文件太多了。")
    if BOT_PREFIX + BOT_JSON not in seen:
        raise ArchiveFormatError("这个 bot 文件里没有 bot.json。")
    if sum(entry.size for entry in manifest.files) > MAX_BYTES:
        raise ArchiveLimitError(f"这个 bot 文件解压后超过了 {human_bytes(MAX_BYTES)} 的上限。")
    return manifest


# ------------------------------------------------------------------------------------------------------------- pack
def _fsync(path: Path) -> None:
    try:
        with open(path, "rb+") as handle:
            os.fsync(handle.fileno())
    except OSError:
        pass  # (durability is best effort; the file is complete)


def _sources(files: Sequence[tuple[str, Path]]) -> list[tuple[str, bytes, float]]:
    """``[(member name, bytes, mtime)]`` of what is to be packed, every file read once, whole, and checked like the importer will check it."""
    rows: list[tuple[str, bytes, float]] = []
    seen: set[str] = set()
    total = 0
    for rel, path in files:
        if not isinstance(rel, str) or not BOT_FILE_RE.fullmatch(rel):
            raise ArchiveFormatError(f"这不是 bot 的文件，不能放进 .arenabot：{safe_text(rel)}")
        member = BOT_PREFIX + rel
        check_bot_relative(rel, member)
        if member in seen:
            raise ArchiveFormatError(f"同一个文件放了两次：{safe_text(member)}")
        seen.add(member)
        try:
            info = Path(path).lstat()
        except OSError:
            raise ArchiveIOError(f"读不到 bot 的文件：{safe_text(rel)}") from None
        if not stat.S_ISREG(info.st_mode):
            raise ArchiveFormatError(f"{safe_text(member)} 不是普通文件（符号链接和特殊文件不会被打包）。")
        if info.st_size > MAX_FILE_BYTES:
            raise ArchiveLimitError(f"{safe_text(member)} 太大了（单个文件最多 {human_bytes(MAX_FILE_BYTES)}）。")
        try:
            with open(path, "rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
        except OSError:
            raise ArchiveIOError(f"读不到 bot 的文件：{safe_text(rel)}") from None
        if len(data) > MAX_FILE_BYTES:
            raise ArchiveLimitError(f"{safe_text(member)} 太大了（单个文件最多 {human_bytes(MAX_FILE_BYTES)}）。")
        check_bot_file(member, rel, data)
        total += len(data)
        if total > MAX_BYTES:
            raise ArchiveLimitError(f"这个 bot 的文件加起来超过了 {human_bytes(MAX_BYTES)}，无法导出。")
        rows.append((member, data, info.st_mtime))
    if BOT_PREFIX + BOT_JSON not in seen:
        raise ArchiveFormatError("这个 bot 没有 bot.json，无法导出。")
    if len(rows) > MAX_ENTRIES:
        raise ArchiveLimitError("这个 bot 的文件太多了，无法导出。")
    rows.sort(key=lambda row: row[0])
    return rows


def pack_arenabot(
    target: Path,
    *,
    name: str,
    origin: str,
    files: Sequence[tuple[str, Path]],
    now: datetime | None = None,
) -> None:
    """Write ``target`` (a ``.arenabot``) for the bot ``name`` (``origin``: where the bot came from - for people to read).

    ``files`` is ``[(path inside the bot's directory, the file)]`` as :meth:`app.storage.bots.BotStore.bot_files` returns it - the whitelist
    is checked again here (:class:`ArchiveFormatError` for anything else, a link, a file that is not UTF-8 text, a missing ``bot.json``),
    and the whole bot may not be bigger than 20 MiB (:class:`ArchiveLimitError`).  The file only gets its name when it is complete (``.part``
    first), so an interrupted export leaves nothing behind that looks like one; a failure removes the ``.part``."""
    from app.archive.export import app_version_label  # (lazily: it reads the application's config)

    target = Path(target)
    rows = _sources(files)
    created = (now or datetime.now().astimezone()).replace(microsecond=0)
    if created.tzinfo is None:  # (a time with an offset keeps it, like the manifest of the whole archive: the file does not depend on the time zone of the machine)
        created = created.astimezone()
    entries = [FileEntry(path=member, size=len(data), sha256=hashlib.sha256(data).hexdigest()) for member, data, _mtime in rows]
    manifest = BotManifest(
        format=FORMAT_NAME,
        format_version=FORMAT_VERSION,
        created_at=created.isoformat(timespec="seconds"),
        app_version=app_version_label(),
        bot=BotInfo(name=_CONTROL.sub("", str(name))[:_NAME_CHARS], origin=_CONTROL.sub("", str(origin))[:64]),
        files=entries,
    )
    text = json.dumps(manifest.model_dump(), ensure_ascii=False, indent=1) + "\n"
    part = target.with_name(target.name + ".part")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for member, data, mtime in rows:
                zf.writestr(zip_info(member, mtime), data)
            zf.writestr(zip_info(MANIFEST_NAME, created.timestamp()), text.encode("utf-8"))
        _fsync(part)
        os.replace(part, target)
    except OSError as exc:
        _unlink(part)
        raise ArchiveIOError("写入 .arenabot 文件失败，请检查磁盘空间。") from exc
    except BaseException:
        _unlink(part)
        raise


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove the unfinished file %s", path.name)


# ----------------------------------------------------------------------------------------------------------- unpack
def unpack_arenabot(source: Path, workdir: Path, *, limits: ArchiveLimits = LIMITS) -> Path:
    """Check ``source`` (a ``.arenabot``) completely and unpack its ``bot/`` directory into ``workdir`` - ``<workdir>/bot`` is made here and
    returned.  Raises :class:`ArchiveError` (``.status`` / ``.code`` / ``.message``) when the file is not a ``.arenabot`` (or a total archive
    or a game file by mistake), is too big, is damaged or was changed after it was made (SHA-256), has a member the format does not allow
    (a path that leaves the folder, a nested archive, an unknown file ...) or a file that is not text.  Nothing is left behind on failure."""
    source = Path(source)
    workdir = Path(workdir)
    try:
        size = source.stat().st_size
    except OSError:
        raise ArchiveFormatError("找不到要导入的 bot 文件。") from None
    message = wrong_kind_message(sniff_format(source), expected=FORMAT_NAME)
    if message:
        raise ArchiveFormatError(message)
    if size > MAX_BYTES:
        raise ArchiveLimitError(f".arenabot 文件超过了 {human_bytes(MAX_BYTES)} 的上限。")
    target = workdir / "bot"
    with OpenedZip(source, limits, classify=classify_bot_member, parse_manifest=parse_bot_manifest) as opened:
        assert opened.zf is not None
        free = shutil.disk_usage(workdir).free if workdir.is_dir() else None
        if free is not None and free < opened.uncompressed_bytes + limits.min_free_margin_bytes:
            raise ArchiveIOError(f"磁盘空间不足：解压需要约 {human_bytes(opened.uncompressed_bytes)}，可用 {human_bytes(free)}。")
        _make_dir(target, "bot")
        try:
            for entry in opened.manifest.files:
                rel = entry.path[len(BOT_PREFIX) :]
                destination = target.joinpath(*rel.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                with open(destination, "xb") as sink:
                    data = stream_member(opened.zf, opened.infos[entry.path], entry, keep=True, sink=sink, on_bytes=_ignore)
                check_bot_file(entry.path, rel, data or b"")
        except ArchiveError:
            shutil.rmtree(target, ignore_errors=True)
            raise
        except OSError:
            shutil.rmtree(target, ignore_errors=True)
            raise ArchiveIOError("解压 bot 文件失败：磁盘空间不足或没有写入权限。") from None
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
    return target


def _ignore(_count: int) -> None:
    return None


def _make_dir(target: Path, what: str) -> None:
    """Make the directory the unpacked files go into - it must not exist yet: what a failed unpacking removes again is only ever what it made."""
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise ArchiveIOError(f"解压目录里已经有「{what}」文件夹，没有覆盖它。") from None
    except OSError:
        raise ArchiveIOError("无法创建解压目录：磁盘空间不足或没有写入权限。") from None


def read_arenabot_manifest(source: Path, *, limits: ArchiveLimits = LIMITS) -> BotManifest:
    """The checked manifest of a ``.arenabot`` (the structure, the names and the manifest - not the content of the files): who is in it, for a
    preview.  Raises :class:`ArchiveError` like :func:`unpack_arenabot`."""
    with OpenedZip(Path(source), limits, classify=classify_bot_member, parse_manifest=parse_bot_manifest) as opened:
        return opened.manifest


__all__ = [
    "BOT_PREFIX",
    "FILE_SUFFIX",
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "LIMITS",
    "MAX_BYTES",
    "BotInfo",
    "BotManifest",
    "classify_bot_member",
    "pack_arenabot",
    "parse_bot_manifest",
    "read_arenabot_manifest",
    "unpack_arenabot",
]
