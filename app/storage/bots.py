"""BotStore: the bots at the table (v0.14; contract §13.2; decisions H1-H9, H22, H23, H30, H37, H41, H44, H45).

A *bot* is a first-class thing: a name, an avatar, layered memory and an initialization record, kept in its own directory
``<data>/bots/<bot_id>/`` (the id is ``b-<8 hex>``, used only in paths; the user never sees it).  A *seat* is only where a bot
sits in this game: the line-up ``<data>/bots/lineup.json`` is the ordered list of the twelve bot ids, the index is the seat.
Memory is bound to the **name** (H2): renaming a bot that already has learned something makes it a new identity - its L0 is
kept, everything else is cleared, it gets a new id (so the line-up and the default model binding move with it).

The formats of the files are in :mod:`app.storage.botfiles`; this module adds the rest:

* the **line-up**, the memory switch and the moderator's avatar (``lineup.json``);
* ``bot.json``: ``{"format", "id", "name", "avatar", "created_at", "origin", "init"}``.  On disk ``init.status`` is only
  ``uninitialized`` / ``ready`` / ``failed``; what the outside sees is *derived* (:meth:`BotStore.summary`): ``initializing``
  while a job runs (the caller says so), ``stale`` when a ``ready`` record's hashes no longer match the L0 / contract /
  scenario / materials (and which ones), the rest as stored;
* **freezing** (H23, H57): a bot is frozen once it has initialization output (``ready``, or any of notes / digest / playbook /
  questions not empty).  Renaming or changing the avatar of a frozen bot needs ``confirm_reset=True`` and starts a new identity;
* the **L0 proposals** a review makes (``staging/<pid>.json``): accepted = written into L0 (the hash change makes the bot
  ``stale`` by itself), rejected = deleted;
* ``problems`` (the start check, H25), ``summary`` / ``detail`` (``BotSummary`` / ``BotDetail`` of contract §13.7, without what
  only the orchestrator knows: the seat's binding, the running job);
* installing a whole bot directory (``.arenabot`` import, "import from the default library"), the default library
  (``<program>/bots/``, H44), the migration of a v0.13 roster (:meth:`BotStore.import_legacy_roster`, H45).

Every change is validated before anything is written and every file is written atomically; nothing is cut to fit and no
history is kept (C9).  The store is safe to use from several threads (one re-entrant lock).
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from app.bots.types import (
    ARCH_NOTES_CHARS,
    BOT_ID_RE,
    CONTRACT_MUST_READ_CHARS,
    CORE_EXTRA_CHARS,
    CORE_EXTRA_MAX,
    CORE_LIMITS,
    CORE_SECTIONS,
    DIGEST_CHARS,
    IMPRESSION_CHARS,
    INIT_FAILED,
    INIT_HASHES,
    INIT_READY,
    INIT_RUNNING,
    INIT_STALE,
    INIT_TIMEOUT_S,
    INIT_UNINITIALIZED,
    JOT_CHARS,
    JOT_MAX,
    MEMORY_PARTS,
    PEOPLE_MAX,
    PLAYBOOK_ENTRY_CHARS,
    PLAYBOOK_MAX,
    QUESTIONS_CHARS,
    REVIEW_TIMEOUT_S,
    BotMind,
    CoreFiles,
    PlaybookEntry,
    RulesMemory,
    SourceHashes,
    WriteResult,
    core_hash,
)
from app.storage import botfiles
from app.storage._atomic import ensure_private_directory
from app.storage.botfiles import BotFileError
from app.storage.botfiles import file_sha256  # noqa: F401  (re-exported: the API of the bots uses it)
from app.storage.errors import StorageError
from app.storage.paths import get_base_dir, get_data_dir
from app.storage.rules import default_scenario_id, default_scenario_path  # noqa: F401  (re-exported)
from app.storage.roster import (
    DEFAULT_NAMES,
    MAX_NAME_CHARS,
    RosterError,
    clean_name,
    default_name,
    parse_legacy_persona,
    read_legacy_roster,
    seat_id,
)

logger = logging.getLogger(__name__)

BOTS_DIRNAME = "bots"
LIBRARY_DIRNAME = "bots"  # the default library: ``<program dir>/bots/<name>/``
LINEUP_FILE = "lineup.json"
BOT_FILE = "bot.json"
LINEUP_FORMAT = 1
BOT_FORMAT = 1
ORIGINS = ("default", "user", "import", "migrated")
MAX_STAGING = 30  # pending L0 proposals of one bot
PROPOSAL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MODERATOR_ID = "moderator"
MODERATOR_NAME = "主持人"
RULE_FILES = ("digest.md", "playbook.md", "questions.md")
TEMP_PREFIXES = (".install-", ".old-", ".new-", ".trash-")

#: What a confirmed reset clears (H23); ``NeedsConfirm.will_clear`` and the confirmation text.
WILL_CLEAR = ("初始化结果", "操作笔记", "规则要点", "打法", "待解问题", "印象", "战绩")
RESET_CONFIRM_TEXT = (
    "修改名字或头像会清空这个玩家的初始化结果和全部记忆（操作笔记、规则要点、打法、待解问题、印象、战绩），"
    "底层设定保留。修改后需要重新初始化才能开局。是否确认？"
)
#: The names of the sources an initialization depends on (``init.stale``), as the start check says them.
SOURCE_LABELS = {"core": "底层设定", "contract": "官方契约", "scenario": "剧本", "materials": "学习材料"}


# ======================================================================================================================
# errors
# ======================================================================================================================
class BotError(ValueError):
    """A bot request that cannot be done (the message is Chinese and safe to show).  ``status`` / ``code`` are what the HTTP
    layer answers with (docs/v0.12-contracts.md §13.7)."""

    status = 400
    code = ""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code


class BotNotFound(BotError):
    status = 404
    code = "not_found"


class LineupError(BotError):
    status = 422
    code = "bad_lineup"


class NameTaken(BotError):
    """The name is used by another bot (any case)."""

    status = 409
    code = "name_taken"


class NeedsConfirm(BotError):
    """The bot is frozen: changing its name or avatar clears its initialization and memory and needs ``confirm_reset``."""

    status = 409
    code = "needs_confirm"

    def __init__(self, message: str = RESET_CONFIRM_TEXT, *, will_clear: Sequence[str] = WILL_CLEAR) -> None:
        super().__init__(message)
        self.will_clear = list(will_clear)


class AvatarUnavailable(BotError):
    """The avatar parts library cannot be used (missing, broken)."""


# ======================================================================================================================
# helpers that need no store
# ======================================================================================================================
def _unix(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value > 0 else None


def _text_or_none(value: Any) -> str | None:
    text = str(value).strip() if isinstance(value, str) else ""
    return text or None


def _one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def read_lineup_ids(data_dir: Path) -> list[str]:
    """The line-up of a data directory (bot ids, seat 1 first); ``[]`` when there is none.  A plain read that creates nothing:
    :mod:`app.storage.settings` uses it to translate seats into bots."""
    try:
        raw = json.loads(botfiles.read_text(Path(data_dir) / BOTS_DIRNAME / LINEUP_FILE) or "null")
    except (ValueError, StorageError):
        return []
    order = raw.get("order") if isinstance(raw, dict) else None
    if not isinstance(order, list):
        return []
    out: list[str] = []
    for item in order:
        if isinstance(item, str) and BOT_ID_RE.match(item) and item not in out:
            out.append(item)
    return out


def default_library_dir() -> Path:
    """The default bot library (``bots/`` of the program directory, H44).  Tests point ``BASE_DIR`` at a temporary program
    directory that may not have one: then the library next to the ``app`` package is used."""
    candidates = (get_base_dir() / LIBRARY_DIRNAME, Path(__file__).resolve().parents[2] / LIBRARY_DIRNAME)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def limits_payload() -> dict[str, Any]:
    """``BotsPayload.limits``: the numbers the UI counts against (all from :mod:`app.bots.types`)."""
    return {
        "name_chars": MAX_NAME_CHARS,
        "core_limits": dict(CORE_LIMITS),
        "core_extra_max": CORE_EXTRA_MAX,
        "core_extra_chars": CORE_EXTRA_CHARS,
        "extra_title_chars": botfiles.EXTRA_TITLE_CHARS,
        "arch_notes_chars": ARCH_NOTES_CHARS,
        "digest_chars": DIGEST_CHARS,
        "questions_chars": QUESTIONS_CHARS,
        "playbook_max": PLAYBOOK_MAX,
        "playbook_entry_chars": PLAYBOOK_ENTRY_CHARS,
        "people_max": PEOPLE_MAX,
        "impression_chars": IMPRESSION_CHARS,
        "contract_must_read_chars": CONTRACT_MUST_READ_CHARS,
        "init_timeout_s": INIT_TIMEOUT_S,
        "review_timeout_s": REVIEW_TIMEOUT_S,
        "jot_max": JOT_MAX,
        "jot_chars": JOT_CHARS,
        "memory_parts": list(MEMORY_PARTS),
        "part_labels": dict(botfiles.PART_LABELS),
    }


# ---- avatars -------------------------------------------------------------------------------------------------------
def _avatars_module() -> Any:
    try:
        from app import avatars
    except ImportError:  # (the pixel-avatar library is optional: the front end falls back to a plain disc)
        return None
    return avatars


def avatar_code(description: Any) -> str | None:
    """The compact code of an avatar description (contract §12.6), ``None`` when there is none / it cannot be encoded."""
    avatars = _avatars_module()
    if avatars is None or not isinstance(description, dict) or not description:
        return None
    try:
        return avatars.encode_description(description)
    except ValueError:
        return None


def validate_avatar(description: Any) -> dict[str, Any]:
    """The avatar description as it is stored; :class:`BotError` (Chinese) when it is not valid, :class:`AvatarUnavailable`
    when the parts library cannot be read."""
    if not isinstance(description, dict) or not description:
        raise BotError("头像描述无效。")
    avatars = _avatars_module()
    if avatars is None:
        raise AvatarUnavailable("头像素材库不可用。")
    try:
        return avatars.validate_description(description)
    except FileNotFoundError:
        raise AvatarUnavailable("头像素材库不可用，请先运行 tools/avatar_lab.py 生成。") from None
    except ValueError as exc:
        raise BotError(str(exc)) from None


def pick_avatars(count: int, taken: Sequence[dict[str, Any]] = ()) -> list[dict[str, Any] | None]:
    """``count`` new avatar descriptions that look clearly different from each other and from ``taken``; ``None`` for each when
    the parts library is not available."""
    if count <= 0:
        return []
    avatars = _avatars_module()
    if avatars is None:
        return [None] * count
    try:
        parts = avatars.load_parts()
        return list(avatars.pick_roster(parts, count, random.Random(), [a for a in taken if isinstance(a, dict)]))
    except (OSError, ValueError, RuntimeError):
        logger.warning("Could not pick avatars", exc_info=True)
        return [None] * count


# ---- init record ---------------------------------------------------------------------------------------------------
def empty_init() -> dict[str, Any]:
    return {"status": INIT_UNINITIALIZED, "at": None, "model": None, "duration_s": None, "error": None, "scenario_id": None, "hashes": None}


def normalize_init(raw: Any) -> dict[str, Any]:
    """``bot.json: init`` as it is read: only the three stored statuses; hashes only for a ``ready`` record."""
    data = raw if isinstance(raw, dict) else {}
    status = data.get("status")
    if status not in (INIT_UNINITIALIZED, INIT_READY, INIT_FAILED):
        status = INIT_UNINITIALIZED
    hashes = None
    if status == INIT_READY and isinstance(data.get("hashes"), dict):
        hashes = {key: str(data["hashes"].get(key) or "") for key in INIT_HASHES}
    duration = data.get("duration_s")
    return {
        "status": status,
        "at": _unix(data.get("at")),
        "model": _text_or_none(data.get("model")),
        "duration_s": float(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None,
        "error": _text_or_none(data.get("error")),
        "scenario_id": _text_or_none(data.get("scenario_id")),
        "hashes": hashes,
    }


def derive_init(init: dict[str, Any], current_core_hash: str, sources: SourceHashes, running: bool = False) -> dict[str, Any]:
    """The status the outside sees (contract §13.2): ``initializing`` when ``running``; ``stale`` when a ``ready`` record's
    hashes do not match the current L0 / contract / scenario / materials (``stale`` lists the sources that differ, in
    ``INIT_HASHES`` order); the stored status otherwise."""
    status = init["status"]
    stale: list[str] = []
    if status == INIT_READY:
        stored = init.get("hashes")
        current = {"core": current_core_hash, "contract": sources.contract, "scenario": sources.scenario, "materials": sources.materials}
        for key in INIT_HASHES:
            if stored is None or str(stored.get(key, "")) != str(current[key]):
                stale.append(key)
        wanted = sources.scenario_id
        if wanted and init.get("scenario_id") and init["scenario_id"] != wanted and "scenario" not in stale:
            stale.append("scenario")
            stale.sort(key=INIT_HASHES.index)
        if stale:
            status = INIT_STALE
    if running:
        status = INIT_RUNNING
    return {
        "status": status,
        "at": init.get("at"),
        "model": init.get("model"),
        "duration_s": init.get("duration_s"),
        "error": init.get("error") if init["status"] == INIT_FAILED else None,
        "stale": stale,
    }


# ---- the files of a bot directory ----------------------------------------------------------------------------------
def iter_bot_files(root: Path) -> list[tuple[str, Path]]:
    """``(relative posix path, path)`` of every file that belongs to a bot in ``root`` (whitelist: ``bot.json``, ``core/``,
    ``arch/notes.md``, ``rules/<id>/{digest,playbook,questions}.md``, ``people.md``, ``record.json``, ``staging/<pid>.json``);
    symbolic links and unknown files are left out.  What the ``.arenabot`` export packs."""
    root = Path(root)
    out: list[tuple[str, Path]] = []

    def add(rel: str) -> None:
        path = root / rel
        try:
            if path.is_file() and not path.is_symlink():
                out.append((rel, path))
        except OSError:
            return

    add(BOT_FILE)
    for filename in botfiles.CORE_FILE_NAMES.values():
        add(f"core/{filename}")
    for slug, _path in botfiles.extra_files(root / "core"):
        add(f"core/{botfiles.EXTRA_PREFIX}{slug}{botfiles.EXTRA_SUFFIX}")
    add("arch/notes.md")
    rules = root / "rules"
    try:
        scenarios = sorted(p.name for p in rules.iterdir() if p.is_dir() and not p.is_symlink()) if rules.is_dir() else []
    except OSError:
        scenarios = []
    for sid in scenarios:
        if botfiles.SCENARIO_ID_RE.match(sid):
            for filename in RULE_FILES:
                add(f"rules/{sid}/{filename}")
    add("people.md")
    add(botfiles.RECORD_FILE)
    staging = root / "staging"
    try:
        proposals = sorted(p.name for p in staging.glob("*.json")) if staging.is_dir() else []
    except OSError:
        proposals = []
    for filename in proposals:
        if PROPOSAL_ID_RE.match(filename[: -len(".json")]):
            add(f"staging/{filename}")
    return out


def _rmtree(path: Path) -> bool:
    """Delete a directory tree, trying a few times (Windows: a file that is just being scanned).  ``True`` when it is gone."""
    for attempt in range(5):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(0.05 * (attempt + 1))
    logger.warning("Could not delete %s", path)
    return False


def _remove_dir(path: Path) -> None:
    """Delete a directory that must not stay around as a bot: when it cannot be deleted it is renamed so that it is no longer
    one (the next start deletes it)."""
    if _rmtree(path):
        return
    try:
        path.rename(path.with_name(f"{TEMP_PREFIXES[3]}{path.name}-{secrets.token_hex(3)}"))
    except OSError:
        logger.warning("Could not rename %s", path)


def _swap_dir(target: Path, fresh: Path) -> None:
    """Replace the directory ``target`` by ``fresh`` (renames; the old one is put back when the second rename fails)."""
    old: Path | None = None
    try:
        if target.exists():
            old = target.with_name(f"{TEMP_PREFIXES[1]}{target.name}-{secrets.token_hex(3)}")
            for attempt in range(5):
                try:
                    target.rename(old)
                    break
                except OSError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        fresh.rename(target)
    except OSError as exc:
        if old is not None and old.exists() and not target.exists():
            try:
                old.rename(target)
            except OSError:
                logger.error("Could not put %s back", target)
        raise StorageError("Unable to replace the bot directory") from exc
    if old is not None:
        _remove_dir(old)


# ======================================================================================================================
# the default library
# ======================================================================================================================
@dataclass(frozen=True)
class LibraryBot:
    """One bot of the default library (``<program>/bots/<folder>/``)."""

    folder: str
    path: Path
    name: str
    avatar: dict[str, Any] | None
    identity: str  # the first line of the identity, as a short description (``BotsPayload.defaults``)


@dataclass(frozen=True)
class _Inspected:
    """A bot directory that passed the checks of an import: what it says about the bot and which of its files are taken."""

    name: str
    avatar: dict[str, Any] | None
    created_at: int | None
    init: dict[str, Any]
    files: tuple[tuple[str, Path], ...]


def _short(text: str, limit: int = 60) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if len(line) <= limit:
        return line
    cut = line.find("。")
    return line[: cut + 1] if 0 < cut < limit else line[: limit - 1].rstrip() + "…"


# ---- migrating a v0.13 roster (H45) --------------------------------------------------------------------------------
LEGACY_EXPERIENCE_SOURCE = "迁移自 v0.13 的经验"
_LEGACY_BAD_NAME_CHARS = re.compile(r"[\x00-\x1f\x7f<>\"'`\\]")


def _fit_text(text: str, limit: int) -> str:
    """At most ``limit`` characters; what had to be cut ends with an ellipsis (an old text that is too long must not stop a
    migration: the user can fix it in the editor)."""
    value = text.strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _unheading(line: str) -> str:
    """A line that would be read as a section heading of the merged L0 file loses its ``#`` marks."""
    return re.sub(r"^\s*#+[ \t\u3000]*", "", line).strip()


