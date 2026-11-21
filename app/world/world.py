"""``World``: the assembled world = kernel (:class:`~app.world.core.WorldCore`) + chat module + game module.

This is the object the orchestrator creates and the runtime / agents talk to through
:class:`~app.world.port.WorldPort`.  ``new_game`` builds a fresh game (players, system DMs, dealing - or, with
``deal=False``, everything but the dealing, which ``deal_identities`` does when the game starts, v0.14);
``load`` restores what :meth:`~app.world.core.WorldCore.dump` produced.  Contracts §2.1, §13.3.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any

from app.world.chat import ChatModule
from app.world.core import WorldCore
from app.world.game import GameModule
from app.world.port import Clock
from app.world.projection import world_snapshot
from app.world.spec import ScenarioSpec
from app.world.state import PlayerState, WorldState
from app.world.types import ENGINE_ID, MODERATOR_ID, AgentMemory, LedgerEntry

DUMP_FORMAT = 1  # ``WorldCore.dump()["format"]``


def _player_states(players: list[Mapping[str, Any]]) -> list[PlayerState]:
    """``[{"id","seat","name","persona"}]`` -> ``PlayerState`` list (``seat`` / ``name`` / ``persona`` optional)."""
    states: list[PlayerState] = []
    seen: set[str] = set()
    for index, raw in enumerate(players, start=1):
        player_id = str(raw.get("id", "")).strip()
        if not player_id:
            raise ValueError(f"第 {index} 名玩家缺少 id。")
        if player_id in (MODERATOR_ID, ENGINE_ID):
            raise ValueError(f"玩家 id 不能使用保留名称：{player_id}")
        if player_id in seen:
            raise ValueError(f"玩家 id 重复：{player_id}")
        seen.add(player_id)
        states.append(
            PlayerState(
                id=player_id,
                seat=str(raw.get("seat", index)),
                name=str(raw.get("name") or player_id),
                persona=str(raw.get("persona") or ""),
            )
        )
    return states


class World(WorldCore):
    chat: ChatModule
    game: GameModule

    def __init__(
        self,
        spec: ScenarioSpec,
        clock: Clock,
        *,
        state: WorldState | None = None,
        ledger: list[LedgerEntry] | None = None,
        seed: int | None = None,
    ) -> None:
        super().__init__(spec, clock, state=state, ledger=ledger, seed=seed)
        self.chat = ChatModule(self)
        self.game = GameModule(self)
        self.add_module(self.chat)
        self.add_module(self.game)

    # ------------------------------------------------------------------ construction --
    @classmethod
    def new_game(
        cls,
        spec: ScenarioSpec,
        players: list[Mapping[str, Any]],
        clock: Clock,
        *,
        seed: int | None = None,
        deal: bool = True,
    ) -> "World":
        """A new game: the players, one system DM each (D23), dealt identities (D9, D24).

        ``players`` is ``[{"id", "seat", "name", "persona"}]`` in seating order.  The game is ``idle`` and
        not running: the orchestrator opens the gate with ``set_running(True, ...)``.  Without ``seed`` a
        random one is drawn (it is stored, so the same game can be replayed from its save).
        Raises ``ValueError`` for bad player lists and when the identity pool does not fit the players.

        ``deal`` (v0.14, H52): ``True`` (the default, so libraries and tests are unchanged) deals the identities
        right here.  ``False`` leaves the players without any identity - ``state.dealt`` is ``False`` and no view of
        the world carries role / faction / identity tags - and the system DMs are still created and the size of the
        identity pool is still checked against the players; the orchestrator calls :meth:`deal_identities` when the
        game starts.
        """
        chosen = int(seed) if seed is not None else (secrets.randbits(32) or 1)
        world = cls(spec, clock, state=WorldState(players=_player_states(list(players))), seed=chosen)
        world.chat.create_system_dms()
        if deal:
            world.game.deal()
        else:
            world.game.check_pool()
        return world

    def deal_identities(self) -> list[dict[str, Any]]:
        """Deal the identities of a game created with ``new_game(..., deal=False)`` (v0.14, H52).

        :meth:`GameModule.deal` (shuffle with the world's seeded rng, ``role`` / ``faction`` / ``tags`` / ``attributes``
        of every player, one private ``player.updated`` ledger entry with ``reason == "deal"`` each) plus the guard
        that a game is dealt exactly once: a second call raises ``RuntimeError``.  Sets ``state.dealt``; raises
        ``ValueError`` (nothing changed) when the identity pool does not fit the players.  Spectators are told with a
        ``player.updated`` UI event per player (the tags they now carry).
        Returns ``[{"player_id", "role", "faction"}]`` in player order.
        """
        dealt = self.game.deal()
        for row in dealt:
            player = self.player(row["player_id"])
            if player is not None:
                self.emit("player.updated", player_id=player.id, tags=list(player.tags), out=bool(self.is_out(player)))
        self._notify()
        return dealt

    @classmethod
    def load(cls, data: dict[str, Any], spec: ScenarioSpec, clock: Clock) -> "World":
        """Restore a game from ``dump()``.  The caller has already set ``clock`` to ``data["clock_now"]``.

        A save taken while the game was running comes back ``paused``: the commit gate of a freshly loaded
        world is closed until the orchestrator resumes it (``set_running(True, ...)`` then ``redeliver()``).
        """
        if data.get("format") != DUMP_FORMAT:
            raise ValueError(f"不支持的世界存档格式：{data.get('format')!r}")
        state = WorldState.model_validate(data["state"])
        if "dealt" not in data["state"]:
            # A world saved before v0.14 has no ``dealt``: identities were always dealt when the game was created, so
            # it is dealt when any player has a role - or when there was nothing to deal.
            state.dealt = any(p.role for p in state.players) or not any(i.count > 0 for i in spec.setup.identity_pool)
        ledger = [LedgerEntry.model_validate(entry) for entry in data["ledger"]]
        for index, entry in enumerate(ledger, start=1):
            if entry.seq != index:
                raise ValueError(f"存档的账本序号不连续：第 {index} 条的 seq 是 {entry.seq}。")
        if state.status == "running":
            state.status = "paused"
        world = cls(spec, clock, state=state, ledger=ledger)
        saved_modules = data.get("modules") or {}
        for module in world.modules:
            module.load(saved_modules.get(module.name) or {})
        return world

    # -------------------------------------------------- WorldPort: delegated to the game module --
    def board(self, agent_id: str) -> dict[str, Any]:
        return self.game.board(agent_id)

    def memory(self, agent_id: str) -> AgentMemory:
        return self.game.memory(agent_id)

    def write_note(self, agent_id: str, title: str, content: str, mode: str = "overwrite") -> str:
        return self.game.write_note(agent_id, title, content, mode)

    def read_notes_text(self, agent_id: str) -> str:
        return self.game.read_notes_text(agent_id)

    def compact(self, agent_id: str, summary: str, keep_recent_count: int = 10) -> dict[str, Any]:
        return self.game.compact(agent_id, summary, keep_recent_count)

    # ------------------------------------------------------------------- spectators ---
    def snapshot(self, *, max_messages: int = 2000) -> dict[str, Any]:
        """The god-view snapshot for the UI (contracts §2.8)."""
        return world_snapshot(self, max_messages=max_messages)
