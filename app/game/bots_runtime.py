"""The bots at the table: who sits where, whether they are ready, what they take into a game (v0.14).

Mixed into :class:`~app.game.orchestrator.GameOrchestrator`; contract docs/v0.12-contracts.md §13.3 (dealing, the v6 save), §13.7
(``BotsPayload`` / ``BotSummary`` / ``Problem``) and §13.8 (events).  It replaces the roster of v0.13 (``roster_runtime``):

* **the table** - the line-up of the bot store (``BotStore.lineup()``) says which bot sits in which seat; ``config.yml`` only says how
  many seats there are and what they are called (``N1`` ...).  A game that has not started takes the table as it is *now*
  (:meth:`_lineup_players`, :meth:`apply_table`); a started game keeps the table it started with;
* **the start check** (H25, :meth:`check_ready`): every bot has a complete L0 and a finished, current initialization, the contract and
  the scenario can be used, every actor has a model.  A game that does not pass is not started and nothing is dealt
  (:class:`NotReadyError`, HTTP 409 ``NOT_READY`` with the ``problems``);
* **the snapshots** (H11, :meth:`take_snapshots`): when the game starts, what each bot carries into it (``BotMind.to_dict()``) and the
  official contract are written into the game, and every brain is built from them - so a game that is paused and resumed, or a
  finished one that is continued, builds exactly the prompts it started with;
* **the objects of the API** (:meth:`bots_payload`, :meth:`bot_detail`, :meth:`system_status`) and the events they travel in.

Everything here is synchronous: the bot store is a few small files, read on the event loop like the roster was.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.agents.brain import ArenaBrain
from app.agents.legacy import LegacyBrain
from app.agents.mind import BotBrain
from app.bots.types import BotMind, ContractDoc, SourceHashes, sha256_text
from app.game.bindings import BindingError
from app.game.session import MODERATOR_ID, GameSession, Participant
from app.game.sources import SourceError, Sources
from app.world.types import UiEvent

logger = logging.getLogger(__name__)

__all__ = ["BotsRuntimeMixin", "NotReadyError", "StartSnapshots", "TableRead", "not_ready_text"]

#: How a pause is explained in the status label (``system_status.reason``).
PAUSE_TEXTS = {
    "user": "手动暂停",
    "hold": "主持人暂停了对局",
    "stall": "对局长时间没有进展，已暂停",
    "error": "运行出错，已暂停",
    "budget": "已达到预算上限，已暂停",
}


def not_ready_text(problems: Sequence[dict[str, Any]]) -> str:
    """One Chinese sentence for ``NOT_READY`` (what the dialog shows in full is in ``problems``)."""
    parts: list[str] = []
    for problem in problems[:3]:
        who = problem.get("name") or {"contract": "官方契约", "scenario": "剧本", "binding": "模型绑定", "game": "对局"}.get(
            str(problem.get("scope")), ""
        )
        items = problem.get("items") or []
        first = str(items[0].get("message") if items else "").strip().rstrip("。")
        parts.append(f"{who}：{first}" if who else first)
    more = f"（共 {len(problems)} 处）" if len(problems) > 3 else ""
    return "还没有准备好，不能开局：" + "；".join(p for p in parts if p) + more + "。"


class NotReadyError(RuntimeError):
    """The game cannot start yet (H25): ``problems`` are the ``Problem`` objects of contract 13.7, grouped by player / contract /
    scenario / model binding.  The API answers 409 ``{"detail", "code": "NOT_READY", "problems"}``; nothing was dealt."""

    status = 409
    code = "NOT_READY"

    def __init__(self, problems: list[dict[str, Any]]) -> None:
        self.problems = problems
        super().__init__(not_ready_text(problems))


@dataclass
class StartSnapshots:
    """What a game that is starting takes from the table: ``minds`` (``{seat id: snapshot}``, each ``BotMind.to_dict()`` plus the
    hashes of the contract and the scenario), the official ``contract`` and whether the bots' learned memory is on."""

    minds: dict[str, dict[str, Any]]
    contract: ContractDoc
    memory_enabled: bool


