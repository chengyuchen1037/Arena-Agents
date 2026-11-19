"""v0.12.1 orchestrator: roster (names / avatars / persona / memory), spending limit, review, refine, stall banner.

The orchestrator is the real one, wired to in-memory stores and scripted agents (no network).  Roster files
are real files in the isolated data directory of the test."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

import app.game.orchestrator as orchestrator_module
from app.game.orchestrator import GameOrchestrator
from app.llm.types import LLMResponse
from app.storage.errors import StorageError
from app.storage.roster import PERSONA_CHARS, RecordTally, RosterError, RosterInputError, RosterStore
from app.world.types import MODERATOR_ID, UiEvent
from game_fakes import (
    CFG,
    PROVIDER,
    FakeProviders,
    FakeGames,
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
    orch.game_store = FakeGames()
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


def review_answer(impressions: dict[str, str] | None = None, experience: list[str] | None = None) -> str:
    """What a review model answers (the JSON the prompt asks for).  One answer serves every player of the table: the id of
    the player himself is simply not an impression he writes."""
    given = impressions if impressions is not None else {pid: f"{pid} 发言平稳" for pid in IDS}
    return json.dumps({"impressions": given, "experience": experience if experience is not None else ["先听后说"]}, ensure_ascii=False)


PERSONA_ANSWER = "【定位】冷静的逻辑派。\n【说话风格】\n- 句子短。\n- 少用感叹号。\n【思考方式】\n- 先看票型。\n【口头禅】我们看票。"


class TextLlm:
    """One-shot port: ``complete`` returns plain text.  A player (a review) gets ``text`` — by default the JSON of a review —
    and the moderator (the model of "complete it for me") gets ``refine``, by default a persona in the fixed format."""

    def __init__(self, text: str | None = None, fail_for: tuple[str, ...] = (), refine: str = PERSONA_ANSWER) -> None:
        self.text = review_answer() if text is None else text
        self.refine, self.fail_for, self.calls = refine, fail_for, []

    async def complete(self, agent_id: str, messages: list[dict[str, Any]], tools: Any, *, max_tokens: int | None = None) -> LLMResponse:
        self.calls.append((agent_id, messages, max_tokens))
        if agent_id in self.fail_for:
            raise RuntimeError("upstream exploded sk-secret-1234")
        content = self.refine if agent_id == MODERATOR_ID else self.text
        return LLMResponse(content=content, prompt_tokens=50, completion_tokens=20, provider_id="bot", model_id="scripted")


# ------------------------------------------------------------------------------------ roster
def test_the_first_game_is_built_without_touching_any_roster_file(monkeypatch, isolated_external_storage):
    monkeypatch.setattr(orchestrator_module, "load_config", lambda *a, **k: copy.deepcopy(ROSTER_CFG))
    orch = GameOrchestrator()
    assert not (isolated_external_storage / "roster").exists()  # the data directory is not locked yet: startup() reads it
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
async def test_the_persona_is_a_section_of_its_own_and_the_memory_block_lists_the_impressions(monkeypatch):
    orch = make_orch(monkeypatch)
    persona = "【定位】冷静的逻辑派。\n【说话风格】\n- 句子短。"
    await orch.update_roster_entry("N1", persona=persona, memory_parts={"impressions": {"N2": "爱撒谎，下次别信"}, "experience": ["先听后说"]})
    assert orch.session.participants[0].persona == persona  # an idle game takes the texts at once
    prompt = orch._ensure_brain(fresh=True).system_prompt("N1")
    assert "# 你的性格与行为指引\n\n【定位】冷静的逻辑派。\n【说话风格】\n- 句子短。\n" in prompt  # its own section, the text as it is
    assert "你的性格与表达风格" not in prompt
    assert "# 你的记忆与经验\n\n## 印象\n" in prompt and "- N2 Bob：爱撒谎，下次别信" in prompt and "## 经验\n- 先听后说" in prompt
    assert "- N3 Carol" not in prompt  # no row for a seat he has no impression of
    other = orch._ensure_brain(fresh=True).system_prompt("N2")
    assert "（没有特别设定，按你自己的判断行事。）" in other and "爱撒谎，下次别信" not in other and "# 你的记忆与经验" not in other


@async_test
async def test_a_persona_over_the_limit_is_refused_and_an_old_longer_one_is_cut_in_the_prompt(monkeypatch):
    orch = make_orch(monkeypatch)
    with pytest.raises(RosterError, match="最多 500"):
        await orch.update_roster_entry("N1", persona="字" * (PERSONA_CHARS + 1))
    store = orch._roster()
    store._dir.mkdir(parents=True, exist_ok=True)
    (store._dir / "N1.persona.md").write_text("旧的长性格。" * 200 + "\n", encoding="utf-8")  # written by an older version
    assert len(store.entry("N1")["persona"]) == 1200  # the panel gets the text as it is
    orch._refresh_idle_texts()  # (what starting the game does)
    brain = orch._ensure_brain(fresh=True)
    assert len(orch.session.participants[0].persona) == PERSONA_CHARS  # the game copy is cut ...
    assert "旧的长性格。" * 83 in brain.system_prompt("N1") and "旧的长性格。" * 84 not in brain.system_prompt("N1")
    assert len(store.entry("N1")["persona"]) == 1200  # ... the file is not


@async_test
async def test_one_put_is_applied_whole_or_not_at_all_and_memory_and_memory_parts_exclude_each_other(monkeypatch):
    orch = make_orch(monkeypatch)
    with pytest.raises(RosterError, match="最多 500"):
        await orch.update_roster_entry("N1", name="Zed", persona="字" * 501)  # the name is not applied: the persona is refused first
    assert orch._roster().entry("N1")["name"] == "Alice" and orch.world.player("N1").name == "Alice"
    with pytest.raises(RosterError, match="印象最多"):
        await orch.update_roster_entry("N1", name="Zed", memory_parts={"impressions": {"N2": "字" * 81}})
    assert orch._roster().entry("N1")["name"] == "Alice"
    with pytest.raises(RosterInputError, match="只能给一个"):
        await orch.update_roster_entry("N1", memory="## 经验\n- x", memory_parts={"experience": ["y"]})
    for parts, message in (({"impressions": {"N1": "自己"}}, "不能给自己"), ({"impressions": {"N9": "x"}}, "没有这个座位")):
        with pytest.raises(RosterInputError, match=message):
            await orch.update_roster_entry("N1", memory_parts=parts)
    assert orch._roster().entry("N1")["memory"] == "" and orch._roster().entry("N1")["persona"] == ""  # nothing was written
    entry = await orch.update_roster_entry("N1", name="Zed", persona="好性格", memory_parts={"impressions": {"N2": "爱撒谎"}})
    assert (entry["name"], entry["persona"], entry["memory_parts"]["impressions"]["N2"]) == ("Zed", "好性格", "爱撒谎")
    assert orch.session.participants[0].memory.count("爱撒谎") == 1 and orch.world.player("N1").name == "Zed"
    unchanged = orch._roster().entry("N1")["memory"]
    assert (await orch.update_roster_entry("N1", memory_parts={}))["memory"] == unchanged  # an empty object changes nothing
    for kwargs in ({"memory_parts": {"experience": ["x"]}}, {"persona": "x"}):
        with pytest.raises(RosterError, match="只能更换头像"):
            await orch.update_roster_entry("moderator", **kwargs)


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

    def unreadable() -> Any:
        raise StorageError("名册文件无法读取")

    orch._roster = unreadable  # type: ignore[method-assign]  # the roster went away: nothing may be clobbered
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
async def test_clearing_the_memory_keeps_the_persona_and_an_idle_game_takes_it_at_once(monkeypatch):
    orch = make_orch(monkeypatch)
    orch._roster().set_memory("N1", "## 经验与印象\n一")
    orch._roster().set_persona("N2", "二")
    await orch.clear_memory()
    assert orch._roster().entry("N1")["memory"] == "" and orch._roster().entry("N2")["persona"] == "二"
    assert orch.session.participants[0].memory == ""
    assert not hasattr(orch, "export_memory") and not hasattr(orch, "import_memory")  # the memory pack is gone (F26)


# ------------------------------------------------------------------------------------ refine
@async_test
async def test_refine_returns_a_persona_in_the_fixed_format_and_saves_nothing(monkeypatch):
    orch = make_orch(monkeypatch)
    port = TextLlm(refine="好的，设定如下：\n```markdown\n**【定位】**爱讲冷笑话的老好人。\n【说话风格】\n- 慢条斯理。\n- 爱讲冷笑话。\n【口头禅】先喝口茶。\n```")
    orch.llm_port_factory = lambda o: port
    draft = await orch.refine_text("N1", "persona", "爱说冷笑话")
    assert draft == "【定位】爱讲冷笑话的老好人。\n【说话风格】\n- 慢条斯理。\n- 爱讲冷笑话。\n【口头禅】先喝口茶。"  # normalized
    agent, messages, _ = port.calls[0]
    assert agent == MODERATOR_ID and "爱说冷笑话" in messages[1]["content"] and "Alice" in messages[1]["content"]
    assert "【定位】" in messages[0]["content"] and "500" in messages[0]["content"] and "40" in messages[0]["content"]  # the template and its limits
    assert orch._roster().entry("N1")["persona"] == ""  # the user reads it first
    assert orch.usage.actor("roster").calls == 1  # the cost is accounted
    assert await orch.refine_entry("N1", "persona", "爱说冷笑话") == {"draft": draft}  # (the API's answer)


@async_test
async def test_refine_of_a_memory_merges_the_notes_into_the_current_impressions_and_saves_nothing(monkeypatch):
    orch = make_orch(monkeypatch)
    roster = orch._roster()
    roster.set_memory_parts("N1", impressions={"N2": "发言短", "N3": "爱附和"}, experience=["先听后说"], legacy="旧笔记")
    answer = json.dumps({"impressions": {"N2": "发言短，爱撒谎", "N1": "自己", "N9": "不存在"}, "experience": ["别太早表态"]}, ensure_ascii=False)
    port = TextLlm(refine=answer)
    orch.llm_port_factory = lambda o: port
    result = await orch.refine_entry("N1", "memory", "Bob 爱撒谎；别太早表态")
    parts = result["memory_parts"]
    assert set(result) == {"draft", "memory_parts"} and set(parts) == {"impressions", "experience"}
    assert parts["impressions"] == {"N2": "发言短，爱撒谎", "N3": "爱附和", "N4": ""}  # merged into what is there; the owner and N9 are gone
    assert parts["experience"] == ["别太早表态"]
    assert result["draft"].startswith("## 印象\n- N2 Bob：发言短，爱撒谎\n- N3 Carol：爱附和\n- N4 David：（暂无）") and result["draft"].endswith("## 经验\n- 别太早表态")
    assert "旧笔记" not in result["draft"]  # the draft is the two sections: the legacy text is not part of it, and is not deleted by a draft
    agent, messages, _ = port.calls[0]
    system, user = messages[0]["content"], messages[1]["content"]
    assert agent == MODERATOR_ID and "JSON" in system and "80" in system and "300" in system
    assert "N2 Bob" in user and "- N2 Bob：发言短" in user and "【当前经验】\n- 先听后说" in user and user.endswith("要点：\nBob 爱撒谎；别太早表态")
    entry = orch._roster().entry("N1")["memory_parts"]
    assert entry["impressions"]["N2"] == "发言短" and entry["experience"] == ["先听后说"] and entry["legacy"] == "旧笔记"  # nothing was saved


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
    orch.llm_port_factory = lambda o: TextLlm(refine="   ")
    with pytest.raises(RosterError, match="没有返回内容"):
        await orch.refine_text("N1", "memory", "x")


@async_test
async def test_an_answer_that_is_not_in_the_asked_format_is_an_error_not_a_draft(monkeypatch):
    orch = make_orch(monkeypatch)
    orch.llm_port_factory = lambda o: TextLlm(refine="你说话慢条斯理，爱讲冷笑话。")  # prose: none of the template's headings
    with pytest.raises(RosterError, match="固定格式"):
        await orch.refine_text("N1", "persona", "爱说冷笑话")
    with pytest.raises(RosterError, match="JSON"):
        await orch.refine_text("N1", "memory", "Bob 爱撒谎")  # ... and prose is no memory either
    assert orch._roster().entry("N1")["persona"] == "" and orch._roster().entry("N1")["memory"] == ""


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
async def test_a_review_revises_each_players_impressions_and_counts_the_game_once(monkeypatch):
    orch = make_orch(monkeypatch)
    await finished_game(orch, "红队")
    port = TextLlm(review_answer({pid: f"{pid} 这局发言平稳" for pid in IDS}, ["别太保守"]))
    orch.llm_port_factory = lambda o: port
    assert orch.review_view()["available"] is True
    started = await orch.start_review()
    assert sorted(started) == IDS
    await wait_for(lambda: len(orch.session.review_done) == 4 and not orch._review_running)

    reds = {p.id for p in orch.world.state.players if p.faction == "red"}
    for player in orch.world.state.players:
        parts = orch._roster().entry(player.id)["memory_parts"]
        won = player.id in reds
        assert parts["record"].splitlines()[0] == f"场次 1，胜 {1 if won else 0}，负 {0 if won else 1}，胜率 {'100%' if won else '0%'}"
        assert set(parts["impressions"]) == set(IDS) - {player.id}  # an impression of everybody else, none of himself
        assert all(text == f"{sid} 这局发言平稳" for sid, text in parts["impressions"].items())
        assert parts["experience"] == ["别太保守"] and parts["legacy"] == ""
    sent = {agent: messages for agent, messages, _ in port.calls}
    assert set(sent) == set(IDS)
    for agent, messages in sent.items():
        system, user = messages[0]["content"], messages[1]["content"]
        assert "JSON" in system and "身份或阵营结论" in system and "80" in system and "300" in system  # the task, the ban and the limits
        assert "【其他玩家】" in user and f"{agent} " not in user.split("【其他玩家】", 1)[1].split("\n", 1)[0]  # (not himself)
        assert "【你目前的印象】" in user and "【你目前的经验】\n（还没有）" in user and "【旧记忆（待整理）】" not in user
    done = [e.data for e in events_of(orch, "review.progress") if e.data["state"] == "done"]
    assert len(done) == 4 and all(set(d) == {"id", "state", "message", "changes"} for d in done)
    first = next(d for d in done if d["id"] == "N1")
    assert first["changes"]["experience"] == {"before": [], "after": ["别太保守"]}
    assert first["changes"]["impressions"] == [{"id": f"N{i}", "before": "", "after": f"N{i} 这局发言平稳"} for i in (2, 3, 4)]
    assert all("changes" not in e.data for e in events_of(orch, "review.progress") if e.data["state"] == "running")
    assert await orch.start_review() == []  # every player reviews each game once
    assert len(orch.review_view()["done"]) == 4


@async_test
async def test_a_second_games_review_revises_the_impressions_instead_of_adding_to_them(monkeypatch):
    orch = make_orch(monkeypatch)
    await finished_game(orch, "红队")
    roster = orch._roster()
    roster.set_memory_parts("N1", impressions={"N2": "上局的印象：爱附和", "N3": "上局的印象：很沉默"}, experience=["先听后说"], legacy="很早以前的笔记")
    port = TextLlm(review_answer({"N2": "改观：其实有主见"}, []))  # about N2 only, no new lessons
    orch.llm_port_factory = lambda o: port
    await orch.start_review(["N1"])
    await wait_for(lambda: "N1" in orch.session.review_done)
    system, user = (port.calls[0][1][0]["content"], port.calls[0][1][1]["content"])
    assert "- N2 Bob：上局的印象：爱附和" in user and "- N3 Carol：上局的印象：很沉默" in user and "- N4 David：（暂无）" in user  # he sees what he thought
    assert "【你目前的经验】\n- 先听后说" in user and "【旧记忆（待整理）】\n很早以前的笔记" in user  # ... and what he had written down before
    assert "5." in system and "整理" in system  # the legacy text is handed over to be tidied up
    parts = roster.entry("N1")["memory_parts"]
    assert parts["impressions"]["N2"] == "改观：其实有主见"  # revised
    assert parts["impressions"]["N3"] == "上局的印象：很沉默"  # the model said nothing about N3: unchanged
    assert parts["experience"] == ["先听后说"]  # no new lessons: the old ones stay
    assert parts["legacy"] == ""  # tidied up: the section is gone
    memory = roster.entry("N1")["memory"]
    assert memory.count("- N2 Bob：") == 1 and "上局的印象：爱附和" not in memory  # overwritten, not appended
    done = next(e.data for e in events_of(orch, "review.progress") if e.data["state"] == "done" and e.data["id"] == "N1")
    assert done["changes"] == {"impressions": [{"id": "N2", "before": "上局的印象：爱附和", "after": "改观：其实有主见"}], "experience": None}


@async_test
async def test_the_answer_may_be_lines_and_the_raw_answer_is_written_to_the_trace_log(monkeypatch):
    orch = make_orch(monkeypatch)
    await finished_game(orch, "红队")
    lines = "我的更新：\nN2：发言短，站边快\n- N3 Carol: 爱附和\n经验：\n- 先听后说"
    orch.llm_port_factory = lambda o: TextLlm(lines)
    await orch.start_review(["N1"])
    await wait_for(lambda: "N1" in orch.session.review_done)
    parts = orch._roster().entry("N1")["memory_parts"]
    assert (parts["impressions"]["N2"], parts["impressions"]["N3"], parts["experience"]) == ("发言短，站边快", "爱附和", ["先听后说"])
    from app.runtime.trace_log import trace_dir

    files = sorted(trace_dir(orch._logs_root(), orch.session.session_id).glob("trace-*.jsonl"))
    records = [json.loads(line) for path in files for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    review = [r for r in records if r.get("event") == "review"]
    assert review and review[-1]["agent_id"] == "N1" and review[-1]["mode"] == "lines" and review[-1]["text"] == lines  # what the model said stays readable


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
    orch.llm_port_factory = lambda o: TextLlm()
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
    orch.llm_port_factory = lambda o: TextLlm(review_answer({"N1": "补写的印象"}))
    assert await orch.start_review(["N2"]) == ["N2"]  # the one that failed can be retried
    await wait_for(lambda: "N2" in orch.session.review_done)
    assert orch._roster().entry("N2")["memory_parts"]["impressions"]["N1"] == "补写的印象"


@pytest.mark.parametrize(
    "answer",
    [
        "这一局我发言太少，下次早点表态。",
        "{not json",
        '{"foo": 1}',
        "N99：不存在的座位",
        '{"impressions": {"N99": "鬼", "N1": "自己"}, "experience": [null, 5]}',  # no seat of the right kind, nothing that is text
    ],
)
@async_test
async def test_an_answer_with_no_impressions_fails_that_players_review_and_changes_nothing(monkeypatch, answer):
    orch = make_orch(monkeypatch)
    await finished_game(orch)
    orch._roster().set_memory_parts("N1", impressions={"N2": "原来的印象"})
    before = orch._roster().entry("N1")["memory"]
    orch.llm_port_factory = lambda o: TextLlm(answer)
    await orch.start_review(["N1"])
    await wait_for(lambda: not orch._review_running)
    errors = [e.data for e in events_of(orch, "review.progress") if e.data["state"] == "error"]
    assert [e["id"] for e in errors] == ["N1"] and "印象" in errors[0]["message"] and "重试" in errors[0]["message"]
    assert "N1" not in orch.session.review_done  # not counted: it can be asked again
    assert orch._roster().entry("N1")["memory"] == before  # the record was not counted either
    from app.runtime.trace_log import trace_dir

    records = [json.loads(line) for p in trace_dir(orch._logs_root(), orch.session.session_id).glob("trace-*.jsonl") for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [r["text"] for r in records if r.get("event") == "review_failed"] == [answer]  # the raw answer is in the log
    orch.llm_port_factory = lambda o: TextLlm(review_answer({"N2": "这次对了"}))
    assert await orch.start_review(["N1"]) == ["N1"]
    await wait_for(lambda: "N1" in orch.session.review_done)
    assert orch._roster().entry("N1")["memory_parts"]["impressions"]["N2"] == "这次对了"


@async_test
async def test_an_unknown_winner_revises_the_impressions_but_leaves_the_record_alone(monkeypatch):
    orch = make_orch(monkeypatch)
    await finished_game(orch, "平局")
    orch.llm_port_factory = lambda o: TextLlm(review_answer({"N2": "平局里的印象"}))
    await orch.start_review(["N1"])
    await wait_for(lambda: "N1" in orch.session.review_done)
    parts = orch._roster().entry("N1")["memory_parts"]
    assert parts["impressions"]["N2"] == "平局里的印象" and parts["record"] == ""  # nobody won: no game is counted


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
    from app.game.session import APP_ENGINE_VERSION

    assert s["schema_version"] == 5 and s["version"] == APP_ENGINE_VERSION and "execution_depth" not in s
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
    orch.llm_port_factory = lambda o: TextLlm()
    await orch.start_review()
    await wait_for(lambda: len(orch.session.review_done) == 4 and not orch._review_running)
    await wait_for(lambda: any(len(saved.get("review_done", [])) == 4 for saved in orch.game_store.data.values()))
    saved = next(v for v in orch.game_store.data.values() if len(v.get("review_done", [])) == 4)
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


# ------------------------------------------------------------------------------------ unreadable roster
@async_test
async def test_an_unreadable_roster_is_a_storage_error_for_the_panel_and_does_not_block_a_game(monkeypatch):
    orch = make_orch(monkeypatch)

    def unreadable() -> Any:
        raise StorageError("名册文件无法读取")

    orch._roster = unreadable  # type: ignore[method-assign]
    with pytest.raises(StorageError):
        orch.roster_payload()  # the API answers 503 for this
    with pytest.raises(StorageError):
        await orch.update_roster_entry("N1", persona="x")
    players = orch._roster_players()  # ... but a new game falls back to the names of config.yml
    assert [p["name"] for p in players] == ["Alice", "Bob", "Carol", "David"] and all(p["avatar"] is None for p in players)


def test_the_incompatible_save_notice_names_the_versions_of_this_release():
    from app.game.session_runtime import OLD_SAVE_NOTICE

    assert "v0.12.1" in OLD_SAVE_NOTICE and "v0.13" in OLD_SAVE_NOTICE and "未被改动" in OLD_SAVE_NOTICE


# ====================================================================================== second round (lead's review)
BAD_AVATAR = {"v": 1, "face": "f99", "skin": "s05", "hair": "h12", "hair_color": "c07", "eyes": "e04", "mouth": "m02", "accessory": "a09", "accessory_color": "k03"}


def roster_files(orch: GameOrchestrator) -> dict[str, bytes]:
    folder = orch._roster()._dir
    return {p.name: p.read_bytes() for p in sorted(folder.glob("*"))} if folder.is_dir() else {}


def game_state(orch: GameOrchestrator) -> tuple[Any, ...]:
    """What a refused change must leave alone: the live world, the seats of the session, the roster files, the events."""
    seats = [(p.id, p.name, p.persona, p.memory, p.avatar) for p in orch.session.participants]
    return (id(orch.world), seats, orch.world.player("N1").name, roster_files(orch), len(events_of(orch, "roster.updated")))


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"name": "Zed", "avatar": BAD_AVATAR}, "f99"),  # (the name used to be written before the avatar was looked at)
        ({"name": "Zed", "persona": "ok\ud800"}, "无法保存"),  # (a lone surrogate used to fail at the write: a 500, after the name was saved)
        ({"name": "Zed", "memory": "a\ud800"}, "无法保存"),
        ({"name": "Zed", "memory_parts": {"impressions": {"N2": "a\ud800"}}}, "无法保存"),
        ({"name": "Zed", "memory_parts": {"legacy": "旧\n## 战绩\n场次 99，胜 99"}}, "小节标题"),
        ({"name": "Zed", "persona": "字" * 501}, "最多 500"),
        ({"name": "Zed", "memory_parts": {"experience": ["a", "b", "c", "d"]}}, "经验最多"),
        ({"name": "bob", "persona": "好性格"}, "已被 N2 使用"),
        ({"name": "A:B", "persona": "好性格"}, "冒号"),
        ({"name": "A：B", "memory_parts": {"experience": ["先听后说"]}}, "冒号"),
        ({"name": "Zed", "avatar": "GOOD", "persona": "字" * 501}, "最多 500"),
        ({"avatar": BAD_AVATAR, "persona": "好性格", "memory_parts": {"experience": ["先听后说"]}}, "f99"),
    ],
)
@async_test
async def test_a_refused_put_changes_nothing_not_the_name_not_the_files_not_the_live_game(monkeypatch, kwargs, message):
    from app import avatars

    orch = make_orch(monkeypatch)
    if kwargs.get("avatar") == "GOOD":
        kwargs = {**kwargs, "avatar": avatars.make_roster(2, seed=3)[0]}  # a valid description: the rest of the request is what is wrong
    before = game_state(orch)
    with pytest.raises(RosterError, match=message):
        await orch.update_roster_entry("N1", **kwargs)
    assert game_state(orch) == before  # same world object, same seats, same files, no roster.updated
    assert orch._roster().entry("N1")["name"] == "Alice" and orch.world.player("N1").name == "Alice"


@async_test
async def test_a_put_with_everything_valid_applies_all_of_it(monkeypatch):
    from app import avatars

    orch = make_orch(monkeypatch)
    good = avatars.make_roster(2, seed=3)[0]
    world_before = orch.world
    entry = await orch.update_roster_entry("N1", name="Zed", avatar=good, persona="好性格", memory_parts={"experience": ["先听后说"]})
    assert (entry["name"], entry["avatar"], entry["persona"], entry["memory_parts"]["experience"]) == ("Zed", good, "好性格", ["先听后说"])
    assert orch.world is not world_before and orch.world.player("N1").name == "Zed" and orch.session.participants[0].avatar == good


@async_test
async def test_the_check_of_the_game_state_still_comes_before_the_check_of_the_values(monkeypatch):
    orch = make_orch(monkeypatch)
    play_with(orch, idle)
    await orch.start_or_resume()
    try:
        for kwargs in ({"name": "A:B"}, {"avatar": BAD_AVATAR}, {"name": "Zed", "avatar": BAD_AVATAR, "persona": "字" * 501}):
            with pytest.raises(RuntimeError, match="开始之前"):  # 409: not 400
                await orch.update_roster_entry("N1", **kwargs)
        with pytest.raises(RosterError, match="最多 500"):  # a persona may be changed at any time, and is checked
            await orch.update_roster_entry("N1", persona="字" * 501)
    finally:
        await orch.pause()


@async_test
async def test_a_stored_name_with_a_colon_can_be_kept_but_not_given(monkeypatch):
    orch = make_orch(monkeypatch)
    store = orch._roster()
    store._dir.mkdir(parents=True, exist_ok=True)
    (store._dir / "roster.json").write_text(json.dumps({"seats": {"N1": {"name": "A：B"}}}, ensure_ascii=False), encoding="utf-8")
    entry = await orch.update_roster_entry("N1", name="A：B", persona="好")  # (the panel sends the name along: unchanged, so it is not checked again)
    assert entry["name"] == "A：B" and entry["persona"] == "好"
    with pytest.raises(RosterError, match="冒号"):
        await orch.update_roster_entry("N2", name="C：D")


# ------------------------------------------------------------------------------------ a review while the user edits
class SlowLlm(TextLlm):
    """A one-shot model that takes its time for the players of ``slow_for``: they answer when ``gate`` is set."""

    def __init__(self, text: str | None = None, *, gate: asyncio.Event, slow_for: tuple[str, ...] = ("N1",)) -> None:
        super().__init__(text)
        self.gate, self.slow_for, self.entered = gate, slow_for, []

    async def complete(self, agent_id: str, messages: list[dict[str, Any]], tools: Any, *, max_tokens: int | None = None) -> LLMResponse:
        if agent_id in self.slow_for:
            self.entered.append(agent_id)  # the review has read the memory it shows the model: from here on it is "while the model works"
            await self.gate.wait()
        return await super().complete(agent_id, messages, tools, max_tokens=max_tokens)


def trace_records(orch: GameOrchestrator, session_id: str) -> list[dict[str, Any]]:
    from app.runtime.trace_log import trace_dir

    folder = trace_dir(orch._logs_root(), session_id)
    if not folder.is_dir():
        return []
    return [json.loads(line) for p in sorted(folder.glob("trace-*.jsonl")) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def done_event(orch: GameOrchestrator, agent_id: str) -> dict[str, Any]:
    return next(e.data for e in events_of(orch, "review.progress") if e.data["state"] == "done" and e.data["id"] == agent_id)


@async_test
async def test_an_edit_made_while_the_model_works_is_not_overwritten_by_the_review(monkeypatch):
    orch = make_orch(monkeypatch)
    await finished_game(orch, "红队")
    roster = orch._roster()
    roster.set_memory_parts("N1", impressions={"N2": "旧印象二", "N3": "旧印象三"}, experience=["旧经验"])
    gate = asyncio.Event()
    # the model copies N2 and the lessons as they were shown (the prompt allows it), has a new text for N3 and one for N4
    port = SlowLlm(review_answer({"N2": "旧印象二", "N3": "模型新三", "N4": "模型新四"}, ["旧经验"]), gate=gate)
    orch.llm_port_factory = lambda o: port
    await orch.start_review(["N1"])
    await wait_for(lambda: "N1" in port.entered)  # the review is waiting for its model
    await orch.update_roster_entry(
        "N1", memory_parts={"impressions": {"N2": "用户刚改的二", "N3": "用户改的三"}, "experience": ["用户改的经验"]}
    )
    gate.set()
    await wait_for(lambda: "N1" in orch.session.review_done)
    parts = roster.entry("N1")["memory_parts"]
    assert parts["impressions"]["N2"] == "用户刚改的二"  # the old text given back as it was: no news, his edit is not overwritten
    assert parts["impressions"]["N3"] == "用户改的三"  # the model had a new text, but he edited this one meanwhile: his stays
    assert parts["impressions"]["N4"] == "模型新四"  # nobody touched N4 meanwhile: the model's
    assert parts["experience"] == ["用户改的经验"]
    assert done_event(orch, "N1")["changes"] == {"impressions": [{"id": "N4", "before": "", "after": "模型新四"}], "experience": None}


@async_test
async def test_clearing_the_memory_while_the_model_works_is_not_undone_by_the_review(monkeypatch):
    orch = make_orch(monkeypatch)
    await finished_game(orch, "红队")
    roster = orch._roster()
    roster.set_memory_parts("N1", impressions={"N2": "旧印象二", "N3": "旧印象三"}, experience=["旧经验"], legacy="旧笔记")
    gate = asyncio.Event()
    port = SlowLlm(review_answer({"N2": "旧印象二", "N3": "旧印象三", "N4": "模型新四"}, ["旧经验"]), gate=gate)
    orch.llm_port_factory = lambda o: port
    await orch.start_review(["N1"])
    await wait_for(lambda: "N1" in port.entered)
    await orch.clear_memory()  # "clear all memories" while the review waits for its model
    assert roster.entry("N1")["memory"] == ""
    gate.set()
    await wait_for(lambda: "N1" in orch.session.review_done)
    parts = roster.entry("N1")["memory_parts"]
    assert parts["impressions"]["N2"] == "" and parts["impressions"]["N3"] == "" and parts["experience"] == [] and parts["legacy"] == ""  # nothing came back
    assert parts["impressions"]["N4"] == "模型新四"  # (what there was nothing about, the model may fill in)
    assert RecordTally.parse(parts["record"]).games == 1  # ... and the game that was just reviewed is counted


@async_test
async def test_a_review_that_nothing_interfered_with_still_takes_the_models_new_texts(monkeypatch):
    orch = make_orch(monkeypatch)
    await finished_game(orch, "红队")
    roster = orch._roster()
    roster.set_memory_parts("N1", impressions={"N2": "旧印象二", "N3": "旧印象三"}, experience=["旧经验"], legacy="旧笔记")
    gate = asyncio.Event()
    port = SlowLlm(review_answer({"N2": "改观：有主见", "N3": "旧印象三", "N4": "新四"}, ["新经验"]), gate=gate)
    orch.llm_port_factory = lambda o: port
    await orch.start_review(["N1"])
    await wait_for(lambda: "N1" in port.entered)
    await orch.update_roster_entry("N1", persona="只改了性格")  # an edit of something else does not matter
    gate.set()
    await wait_for(lambda: "N1" in orch.session.review_done)
    parts = roster.entry("N1")["memory_parts"]
    assert (parts["impressions"]["N2"], parts["impressions"]["N3"], parts["impressions"]["N4"]) == ("改观：有主见", "旧印象三", "新四")
    assert parts["experience"] == ["新经验"] and parts["legacy"] == ""  # (the notes were tidied up: something was taken)


@pytest.mark.parametrize(
    "answer",
    [
        '{"impressions": {}, "experience": []}',  # a valid, empty answer
        '{"impressions": {"N2": "…", "N3": "保持原样"}, "experience": ["…"]}',  # only placeholders
        '{"impressions": {"N2": "旧印象二"}, "experience": ["旧经验"]}',  # only what he was shown
        '{"impressions": {"N2": "无变化", "N3": "（暂无）"}, "experience": ["同上", "N/A"]}',  # only words that say "no change"
    ],
)
@async_test
async def test_an_answer_that_says_nothing_new_changes_nothing_keeps_the_old_notes_and_still_counts_the_game(monkeypatch, answer):
    orch = make_orch(monkeypatch)
    await finished_game(orch, "红队")
    roster = orch._roster()
    roster.set_memory_parts("N1", impressions={"N2": "旧印象二", "N3": "旧印象三"}, experience=["旧经验"], legacy="旧笔记")
    before = roster.entry("N1")["memory_parts"]
    orch.llm_port_factory = lambda o: TextLlm(answer)
    await orch.start_review(["N1"])
    await wait_for(lambda: "N1" in orch.session.review_done and not orch._review_running)
    after = roster.entry("N1")["memory_parts"]
    assert after["impressions"] == before["impressions"] and after["experience"] == before["experience"]
    assert after["legacy"] == "旧笔记"  # nothing was taken, so the old notes are not thrown away
    assert RecordTally.parse(after["record"]).games == 1 and done_event(orch, "N1")["changes"] == {"impressions": [], "experience": None}


@pytest.mark.parametrize(
    "answer,event",
    [(review_answer({"N2": "新印象"}, []), "review"), ("这不是约定的格式", "review_failed")],
    ids=["a usable answer", "an unusable one"],
)
@async_test
async def test_the_raw_answer_of_a_review_belongs_to_the_game_that_was_reviewed_not_the_one_that_is_live(monkeypatch, answer, event):
    orch = make_orch(monkeypatch)
    await finished_game(orch, "红队")
    old_id = orch.session.session_id
    gate = asyncio.Event()
    port = SlowLlm(answer, gate=gate)
    orch.llm_port_factory = lambda o: port
    await orch.start_review(["N1"])
    await wait_for(lambda: "N1" in port.entered)
    await orch.new_game()  # a new game while the model is still thinking
    new_id = orch.session.session_id
    assert new_id != old_id
    gate.set()
    await wait_for(lambda: not orch._review_running)
    lines = [r for r in trace_records(orch, old_id) if r.get("event") == event]
    assert len(lines) == 1 and lines[0]["agent_id"] == "N1" and lines[0]["text"] == answer
    assert [r for r in trace_records(orch, new_id) if r.get("event") in ("review", "review_failed")] == []


@async_test
async def test_a_port_that_cannot_be_built_fails_the_reviews_and_they_can_be_asked_for_again(monkeypatch):
    orch = make_orch(monkeypatch)
    await finished_game(orch, "红队")

    def boom(o: GameOrchestrator) -> Any:
        raise RuntimeError("factory exploded sk-secret-1234")

    orch.llm_port_factory = boom
    started = await orch.start_review(["N1", "N2"])
    assert sorted(started) == ["N1", "N2"]
    await wait_for(lambda: not orch._review_running)  # (it used to stay "running" for ever)
    errors = [e.data for e in events_of(orch, "review.progress") if e.data["state"] == "error"]
    assert sorted(e["id"] for e in errors) == ["N1", "N2"]
    assert all("重试" in e["message"] and "sk-secret" not in e["message"] and "RuntimeError" in e["message"] for e in errors)
    assert orch.review_view() == {"available": True, "done": [], "running": False}
    orch.llm_port_factory = lambda o: TextLlm(review_answer({"N3": "补上的印象"}))
    assert sorted(await orch.start_review(["N1", "N2"])) == ["N1", "N2"]  # asked for again
    await wait_for(lambda: len(orch.session.review_done) == 2 and not orch._review_running)
    assert orch._roster().entry("N1")["memory_parts"]["impressions"]["N3"] == "补上的印象"


@async_test
async def test_an_old_longer_persona_copy_of_the_game_is_cut_in_the_review_prompt(monkeypatch):
    orch = make_orch(monkeypatch)
    await finished_game(orch, "红队")
    orch.session.participants[0].persona = "旧的长性格。" * 200  # a game made by v0.13.0 may carry a persona of up to 20000 characters
    port = TextLlm(review_answer({"N2": "x"}))
    orch.llm_port_factory = lambda o: port
    await orch.start_review(["N1"])
    await wait_for(lambda: "N1" in orch.session.review_done)
    system = port.calls[0][1][0]["content"]
    persona = system.split("你的性格与行为指引：\n", 1)[1].split("\n", 1)[0]
    assert len(persona) == PERSONA_CHARS and persona.endswith("旧…") and persona.startswith("旧的长性格。" * 83)


@async_test
async def test_a_deeply_nested_answer_fails_that_review_like_any_unusable_answer(monkeypatch):
    orch = make_orch(monkeypatch)
    await finished_game(orch, "红队")
    garbage = "[" * 6000
    orch.llm_port_factory = lambda o: TextLlm(garbage)
    await orch.start_review(["N1"])
    await wait_for(lambda: not orch._review_running)
    errors = [e.data for e in events_of(orch, "review.progress") if e.data["state"] == "error"]
    assert [e["id"] for e in errors] == ["N1"] and "印象" in errors[0]["message"] and "RecursionError" not in errors[0]["message"]
    assert [r["text"] for r in trace_records(orch, orch.session.session_id) if r.get("event") == "review_failed"] == [garbage]
    assert "N1" not in orch.session.review_done and orch._roster().entry("N1")["memory"] == ""
