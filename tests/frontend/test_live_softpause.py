"""The REAL engine's event stream of a game with a soft pause, reduced by the REAL client store (v0.13.1, contract §12.2).

``test_mock_softpause.py`` checks that the UI mock speaks the contract; this file closes the loop against the engine
itself.  The backend's own whole-game scenario (``tests/test_softpause_demo_game.py``: the demo bots play every seat but
one and the moderator, a person plays a seat, soft-pauses now and then and acts meanwhile; no model is called) is
recorded the way a browser sees it - the snapshot it gets when it connects, then every event in its wire form
(``BusEvent.json()``) - and then

1. every event and both sessions are checked with the mock's validator (``validate_event`` / ``validate_session``): the
   shapes the page's tests are written against are the shapes the engine really sends (``soft_pause`` / ``clock_running``
   in every ``session.updated``, the untimed floors, the agent states ...);
2. the real client store (``static/js/store.js`` through ``tests/frontend/replay_check.js --live``) must reduce the log to
   exactly the session the server reports at the end.

It reuses the helpers of the backend's whole-game tests (``SoftPerson``, ``build``, ``wait_for``): a change of those is
a change of this file.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
MOCK_PATH = ROOT / "tools" / "mock_ui_server.py"
sys.path.insert(0, str(ROOT / "tests"))  # (the helpers of the backend's whole-game tests live there)

from test_softpause_demo_game import HELD, SoftPerson  # noqa: E402
from test_takeover_demo_game import build, wait_for  # noqa: E402

_WINDOWS_NODE = Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"


def _find_node() -> str | None:
    env = os.environ.get("NODE_BIN")
    if env and Path(env).exists():
        return env
    return shutil.which("node") or (str(_WINDOWS_NODE) if _WINDOWS_NODE.exists() else None)


NODE = _find_node()
pytestmark = pytest.mark.skipif(NODE is None, reason="node executable not found")


def _load_mock():
    spec = importlib.util.spec_from_file_location("arena_mock_ui_server_live_softpause", MOCK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mock = _load_mock()


async def record_a_game(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Play the backend's soft-pause game and record what a connected browser would have received."""
    # (the deal is random: a seat that is killed in the first night never gets anything to do - then another game is played)
    for _attempt in range(6):
        orch = build(monkeypatch)
        seat = next(p.id for p in orch.world.state.players if p.role == "villager")
        await orch.takeover(seat)
        # a browser connects now: it gets the snapshot, then every event (a queue so big that nothing is ever shed)
        orch.bus.queue_size = 1_000_000
        _replay, queue = orch.bus.subscribe(None)
        initial = orch.public_session()
        events: list[dict[str, Any]] = []

        async def pump() -> None:
            while True:
                events.append(json.loads((await queue.get()).json()))

        pumping = asyncio.ensure_future(pump())
        person = SoftPerson(orch, seat)
        playing = asyncio.ensure_future(person.play())
        await orch.start_or_resume()
        await wait_for(lambda: orch.session.status in ("finished", "paused"))
        assert orch.session.status == "finished", (orch.session.status, orch.session.hold_reason)
        await playing
        while not queue.empty():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        pumping.cancel()
        orch.bus.unsubscribe(queue)
        final = orch.public_session()
        await orch.shutdown()
        if person.pauses:
            return {"seat": seat, "pauses": person.pauses, "initial": initial, "events": events, "final": final}
    raise AssertionError("the person never got anything to do in six games")


@pytest.fixture(scope="module")
def game(monkeypatch_module: pytest.MonkeyPatch) -> dict[str, Any]:
    return asyncio.run(record_a_game(monkeypatch_module))


@pytest.fixture(scope="module")
def monkeypatch_module():
    patch = pytest.MonkeyPatch()
    yield patch
    patch.undo()


def _updates(game: dict[str, Any]) -> list[dict[str, Any]]:
    return [e["data"] for e in game["events"] if e["type"] == "session.updated"]


