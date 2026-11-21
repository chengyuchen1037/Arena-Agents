"""The ``.arenaagents`` container (docs/archive.md): names, path allow-list, manifest model, checksums.

An archive is a zip file whose members are exactly the files listed in ``manifest.json`` (plus the manifest
itself).  Nothing in it is encrypted except ``secrets.enc`` (the API keys, see :mod:`app.archive.kdf`).  This
module knows *what is allowed*; reading and writing live in :mod:`app.archive.export` / :mod:`app.archive.importer`.

**Format 2** (v0.14, docs/v0.14-plan.md §2.5; contract §13.9) is the data directory as it is: the settings, the providers, the prices,
the official contract (``contract/``), the rules (``rules/<scenario>/``: the scenario and its study materials), the bots (``bots/lineup.json``,
``bots/usage.json``, ``bots/<bot_id>/**``), every game (``games/<id>/game.json`` and its previous copy ``game.prev.json``), every log
(``logs/<id>/trace-*.jsonl``, ``logs/learning/<job>.jsonl``) and - optionally, encrypted - the API keys.  **Format 1** (v0.13) has a roster
(``roster/**``) instead of the bots, the contract and the rules; it is still read (the roster becomes bots when it is imported, H45) but no
longer written - except on request, for tools that still make such archives.  A member name is only allowed in the format version that
knows it (:func:`classify` with ``version``).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.archive.errors import ArchiveFormatError, ArchiveVersionError, safe_text

FORMAT_NAME = "arenaagents"
FORMAT_VERSION = 2  # what is written; ``SUPPORTED_VERSIONS`` is what is read
LEGACY_VERSION = 1  # v0.13: a roster of seats instead of bots, a contract and rules
SUPPORTED_VERSIONS = (1, 2)
FILE_SUFFIX = ".arenaagents"
MANIFEST_NAME = "manifest.json"
SECRETS_NAME = "secrets.enc"

#: Names of the files ``data/exports/`` holds (and the only names the download endpoint serves).
EXPORT_NAME_RE = re.compile(r"^arena-[0-9]{8}-[0-9]{6}\.arenaagents$")
#: The name the user's browser saves a finished export as (H42): ``arena-agents-YYYYmmdd-HHMMSS.arenaagents``.  The file in ``exports/`` keeps
#: its fixed internal name (``arena-YYYYmmdd-HHMMSS.arenaagents``, see :data:`EXPORT_NAME_RE`): a name inside the data directory is never
#: derived from anything but the time.
DOWNLOAD_PREFIX = "arena-agents-"
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
#: Not a game id: ``logs/learning/`` holds the logs of the initializations and reviews, not of a game.
LEARNING_DIR = "learning"
_ID = r"(?P<id>[A-Za-z0-9_-]{1,64})"
_SLUG = r"[a-z0-9][a-z0-9-]{0,31}"  # a material, the extra section of an L0, a proposal
_SCENARIO = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
_BOT_ID = r"b-[0-9a-f]{8}"

KIND_MANIFEST = "manifest"
KIND_SETTINGS = "settings"
KIND_PROVIDERS = "providers"
KIND_PRICING = "pricing"
KIND_ROSTER_JSON = "roster_json"  # format 1 only
KIND_ROSTER_TEXT = "roster_text"  # format 1 only
KIND_CONTRACT = "contract"  # format 2: contract/contract.md
KIND_CONTRACT_SOURCE = "contract_source"  # contract/source.json
KIND_RULES_SCENARIO = "rules_scenario"  # rules/<scenario>/scenario.md
KIND_RULES_SOURCE = "rules_source"  # rules/<scenario>/source.json
KIND_RULES_MATERIAL = "rules_material"  # rules/<scenario>/materials/<slug>.md
KIND_RULES_MATERIAL_META = "rules_material_meta"  # rules/<scenario>/materials/<slug>.json (the title)
KIND_BOTS_LINEUP = "bots_lineup"  # bots/lineup.json
KIND_BOTS_USAGE = "bots_usage"  # bots/usage.json
KIND_BOT_FILE = "bot_file"  # bots/<bot_id>/<file of a bot>
KIND_GAMES_INDEX = "games_index"
KIND_GAME = "game"
KIND_GAME_PREV = "game_prev"
KIND_LOG = "log"  # logs/<game>/trace-NNNN.jsonl
KIND_LEARNING_LOG = "learning_log"  # format 2: logs/learning/<job>.jsonl
KIND_SECRETS = "secrets"

#: The files of one bot, relative to its directory (``bots/<bot_id>/`` in the total archive, ``bot/`` in a ``.arenabot``): the bot itself,
#: the L0 (three required sections and up to three extras), the notes, the memory of each scenario, the impressions, the record and
#: the proposals waiting for the user.  Anything else in a bot's directory is not a bot's file.
BOT_FILE_RE = re.compile(
    r"bot\.json"
    r"|core/(?:identity|voice|mind)\.md"
    rf"|core/extra-{_SLUG}\.md"
    r"|arch/notes\.md"
    rf"|rules/{_SCENARIO}/(?:digest|playbook|questions)\.md"
    r"|people\.md"
    r"|record\.json"
    rf"|staging/{_SLUG}\.json"
)

_V1 = (1,)
_V2 = (2,)
_BOTH = (1, 2)
#: The only member names an archive may have (docs/v0.13-plan.md §2.2, docs/v0.14-plan.md §2.5), and the format versions that have them.
#: Anything else is rejected, however harmless it looks: the importer never has to decide what a stray file means.  Order matters where two
#: forms could match the same name (the learning logs come before the game logs).
_RULES: tuple[tuple[str, re.Pattern[str], tuple[int, ...]], ...] = (
    (KIND_MANIFEST, re.compile(r"manifest\.json"), _BOTH),
    (KIND_SETTINGS, re.compile(r"settings\.json"), _BOTH),
    (KIND_PROVIDERS, re.compile(r"providers\.json"), _BOTH),
    (KIND_PRICING, re.compile(r"pricing\.json"), _BOTH),
    (KIND_ROSTER_JSON, re.compile(r"roster/roster\.json"), _V1),
    (KIND_ROSTER_TEXT, re.compile(r"roster/N(?:[1-9]|[1-9][0-9])\.(?:persona|memory)\.md"), _V1),
    (KIND_CONTRACT, re.compile(r"contract/contract\.md"), _V2),
    (KIND_CONTRACT_SOURCE, re.compile(r"contract/source\.json"), _V2),
    (KIND_RULES_SCENARIO, re.compile(rf"rules/(?P<id>{_SCENARIO})/scenario\.md"), _V2),
    (KIND_RULES_SOURCE, re.compile(rf"rules/(?P<id>{_SCENARIO})/source\.json"), _V2),
    (KIND_RULES_MATERIAL, re.compile(rf"rules/(?P<id>{_SCENARIO})/materials/{_SLUG}\.md"), _V2),
    (KIND_RULES_MATERIAL_META, re.compile(rf"rules/(?P<id>{_SCENARIO})/materials/{_SLUG}\.json"), _V2),
    (KIND_BOTS_LINEUP, re.compile(r"bots/lineup\.json"), _V2),
    (KIND_BOTS_USAGE, re.compile(r"bots/usage\.json"), _V2),
    (KIND_BOT_FILE, re.compile(rf"bots/(?P<id>{_BOT_ID})/(?:{BOT_FILE_RE.pattern})"), _V2),
    (KIND_GAMES_INDEX, re.compile(r"games/index\.json"), _BOTH),
    (KIND_GAME, re.compile(rf"games/{_ID}/game\.json"), _BOTH),
    (KIND_GAME_PREV, re.compile(rf"games/{_ID}/game\.prev\.json"), _BOTH),
    (KIND_LEARNING_LOG, re.compile(rf"logs/{LEARNING_DIR}/[A-Za-z0-9_-]{{1,64}}\.jsonl"), _V2),
    (KIND_LOG, re.compile(rf"logs/{_ID}/trace-[0-9]{{4,}}\.jsonl"), _BOTH),
    (KIND_SECRETS, re.compile(r"secrets\.enc"), _BOTH),
)
#: Kinds that hold data the program parses (as opposed to logs, which are only stored).
JSON_KINDS = frozenset(
    {
        KIND_SETTINGS,
        KIND_PROVIDERS,
        KIND_PRICING,
        KIND_ROSTER_JSON,
        KIND_GAMES_INDEX,
        KIND_GAME,
        KIND_GAME_PREV,
        KIND_CONTRACT_SOURCE,
        KIND_RULES_SOURCE,
        KIND_RULES_MATERIAL_META,
        KIND_BOTS_LINEUP,
        KIND_BOTS_USAGE,
    }
)
#: Kinds that are plain UTF-8 text (checked for size and encoding, not parsed).
TEXT_KINDS = frozenset({KIND_ROSTER_TEXT, KIND_CONTRACT, KIND_RULES_SCENARIO, KIND_RULES_MATERIAL})
#: Kinds that are only stored: the logs (their content is never read by the importer).
LOG_KINDS = frozenset({KIND_LOG, KIND_LEARNING_LOG})
#: Kinds of the manifest's ``files`` list (the manifest does not list itself).
LISTED_KINDS = frozenset(kind for kind, _, _ in _RULES if kind != KIND_MANIFEST)


def kinds_for(version: int) -> frozenset[str]:
    """The kinds of member (:func:`classify`) an archive of format ``version`` may have, the manifest included."""
    return frozenset(kind for kind, _pattern, versions in _RULES if version in versions)


def download_name(stored_name: str) -> str:
    """``arena-agents-YYYYmmdd-HHMMSS.arenaagents`` for the stored export ``arena-YYYYmmdd-HHMMSS.arenaagents`` (H42); any other name is returned
    as it is."""
    if EXPORT_NAME_RE.fullmatch(stored_name):
        return DOWNLOAD_PREFIX + stored_name[len("arena-") :]
    return stored_name


def is_reserved_id(value: str) -> bool:
    """``True`` for names that cannot be a game's directory: the ones Windows cannot use (``CON``, ``NUL``, ``COM1`` ...) and ``learning``
    (``logs/learning/`` is where the logs of the initializations and reviews live)."""
    return value.upper() in _WINDOWS_RESERVED or value.lower() == LEARNING_DIR


def is_reserved_name(value: str) -> bool:
    """``True`` for names Windows cannot use as a directory (``CON``, ``NUL``, ``COM1`` ...)."""
    return value.upper() in _WINDOWS_RESERVED


def check_bot_relative(rel: str, shown: str) -> None:
    """One more thing :data:`BOT_FILE_RE` cannot say about a bot's file (``rel`` is its path inside the bot's directory, ``shown`` the name to
    put into the message): the scenario folder of the bot's memory (``rules/<scenario>/``) is no name Windows cannot use as a directory."""
    parts = rel.split("/")
    if len(parts) == 3 and parts[0] == "rules" and is_reserved_name(parts[1]):
        raise ArchiveFormatError(f"存档里的剧本编号是系统保留名称：{safe_text(shown)}")


