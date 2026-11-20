"""The rules of a game - the scenario file and its study materials - in the data directory (v0.14; H8, H39;
docs/v0.12-contracts.md §13.2, §13.7).

    <data>/rules/<scenario_id>/scenario.md          the scenario: mechanics, player rules, moderator manual (one file)
    <data>/rules/<scenario_id>/source.json          {"origin": "default|imported", "version", "title", "imported_at": <unix s>|null, "sha256"}
    <data>/rules/<scenario_id>/materials/<slug>.md  study materials (optional, empty by default, C10)
    <data>/rules/<scenario_id>/materials/<slug>.json  {"title": "..."}  (only when the user gave a title that is not in the text)

The engine reads the scenario from here (``app.game.sources``); the file ``config.yml: scenario.path`` names is only the *default*
that is copied here on the first run and again by "恢复默认".  A default that was never changed is refreshed when a newer version of
the program ships another one (the bots then show "需要重新初始化" by themselves: the hash changed); one the user replaced - or edited
by hand in the data directory - is theirs and is never touched.

Everything written is **checked first and written atomically**; nothing is cut to fit.  Texts are stored with LF line ends, without
a byte-order mark and with exactly one final newline (:func:`normalize_text`) so that the hash of a file does not depend on the editor
that saved it; the shipped files are in that form (a test keeps them so), which is what makes the hash of a default bot's initialization
match the data directory's copy.  File names inside the data directory are fixed or made from an ASCII slug, never from user input.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.bots.types import SLUG_RE, sha256_text
from app.storage import botfiles
from app.storage._atomic import ensure_private_directory
from app.storage.errors import StorageError
from app.storage.paths import get_base_dir, get_data_dir
from app.world.loader import parse_scenario_v2
from app.world.spec import ScenarioError

logger = logging.getLogger(__name__)

RULES_DIRNAME = "rules"
SCENARIO_FILE = "scenario.md"
SOURCE_FILE = "source.json"
MATERIALS_DIRNAME = "materials"
ORIGIN_DEFAULT = "default"
ORIGIN_IMPORTED = "imported"

SCENARIO_ID_RE = botfiles.SCENARIO_ID_RE  # ``[A-Za-z0-9][A-Za-z0-9_-]{0,63}``: the only thing that becomes part of a path
MAX_SCENARIO_BYTES = 512 * 1024  # the bundled scenario is about 70 KB
MAX_SCENARIOS = 20
MAX_MATERIAL_BYTES = 100 * 1024
MAX_MATERIALS = 20  # per scenario
MATERIAL_TITLE_CHARS = 60

#: What a scenario may be called in a message.
_ID_RULE = "剧本 id 只能由字母、数字、下划线和连字符组成（以字母或数字开头），最多 64 个字符。"
_SLUG_RULE = "材料的标识只能用小写字母、数字和连字符（以字母或数字开头），最多 32 个字符。"


# ======================================================================================================================
# errors
# ======================================================================================================================
class RulesError(ValueError):
    """A scenario or a material that cannot be saved (or a request about one that cannot be answered).  ``problems`` are Chinese
    sentences, one per fault (``str(error)`` joins them); ``status`` / ``code`` are what the HTTP layer answers with."""

    status = 400
    code = "invalid"

    def __init__(self, problems: Any, *, status: int | None = None, code: str | None = None) -> None:
        items = [str(problems)] if isinstance(problems, str) else [str(item) for item in problems]
        items = [item for item in items if item] or ["内容无效。"]
        super().__init__(" ".join(items))
        self.problems: list[str] = items
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code


class RulesNotFound(RulesError):
    status = 404
    code = "not_found"


# ======================================================================================================================
# text
# ======================================================================================================================
def normalize_text(text: Any) -> str:
    """A text as it is stored: no byte-order mark, LF line ends, trailing whitespace replaced by one newline (``""`` stays empty)."""
    value = botfiles.normalize_newlines("" if text is None else str(text)).rstrip()
    return value + "\n" if value else ""


def _checked(text: Any, what: str) -> str:
    """:func:`normalize_text` of ``text``; a NUL or a lone surrogate (it could not be written) is a :class:`RulesError`."""
    if text is not None and not isinstance(text, str):
        raise RulesError(f"{what}必须是文字。")
    value = normalize_text(text)
    if "\x00" in value:
        raise RulesError(f"{what}里含有空字符，不能保存。")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise RulesError(f"{what}里含有无法保存的字符（不成对的 Unicode 代理字符）。") from None
    return value


def _kib(size: int) -> int:
    return (size + 1023) // 1024


def _unix(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value > 0 else None


# ======================================================================================================================
# the default scenario (``config.yml: scenario.path``) and what a scenario file says about itself
# ======================================================================================================================
_config_cache: dict[str, str] = {}


def default_scenario_path() -> Path | None:
    """The scenario file the program ships (``config.yml: scenario.path``), ``None`` when there is none.  ``config.yml`` is parsed
    again only when its content changed."""
    config_file = get_base_dir() / "config.yml"
    try:
        raw = config_file.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    if digest not in _config_cache:
        try:
            from app.config import load_config

            section = load_config().get("scenario") or {}
            value = str(section.get("path") or section.get("document") or "")
        except Exception:  # noqa: BLE001 - no configuration, no default scenario
            value = ""
        if len(_config_cache) > 8:
            _config_cache.clear()
        _config_cache[digest] = value
    value = _config_cache[digest]
    if not value:
        return None
    file = Path(value)
    return file if file.is_absolute() else get_base_dir() / file


_ID_LINE_RE = re.compile(r"^id:[ \t]*([A-Za-z0-9][A-Za-z0-9_-]{0,63})[ \t]*$", re.MULTILINE)


def scenario_id_of_file(file: Path | None) -> str:
    """The ``id:`` of a scenario file's front matter (else the file's name without its suffix); ``""`` when there is no file."""
    if file is None:
        return ""
    try:
        head = Path(file).read_text(encoding="utf-8-sig")[:4000]
    except OSError:
        return ""
    match = _ID_LINE_RE.search(head)
    if match:
        return match.group(1)
    return Path(file).stem if SCENARIO_ID_RE.match(Path(file).stem) else ""


def default_scenario_id() -> str:
    """The id of the scenario the program ships; ``""`` when there is none."""
    return scenario_id_of_file(default_scenario_path())


@dataclass(frozen=True)
class ScenarioInfo:
    """What the loader makes of a scenario text: its ``id`` / ``version`` / ``title``, or why it cannot be loaded (``problem``)."""

    id: str = ""
    version: str = ""
    title: str = ""
    problem: str | None = None


_info_cache: dict[tuple[str, str], ScenarioInfo] = {}


def scenario_info(text: str, where: str = "scenario.md") -> ScenarioInfo:
    """Load ``text`` as a scenario (:func:`app.world.loader.parse_scenario_v2`: the same check the engine makes).  Parsing the bundled
    scenario takes some 40 ms, so the verdict is remembered by the hash of the text."""
    key = (sha256_text(text), where)
    cached = _info_cache.get(key)
    if cached is not None:
        return cached
    try:
        spec = parse_scenario_v2(text, where=where)
        info = ScenarioInfo(spec.id, spec.version, spec.title or "", None)
    except ScenarioError as exc:
        info = ScenarioInfo(problem=str(exc))
    except Exception as exc:  # noqa: BLE001 - whatever else a strange file makes the loader raise is "cannot be loaded" too
        info = ScenarioInfo(problem=f"{where}: 剧本无法加载（{type(exc).__name__}）")
    if len(_info_cache) > 64:
        _info_cache.clear()
    _info_cache[key] = info
    return info


# ======================================================================================================================
# the store
# ======================================================================================================================
def _plain_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _derive_title(text: str, slug: str) -> str:
    """The title of a material that has no explicit one: its first ``# `` heading (outside code fences), else the slug."""
    fenced = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not fenced and re.match(r"^#(?!#)[ \t]+\S", stripped):
            title = re.sub(r"\s+", " ", stripped[1:]).strip()
            return title[:MATERIAL_TITLE_CHARS]
    return slug


