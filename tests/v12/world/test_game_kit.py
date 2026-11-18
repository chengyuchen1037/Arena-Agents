"""Shared helpers of the world-game tests (``test_game_*.py``, ``test_board.py``, ``test_projection.py``).

This module contains no tests.  It builds a :class:`WorldCore` with only the game module attached
(the chat module belongs to another test suite): channels are created by hand in ``world.state``
and a recording fake stands in for ``world.chat``.
"""

from __future__ import annotations

import copy
from typing import Any

from app.world.core import WorldCore
from app.world.game import GameModule
from app.world.spec import ScenarioSpec
from app.world.state import ChannelState, PlayerState
from app.world.types import MODERATOR_ID, Delivery, Intent, LedgerEntry, Receipt, UiEvent

PLAYER_IDS = ["u1", "u2", "u3", "u4"]
PLAYER_NAMES = {"u1": "甲", "u2": "乙", "u3": "丙", "u4": "丁"}

SPEC_DICT: dict[str, Any] = {
    "id": "unit_test",
    "version": "1.0.0",
    "title": "单元测试剧本",
    "setup": {
        "identity_pool": [
            {"role": "alpha", "count": 1},
            {"role": "beta", "count": 1},
            {"role": "gamma", "count": 2},
        ],
        "roles": {
            "alpha": {
                "faction": "red",
                "tags": ["active", "red"],
                "attributes": {"lives": 2},
                "brief": "你是 alpha，属于红队。",
            },
            "beta": {
                "faction": "blue",
                "tags": ["active", "blue"],
                "attributes": {},
                "brief": "你是 beta，属于蓝队。",
            },
            "gamma": {
                "faction": "blue",
                "tags": ["active"],
                "attributes": {"group": "plain"},
                "brief": "你是 gamma。",
            },
        },
        "public_tags": ["active", "out", "leader"],
        "shuffle": True,
    },
    "defaults": {
        "request": {"duration_s": 30, "max_text_chars": 20},
        "dm": {"max_chars_per_message": 50},
        "moderator": {"max_chars_per_message": 100},
    },
    "abilities": {
        "strike": {
            "holders": "${alive(by_role('alpha'))}",
            "quota": 1,
            "params": {"target": "player_id", "note": "text(10)?"},
            "freeze_channels": ["public"],
            "reveal": {"channel_id": "public", "text": "{{actor}}发动了 strike，目标是 {{target}}。"},
            "description": "对一名玩家发动 strike。",
        },
        "ping": {
            "holders": "${alive()}",
            "quota": 2,
            "description": "向主持人打个招呼。",
        },
    },
    "checks": {
        "red_alive": "${count(alive(by_faction('red')))}",
        "blue_ids": "${ids(by_faction('blue'))}",
        "broken": "${nope(1)}",
    },
    "ui": {"out_tags": ["out"], "status_tags": ["leader"], "highlight_tags": ["leader"]},
    "invalidates_drafts": ["player.updated"],
}


def make_spec(**overrides: Any) -> ScenarioSpec:
    """The unit-test scenario; top-level keys of ``overrides`` replace the defaults."""
    data = copy.deepcopy(SPEC_DICT)
    data.update(overrides)
    return ScenarioSpec.model_validate(data)


class ManualClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = float(start)

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt

    def set(self, t: float) -> None:
        self.t = float(t)


class FakeChat:
    """Stands in for ``world.chat``: records the freeze calls of the game module."""

    def __init__(self) -> None:
        self.frozen: list[tuple[str, str]] = []

    def freeze_channel(self, channel_id: str, reason: str = "") -> bool:
        self.frozen.append((channel_id, reason))
        return True


def add_players(world: WorldCore, ids: list[str] | None = None) -> None:
    for index, pid in enumerate(ids or PLAYER_IDS, start=1):
        world.state.players.append(PlayerState(id=pid, seat=str(index), name=PLAYER_NAMES.get(pid, pid)))


def add_channels(world: WorldCore) -> None:
    """One system DM per player (free mode) and a public channel with everybody."""
    everyone = [MODERATOR_ID, *(p.id for p in world.state.players)]
    for p in world.state.players:
        world.state.channels.append(
            ChannelState(
                id=f"dm-{p.id}",
                name=f"与主持人的私聊（{p.name}）",
                type="dm",
                mode="free",
                members=[MODERATOR_ID, p.id],
                system=True,
                owner=p.id,
            )
        )
    world.state.channels.append(ChannelState(id="public", name="公共频道", type="public", mode="free", members=everyone))


def make_world(
    *,
    spec: ScenarioSpec | None = None,
    seed: int = 7,
    deal: bool = True,
    running: bool = True,
    players: list[str] | None = None,
    clock: ManualClock | None = None,
) -> WorldCore:
    """A world with the game module, ``world.game``, a fake ``world.chat``, players, DMs, a public
    channel; dealt and running by default.  Outboxes are drained so tests start from a clean slate."""
    world = WorldCore(spec or make_spec(), clock or ManualClock(), seed=seed)
    game = GameModule(world)
    world.add_module(game)
    world.game = game  # type: ignore[attr-defined]
    world.chat = FakeChat()  # type: ignore[attr-defined]
    add_players(world, players)
    add_channels(world)
    if deal:
        game.deal()
    if running:
        world.set_running(True, "start")
    world.drain_outbox()
    world.drain_events()
    return world


def ask(world: WorldCore, actor: str, intent_type: str, **payload: Any) -> Receipt:
    return world.submit(
        Intent(actor_id=actor, type=intent_type, payload=payload, based_on_seq=world.seq)
    )


def mod(world: WorldCore, intent_type: str, **payload: Any) -> Receipt:
    """A moderator intent; ``intent_type`` without the ``mod.`` prefix."""
    return ask(world, MODERATOR_ID, f"mod.{intent_type}", **payload)


def entries(world: WorldCore, entry_type: str, *, after_seq: int = 0) -> list[LedgerEntry]:
    return [e for e in world.ledger if e.type == entry_type and e.seq > after_seq]


def deliveries(world: WorldCore) -> list[Delivery]:
    return world.drain_outbox()


def to(items: list[Delivery], target: str, event: str | None = None) -> list[Delivery]:
    return [
        d
        for d in items
        if d.target == target and (event is None or d.envelope.payload.get("event") == event)
    ]


def events(world: WorldCore, event_type: str | None = None) -> list[UiEvent]:
    out = world.drain_events()
    return [e for e in out if event_type is None or e.type == event_type]


def player(world: WorldCore, player_id: str) -> PlayerState:
    found = world.player(player_id)
    assert found is not None
    return found


def role_holder(world: WorldCore, role: str) -> str:
    """Id of the (first) player dealt ``role``."""
    for p in world.state.players:
        if p.role == role:
            return p.id
    raise AssertionError(f"nobody has role {role}")


def choice_options() -> list[dict[str, str]]:
    return [{"id": "yes", "label": "同意"}, {"id": "no", "label": "反对"}]
