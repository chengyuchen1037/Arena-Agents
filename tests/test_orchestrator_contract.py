"""The real orchestrator against the contract the page's tests are written against (v0.14, docs/v0.12-contracts.md §13.7 / §13.8).

``tools/mock_ui_server.py`` is the UI mock of the web streams; its validators (``validate_session``, ``validate_event`` and the ones of the bots hub)
are the shapes the page is built on.  Here they are run over what the REAL engine sends - the ``PublicSession`` in every state of a game and every
event of an initialization, a whole game and its review - so that a change on either side that breaks the other shows up here, not in a browser.
No model is called (scripted agents and a scripted learning model).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from game_fakes import (
    TABLE_IDS,
    async_test,
    bots_for,
    idle_moderator,
    make_table_orch,
    wait_for,
)
from learning_fakes import ScriptedLlm, by_kind
from test_call_log_api import end_at_once, play_with

ROOT = Path(__file__).resolve().parent.parent
MOCK_PATH = ROOT / "tools" / "mock_ui_server.py"


def _load_mock() -> Any:
    spec = importlib.util.spec_from_file_location("arena_mock_ui_server_orchestrator_contract", MOCK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mock = _load_mock()
hub = mock.hubmod


def problems_of_session(orch: Any) -> list[str]:
    session = json.loads(json.dumps(orch.public_session(), ensure_ascii=False))  # (the wire form)
    return mock.validate_session(session)


def problems_of_events(orch: Any) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for event in list(orch.bus._ring):
        for problem in mock.validate_event(json.loads(event.json())):
            found.setdefault(event.type, []).append(problem)
    return found


# ======================================================================================================== PublicSession
@async_test
async def test_the_public_session_is_valid_in_every_state_of_a_game(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch, ready=False)
    assert problems_of_session(orch) == []  # idle, nobody initialized: "未初始化", every card with its problem
    assert orch.public_session()["system_status"]["code"] == "uninitialized" and not orch.public_session()["bots"]["ready"]["ok"]

    gate = asyncio.Event()
    llm = ScriptedLlm(by_kind(), gate=gate)
    orch.llm_port_factory = lambda o: llm
    await orch.start_init()
    await wait_for(lambda: llm.in_flight == 4)
    assert problems_of_session(orch) == [] and orch.public_session()["system_status"]["code"] == "initializing"  # a job is running: init_job
    assert orch.public_session()["bots"]["init_job"]["ids"] == orch._bots().lineup()
    gate.set()
    await wait_for(lambda: orch.init_job_view() is None and not orch.init_running_ids())
    assert problems_of_session(orch) == [] and orch.public_session()["system_status"]["code"] == "before_game"  # everybody ready

    play_with(orch, moderator=end_at_once)
    await orch.start_or_resume()
    assert problems_of_session(orch) == [] and orch.public_session()["system_status"]["code"] == "in_game" and orch.public_session()["dealt"] is True
    await wait_for(lambda: orch.session.status == "finished")
    assert problems_of_session(orch) == [] and orch.public_session()["system_status"]["code"] == "finished"

    gate = asyncio.Event()
    llm = ScriptedLlm(by_kind(), gate=gate)
    orch.llm_port_factory = lambda o: llm
    await orch.start_review()
    await wait_for(lambda: llm.in_flight == 4)
    assert problems_of_session(orch) == [] and orch.public_session()["system_status"]["code"] == "reviewing"
    assert orch.public_session()["review"]["running"] is True and isinstance(orch.public_session()["review"]["seconds_left"], int)
    gate.set()
    await wait_for(lambda: not orch.review_running_ids())
    assert problems_of_session(orch) == [] and orch.public_session()["review"]["done"] == TABLE_IDS

    await orch.new_game()
    assert problems_of_session(orch) == [] and orch.public_session()["dealt"] is False and orch.public_session()["system_status"]["code"] == "before_game"
    await orch.shutdown()


@async_test
async def test_the_public_session_is_valid_while_paused_with_a_seat_taken_over_and_for_a_game_of_v0_13(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    bots_for(orch, idle_moderator)
    orch.llm_port_factory = lambda o: __import__("v12.bots", fromlist=["BotLlm"]).BotLlm(o.world, {})
    await orch.start_or_resume()
    await orch.pause("user")
    await orch.takeover("N2")
    assert problems_of_session(orch) == [] and orch.public_session()["system_status"] == {"code": "paused", "text": "已暂停", "reason": "手动暂停"}
    await orch.release_takeover()
    payload = json.loads(json.dumps(orch.session_payload()))
    payload["schema_version"] = 5
    for key in ("lineup", "contract_snapshot"):
        payload.pop(key)
    for participant in payload["participants"]:
        participant.pop("bot_id")
        participant.pop("memory_snapshot")
    await orch._adopt_loaded_session(orch._validate_session_data(payload), "已载入对局")
    session = orch.public_session()
    assert problems_of_session(orch) == [] and session["schema_version"] == 6 and session["review"]["supported"] is False
    assert [p["bot_id"] for p in session["participants"]] == [""] * 4 and [p["init_status"] for p in session["participants"]] == [None] * 4
    await orch.shutdown()


@async_test
async def test_the_full_bots_payload_has_the_limits_and_the_defaults_and_nothing_else_does(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    assert hub.validate_bots_payload(json.loads(json.dumps(orch.bots_payload(full=True))), "bots", full=True) == []
    assert hub.validate_bots_payload(json.loads(json.dumps(orch.bots_payload())), "bots") == []
    detail = json.loads(json.dumps(orch.bot_detail(orch._bots().lineup()[0])))  # (BotDetail is a BotSummary and more: its ``staging`` lists the proposals)
    assert isinstance(detail["staging"], list) and detail["seat"] == "N1" and detail["binding"]["ok"] is True
    assert hub.validate_summary({**detail, "staging": len(detail["staging"])}, "detail") == []


# ============================================================================================================== events
@async_test
async def test_every_event_of_an_initialization_a_whole_game_and_its_review_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch, ready=False)
    llm = ScriptedLlm(by_kind(), fail_for=(orch._bots().lineup()[3],))  # (one bot cannot be initialized: the failed card is an event, too)
    orch.llm_port_factory = lambda o: llm
    await orch.start_init()
    await wait_for(lambda: orch.init_job_view() is None and not orch.init_running_ids())
    orch.llm_port_factory = lambda o: ScriptedLlm(by_kind())
    await orch.start_init()  # (the one that failed, again)
    await wait_for(lambda: orch.init_job_view() is None and not orch.init_running_ids())

    play_with(orch, moderator=end_at_once)
    await orch.start_or_resume()
    await wait_for(lambda: orch.session.status == "finished")
    orch.llm_port_factory = lambda o: ScriptedLlm(by_kind(), fail_for=("N4",))  # (one seat's model is down)
    await orch.start_review()
    await wait_for(lambda: not orch.review_running_ids())
    await orch.start_review()  # N4 again, now with a model that answers
    orch.llm_port_factory = lambda o: ScriptedLlm(by_kind())
    await orch.start_review()
    await wait_for(lambda: not orch.review_running_ids())
    await orch.new_game()
    await orch.bots_changed(roster=True)

    problems = problems_of_events(orch)
    assert problems == {}, {kind: lines[:3] for kind, lines in problems.items()}
    seen = {event.type for event in orch.bus._ring}
    assert {
        "bot.init.progress", "bots.updated", "review.progress", "session.updated", "snapshot", "roster.updated", "player.updated", "game.status",
        "llm.call", "agent.metrics",
    } <= seen, seen
    assert "identities_shuffled" not in seen
    states = {e.data["state"] for e in orch.bus._ring if e.type == "bot.init.progress"}
    assert states == {"queued", "running", "done", "failed"}
    review_states = {e.data["state"] for e in orch.bus._ring if e.type == "review.progress"}
    assert {"queued", "running", "done", "error"} <= review_states
    purposes = {e.data["call"]["purpose"] for e in orch.bus._ring if e.type == "llm.call"}
    assert {"init", "review", "turn"} <= purposes  # (the error log has the three kinds of calls)
    await orch.shutdown()


@async_test
async def test_a_timeout_and_a_cancel_are_valid_events_too(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.learning.jobs as jobs_module

    orch = make_table_orch(monkeypatch, ready=False)
    llm = ScriptedLlm(by_kind(), gate=asyncio.Event())
    orch.llm_port_factory = lambda o: llm
    await orch.start_init()
    await wait_for(lambda: llm.in_flight == 4)
    await orch.cancel_init()
    problems = problems_of_events(orch)
    assert problems == {} and {e.data["state"] for e in orch.bus._ring if e.type == "bot.init.progress"} == {"queued", "running", "cancelled"}
    # (the deadline of a bot is the contract's 120 seconds; a test shortens it, and the page's validator rightly objects - only the state is checked)
    monkeypatch.setattr(jobs_module, "INIT_TIMEOUT_S", 120)
    assert all(e.data["deadline_s"] == 120 for e in orch.bus._ring if e.type == "bot.init.progress")
    await orch.shutdown()
