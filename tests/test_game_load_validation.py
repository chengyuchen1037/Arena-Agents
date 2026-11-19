"""What the orchestrator accepts as "a game" (v0.13): ported from the v0.12.1 tests of the single-game import.

A game reaches the engine from the game store (``load_game``, the history's "继续"), from the last-game pointer at start-up
and from an imported archive.  Whatever the route, a file that is not a complete game of this version must be refused
whole, with a message, before anything of the live game changes - and a refused file must never be modified or saved over.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from app.game.orchestrator import SessionLoadError
from app.world.loader import load_scenario_v2
from game_fakes import async_test, real_orch


def real_spec() -> dict[str, Any]:
    return copy.deepcopy(load_scenario_v2("scenarios/minimal_test.md").model_dump(mode="json"))


def good_game(monkeypatch: pytest.MonkeyPatch, session_id: str = "imported-1") -> dict[str, Any]:
    payload = json.loads(json.dumps(real_orch(monkeypatch).session_payload()))
    payload["session_id"] = session_id
    return payload


@async_test
async def test_a_complete_game_is_accepted_and_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    orch.game_store.data["imported-1"] = good_game(monkeypatch)
    game = await orch.load_game("imported-1")
    assert game.session_id == "imported-1" and game.status == "idle" and not orch.world.running


@pytest.mark.parametrize("raw", [None, [], [1, 2, 3], "text", 5])
def test_something_that_is_not_an_object_is_refused(monkeypatch: pytest.MonkeyPatch, raw: Any) -> None:
    orch = real_orch(monkeypatch)
    with pytest.raises(SessionLoadError, match="JSON 对象"):
        orch._validate_session_data(raw)


@pytest.mark.parametrize("schema", [1, 2, 3, 4, 6, 99, None, "5"])
def test_a_game_of_another_save_format_is_refused_with_the_way_to_bring_it_over(monkeypatch: pytest.MonkeyPatch, schema: Any) -> None:
    orch = real_orch(monkeypatch)
    payload = good_game(monkeypatch)
    payload["schema_version"] = schema
    with pytest.raises(SessionLoadError, match="migrate_v0121_to_archive") as raised:
        orch._validate_session_data(payload)
    assert "旧版本" in str(raised.value)


@pytest.mark.parametrize(
    "mutation",
    [
        {"session_id": "../../etc/passwd"},
        {"session_id": "a/b"},
        {"session_id": ""},
        {"status": "exploding"},
        {"participants": "nope"},
        {"participants": [{"bogus": 1}]},
        {"usage_stats": "many"},
        {"model_bindings": {"p01": {"provider_id": 1}}},
        {"takeover": {"seat": 5}},
        {"human_spans": [{"seat": "N1", "from_seq": "start"}]},
        {"created_at": 12345},
    ],
)
def test_a_game_with_an_invalid_structure_is_refused(monkeypatch: pytest.MonkeyPatch, mutation: dict[str, Any]) -> None:
    orch = real_orch(monkeypatch)
    payload = good_game(monkeypatch)
    payload.update(mutation)
    with pytest.raises(SessionLoadError):
        orch._validate_session_data(payload)


@pytest.mark.parametrize(
    "scenario_spec",
    [
        [],
        {},
        {"id": "x"},
        {"id": "", "version": "1"},
        {"id": "x", "version": "1", "schema_version": 1},
        {"id": "x", "version": "1", "templates": {"t": {"text": "x", "users": "everyone"}}},
        {"id": "x", "version": "1", "setup": {"identity_pool": [{"role": "ghost", "count": 1}]}},
        {"id": "x", "version": "1", "unexpected_key": {"a": 1}},
        {"id": "x", "version": "1", "defaults": {"floor": {"duration_s": "soon"}}},
    ],
)
def test_a_game_with_an_invalid_scenario_is_refused(monkeypatch: pytest.MonkeyPatch, scenario_spec: Any) -> None:
    orch = real_orch(monkeypatch)
    payload = good_game(monkeypatch)
    payload["scenario_spec"] = scenario_spec
    with pytest.raises(SessionLoadError):
        orch._validate_session_data(payload)


def test_nothing_unknown_gets_through_however_deeply_it_is_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    deep: dict[str, Any] = {}
    cursor = deep
    for _ in range(100):
        cursor["x"] = {}
        cursor = cursor["x"]
    payload = good_game(monkeypatch)
    payload["scenario_spec"] = {**real_spec(), "extra": deep}  # extra="forbid"
    with pytest.raises(SessionLoadError):
        orch._validate_session_data(payload)


def test_a_world_that_cannot_be_loaded_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    for world in ({}, {"format": 1, "state": {"players": "nope"}}, {"format": 7}, "nope"):
        payload = good_game(monkeypatch)
        payload["world"] = world
        with pytest.raises(SessionLoadError):
            orch._validate_session_data(payload)


@async_test
async def test_a_refused_game_changes_nothing_and_is_never_modified_or_saved_over(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = real_orch(monkeypatch)
    orch._autosave_enabled = True
    live = orch.session
    before = live.model_dump_json()
    orch.game_store.data.update(
        {
            "forged-1": {**good_game(monkeypatch, "forged-1"), "scenario_spec": {"id": "x", "version": "1", "schema_version": 1}},
            "forged-2": {**good_game(monkeypatch, "forged-2"), "schema_version": 4},
            "forged-3": {**good_game(monkeypatch, "forged-3"), "world": {"format": 1, "state": {"players": "nope"}}},
            "forged-4": {**good_game(monkeypatch, "forged-4"), "session_id": "../../escape"},  # a file whose own id lies
        }
    )
    stored = copy.deepcopy(orch.game_store.data)

    for session_id in ("forged-1", "forged-2", "forged-3", "forged-4"):
        with pytest.raises(SessionLoadError):
            await orch.load_game(session_id)
    with pytest.raises(ValueError):
        await orch.load_game("../../escape")  # and an id that is a path never reaches the store

    await asyncio.sleep(0.05)
    assert orch.session is live and live.model_dump_json() == before  # the live game was not touched ...
    assert orch.game_store.saves == [] and orch.game_store.data == stored  # ... nor saved, nor was any stored game changed