class RulesStore:
    """The scenarios and study materials of one data directory.  ``default_path`` is the scenario file to install as the default
    (``None``: the one ``config.yml`` names); ``clock`` returns the time in seconds."""

    def __init__(self, data_dir: Path, *, default_path: Path | None = None, clock: Any = time.time) -> None:
        self._data_dir = Path(data_dir).resolve()
        self._root = self._data_dir / RULES_DIRNAME
        self._default_path = Path(default_path) if default_path is not None else None
        self._clock = clock
        self._lock = threading.RLock()
        self._installed = False

    # ---------------------------------------------------------------------------------------------------- paths and ids
    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def root(self) -> Path:
        return self._root

    def _now(self) -> int:
        return int(self._clock())

    def _check_new_id(self, sid: Any) -> str:
        if not isinstance(sid, str) or not SCENARIO_ID_RE.match(sid):
            raise RulesError(_ID_RULE)
        return sid

    def _scenario_dir(self, sid: Any) -> Path:
        """The directory of a scenario that may or may not exist; an id that is not a valid one is :class:`RulesNotFound`."""
        if not isinstance(sid, str) or not SCENARIO_ID_RE.match(sid):
            raise RulesNotFound(f"没有这个剧本：{str(sid)[:24]}")
        return self._root / sid

    def _default_file(self) -> Path | None:
        return self._default_path if self._default_path is not None else default_scenario_path()

    def default_id(self) -> str | None:
        """The id of the default scenario (the one ``restore default`` can bring back), ``None`` when the program ships none."""
        found = scenario_id_of_file(self._default_file())
        return found or None

    def _exists(self, sid: str) -> bool:
        try:
            folder = self._scenario_dir(sid)
        except RulesNotFound:
            return False
        try:
            return not folder.is_symlink() and _plain_file(folder / SCENARIO_FILE)
        except OSError:
            return False

    def has(self, sid: str) -> bool:
        with self._lock:
            return self._exists(sid)

    def ids(self) -> list[str]:
        """The scenarios there are, the default one first, the others by id."""
        with self._lock:
            try:
                names = sorted(p.name for p in self._root.iterdir() if p.is_dir()) if self._root.is_dir() else []
            except OSError:
                names = []
            found = [name for name in names if SCENARIO_ID_RE.match(name) and self._exists(name)]
            default = self.default_id()
            if default in found:
                found.remove(default)
                found.insert(0, default)
            return found

    def scenario_path(self, sid: str) -> Path:
        """The scenario file of ``sid`` (:class:`RulesNotFound` when there is none)."""
        with self._lock:
            if not self._exists(sid):
                raise RulesNotFound(f"没有这个剧本：{str(sid)[:24]}")
            return self._scenario_dir(sid) / SCENARIO_FILE

    def scenario_text(self, sid: str) -> str:
        """The whole scenario file as stored (:class:`RulesNotFound` when there is none)."""
        with self._lock:
            return botfiles.read_text(self.scenario_path(sid))

    # ---------------------------------------------------------------------------------------------------- the shipped default
    def _shipped_text(self) -> str | None:
        """The text of the default scenario file, normalized; ``None`` when it cannot be read."""
        file = self._default_file()
        if file is None:
            return None
        text = botfiles.read_text(file)
        return normalize_text(text) if text.strip() else None

    def _is_shipped(self, sid: str, sha: str) -> bool:
        if sid != self.default_id():
            return False
        shipped = self._shipped_text()
        return shipped is not None and sha256_text(shipped) == sha

    # ---------------------------------------------------------------------------------------------------- source.json
    def _read_source(self, sid: str) -> dict[str, Any] | None:
        try:
            raw = json.loads(botfiles.read_text(self._scenario_dir(sid) / SOURCE_FILE) or "null")
        except (ValueError, StorageError, RulesNotFound):
            return None
        return raw if isinstance(raw, dict) else None

    def _write_source(self, sid: str, *, text: str, origin: str, imported_at: int | None) -> None:
        info = scenario_info(text, where=f"{sid}.md")
        data = {
            "origin": origin,
            "version": info.version,
            "title": info.title,
            "imported_at": imported_at,
            "sha256": sha256_text(text),
        }
        botfiles.write_bytes(
            self._scenario_dir(sid) / SOURCE_FILE, (json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
        )

    def source(self, sid: str) -> dict[str, Any]:
        """``{"origin": "default|imported", "version", "imported_at": <unix s>|null, "sha256"}`` of the scenario as it is *now*: the
        hash is the file's own, so a file edited by hand in the data directory is reported as imported (not as the default it
        once was) with the time of its last change."""
        with self._lock:
            path = self.scenario_path(sid)
            text = botfiles.read_text(path)
            sha = sha256_text(text)
            stored = self._read_source(sid)
            if stored is not None and stored.get("sha256") == sha and stored.get("origin") in (ORIGIN_DEFAULT, ORIGIN_IMPORTED):
                origin, imported_at = str(stored["origin"]), _unix(stored.get("imported_at"))
            elif self._is_shipped(sid, sha):
                origin, imported_at = ORIGIN_DEFAULT, None
            else:
                origin = ORIGIN_IMPORTED
                try:
                    imported_at = int(path.stat().st_mtime)
                except OSError:
                    imported_at = None
            info = scenario_info(text, where=f"{sid}.md")
            return {"origin": origin, "version": info.version, "imported_at": imported_at, "sha256": sha}

    # ---------------------------------------------------------------------------------------------------- the scenario
    def validate_scenario(self, sid: str, markdown: Any) -> list[str]:
        """Every reason this text cannot be the scenario ``sid`` (``[]`` = it can): empty, too large, not loadable (the loader's own
        Chinese explanation), or an ``id`` in the file that is not ``sid``.  Nothing is written."""
        self._check_new_id(sid)
        try:
            text = _checked(markdown, "剧本")
        except RulesError as exc:
            return exc.problems
        if not text.strip():
            return ["剧本是空的。"]
        size = len(text.encode("utf-8"))
        if size > MAX_SCENARIO_BYTES:
            return [f"剧本最多 {MAX_SCENARIO_BYTES // 1024} KB（现在约 {_kib(size)} KB）。"]
        info = scenario_info(text, where=f"{sid}.md")
        if info.problem:
            return [info.problem]
        if info.id != sid:
            return [f"剧本文件里的 id 是「{info.id}」，与这个剧本的 id「{sid}」不同。"]
        return []

    def _install(self, sid: str, text: str, *, origin: str) -> None:
        folder = self._scenario_dir(sid)
        ensure_private_directory(folder)
        botfiles.write_bytes(folder / SCENARIO_FILE, text.encode("utf-8"))
        self._write_source(sid, text=text, origin=origin, imported_at=None if origin == ORIGIN_DEFAULT else self._now())

    def set_scenario(self, sid: str, markdown: Any) -> dict[str, Any]:
        """Replace (or create) the scenario ``sid``: checked first (:meth:`validate_scenario`; :class:`RulesError` with every
        problem and nothing written), then written atomically.  Returns :meth:`source`.  The study materials stay."""
        self._check_new_id(sid)
        text = _checked(markdown, "剧本")
        problems = self.validate_scenario(sid, text)
        if problems:
            raise RulesError(problems)
        with self._lock:
            if not self._exists(sid) and len(self.ids()) >= MAX_SCENARIOS:
                raise RulesError(f"最多 {MAX_SCENARIOS} 个剧本。")
            origin = ORIGIN_DEFAULT if self._is_shipped(sid, sha256_text(text)) else ORIGIN_IMPORTED
            self._install(sid, text, origin=origin)
            return self.source(sid)

    def reset(self, sid: str) -> dict[str, Any]:
        """Bring the default scenario back (the file ``config.yml`` names, copied again; the materials stay).  Only the default
        scenario has a default: any other id is a :class:`RulesError` (``no_default``)."""
        self._check_new_id(sid)
        with self._lock:
            if sid != self.default_id():
                raise RulesError("这个剧本没有默认版本，不能恢复默认。", code="no_default")
            text = self._shipped_text()
            if text is None:
                raise RulesError("找不到默认剧本文件，无法恢复默认。", status=503, code="default_missing")
            info = scenario_info(text, where=f"{sid}.md")
            if info.problem:
                raise RulesError(f"默认剧本文件本身有问题，没有恢复：{info.problem}", status=503, code="default_invalid")
            self._install(sid, text, origin=ORIGIN_DEFAULT)
            return self.source(sid)

    # ---------------------------------------------------------------------------------------------------- materials
    def _materials_dir(self, sid: str) -> Path:
        return self._scenario_dir(sid) / MATERIALS_DIRNAME

    def _check_slug(self, slug: Any) -> str:
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            raise RulesError(_SLUG_RULE)
        return slug

    def _material_slugs(self, sid: str) -> list[str]:
        folder = self._materials_dir(sid)
        try:
            names = sorted(p.name for p in folder.glob("*.md") if _plain_file(p)) if folder.is_dir() and not folder.is_symlink() else []
        except OSError:
            return []
        return [name[: -len(".md")] for name in names if SLUG_RE.match(name[: -len(".md")])]

    def materials(self, sid: str) -> dict[str, str]:
        """``{slug: whole text}`` of the study materials of ``sid``, by slug (empty when there are none; :class:`RulesNotFound` for
        an unknown scenario).  This is what the materials hash of the bots is made of."""
        with self._lock:
            self.scenario_path(sid)
            return {slug: botfiles.read_text(self._materials_dir(sid) / f"{slug}.md") for slug in self._material_slugs(sid)}

    def _title_of(self, sid: str, slug: str, text: str) -> str:
        try:
            raw = json.loads(botfiles.read_text(self._materials_dir(sid) / f"{slug}.json") or "null")
        except (ValueError, StorageError):
            raw = None
        title = raw.get("title") if isinstance(raw, dict) else None
        if isinstance(title, str) and title.strip():
            return re.sub(r"\s+", " ", title).strip()[:MATERIAL_TITLE_CHARS]
        return _derive_title(text, slug)

    def _info_of(self, sid: str, slug: str, text: str) -> dict[str, Any]:
        return {"slug": slug, "title": self._title_of(sid, slug, text), "chars": len(text), "sha256": sha256_text(text)}

    def material_infos(self, sid: str) -> list[dict[str, Any]]:
        """``[{"slug", "title", "chars", "sha256"}]`` by slug."""
        with self._lock:
            return [self._info_of(sid, slug, text) for slug, text in self.materials(sid).items()]

    def material_text(self, sid: str, slug: str) -> str:
        with self._lock:
            self.scenario_path(sid)
            path = self._materials_dir(sid) / f"{slug}.md" if isinstance(slug, str) and SLUG_RE.match(slug) else None
            if path is None or not _plain_file(path):
                raise RulesNotFound(f"没有这份学习材料：{str(slug)[:32]}")
            return botfiles.read_text(path)

    def material(self, sid: str, slug: str) -> dict[str, Any]:
        """One material: its info and its whole text as ``markdown``."""
        with self._lock:
            text = self.material_text(sid, slug)
            return {**self._info_of(sid, slug, text), "markdown": text}

    def set_material(self, sid: str, slug: str, markdown: Any, title: Any = None) -> dict[str, Any]:
        """Add or replace one study material (a whole replacement: a ``title`` left out is derived again from the text).  Checked first:
        the slug, the text (not empty, at most ``MAX_MATERIAL_BYTES``, no NUL), the title (one line, at most ``MATERIAL_TITLE_CHARS``),
        and at most ``MAX_MATERIALS`` materials.  Returns the info (:meth:`material_infos` item)."""
        with self._lock:
            self.scenario_path(sid)
            self._check_slug(slug)
            text = _checked(markdown, "学习材料")
            if not text.strip():
                raise RulesError("学习材料是空的。")
            size = len(text.encode("utf-8"))
            if size > MAX_MATERIAL_BYTES:
                raise RulesError(
                    f"学习材料最多 {MAX_MATERIAL_BYTES // 1024} KB（现在约 {_kib(size)} KB）。", status=413, code="too_large"
                )
            explicit: str | None = None
            if title is not None:
                if not isinstance(title, str):
                    raise RulesError("材料的标题必须是文字。")
                explicit = re.sub(r"\s+", " ", title).strip()
                if "\x00" in explicit:
                    raise RulesError("材料的标题里含有空字符。")
                if len(explicit) > MATERIAL_TITLE_CHARS:
                    raise RulesError(f"材料的标题最多 {MATERIAL_TITLE_CHARS} 字（现在 {len(explicit)} 字）。")
            slugs = self._material_slugs(sid)
            if slug not in slugs and len(slugs) >= MAX_MATERIALS:
                raise RulesError(f"每个剧本最多 {MAX_MATERIALS} 份学习材料，请先删掉一些。", code="too_many")
            folder = self._materials_dir(sid)
            botfiles.write_bytes(folder / f"{slug}.md", text.encode("utf-8"))
            sidecar = folder / f"{slug}.json"
            if explicit:
                botfiles.write_bytes(sidecar, (json.dumps({"title": explicit}, ensure_ascii=False) + "\n").encode("utf-8"))
            else:
                botfiles.remove_file(sidecar)
            return self._info_of(sid, slug, text)

    def delete_material(self, sid: str, slug: str) -> None:
        """Delete a material (:class:`RulesNotFound` when there is none)."""
        with self._lock:
            self.material_text(sid, slug)  # (404 first)
            folder = self._materials_dir(sid)
            botfiles.remove_file(folder / f"{slug}.md")
            botfiles.remove_file(folder / f"{slug}.json")

    # ---------------------------------------------------------------------------------------------------- RulesItem
    def item(self, sid: str) -> dict[str, Any]:
        """``RulesItem`` (contract §13.7): ``{"id", "version", "title", "origin", "imported_at", "sha256", "chars", "materials":
        [{"slug", "title", "chars", "sha256"}], "problem"}`` - ``problem`` (an addition) is the loader's explanation when the file in
        the data directory cannot be loaded (edited by hand), else ``null``."""
        with self._lock:
            text = self.scenario_text(sid)
            info = scenario_info(text, where=f"{sid}.md")
            source = self.source(sid)
            return {
                "id": sid,
                "version": source["version"],
                "title": info.title or sid,
                "origin": source["origin"],
                "imported_at": source["imported_at"],
                "sha256": source["sha256"],
                "chars": len(text),
                "materials": self.material_infos(sid),
                "problem": info.problem,
            }

    def items(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self.item(sid) for sid in self.ids()]

    # ---------------------------------------------------------------------------------------------------- the first run
    def ensure_installed(self) -> None:
        """Make the default scenario available (once per store): copied on the first run, copied again when it is missing, refreshed
        when it is an untouched default of an older program version.  A default that cannot be read or loaded is logged and skipped
        (the start check then says the scenario is missing).  Anything the user imported or edited is left alone."""
        with self._lock:
            if self._installed:
                return
            ensure_private_directory(self._root)
            sid = self.default_id()
            shipped = self._shipped_text() if sid else None
            if sid and shipped is not None:
                info = scenario_info(shipped, where=f"{sid}.md")
                if info.problem:
                    logger.warning("The default scenario cannot be loaded and is not installed: %s", info.problem)
                else:
                    self._sync_default(sid, shipped)
            elif sid:
                logger.warning("The default scenario file cannot be read; the rules store has no default")
            self._installed = True

    def _sync_default(self, sid: str, shipped: str) -> None:
        path = self._scenario_dir(sid) / SCENARIO_FILE
        current = botfiles.read_text(path) if _plain_file(path) else ""
        if not current.strip():
            self._install(sid, shipped, origin=ORIGIN_DEFAULT)
            return
        stored = self._read_source(sid)
        sha = sha256_text(current)
        if stored is not None and stored.get("origin") == ORIGIN_DEFAULT and stored.get("sha256") == sha and sha != sha256_text(shipped):
            self._install(sid, shipped, origin=ORIGIN_DEFAULT)  # (an untouched default of an older version)


# ======================================================================================================================
# the process-wide store
# ======================================================================================================================
_store: RulesStore | None = None
_store_lock = threading.Lock()


def get_rules_store() -> RulesStore:
    """The rules store of the data directory of this process.  The first call on a data directory installs the default scenario, so
    call it only once the data directory is locked (:func:`app.storage.datadir.ensure_data_dir`); a new store is made when the data
    directory changes (tests)."""
    global _store
    with _store_lock:
        data_dir = Path(get_data_dir()).resolve()
        if _store is None or _store.data_dir != data_dir:
            _store = RulesStore(data_dir)
        store = _store
    store.ensure_installed()
    return store


def reset_rules_store() -> None:
    global _store
    with _store_lock:
        _store = None


__all__ = [
    "MATERIAL_TITLE_CHARS",
    "MAX_MATERIALS",
    "MAX_MATERIAL_BYTES",
    "MAX_SCENARIOS",
    "MAX_SCENARIO_BYTES",
    "ORIGIN_DEFAULT",
    "ORIGIN_IMPORTED",
    "RulesError",
    "RulesNotFound",
    "RulesStore",
    "ScenarioInfo",
    "default_scenario_id",
    "default_scenario_path",
    "get_rules_store",
    "normalize_text",
    "reset_rules_store",
    "scenario_id_of_file",
    "scenario_info",
]
