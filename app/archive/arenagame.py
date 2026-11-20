"""``.arenagame``: one saved game as a file (docs/v0.14-plan.md H40; contract section 13.9).

A ``.arenagame`` is a zip file with exactly these members::

    manifest.json                       {"format": "arenagame", "format_version": 1, "session_id", "created_at", "app_version",
                                         "files": [{"path", "size", "sha256"}, ...]}
    games/<session_id>/game.json        the game (plain JSON; save format 5 of v0.13 or 6 of v0.14)
    logs/<session_id>/trace-NNNN.jsonl  the game's logs (the thinking record and the error log are rebuilt from them)

There is no ``game.prev.json``: the export packs the *good* copy of the game (:meth:`app.storage.games.GameStore.export_parts`).  A game of
v0.12.1 (save format 4) is not accepted - ``tools/migrate_v0121_to_archive.py`` converts it first.  Nothing else about the application's state
is in the file, so a game never carries the keys or the bots of the machine it came from; a v6 game carries what its bots remembered when it
started (``memory_snapshot``), which is all its replay and its resumption need.

The module has two jobs:

* :func:`export_game` / :func:`pack_arenagame` - make the file (atomically: ``.part`` then a rename);
* :func:`unpack_arenagame` / :func:`import_arenagame` - check a file completely (member names, sizes, SHA-256, the game itself) and install it
  into the games of this data directory (:meth:`app.storage.games.GameStore.install_game`), with a policy for a game id that is taken:
  ``ask`` (409 ``exists``), ``overwrite`` (the game and its logs are replaced; never the live game), ``copy`` (kept under a new id).

Blocking: run them in a worker thread.  The content of a file never appears in an error message.
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
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.archive.errors import (
    ArchiveError,
    ArchiveExistsError,
    ArchiveFormatError,
    ArchiveIOError,
    ArchiveLimitError,
    ArchiveVersionError,
    safe_text,
)
from app.archive.format import (
    CHUNK_BYTES,
    DEFAULT_LIMITS,
    KIND_GAME,
    KIND_LOG,
    KIND_MANIFEST,
    ArchiveLimits,
    FileEntry,
    classify,
    is_reserved_id,
)
from app.archive.validate import check_game
from app.archive.zipcheck import (
    MANIFEST_NAME,
    OpenedZip,
    human_bytes,
    restore_mtime,
    sniff_format,
    stream_member,
    wrong_kind_message,
    zip_info,
)
from app.storage.errors import StorageError
from app.storage.games import GameExistsError, GameProtectedError, GameStore, InstalledGame

logger = logging.getLogger(__name__)

FORMAT_NAME = "arenagame"
FORMAT_VERSION = 1
FILE_SUFFIX = ".arenagame"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: The game itself is parsed in memory to be checked, so it has a limit of its own (a game of 12 players is well under 10 MB).
MAX_GAME_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 10_000  # trace files of ~20 MB each: far more than any game has
LIMITS = replace(DEFAULT_LIMITS, max_entries=MAX_ENTRIES + 1, max_json_bytes=MAX_GAME_BYTES, max_manifest_bytes=8 * 1024 * 1024)
_ZIP64_HINT = 1 << 30
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# --------------------------------------------------------------------------------------------------------- manifest
class GameManifest(BaseModel):
    """``manifest.json`` of a ``.arenagame``."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["arenagame"]
    format_version: int = Field(ge=1)
    session_id: str
    created_at: str
    app_version: str = Field(min_length=1, max_length=64)
    files: list[FileEntry]

    @field_validator("session_id")
    @classmethod
    def _session_id(cls, value: str) -> str:
        if not SESSION_ID_RE.fullmatch(value) or is_reserved_id(value):
            raise ValueError("session_id must be 1-64 letters, digits, - or _ (and no reserved name)")
        return value

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


def classify_game_member(name: object) -> tuple[str, str | None]:
    """``(kind, session id)`` of a member name: ``("manifest", None)``, ``("game", id)`` for ``games/<id>/game.json`` or ``("log", id)`` for
    ``logs/<id>/trace-NNNN.jsonl``; every other name - ``game.prev.json``, a settings file, a nested archive, a path that leaves the
    folder - is refused with its reason.  The one place that says which member names a ``.arenagame`` may have."""
    kind, key = classify(name)  # (a safe relative path of a form the data directory knows, no reserved id)
    if kind not in (KIND_MANIFEST, KIND_GAME, KIND_LOG):
        raise ArchiveFormatError(f"这个对局文件里有不该有的文件：{safe_text(name)}")
    return kind, key


