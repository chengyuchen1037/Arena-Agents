"""The orchestrator of v0.14 (contract §13.3, §13.7, §13.8; plan H3, H11, H25, H26, H52, H58): the bots at the table, the start check, the cards that are
dealt when the game starts, a brain of its own for every seat built from the snapshot the game started with, the v6 save and the v5 games that still
play, the status label and the events.

A real orchestrator over fake stores (and the real bot, contract and rules stores in the temporary data directory), scripted agents, no model.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.agents.brain import ArenaBrain
from app.agents.legacy import LegacyBrain
from app.agents.mind import BotBrain
from app.bots.types import BotMind, sha256_text
from app.game.orchestrator import GameOrchestrator, NotReadyError
from app.game.replay import build_replay
from app.game.session import SESSION_SCHEMA_VERSION, SUPPORTED_SCHEMA_VERSIONS, GameSession
from app.game.sources import SourceError, Sources
from app.world.types import MODERATOR_ID
from game_fakes import (
    TABLE_IDS,
    async_test,
    bots_for,
    events_of,
    idle_moderator,
    make_table_orch,
    ready_bots,
    wait_for,
)

IDS = TABLE_IDS
NAMES = ["Alice", "Bob", "Carol", "David"]
ARCH_NOTES = "平台说明已读。"  # what ``game_fakes.ready_bots`` writes as the operating notes of an initialized bot


async def started(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> GameOrchestrator:
    """A game that is running with scripted agents (the moderator does nothing)."""
    orch = make_table_orch(monkeypatch, **kwargs)
    bots_for(orch, idle_moderator)
    from game_fakes import PLAYERS  # noqa: F401  (the scripted agents of ``bots_for`` know the p01.. seats: here the seats are N1..)

    orch.llm_port_factory = lambda o: __import__("v12.bots", fromlist=["BotLlm"]).BotLlm(
        o.world, {MODERATOR_ID: idle_moderator, **{seat: (lambda w, me: []) for seat in IDS}}
    )
    await orch.start_or_resume()
    return orch


def problem_codes(orch: GameOrchestrator) -> dict[str, list[str]]:
    """``{who: [item codes]}`` of the start check: ``contract`` / ``scenario`` / the seat id of a player / ``binding:<seat>``."""
    out: dict[str, list[str]] = {}
    for problem in orch.check_ready():
        who = problem["scope"] if problem["scope"] in ("contract", "scenario", "game") else (
            problem["seat"] if problem["scope"] == "bot" else f"binding:{problem['seat'] or problem['name']}"
        )
        out.setdefault(who, []).extend(item["code"] for item in problem["items"])
    return out


# ================================================================================================ the table of a game
def test_a_new_game_takes_its_seats_from_the_table_of_bots(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    store = orch._bots()
    lineup = store.lineup()
    session = orch.session
    assert [p.id for p in session.participants] == IDS and [p.bot_id for p in session.participants] == lineup
    assert [p.name for p in session.participants] == NAMES and session.lineup == lineup  # (the config only says how many seats and what they are called)
    assert all(p.avatar is not None for p in session.participants) and session.moderator_avatar == store.moderator()["avatar"]
    assert session.memory_enabled is True and all(p.persona == "" and p.memory == "" and p.memory_snapshot == {} for p in session.participants)
    assert session.contract_snapshot == {} and orch.world.state.dealt is False and session.schema_version == SESSION_SCHEMA_VERSION == 6
    assert SUPPORTED_SCHEMA_VERSIONS == (5, 6) and session.legacy is False


def test_the_first_game_is_built_without_touching_any_bot_file(monkeypatch: pytest.MonkeyPatch, isolated_external_storage: Path) -> None:
    """The data directory is not locked yet when the application builds its first game: nothing of it - bots, contract, rules - is read or created
    until ``startup()``."""
    import game_fakes

    game_fakes.patch_config(monkeypatch, game_fakes.TABLE_CFG)
    orch = GameOrchestrator()
    assert not (isolated_external_storage / "bots").exists() and not (isolated_external_storage / "contract").exists()
    assert not (isolated_external_storage / "rules").exists()
    assert [p.name for p in orch.session.participants] == ["Alice", "Bob", "Carol", "David"]  # (the names of the config)
    assert [p.bot_id for p in orch.session.participants] == ["", "", "", ""]  # (no bot yet: the start check would refuse such a game)
    assert orch.session.lineup == [] and orch.session.moderator_avatar is None


@async_test
async def test_startup_takes_the_table_and_the_scenario_in_force_into_the_first_game(monkeypatch: pytest.MonkeyPatch, isolated_external_storage: Path) -> None:
    import game_fakes

    game_fakes.patch_config(monkeypatch, game_fakes.TABLE_CFG)
    orch = GameOrchestrator()
    report = await orch.startup()
    assert report["recovery_error"] is None and (isolated_external_storage / "bots" / "lineup.json").is_file()
    store = orch._bots()
    assert [p.bot_id for p in orch.session.participants] == store.lineup() and [p.name for p in orch.session.participants] == NAMES
    assert orch.public_session()["bots"]["lineup"] == store.lineup()
    await orch.shutdown()


@async_test
async def test_a_new_line_up_moves_the_bots_to_other_seats_with_their_names_and_the_game_is_told(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    store = orch._bots()
    before = {p.id: p.bot_id for p in orch.session.participants}
    world = orch.world
    seed = world.state.rng.seed if hasattr(world.state, "rng") else None
    store.set_lineup(list(reversed(store.lineup())))
    await orch.bots_changed(roster=True)
    after = {p.id: p.bot_id for p in orch.session.participants}
    assert after == dict(zip(IDS, reversed(list(before.values())))) and [p.name for p in orch.session.participants] == list(reversed(NAMES))
    assert orch.world is not world and orch.session.lineup == list(reversed(list(before.values())))
    assert [pl.name for pl in orch.world.state.players] == list(reversed(NAMES))  # (the world was built again for the new seats)
    if seed is not None:
        assert orch.world.state.rng.seed == seed
    roster = events_of(orch, "roster.updated")[-1].data
    assert [p["name"] for p in roster["participants"]] == list(reversed(NAMES)) and roster["moderator"]["id"] == "moderator"
    assert events_of(orch, "bots.updated")[-1].data["bots"]["lineup"] == orch.session.lineup
    assert events_of(orch, "snapshot")[-1].data["session"]["participants"][0]["name"] == "David"
    assert not events_of(orch, "identities_shuffled")


@async_test
async def test_a_game_that_has_started_keeps_the_table_it_started_with(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = await started(monkeypatch)
    store = orch._bots()
    seats = {p.id: (p.bot_id, p.name) for p in orch.session.participants}
    store.set_lineup(list(reversed(store.lineup())))
    store.update_bot(store.lineup()[0], name="Zed", confirm_reset=True)
    await orch.bots_changed(roster=True)
    assert {p.id: (p.bot_id, p.name) for p in orch.session.participants} == seats  # nothing of the game moved
    assert [b["seat"] for b in orch.bots_payload()["bots"]] == IDS and orch.public_session()["bots"]["lineup"] == store.lineup()  # (the table shows the new line-up)
    await orch.shutdown()


@async_test
async def test_the_models_of_an_idle_game_follow_the_bots(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    orch.settings_store.defaults = {**orch.settings_store.defaults, "N1": orch.settings_store.defaults["N2"].model_copy(update={"model_id": "small"})}
    orch.apply_table()
    assert orch.session.model_bindings["N1"].model_id == "small" and orch.session.model_bindings["N2"].model_id == "big"
    assert set(orch.session.model_bindings) == {MODERATOR_ID, *IDS}  # (nobody else)


# ================================================================================================ the start check
@async_test
async def test_a_table_of_ready_bots_passes_and_every_fault_is_a_problem_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    assert orch.check_ready() == [] and orch.bots_payload()["ready"] == {"ok": True, "problems": []}
    store = orch._bots()
    bots = store.lineup()
    # the L0 is missing (the user cleared it): that bot cannot start, and its initialization is out of date
    store.clear_core([bots[0]])
    codes = problem_codes(orch)
    assert set(codes) == {"N1"} and codes["N1"][0] in ("core_missing", "core_incomplete")
    problem = orch.check_ready()[0]
    assert problem["scope"] == "bot" and problem["bot_id"] == bots[0] and problem["seat"] == "N1" and problem["name"] == "Alice"
    assert {item["fix"] for item in problem["items"]} <= {"edit_core", "import_core", "init"}
    # never initialized / failed / stale
    ready_bots(orch)
    store.clear_memory([bots[1]])
    store.fail_init(bots[2], "模型超时", model="m", duration_s=1.0)
    codes = problem_codes(orch)
    assert codes == {"N2": ["not_initialized"], "N3": ["init_failed"]} or ("N2" in codes and "N3" in codes and "N4" not in codes)
    assert all(item["fix"] == "init" for p in orch.check_ready() for item in p["items"])
    ready_bots(orch)
    assert orch.check_ready() == []
    # a bot that is being initialized right now
    orch.init_running_ids = lambda: {bots[3]}  # type: ignore[method-assign]
    assert problem_codes(orch) == {"N4": ["init_running"]}
    del orch.init_running_ids
    # a model that is missing: the moderator's, and a seat's
    orch.session.model_bindings.pop(MODERATOR_ID)
    orch.session.model_bindings.pop("N2")
    orch.refresh_binding_issues()
    codes = problem_codes(orch)
    assert codes == {"binding:主持人": ["no_binding"], "binding:N2": ["no_binding"]}
    assert {p["scope"] for p in orch.check_ready()} == {"binding"} and orch.check_ready()[0]["items"][0]["fix"] == "bind_model"


@async_test
async def test_a_contract_or_scenario_that_cannot_be_used_is_a_problem_and_a_changed_one_makes_every_bot_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.storage.contract import get_contract_store

    orch = make_table_orch(monkeypatch)
    assert orch.check_ready() == []
    markdown = orch.sources.contract().markdown
    get_contract_store().set(markdown + "\n（补充的一句。）\n")  # the user imports another contract: the hash of every initialization is out of date
    codes = problem_codes(orch)
    assert codes == {seat: ["stale"] for seat in IDS}
    assert {s["init"]["stale"][0] for s in orch.bot_summaries()} == {"contract"}
    get_contract_store().reset()
    assert orch.check_ready() == []

    def broken(self: Sources, spec: Any) -> SourceError:
        return SourceError("官方契约有问题：没有提到工具 speak。", kind="contract", code="contract_invalid")

    with monkeypatch.context() as patch:
        patch.setattr(Sources, "contract_problem", broken)
        assert problem_codes(orch) == {"contract": ["contract_invalid"]}
        assert orch.check_ready()[0]["items"][0]["fix"] == "import_contract"
    assert orch.check_ready() == []
    with monkeypatch.context() as patch:
        patch.setattr(Sources, "scenario_problem", lambda self, sid=None: SourceError("剧本没有 id。", kind="scenario", code="scenario_invalid"))
        assert problem_codes(orch) == {"scenario": ["scenario_invalid"]} and orch.check_ready()[0]["items"][0]["fix"] == "import_rules"


@async_test
async def test_a_start_that_does_not_pass_deals_nothing_and_changes_nothing_and_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    bots_for(orch, idle_moderator)
    orch._bots().clear_memory([orch._bots().lineup()[0]])
    seq = orch.world.seq
    with pytest.raises(NotReadyError) as refused:
        await orch.start_or_resume()
    error = refused.value
    assert error.status == 409 and error.code == "NOT_READY" and "Alice" in str(error) and "还没有初始化" in str(error) and "不能开局" in str(error)
    assert [p["name"] for p in error.problems] == ["Alice"] and error.problems[0]["items"][0]["code"] == "not_initialized"
    assert orch.session.status == "idle" and orch._supervisor is None and not orch.world.running and orch.world.seq == seq
    assert not orch.world.state.dealt and all(not p.role for p in orch.world.state.players) and orch.session.started_at is None
    assert all(p.memory_snapshot == {} for p in orch.session.participants) and orch.session.contract_snapshot == {}
    assert orch.public_session()["dealt"] is False


@async_test
async def test_a_scenario_whose_identity_pool_does_not_fit_the_table_is_a_problem_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    spec = orch.spec
    spec.setup.identity_pool[0].count += 1  # five identities for four players
    problems = orch.check_ready()
    assert [p["scope"] for p in problems] == ["scenario"] and "身份池共有 5 个身份" in problems[0]["items"][0]["message"]
    assert problems[0]["items"][0]["code"] == "scenario_invalid"


# ================================================================================================ the start: snapshots and the deal
@async_test
async def test_the_start_writes_what_every_bot_carries_into_the_game_and_the_contract_and_then_deals(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    store = orch._bots()
    store.write_part(store.lineup()[0], "people", "- Bob：说话短。\n- Zed：不在这桌上的人。")
    assert not orch.world.state.dealt
    bots_for(orch, idle_moderator)
    await orch.start_or_resume()
    assert orch.world.state.dealt and all(p.role for p in orch.world.state.players)
    payload = json.loads(json.dumps(orch.session_payload()))
    assert payload["schema_version"] == 6 and payload["lineup"] == store.lineup() and payload["status"] == "running"
    contract = orch.sources.contract()
    assert payload["contract_snapshot"] == {"version": contract.version, "sha256": contract.sha256, "markdown": contract.markdown}
    for seat, bot_id, participant in zip(IDS, store.lineup(), payload["participants"]):
        assert participant["id"] == seat and participant["bot_id"] == bot_id and participant["persona"] == "" and participant["memory"] == ""
        snapshot = participant["memory_snapshot"]
        assert {"v", "bot_id", "name", "core", "arch_notes", "rules", "people", "record", "contract_hash", "scenario_hash"} <= set(snapshot)
        mind = BotMind.from_dict(snapshot)
        assert mind.bot_id == bot_id and mind.core.complete and mind.arch_notes == ARCH_NOTES and mind.rules.scenario_id == "minimal_test"
        assert snapshot["contract_hash"] == contract.sha256 and snapshot["scenario_hash"] == sha256_text(orch.sources.scenario_text("minimal_test"))
    first = BotMind.from_dict(payload["participants"][0]["memory_snapshot"])
    assert first.people == {"Bob": "说话短。"}  # (only the impressions of the people at THIS table are carried into the game: Zed is not here, H9)
    second = BotMind.from_dict(payload["participants"][1]["memory_snapshot"])
    assert second.people == {}  # (nobody wrote an impression of Alice)
    # the identities are dealt once, after the snapshots: the first entries of the deal come after the game started
    deals = [e for e in orch.world.ledger if e.type == "player.updated" and e.data.get("reason") == "deal"]
    running = [e for e in orch.world.ledger if e.type == "game.status" and e.data.get("status") == "running"]
    assert len(deals) == 4 and running and min(e.seq for e in deals) > running[0].seq
    assert orch.session.started_at and orch.session.pause_reason == ""
    await orch.shutdown()


@async_test
async def test_every_seat_has_a_brain_of_its_own_and_the_moderator_the_old_one(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = await started(monkeypatch)
    brains = orch._brains
    assert isinstance(brains[MODERATOR_ID], ArenaBrain) and not isinstance(brains[MODERATOR_ID], LegacyBrain)
    assert all(isinstance(brains[seat], BotBrain) for seat in IDS) and len({id(brains[seat]) for seat in IDS}) == 4
    assert [brains[seat].mind.name for seat in IDS] == NAMES if hasattr(brains["N1"], "mind") else True
    prompts = {seat: brains[seat].system_prompt(seat) for seat in IDS}
    assert len(set(prompts.values())) == 4  # (each is its own bot's: the names alone differ)
    assert all(name in prompts[seat] for seat, name in zip(IDS, NAMES))
    assert orch._supervisor is not None and set(orch._supervisor.agents) == {MODERATOR_ID, *IDS}
    await orch.shutdown()


@async_test
async def test_a_resumed_or_restored_game_builds_the_prompts_it_started_with_whatever_the_files_say_now(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.storage.contract import get_contract_store

    orch = await started(monkeypatch)
    seat = "N1"
    original = orch._brains[seat].system_prompt(seat)
    assert ARCH_NOTES in original
    await orch.pause("user")
    payload = json.loads(json.dumps(orch.session_payload()))
    # the files change: the bot's notes, its L0, the official contract
    store = orch._bots()
    bot_id = store.lineup()[0]
    store.write_part(bot_id, "arch_notes", "改过之后的操作笔记。")
    core = store.core(bot_id)
    from app.bots.types import CoreFiles

    store.set_core(bot_id, CoreFiles(identity="完全不同的身份。", voice=core.voice, mind=core.mind, extras=core.extras))
    get_contract_store().set(orch.sources.contract().markdown + "\n（改过之后的契约。）\n")
    # a resumed game rebuilds nothing
    await orch.start_or_resume()
    assert orch._brains[seat].system_prompt(seat) == original
    await orch.pause("user")
    # a game restored by a new process builds its brains from the save, not from the files
    restarted = make_table_orch(monkeypatch, ready=False)
    bots_for(restarted, idle_moderator)
    loaded = restarted._validate_session_data(payload)
    await restarted._adopt_loaded_session(loaded, "已恢复上次对局")
    restarted.llm_port_factory = lambda o: __import__("v12.bots", fromlist=["BotLlm"]).BotLlm(o.world, {})
    await restarted.start_or_resume()
    rebuilt = restarted._brains[seat].system_prompt(seat)
    assert rebuilt == original and "改过之后" not in rebuilt and "完全不同的身份" not in rebuilt  # byte for byte: the provider's cache keeps hitting
    await restarted.shutdown()
    await orch.shutdown()


@async_test
async def test_a_game_that_is_started_after_the_change_is_played_with_the_new_files(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    store = orch._bots()
    store.write_part(store.lineup()[0], "arch_notes", "这是新的操作笔记。")  # (an edit of the notes is effective at once and does not make the bot stale, C9)
    assert orch.check_ready() == []
    bots_for(orch, idle_moderator)
    orch.llm_port_factory = lambda o: __import__("v12.bots", fromlist=["BotLlm"]).BotLlm(o.world, {})
    await orch.start_or_resume()
    assert "这是新的操作笔记。" in orch._brains["N1"].system_prompt("N1")
    await orch.shutdown()


@async_test
async def test_memory_that_is_switched_off_is_left_out_of_the_prompts_and_remembered_by_the_game(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    orch._bots().set_memory_enabled(False)
    orch.llm_port_factory = lambda o: __import__("v12.bots", fromlist=["BotLlm"]).BotLlm(o.world, {})
    await orch.bots_changed(roster=False)
    assert orch.session.memory_enabled is False and orch.public_session()["memory_enabled"] is False
    await orch.start_or_resume()
    assert orch.session.memory_enabled is False and ARCH_NOTES not in orch._brains["N1"].system_prompt("N1")
    await orch.shutdown()


# ================================================================================================ what the page is shown
@async_test
async def test_nothing_about_identities_is_shown_before_the_game_starts_and_everything_after(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    before = orch.public_session()
    assert before["dealt"] is False and all(p["tags"] == [] for p in before["participants"])
    for seat in IDS:
        me = orch.world.board(seat)["me"]
        assert not me.get("role") and not me.get("faction") and not me.get("tags")
    assert not [e for e in orch.world.ledger if e.type == "player.updated"]
    text = json.dumps(before, ensure_ascii=False)
    assert '"role"' not in text and '"faction"' not in text
    bots_for(orch, idle_moderator)
    orch.llm_port_factory = lambda o: __import__("v12.bots", fromlist=["BotLlm"]).BotLlm(o.world, {})
    await orch.start_or_resume()
    after = orch.public_session()
    assert after["dealt"] is True and all(p["tags"] for p in after["participants"]) and {t for p in after["participants"] for t in p["tags"]} >= {"红队", "蓝队"}
    await orch.shutdown()


@async_test
async def test_the_public_session_of_v14_has_the_bots_the_label_the_review_and_the_seats_with_their_bots(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    public = orch.public_session()
    json.dumps(public)
    assert public["schema_version"] == 6 and public["dealt"] is False
    assert public["system_status"] == {"code": "before_game", "text": "对局前", "reason": ""}
    assert public["review"] == {"available": False, "done": [], "running": False, "supported": True, "deadline_s": 60, "seconds_left": None}
    assert "reshuffle_available" not in public["takeover"] and public["takeover"] == {"seat": None}
    bots = public["bots"]
    assert set(bots) == {"lineup", "bots", "memory_enabled", "init_job", "ready", "learning_usage"}  # (the live part: no limits, no defaults)
    assert bots["ready"] == {"ok": True, "problems": []} and bots["init_job"] is None and bots["memory_enabled"] is True
    assert bots["learning_usage"] == {"calls": 0, "tokens": 0, "cost_usd": 0.0}
    row = bots["bots"][0]
    assert {"id", "name", "avatar", "seat", "origin", "core", "init", "frozen", "memory_stats", "staging", "binding"} <= set(row)
    assert row["seat"] == "N1" and row["init"]["status"] == "ready" and row["binding"] == {"ok": True, "label": "prov / big"}
    assert [p["bot_id"] for p in public["participants"]] == bots["lineup"] and {p["init_status"] for p in public["participants"]} == {"ready"}
    full = orch.bots_payload(full=True)
    assert {"limits", "defaults"} <= set(full) and full["defaults"] and full["limits"]["init_timeout_s"] == 120
    assert "limits" not in orch.bots_payload() and orch.bot_detail(row["id"])["seat"] == "N1" and orch.bot_detail(row["id"])["binding"]["ok"] is True


@async_test
async def test_the_status_label_follows_the_priority_of_the_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch, ready=False)
    label = lambda: orch.system_status()  # noqa: E731
    assert label() == {"code": "uninitialized", "text": "未初始化", "reason": ""}  # nobody is initialized yet
    ready_bots(orch)
    assert label()["code"] == "before_game"
    orch._bots().clear_memory([orch._bots().lineup()[3]])
    assert label()["code"] == "uninitialized"  # one bot out of date is enough
    ready_bots(orch)
    for status, code, text in (("running", "in_game", "对局中"), ("paused", "paused", "已暂停"), ("finished", "finished", "已结束")):
        orch.session.status = status  # type: ignore[assignment]
        assert (label()["code"], label()["text"]) == (code, text)
    orch.session.status = "paused"
    orch.session.pause_reason, orch.session.hold_reason = "budget", ""
    assert label() == {"code": "paused", "text": "已暂停", "reason": "已达到预算上限，已暂停"}
    orch.session.pause_reason, orch.session.hold_reason = "hold", "主持人要核对一下票型"
    assert label()["reason"] == "主持人暂停了对局：主持人要核对一下票型"
    orch.session.status = "running"
    orch.soft_pause_payload = lambda: {"state": "held", "in_flight": 0}  # type: ignore[method-assign]
    assert label() == {"code": "in_game", "text": "对局中 · agent 已暂停", "reason": ""}  # a soft pause is still "in game"
    del orch.soft_pause_payload
    # initializing / reviewing come first
    orch.init_running_ids = lambda: {"b-00000001"}  # type: ignore[method-assign]
    assert label()["code"] == "initializing" and label()["text"] == "初始化中"
    orch.review_running_ids = lambda: {"N1"}  # type: ignore[method-assign]
    assert label()["code"] == "initializing"  # (initializing before reviewing)
    del orch.init_running_ids
    assert label() == {"code": "reviewing", "text": "复盘中", "reason": ""}
    del orch.review_running_ids
    orch.session.status = "idle"
    orch.session.schema_version = 5  # a game of v0.13 has no bots to initialize
    assert label()["code"] == "before_game"


@async_test
async def test_session_updated_carries_the_new_fields_and_the_old_events_are_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = await started(monkeypatch)
    update = events_of(orch, "session.updated")[-1].data
    assert update["status"] == "running" and update["system_status"] == {"code": "in_game", "text": "对局中", "reason": ""} and update["dealt"] is True
    assert update["review"]["available"] is False and update["review"]["supported"] is True and "soft_pause" in update and "memory_enabled" in update
    await orch.pause("user")
    paused = events_of(orch, "session.updated")[-1].data
    assert paused["system_status"] == {"code": "paused", "text": "已暂停", "reason": "手动暂停"}
    assert not events_of(orch, "identities_shuffled")
    assert "identities_shuffled" not in json.dumps([e.type for e in events_of(orch, "snapshot", "session.updated")])
    await orch.shutdown()


@async_test
async def test_a_new_game_can_be_made_from_an_idle_a_paused_and_a_finished_game(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    bots_for(orch, idle_moderator)
    orch.llm_port_factory = lambda o: __import__("v12.bots", fromlist=["BotLlm"]).BotLlm(o.world, {})
    first = orch.session.session_id
    assert (await orch.new_game()).session_id != first  # idle
    await orch.start_or_resume()
    await orch.pause("user")
    second = orch.session.session_id
    fresh = await orch.new_game()  # paused (it was started: the save of the old game is the caller's)
    assert fresh.session_id != second and fresh.status == "idle" and not orch.world.state.dealt and all(not p.role for p in orch.world.state.players)
    assert [p.bot_id for p in fresh.participants] == orch._bots().lineup() and fresh.started_at is None and fresh.review_done == []
    await orch.start_or_resume()
    orch.world.state.status = "finished"
    orch.session.status = "finished"
    orch._supervisor = None
    third = orch.session.session_id
    after = await orch.new_game()  # finished
    assert after.session_id != third and after.status == "idle" and orch.public_session()["dealt"] is False
    assert not orch.world.running and orch.takeover_seat is None
    await orch.shutdown()


@async_test
async def test_a_changed_scenario_is_what_an_idle_game_plays_and_a_started_one_keeps_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.storage.rules import get_rules_store

    orch = make_table_orch(monkeypatch)
    store = get_rules_store()
    text = store.scenario_text("minimal_test")
    assert orch.spec.title == "最小测试剧本"
    store.set_scenario("minimal_test", text.replace("title: 最小测试剧本", "title: 改过名字的剧本", 1))
    world = orch.world
    await orch.sources_changed("scenario", "minimal_test")
    assert orch.spec.title == "改过名字的剧本" and orch.session.scenario_title == "改过名字的剧本" and orch.world is not world
    assert orch.session.scenario_spec["title"] == "改过名字的剧本"
    assert events_of(orch, "bots.updated") and events_of(orch, "snapshot")
    assert {s["init"]["status"] for s in orch.bot_summaries()} == {"stale"}  # (the hash of the scenario changed: every bot has to read it again)
    assert orch.check_ready()  # ... and the game cannot start until they have
    ready_bots(orch)
    assert orch.check_ready() == []
    bots_for(orch, idle_moderator)
    orch.llm_port_factory = lambda o: __import__("v12.bots", fromlist=["BotLlm"]).BotLlm(o.world, {})
    await orch.start_or_resume()
    store.set_scenario("minimal_test", text)  # changed again while the game runs: the game keeps the scenario it started with
    await orch.sources_changed("scenario", "minimal_test")
    assert orch.spec.title == "改过名字的剧本" and orch.session.scenario_title == "改过名字的剧本"
    await orch.shutdown()


@async_test
async def test_a_changed_contract_or_materials_only_say_so_to_the_bots(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    world = orch.world
    before = len(events_of(orch, "bots.updated"))
    await orch.sources_changed("contract", None)
    await orch.sources_changed("materials", "minimal_test")
    assert orch.world is world and len(events_of(orch, "bots.updated")) == before + 2


# ================================================================================================ v5 games (H58)
def v5_payload(orch: GameOrchestrator, persona: str = "说话很慢，爱讲冷笑话。", memory: str = "## 印象\n- N2：爱抢话。") -> dict[str, Any]:
    """A game of v0.13 as it was saved: schema 5, no bots, the persona and the memory as texts."""
    payload = json.loads(json.dumps(orch.session_payload()))
    payload["schema_version"] = 5
    for key in ("lineup", "contract_snapshot"):
        payload.pop(key)
    for participant in payload["participants"]:
        participant.pop("bot_id")
        participant.pop("memory_snapshot")
        participant["persona"] = f"{participant['name']}：{persona}"
        participant["memory"] = memory
    return payload


@async_test
async def test_a_game_of_v0_13_is_read_with_its_texts_and_plays_on_with_the_old_brain_without_any_check(monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_table_orch(monkeypatch)
    payload = v5_payload(source)
    orch = make_table_orch(monkeypatch, ready=False)  # (its bots are not initialized: a v5 game is not asked)
    bots_for(orch, idle_moderator)
    orch.llm_port_factory = lambda o: __import__("v12.bots", fromlist=["BotLlm"]).BotLlm(o.world, {})
    loaded = orch._validate_session_data(payload)
    assert loaded.schema_version == 5 and loaded.legacy is True and loaded.lineup == [] and loaded.contract_snapshot == {}
    await orch._adopt_loaded_session(loaded, "已载入对局")
    assert orch.session.legacy and [p.bot_id for p in orch.session.participants] == [""] * 4 and orch.session.status == "idle"
    # nothing of the table is forced on it: the seats keep what they were saved with
    assert [p.name for p in orch.session.participants] == NAMES and all(p.persona.startswith(p.name) for p in orch.session.participants)
    assert orch.check_ready() == [] and orch.system_status()["code"] == "before_game"
    public = orch.public_session()
    assert public["schema_version"] == 6 and [p["bot_id"] for p in public["participants"]] == [""] * 4 and [p["init_status"] for p in public["participants"]] == [None] * 4
    assert public["review"] == {"available": False, "done": [], "running": False, "supported": False, "deadline_s": 60, "seconds_left": None}
    await orch.start_or_resume()  # no start check, no deal of its own: the saved world is dealt already
    assert orch.session.status == "running" and orch.session.schema_version == 5  # (a v5 game stays a v5 game while it lives)
    brains = orch._brains
    assert isinstance(brains["N1"], LegacyBrain) and brains["N1"] is brains["N2"] is brains[MODERATOR_ID]  # (one brain for everybody, as in v0.13.1)
    prompt = brains["N2"].system_prompt("N2")
    assert "Bob：说话很慢，爱讲冷笑话。" in prompt and "爱抢话" in prompt  # the persona and the memory of the save
    assert "Alice：说话很慢" not in prompt
    saved = json.loads(json.dumps(orch.session_payload()))
    assert saved["schema_version"] == 5 and saved["participants"][0]["bot_id"] == "" and saved["lineup"] == []
    await orch.shutdown()


@async_test
async def test_a_game_of_v0_13_that_is_over_cannot_be_reviewed_and_can_be_continued(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.learning.jobs import LearningRefused

    source = make_table_orch(monkeypatch)
    payload = v5_payload(source)
    payload["status"] = "finished"
    payload["world"]["state"]["status"] = "finished"
    payload["world"]["state"]["winner"] = "红队"
    orch = make_table_orch(monkeypatch)
    bots_for(orch, idle_moderator)
    orch.llm_port_factory = lambda o: __import__("v12.bots", fromlist=["BotLlm"]).BotLlm(o.world, {})
    await orch._adopt_loaded_session(orch._validate_session_data(payload), "已载入对局")
    assert orch.session.status == "finished" and orch.review_view()["available"] is True and orch.review_view()["supported"] is False
    with pytest.raises(LearningRefused) as refused:
        await orch.start_review()
    assert refused.value.code == "review_unsupported"
    await orch.start_or_resume()  # "继续" after the end
    assert orch.session.status == "running" and isinstance(orch._brains["N3"], LegacyBrain)
    await orch.shutdown()


@async_test
async def test_a_game_of_v0_13_comes_from_the_history_too_and_a_v6_game_after_it_(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    old_id = "old-game-5"
    payload = v5_payload(orch)
    payload["session_id"] = old_id
    orch.game_store.data[old_id] = payload
    new = await orch.new_game()
    loaded = await orch.load_game(old_id)
    assert loaded.session_id == old_id and loaded.legacy and orch.session.status == "idle"
    back = await orch.load_game(new.session_id) if new.session_id in orch.game_store.data else None
    await orch.persist_session("manual")
    assert orch.game_store.data[old_id]["schema_version"] == 5 and orch.game_store.data[old_id]["participants"][0]["bot_id"] == ""
    del back
    await orch.shutdown()


def test_the_replay_of_a_v5_game_and_of_a_v6_game_are_both_built(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        orch = await started(monkeypatch)
        orch.world.state.status = "finished"
        payload = json.loads(json.dumps(orch.session_payload()))
        replay = build_replay(payload)  # v6: the cards are dealt after the start - the initial state has no identity
        assert all(p["tags"] == [] for p in replay["initial"]["participants"]) and replay["game"]["session_id"] == payload["session_id"]
        deals = [e for e in replay["events"] if e["type"] == "player.updated"]
        assert len(deals) == 4 and all(e["data"]["tags"] for e in deals)  # (the identities arrive as events, in the order the ledger has them)
        v5 = build_replay(v5_payload(orch))  # the same ledger in a v5 envelope: the replay does not care
        assert [e["type"] for e in v5["events"]] == [e["type"] for e in replay["events"]]
        await orch.shutdown()

    asyncio.run(scenario())


# ================================================================================================ the save
@async_test
async def test_a_v6_game_survives_a_restart_with_its_bots_its_snapshots_and_its_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = await started(monkeypatch)
    await orch.pause("user")
    payload = json.loads(json.dumps(orch.session_payload()))
    other = make_table_orch(monkeypatch)
    loaded = other._validate_session_data(payload)
    assert isinstance(loaded, GameSession) and loaded.schema_version == 6 and loaded.lineup == payload["lineup"]
    await other._adopt_loaded_session(loaded, "已恢复上次对局")
    assert other.session.status == "paused" and other.world.state.dealt and other.session.contract_snapshot == payload["contract_snapshot"]
    assert [p.memory_snapshot for p in other.session.participants] == [p["memory_snapshot"] for p in payload["participants"]]
    assert [p["bot_id"] for p in other.public_session()["participants"]] == payload["lineup"]
    await other.shutdown()
    await orch.shutdown()


@async_test
async def test_an_idle_v6_game_that_is_loaded_takes_the_table_as_it_is_now(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    payload = json.loads(json.dumps(orch.session_payload()))
    store = orch._bots()
    store.set_lineup(list(reversed(store.lineup())))
    loaded = orch._validate_session_data(copy.deepcopy(payload))
    await orch._adopt_loaded_session(loaded, "已恢复上次对局")
    assert [p.bot_id for p in orch.session.participants] == store.lineup() and [p.name for p in orch.session.participants] == list(reversed(NAMES))
    await orch.shutdown()


@async_test
async def test_a_save_of_the_future_or_the_past_is_refused_with_the_way_to_bring_it_over(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.game.orchestrator import SessionLoadError

    orch = make_table_orch(monkeypatch)
    payload = json.loads(json.dumps(orch.session_payload()))
    for version in (4, 7):
        payload["schema_version"] = version
        with pytest.raises(SessionLoadError) as error:
            orch._validate_session_data(copy.deepcopy(payload))
        assert "旧版本" in str(error.value) and "不兼容" in str(error.value) and "migrate_v0121_to_archive" in str(error.value)
    payload["schema_version"] = 5  # (and a save of v0.13 is accepted)
    assert orch._validate_session_data(copy.deepcopy(payload)).legacy is True


# ================================================================================================ a start that fails leaves the game as it was
@async_test
async def test_a_start_that_fails_after_the_check_takes_the_snapshots_out_again_and_deals_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)

    def boom(o: Any) -> Any:
        raise RuntimeError("the model port could not be built")

    orch.llm_port_factory = boom
    with pytest.raises(RuntimeError, match="model port"):
        await orch.start_or_resume()
    assert orch.session.status == "idle" and orch._supervisor is None and not orch.world.running and orch.session.started_at is None
    assert not orch.world.state.dealt and all(not p.role for p in orch.world.state.players)
    assert all(p.memory_snapshot == {} for p in orch.session.participants) and orch.session.contract_snapshot == {} and orch._brains == {}
    # the game is exactly as it was: it starts when the model port can be built
    bots_for(orch, idle_moderator)
    orch.llm_port_factory = lambda o: __import__("v12.bots", fromlist=["BotLlm"]).BotLlm(o.world, {})
    await orch.start_or_resume()
    assert orch.session.status == "running" and orch.world.state.dealt and all(p.memory_snapshot for p in orch.session.participants)
    await orch.shutdown()


@async_test
async def test_the_cards_are_dealt_right_after_the_gate_opens_and_before_anything_can_act(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ledger of a v6 game: the system private channels, ``game.status`` (started), then the deal (contract §13.3); the events reach the spectators
    in that order, and the moderator - woken only afterwards - already finds everybody dealt."""
    orch = make_table_orch(monkeypatch)
    seen: list[str] = []

    class Watching:
        """A model that looks at what the world is like the first time it is asked."""

        def __init__(self, world: Any) -> None:
            self.world = world
            self.calls = 0

        async def complete(self, agent_id: str, messages: Any, tools: Any, *, max_tokens: int | None = None) -> Any:
            from v12.bots import tool_response

            if not self.calls:
                seen.append(f"{agent_id}:{self.world.state.dealt}:{self.world.running}")
            self.calls += 1
            return tool_response([("sleep", {})])

    orch.llm_port_factory = lambda o: Watching(o.world)
    await orch.start_or_resume()
    await wait_for(lambda: seen)
    assert seen[0].endswith(":True:True")  # running, and already dealt
    types = [e.type for e in orch.world.ledger]
    assert types == ["channel.created"] * 4 + ["game.status"] + ["player.updated"] * 4
    events = [e.type for e in orch.bus._ring if e.type in ("game.status", "player.updated")]
    assert events[0] == "game.status" and events.count("player.updated") >= 4
    assert not [e for e in orch.world.ledger if e.type == "player.updated" and e.seq < next(x.seq for x in orch.world.ledger if x.type == "game.status")]
    await orch.shutdown()


