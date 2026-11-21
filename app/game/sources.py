"""Where the official contract and the scenario of a game come from (v0.14, H7, H8, H38, H39; contract 13.2 / 13.13).

The engine reads both from the data directory (the *contract store* and the *rules store* of the files stream:
``app.storage.contract`` / ``app.storage.rules``); ``config.yml``'s ``scenario.path`` and ``prompts/contract.md`` are only the
defaults those stores copy on the first run.  Until a store exists - or while the data directory is not locked yet (the first game
is built in the constructor) - this class reads the defaults directly, so the orchestrator never needs to know which of the two it
is talking to.

Everything here is synchronous and cheap enough to be called for every ``bots_payload``: the texts are small, hashing them costs
microseconds, and the verdict about a scenario text is remembered by its hash (parsing the bundled scenario takes some 40 ms).
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import app.config as app_config
from app.agents.contract import load_default_contract, parse_contract, validate_contract
from app.bots.types import ContractDoc, SourceHashes, materials_hash, sha256_text
from app.world.loader import parse_scenario_v2
from app.world.spec import ScenarioError, ScenarioSpec

logger = logging.getLogger(__name__)

__all__ = ["SourceError", "Sources"]


class SourceError(ValueError):
    """The contract or the scenario cannot be used.  ``message`` is Chinese; ``kind`` is ``"contract"`` or ``"scenario"`` and
    ``code`` the code of the ``Problem`` item the start check reports (``contract_missing`` / ``contract_invalid`` /
    ``scenario_missing`` / ``scenario_invalid``)."""

    def __init__(self, message: str, *, kind: str, code: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code


def _store(module_name: str, getter: str) -> Any | None:
    """The store ``module_name.getter()`` makes, ``None`` when that module does not exist (yet)."""
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    make = getattr(module, getter, None)
    return make() if callable(make) else None


class Sources:
    """The official contract and the scenarios of this installation.

    ``cfg()`` returns ``config.yml`` as loaded (the default scenario's path lives in it); ``io_allowed()`` is false while the data
    directory must not be read yet."""

    def __init__(self, cfg: Callable[[], dict[str, Any]], io_allowed: Callable[[], bool] | None = None) -> None:
        self._cfg = cfg
        self._io_allowed = io_allowed or (lambda: True)
        self._verdicts: dict[str, str | None] = {}  # sha256 of a scenario text -> why it cannot be loaded (None: it can)
        self._default_id: tuple[tuple[str, int, int], str] | None = None

    # ------------------------------------------------------------------------------------------------ the stores
    def _contract_store(self) -> Any | None:
        return _store("app.storage.contract", "get_contract_store") if self._io_allowed() else None

    def _rules_store(self) -> Any | None:
        return _store("app.storage.rules", "get_rules_store") if self._io_allowed() else None

    # ------------------------------------------------------------------------------------------------ the contract
    def contract(self) -> ContractDoc:
        """The official contract in force.  :class:`SourceError` (``contract_missing``) when there is none."""
        try:
            store = self._contract_store()
            if store is not None:
                doc = store.current()
                if isinstance(doc, ContractDoc) and doc.markdown.strip():
                    return doc
                raise SourceError("官方契约是空的，请在设置里导入契约或恢复默认。", kind="contract", code="contract_missing")
            return load_default_contract()
        except SourceError:
            raise
        except (OSError, ValueError) as exc:
            raise SourceError(f"读不到官方契约：{exc}", kind="contract", code="contract_missing") from exc
        except Exception as exc:  # noqa: BLE001 - a store that fails is "no contract", never a crash of the page
            logger.warning("The contract store failed", exc_info=True)
            raise SourceError(f"读不到官方契约（{type(exc).__name__}）。", kind="contract", code="contract_missing") from exc

    def contract_from_snapshot(self, snapshot: dict[str, Any]) -> ContractDoc:
        """The contract a game started with (``GameSession.contract_snapshot``)."""
        return parse_contract(str(snapshot.get("markdown") or ""))

    @staticmethod
    def snapshot_of(doc: ContractDoc) -> dict[str, Any]:
        return {"version": doc.version, "sha256": doc.sha256, "markdown": doc.markdown}

    def contract_problem(self, spec: ScenarioSpec | None) -> SourceError | None:
        """Why the contract in force cannot be used with ``spec`` (``None``: it can)."""
        try:
            doc = self.contract()
        except SourceError as exc:
            return exc
        problems = validate_contract(doc.markdown, spec=spec)
        if problems:
            return SourceError("官方契约有问题：" + " ".join(problems), kind="contract", code="contract_invalid")
        return None

    # ------------------------------------------------------------------------------------------------ the scenario
    def config_scenario_path(self) -> Path:
        scenario_cfg = self._cfg().get("scenario") or {}
        path = scenario_cfg.get("path") or scenario_cfg.get("document")
        if not path:
            raise ScenarioError("config.scenario.path is not set")
        target = Path(str(path))
        return target if target.is_absolute() else Path(app_config.BASE_DIR) / target

    def _config_text(self) -> str:
        path = self.config_scenario_path()
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SourceError(f"读不到剧本文件 {path.name}：{exc}", kind="scenario", code="scenario_missing") from exc

    def default_scenario_id(self) -> str:
        """The scenario a new game plays: the one ``config.yml`` names (the rules store keeps a copy of it under its own id)."""
        path = self.config_scenario_path()
        try:
            stat = path.stat()
            key = (str(path), stat.st_mtime_ns, stat.st_size)
        except OSError as exc:
            raise SourceError(f"找不到剧本文件：{path}", kind="scenario", code="scenario_missing") from exc
        if self._default_id is not None and self._default_id[0] == key:
            return self._default_id[1]
        text = self._config_text()
        try:
            scenario_id = parse_scenario_v2(text, where=path.name).id
        except ScenarioError as exc:
            raise SourceError(str(exc), kind="scenario", code="scenario_invalid") from exc
        self._default_id = (key, scenario_id)
        return scenario_id

    def scenario_ids(self) -> list[str]:
        store = self._rules_store()
        if store is not None:
            try:
                return [str(i) for i in store.ids()]
            except Exception:  # noqa: BLE001
                logger.warning("The rules store failed", exc_info=True)
                return []
        try:
            return [self.default_scenario_id()]
        except SourceError:
            return []

    def current_scenario_id(self) -> str:
        """The scenario of the next new game: the config's default when the rules store has it, else the store's first."""
        wanted, why = "", None
        try:
            wanted = self.default_scenario_id()
        except SourceError as exc:
            why = exc
        store = self._rules_store()
        if store is None:
            if wanted:
                return wanted
            raise why or SourceError("找不到剧本：config.yml 里的 scenario.path 不可用。", kind="scenario", code="scenario_missing")
        ids = self.scenario_ids()
        if wanted and wanted in ids:
            return wanted
        if ids:
            return ids[0]
        raise SourceError("没有可用的剧本，请在设置里导入剧本或恢复默认。", kind="scenario", code="scenario_missing")

    def scenario_text(self, scenario_id: str | None = None) -> str:
        """The scenario file of ``scenario_id`` (default: :meth:`current_scenario_id`), the whole markdown."""
        sid = scenario_id or self.current_scenario_id()
        store = self._rules_store()
        if store is None:
            return self._config_text()
        try:
            text = store.scenario_text(sid)
        except Exception as exc:  # noqa: BLE001 - an unknown id, an unreadable file: "missing" for the start check
            raise SourceError(f"读不到剧本 {sid}：{exc}", kind="scenario", code="scenario_missing") from exc
        if not isinstance(text, str) or not text.strip():
            raise SourceError(f"剧本 {sid} 是空的。", kind="scenario", code="scenario_missing")
        return text

    def spec(self, scenario_id: str | None = None) -> ScenarioSpec:
        """The loaded scenario (:class:`SourceError` when it is missing or invalid)."""
        text = self.scenario_text(scenario_id)
        try:
            spec = parse_scenario_v2(text, where=f"{scenario_id or 'scenario'}.md")
        except ScenarioError as exc:
            self._verdicts[sha256_text(text)] = str(exc)
            raise SourceError(str(exc), kind="scenario", code="scenario_invalid") from exc
        self._verdicts[sha256_text(text)] = None
        return spec

    def scenario_problem(self, scenario_id: str | None = None) -> SourceError | None:
        """Why the scenario cannot be used (``None``: it loads).  The verdict is remembered by the hash of the text."""
        try:
            text = self.scenario_text(scenario_id)
        except SourceError as exc:
            return exc
        digest = sha256_text(text)
        if digest not in self._verdicts:
            try:
                parse_scenario_v2(text, where=f"{scenario_id or 'scenario'}.md")
                self._verdicts[digest] = None
            except ScenarioError as exc:
                self._verdicts[digest] = str(exc)
            if len(self._verdicts) > 64:
                self._verdicts.pop(next(iter(self._verdicts)))
        why = self._verdicts[digest]
        return SourceError(why, kind="scenario", code="scenario_invalid") if why else None

    def materials(self, scenario_id: str | None = None) -> dict[str, str]:
        """The study materials of the scenario (``{slug: whole text}``; empty by default, C10)."""
        store = self._rules_store()
        if store is None:
            return {}
        try:
            return {str(k): str(v) for k, v in dict(store.materials(scenario_id or self.current_scenario_id())).items()}
        except Exception:  # noqa: BLE001 - no materials is the normal case
            logger.warning("Could not read the study materials", exc_info=True)
            return {}

    # ------------------------------------------------------------------------------------------------ hashes
    def hashes(self, scenario_id: str | None = None) -> SourceHashes:
        """What an initialization depends on, for ``BotStore`` (``ready`` / ``stale``) and for stamping a new initialization.  Never
        raises: a contract or scenario that cannot be read has the empty hash (nothing initialized matches it)."""
        try:
            sid = scenario_id or self.current_scenario_id()
        except SourceError:
            sid = scenario_id or ""
        contract = scenario = materials = ""
        try:
            contract = self.contract().sha256
        except SourceError:
            pass
        if sid:
            try:
                scenario = sha256_text(self.scenario_text(sid))
            except SourceError:
                pass
            materials = materials_hash(self.materials(sid))
        return SourceHashes(contract=contract, scenario=scenario, materials=materials, scenario_id=sid)
