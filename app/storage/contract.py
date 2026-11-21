"""The official contract in the data directory (v0.14; H7, H38; docs/v0.12-contracts.md §13.2, §13.7).

    <data>/contract/contract.md    the contract in force: front matter, ``## 必读`` (mounted into every bot's prompt), ``## 参考``
    <data>/contract/source.json    {"origin": "default|imported", "version", "imported_at": <unix s>|null, "sha256"}

The engine reads the contract from here (``app.game.sources``); ``prompts/contract.md`` is only the *default* that is copied here on
the first run and again by "恢复默认".  A default that was never changed is refreshed when a newer program ships another one (every
bot then shows "需要重新初始化" by itself: the hash changed); one the user imported - or edited by hand in the data directory - is theirs
and is never touched.

What a contract has to be (the check is mind's :func:`app.agents.contract.validate_contract`; this store only adds what saving needs):
the front matter ``format: arena-contract``, a ``## 必读`` part of at most ``CONTRACT_MUST_READ_CHARS`` characters, a ``## 参考`` part, at
most ``CONTRACT_MAX_CHARS`` characters in all, and a mention of every tool a bot has and every receipt code.  A contract that fails is
refused with *every* problem (:class:`ContractError`) and nothing is written.

Texts are stored with LF line ends, without a byte-order mark and with exactly one final newline (:func:`app.storage.rules.normalize_text`),
so that the hash does not depend on the editor that saved the file; the shipped ``prompts/contract.md`` is in that form (a test keeps it so),
which is what makes the hash of a default bot's initialization match the data directory's copy.  Reading is always from the file (a few
kilobytes): an edit made outside the program is seen at once and nothing is cached.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from app.bots.types import ContractDoc, sha256_text
from app.storage import botfiles
from app.storage._atomic import ensure_private_directory
from app.storage.errors import StorageError
from app.storage.paths import get_data_dir
from app.storage.rules import ORIGIN_DEFAULT, ORIGIN_IMPORTED, normalize_text

logger = logging.getLogger(__name__)

CONTRACT_DIRNAME = "contract"
CONTRACT_FILE = "contract.md"
SOURCE_FILE = "source.json"
CUSTOM_VERSION = "custom"


class ContractError(ValueError):
    """A contract that cannot be saved.  ``problems`` are Chinese sentences, one per fault (``str(error)`` joins them);
    ``status`` / ``code`` are what the HTTP layer answers with."""

    status = 400
    code = "invalid"

    def __init__(self, problems: Any, *, status: int | None = None, code: str | None = None) -> None:
        items = [str(problems)] if isinstance(problems, str) else [str(item) for item in problems]
        items = [item for item in items if item] or ["契约无效。"]
        super().__init__(" ".join(items))
        self.problems: list[str] = items
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code


def _unix(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value > 0 else None


def _agent_contract() -> Any:
    """``app.agents.contract`` (mind's checks and the default contract), imported when it is needed: it pulls in the agent package."""
    from app.agents import contract as agent_contract

    return agent_contract


class ContractStore:
    """The contract of one data directory.  ``default_path`` is the contract file to install as the default (``None``:
    ``prompts/contract.md`` of the program directory); ``clock`` returns the time in seconds."""

    def __init__(self, data_dir: Path, *, default_path: Path | None = None, clock: Any = time.time) -> None:
        self._data_dir = Path(data_dir).resolve()
        self._root = self._data_dir / CONTRACT_DIRNAME
        self._default_path = Path(default_path) if default_path is not None else None
        self._clock = clock
        self._lock = threading.RLock()
        self._installed = False

    # ---------------------------------------------------------------------------------------------------- paths
    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def path(self) -> Path:
        """``contract/contract.md`` (it need not exist)."""
        return self._root / CONTRACT_FILE

    def _now(self) -> int:
        return int(self._clock())

    def _read_text(self) -> str:
        path = self.path
        try:
            if path.is_symlink() or not path.is_file():
                return ""
        except OSError:
            return ""
        return botfiles.read_text(path)

    # ---------------------------------------------------------------------------------------------------- the shipped default
    def default_path(self) -> Path:
        if self._default_path is not None:
            return self._default_path
        return _agent_contract().default_contract_path()

    def default_text(self) -> str | None:
        """The shipped default contract, normalized; ``None`` when its file cannot be read."""
        text = botfiles.read_text(self.default_path())
        return normalize_text(text) if text.strip() else None

    def default(self) -> ContractDoc:
        """The shipped default contract.  :class:`FileNotFoundError` when its file cannot be read."""
        text = self.default_text()
        if text is None:
            raise FileNotFoundError(f"找不到默认契约文件：{self.default_path().name}")
        return _agent_contract().parse_contract(text)

    def _is_shipped(self, sha: str) -> bool:
        shipped = self.default_text()
        return shipped is not None and sha256_text(shipped) == sha

    # ---------------------------------------------------------------------------------------------------- reading
    def current(self) -> ContractDoc:
        """The contract in force, read from the data directory.  An empty ``ContractDoc`` (no markdown) when there is none - the
        file was deleted by hand; the start check says so and "恢复默认" brings it back."""
        with self._lock:
            text = self._read_text()
            if not text.strip():
                return ContractDoc(version=CUSTOM_VERSION)
            return _agent_contract().parse_contract(text)

    def _read_source(self) -> dict[str, Any] | None:
        try:
            raw = json.loads(botfiles.read_text(self._root / SOURCE_FILE) or "null")
        except (ValueError, StorageError):
            return None
        return raw if isinstance(raw, dict) else None

    def source(self) -> dict[str, Any]:
        """``{"origin": "default|imported", "version", "imported_at": <unix s>|null, "sha256"}`` of the contract as it is *now*: the
        hash is the file's own (``ContractDoc.sha256``), so a file edited by hand in the data directory is reported as imported (not as
        the default it once was) with the time of its last change.  ``version`` is the one the front matter names (``custom`` when it
        names none)."""
        with self._lock:
            text = self._read_text()
            sha = sha256_text(text)
            stored = self._read_source()
            if stored is not None and stored.get("sha256") == sha and stored.get("origin") in (ORIGIN_DEFAULT, ORIGIN_IMPORTED):
                origin, imported_at = str(stored["origin"]), _unix(stored.get("imported_at"))
            elif text.strip() and self._is_shipped(sha):
                origin, imported_at = ORIGIN_DEFAULT, None
            else:
                origin = ORIGIN_IMPORTED
                try:
                    imported_at = int(self.path.stat().st_mtime) if text.strip() else None
                except OSError:
                    imported_at = None
            version = _agent_contract().contract_version(text) if text.strip() else CUSTOM_VERSION
            return {"origin": origin, "version": version, "imported_at": imported_at, "sha256": sha}

    def validate(self, markdown: Any) -> list[str]:
        """Every reason this text cannot be the contract (``[]`` = it can), in Chinese: mind's :func:`validate_contract` (front matter,
        the two parts, the length of ``必读``, the tools and receipt codes it does not mention, the size), after the text has been
        normalized.  Nothing is written."""
        if markdown is not None and not isinstance(markdown, str):
            return ["契约必须是文字。"]
        text = normalize_text(markdown)
        if "\x00" in text:
            return ["契约里含有空字符，不能保存。"]
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            return ["契约里含有无法保存的字符（不成对的 Unicode 代理字符）。"]
        return list(_agent_contract().validate_contract(text))

    def must_read_chars(self) -> int:
        return len(self.current().must_read)

    # ---------------------------------------------------------------------------------------------------- writing
    def _install(self, text: str, *, origin: str) -> None:
        ensure_private_directory(self._root)
        botfiles.write_bytes(self.path, text.encode("utf-8"))
        data = {
            "origin": origin,
            "version": _agent_contract().contract_version(text),
            "imported_at": None if origin == ORIGIN_DEFAULT else self._now(),
            "sha256": sha256_text(text),
        }
        botfiles.write_bytes(self._root / SOURCE_FILE, (json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))

    def set(self, markdown: Any) -> dict[str, Any]:
        """Replace the contract: checked first (:meth:`validate`; :class:`ContractError` with every problem and nothing written), then
        written atomically.  Returns :meth:`source`.  Every bot's initialization becomes ``stale`` (the hash changed)."""
        problems = self.validate(markdown)
        if problems:
            raise ContractError(problems)
        text = normalize_text(markdown)
        with self._lock:
            origin = ORIGIN_DEFAULT if self._is_shipped(sha256_text(text)) else ORIGIN_IMPORTED
            self._install(text, origin=origin)
            return self.source()

    def reset(self) -> dict[str, Any]:
        """Bring the default contract back (``prompts/contract.md``, copied again).  :class:`ContractError` (503) when that file cannot
        be read."""
        with self._lock:
            text = self.default_text()
            if text is None:
                raise ContractError("找不到默认契约文件，无法恢复默认。", status=503, code="default_missing")
            self._install(text, origin=ORIGIN_DEFAULT)
            return self.source()

    # ---------------------------------------------------------------------------------------------------- the first run
    def ensure_installed(self) -> None:
        """Make the default contract available (once per store): copied on the first run, copied again when it is missing, refreshed
        when it is an untouched default of an older program version.  A default file that cannot be read is logged and skipped (the
        start check then says the contract is missing).  Anything the user imported or edited is left alone."""
        with self._lock:
            if self._installed:
                return
            ensure_private_directory(self._root)
            shipped = self.default_text()
            if shipped is None:
                logger.warning("The default contract file cannot be read; the contract store has no default")
            else:
                current = self._read_text()
                if not current.strip():
                    self._install(shipped, origin=ORIGIN_DEFAULT)
                else:
                    stored = self._read_source()
                    sha = sha256_text(current)
                    if (
                        stored is not None
                        and stored.get("origin") == ORIGIN_DEFAULT
                        and stored.get("sha256") == sha
                        and sha != sha256_text(shipped)
                    ):
                        self._install(shipped, origin=ORIGIN_DEFAULT)  # (an untouched default of an older version)
            self._installed = True

    # ---------------------------------------------------------------------------------------------------- download name
    def download_label(self) -> str:
        """The part of the download file name that says which contract it is (H42): the version of the front matter (``v0.14.0``), or
        ``custom-YYYYmmdd`` (the day it was imported) for one that names none."""
        source = self.source()
        version = source["version"]
        if version != CUSTOM_VERSION and re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$", version):
            return version
        stamp = source["imported_at"] or self._now()
        return "custom-" + time.strftime("%Y%m%d", time.localtime(stamp))


# ======================================================================================================================
# the process-wide store
# ======================================================================================================================
_store: ContractStore | None = None
_store_lock = threading.Lock()


def get_contract_store() -> ContractStore:
    """The contract store of the data directory of this process.  The first call on a data directory installs the default contract,
    so call it only once the data directory is locked (:func:`app.storage.datadir.ensure_data_dir`); a new store is made when the
    data directory changes (tests)."""
    global _store
    with _store_lock:
        data_dir = Path(get_data_dir()).resolve()
        if _store is None or _store.data_dir != data_dir:
            _store = ContractStore(data_dir)
        store = _store
    store.ensure_installed()
    return store


def reset_contract_store() -> None:
    global _store
    with _store_lock:
        _store = None


__all__ = [
    "CONTRACT_DIRNAME",
    "CONTRACT_FILE",
    "CUSTOM_VERSION",
    "ContractError",
    "ContractStore",
    "get_contract_store",
    "reset_contract_store",
]
