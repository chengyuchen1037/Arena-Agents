"""The ``.arenaagents`` container (docs/archive.md): names, path allow-list, manifest model, checksums.

An archive is a zip file whose members are exactly the files listed in ``manifest.json`` (plus the manifest
itself).  Nothing in it is encrypted except ``secrets.enc`` (the API keys, see :mod:`app.archive.kdf`).  This
module knows *what is allowed*; reading and writing live in :mod:`app.archive.export` / :mod:`app.archive.importer`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.archive.errors import ArchiveFormatError, ArchiveVersionError, safe_text

FORMAT_NAME = "arenaagents"
FORMAT_VERSION = 1
FILE_SUFFIX = ".arenaagents"
MANIFEST_NAME = "manifest.json"
SECRETS_NAME = "secrets.enc"

#: Names of the files ``data/exports/`` holds (and the only names the download endpoint serves).
EXPORT_NAME_RE = re.compile(r"^arena-[0-9]{8}-[0-9]{6}\.arenaagents$")
ARCHIVE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

CHUNK_BYTES = 1024 * 1024

# --------------------------------------------------------------------------------------------- limits


@dataclass(frozen=True)
class ArchiveLimits:
    """What an archive may cost.  A zip header can lie about sizes, so these are checked against the
    declared sizes *and* enforced while the data is read."""

    max_entries: int = 50_000  # members, manifest included
    max_file_bytes: int = 8 * 1024**3  # one unpacked member
    max_total_bytes: int = 64 * 1024**3  # all members unpacked
    max_ratio: int = 300  # unpacked / packed for members above ``ratio_floor_bytes``
    ratio_floor_bytes: int = 1024 * 1024
    max_manifest_bytes: int = 32 * 1024 * 1024
    max_json_bytes: int = 256 * 1024 * 1024  # a member that has to be parsed in memory (a game, the settings ...)
    max_secrets_bytes: int = 1024 * 1024
    min_free_margin_bytes: int = 256 * 1024 * 1024  # kept free on the disk while unpacking


DEFAULT_LIMITS = ArchiveLimits()

# ------------------------------------------------------------------------------------------- the paths

_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_ID = r"(?P<id>[A-Za-z0-9_-]{1,64})"

KIND_MANIFEST = "manifest"
KIND_SETTINGS = "settings"
KIND_PROVIDERS = "providers"
KIND_PRICING = "pricing"
KIND_ROSTER_JSON = "roster_json"
KIND_ROSTER_TEXT = "roster_text"
KIND_GAMES_INDEX = "games_index"
KIND_GAME = "game"
KIND_GAME_PREV = "game_prev"
KIND_LOG = "log"
KIND_SECRETS = "secrets"

#: The only member names an archive may have (docs/v0.13-plan.md §2.2).  Anything else is rejected, however
#: harmless it looks: the importer never has to decide what a stray file means.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (KIND_MANIFEST, re.compile(r"manifest\.json")),
    (KIND_SETTINGS, re.compile(r"settings\.json")),
    (KIND_PROVIDERS, re.compile(r"providers\.json")),
    (KIND_PRICING, re.compile(r"pricing\.json")),
    (KIND_ROSTER_JSON, re.compile(r"roster/roster\.json")),
    (KIND_ROSTER_TEXT, re.compile(r"roster/N(?:[1-9]|[1-9][0-9])\.(?:persona|memory)\.md")),
    (KIND_GAMES_INDEX, re.compile(r"games/index\.json")),
    (KIND_GAME, re.compile(rf"games/{_ID}/game\.json")),
    (KIND_GAME_PREV, re.compile(rf"games/{_ID}/game\.prev\.json")),
    (KIND_LOG, re.compile(rf"logs/{_ID}/trace-[0-9]{{4,}}\.jsonl")),
    (KIND_SECRETS, re.compile(r"secrets\.enc")),
)
#: Kinds that hold data the program parses (as opposed to logs, which are only stored).
JSON_KINDS = frozenset({KIND_SETTINGS, KIND_PROVIDERS, KIND_PRICING, KIND_ROSTER_JSON, KIND_GAMES_INDEX, KIND_GAME, KIND_GAME_PREV})
#: Kinds of the manifest's ``files`` list (the manifest does not list itself).
LISTED_KINDS = frozenset(kind for kind, _ in _RULES if kind != KIND_MANIFEST)


def is_reserved_id(value: str) -> bool:
    """``True`` for names Windows cannot use as a directory (``CON``, ``NUL``, ``COM1`` ...)."""
    return value.upper() in _WINDOWS_RESERVED


def classify(name: object) -> tuple[str, str | None]:
    """``(kind, game_id)`` of a member name; :class:`ArchiveFormatError` when the name is not allowed.

    Rejected with a specific reason first: absolute paths, drive letters, backslashes, ``..`` / ``.`` /
    empty components, control characters, directories.  Everything that survives must match one of the
    allow-listed forms exactly.
    """
    if not isinstance(name, str) or not name:
        raise ArchiveFormatError("存档里有一个空的文件名。")
    shown = safe_text(name)
    if len(name) > 256 or any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in name):
        raise ArchiveFormatError(f"存档里有不合法的文件名：{shown}")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or name.startswith("\\\\"):
        raise ArchiveFormatError(f"存档里有绝对路径：{shown}")
    if "\\" in name:
        raise ArchiveFormatError(f"存档里的路径含有反斜杠：{shown}")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ArchiveFormatError(f"存档里的路径含有 .. 或空的部分：{shown}")
    for kind, pattern in _RULES:
        match = pattern.fullmatch(name)
        if match is None:
            continue
        game_id = match.groupdict().get("id")
        if game_id is not None and is_reserved_id(game_id):
            raise ArchiveFormatError(f"存档里的对局编号是系统保留名称：{shown}")
        return kind, game_id
    raise ArchiveFormatError(f"存档里有不认识的文件：{shown}")


# ------------------------------------------------------------------------------------------ the manifest


class FileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    size: int = Field(ge=0)
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _hex(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("sha256 must be 64 lowercase hex digits")
        return value


class Contents(BaseModel):
    model_config = ConfigDict(extra="forbid")

    games: int = Field(ge=0)
    providers: int = Field(ge=0)
    roster_seats: int = Field(ge=0)
    has_secrets: bool
    has_logs: bool


class Manifest(BaseModel):
    """``manifest.json``: what is in the archive and how to check it (docs/archive.md "清单")."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["arenaagents"]
    format_version: int = Field(ge=1)
    app_version: str = Field(min_length=1, max_length=64)
    archive_id: str
    created_at: str
    contents: Contents
    files: list[FileEntry]
    files_sha256: str

    @field_validator("archive_id")
    @classmethod
    def _archive_id(cls, value: str) -> str:
        if not ARCHIVE_ID_RE.fullmatch(value):
            raise ValueError("archive_id must be 32 lowercase hex digits")
        return value

    @field_validator("created_at")
    @classmethod
    def _created_at(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)  # ValueError -> a validation error
        if parsed.tzinfo is None:
            raise ValueError("created_at must carry a UTC offset")
        return value

    @field_validator("app_version")
    @classmethod
    def _app_version(cls, value: str) -> str:
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            raise ValueError("app_version holds control characters")
        return value

    @field_validator("files_sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("files_sha256 must be 64 lowercase hex digits")
        return value


def files_digest(files: Iterable[FileEntry | dict[str, Any]]) -> str:
    """SHA-256 of the file list: the list sorted by path, each item ``{"path", "sha256", "size"}``, serialized as
    compact JSON with sorted keys and non-ASCII kept (UTF-8).  Anyone can recompute it with a few lines."""
    rows = []
    for item in files:
        row = item.model_dump() if isinstance(item, FileEntry) else dict(item)
        rows.append({"path": row["path"], "sha256": row["sha256"], "size": int(row["size"])})
    rows.sort(key=lambda row: row["path"])
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_manifest(
    *,
    archive_id: str,
    created_at: str,
    app_version: str,
    contents: Contents,
    files: list[FileEntry],
) -> Manifest:
    ordered = sorted(files, key=lambda entry: entry.path)
    return Manifest(
        format=FORMAT_NAME,
        format_version=FORMAT_VERSION,
        app_version=app_version,
        archive_id=archive_id,
        created_at=created_at,
        contents=contents,
        files=ordered,
        files_sha256=files_digest(ordered),
    )


def manifest_bytes(manifest: Manifest) -> bytes:
    return (json.dumps(manifest.model_dump(), ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def parse_manifest(raw: bytes) -> Manifest:
    """Parse and check ``manifest.json``: JSON, the format name, the format version, the model, every listed path,
    duplicates, the consistency of ``contents`` with the list and ``files_sha256`` of the list.

    A *newer* format version is reported as such (``ArchiveVersionError``) before anything else is judged: a
    newer program may have changed the model.
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ArchiveFormatError("manifest.json 不是有效的 JSON。") from None
    if not isinstance(data, dict):
        raise ArchiveFormatError("manifest.json 的内容不是一个对象。")
    if data.get("format") != FORMAT_NAME:
        raise ArchiveFormatError("这不是 Arena-Agents 存档（manifest.json 里的格式名不是 arenaagents）。")
    version = data.get("format_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ArchiveFormatError("存档的格式版本无效。")
    if version > FORMAT_VERSION:
        raise ArchiveVersionError(
            f"这份存档使用更新的格式（v{version}），当前程序只支持到 v{FORMAT_VERSION}。请先把 Arena-Agents 升级到较新的版本再导入。"
        )
    try:
        manifest = Manifest.model_validate(data)
    except ValidationError as exc:
        where = ", ".join(sorted({".".join(str(p) for p in err["loc"]) or "manifest" for err in exc.errors()[:5]}))
        raise ArchiveFormatError(f"manifest.json 的内容不符合格式（{safe_text(where, 120)}）。") from None
    seen: set[str] = set()
    seen_lower: set[str] = set()
    for entry in manifest.files:
        kind, _ = classify(entry.path)
        if kind == KIND_MANIFEST:
            raise ArchiveFormatError("清单不应该列出 manifest.json 自己。")
        if entry.path in seen:
            raise ArchiveFormatError(f"清单里的文件重复：{safe_text(entry.path)}")
        if entry.path.lower() in seen_lower:
            raise ArchiveFormatError(f"清单里有只差大小写的路径：{safe_text(entry.path)}")
        seen.add(entry.path)
        seen_lower.add(entry.path.lower())
    if manifest.files_sha256 != files_digest(manifest.files):
        raise ArchiveFormatError("清单的文件列表校验值（files_sha256）与列表不符：存档可能已被改动。")
    has_secrets = SECRETS_NAME in seen
    has_logs = any(classify(entry.path)[0] == KIND_LOG for entry in manifest.files)
    if manifest.contents.has_secrets != has_secrets or manifest.contents.has_logs != has_logs:
        raise ArchiveFormatError("清单的内容统计（是否含 API Key / 日志）与文件列表不符。")
    return manifest


def sha256_stream(stream: BinaryIO, *, limit: int | None = None) -> tuple[str, int]:
    """``(hex digest, bytes read)`` of a binary stream, 1 MiB at a time; ``limit`` bounds what is read."""
    digest = hashlib.sha256()
    total = 0
    while True:
        want = CHUNK_BYTES if limit is None else min(CHUNK_BYTES, limit - total + 1)
        chunk = stream.read(want)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
        if limit is not None and total > limit:
            break
    return digest.hexdigest(), total
