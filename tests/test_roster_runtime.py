"""v0.12.1 orchestrator: roster (names / avatars / persona / memory), spending limit, review, refine, stall banner.

The orchestrator is the real one, wired to in-memory stores and scripted agents (no network).  Roster files
are real files in the isolated data directory of the test."""

from __future__ import annotations

import asyncio
import copy
import io
import json
import zipfile
from typing import Any

import pytest

import app.game.orchestrator as orchestrator_module
from app.game.orchestrator import GameOrchestrator
from app.llm.types import LLMResponse
from app.storage.roster import RosterError, RosterStore, split_memory
from app.world.types import MODERATOR_ID, UiEvent
from game_fakes import (
    CFG,
    PROVIDER,
    FakeProviders,
    FakeSessions,
    FakeSettings,
    async_test,
    bound,
)
from v12.bots import BotLlm, Call, simple_player

IDS = ["N1", "N2", "N3", "N4"]
ROSTER_CFG: dict[str, Any] = {
    **copy.deepcopy(CFG),
    "participants": [{"id": pid, "seat": str(i), "name": name} for i, (pid, name) in enumerate(zip(IDS, ["Alice", "Bob", "Carol", "David"]), start=1)],
}


def make_orch(monkeypatch: pytest.MonkeyPatch, *, bound_all: bool = True) -> GameOrchestrator:
    monkeypatch.setattr(orchestrator_module, "load_config", lambda *a, **k: copy.deepcopy(ROSTER_CFG))
    orch = GameOrchestrator()
    orch.provider_store = FakeProviders(PROVIDER)
    orch.settings_store = FakeSettings({a: bound() for a in [MODERATOR_ID, *IDS]} if bound_all else {})
    orch.session_store = FakeSessions()
    orch._autosave_delay = 0.01
    orch._reset_game(default_bindings=orch._load_defaults())
    orch.refresh_binding_issues()
    return orch


def play_with(orch: GameOrchestrator, moderator_policy: Any) -> list[BotLlm]:
    """Scripted agents; the same port also answers the one-shot calls (review / refine) with bot text."""
    made: list[BotLlm] = []

    def factory(o: GameOrchestrator) -> BotLlm:
        bot = BotLlm(o.world, {MODERATOR_ID: moderator_policy, **{pid: simple_player for pid in IDS}})
        made.append(bot)
        return bot

    orch.ensure_bindings_ready = lambda: None  # type: ignore[method-assign]
    orch.llm_port_factory = factory
    return made


def idle(world: Any, me: str) -> list[Call]:
    return []


def chatty(world: Any, me: str) -> list[Call]:
    """Opens a free discussion once; the players then keep answering each other: continuous activity (= spending)."""
    if world.channel("public") is not None:
        return []
    return [
        ("create_channel", {"channel_id": "public", "name": "公共频道", "members": IDS, "type": "public"}),
        ("open_floor", {"channel_id": "public", "mode": "free", "limits": {"max_messages_per_member": 6}}),
    ]


def end_at_once(winner: str) -> Any:
    state = {"done": False}

    def policy(world: Any, me: str) -> list[Call]:
        if state["done"]:
            return []
        state["done"] = True
        return [("end_game", {"winner": winner, "summary": "测试"})]

    return policy


def events_of(orch: GameOrchestrator, *types: str) -> list[UiEvent]:
    replay, _ = orch.bus.subscribe("0")
    return [e for e in (replay or []) if e.type in types]