def check_member_name(name: object) -> str:
    """The name of a zip member when it is a safe relative path of ``/``-separated parts, else :class:`ArchiveFormatError`.  This is the part of
    the allow-list every container shares (the ``.arenaagents``, the ``.arenabot`` and the ``.arenagame``); what is *allowed* is up to each.

    Rejected, each with its own reason: an empty name, control characters and over-long names, absolute paths, drive letters, UNC paths,
    backslashes, ``..`` / ``.`` / empty parts (a trailing ``/`` - a directory entry - is an empty part)."""
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
    return name


def classify(name: object, *, version: int | None = None) -> tuple[str, str | None]:
    """``(kind, key)`` of a member name - ``key`` is the game id of a game / log, the bot id of a bot's file, the scenario id of a scenario's
    file, else ``None``; :class:`ArchiveFormatError` when the name is not allowed.  ``version`` limits the allowed names to the ones that
    format version has (``None``: any version's: the zip is scanned before its manifest says which it is).

    A name first has to be a safe path (:func:`check_member_name`); everything that survives must match one of the allow-listed forms
    exactly.
    """
    name = check_member_name(name)
    shown = safe_text(name)
    for kind, pattern, versions in _RULES:
        match = pattern.fullmatch(name)
        if match is None:
            continue
        if version is not None and version not in versions:
            raise ArchiveFormatError(f"存档里有这个格式版本（v{version}）不该有的文件：{shown}")
        key = match.groupdict().get("id")
        if key is not None and kind in (KIND_GAME, KIND_GAME_PREV, KIND_LOG) and is_reserved_id(key):
            raise ArchiveFormatError(f"存档里的对局编号是系统保留名称：{shown}")
        if key is not None and kind in (KIND_RULES_SCENARIO, KIND_RULES_SOURCE, KIND_RULES_MATERIAL, KIND_RULES_MATERIAL_META) and is_reserved_name(key):
            raise ArchiveFormatError(f"存档里的剧本编号是系统保留名称：{shown}")
        if kind == KIND_BOT_FILE:
            check_bot_relative(name.split("/", 2)[2], name)
        return kind, key
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
    """What the archive holds, counted.  Format 1 counts ``roster_seats``; format 2 counts ``bots``, whether there is a ``contract`` and the
    number of ``rules`` (scenarios).  The fields of the other format stay ``None`` and are left out of ``manifest.json``."""

    model_config = ConfigDict(extra="forbid")

    games: int = Field(ge=0)
    providers: int = Field(ge=0)
    roster_seats: int | None = Field(default=None, ge=0)  # format 1
    bots: int | None = Field(default=None, ge=0)  # format 2
    contract: bool | None = None  # format 2
    rules: int | None = Field(default=None, ge=0)  # format 2
    has_secrets: bool
    has_logs: bool

    def to_manifest(self) -> dict[str, Any]:
        """The ``contents`` object of ``manifest.json``: only the fields this format has."""
        return {key: value for key, value in self.model_dump().items() if value is not None}


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

    @model_validator(mode="after")
    def _contents_fit_the_version(self) -> "Manifest":
        contents = self.contents
        if self.format_version == LEGACY_VERSION:
            if contents.roster_seats is None or any(item is not None for item in (contents.bots, contents.contract, contents.rules)):
                raise ValueError("contents of format 1: roster_seats, and none of bots / contract / rules")
        elif contents.roster_seats is not None or any(item is None for item in (contents.bots, contents.contract, contents.rules)):
            raise ValueError("contents of format 2: bots, contract and rules, and no roster_seats")
        return self


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
    format_version: int = FORMAT_VERSION,
) -> Manifest:
    ordered = sorted(files, key=lambda entry: entry.path)
    return Manifest(
        format=FORMAT_NAME,
        format_version=format_version,
        app_version=app_version,
        archive_id=archive_id,
        created_at=created_at,
        contents=contents,
        files=ordered,
        files_sha256=files_digest(ordered),
    )