def parse_game_manifest(raw: bytes) -> GameManifest:
    """Parse and check ``manifest.json``: JSON, the format name, the version (a newer one is reported as such), the model, and that the file list is
    exactly one ``games/<session_id>/game.json`` and logs ``logs/<session_id>/trace-NNNN.jsonl`` of the same game, none twice (also not by case)."""
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
            or "这不是对局文件（manifest.json 里没有格式名 arenagame）。"
        )
    version = data.get("format_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ArchiveFormatError("这个对局文件的格式版本无效。")
    if version > FORMAT_VERSION:
        raise ArchiveVersionError(
            f"这个对局文件使用更新的格式（v{version}），当前程序只支持到 v{FORMAT_VERSION}。请先把 Arena-Agents 升级到较新的版本再导入。"
        )
    try:
        manifest = GameManifest.model_validate(data)
    except ValidationError as exc:
        where = ", ".join(sorted({".".join(str(p) for p in err["loc"]) or "manifest" for err in exc.errors()[:5]}))
        raise ArchiveFormatError(f"manifest.json 的内容不符合格式（{safe_text(where, 120)}）。") from None
    seen: set[str] = set()
    seen_lower: set[str] = set()
    games = 0
    for entry in manifest.files:
        kind, key = classify_game_member(entry.path)
        if kind == KIND_MANIFEST:
            raise ArchiveFormatError("清单不应该列出 manifest.json 自己。")
        if key != manifest.session_id:
            raise ArchiveFormatError(f"{safe_text(entry.path)} 不属于清单里的对局（{safe_text(manifest.session_id)}）。")
        if entry.path in seen:
            raise ArchiveFormatError(f"清单里的文件重复：{safe_text(entry.path)}")
        if entry.path.lower() in seen_lower:
            raise ArchiveFormatError(f"清单里有只差大小写的路径：{safe_text(entry.path)}")
        seen.add(entry.path)
        seen_lower.add(entry.path.lower())
        games += kind == KIND_GAME
        if kind == KIND_GAME and entry.size > MAX_GAME_BYTES:
            raise ArchiveLimitError(f"对局文件太大了（最多 {human_bytes(MAX_GAME_BYTES)}）。")
    if games != 1:
        raise ArchiveFormatError("这个对局文件里没有对局本身（games/<id>/game.json）。")
    if len(manifest.files) > MAX_ENTRIES:
        raise ArchiveLimitError("这个对局文件里的文件太多了。")
    return manifest


# ------------------------------------------------------------------------------------------------------------- pack
@dataclass
class ExportedGame:
    """What :func:`export_game` made."""

    path: Path
    session_id: str
    size_bytes: int
    log_files: int
    used_previous: bool = False  # the main file of the game was damaged: the previous good copy was packed
    warnings: list[str] = field(default_factory=list)


def _fsync(path: Path) -> None:
    try:
        with open(path, "rb+") as handle:
            os.fsync(handle.fileno())
    except OSError:
        pass  # (durability is best effort; the file is complete)


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove the unfinished file %s", path.name)


def pack_arenagame(
    target: Path,
    *,
    session_id: str,
    game: bytes,
    logs: Sequence[tuple[str, Path]] = (),
    now: datetime | None = None,
) -> None:
    """Write ``target`` (a ``.arenagame``) for the game ``session_id``: ``game`` is the bytes of its ``game.json`` - they are checked first
    (a game of this version's save formats that loads, whose id is ``session_id``: :class:`ArchiveFormatError` otherwise, so no file is made
    that this version could not import) - and ``logs`` is ``[(trace-NNNN.jsonl, path)]``, streamed (a log may be hundreds of MB) and hashed as
    it is written.  The file gets its name only when it is complete (``.part`` first); a failure removes the ``.part``."""
    from app.archive.export import app_version_label  # (lazily: it reads the application's config)

    target = Path(target)
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id) or is_reserved_id(session_id):
        raise ArchiveFormatError("无效的对局 id。")
    if len(game) > MAX_GAME_BYTES:
        raise ArchiveLimitError(f"这局的存档太大了（最多 {human_bytes(MAX_GAME_BYTES)}），无法导出。")
    check_game(game, session_id, label=f"games/{session_id}/game.json")
    names: set[str] = set()
    sources: list[tuple[str, Path, os.stat_result]] = []
    for name, path in sorted(logs, key=lambda item: str(item[0])):
        member = f"logs/{session_id}/{name}"
        kind, _key = classify_game_member(member)
        if kind != KIND_LOG or member in names:
            raise ArchiveFormatError(f"这不是对局的日志文件：{safe_text(name)}")
        names.add(member)
        try:
            info = Path(path).lstat()
        except OSError:
            raise ArchiveIOError(f"读不到日志文件：{safe_text(name)}") from None
        if not stat.S_ISREG(info.st_mode):
            raise ArchiveFormatError(f"日志 {safe_text(name)} 不是普通文件（符号链接和特殊文件不会被打包）。")
        sources.append((member, Path(path), info))
    if len(sources) + 1 > MAX_ENTRIES:
        raise ArchiveLimitError("这局的日志文件太多了，无法导出。")

    created = (now or datetime.now()).astimezone()
    game_member = f"games/{session_id}/game.json"
    entries = [FileEntry(path=game_member, size=len(game), sha256=hashlib.sha256(game).hexdigest())]
    part = target.with_name(target.name + ".part")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            zf.writestr(zip_info(game_member, created.timestamp()), game)
            for member, path, info in sources:
                member_info = zip_info(member, info.st_mtime)
                member_info.file_size = info.st_size
                digest = hashlib.sha256()
                size = 0
                with open(path, "rb") as reader, zf.open(member_info, "w", force_zip64=info.st_size >= _ZIP64_HINT) as writer:
                    while True:
                        chunk = reader.read(CHUNK_BYTES)
                        if not chunk:
                            break
                        digest.update(chunk)
                        writer.write(chunk)
                        size += len(chunk)
                entries.append(FileEntry(path=member, size=size, sha256=digest.hexdigest()))
            manifest = GameManifest(
                format=FORMAT_NAME,
                format_version=FORMAT_VERSION,
                session_id=session_id,
                created_at=created.isoformat(timespec="seconds"),
                app_version=app_version_label(),
                files=sorted(entries, key=lambda entry: entry.path),
            )
            zf.writestr(zip_info(MANIFEST_NAME, created.timestamp()), json.dumps(manifest.model_dump(), ensure_ascii=False, indent=1) + "\n")
        _fsync(part)
        os.replace(part, target)
    except OSError as exc:
        _unlink(part)
        raise ArchiveIOError("写入 .arenagame 文件失败，请检查磁盘空间。") from exc
    except BaseException:
        _unlink(part)
        raise