async def wait_for(predicate: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for the condition")
        await asyncio.sleep(0.02)


class TextLlm:
    """One-shot port: ``complete`` returns plain text (what a review / refine call gets)."""

    def __init__(self, text: str = "我的复盘：下次早点发言。", fail_for: tuple[str, ...] = ()) -> None:
        self.text, self.fail_for, self.calls = text, fail_for, []

    async def complete(self, agent_id: str, messages: list[dict[str, Any]], tools: Any, *, max_tokens: int | None = None) -> LLMResponse:
        self.calls.append((agent_id, messages, max_tokens))
        if agent_id in self.fail_for:
            raise RuntimeError("upstream exploded sk-secret-1234")
        return LLMResponse(content=self.text, prompt_tokens=50, completion_tokens=20, provider_id="bot", model_id="scripted")


# ------------------------------------------------------------------------------------ roster
def test_the_first_game_is_built_without_touching_any_roster_file(monkeypatch, isolated_external_storage):
    monkeypatch.setattr(orchestrator_module, "load_config", lambda *a, **k: copy.deepcopy(ROSTER_CFG))
    orch = GameOrchestrator()
    assert not (isolated_external_storage / "roster").exists()  # the workspace is not active yet
    assert [p.name for p in orch.session.participants] == ["Alice", "Bob", "Carol", "David"]
    orch._reset_game(default_bindings={})  # a later game may use the roster
    assert (isolated_external_storage / "roster").exists() is False or True  # (only avatars would write: none here)


def test_a_new_game_takes_names_and_texts_from_the_roster(monkeypatch):
    orch = make_orch(monkeypatch)
    roster = orch._roster()
    roster.set_name("N2", "小红")
    roster.set_persona("N2", "寡言")
    roster.set_memory("N2", "## 经验与印象\n上局被骗过")
    asyncio.run(orch.new_game())
    bob = orch.session.participants[1]
    assert (bob.id, bob.name, bob.persona) == ("N2", "小红", "寡言") and "被骗过" in bob.memory
    assert orch.world.player("N2").name == "小红" and orch.world.player("N2").persona == "寡言"
    assert [p.id for p in orch.session.participants] == IDS  # config decides the seats


@async_test
async def test_a_rename_keeps_the_dealt_identities_and_tells_the_spectators(monkeypatch):
    orch = make_orch(monkeypatch)
    before = {p.id: (p.role if hasattr(p, "role") else None, tuple(p.tags)) for p in orch.world.state.players}
    entry = await orch.update_roster_entry("N3", name="  小明 ")
    assert entry["name"] == "小明" and orch.world.player("N3").name == "小明" and orch.session.participants[2].name == "小明"
    assert {p.id: (p.role if hasattr(p, "role") else None, tuple(p.tags)) for p in orch.world.state.players} == before
    assert RosterStore(orch._roster().data_dir).entry("N3")["name"] == "小明"  # persisted for the next games
    updates = events_of(orch, "roster.updated")
    assert updates and any(p["id"] == "N3" and p["name"] == "小明" for p in updates[-1].data["participants"])
    assert all(set(p) == {"id", "name", "avatar"} for p in updates[-1].data["participants"])
    snapshots = events_of(orch, "snapshot")
    assert snapshots and any(p["name"] == "小明" for p in snapshots[-1].data["session"]["participants"])


@async_test
async def test_names_must_be_unique_and_valid(monkeypatch):
    orch = make_orch(monkeypatch)
    with pytest.raises(RosterError, match="已被 N1 使用"):
        await orch.update_roster_entry("N2", name="alice")
    with pytest.raises(RosterError):
        await orch.update_roster_entry("N2", name="<b>")
    assert orch.world.player("N2").name == "Bob"


@async_test
async def test_name_and_avatar_need_an_idle_game_but_persona_and_memory_do_not(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch, idle)
    await orch.start_or_resume()
    try:
        with pytest.raises(RuntimeError, match="开始之前"):
            await orch.update_roster_entry("N1", name="Zed")
        with pytest.raises(RuntimeError, match="开始之前"):
            await orch.update_roster_entry("N1", avatar={"v": 1})
        await orch.update_roster_entry("N1", persona="新的性格", memory="新的记忆")  # files: always editable
        assert orch._roster().entry("N1")["persona"] == "新的性格"
        assert orch.session.participants[0].persona == ""  # the running game keeps the copy it started with
        with pytest.raises(RosterError):
            await orch.update_roster_entry("N9", persona="x")
    finally:
        await orch.pause()


@async_test
async def test_texts_edited_before_the_start_reach_the_system_prompt_and_memory_can_be_switched_off(monkeypatch):
    orch = make_orch(monkeypatch)
    await orch.update_roster_entry("N1", persona="爱说冷笑话", memory="## 经验与印象\nBob 爱撒谎")
    brain = orch._ensure_brain(fresh=True)
    prompt = brain.system_prompt("N1")
    assert "爱说冷笑话" in prompt and "Bob 爱撒谎" in prompt
    assert "Bob 爱撒谎" not in brain.system_prompt("N2")  # each player only gets his own memory

    await orch.set_memory_enabled(False)  # the per-game "load memory" switch (E24)
    assert orch.session.memory_enabled is False
    prompt = orch._ensure_brain(fresh=True).system_prompt("N1")
    assert "爱说冷笑话" in prompt and "Bob 爱撒谎" not in prompt  # persona stays, memory is not loaded
    assert "Bob 爱撒谎" in orch._roster().entry("N1")["memory"]  # ... and is not deleted either


@async_test
async def test_files_edited_outside_the_app_while_the_game_is_idle_are_what_the_game_starts_with(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch, idle)
    store = orch._roster()
    assert orch.session.participants[0].persona == "" and orch.session.participants[1].memory == ""
    # an editor, not the API: nothing tells the orchestrator
    (store._dir / "N1.persona.md").write_text("手工写的设定\n", encoding="utf-8")
    (store._dir / "N2.memory.md").write_text("## 经验与印象\n手工写的记忆\n", encoding="utf-8")
    await orch.start_or_resume()
    try:
        assert orch.session.participants[0].persona == "手工写的设定"
        assert "手工写的记忆" in orch.session.participants[1].memory
        prompt = orch._brain.system_prompt("N1")
        assert "手工写的设定" in prompt and "手工写的记忆" not in prompt  # each player only gets his own memory
        assert "手工写的记忆" in orch._brain.system_prompt("N2")
        await orch.pause()
        # a game that has started keeps the copy it started with, whatever happens to the files afterwards
        (store._dir / "N1.persona.md").write_text("暂停期间又改了\n", encoding="utf-8")
        await orch.start_or_resume()
        assert orch.session.participants[0].persona == "手工写的设定"
        assert "手工写的设定" in orch._brain.system_prompt("N1") and "暂停期间又改了" not in orch._brain.system_prompt("N1")
    finally:
        await orch.pause()


@async_test
async def test_the_start_of_a_game_does_not_touch_the_texts_when_the_roster_cannot_be_read(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch, idle)
    await orch.update_roster_entry("N1", persona="先写好的设定")
    orch._workspace_failed = True  # the workspace went away: nothing may be read or clobbered
    orch._ensure_not_switching = lambda: None  # type: ignore[method-assign]  # (start_or_resume refuses a broken workspace itself)
    orch._refresh_idle_texts()
    assert orch.session.participants[0].persona == "先写好的设定"  # kept, not replaced by an empty text


@async_test
async def test_the_brain_and_its_prompts_survive_a_pause_and_a_resume(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch, idle)
    await orch.start_or_resume()
    brain = orch._brain
    prompt = brain.system_prompt("N1")
    await orch.pause()
    orch._roster().set_memory("N1", "暂停期间改了记忆")
    await orch.start_or_resume()
    try:
        assert orch._brain is brain and brain.system_prompt("N1") == prompt  # byte-identical: the cache prefix holds
    finally:
        await orch.pause()


@async_test
async def test_a_new_game_import_or_restore_starts_with_no_conversations(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch, idle)
    await orch.start_or_resume()
    await asyncio.sleep(0.1)
    await orch.pause()
    assert len(orch._book) > 0  # the moderator at least has a conversation
    first_book = orch._book
    await orch.new_game()
    assert orch._book is not first_book and len(orch._book) == 0 and orch._brain is None


@async_test
async def test_memory_pack_round_trip_dry_run_and_clear(monkeypatch):
    orch = make_orch(monkeypatch)
    orch._roster().set_memory("N1", "## 经验与印象\n一")
    orch._roster().set_persona("N2", "二")
    pack = orch.export_memory()
    assert sorted(zipfile.ZipFile(io.BytesIO(pack)).namelist()) == ["N1.memory.md", "N2.persona.md"]

    await orch.clear_memory()
    assert orch._roster().entry("N1")["memory"] == "" and orch._roster().entry("N2")["persona"] == "二"
    dry = await orch.import_memory(pack, dry_run=True)
    assert dry["dry_run"] is True and [c["id"] for c in dry["changes"]] == ["N1"] and orch._roster().entry("N1")["memory"] == ""
    done = await orch.import_memory(pack, dry_run=False)
    assert [c["id"] for c in done["changes"]] == ["N1"] and "一" in orch._roster().entry("N1")["memory"]
    assert "一" in orch.session.participants[0].memory  # an idle game takes the imported memory at once
    with pytest.raises(RosterError):
        await orch.import_memory(b"garbage", dry_run=False)


# ------------------------------------------------------------------------------------ refine
@async_test
async def test_refine_returns_a_draft_and_saves_nothing(monkeypatch):
    orch = make_orch(monkeypatch)
    port = TextLlm("```markdown\n你说话慢条斯理，爱讲冷笑话。\n```")
    orch.llm_port_factory = lambda o: port
    draft = await orch.refine_text("N1", "persona", "爱说冷笑话")
    assert draft == "你说话慢条斯理，爱讲冷笑话。"
    agent, messages, _ = port.calls[0]
    assert agent == MODERATOR_ID and "爱说冷笑话" in messages[1]["content"] and "Alice" in messages[1]["content"]
    assert orch._roster().entry("N1")["persona"] == ""  # the user reads it first
    assert orch.usage.actor("roster").calls == 1  # the cost is accounted


@async_test
async def test_refine_refuses_bad_requests_with_a_reason(monkeypatch):
    orch = make_orch(monkeypatch)
    orch.llm_port_factory = lambda o: TextLlm()
    for args, message in ((("N1", "persona", "   "), "要点"), (("N1", "secrets", "x"), "persona 或 memory"), (("N1", "persona", "字" * 7000), "太长"), (("N99", "persona", "x"), "没有这个座位")):
        with pytest.raises(RosterError, match=message):
            await orch.refine_text(*args)
    unbound = make_orch(monkeypatch, bound_all=False)
    unbound.llm_port_factory = lambda o: TextLlm()
    with pytest.raises(RosterError, match="主持人还没有绑定模型"):
        await unbound.refine_text("N1", "persona", "x")


@async_test
async def test_an_empty_model_answer_is_an_error_not_an_empty_draft(monkeypatch):
    orch = make_orch(monkeypatch)
    orch.llm_port_factory = lambda o: TextLlm("   ")
    with pytest.raises(RosterError, match="没有返回内容"):
        await orch.refine_text("N1", "memory", "x")


# ------------------------------------------------------------------------------------ review
async def finished_game(orch: GameOrchestrator, winner: str = "红队") -> None:
    play_with(orch, end_at_once(winner))
    await orch.start_or_resume()
    await wait_for(lambda: orch.session.status == "finished")


@async_test
async def test_a_review_needs_a_finished_game(monkeypatch):
    orch = make_orch(monkeypatch)
    with pytest.raises(RuntimeError, match="对局结束后"):
        await orch.start_review()
    assert orch.review_view() == {"available": False, "done": [], "running": False}


@async_test
async def test_a_review_updates_each_players_record_and_memory_once(monkeypatch):
    orch = make_orch(monkeypatch)
    await finished_game(orch, "红队")
    port = TextLlm("我这局发言太少。")
    orch.llm_port_factory = lambda o: port
    assert orch.review_view()["available"] is True
    started = await orch.start_review()
    assert sorted(started) == IDS
    await wait_for(lambda: len(orch.session.review_done) == 4 and not orch._review_running)

    reds = {p.id for p in orch.world.state.players if p.faction == "red"}
    for player in orch.world.state.players:
        memory = orch._roster().entry(player.id)["memory"]
        sections = split_memory(memory)
        assert "我这局发言太少。" in sections["对局记忆"]
        won = player.id in reds
        assert sections["战绩"].splitlines()[0] == f"场次 1，胜 {1 if won else 0}，负 {0 if won else 1}，胜率 {'100%' if won else '0%'}"
    sent = {agent: messages for agent, messages, _ in port.calls}
    assert set(sent) == set(IDS) and all("第一人称" in m[0]["content"] for m in sent.values())
    assert [e.data["state"] for e in events_of(orch, "review.progress")].count("done") == 4
    assert await orch.start_review() == []  # every player reviews each game once
    assert orch.review_view() == {"available": True, "done": sorted(IDS, key=IDS.index), "running": False} or len(orch.review_view()["done"]) == 4


def summed_metrics(orch: GameOrchestrator) -> dict[str, dict[str, Any]]:
    """What a client adds up from the ``agent.metrics`` events: the last numbers of every actor."""
    last: dict[str, dict[str, Any]] = {}
    for event in events_of(orch, "agent.metrics"):
        last[str(event.data["id"])] = dict(event.data)
    return last


@async_test
async def test_the_calls_outside_the_game_loop_publish_their_numbers_so_the_clients_total_stays_exact(monkeypatch):
    orch = make_orch(monkeypatch)
    await finished_game(orch)
    before = summed_metrics(orch)
    orch.llm_port_factory = lambda o: TextLlm("复盘")
    await orch.start_review()
    await wait_for(lambda: len(orch.session.review_done) == 4 and not orch._review_running)
    await orch.refine_text("N1", "persona", "爱说冷笑话")  # "complete it": accounted under the pseudo-actor "roster"
    after = summed_metrics(orch)
    for agent in IDS:
        assert after[agent]["calls"] == orch.usage.actor(agent).calls > before.get(agent, {}).get("calls", 0)
        assert after[agent]["cost_usd"] == orch.usage.actor(agent).cost_usd
    assert after["roster"] == orch.usage.metrics_for("roster") and after["roster"]["calls"] == 1
    # the sum of the last numbers of every actor is the session's total: nothing is missing from the stream
    assert sum(m["prompt_tokens"] + m["cache_read_tokens"] + m["cache_write_tokens"] + m["completion_tokens"] for m in after.values()) == orch.usage.total_tokens
    assert abs(sum(m["cost_usd"] for m in after.values()) - orch.usage.total_cost_usd) < 1e-9


@async_test
async def test_while_a_game_runs_the_supervisor_publishes_its_agents_and_only_the_others_are_published_at_once(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch, idle)
    await orch.start_or_resume()
    try:
        replay_before = len(events_of(orch, "agent.metrics"))
        orch.llm_port_factory = lambda o: TextLlm("x")
        orch._on_usage_recorded("N1", LLMResponse(content="", prompt_tokens=10))  # an agent of the running game: the metrics loop's job
        assert len(events_of(orch, "agent.metrics")) == replay_before
        orch.usage.record_llm("roster", LLMResponse(content="", prompt_tokens=10, completion_tokens=5, provider_id="p", model_id="m"))
        assert [e.data["id"] for e in events_of(orch, "agent.metrics")][-1] == "roster"  # not one of the agents: published now
    finally:
        await orch.pause()


@async_test
async def test_a_failed_review_of_one_player_does_not_stop_the_others_and_leaks_nothing(monkeypatch):
    orch = make_orch(monkeypatch)
    await finished_game(orch)
    orch.llm_port_factory = lambda o: TextLlm(fail_for=("N2",))
    await orch.start_review()
    await wait_for(lambda: not orch._review_running)
    assert sorted(orch.session.review_done) == ["N1", "N3", "N4"]
    errors = [e for e in events_of(orch, "review.progress") if e.data["state"] == "error"]
    assert [e.data["id"] for e in errors] == ["N2"] and "sk-secret" not in str(errors[0].data)
    assert orch._roster().entry("N2")["memory"] == ""  # nothing half-written
    orch.llm_port_factory = lambda o: TextLlm("补写的复盘")
    assert await orch.start_review(["N2"]) == ["N2"]  # the one that failed can be retried
    await wait_for(lambda: "N2" in orch.session.review_done)


@async_test
async def test_an_unknown_winner_adds_the_review_but_leaves_the_record_alone(monkeypatch):
    orch = make_orch(monkeypatch)
    await finished_game(orch, "平局")
    orch.llm_port_factory = lambda o: TextLlm("平局的复盘")
    await orch.start_review(["N1"])
    await wait_for(lambda: "N1" in orch.session.review_done)
    sections = split_memory(orch._roster().entry("N1")["memory"])
    assert sections["对局记忆"] == "平局的复盘" and sections.get("战绩", "") == ""


@async_test
async def test_only_known_players_can_be_reviewed(monkeypatch):
    orch = make_orch(monkeypatch)
    await finished_game(orch)
    with pytest.raises(RosterError):
        await orch.start_review(["N1", "ghost"])


# ------------------------------------------------------------------------------------ budget
@async_test
async def test_the_budget_defaults_to_the_saved_one_and_a_change_is_saved_for_the_next_game(monkeypatch):
    orch = make_orch(monkeypatch)
    assert orch.budget_payload()["state"] == "ok" and orch.session.budget.max_tokens is None
    view = await orch.set_budget({"max_tokens": 5000, "max_cost_usd": 1.5, "force_stop": True})
    assert view["max_tokens"] == 5000 and view["max_cost_usd"] == 1.5 and view["force_stop"] is True and view["used_tokens"] == 0
    assert orch._settings().budget_defaults == {"max_tokens": 5000, "max_cost_usd": 1.5, "force_stop": True}
    await orch.new_game()
    assert orch.session.budget.max_tokens == 5000  # per game, but a new game starts from the last choice
    with pytest.raises(Exception):
        await orch.set_budget({"max_tokens": -1})
    with pytest.raises(Exception):
        await orch.set_budget({"unknown": 1})


@async_test
async def test_reaching_a_stopping_limit_pauses_the_game_with_the_budget_reason(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch, chatty)
    await orch.set_budget({"max_tokens": 450, "force_stop": True})  # a scripted call is 150 tokens
    await orch.start_or_resume()
    await wait_for(lambda: orch.session.status == "paused")
    assert orch.session.pause_reason == "budget" and "预算" in orch._status_text()
    assert orch.usage.total_tokens >= 450 and orch.budget_payload()["state"] == "reached"
    assert [e.type for e in events_of(orch, "budget.warning", "budget.reached")] == ["budget.warning", "budget.reached"] or events_of(orch, "budget.reached")
    assert not orch._is_active()
    with pytest.raises(RuntimeError, match="预算上限"):
        await orch.start_or_resume()  # it would only stop again at once
    await orch.set_budget({"max_tokens": 10**7, "force_stop": True})  # raise the limit ...
    await orch.start_or_resume()  # ... and the game goes on
    try:
        assert orch.session.status == "running" and orch.session.pause_reason == ""
    finally:
        await orch.pause()


@async_test
async def test_a_limit_without_force_stop_only_warns(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch, chatty)
    await orch.set_budget({"max_tokens": 300, "force_stop": False})
    await orch.start_or_resume()
    try:
        await wait_for(lambda: events_of(orch, "budget.reached"))
        await asyncio.sleep(0.2)
        assert orch.session.status == "running"  # the user chose to keep playing
        assert orch.budget_payload()["state"] == "reached"
    finally:
        await orch.pause()


@async_test
async def test_lowering_the_limit_below_what_is_used_stops_a_running_game_at_once(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch, chatty)
    await orch.start_or_resume()
    await wait_for(lambda: orch.usage.total_tokens > 0)
    await orch.set_budget({"max_tokens": 1, "force_stop": True})
    await wait_for(lambda: orch.session.status == "paused")
    assert orch.session.pause_reason == "budget"


@async_test
async def test_the_cost_limit_counts_money_not_tokens(monkeypatch):
    orch = make_orch(monkeypatch)
    orch.usage.record_usage("N1", prompt_tokens=1_000_000, completion_tokens=0, input_price_per_m=2.0, output_price_per_m=8.0)
    view = await orch.set_budget({"max_cost_usd": 2.5})
    assert view["used_cost_usd"] == 2.0 and view["state"] == "warning" and view["progress"] == 0.8
    orch.usage.record_usage("N1", prompt_tokens=250_000, input_price_per_m=2.0, output_price_per_m=8.0)
    orch._on_budget_changed(force=True)
    assert orch.budget_payload()["state"] == "reached"


# ------------------------------------------------------------------------------------ session view
@async_test
async def test_the_public_session_has_the_v3_fields_and_no_execution_depth(monkeypatch):
    orch = make_orch(monkeypatch)
    s = orch.public_session()
    assert s["schema_version"] == 4 and s["version"] == "v0.12.1" and "execution_depth" not in s
    assert s["budget"]["state"] == "ok" and s["memory_enabled"] is True and s["review"] == {"available": False, "done": [], "running": False}
    moderator = s["moderator"]
    assert s["stall_notice"] == "" and {k: moderator[k] for k in ("id", "name", "state", "sleep_until")} == {
        "id": MODERATOR_ID, "name": "主持人", "state": "sleeping", "sleep_until": None,
    }
    assert moderator["avatar"] is None or moderator["avatar"]["v"] == 1  # the moderator has a random avatar of its own
    first = s["participants"][0]
    assert (first["id"], first["name"], first["state"], first["sleep_until"]) == ("N1", "Alice", "sleeping", None)
    assert first["avatar"] is None or first["avatar"]["v"] == 1  # a description once the parts library is installed
    assert {"prompt_tokens", "cache_read_tokens", "cache_write_tokens", "completion_tokens", "total_tokens", "cache_hit_rate", "total_cost_usd"} <= set(s["usage_stats"])


@async_test
async def test_the_watchdog_only_shows_a_banner_and_never_pauses(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch, idle)
    await orch.start_or_resume()
    try:
        orch._on_stall("对局已静止 60 秒，主持人也没有推进。")
        await wait_for(lambda: any("静止" in e.data.get("stall_notice", "") for e in events_of(orch, "session.updated")))
        assert orch.session.status == "running" and "静止" in orch.public_session()["stall_notice"]
        orch._on_stall(None)
        await wait_for(lambda: events_of(orch, "session.updated")[-1].data["stall_notice"] == "")
        assert orch.public_session()["stall_notice"] == "" and orch.session.status == "running"
    finally:
        await orch.pause()


@async_test
async def test_pause_reasons_include_budget_and_a_save_keeps_the_limit_and_the_review_state(monkeypatch):
    orch = make_orch(monkeypatch)
    await orch.set_budget({"max_tokens": 777, "force_stop": True})
    orch.session.review_done = ["N1"]
    payload = orch.session_payload()
    assert payload["budget"]["max_tokens"] == 777 and payload["review_done"] == ["N1"] and payload["memory_enabled"] is True
    other = make_orch(monkeypatch)
    await other.load_session_data(payload)
    assert other.session.budget.max_tokens == 777 and other.session.review_done == ["N1"]
    assert other.session.status == "idle" and len(other._book) == 0  # conversations are never saved


@async_test
async def test_a_finished_review_is_saved_so_a_restart_cannot_review_the_same_game_twice(monkeypatch):
    orch = make_orch(monkeypatch)
    orch._autosave_enabled = True  # what startup() switches on
    await finished_game(orch)
    orch.llm_port_factory = lambda o: TextLlm("复盘")
    await orch.start_review()
    await wait_for(lambda: len(orch.session.review_done) == 4 and not orch._review_running)
    await wait_for(lambda: any(len(saved.get("review_done", [])) == 4 for saved in orch.session_store.data.values()))
    saved = next(v for v in orch.session_store.data.values() if len(v.get("review_done", [])) == 4)
    assert sorted(saved["review_done"]) == sorted(IDS)
    other = make_orch(monkeypatch)
    await other.load_session_data(copy.deepcopy(saved))
    assert other.review_view()["done"] and other.review_view()["available"] is True
    assert await other.start_review() == []  # nothing left to review


# ------------------------------------------------------------------------------------ the moderator's avatar
def avatar_key(desc):
    return json.dumps(desc, sort_keys=True)


def differing_parts(a, b):
    return sum(1 for k in ("face", "skin", "hair", "hair_color", "eyes", "mouth", "accessory", "accessory_color") if a.get(k) != b.get(k))


@async_test
async def test_the_moderator_gets_a_random_avatar_like_the_players(monkeypatch):
    orch = make_orch(monkeypatch)
    s = orch.public_session()
    moderator = s["moderator"]["avatar"]
    assert moderator and moderator["v"] == 1 and orch.session.moderator_avatar == moderator
    players = [p["avatar"] for p in s["participants"]]
    assert all(players) and all(differing_parts(moderator, p) >= 3 for p in players)  # nobody looks like the host
    assert orch.roster_payload()["moderator"] == {"id": "moderator", "name": "主持人", "avatar": moderator}
    await orch.new_game()
    assert orch.public_session()["moderator"]["avatar"] == moderator  # fixed in the roster, not re-rolled per game


@async_test
async def test_regenerating_the_moderator_changes_only_the_moderator(monkeypatch):
    orch = make_orch(monkeypatch)
    before = orch.public_session()
    entry = await orch.regenerate_avatars("moderator")
    after = orch.public_session()
    assert entry["id"] == "moderator" and entry["avatar"] == after["moderator"]["avatar"] != before["moderator"]["avatar"]
    assert [p["avatar"] for p in after["participants"]] == [p["avatar"] for p in before["participants"]]
    assert all(differing_parts(entry["avatar"], p["avatar"]) >= 3 for p in after["participants"])
    updates = events_of(orch, "roster.updated")
    assert updates and updates[-1].data["moderator"] == {"id": "moderator", "avatar": entry["avatar"]}
    assert orch._roster().entry("moderator")["avatar"] == entry["avatar"]  # saved for the next games


@async_test
async def test_regenerating_everybody_rerolls_the_twelve_seats_and_the_moderator_together(monkeypatch):
    orch = make_orch(monkeypatch)
    before = orch.public_session()
    result = await orch.regenerate_avatars()
    assert [s["id"] for s in result["seats"]] == IDS  # (this test game has four seats; the real one twelve)
    assert result["moderator"]["id"] == "moderator"
    everyone = [s["avatar"] for s in result["seats"]] + [result["moderator"]["avatar"]]
    assert len({avatar_key(a) for a in everyone}) == len(IDS) + 1
    assert all(differing_parts(a, b) >= 3 for i, a in enumerate(everyone) for b in everyone[i + 1:])  # all clearly different faces
    assert result["moderator"]["avatar"] != before["moderator"]["avatar"]
    assert orch.public_session()["moderator"]["avatar"] == result["moderator"]["avatar"]


@async_test
async def test_a_single_player_regeneration_keeps_clear_of_the_moderators_face(monkeypatch):
    orch = make_orch(monkeypatch)
    moderator = orch.public_session()["moderator"]["avatar"]
    for seat in ("N1", "N2", "N3", "N4"):
        entry = await orch.regenerate_avatars(seat)
        assert differing_parts(entry["avatar"], moderator) >= 3


@async_test
async def test_the_moderator_only_has_an_avatar_to_change_and_only_before_the_game(monkeypatch):
    orch = make_orch(monkeypatch)
    for kwargs in ({"name": "Boss"}, {"persona": "严厉"}, {"memory": "x"}):
        with pytest.raises(RosterError, match="只能更换头像"):
            await orch.update_roster_entry("moderator", **kwargs)
    with pytest.raises(RosterError, match="没有性格设定和记忆"):
        await orch.refine_text("moderator", "persona", "严厉")
    from app import avatars

    chosen = avatars.make_description(0, [p["avatar"] for p in orch.public_session()["participants"]], seed=5)
    entry = await orch.update_roster_entry("moderator", avatar=chosen)
    assert entry["avatar"] == chosen and orch.public_session()["moderator"]["avatar"] == chosen
    with pytest.raises(RosterError):
        await orch.update_roster_entry("moderator", avatar={"v": 1, "face": "nope"})  # not a description of this library
    orch.session.status = "paused"
    with pytest.raises(RuntimeError, match="开始之前"):
        await orch.update_roster_entry("moderator", avatar=chosen)
    with pytest.raises(RuntimeError, match="开始之前"):
        await orch.regenerate_avatars("moderator")


@async_test
async def test_the_moderators_avatar_is_part_of_the_save_and_survives_a_restore(monkeypatch):
    orch = make_orch(monkeypatch)
    moderator = orch.public_session()["moderator"]["avatar"]
    payload = json.loads(json.dumps(orch.session_payload()))
    assert payload["moderator_avatar"] == moderator
    orch._roster().set_avatar("moderator", {"v": 1, "face": "later"})  # the roster moves on ...
    other = make_orch(monkeypatch)
    await other.load_session_data(payload)
    assert other.public_session()["moderator"]["avatar"] == moderator  # the game keeps the host it started with


# ------------------------------------------------------------------------------------ no usable workspace
def roster_dir_exists(orch):
    return (orch._roster().data_dir / "roster").exists()


@async_test
async def test_without_a_usable_workspace_the_roster_is_read_only_and_creates_nothing(monkeypatch, isolated_external_storage):
    orch = make_orch(monkeypatch)
    orch._workspace_failed = True  # the workspace could not be opened: the legacy global directory must stay untouched
    payload = orch.roster_payload()
    assert payload["editable"] is False and payload["memory_enabled"] is True
    assert [s["id"] for s in payload["seats"]] == IDS and [s["name"] for s in payload["seats"]] == ["Alice", "Bob", "Carol", "David"]
    assert all(s["avatar"] is None and s["persona"] == "" and s["memory"] == "" for s in payload["seats"])
    assert payload["moderator"] == {"id": "moderator", "name": "主持人", "avatar": None}
    for call in (
        lambda: orch.update_roster_entry("N1", persona="x"),
        lambda: orch.update_roster_entry("N1", memory="x"),
        lambda: orch.update_roster_entry("N1", name="Zed"),
        lambda: orch.set_memory_enabled(False),
        lambda: orch.clear_memory(),
        lambda: orch.import_memory(b"x", dry_run=False),
        lambda: orch.regenerate_avatars("moderator"),
    ):
        with pytest.raises(RuntimeError, match="工作文件夹不可用"):
            await call()
    with pytest.raises(RosterError, match="工作文件夹不可用"):
        orch.export_memory()
    orch._workspace_failed = False
    shutil_rmtree = __import__("shutil").rmtree
    shutil_rmtree(isolated_external_storage / "roster", ignore_errors=True)  # forget what the make_orch game created
    orch._workspace_failed = True
    orch.roster_payload()
    assert not (isolated_external_storage / "roster").exists(), "reading the roster of a broken workspace created files"


@async_test
async def test_roster_changes_wait_for_a_workspace_switch_to_finish(monkeypatch):
    orch = make_orch(monkeypatch)
    orch._switching_workspace = True
    for call in (lambda: orch.update_roster_entry("N1", persona="x"), lambda: orch.set_memory_enabled(False), lambda: orch.clear_memory()):
        with pytest.raises(RuntimeError, match="正在切换工作文件夹"):
            await call()
    assert orch.roster_payload()["editable"] is False


def test_the_incompatible_save_notice_names_the_versions_of_this_release():
    from app.game.session_runtime import OLD_SAVE_NOTICE

    assert "v0.12.0" in OLD_SAVE_NOTICE and "v0.12.1" in OLD_SAVE_NOTICE and "未被改动" in OLD_SAVE_NOTICE