@dataclass
class TableRead:
    """One read of the table (the line-up and every bot's summary, with the seat and the model binding added): what ``bots_payload``,
    ``system_status`` and the cards of ``public_session`` all need - read once, because every summary is a handful of small files."""

    store: Any
    lineup: list[str]
    summaries: list[dict[str, Any]]
    memory_enabled: bool

    def init_statuses(self) -> dict[str, str]:
        return {str(row["id"]): str(row["init"]["status"]) for row in self.summaries}

    def uninitialized(self) -> bool:
        """Some bot has no current initialization (never, stale or failed)."""
        return any(status != "ready" for status in self.init_statuses().values())


class BotsRuntimeMixin:
    # Class-level defaults so partially initialized instances (tests) stay valid.
    bot_store: Any = None  # injectable (tests); ``None`` = the data directory's store
    _table_io_suspended: bool = False
    _memory_enabled_snapshot: bool = True
    _moderator_avatar_snapshot: dict[str, Any] | None = None
    _sources_hub: Sources | None = None

    def demo_prepare(self) -> list[str]:
        """Demo mode (``app.game.demo.install_demo`` replaces this): give the bots that were never initialized their fixed memory.  A no-op otherwise."""
        return []

    # ------------------------------------------------------------------------------------------------ the stores
    def _seat_ids(self) -> list[str]:
        return [str(p["id"]) for p in self.cfg.get("participants", [])]  # type: ignore[attr-defined]

    def _table_io_allowed(self) -> bool:
        """No bot file is read or created while the first game is built (the data directory is not locked yet); ``startup()``
        takes the table afterwards."""
        return not self._table_io_suspended

    def _bots(self) -> Any:
        """The bot store (``get_bot_store`` creates the table of a data directory on the first call: the data directory must be
        locked by then)."""
        if self.bot_store is not None:
            return self.bot_store
        from app.storage.bots import get_bot_store

        return get_bot_store(len(self._seat_ids()) or 12)

    def _bots_or_none(self) -> Any | None:
        if not self._table_io_allowed():
            return None
        try:
            return self._bots()
        except Exception:  # noqa: BLE001 - a table that cannot be read is a problem to show, never a crash of the page
            logger.warning("Could not open the bot store", exc_info=True)
            return None

    @property
    def sources(self) -> Sources:
        """The official contract and the scenarios (:mod:`app.game.sources`)."""
        hub = self._sources_hub
        if hub is None:
            hub = self._sources_hub = Sources(lambda: self.cfg, self._table_io_allowed)  # type: ignore[attr-defined]
        return hub

    def source_hashes(self) -> SourceHashes:
        """What an initialization depends on (apart from the bot's own L0): the hashes of the contract, the scenario and its study
        materials as they are now."""
        return self.sources.hashes()

    # ------------------------------------------------------------------------------------------------ seats
    def _seat_of(self, bot_id: str) -> str | None:
        """The seat id of ``bot_id`` in the table now (``None`` when it does not sit at it)."""
        store = self._bots_or_none()
        if store is None:
            return None
        try:
            order = store.lineup()
        except Exception:  # noqa: BLE001
            return None
        seats = self._seat_ids()
        return seats[order.index(bot_id)] if bot_id in order and order.index(bot_id) < len(seats) else None

    def _lineup_players(self) -> list[dict[str, Any]]:
        """The seats of a new game: ``config.yml`` says how many there are and what they are called (``id`` / ``seat``), the table says
        who sits in them (name, avatar, the bot).  Without a readable table the seats keep the names of the config and have no bot
        (the start check refuses such a game)."""
        config_players = list(self.cfg.get("participants", []))  # type: ignore[attr-defined]
        store = self._bots_or_none()
        order: list[str] = []
        summaries: dict[str, dict[str, Any]] = {}
        moderator_avatar: dict[str, Any] | None = None
        memory_enabled = True
        if store is not None:
            try:
                order = list(store.lineup())
                for bot_id in order[: len(config_players)]:
                    summaries[bot_id] = store.summary(bot_id)
                moderator_avatar = store.moderator().get("avatar")
                memory_enabled = bool(store.memory_enabled())
            except Exception:  # noqa: BLE001 - never block a new game on a table problem
                logger.warning("Could not read the table of bots", exc_info=True)
                order, summaries = [], {}
        players: list[dict[str, Any]] = []
        for index, item in enumerate(config_players, start=1):
            bot_id = order[index - 1] if index - 1 < len(order) else ""
            row = summaries.get(bot_id) or {}
            players.append(
                {
                    "id": str(item["id"]),
                    "seat": str(item.get("seat", index)),
                    "name": str(row.get("name") or item.get("name") or item["id"]),
                    "persona": "",
                    "memory": "",
                    "avatar": row.get("avatar") if isinstance(row.get("avatar"), dict) else None,
                    "bot_id": bot_id if row else "",
                }
            )
        self._memory_enabled_snapshot = memory_enabled
        self._moderator_avatar_snapshot = moderator_avatar if isinstance(moderator_avatar, dict) else None
        return players

    def _roster_event(self) -> UiEvent:
        """``roster.updated`` (§13.8): the names and avatars of the seats and the moderator's avatar changed."""
        return UiEvent(
            type="roster.updated",
            data={
                "participants": [{"id": p.id, "name": p.name, "avatar": p.avatar} for p in self.session.participants],  # type: ignore[attr-defined]
                "moderator": {"id": MODERATOR_ID, "avatar": self.session.moderator_avatar},  # type: ignore[attr-defined]
            },
        )

    def _bots_event(self) -> UiEvent:
        """``bots.updated`` (§13.8): the line-up, the states or the memory statistics of the bots changed."""
        return UiEvent(type="bots.updated", data={"bots": self.bots_payload()})

    def publish_bots_updated(self) -> None:
        """Tell the clients the bots changed (``bots.updated``); never raises - a table that cannot be read is told by the next request."""
        try:
            self.bus.publish(self._bots_event())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.warning("Could not publish bots.updated", exc_info=True)

    # ------------------------------------------------------------------------------------------------ the table of an idle game
    def apply_table(self) -> None:
        """Take the table as it is now into the game that has not started: seats (names, avatars, bots), the line-up, the memory
        switch, the moderator's avatar and the model bindings (they follow the bots, H4).  The world is built again with the *same
        seed*, and a person who plays a seat keeps it.  Only for ``idle``; the caller says so to the clients."""
        session = self.session  # type: ignore[attr-defined]
        seed = self.world.state.rng.seed  # type: ignore[attr-defined]
        players = self._lineup_players()
        world, clock = self._new_world(self.spec, seed=seed, players=players)  # type: ignore[attr-defined]
        self.world, self.clock = world, clock  # type: ignore[attr-defined]
        session.participants = [Participant(**p) for p in players]
        session.lineup = [p["bot_id"] for p in players if p["bot_id"]]
        session.memory_enabled = bool(self._memory_enabled_snapshot)
        session.moderator_avatar = self._moderator_avatar_snapshot
        self._derive_bindings(session)
        self._reset_conversations()  # type: ignore[attr-defined]

    def _adopt_table_of_idle_game(self) -> None:
        """A v6 game that has never been started takes the table (and the scenario) as they are now: until a game starts, the bots, their
        seats and the scenario belong to the table, not to the game (H29, H39).  A game that has started keeps what it started with."""
        session = self.session  # type: ignore[attr-defined]
        if session.status != "idle" or session.legacy or not self._table_io_allowed():
            return
        try:
            if self.default_spec.model_dump(mode="json") != self.spec.model_dump(mode="json"):  # type: ignore[attr-defined]
                self.spec = self.default_spec  # type: ignore[attr-defined]
                session.scenario_name = self.spec.id  # type: ignore[attr-defined]
                session.scenario_title = self.spec.title  # type: ignore[attr-defined]
                session.scenario_spec = self.spec.model_dump(mode="json")  # type: ignore[attr-defined]
            self.apply_table()
        except Exception:  # noqa: BLE001 - the game stays as it was saved; the start check says what is wrong
            logger.warning("Could not take the table into the game that was loaded", exc_info=True)

    def _reload_fresh_game_from_data_dir(self) -> None:
        """At startup the first (idle) game was built before the data directory was locked: take the table, the scenario in force and the
        budget default from it now.  Nothing is saved."""
        if self.session.status != "idle":  # type: ignore[attr-defined]
            return
        self._adopt_table_of_idle_game()
        self.session.budget = self._load_budget_defaults()  # type: ignore[attr-defined]

    def _derive_bindings(self, session: GameSession) -> None:
        """The model bindings of an idle game are the saved defaults of the bots in their seats (they follow the bots)."""
        defaults = self._load_defaults()  # type: ignore[attr-defined]
        allowed = {MODERATOR_ID, *(p.id for p in session.participants)}
        session.model_bindings = {a: b.model_copy(deep=True) for a, b in defaults.items() if a in allowed}
        self.refresh_binding_issues()  # type: ignore[attr-defined]

    async def bots_changed(self, *, roster: bool = False) -> None:
        """Something about the bots changed (the API of the bots calls this after every change): an idle game takes the new table
        (``roster``: names, avatars, seats or the moderator's avatar changed - the world is rebuilt, ``roster.updated`` is sent and the
        clients get a fresh snapshot), and the clients get ``bots.updated``."""
        session = self.session  # type: ignore[attr-defined]
        if session.status == "idle" and not session.legacy and self._table_io_allowed():
            try:
                if roster:
                    self.apply_table()
                    await self.broadcast_state("table_changed")  # type: ignore[attr-defined]  # a fresh snapshot, no autosave churn
                    self.bus.publish(self._roster_event())  # type: ignore[attr-defined]
                else:
                    session.memory_enabled = bool(self._bots().memory_enabled())
                    self.refresh_binding_issues()  # type: ignore[attr-defined]
                    await self.broadcast_state("bots_updated")  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - the change itself is done; the page can still ask for the state again
                logger.warning("Could not take the new table into the game", exc_info=True)
        self.publish_bots_updated()

    async def sources_changed(self, kind: str = "all", scenario_id: str | None = None) -> None:
        """The contract, a scenario or the study materials of a scenario changed - the contract / rules stores' API calls this
        (``kind``: ``"contract"`` | ``"scenario"`` | ``"materials"``; ``scenario_id``: which one, ``None`` for the contract).  Every initialization may
        have become ``stale`` (the hashes changed: ``bots.updated``); a *scenario* that changed is also what a game that has not started plays
        (the world is built again, H39) - the contract and the materials do not change a world.  A game that has started keeps the contract and
        the scenario it started with."""
        session = self.session  # type: ignore[attr-defined]
        if kind in ("scenario", "all") and session.status == "idle" and not session.legacy and not self._is_active():  # type: ignore[attr-defined]
            try:
                if self.sync_idle_game():
                    await self.broadcast_state("table_changed")  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                logger.warning("Could not take the new scenario into the game", exc_info=True)
        self.publish_bots_updated()

    def sync_idle_game(self) -> bool:
        """Make the idle game the game of the table as it is now: the scenario in force (the default spec is read again) and the bots in
        their seats.  When either differs from what the game was built with, the world is built again (same seed).  ``True`` when it was.
        A scenario that cannot be loaded (or does not fit the table) changes nothing - the start check says why."""
        session = self.session  # type: ignore[attr-defined]
        try:
            self.reload_config()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - the previous one stays in force
            logger.warning("Could not read the scenario again", exc_info=True)
            return False
        new_spec = self.default_spec.model_dump(mode="json") != self.spec.model_dump(mode="json")  # type: ignore[attr-defined]
        wanted = self._lineup_players()
        new_table = [(p["id"], p["bot_id"], p["name"], p["avatar"]) for p in wanted] != [
            (p.id, p.bot_id, p.name, p.avatar) for p in session.participants
        ]
        if not new_spec and not new_table:
            return False
        previous = (self.spec, session.scenario_name, session.scenario_title, session.scenario_spec)  # type: ignore[attr-defined]
        if new_spec:
            self.spec = self.default_spec  # type: ignore[attr-defined]
            session.scenario_name = self.spec.id  # type: ignore[attr-defined]
            session.scenario_title = self.spec.title  # type: ignore[attr-defined]
            session.scenario_spec = self.spec.model_dump(mode="json")  # type: ignore[attr-defined]
        try:
            self.apply_table()
        except ValueError:
            logger.warning("The scenario in force does not fit the table", exc_info=True)
            self.spec, session.scenario_name, session.scenario_title, session.scenario_spec = previous  # type: ignore[attr-defined]
            return False
        return True

    # ------------------------------------------------------------------------------------------------ bindings of the bots
    def _bot_binding(self, bot_id: str) -> Any | None:
        """The default model binding of one bot (what its initialization uses, H20): the settings store keeps it by bot id."""
        settings = self._settings()  # type: ignore[attr-defined]
        getter = getattr(settings, "get_bot_binding", None)
        try:
            if callable(getter):
                return getter(bot_id)
            seat = self._seat_of(bot_id)  # a settings store that only knows seats (a fake)
            return (self._load_defaults() or {}).get(seat) if seat else None  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.warning("Could not read the binding of %s", bot_id, exc_info=True)
            return None

    def _binding_view(self, binding: Any | None, cache: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
        """``BotSummary.binding``: ``{"ok", "label"}`` of a model binding (resolved once per distinct binding)."""
        if getattr(self, "demo_mode", False):
            return {"ok": True, "label": "演示模式"}
        if binding is None:
            return {"ok": False, "label": "未绑定模型"}
        key = (str(getattr(binding, "provider_id", "")), str(getattr(binding, "model_id", "")))
        if key in cache:
            return cache[key]
        try:
            provider, _model = self._providers().resolve(binding)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - the provider or the model is gone
            view = {"ok": False, "label": f"{key[0]} / {key[1]}（已不存在）"}
        else:
            label = f"{getattr(provider, 'name', '') or key[0]} / {key[1]}"
            if not str(getattr(provider, "api_key", "") or "").strip():
                view = {"ok": False, "label": f"{label}（未设置 API Key）"}
            else:
                view = {"ok": True, "label": label}
        cache[key] = view
        return view

    # ------------------------------------------------------------------------------------------------ the start check (H25)
    @staticmethod
    def _problem(scope: str, code: str, message: str, fix: str, **who: Any) -> dict[str, Any]:
        return {
            "scope": scope,
            "bot_id": who.get("bot_id"),
            "seat": who.get("seat"),
            "name": who.get("name"),
            "items": [{"code": code, "message": message, "fix": fix}],
        }

    def check_ready(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """What keeps this game from starting (H25): ``[Problem]`` (contract 13.7), empty when it can.

        Per player: the L0 (complete), the initialization (``ready``: not ``stale`` / ``failed`` / running / missing) and the
        required memory files; then the official contract and the scenario (they exist and can be used - the contract is checked
        against the scenario's tools), then every actor's model binding (``refresh``: the providers are asked again, else what the
        session holds is used).  A v5 game plays on with what it was saved with and is not checked (H58)."""
        session = self.session  # type: ignore[attr-defined]
        if session.legacy:
            return []
        problems: list[dict[str, Any]] = []
        store = self._bots_or_none()
        hub = self.sources
        spec = self.spec  # type: ignore[attr-defined]
        # ---- the contract and the scenario
        contract_problem = hub.contract_problem(spec)
        if contract_problem is not None:
            problems.append(self._problem("contract", contract_problem.code, str(contract_problem), "import_contract"))
        scenario_problem = hub.scenario_problem()
        pool = sum(item.count for item in spec.setup.identity_pool)
        if scenario_problem is not None:
            problems.append(self._problem("scenario", scenario_problem.code, str(scenario_problem), "import_rules"))
        elif pool and pool != len(session.participants):
            problems.append(
                self._problem(
                    "scenario",
                    "scenario_invalid",
                    f"剧本的身份池共有 {pool} 个身份，但对局有 {len(session.participants)} 名玩家，无法发牌。",
                    "import_rules",
                )
            )
        # ---- the players
        if store is None or any(not p.bot_id for p in session.participants):
            problems.append(
                self._problem("game", "core_missing", "读不到玩家（bot）的数据：请检查数据目录，或重新载入页面后再试。", "")
            )
        else:
            running = self.init_running_ids()  # type: ignore[attr-defined]
            sources = hub.hashes()
            for participant in session.participants:
                try:
                    items = store.problems(participant.bot_id, sources=sources, running=participant.bot_id in running)
                except Exception:  # noqa: BLE001 - a bot that cannot be read cannot play
                    logger.warning("Could not check bot %s", participant.bot_id, exc_info=True)
                    items = [{"code": "core_missing", "message": "读不到这个玩家的数据。", "fix": "import_core"}]
                if items:
                    problems.append(
                        {"scope": "bot", "bot_id": participant.bot_id, "seat": participant.id, "name": participant.name, "items": items}
                    )
        # ---- the models
        if not getattr(self, "demo_mode", False):
            issues = list(session.binding_issues)  # type: ignore[attr-defined]
            if refresh:
                try:
                    self.ensure_bindings_ready()  # type: ignore[attr-defined]  # (asks the providers again; the demo and the tests turn it off)
                    issues = []
                except BindingError as exc:
                    issues = list(exc.issues)
            by_seat = {p.id: p for p in session.participants}
            for issue in issues:
                participant = by_seat.get(issue.actor_id)
                who = (
                    {"bot_id": participant.bot_id or None, "seat": participant.id, "name": participant.name}
                    if participant is not None
                    else {"name": "主持人" if issue.actor_id == MODERATOR_ID else issue.actor_id}
                )
                problems.append(self._problem("binding", "no_binding", issue.message, "bind_model", **who))
        return problems

    # ------------------------------------------------------------------------------------------------ the start: snapshots
    def take_snapshots(self) -> StartSnapshots:
        """What every bot carries into the game that is about to start (H11), read from the table now.  Nothing is written into the
        session here - the caller does it (:meth:`apply_snapshots`) when the whole start is ready.  :class:`NotReadyError` when a bot or the
        contract cannot be read."""
        session = self.session  # type: ignore[attr-defined]
        store = self._bots()
        hub = self.sources
        scenario_id = self.spec.id  # type: ignore[attr-defined]
        memory = bool(store.memory_enabled())
        names = [p.name for p in session.participants]
        try:
            contract = hub.contract()
        except SourceError as exc:
            raise NotReadyError([self._problem("contract", exc.code, str(exc), "import_contract")]) from exc
        try:
            scenario_hash = sha256_text(hub.scenario_text(scenario_id))
        except SourceError:
            scenario_hash = ""
        minds: dict[str, dict[str, Any]] = {}
        for participant in session.participants:
            others = [name for name in names if name != participant.name]
            try:
                mind = store.load_mind(participant.bot_id, scenario_id, memory=memory, others=others)
            except Exception as exc:  # noqa: BLE001 - the table changed under our feet: the start check says who
                logger.warning("Could not load bot %s", participant.bot_id, exc_info=True)
                raise NotReadyError(
                    [
                        self._problem(
                            "bot", "core_missing", f"读不到这个玩家的数据（{exc}）。", "import_core",
                            bot_id=participant.bot_id, seat=participant.id, name=participant.name,
                        )
                    ]
                ) from exc
            minds[participant.id] = {**mind.to_dict(), "contract_hash": contract.sha256, "scenario_hash": scenario_hash}
        return StartSnapshots(minds=minds, contract=contract, memory_enabled=memory)

    def apply_snapshots(self, snapshots: StartSnapshots) -> Callable[[], None]:
        """Write the snapshots (and the contract in force, and the line-up) into the game that is starting.  Returns the function that takes them
        out again - a start that fails after this leaves the game as it was."""
        session = self.session  # type: ignore[attr-defined]
        before = (
            [(p.memory_snapshot, p.persona, p.memory) for p in session.participants],
            list(session.lineup),
            dict(session.contract_snapshot),
            session.memory_enabled,
        )
        for participant in session.participants:
            participant.memory_snapshot = copy.deepcopy(snapshots.minds[participant.id])
            participant.persona = ""
            participant.memory = ""
        session.lineup = [p.bot_id for p in session.participants]
        session.contract_snapshot = Sources.snapshot_of(snapshots.contract)
        session.memory_enabled = snapshots.memory_enabled

        def undo() -> None:
            for participant, (snapshot, persona, memory) in zip(session.participants, before[0]):
                participant.memory_snapshot, participant.persona, participant.memory = snapshot, persona, memory
            session.lineup, session.contract_snapshot, session.memory_enabled = before[1], before[2], before[3]
            self._brains = {}  # type: ignore[attr-defined]  # (the brains built from them are not the game's)

        return undo

    # ------------------------------------------------------------------------------------------------ the brains
    def _contract_of_game(self) -> ContractDoc:
        """The contract the game started with (``contract_snapshot``), not the file as it is now: the prompts of a resumed game must read as they did."""
        snapshot = self.session.contract_snapshot  # type: ignore[attr-defined]
        if str(snapshot.get("markdown") or "").strip():
            return self.sources.contract_from_snapshot(snapshot)
        return self.sources.contract()

    def build_brains(self) -> dict[str, Any]:
        """One brain per agent: a :class:`BotBrain` of its own for every seat of a v6 game (built from the snapshot it started with), the
        moderator's :class:`ArenaBrain` (the moderator is not a bot, H46).  A v5 game (H58) plays on with ONE :class:`LegacyBrain` for
        everybody - the v0.13.1 prompts, with the persona / memory texts the game was saved with."""
        session = self.session  # type: ignore[attr-defined]
        world, spec, cfg = self.world, self.spec, self.cfg  # type: ignore[attr-defined]
        if session.legacy:
            personas = {p.id: p.persona for p in session.participants}
            memories = {p.id: p.memory for p in session.participants if p.memory} if session.memory_enabled else {}
            shared = LegacyBrain(world, spec, cfg, personas=personas, memories=memories)
            return {MODERATOR_ID: shared, **{p.id: shared for p in session.participants}}
        contract = self._contract_of_game()
        brains: dict[str, Any] = {MODERATOR_ID: ArenaBrain(world, spec, cfg)}
        for participant in session.participants:
            brains[participant.id] = BotBrain(
                world,
                spec,
                participant.id,
                BotMind.from_dict(participant.memory_snapshot),
                contract,
                cfg=cfg,
                memory_enabled=bool(session.memory_enabled),
            )
        return brains

    # ------------------------------------------------------------------------------------------------ BotsPayload (§13.7)
    def read_table(self) -> TableRead:
        """Read the table once: the line-up and the ``BotSummary`` of every bot (seat order): the store's summary plus the seat and the model
        binding."""
        store = self._bots()
        hashes = self.sources.hashes()
        running = self.init_running_ids()  # type: ignore[attr-defined]
        seats = self._seat_ids()
        cache: dict[tuple[str, str], dict[str, Any]] = {}
        lineup = list(store.lineup())
        summaries: list[dict[str, Any]] = []
        for index, bot_id in enumerate(lineup):
            summary = store.summary(bot_id, sources=hashes, running=bot_id in running)
            summary["seat"] = seats[index] if index < len(seats) else None
            summary["binding"] = self._binding_view(self._bot_binding(bot_id), cache)
            summaries.append(summary)
        return TableRead(store=store, lineup=lineup, summaries=summaries, memory_enabled=bool(store.memory_enabled()))

    def bot_summaries(self) -> list[dict[str, Any]]:
        """The ``BotSummary`` of every bot in the line-up (seat order)."""
        return self.read_table().summaries

    def bots_payload(self, *, full: bool = False, table: TableRead | None = None) -> dict[str, Any]:
        """``BotsPayload`` (contract §13.7): the line-up, every bot's summary, the memory switch, the initialization job, what the start check
        says (``ready``) and the learning use; ``full`` (``GET /api/bots``) also carries ``limits`` and ``defaults``.  ``table``: a read of the
        table the caller has made already."""
        table = table or self.read_table()
        problems = self.check_ready()
        payload: dict[str, Any] = {
            "lineup": list(table.lineup),
            "bots": table.summaries,
            "memory_enabled": table.memory_enabled,
            "init_job": self.init_job_view(),  # type: ignore[attr-defined]
            "ready": {"ok": not problems, "problems": problems},
            "learning_usage": self.learning_usage(),  # type: ignore[attr-defined]
        }
        if full:
            from app.storage.bots import limits_payload

            payload["limits"] = limits_payload()
            payload["defaults"] = table.store.defaults()
        return payload

    def bot_detail(self, bot_id: str, scenario_id: str | None = None) -> dict[str, Any]:
        """``BotDetail`` (§13.7): the summary plus L0, memory, proposals; ``scenario_id`` asks for another scenario's memory."""
        store = self._bots()
        hashes = self.sources.hashes()
        data = store.detail(bot_id, scenario_id or "", sources=hashes, running=bot_id in self.init_running_ids())  # type: ignore[attr-defined]
        data["seat"] = self._seat_of(bot_id)
        data["binding"] = self._binding_view(self._bot_binding(bot_id), {})
        return data

    # ------------------------------------------------------------------------------------------------ PublicSession pieces (§13.8)
    def system_status(self, table: TableRead | None = None) -> dict[str, Any]:
        """``PublicSession.system_status`` (H26): ``{"code", "text", "reason"}``.  Priority: initializing / reviewing, then in game /
        paused / finished, then uninitialized, then before the game.  A soft pause is "in game" (with its own text).  ``table``: a read of the
        table the caller has made already (only a game that has not started needs it)."""
        session = self.session  # type: ignore[attr-defined]

        def status(code: str, text: str, reason: str = "") -> dict[str, Any]:
            return {"code": code, "text": text, "reason": reason}

        if self.init_running_ids():  # type: ignore[attr-defined]
            return status("initializing", "初始化中")
        if self.review_running_ids():  # type: ignore[attr-defined]
            return status("reviewing", "复盘中")
        if session.status == "running":
            soft = self.soft_pause_payload() is not None  # type: ignore[attr-defined]
            return status("in_game", "对局中 · agent 已暂停" if soft else "对局中")
        if session.status == "paused":
            reason = PAUSE_TEXTS.get(session.pause_reason, "已暂停")
            if session.hold_reason:
                reason = f"{reason}：{session.hold_reason}"
            return status("paused", "已暂停", reason)
        if session.status == "finished":
            return status("finished", "已结束")
        if not session.legacy and self._table_uninitialized(table):
            return status("uninitialized", "未初始化")
        return status("before_game", "对局前")

    def init_statuses(self, table: TableRead | None = None) -> dict[str, str]:
        """``{bot id: init status}`` of the bots of the table (what ``PublicSession.participants[i].init_status`` shows)."""
        try:
            table = table or (self.read_table() if self._bots_or_none() is not None else None)
        except Exception:  # noqa: BLE001
            logger.warning("Could not read the initialization states of the bots", exc_info=True)
            return {}
        return table.init_statuses() if table is not None else {}

    def _table_uninitialized(self, table: TableRead | None = None) -> bool:
        """Some bot of the table has no current initialization (never, stale or failed): a game that has not started cannot start."""
        statuses = self.init_statuses(table)
        return any(status != "ready" for status in statuses.values())

    # ------------------------------------------------------------------------------------------------ one-shot model calls
    def _oneshot_port(self) -> Any:
        """The model port for calls outside the runtime loop: the demo bots when installed, else the engine client behind the same
        per-agent bindings the game uses.  Every call of it is a line of the error log (``refine`` for the moderator's model
        completing a text, ``review`` / ``init`` for the learning runs)."""
        from app.game.observe_runtime import oneshot_label
        from app.runtime.llm_gate import GatedEngineLlm, LlmGate, LoggedLlm

        call_log = self.call_log()  # type: ignore[attr-defined]
        if self.llm_port_factory is not None:  # type: ignore[attr-defined]
            return LoggedLlm(
                self.llm_port_factory(self),  # type: ignore[attr-defined]
                call_log,
                self.session.get_model_binding,  # type: ignore[attr-defined]
                oneshot_label,
            )
        rt = self.cfg.get("runtime") or {}  # type: ignore[attr-defined]
        return GatedEngineLlm(
            self.llm,  # type: ignore[attr-defined]
            self.session.get_model_binding,  # type: ignore[attr-defined]
            LlmGate(int(rt.get("llm_concurrency", 0)), int(rt.get("llm_concurrency_per_provider", 0))),
            float(rt.get("llm_timeout_seconds", 120)),
            call_log=call_log,
            label=oneshot_label,
        )

    def _record_oneshot(self, who: str, resp: Any) -> None:
        try:
            self.usage.record_llm(who, resp)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.warning("Could not account a one-shot call", exc_info=True)

    async def complete_oneshot(
        self, agent_id: str, messages: list[dict[str, Any]], *, max_tokens: int | None = None, purpose: str = "refine"
    ) -> str:
        """One model call outside the game (the moderator's model drafting a text for the user, ``purpose="refine"``): accounted in the
        usage of the game (as the roster's), a line of the error log; returns the text of the answer.  Raises what the model call raises."""
        resp = await self._oneshot_port().complete(agent_id, messages, None, max_tokens=max_tokens)
        self._record_oneshot("roster", resp)
        return str(getattr(resp, "content", "") or "")