def legacy_persona_to_core(persona: str) -> CoreFiles:
    """A v0.13 persona -> L0, by its headings: ``【定位】`` -> identity; ``【说话风格】`` and ``【口头禅】`` -> voice; ``【思考方式】``,
    ``【决策偏好】`` and ``【压力下】`` -> mind (the last two lines are labelled with their heading so they keep their meaning).
    A persona without any heading (or text in front of the first one) is who the bot is: it goes to identity.  An empty persona
    gives no L0 at all.  Every section is cut to its limit (with an ellipsis) rather than refused."""
    sections = parse_legacy_persona(persona)
    if not any(sections.values()):
        return CoreFiles()

    def lines(*pairs: tuple[str, str]) -> str:
        out: list[str] = []
        for heading, label in pairs:
            for item in sections.get(heading, []):
                text = _unheading(item)
                if text:
                    out.append(f"{label}{text}")
        return "\n".join(out)

    identity = lines(("定位", ""), ("", ""))
    voice = lines(("说话风格", ""), ("口头禅", "口头禅："))
    mind = lines(("思考方式", ""), ("决策偏好", "决策偏好："), ("压力下", "压力下："))
    return CoreFiles(
        identity=_fit_text(identity, CORE_LIMITS["identity"]),
        voice=_fit_text(voice, CORE_LIMITS["voice"]),
        mind=_fit_text(mind, CORE_LIMITS["mind"]),
    )


def _legacy_name(value: str, seat: int) -> str:
    """A v0.13 name as a name of today: a colon becomes a space (it would cut an impression short), characters that are not
    allowed are dropped, at most 20 characters; a name that is nothing after that is the default name of the seat."""
    text = re.sub(r"[:：]", " ", str(value or ""))
    text = _LEGACY_BAD_NAME_CHARS.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()[:MAX_NAME_CHARS].strip()
    try:
        return clean_name(text)
    except RosterError:
        return default_name(seat)


def legacy_bot_id(seat: int, name: str) -> str:
    """The id a migrated bot gets: derived from its seat and name, so migrating the same roster again gives the same bots."""
    return "b-" + hashlib.sha256(f"legacy:{seat}:{name}".encode("utf-8")).hexdigest()[:8]