def export_game(store: GameStore, session_id: str, target: Path, *, now: datetime | None = None) -> ExportedGame:
    """Pack the saved game ``session_id`` of ``store`` into ``target``: its good copy and its logs.  :class:`~app.storage.games.GameNotFoundError`
    when there is no such game, :class:`~app.storage.errors.StorageError` when neither of its files can be read, :class:`ArchiveError` when
    the game is not one this version can export (an old save format, a game that does not load)."""
    parts = store.export_parts(session_id)
    warnings: list[str] = []
    if parts.used_previous:
        warnings.append("这局的主文件已损坏，导出的是它的上一份备份副本。")
    pack_arenagame(target, session_id=session_id, game=parts.game_bytes, logs=parts.logs, now=now)
    return ExportedGame(
        path=Path(target),
        session_id=session_id,
        size_bytes=Path(target).stat().st_size,
        log_files=len(parts.logs),
        used_previous=parts.used_previous,
        warnings=warnings,
    )


# ----------------------------------------------------------------------------------------------------------- unpack
@dataclass
class UnpackedGame:
    """A checked ``.arenagame``, unpacked: ``game_file`` is the game, ``logs`` is ``[(name, path)]``; ``facts`` is what
    :func:`app.archive.validate.check_game` says about the game (title, status, winner, players ...)."""

    session_id: str
    game_file: Path
    logs: list[tuple[str, Path]]
    facts: dict[str, Any]
    created_at: str
    app_version: str
    uncompressed_bytes: int


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