def test_the_soft_pause_really_happened_and_every_session_updated_says_where_it_stands(game: dict[str, Any]) -> None:
    assert game["pauses"] and all(p["ok"] for p in game["pauses"]), game["pauses"]
    updates = _updates(game)
    assert updates and all({"soft_pause", "clock_running", "clock"} <= set(u) for u in updates), "§12.2: in every session.updated"
    held = [u for u in updates if u["soft_pause"] == HELD]
    assert held, "the pause showed: `held` was announced"
    assert all(u["clock_running"] is False and u["status"] == "running" for u in held), "a soft pause: the game runs, its clock does not"
    assert len({u["clock"] for u in held}) <= len(game["pauses"]), "the frozen clock is the same all through one pause"
    running = [u for u in updates if u["status"] == "running" and u["soft_pause"] is None]
    assert running and all(u["clock_running"] is True for u in running)
    assert all(u["clock_running"] is False for u in updates if u["status"] != "running")
    # every pause ended: the session at the end has none, and its clock does not run (the game is over)
    final = game["final"]
    assert (final["status"], final["soft_pause"], final["clock_running"]) == ("finished", None, False)
    assert game["initial"]["soft_pause"] is None and game["initial"]["clock_running"] is False
    # the states an agent can have: nothing but the five of §12.2 (and the person's seat is `human`)
    states = {e["data"]["state"] for e in game["events"] if e["type"] == "agent.status"}
    assert states <= {"thinking", "acting", "sleeping", "held"}, states
    assert not [e for e in game["events"] if e["type"] == "agent.status" and e["data"]["id"] == game["seat"]], "no agent plays the person's seat"


def test_the_persons_turns_in_the_public_channel_have_no_countdown(game: dict[str, Any]) -> None:
    seat = game["seat"]
    floors = [e["data"] for e in game["events"] if e["type"] == "floor.changed"]
    untimed = [f for f in floors if f["floor"].get("untimed")]
    assert untimed, "the person held a public floor: `untimed` was sent"
    for f in untimed:
        assert f["floor"]["active"] == seat and f["floor"]["seconds_left"] is None, f
    # the agents' turns are timed, and every FloorView says so (the key is always there)
    assert all("untimed" in f["floor"] for f in floors)
    timed = [f for f in floors if f["floor"]["kind"] in ("queue", "turn") and f["floor"]["active"] not in (None, seat)]
    assert timed and all(f["floor"]["untimed"] is False for f in timed)
    # the seat's own state of a held floor has neither a countdown nor a deadline
    pushes = [e["data"]["takeover"] for e in game["events"] if e["type"] == "takeover.updated"]
    mine = [c["floor"] for t in pushes for c in t["channels"].values() if c["floor"] and c["floor"]["mine"] and c["floor"].get("untimed")]
    assert mine and all(f["seconds_left"] is None and f["deadline_clock"] is None for f in mine)
    assert all("untimed" in c["floor"] for t in pushes for c in t["channels"].values() if c["floor"])


def test_every_event_and_both_sessions_match_the_contract_the_pages_tests_are_written_against(game: dict[str, Any]) -> None:
    problems: dict[str, list[str]] = {}
    for ev in game["events"]:
        for p in mock.validate_event(ev):
            problems.setdefault(ev["type"], []).append(p)
    for name in ("initial", "final"):
        for p in mock.validate_session(game[name]):
            problems.setdefault(f"session:{name}", []).append(p)
    assert not problems, {k: (len(v), v[:3]) for k, v in problems.items()}


def test_the_real_client_store_reduces_the_log_of_the_real_engine_to_the_servers_session(game: dict[str, Any], tmp_path: Path) -> None:
    log = tmp_path / "live-softpause.json"
    log.write_text(json.dumps({"initial": game["initial"], "events": game["events"], "final": game["final"]}, ensure_ascii=False, default=str), encoding="utf-8")
    result = subprocess.run(
        [NODE, str(ROOT / "tests" / "frontend" / "replay_check.js"), str(log), "--live"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "live replay ok" in result.stdout