# ======================================================================================================================
# the store
# ======================================================================================================================
class BotStore:
    """The bots of one data directory.  ``seats`` is the number of seats (12; fewer only in tests); ``library_dir`` the default
    library (``None``: :func:`default_library_dir`); ``clock`` returns the time in seconds."""

    def __init__(
        self,
        data_dir: Path,
        *,
        seats: int = 12,
        library_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._data_dir = Path(data_dir).resolve()
        self._root = self._data_dir / BOTS_DIRNAME
        self._seats = max(1, int(seats))
        self._library_dir = Path(library_dir) if library_dir is not None else None
        self._clock = clock
        self._lock = threading.RLock()
        self._bootstrapped = False

    # ---------------------------------------------------------------------------------------------------- paths
    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def root(self) -> Path:
        return self._root

    @property
    def seats(self) -> int:
        return self._seats

    def _now(self) -> int:
        return int(self._clock())

    def bot_dir(self, bot_id: str) -> Path:
        """``<data>/bots/<bot_id>`` (the directory need not exist).  An id that is not ``b-<8 hex>`` is refused: ids are the only
        thing that ever becomes part of a path."""
        if not isinstance(bot_id, str) or not BOT_ID_RE.match(bot_id):
            raise BotNotFound(f"没有这个玩家：{str(bot_id)[:24]}")
        return self._root / bot_id

    def _exists(self, bot_id: str) -> bool:
        """A bot is a directory with a ``bot.json`` that can be read (a damaged one is no bot: the line-up repair replaces it)."""
        return self._read_meta(bot_id) is not None

    def _new_id(self) -> str:
        for _ in range(200):
            candidate = "b-" + secrets.token_hex(4)
            if not (self._root / candidate).exists():
                return candidate
        raise StorageError("Unable to allocate a bot id")

    # ---------------------------------------------------------------------------------------------------- bot.json
    def _read_meta(self, bot_id: str) -> dict[str, Any] | None:
        try:
            path = self.bot_dir(bot_id) / BOT_FILE
        except BotError:
            return None
        text = botfiles.read_text(path)
        if not text.strip():
            return None
        try:
            raw = json.loads(text)
        except ValueError:
            return None
        if not isinstance(raw, dict):
            return None
        return self._normalize_meta(raw, bot_id)

    @staticmethod
    def _normalize_meta(raw: dict[str, Any], bot_id: str) -> dict[str, Any]:
        name = str(raw.get("name") or "").strip()
        try:
            name = clean_name(name, allow_colon=True)  # (a stored name is shown as it is)
        except RosterError:
            name = name[:MAX_NAME_CHARS] or bot_id
        avatar = raw.get("avatar") if isinstance(raw.get("avatar"), dict) and raw.get("avatar") else None
        origin = raw.get("origin") if raw.get("origin") in ORIGINS else "user"
        return {
            "id": bot_id,
            "name": name,
            "avatar": avatar,
            "created_at": _unix(raw.get("created_at")) or 0,
            "origin": origin,
            "init": normalize_init(raw.get("init")),
        }

    def _require_meta(self, bot_id: str) -> dict[str, Any]:
        meta = self._read_meta(bot_id)
        if meta is None:
            raise BotNotFound(f"没有这个玩家：{str(bot_id)[:24]}")
        return meta

    @staticmethod
    def _meta_bytes(bot_id: str, meta: dict[str, Any]) -> bytes:
        init = normalize_init(meta.get("init"))
        data = {
            "format": BOT_FORMAT,
            "id": bot_id,
            "name": meta["name"],
            "avatar": meta.get("avatar"),
            "created_at": int(meta.get("created_at") or 0),
            "origin": meta.get("origin") if meta.get("origin") in ORIGINS else "user",
            "init": init,
        }
        return (json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode("utf-8")

    def _write_meta(self, bot_id: str, meta: dict[str, Any]) -> None:
        botfiles.write_bytes(self.bot_dir(bot_id) / BOT_FILE, self._meta_bytes(bot_id, meta))

    def init_record(self, bot_id: str) -> dict[str, Any]:
        """``bot.json: init`` as it is stored (status ``uninitialized`` / ``ready`` / ``failed``, ``at``, ``model``,
        ``duration_s``, ``error``, ``scenario_id``, ``hashes``)."""
        with self._lock:
            return dict(self._require_meta(bot_id)["init"])

    # ---------------------------------------------------------------------------------------------------- line-up
    def _read_lineup_raw(self) -> dict[str, Any] | None:
        try:
            raw = json.loads(botfiles.read_text(self._root / LINEUP_FILE) or "null")
        except ValueError:
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("order"), list):
            return None
        return raw

    def _write_lineup_raw(self, raw: dict[str, Any]) -> None:
        data = dict(raw)
        data["format"] = LINEUP_FORMAT
        data["order"] = [str(i) for i in data.get("order", [])]
        data["memory_enabled"] = bool(data.get("memory_enabled", True))
        botfiles.write_bytes(self._root / LINEUP_FILE, (json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))

    def lineup(self) -> list[str]:
        """The bot ids by seat (seat 1 first): the ids of ``lineup.json`` that exist."""
        with self._lock:
            raw = self._read_lineup_raw()
            order: list[str] = []
            for item in (raw or {}).get("order", []):
                if isinstance(item, str) and item not in order and self._exists(item):
                    order.append(item)
            return order[: self._seats]

    def bot_ids(self) -> list[str]:
        """Every bot of the table, in seat order."""
        return self.lineup()

    def seat_of(self, bot_id: str) -> str | None:
        """The seat id (``N3``) of a bot in the line-up, ``None`` when it is not in it."""
        order = self.lineup()
        return seat_id(order.index(bot_id) + 1) if bot_id in order else None

    def set_lineup(self, order: Sequence[str]) -> list[str]:
        """Seat the bots in this order.  It must be a permutation of the bots there are (no more, no fewer, no repeats); the
        game being idle is the caller's check.  Names, avatars, memory and default model bindings follow the bots."""
        with self._lock:
            current = self.lineup()
            if not isinstance(order, (list, tuple)) or any(not isinstance(item, str) for item in order):
                raise LineupError("阵容必须是玩家 id 的列表。")
            if len(order) != len(current) or sorted(order) != sorted(current):
                raise LineupError(f"阵容必须是现在这 {len(current)} 个玩家的一个排列：不能多、不能少、不能重复。")
            raw = self._read_lineup_raw() or {}
            raw["order"] = list(order)
            self._write_lineup_raw(raw)
            return list(order)

    def memory_enabled(self) -> bool:
        """Whether a game loads the bots' learned memory (E24 / ``PUT /api/bots/memory-enabled``)."""
        with self._lock:
            return bool((self._read_lineup_raw() or {}).get("memory_enabled", True))

    def set_memory_enabled(self, enabled: bool) -> None:
        with self._lock:
            raw = self._read_lineup_raw() or {"order": self.lineup()}
            raw["memory_enabled"] = bool(enabled)
            self._write_lineup_raw(raw)

    # ---------------------------------------------------------------------------------------------------- moderator
    def moderator(self) -> dict[str, Any]:
        """``{"id": "moderator", "name": "主持人", "avatar": {…}|None}``: the moderator is not a bot (no memory, no name to
        change), it only has an avatar; it is kept in ``lineup.json`` so it travels with the archive."""
        with self._lock:
            row = (self._read_lineup_raw() or {}).get("moderator")
            avatar = row.get("avatar") if isinstance(row, dict) and isinstance(row.get("avatar"), dict) else None
            return {"id": MODERATOR_ID, "name": MODERATOR_NAME, "avatar": avatar or None}

    def _set_moderator_avatar(self, avatar: dict[str, Any] | None) -> None:
        raw = self._read_lineup_raw() or {"order": self.lineup()}
        raw["moderator"] = {"avatar": avatar}
        self._write_lineup_raw(raw)

    def set_moderator_avatar(self, avatar: Any) -> dict[str, Any]:
        """The moderator's avatar (a description; :class:`BotError` when it is not valid)."""
        checked = validate_avatar(avatar)
        with self._lock:
            self._set_moderator_avatar(checked)
            return self.moderator()

    def regenerate_moderator_avatar(self) -> dict[str, Any]:
        """A new avatar for the moderator, clearly different from every bot's."""
        with self._lock:
            taken = [m["avatar"] for m in self._all_meta() if m["avatar"]]
            current = self.moderator()["avatar"]
            if current:
                taken.append(current)
            (picked,) = pick_avatars(1, taken)
            if picked is None:
                raise AvatarUnavailable("头像素材库不可用，请先运行 tools/avatar_lab.py 生成。")
            self._set_moderator_avatar(picked)
            return self.moderator()

    # ---------------------------------------------------------------------------------------------------- names
    def _all_meta(self) -> list[dict[str, Any]]:
        out = []
        for bot_id in self.lineup():
            meta = self._read_meta(bot_id)
            if meta is not None:
                out.append(meta)
        return out

    def names(self) -> dict[str, str]:
        """``{bot id: name}`` in seat order."""
        with self._lock:
            return {m["id"]: m["name"] for m in self._all_meta()}

    def name_of(self, bot_id: str) -> str:
        with self._lock:
            return self._require_meta(bot_id)["name"]

    def bot_id_of(self, name: str) -> str | None:
        """The bot with this name (any case), ``None`` when nobody has it."""
        key = str(name or "").strip().casefold()
        if not key:
            return None
        with self._lock:
            for meta in self._all_meta():
                if meta["name"].casefold() == key:
                    return meta["id"]
        return None

    def validate_name(self, name: Any, *, exclude: str | None = None) -> str:
        """The name as it will be stored (:func:`app.storage.roster.clean_name`: 1-20 characters, no colon, quote, backslash,
        angle bracket or control character); :class:`BotError` when it is not allowed, :class:`NameTaken` when another bot (not
        ``exclude``) has it, any case.  Writes nothing."""
        try:
            value = clean_name(name)
        except RosterError as exc:
            raise BotError(str(exc)) from None
        with self._lock:
            order = self.lineup()
            for meta in self._all_meta():
                if meta["id"] != exclude and meta["name"].casefold() == value.casefold():
                    seat = order.index(meta["id"]) + 1
                    raise NameTaken(f"名字「{value}」已被 {seat_id(seat)} 使用，名字不能重复。")
        return value

    # ---------------------------------------------------------------------------------------------------- freezing
    def is_frozen(self, bot_id: str) -> bool:
        """``True`` once the bot has initialization output: ``init.status == "ready"``, or its operating notes, digest, playbook
        or questions (any scenario) are not empty (H23, H57).  A frozen bot's name and avatar need a confirmed reset."""
        with self._lock:
            return self._frozen(bot_id, self._require_meta(bot_id))

    def _frozen(self, bot_id: str, meta: dict[str, Any]) -> bool:
        if meta["init"]["status"] == INIT_READY:
            return True
        bot_dir = self.bot_dir(bot_id)
        if botfiles.read_text(bot_dir / "arch" / "notes.md").strip():
            return True
        rules = bot_dir / "rules"
        try:
            scenarios = [p for p in rules.iterdir() if p.is_dir()] if rules.is_dir() else []
        except OSError:
            scenarios = []
        for folder in scenarios:
            for filename in RULE_FILES:
                if botfiles.read_text(folder / filename).strip():
                    return True
        return False

    def update_bot(self, bot_id: str, *, name: Any = None, avatar: Any = None, confirm_reset: bool = False) -> str:
        """Change the name and / or the avatar; returns the id of the bot afterwards.

        Everything is validated first.  A bot that has not been initialized is changed in place (the id stays).  A **frozen** bot
        (:meth:`is_frozen`) needs ``confirm_reset=True`` (else :class:`NeedsConfirm`, nothing changes); then it starts a new
        identity: only ``core/`` is kept, the bot gets a new id, the old directory is deleted (no copy is kept, C9), the line-up
        and the default model binding move to the new id, the pending proposals are dropped; the status is ``uninitialized``.
        The game being idle is the caller's check."""
        with self._lock:
            meta = self._require_meta(bot_id)
            new_name = meta["name"]
            renamed = False
            if name is not None:
                new_name = self.validate_name(name, exclude=bot_id)
                renamed = new_name != meta["name"]
            new_avatar = meta["avatar"]
            re_avatar = False
            if avatar is not None:
                new_avatar = validate_avatar(avatar)
                re_avatar = new_avatar != meta["avatar"]
            if not renamed and not re_avatar:
                return bot_id
            if not self._frozen(bot_id, meta):
                meta["name"], meta["avatar"] = new_name, new_avatar
                self._write_meta(bot_id, meta)
                return bot_id
            if not confirm_reset:
                raise NeedsConfirm()
            return self._reset_identity(bot_id, meta, new_name, new_avatar)

    def _reset_identity(self, old_id: str, meta: dict[str, Any], new_name: str, new_avatar: dict[str, Any] | None) -> str:
        new_id = self._new_id()
        fresh = self.bot_dir(new_id)
        old_dir = self.bot_dir(old_id)
        try:
            for rel, path in iter_bot_files(old_dir):
                if rel.startswith("core/"):  # (L0 only: the rest is what the identity learned)
                    botfiles.write_bytes(fresh / rel, path.read_bytes())
            ensure_private_directory(fresh)
            fresh_meta = {
                "name": new_name,
                "avatar": new_avatar,
                "created_at": self._now(),
                "origin": meta["origin"],
                "init": empty_init(),
            }
            botfiles.write_bytes(fresh / BOT_FILE, self._meta_bytes(new_id, fresh_meta))  # (last: the bot exists from here on)
            raw = self._read_lineup_raw() or {"order": []}
            raw["order"] = [new_id if item == old_id else item for item in raw.get("order", [])]
            self._write_lineup_raw(raw)
        except BaseException:
            _rmtree(fresh)
            raise
        try:
            self._settings().rename_binding_key(old_id, new_id)
        except Exception:  # noqa: BLE001 - the binding is a convenience; the reset must not fail half way
            logger.warning("Could not move the default model binding of %s", old_id, exc_info=True)
        _remove_dir(old_dir)
        return new_id

    def regenerate_avatar(self, bot_id: str, *, confirm_reset: bool = False) -> str:
        """A new random avatar for one bot (:meth:`update_bot` rules apply: a frozen bot needs ``confirm_reset``); returns its id."""
        with self._lock:
            meta = self._require_meta(bot_id)
            taken = [m["avatar"] for m in self._all_meta() if m["avatar"]]
            moderator = self.moderator()["avatar"]
            if moderator:
                taken.append(moderator)
            (picked,) = pick_avatars(1, taken)
            if picked is None:
                raise AvatarUnavailable("头像素材库不可用，请先运行 tools/avatar_lab.py 生成。")
            return self.update_bot(meta["id"], avatar=picked, confirm_reset=confirm_reset)

    def regenerate_avatars(self) -> tuple[list[str], list[str]]:
        """New avatars for every bot that is not frozen, picked together so that no two look alike (and none like a frozen bot's
        or the moderator's).  Returns ``(changed ids, skipped ids)``; the skipped are the frozen ones."""
        with self._lock:
            ids = self.lineup()
            metas = {i: self._require_meta(i) for i in ids}
            skipped = [i for i in ids if self._frozen(i, metas[i])]
            targets = [i for i in ids if i not in skipped]
            taken = [metas[i]["avatar"] for i in skipped if metas[i]["avatar"]]
            moderator = self.moderator()["avatar"]
            if moderator:
                taken.append(moderator)
            picked = pick_avatars(len(targets), taken)
            if targets and any(p is None for p in picked):
                raise AvatarUnavailable("头像素材库不可用，请先运行 tools/avatar_lab.py 生成。")
            for bot_id, avatar in zip(targets, picked):
                metas[bot_id]["avatar"] = avatar
                self._write_meta(bot_id, metas[bot_id])
            return targets, skipped

    def ensure_avatars(self) -> bool:
        """Give every bot - and the moderator - that has no avatar one, clearly different from the others.  ``True`` when
        something was written."""
        with self._lock:
            metas = self._all_meta()
            taken = [m["avatar"] for m in metas if m["avatar"]]
            moderator = self.moderator()["avatar"]
            if moderator:
                taken.append(moderator)
            changed = False
            for meta in metas:
                if not meta["avatar"]:
                    (picked,) = pick_avatars(1, taken)
                    if picked is None:
                        return changed
                    meta["avatar"] = picked
                    taken.append(picked)
                    self._write_meta(meta["id"], meta)
                    changed = True
            if not moderator:
                (picked,) = pick_avatars(1, taken)
                if picked is not None:
                    self._set_moderator_avatar(picked)
                    changed = True
            return changed

    # ---------------------------------------------------------------------------------------------------- L0
    def core(self, bot_id: str) -> CoreFiles:
        with self._lock:
            self._require_meta(bot_id)
            return botfiles.read_core(self.bot_dir(bot_id) / "core")

    def core_on_disk(self, bot_id: str) -> botfiles.CoreOnDisk:
        """L0 with the slug of every extra section and the time of the newest file."""
        with self._lock:
            self._require_meta(bot_id)
            return botfiles.read_core_on_disk(self.bot_dir(bot_id) / "core")

    def core_hash_of(self, bot_id: str) -> str:
        """The hash of the bot's L0 as it is now (:func:`app.bots.types.core_hash`): a learning run takes it when it begins and
        hands it back to :meth:`commit_init` (``core_hash_at_start``), so an L0 edited meanwhile makes the result ``stale``."""
        return core_hash(self.core(bot_id))

    def core_markdown(self, bot_id: str) -> str:
        """The merged L0 file of this bot (``<名字>.core.md``)."""
        with self._lock:
            meta = self._require_meta(bot_id)
            return botfiles.render_core_markdown(botfiles.read_core(self.bot_dir(bot_id) / "core"), meta["name"])

    def set_core_markdown(self, bot_id: str, markdown: Any) -> botfiles.ParsedCore:
        """Replace L0 by what a merged file says (:func:`botfiles.parse_core_markdown`: all of it is checked first,
        :class:`BotFileError` names every fault and nothing is written).  The name in the file is only a hint: the result
        carries it so that the caller can say it was not applied.  The initialization becomes ``stale`` by itself (the L0 hash
        changed)."""
        parsed = botfiles.parse_core_markdown(markdown)
        with self._lock:
            self._require_meta(bot_id)
            botfiles.write_core(self.bot_dir(bot_id) / "core", parsed.core)
        return parsed

    def set_core(self, bot_id: str, core: CoreFiles) -> None:
        """Replace L0 (not necessarily complete; limits are checked: :class:`BotFileError`)."""
        with self._lock:
            self._require_meta(bot_id)
            botfiles.write_core(self.bot_dir(bot_id) / "core", core)

    def clear_core(self, bot_ids: Iterable[str] | None = None) -> list[str]:
        """Empty the L0 of these bots (``None``: all), ``core:clear`` (H37); the initialization is not touched - it becomes
        ``stale`` and the start check stops the game (``core_missing``).  Returns the ids."""
        with self._lock:
            ids = self._pick_ids(bot_ids)
            for bot_id in ids:
                botfiles.clear_core(self.bot_dir(bot_id) / "core")
            return ids

    def _pick_ids(self, bot_ids: Iterable[str] | None) -> list[str]:
        if bot_ids is None:
            return self.lineup()
        ids: list[str] = []
        for bot_id in bot_ids:
            self._require_meta(bot_id)
            if bot_id not in ids:
                ids.append(bot_id)
        return ids

    # ---------------------------------------------------------------------------------------------------- memory
    def read_part(self, bot_id: str, part: str, scenario_id: str = "") -> str:
        """The text of a part of the memory (``MEMORY_PARTS``) as it is stored: ``""`` when there is none; ``scenario_id`` is
        only used by ``digest`` / ``playbook`` / ``questions``."""
        with self._lock:
            self._require_meta(bot_id)
            try:
                return botfiles.read_part(self.bot_dir(bot_id), part, scenario_id)
            except BotFileError as exc:
                raise BotError(str(exc)) from None

    def write_part(
        self, bot_id: str, part: str, content: Any, scenario_id: str = "", *, known_tags: Iterable[str] | None = None
    ) -> WriteResult:
        """Validate and atomically write a part (the user's edit and a review's write are the same thing): the canonical text is
        stored, an empty ``content`` clears the part, a refused text changes nothing (``ok=False``, the message says which
        limit and how long it is).  It does not touch the initialization record (C9: edits take effect at once).
        ``known_tags`` (the tags of the scenario's ``situations``): a playbook tag outside it is written all the same, and the
        message says it will not be retrieved - the scenario may be replaced later, so it is no fault."""
        with self._lock:
            self._require_meta(bot_id)
            return botfiles.write_part(self.bot_dir(bot_id), part, content, scenario_id, known_tags=known_tags)

    def clear_part(self, bot_id: str, part: str, scenario_id: str = "") -> WriteResult:
        return self.write_part(bot_id, part, "", scenario_id)

    def clear_memory(self, bot_ids: Iterable[str] | None = None) -> list[str]:
        """Forget what these bots learned (``None``: all): the operating notes, every scenario's digest / playbook / questions,
        the impressions and the record; the initialization result goes too (status ``uninitialized``); L0 and pending proposals
        stay.  Returns the ids."""
        with self._lock:
            ids = self._pick_ids(bot_ids)
            for bot_id in ids:
                bot_dir = self.bot_dir(bot_id)
                for folder in ("arch", "rules"):
                    if (bot_dir / folder).exists():
                        _remove_dir(bot_dir / folder)
                botfiles.remove_file(bot_dir / "people.md")
                botfiles.remove_file(bot_dir / botfiles.RECORD_FILE)
                meta = self._require_meta(bot_id)
                meta["init"] = empty_init()
                self._write_meta(bot_id, meta)
            return ids

    def clear_all_parts(self, bot_id: str) -> None:
        """``DELETE /api/bots/{id}/memory`` without a part: everything this bot learned (see :meth:`clear_memory`)."""
        self.clear_memory([bot_id])

    def add_record(self, bot_id: str, role_label: str, won: bool | None) -> None:
        """Count a finished game in the record (``won=None``, an outcome nobody knows, leaves it alone)."""
        with self._lock:
            self._require_meta(bot_id)
            botfiles.add_record(self.bot_dir(bot_id), role_label, won)

    def load_mind(self, bot_id: str, scenario_id: str, *, memory: bool = True, others: Sequence[str] = ()) -> BotMind:
        """What the bot carries into a game (the snapshot of H11): L0, and - when ``memory`` - its notes, its digest / playbook /
        questions for ``scenario_id``, the record and its impressions of the people in ``others`` (their names; nobody else's
        impression is mounted, and none of itself).  Without ``memory`` it is L0 only."""
        with self._lock:
            meta = self._require_meta(bot_id)
            bot_dir = self.bot_dir(bot_id)
            core = botfiles.read_core(bot_dir / "core")
            if not memory:
                return BotMind(bot_id=bot_id, name=meta["name"], core=core, rules=RulesMemory(scenario_id=scenario_id or ""))
            sid = scenario_id or ""
            rules = RulesMemory(scenario_id=sid)
            if sid:
                try:
                    botfiles.check_scenario_id(sid)
                except BotFileError as exc:
                    raise BotError(str(exc)) from None
                rules = RulesMemory(
                    scenario_id=sid,
                    digest=botfiles.read_part(bot_dir, "digest", sid),
                    playbook=tuple(botfiles.parse_playbook(botfiles.read_part(bot_dir, "playbook", sid))),
                    questions=botfiles.read_part(bot_dir, "questions", sid),
                )
            stored = botfiles.parse_people(botfiles.read_part(bot_dir, "people"))
            by_folded = {name.casefold(): impression for name, impression in stored.items()}
            own = meta["name"].casefold()
            people: dict[str, str] = {}
            for other in others:
                key = str(other).strip().casefold()
                if key and key != own and key in by_folded and str(other).strip() not in people:
                    people[str(other).strip()] = by_folded[key]
            return BotMind(
                bot_id=bot_id,
                name=meta["name"],
                core=core,
                arch_notes=botfiles.read_part(bot_dir, "arch_notes"),
                rules=rules,
                people=people,
                record=botfiles.record_text(botfiles.read_record(bot_dir)),
            )

    # ---------------------------------------------------------------------------------------------------- init record
    def commit_init(
        self,
        bot_id: str,
        scenario_id: str,
        files: dict[str, str],
        *,
        model: str,
        duration_s: float,
        sources: SourceHashes,
        core_hash_at_start: str | None = None,
    ) -> None:
        """A finished initialization (H21, C16: all or nothing): every file is checked first (``arch_notes``, ``digest`` and
        ``playbook`` are required and must not be empty, ``questions`` is optional; a key that is left out keeps the file as it
        is), then written, then ``bot.json: init`` becomes ``ready`` with the hashes - L0 (``core_hash_at_start`` when given:
        the hash when the run began, so that an edit made meanwhile shows up as ``stale``; else the current one), and the
        contract / scenario / materials of ``sources``.  A refused set raises :class:`BotFileError` (every fault in
        ``problems``) and changes nothing."""
        with self._lock:
            meta = self._require_meta(bot_id)
            sid = botfiles.check_scenario_id(scenario_id)
            allowed = ("arch_notes", "digest", "playbook", "questions")
            problems: list[str] = []
            texts: dict[str, str] = {}
            refused: set[str] = set()
            for part, content in files.items():
                if part not in allowed:
                    problems.append(f"不认识的文件「{part}」，初始化只能写 " + "、".join(allowed) + "。")
                    continue
                try:
                    texts[part] = botfiles.normalize_part(part, content)[0]
                except BotFileError as exc:
                    refused.add(part)
                    problems.extend(exc.problems)
            for part in ("arch_notes", "digest", "playbook"):
                if part in refused or texts.get(part, "").strip():
                    continue
                if part in files:
                    problems.append(f"「{botfiles.PART_LABELS[part]}」不能为空。")
                else:
                    problems.append(f"缺少必需的文件：{botfiles.PART_LABELS[part]}（{part}）。")
            if problems:
                raise BotFileError(problems)
            bot_dir = self.bot_dir(bot_id)
            for part, text in texts.items():
                botfiles.write_text(bot_dir / botfiles.part_relpath(part, sid), text)
            core_now = core_hash_at_start if core_hash_at_start is not None else core_hash(botfiles.read_core(bot_dir / "core"))
            meta["init"] = {
                "status": INIT_READY,
                "at": self._now(),
                "model": _text_or_none(model),
                "duration_s": round(float(duration_s or 0.0), 1),
                "error": None,
                "scenario_id": sid,
                "hashes": {"core": core_now, "contract": sources.contract, "scenario": sources.scenario, "materials": sources.materials},
            }
            self._write_meta(bot_id, meta)

    def fail_init(self, bot_id: str, error: str, *, model: str = "", duration_s: float = 0.0) -> None:
        """An initialization that did not finish (timeout, a missing file, over a limit ...): the status becomes ``failed`` with
        the reason; the files of the memory stay exactly as they were."""
        with self._lock:
            meta = self._require_meta(bot_id)
            previous = meta["init"]
            meta["init"] = {
                "status": INIT_FAILED,
                "at": self._now(),
                "model": _text_or_none(model),
                "duration_s": round(float(duration_s or 0.0), 1),
                "error": _text_or_none(error) or "初始化没有完成。",
                "scenario_id": previous.get("scenario_id"),
                "hashes": None,
            }
            self._write_meta(bot_id, meta)

    # ---------------------------------------------------------------------------------------------------- L0 proposals
    def _staging_dir(self, bot_id: str) -> Path:
        return self.bot_dir(bot_id) / "staging"

    @staticmethod
    def _proposal_from(raw: Any, pid: str) -> dict[str, Any] | None:
        if not isinstance(raw, dict) or raw.get("id") != pid or not PROPOSAL_ID_RE.match(pid):
            return None
        try:
            botfiles.split_section(str(raw.get("section")))
        except BotFileError:
            return None
        after = raw.get("after")
        if not isinstance(after, str) or not after.strip():
            return None
        return {
            "id": pid,
            "section": str(raw["section"]).strip(),
            "before": str(raw.get("before") or ""),
            "after": after,
            "reason": str(raw.get("reason") or ""),
            "source": str(raw.get("source") or ""),
            "created_at": _unix(raw.get("created_at")) or 0,
        }

    def _read_proposal(self, bot_id: str, pid: str) -> dict[str, Any] | None:
        if not PROPOSAL_ID_RE.match(str(pid)):
            return None
        text = botfiles.read_text(self._staging_dir(bot_id) / f"{pid}.json")
        try:
            return self._proposal_from(json.loads(text), pid)
        except ValueError:
            return None

    def staging(self, bot_id: str) -> list[dict[str, Any]]:
        """The pending L0 proposals, oldest first: ``{"id", "section", "before", "after", "reason", "source", "created_at"}``."""
        with self._lock:
            self._require_meta(bot_id)
            folder = self._staging_dir(bot_id)
            try:
                names = sorted(p.stem for p in folder.glob("*.json")) if folder.is_dir() else []
            except OSError:
                names = []
            ranked: list[tuple[int, int, str, dict[str, Any]]] = []
            for pid in names:
                proposal = self._read_proposal(bot_id, pid)
                if proposal is None:
                    continue
                try:  # (proposals made in the same second keep the order they were made in: the file's own time breaks the tie)
                    stamp = (folder / f"{pid}.json").stat().st_mtime_ns
                except OSError:
                    stamp = 0
                ranked.append((proposal["created_at"], stamp, proposal["id"], proposal))
            ranked.sort(key=lambda item: item[:3])
            return [item[3] for item in ranked]

    def stage_core(self, bot_id: str, section: str, content: str, reason: str, source: str) -> str:
        """Put a proposal to change one L0 section on the list for the user to decide (a review may only propose, H34).
        ``section`` is ``identity`` / ``voice`` / ``mind`` / ``extra:<slug or title>`` (an extra that does not exist yet is
        created when the proposal is accepted).  The text is checked as it would be written (limits, at most three extras):
        :class:`BotFileError` when it cannot be.  Returns the id of the proposal."""
        with self._lock:
            self._require_meta(bot_id)
            kind, ident = botfiles.split_section(section)
            text = botfiles.checked_text(content, "提案内容").strip()
            if not text:
                raise BotFileError("提案内容不能为空。")
            disk = botfiles.read_core_on_disk(self.bot_dir(bot_id) / "core")
            candidate = botfiles.apply_section(disk, section, text)
            problems = botfiles.validate_core(candidate)
            if problems:
                raise BotFileError(problems)
            if len(self.staging(bot_id)) >= MAX_STAGING:
                raise BotFileError(f"待确认的提案已经有 {MAX_STAGING} 条，请先处理掉一些。")
            normalized = kind
            if kind == "extra":
                normalized = f"{botfiles.EXTRA_SECTION_PREFIX}{ident}"
                for index, (title, _body) in enumerate(disk.core.extras):
                    slug = disk.slugs[index] if index < len(disk.slugs) else ""
                    if ident == slug or ident.casefold() == title.strip().casefold():
                        normalized = f"{botfiles.EXTRA_SECTION_PREFIX}{slug}"
                        break
            pid = "p-" + secrets.token_hex(4)
            proposal = {
                "id": pid,
                "section": normalized,
                "before": botfiles.core_section_text(disk, normalized),
                "after": text,
                "reason": _one_line(reason)[:200],
                "source": _one_line(source)[:80],
                "created_at": self._now(),
            }
            botfiles.write_bytes(
                self._staging_dir(bot_id) / f"{pid}.json",
                (json.dumps(proposal, ensure_ascii=False, indent=1) + "\n").encode("utf-8"),
            )
            return pid

    def accept_staging(self, bot_id: str, pid: str) -> None:
        """Accept a proposal: it is written into L0 (an extra section that does not exist is created; more than three extras or a
        text over its limit is refused with :class:`BotFileError`, the proposal stays) and removed from the list.  The L0 hash
        changes, so an initialized bot becomes ``stale``."""
        with self._lock:
            self._require_meta(bot_id)
            proposal = self._read_proposal(bot_id, pid)
            if proposal is None:
                raise BotNotFound("没有这条待确认的提案。")
            core_dir = self.bot_dir(bot_id) / "core"
            disk = botfiles.read_core_on_disk(core_dir)
            core = botfiles.apply_section(disk, proposal["section"], proposal["after"])
            problems = botfiles.validate_core(core)
            if problems:
                raise BotFileError(problems)
            botfiles.write_core(core_dir, core)
            botfiles.remove_file(self._staging_dir(bot_id) / f"{pid}.json")

    def reject_staging(self, bot_id: str, pid: str) -> None:
        with self._lock:
            self._require_meta(bot_id)
            if self._read_proposal(bot_id, pid) is None:
                raise BotNotFound("没有这条待确认的提案。")
            botfiles.remove_file(self._staging_dir(bot_id) / f"{pid}.json")

    # ---------------------------------------------------------------------------------------------------- reports
    def _memory_view(self, bot_dir: Path, scenario_id: str) -> dict[str, Any]:
        sid = scenario_id or ""
        playbook_md = botfiles.read_part(bot_dir, "playbook", sid) if sid else ""
        people_md = botfiles.read_part(bot_dir, "people")
        return {
            "scenario_id": sid,
            "arch_notes": botfiles.read_part(bot_dir, "arch_notes"),
            "digest": botfiles.read_part(bot_dir, "digest", sid) if sid else "",
            "playbook": [entry.to_dict() for entry in botfiles.parse_playbook(playbook_md)],
            "playbook_md": playbook_md,
            "questions": botfiles.read_part(bot_dir, "questions", sid) if sid else "",
            "people": botfiles.parse_people(people_md),
            "people_md": people_md,
            "record": botfiles.record_text(botfiles.read_record(bot_dir)),
        }

    def _summary(self, bot_id: str, sources: SourceHashes, running: bool, scenario_id: str | None) -> tuple[dict[str, Any], botfiles.CoreOnDisk]:
        meta = self._require_meta(bot_id)
        bot_dir = self.bot_dir(bot_id)
        disk = botfiles.read_core_on_disk(bot_dir / "core")
        counts = botfiles.core_char_counts(disk.core, disk.slugs)
        sid = sources.scenario_id if scenario_id is None else scenario_id
        stats: dict[str, Any] = dict(botfiles.part_stats(bot_dir, sid))
        stats["record"] = botfiles.record_summary(botfiles.read_record(bot_dir))
        avatar = meta["avatar"]
        summary = {
            "id": bot_id,
            "name": meta["name"],
            "avatar": avatar,
            "avatar_code": avatar_code(avatar),
            "origin": meta["origin"],
            "core": {
                "complete": disk.core.complete,
                "missing": list(disk.core.missing),
                "chars": counts["chars"],
                "extras": counts["extras"],
                "updated_at": int(disk.updated_at) if disk.updated_at else None,
            },
            "init": derive_init(meta["init"], core_hash(disk.core), sources, running),
            "frozen": self._frozen(bot_id, meta),
            "memory_stats": stats,
            "staging": len(self.staging(bot_id)),
        }
        return summary, disk

    def summary(self, bot_id: str, *, sources: SourceHashes = SourceHashes(), running: bool = False) -> dict[str, Any]:
        """``BotSummary`` (contract §13.7) without ``seat`` and ``binding`` - the orchestrator adds those, and says whether an
        initialization is ``running`` (then ``init.status`` is ``initializing``).  ``sources`` are the current hashes of the
        contract, scenario and materials (and the scenario whose memory is counted)."""
        with self._lock:
            return self._summary(bot_id, sources, running, None)[0]

    def detail(self, bot_id: str, scenario_id: str = "", *, sources: SourceHashes = SourceHashes(), running: bool = False) -> dict[str, Any]:
        """``BotDetail``: the summary plus the L0 (``core.markdown``, ``core.sections``, ``core.extras`` with their texts), the
        memory of ``scenario_id`` and the pending proposals (``staging`` is the list here, not the count)."""
        with self._lock:
            sid = scenario_id or sources.scenario_id
            data, disk = self._summary(bot_id, sources, running, sid)
            meta = self._require_meta(bot_id)
            core = dict(data["core"])
            core["markdown"] = botfiles.render_core_markdown(disk.core, meta["name"])
            core["sections"] = {key: getattr(disk.core, key).strip() for key in CORE_SECTIONS}
            counts = {e["title"]: e for e in data["core"]["extras"]}
            core["extras"] = [
                {"title": title, "slug": counts.get(title, {}).get("slug", ""), "chars": len(text.strip()), "text": text.strip()}
                for title, text in disk.core.extras
            ]
            data["core"] = core
            data["memory"] = self._memory_view(self.bot_dir(bot_id), sid)
            data["staging"] = self.staging(bot_id)
            return data

    def problems(self, bot_id: str, *, sources: SourceHashes = SourceHashes(), running: bool = False) -> list[dict[str, str]]:
        """What keeps this bot out of a game (the start check, H25), as the ``items`` of a ``Problem``: ``{"code", "message",
        "fix"}`` - an L0 that is missing / incomplete / over a limit, a bot that is not initialized / ``stale`` / failed / being
        initialized (``running``), a required memory file that is gone.  Problems of the contract, the scenario and the model
        binding belong to the caller."""
        with self._lock:
            meta = self._require_meta(bot_id)
            bot_dir = self.bot_dir(bot_id)
            disk = botfiles.read_core_on_disk(bot_dir / "core")
            core = disk.core
            items: list[dict[str, str]] = []
            if len(core.missing) == len(CORE_SECTIONS) and not core.extras:
                items.append({"code": "core_missing", "message": "底层设定还没有写：身份、说话方式、分析方式都是空的。", "fix": "import_core"})
            else:
                faults = botfiles.validate_core(core)
                if core.missing:
                    faults.insert(0, "缺少" + "、".join(f"「{botfiles.CORE_HEADINGS[k]}」" for k in core.missing) + "。")
                if faults:
                    items.append({"code": "core_incomplete", "message": "底层设定不完整：" + " ".join(faults), "fix": "edit_core"})
            derived = derive_init(meta["init"], core_hash(core), sources, running)
            status = derived["status"]
            if status == INIT_RUNNING:
                items.append({"code": "init_running", "message": "正在初始化，请等它结束。", "fix": "init"})
            elif status == INIT_UNINITIALIZED:
                items.append({"code": "not_initialized", "message": "还没有初始化。", "fix": "init"})
            elif status == INIT_FAILED:
                items.append({"code": "init_failed", "message": f"上次初始化没有成功：{derived['error'] or '原因不明'}", "fix": "init"})
            elif status == INIT_STALE:
                changed = "、".join(SOURCE_LABELS[key] for key in derived["stale"])
                items.append({"code": "stale", "message": f"初始化之后{changed}变了，需要重新初始化。", "fix": "init"})
            if meta["init"]["status"] == INIT_READY and sources.scenario_id:
                missing = [
                    botfiles.PART_LABELS[part]
                    for part in ("arch_notes", "digest", "playbook")
                    if not botfiles.read_part(bot_dir, part, sources.scenario_id).strip()
                ]
                if missing:
                    items.append(
                        {"code": "not_initialized", "message": "缺少必需的记忆文件：" + "、".join(missing) + "，请重新初始化。", "fix": "init"}
                    )
            return items

    def problem_entry(self, bot_id: str, *, sources: SourceHashes = SourceHashes(), running: bool = False) -> dict[str, Any] | None:
        """A ``Problem`` of scope ``bot`` (``{"scope", "bot_id", "seat", "name", "items"}``), ``None`` when the bot is fine."""
        with self._lock:
            items = self.problems(bot_id, sources=sources, running=running)
            if not items:
                return None
            return {"scope": "bot", "bot_id": bot_id, "seat": self.seat_of(bot_id), "name": self.name_of(bot_id), "items": items}

    # ---------------------------------------------------------------------------------------------------- export / import
    def bot_files(self, bot_id: str) -> list[tuple[str, Path]]:
        """``(relative path, path)`` of every file that belongs to the bot (what a ``.arenabot`` carries, :func:`iter_bot_files`)."""
        with self._lock:
            self._require_meta(bot_id)
            return iter_bot_files(self.bot_dir(bot_id))

    def _inspect(self, staged: Path, *, exclude_id: str | None, check_name: bool = True) -> _Inspected:
        """Check a bot directory that is to be installed: ``bot.json`` (a valid name that no other bot has, the avatar), the L0
        (limits), every memory file (formats and limits), the proposals.  :class:`BotFileError` lists every fault; a name that
        another bot has is :class:`NameTaken` (not asked for with ``check_name=False``: a table is being built or checked as a
        whole).  Nothing is written."""
        staged = Path(staged)
        if not staged.is_dir() or staged.is_symlink():
            raise BotFileError("要导入的 bot 目录不存在。")
        try:
            raw = json.loads(botfiles.read_text(staged / BOT_FILE) or "null")
        except ValueError:
            raw = None
        if not isinstance(raw, dict):
            raise BotFileError("缺少 bot.json，或者它不是有效的 JSON。")
        problems: list[str] = []
        name = ""
        try:
            name = clean_name(raw.get("name"))
        except RosterError as exc:
            problems.append(f"bot.json 里的名字不合法：{exc}")
        files = iter_bot_files(staged)
        taken: list[tuple[str, Path]] = []
        for rel, path in files:
            if rel == BOT_FILE:
                continue
            if rel.startswith("staging/"):
                pid = rel[len("staging/") : -len(".json")]
                try:
                    if self._proposal_from(json.loads(botfiles.read_text(path)), pid) is None:
                        continue  # (a proposal that is not one is not imported; it is no reason to refuse the bot)
                except ValueError:
                    continue
            elif rel == botfiles.RECORD_FILE:
                try:
                    if not isinstance(json.loads(botfiles.read_text(path)), dict):
                        continue
                except ValueError:
                    continue
            else:
                part = self._part_of(rel)
                if part is not None:
                    try:
                        botfiles.normalize_part(part, botfiles.read_text(path))
                    except BotFileError as exc:
                        problems.extend(f"{rel}：{p}" for p in exc.problems)
            taken.append((rel, path))
        problems.extend(botfiles.validate_core(botfiles.read_core(staged / "core")))
        if problems:
            raise BotFileError(problems)
        if name and check_name:
            self.validate_name(name, exclude=exclude_id)
        avatar = None
        if isinstance(raw.get("avatar"), dict) and raw["avatar"]:
            try:
                avatar = validate_avatar(raw["avatar"])
            except AvatarUnavailable:
                avatar = raw["avatar"]  # (cannot be checked now: kept as it is)
            except BotError:
                avatar = None  # (an avatar that does not exist is replaced by a new one, not a reason to refuse the bot)
        return _Inspected(
            name=name,
            avatar=avatar,
            created_at=_unix(raw.get("created_at")),
            init=normalize_init(raw.get("init")),
            files=tuple(taken),
        )

    @staticmethod
    def _part_of(rel: str) -> str | None:
        if rel == "arch/notes.md":
            return "arch_notes"
        if rel == "people.md":
            return "people"
        pieces = rel.split("/")
        if len(pieces) == 3 and pieces[0] == "rules" and pieces[2] in RULE_FILES:
            return pieces[2][: -len(".md")]
        return None

    def validate_bot_dir(self, staged: Path, *, for_bot: str | None = None) -> str:
        """Check a bot directory without installing it (:meth:`install_bot_dir` does the same check first); returns the bot's name.
        ``for_bot`` is the bot it would replace (its own name is no conflict)."""
        with self._lock:
            return self._inspect(staged, exclude_id=for_bot).name

    def install_bot_dir(self, bot_id: str, staged: Path, *, origin: str = "import") -> None:
        """Replace everything of ``bot_id`` by the bot directory ``staged`` (the contents of a ``.arenabot``: ``bot.json``,
        ``core/`` ...): name, avatar, L0, memory, record, proposals.  The id in the file is ignored (the bot keeps its id, so the
        line-up and the binding stay).  Checked completely first (:meth:`_inspect`: :class:`BotFileError` with every fault,
        :class:`NameTaken` when another bot has the name); then the new directory is built beside the old one and swapped in, so a
        failure leaves the old bot as it was.  The initialization status follows from the hashes of the imported record."""
        with self._lock:
            self._require_meta(bot_id)
            inspected = self._inspect(staged, exclude_id=bot_id)
            meta = {
                "name": inspected.name,
                "avatar": inspected.avatar,
                "created_at": inspected.created_at or self._now(),
                "origin": origin if origin in ORIGINS else "import",
                "init": inspected.init,
            }
            if meta["avatar"] is None:
                taken = [m["avatar"] for m in self._all_meta() if m["id"] != bot_id and m["avatar"]]
                moderator = self.moderator()["avatar"]
                if moderator:
                    taken.append(moderator)
                (picked,) = pick_avatars(1, taken)
                meta["avatar"] = picked
            tmp = self._root / f"{TEMP_PREFIXES[0]}{bot_id}-{secrets.token_hex(3)}"
            try:
                ensure_private_directory(tmp)
                for rel, path in inspected.files:
                    botfiles.write_bytes(tmp / rel, path.read_bytes())
                botfiles.write_bytes(tmp / BOT_FILE, self._meta_bytes(bot_id, meta))
                _swap_dir(self.bot_dir(bot_id), tmp)
            finally:
                if tmp.exists():
                    _rmtree(tmp)

    # ---------------------------------------------------------------------------------------------------- default library
    def library_dir(self) -> Path:
        return self._library_dir if self._library_dir is not None else default_library_dir()

    def library(self) -> list[LibraryBot]:
        """The bots of the default library (H44), in the order Alice, Bob ... (other names after them, by name)."""
        folder = self.library_dir()
        try:
            entries = sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")) if folder.is_dir() else []
        except OSError:
            return []
        found: list[LibraryBot] = []
        for path in entries:
            try:
                raw = json.loads(botfiles.read_text(path / BOT_FILE) or "null")
            except ValueError:
                raw = None
            raw = raw if isinstance(raw, dict) else {}
            try:
                name = clean_name(raw.get("name") or path.name)
            except RosterError:
                logger.warning("The default bot %s has no valid name; skipped", path.name)
                continue
            avatar = raw.get("avatar") if isinstance(raw.get("avatar"), dict) and raw.get("avatar") else None
            core = botfiles.read_core(path / "core")
            found.append(LibraryBot(folder=path.name, path=path, name=name, avatar=avatar, identity=_short(core.identity)))

        def order(item: LibraryBot) -> tuple[int, int, str]:
            names = [n.casefold() for n in DEFAULT_NAMES]
            key = item.name.casefold()
            return (0, names.index(key), "") if key in names else (1, 0, key)

        return sorted(found, key=order)

    def defaults(self) -> list[dict[str, Any]]:
        """``BotsPayload.defaults``: ``[{"name", "avatar", "identity"}]``, what "import from the default library" offers."""
        return [{"name": b.name, "avatar": b.avatar, "identity": b.identity} for b in self.library()]

    def import_default(self, bot_id: str, name: str) -> None:
        """Replace ``bot_id`` by the default-library bot called ``name`` (any case); :class:`BotNotFound` when the library has none,
        :class:`NameTaken` when another bot at the table already has that name."""
        with self._lock:
            self._require_meta(bot_id)
            key = str(name or "").strip().casefold()
            entry = next((b for b in self.library() if b.name.casefold() == key), None)
            if entry is None:
                raise BotNotFound(f"默认库里没有「{str(name)[:20]}」。")
            self.install_bot_dir(bot_id, entry.path, origin="default")

    # ---------------------------------------------------------------------------------------------------- creating bots
    @staticmethod
    def _unique_name(name: str, used: set[str]) -> str:
        """``name`` when no bot of ``used`` (case-folded names) has it, else ``name2``, ``name3`` ... (kept within the limit)."""
        if name.casefold() not in used:
            return name
        number = 2
        while True:
            suffix = str(number)
            candidate = name[: MAX_NAME_CHARS - len(suffix)] + suffix
            if candidate.casefold() not in used:
                return candidate
            number += 1

    def _create_bot(
        self,
        *,
        name: str,
        avatar: dict[str, Any] | None,
        origin: str,
        files: Iterable[tuple[str, Path]] = (),
        core: CoreFiles | None = None,
        init: dict[str, Any] | None = None,
        created_at: int | None = None,
        bot_id: str | None = None,
        prepare: Callable[[Path], None] | None = None,
    ) -> str:
        """Write a new bot directory: ``files`` (``(relative path, source)``) are copied as they are, ``core`` (if given) is written
        as L0, ``prepare(directory)`` (if given) writes whatever else the bot starts with, and ``bot.json`` comes last - a
        directory without it is no bot, so a crash half way leaves nothing behind that counts.  The line-up is not touched.
        Returns the id."""
        new_id = bot_id or self._new_id()
        root = self.bot_dir(new_id)
        try:
            ensure_private_directory(root)
            for rel, path in files:
                if rel != BOT_FILE:
                    botfiles.write_bytes(root / rel, path.read_bytes())
            if core is not None:
                botfiles.write_core(root / "core", core)
            if prepare is not None:
                prepare(root)
            meta = {
                "name": name,
                "avatar": avatar,
                "created_at": created_at or self._now(),
                "origin": origin,
                "init": init if init is not None else empty_init(),
            }
            botfiles.write_bytes(root / BOT_FILE, self._meta_bytes(new_id, meta))
        except BaseException:
            _rmtree(root)
            raise
        return new_id

    def _create_from_library(self, entry: LibraryBot, used: set[str], taken: list[dict[str, Any]], *, origin: str = "default") -> str:
        """A new bot made from a library entry (the name is made unique among ``used``; ``used`` and ``taken`` - the avatars of the
        table - are updated).  :class:`BotFileError` when the entry is not valid."""
        inspected = self._inspect(entry.path, exclude_id=None, check_name=False)
        name = self._unique_name(inspected.name, used)
        avatar = inspected.avatar
        if avatar is None:
            (avatar,) = pick_avatars(1, taken)
        bot_id = self._create_bot(
            name=name, avatar=avatar, origin=origin, files=inspected.files, init=inspected.init, created_at=self._now()
        )
        used.add(name.casefold())
        if avatar:
            taken.append(avatar)
        return bot_id

    def _blank_bots(self, count: int, used: set[str], taken: list[dict[str, Any]]) -> list[str]:
        """``count`` bots without any L0, named Alice, Bob ... (the first of those names nobody has; ``玩家<n>`` after them)."""
        if count <= 0:
            return []
        avatars = pick_avatars(count, taken)
        spare = [n for n in DEFAULT_NAMES if n.casefold() not in used]
        ids: list[str] = []
        for index in range(count):
            if spare:
                name = spare.pop(0)
            else:
                number = len(used) + 1
                while f"玩家{number}".casefold() in used:
                    number += 1
                name = f"玩家{number}"
            avatar = avatars[index]
            ids.append(self._create_bot(name=name, avatar=avatar, origin="user"))
            used.add(name.casefold())
            if avatar:
                taken.append(avatar)
        return ids

    def install_defaults(self) -> list[str]:
        """Create the line-up from the default library (H44): its first ``seats`` bots, and blank bots with the default names when
        it has fewer.  For a table that has no bots; returns the ids in seat order and writes the line-up (the memory
        switch and the moderator's avatar of an existing ``lineup.json`` are kept)."""
        with self._lock:
            ensure_private_directory(self._root)
            if self.lineup():
                raise BotError("已经有玩家了，不能再从默认库重新建立阵容。")
            used: set[str] = set()
            taken: list[dict[str, Any]] = []
            ids: list[str] = []
            for entry in self.library():
                if len(ids) >= self._seats:
                    break
                try:
                    ids.append(self._create_from_library(entry, used, taken))
                except BotFileError as exc:
                    logger.warning("The default bot %s is not valid and is skipped: %s", entry.folder, exc)
            ids.extend(self._blank_bots(self._seats - len(ids), used, taken))
            raw = self._read_lineup_raw() or {}
            raw["order"] = ids
            self._write_lineup_raw(raw)
            return ids

    # ---------------------------------------------------------------------------------------------------- first run, repair
    def _bot_ids_on_disk(self) -> list[str]:
        """The bot directories that really are bots (valid id, ``bot.json`` there), oldest first."""
        try:
            names = sorted(p.name for p in self._root.iterdir() if p.is_dir()) if self._root.is_dir() else []
        except OSError:
            return []
        found = [(self._read_meta(n), n) for n in names if BOT_ID_RE.match(n)]
        return [n for meta, n in sorted(((m, n) for m, n in found if m is not None), key=lambda item: (item[0]["created_at"], item[1]))]

    def _sweep_temp(self) -> None:
        """Delete what an interrupted install / reset left beside the bots (``.install-*``, ``.old-*``, ``.new-*``, ``.trash-*``)."""
        try:
            leftovers = [p for p in self._root.iterdir() if p.is_dir() and p.name.startswith(TEMP_PREFIXES)]
        except OSError:
            return
        for path in leftovers:
            _rmtree(path)

    def _repair_lineup(self) -> bool:
        """Make the line-up exactly ``seats`` bots that exist: the listed ones that exist stay in their seats, bots that are in
        the directory but not listed fill the gaps (oldest first), what is still missing is created (library bots whose names
        nobody has, then blank ones).  ``True`` when ``lineup.json`` was written."""
        raw = self._read_lineup_raw()
        seen: set[str] = set()
        slots: list[str | None] = []  # a bot that is gone leaves a hole in its seat: the others keep theirs
        for item in (raw or {}).get("order", []):
            if isinstance(item, str) and item not in seen:
                seen.add(item)
                slots.append(item if self._exists(item) else None)
        slots = slots[: self._seats]
        slots.extend([None] * (self._seats - len(slots)))
        holes = [index for index, value in enumerate(slots) if value is None]
        if holes:
            for bot_id in self._bot_ids_on_disk():  # (a bot that is there but was not listed)
                if holes and bot_id not in slots:
                    slots[holes.pop(0)] = bot_id
        if holes:
            present = [i for i in slots if i is not None]
            metas = [m for m in (self._read_meta(i) for i in present) if m is not None]
            used = {m["name"].casefold() for m in metas}
            taken = [m["avatar"] for m in metas if m["avatar"]]
            for entry in self.library():
                if not holes:
                    break
                if entry.name.casefold() in used:
                    continue
                try:
                    slots[holes[0]] = self._create_from_library(entry, used, taken)
                    holes.pop(0)
                except BotFileError as exc:
                    logger.warning("The default bot %s is not valid and is skipped: %s", entry.folder, exc)
            for bot_id in self._blank_bots(len(holes), used, taken):
                slots[holes.pop(0)] = bot_id
        order = [i for i in slots if i is not None]
        if raw is not None and list(raw["order"]) == order:
            return False
        data = dict(raw or {})
        data["order"] = order
        self._write_lineup_raw(data)
        return True

    @staticmethod
    def _has_legacy_roster(roster_dir: Path) -> bool:
        try:
            if not roster_dir.is_dir():
                return False
            return (roster_dir / "roster.json").is_file() or any(roster_dir.glob("N*.persona.md")) or any(roster_dir.glob("N*.memory.md"))
        except OSError:
            return False

    def bootstrap(self) -> None:
        """First use of a data directory (once per store): create ``bots/``; a directory without bots gets them - migrated from a
        v0.13 roster (``<data>/roster/``, :meth:`import_legacy_roster`) when there is one, else the default library
        (:meth:`install_defaults`); a line-up that is not complete is repaired; every bot (and the moderator) gets an avatar; the
        default model bindings saved by seat move to the bots.  Idempotent."""
        with self._lock:
            if self._bootstrapped:
                return
            ensure_private_directory(self._root)
            self._sweep_temp()
            if not self.lineup() and not self._bot_ids_on_disk():
                roster_dir = self._data_dir / "roster"
                migrated = False
                if self._has_legacy_roster(roster_dir):
                    try:
                        self.import_legacy_roster(roster_dir)
                        migrated = True
                    except Exception:  # noqa: BLE001 - never leave the table empty because of an old file
                        logger.warning("The v0.13 roster could not be migrated; using the default library", exc_info=True)
                        self._wipe_bots()
                if not migrated:
                    self.install_defaults()
            self._repair_lineup()
            self.ensure_avatars()
            try:
                self._settings().migrate_seat_bindings()
            except Exception:  # noqa: BLE001 - the bindings are read as they are until the next save
                logger.warning("Could not move the default model bindings to the bots", exc_info=True)
            self._bootstrapped = True

    def _wipe_bots(self) -> None:
        """Remove every bot directory and the line-up (what a failed migration leaves; and what a migration replaces)."""
        try:
            entries = list(self._root.iterdir()) if self._root.is_dir() else []
        except OSError:
            entries = []
        for path in entries:
            if path.is_dir() and (BOT_ID_RE.match(path.name) or path.name.startswith(TEMP_PREFIXES)):
                _rmtree(path)
        botfiles.remove_file(self._root / LINEUP_FILE)

    def import_legacy_roster(
        self,
        roster_dir: Path,
        *,
        replace: bool = True,
        scenario_id: str | None = None,
        fill_missing_core: bool = True,
    ) -> list[str]:
        """Turn a v0.13 roster (``roster.json``, ``N<k>.persona.md``, ``N<k>.memory.md``) into the bots of this table (H45); no model
        is called, nothing of the old directory is changed or deleted.  Returns the ids in seat order.

        * name and avatar are kept (a colon in an old name becomes a space; two seats with one name: the later one gets a number);
          the seats become the line-up in the order N1, N2 ...; the memory switch and the moderator's avatar are kept;
        * the persona becomes L0 by its headings (:func:`legacy_persona_to_core`); an empty persona leaves L0 missing - except
          with ``fill_missing_core`` (the default, H59: v0.13 personas were empty by default, so most old tables would have twelve
          bots that cannot start): a bot with no L0 whose name is the name of a default-library bot gets that bot's L0 (name,
          avatar and memory stay the migrated ones); a bot with another name stays without L0;
        * the impressions, kept by seat, are kept by the NAME of that seat's bot (``people.md``); the experiences become
          playbook entries with the lowest confidence under ``scenario_id`` (default: :func:`default_scenario_id`; without
          one they are not migrated); the record is kept (``record.json``); the text of the old "旧记忆（待整理）" section is not
          migrated (the old directory stays as it is);
        * every bot is ``uninitialized``, origin ``migrated``; the bindings saved by seat move to the bots.

        ``replace`` (the default): whatever bots there are now are removed first (a whole data directory is being replaced); with
        ``replace=False`` an existing table is left alone.  Idempotent: a migrated bot's id is made from its seat and name, so
        migrating the same roster twice gives the same directories."""
        with self._lock:
            existing = self.lineup()
            if existing and not replace:
                return existing
            legacy = read_legacy_roster(Path(roster_dir), self._seats)
            sid = default_scenario_id() if scenario_id is None else scenario_id
            if sid:
                sid = botfiles.check_scenario_id(sid)
            used: set[str] = set()
            names: dict[str, str] = {}
            for row in legacy.seats:
                name = self._unique_name(_legacy_name(row.name, row.seat), used)
                used.add(name.casefold())
                names[row.id] = name
            library = {b.name.casefold(): b for b in self.library()} if fill_missing_core else {}
            ensure_private_directory(self._root)
            self._wipe_bots()
            ids: list[str] = []
            for row in legacy.seats:
                name = names[row.id]
                core = legacy_persona_to_core(row.persona)
                entry = library.get(name.casefold())
                if entry is not None and not (core.identity or core.voice or core.mind):
                    core = botfiles.read_core(entry.path / "core")
                people = {
                    names[sid_other]: text
                    for sid_other, text in row.parts.impressions.items()
                    if text and sid_other in names and sid_other != row.id
                }
                playbook = [
                    PlaybookEntry(text=_fit_text(item, PLAYBOOK_ENTRY_CHARS), confidence="低", source=LEGACY_EXPERIENCE_SOURCE)
                    for item in row.parts.experience
                    if item.strip()
                ]
                tally = botfiles.tally_from_text(row.parts.record)

                def prepare(root: Path, people=people, playbook=playbook, tally=tally) -> None:
                    botfiles.write_text(root / "people.md", botfiles.render_people(people))
                    if sid and playbook:
                        botfiles.write_text(root / botfiles.part_relpath("playbook", sid), botfiles.render_playbook(playbook))
                    botfiles.write_record(root, tally)

                bot_id = legacy_bot_id(row.seat, name)
                if (self._root / bot_id).exists():  # (two seats with the same name cannot happen: names are unique)
                    bot_id = self._new_id()
                ids.append(
                    self._create_bot(
                        name=name,
                        avatar=row.avatar,
                        origin="migrated",
                        core=core,
                        bot_id=bot_id,
                        prepare=prepare,
                    )
                )
            raw: dict[str, Any] = {"order": ids, "memory_enabled": legacy.memory_enabled}
            if legacy.moderator_avatar:
                raw["moderator"] = {"avatar": legacy.moderator_avatar}
            self._write_lineup_raw(raw)
            self.ensure_avatars()
            try:
                settings = self._settings()
                settings.migrate_seat_bindings()
                settings.prune_bot_bindings(ids)
            except Exception:  # noqa: BLE001 - the bindings are a convenience; the bots are migrated
                logger.warning("Could not move the default model bindings to the migrated bots", exc_info=True)
            return ids

    def check_integrity(self, *, content: bool = True) -> list[str]:
        """Everything that is wrong with the bots of this data directory, in Chinese (``[]`` = fine): a line-up that is missing,
        has the wrong number of bots, repeats or names a bot that is not there, a name used twice; and - with ``content`` - every
        bot checked like an import (:meth:`_inspect`: the limits of its L0, the format of its memory files).  ``content=False`` is the
        structure alone: what makes a table unusable.  Read only.  Used after a whole data directory was replaced (archive import)."""
        problems: list[str] = []
        with self._lock:
            raw = self._read_lineup_raw()
            if raw is None:
                problems.append("缺少阵容文件（bots/lineup.json），或者它不是有效的 JSON。")
                listed: list[str] = []
            else:
                listed = [str(item) for item in raw["order"]]
                if len(listed) != self._seats:
                    problems.append(f"阵容里有 {len(listed)} 个玩家，应该是 {self._seats} 个。")
                if len(set(listed)) != len(listed):
                    problems.append("阵容里有重复的玩家。")
            names: dict[str, str] = {}
            for bot_id in dict.fromkeys(listed):
                if not self._exists(bot_id):
                    problems.append(f"阵容里的玩家 {bot_id[:14]} 没有对应的 bot 目录。")
                    continue
                meta = self._require_meta(bot_id)
                label = f"玩家「{meta['name']}」"
                folded = meta["name"].casefold()
                if folded in names:
                    problems.append(f"{label}和另一个玩家重名了。")
                names[folded] = bot_id
                if not content:
                    continue
                try:
                    self._inspect(self.bot_dir(bot_id), exclude_id=bot_id, check_name=False)
                except BotFileError as exc:
                    problems.extend(f"{label}：{p}" for p in exc.problems)
            return problems

    # ---------------------------------------------------------------------------------------------------- settings
    def _settings(self) -> Any:
        """The settings store of this data directory (the process-wide one when it is the same directory)."""
        from app.storage import settings as settings_module

        store = settings_module.get_settings_store()
        if store.data_dir != self._data_dir:
            return settings_module.SettingsStore(self._data_dir)
        return store


# ======================================================================================================================
# the process-wide store
# ======================================================================================================================
_store: BotStore | None = None
_store_lock = threading.Lock()


def get_bot_store(seats: int = 12) -> BotStore:
    """The bot store of the data directory of this process.  The first call on a data directory sets the table up
    (:meth:`BotStore.bootstrap`: it creates ``bots/``), so call it only once the data directory is locked
    (:func:`app.storage.datadir.ensure_data_dir`).  A new store is made when the data directory (tests) or the number of seats
    changes."""
    global _store
    with _store_lock:
        data_dir = Path(get_data_dir()).resolve()
        if _store is None or _store.data_dir != data_dir or _store.seats != seats:
            _store = BotStore(data_dir, seats=seats)
        store = _store
    store.bootstrap()
    return store


def reset_bot_store() -> None:
    global _store
    with _store_lock:
        _store = None


__all__ = [
    "AvatarUnavailable",
    "BOTS_DIRNAME",
    "BOT_FILE",
    "BotError",
    "BotNotFound",
    "BotStore",
    "LINEUP_FILE",
    "LibraryBot",
    "LineupError",
    "MODERATOR_ID",
    "MODERATOR_NAME",
    "NameTaken",
    "NeedsConfirm",
    "RESET_CONFIRM_TEXT",
    "SOURCE_LABELS",
    "WILL_CLEAR",
    "avatar_code",
    "default_library_dir",
    "derive_init",
    "empty_init",
    "get_bot_store",
    "iter_bot_files",
    "limits_payload",
    "normalize_init",
    "pick_avatars",
    "read_lineup_ids",
    "reset_bot_store",
    "validate_avatar",
]