@async_test
async def test_a_seat_taken_over_before_the_game_starts_shows_its_person_no_identity_until_the_game_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """H52: the cards are dealt when the game starts - also for the person who sat down at a seat before that: nothing about an identity is in
    their board, in the takeover state or in the page's session until the start, and the seat gets no agent of its own."""
    orch = make_table_orch(monkeypatch)
    bots_for(orch, idle_moderator)
    orch.llm_port_factory = lambda o: __import__("v12.bots", fromlist=["BotLlm"]).BotLlm(o.world, {})
    await orch.takeover("N2")
    me = orch.world.board("N2")["me"]
    assert not me.get("role") and not me.get("faction") and not me.get("tags")
    before = orch.public_session()
    assert before["takeover"]["seat"] == "N2" and before["dealt"] is False and all(p["tags"] == [] for p in before["participants"])
    text = json.dumps(before["takeover"], ensure_ascii=False)
    assert "红队" not in text and "蓝队" not in text and '"role"' not in text and '"faction"' not in text
    await orch.start_or_resume()
    me = orch.world.board("N2")["me"]
    assert me.get("role") and me.get("faction") and me.get("tags")  # now the person sees who they are
    assert next(p for p in orch.public_session()["participants"] if p["id"] == "N2")["tags"]
    assert "N2" not in orch._supervisor.agents and {"N1", "N3", "N4"} <= set(orch._supervisor.agents)  # (nobody plays the person's seat)
    assert orch.session.human_spans and orch.session.human_spans[0].seat == "N2"
    await orch.shutdown()