def manifest_bytes(manifest: Manifest) -> bytes:
    data = manifest.model_dump()
    data["contents"] = manifest.contents.to_manifest()
    return (json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


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
    if version not in SUPPORTED_VERSIONS:
        raise ArchiveFormatError("存档的格式版本无效。")
    try:
        manifest = Manifest.model_validate(data)
    except ValidationError as exc:
        where = ", ".join(sorted({".".join(str(p) for p in err["loc"]) or "manifest" for err in exc.errors()[:5]}))
        raise ArchiveFormatError(f"manifest.json 的内容不符合格式（{safe_text(where, 120)}）。") from None
    seen: set[str] = set()
    seen_lower: set[str] = set()
    kinds: list[tuple[str, str | None, str]] = []
    for entry in manifest.files:
        kind, key = classify(entry.path, version=manifest.format_version)
        if kind == KIND_MANIFEST:
            raise ArchiveFormatError("清单不应该列出 manifest.json 自己。")
        if entry.path in seen:
            raise ArchiveFormatError(f"清单里的文件重复：{safe_text(entry.path)}")
        if entry.path.lower() in seen_lower:
            raise ArchiveFormatError(f"清单里有只差大小写的路径：{safe_text(entry.path)}")
        seen.add(entry.path)
        seen_lower.add(entry.path.lower())
        kinds.append((kind, key, entry.path))
    if manifest.files_sha256 != files_digest(manifest.files):
        raise ArchiveFormatError("清单的文件列表校验值（files_sha256）与列表不符：存档可能已被改动。")
    contents = manifest.contents
    has_secrets = SECRETS_NAME in seen
    has_logs = any(kind in LOG_KINDS for kind, _key, _path in kinds)
    if contents.has_secrets != has_secrets or contents.has_logs != has_logs:
        raise ArchiveFormatError("清单的内容统计（是否含 API Key / 日志）与文件列表不符。")
    if manifest.format_version >= 2:
        bots = {key for kind, key, path in kinds if kind == KIND_BOT_FILE and path.endswith("/bot.json")}
        scenarios = {key for kind, key, _path in kinds if kind == KIND_RULES_SCENARIO}
        has_contract = any(kind == KIND_CONTRACT for kind, _key, _path in kinds)
        if contents.bots != len(bots) or contents.rules != len(scenarios) or contents.contract != has_contract:
            raise ArchiveFormatError("清单的内容统计（bot 数、剧本数、是否含契约）与文件列表不符。")
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