def unpack_arenagame(source: Path, workdir: Path, *, limits: ArchiveLimits = LIMITS) -> UnpackedGame:
    """Check ``source`` (a ``.arenagame``) completely and unpack it into ``<workdir>/game`` (``game.json`` and ``logs/``).  Raises
    :class:`ArchiveError` (``.status`` / ``.code`` / ``.message``) when the file is not a ``.arenagame`` (or a total archive or a bot file by
    mistake), is damaged or was changed after it was made (SHA-256), has a member the format does not allow, would not fit on the disk, or
    holds a game that is not of save format 5 / 6 or does not load.  Nothing is left behind on failure."""
    source = Path(source)
    workdir = Path(workdir)
    if not source.is_file():
        raise ArchiveFormatError("找不到要导入的对局文件。")
    message = wrong_kind_message(sniff_format(source), expected=FORMAT_NAME)
    if message:
        raise ArchiveFormatError(message)
    stage = workdir / "game"
    with OpenedZip(source, limits, classify=classify_game_member, parse_manifest=parse_game_manifest) as opened:
        assert opened.zf is not None
        manifest: GameManifest = opened.manifest
        free = shutil.disk_usage(workdir).free if workdir.is_dir() else None
        if free is not None and free < opened.uncompressed_bytes + limits.min_free_margin_bytes:
            raise ArchiveIOError(f"磁盘空间不足：解压需要约 {human_bytes(opened.uncompressed_bytes)}，可用 {human_bytes(free)}。")
        _make_dir(stage, "game")
        try:
            (stage / "logs").mkdir()
            game_file = stage / "game.json"
            logs: list[tuple[str, Path]] = []
            facts: dict[str, Any] = {}
            for entry in manifest.files:
                kind, _key = classify_game_member(entry.path)
                info = opened.infos[entry.path]
                if kind == KIND_GAME:
                    with open(game_file, "xb") as sink:
                        data = stream_member(opened.zf, info, entry, keep=True, sink=sink, on_bytes=_ignore)
                    facts = check_game(data or b"", manifest.session_id, label=entry.path)
                    restore_mtime(game_file, info)
                else:
                    name = entry.path.rsplit("/", 1)[-1]
                    path = stage / "logs" / name
                    with open(path, "xb") as sink:
                        stream_member(opened.zf, info, entry, keep=False, sink=sink, on_bytes=_ignore)
                    restore_mtime(path, info)
                    logs.append((name, path))
        except ArchiveError:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        except OSError:
            shutil.rmtree(stage, ignore_errors=True)
            raise ArchiveIOError("解压对局文件失败：磁盘空间不足或没有写入权限。") from None
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        return UnpackedGame(
            session_id=manifest.session_id,
            game_file=game_file,
            logs=sorted(logs),
            facts=facts,
            created_at=manifest.created_at,
            app_version=manifest.app_version,
            uncompressed_bytes=opened.uncompressed_bytes,
        )


def import_arenagame(
    store: GameStore,
    source: Path,
    workdir: Path,
    *,
    on_conflict: str = "ask",
    protected: Collection[str] = (),
    limits: ArchiveLimits = LIMITS,
) -> InstalledGame:
    """Check ``source`` (a ``.arenagame``) and install its game into ``store``.

    ``on_conflict`` is what to do when a game with this id exists: ``"ask"`` - :class:`ArchiveExistsError` (409 ``exists``, carrying the id) and
    nothing is changed; ``"overwrite"`` - the game and its logs are replaced completely; ``"copy"`` - it is kept under a new id.  ``protected``
    are ids that count as existing even without files and can never be overwritten - the live game's: ``"overwrite"`` of one of them is
    :class:`ArchiveError` with code ``current`` (409).  ``workdir`` is a scratch directory (the caller removes it)."""
    unpacked = unpack_arenagame(source, workdir, limits=limits)
    try:
        return store.install_game(unpacked.session_id, unpacked.game_file, unpacked.logs, on_conflict=on_conflict, protected=protected)
    except GameExistsError as exc:
        raise ArchiveExistsError(
            f"已经有一局 id 相同的对局（{safe_text(exc.session_id)}）：请选择覆盖它，或者保留两份。", session_id=exc.session_id
        ) from None
    except GameProtectedError as exc:
        raise ArchiveError(str(exc), code="current", status=409) from None
    except StorageError as exc:
        raise ArchiveIOError(str(exc) or "无法保存导入的对局。") from None


__all__ = [
    "FILE_SUFFIX",
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "LIMITS",
    "MAX_GAME_BYTES",
    "ExportedGame",
    "GameManifest",
    "UnpackedGame",
    "classify_game_member",
    "export_game",
    "import_arenagame",
    "pack_arenagame",
    "parse_game_manifest",
    "unpack_arenagame",
]
