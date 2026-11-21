"""The initialization of ONE bot (v0.14, H19-H22; contract §13.6): ``app.learning.init_runner.run_init`` over a real bot store and the real
learning brain, with a scripted model.

What is tested is the promise of the contract: all or nothing (C16) - the files are written together when the run is over and everything required
is there, else not at all and the bot is ``failed`` with the reason -, a revision starts from the memory the bot has, the hashes are those of the
start of the run, and a model that cannot be reached or a clock that runs out leave everything as it was.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from game_fakes import async_test
from learning_fakes import ARCH_NOTES, DIGEST, PLAYBOOK, QUESTIONS, Bench, ScriptedLlm, good_init
from learning_fakes import Call


@pytest.fixture()
def bench(tmp_path: Path) -> Bench:
    return Bench(tmp_path / "data")


def writes(*files: tuple[str, str]) -> list[tuple[str, dict[str, Any]]]:
    return [("write_memory", {"file": name, "content": text}) for name, text in files]


@async_test
async def test_a_good_run_commits_everything_together_and_the_bot_is_ready(bench: Bench) -> None:
    bot = bench.ids[0]
    assert bench.status(bot) == "uninitialized"
    outcome = await bench.init(bot, ScriptedLlm(good_init))
    assert outcome.state == "done" and outcome.error is None and outcome.rounds == 4
    assert outcome.files_written == ["arch_notes", "digest", "playbook", "questions"]
    memory = bench.memory(bot)
    assert (memory["arch_notes"], memory["digest"], memory["questions"], memory["people"]) == (ARCH_NOTES, DIGEST, QUESTIONS, "")
    assert all(line in memory["playbook"] for line in PLAYBOOK.splitlines())  # (the store writes the playbook in its own canonical form)
    summary = bench.store.summary(bot, sources=bench.hashes)
    assert summary["init"]["status"] == "ready" and summary["init"]["model"] == "fake / scripted" and summary["init"]["error"] is None
    assert summary["init"]["stale"] == []
    assert bench.store.init_record(bot)["scenario_id"] == bench.scenario_id


@async_test
async def test_nothing_is_on_disk_until_the_run_is_over(bench: Bench) -> None:
    bot = bench.ids[0]
    seen: list[dict[str, str]] = []

    def script(call: Call) -> Any:
        if call.index == 3:  # the model is about to finish: everything it wrote is only staged
            seen.append(bench.memory(bot))
        return good_init(call)

    outcome = await bench.init(bot, ScriptedLlm(script))
    assert outcome.state == "done"
    assert seen == [{"arch_notes": "", "digest": "", "playbook": "", "questions": "", "people": ""}]
    assert bench.memory(bot)["arch_notes"] == ARCH_NOTES


@async_test
async def test_questions_are_optional(bench: Bench) -> None:
    bot = bench.ids[1]

    def script(call: Call) -> Any:
        return [
            [("read_source", {"source": "contract", "part": "reference"})],
            writes(("arch_notes", ARCH_NOTES), ("digest", DIGEST), ("playbook", PLAYBOOK)),
            [("finish", {})],
        ][min(call.index, 2)]

    outcome = await bench.init(bot, ScriptedLlm(script))
    assert outcome.state == "done" and bench.status(bot) == "ready" and bench.memory(bot)["questions"] == ""


@async_test
async def test_a_second_write_of_a_file_replaces_the_first_and_what_is_over_the_limit_is_refused_with_the_limit(bench: Bench) -> None:
    bot = bench.ids[0]
    too_long = "我理解的东西很多。" * 120  # over the 800 characters of the operating notes
    results: list[str] = []

    def script(call: Call) -> Any:
        if call.index == 0:
            return writes(("arch_notes", too_long))
        if call.index == 1:
            results.extend(call.last_tool_results)
            return writes(("arch_notes", "第一版。"), ("digest", DIGEST), ("playbook", PLAYBOOK))
        if call.index == 2:
            return writes(("arch_notes", ARCH_NOTES))  # the whole file again: this is the one that counts
        return [("finish", {})]

    outcome = await bench.init(bot, ScriptedLlm(script))
    assert "最多 800" in results[0] and "没有" in results[0]  # the model is told what the limit is and that nothing was kept
    assert outcome.state == "done" and bench.memory(bot)["arch_notes"] == ARCH_NOTES


@async_test
async def test_a_playbook_that_is_not_understood_is_refused_and_a_tag_the_scenario_does_not_have_is_only_a_hint(bench: Bench) -> None:
    bot = bench.ids[0]
    results: list[str] = []
    odd_tag = "- [把握:低|支持:0|反例:0|来源:初始化|#没有这个标签] 先看事实。"

    def script(call: Call) -> Any:
        if call.index == 0:
            return writes(("playbook", "随便写写，没有格式"))
        if call.index == 1:
            results.extend(call.last_tool_results)
            return writes(("playbook", odd_tag), ("arch_notes", ARCH_NOTES), ("digest", DIGEST))
        if call.index == 2:
            results.extend(call.last_tool_results)
        return [("finish", {})]

    outcome = await bench.init(bot, ScriptedLlm(script))
    assert "没有读出任何一条打法" in results[0]
    assert "检索不到" in results[1] and "#没有这个标签" in results[1] and outcome.state == "done"  # written all the same
    assert "没有这个标签" in bench.memory(bot)["playbook"]


# ------------------------------------------------------------------------------------------------------------------ all or nothing
@async_test
async def test_a_missing_required_file_fails_the_run_and_the_old_memory_stays_exactly_as_it_was(bench: Bench) -> None:
    bot = bench.ids[0]
    bench.ready(bot)
    before = bench.memory(bot)
    bench.store.write_part(bot, "people", "- Bob：爱抢话。")  # (the impressions are not part of an initialization either way)
    llm = ScriptedLlm(lambda call: writes(("arch_notes", "新的操作笔记。"), ("digest", "新的规则要点。")) if call.index == 0 else [("finish", {})])
    outcome = await bench.init(bot, llm)
    assert outcome.state == "failed" and "playbook" in (outcome.error or "") and "没有改动" in (outcome.error or "")
    assert bench.memory(bot) == {**before, "people": "- Bob：爱抢话。"}
    summary = bench.store.summary(bot, sources=bench.hashes)
    assert summary["init"]["status"] == "failed" and "playbook" in summary["init"]["error"]  # the card says why
    assert llm.counts[bot] == 3  # (finished too early: told once, finished again)


@async_test
async def test_a_run_that_runs_out_of_time_is_a_failure_and_nothing_it_wrote_is_kept(bench: Bench) -> None:
    bot = bench.ids[0]
    bench.ready(bot)
    before = bench.memory(bot)

    class Hangs(ScriptedLlm):
        async def complete(self, agent_id: str, messages: Any, tools: Any, *, max_tokens: int | None = None) -> Any:
            if self.counts.get(agent_id, 0) >= 2:
                self.counts[agent_id] += 1
                await asyncio.sleep(30)
            return await super().complete(agent_id, messages, tools, max_tokens=max_tokens)

    outcome = await bench.init(bot, Hangs(good_init), deadline_s=1.5)
    assert outcome.state == "timeout" and "时间到了" in (outcome.error or "") and outcome.rounds == 3
    assert outcome.files_written == ["arch_notes", "digest"]  # (what it had written by then: staged, never committed)
    assert bench.memory(bot) == before
    assert bench.store.summary(bot, sources=bench.hashes)["init"]["status"] == "failed"


@async_test
async def test_a_model_that_cannot_be_reached_fails_the_run_and_changes_nothing(bench: Bench) -> None:
    bot = bench.ids[0]
    outcome = await bench.init(bot, ScriptedLlm(good_init, fail_for=(bot,)))
    assert outcome.state == "failed" and "调用模型失败" in (outcome.error or "") and "RuntimeError" in (outcome.error or "")
    assert bench.memory(bot) == {"arch_notes": "", "digest": "", "playbook": "", "questions": "", "people": ""}
    assert bench.status(bot) == "failed"


@async_test
async def test_a_bot_without_a_complete_l0_is_not_asked_anything(bench: Bench) -> None:
    bot = bench.ids[0]
    bench.store.clear_core([bot])
    llm = ScriptedLlm(good_init)
    outcome = await bench.init(bot, llm)
    assert outcome.state == "failed" and "底层设定不完整" in (outcome.error or "") and llm.requests == []
    assert bench.status(bot) == "failed" and bench.store.summary(bot)["init"]["error"] == outcome.error


@async_test
async def test_cancelling_a_run_changes_nothing_and_does_not_mark_the_bot(bench: Bench) -> None:
    bot = bench.ids[0]
    bench.ready(bot)
    before = bench.memory(bot)
    gate = asyncio.Event()
    llm = ScriptedLlm(good_init, gate=gate)
    task = asyncio.ensure_future(bench.init(bot, llm, deadline_s=30))
    await asyncio.sleep(0.05)
    assert llm.in_flight == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bench.memory(bot) == before and bench.status(bot) == "ready"  # as it was: the card is not turned into a failure by a cancel


# ------------------------------------------------------------------------------------------------------------------- a revision
@async_test
async def test_a_bot_that_has_a_memory_revises_it_and_reads_what_it_has_first(bench: Bench) -> None:
    bot = bench.ids[0]
    bench.ready(bot)
    bench.store.write_part(bot, "questions", "我之前还不确定的事。", bench.scenario_id)
    reads: list[str] = []
    first_messages: list[str] = []

    def script(call: Call) -> Any:
        if call.index == 0:
            first_messages.append(call.first)
            return [("read_source", {"source": "my_memory"}), ("read_source", {"source": "my_core"})]
        if call.index == 1:
            reads.extend(call.last_tool_results)
            return writes(("arch_notes", "修订后的操作笔记。"), ("digest", DIGEST), ("playbook", PLAYBOOK))
        return [("finish", {})]

    outcome = await bench.init(bot, ScriptedLlm(script))
    assert outcome.state == "done"
    assert "这次是修订" in first_messages[0] and "操作笔记" in first_messages[0]
    assert ARCH_NOTES in reads[0] and "我之前还不确定的事。" in reads[0] and "我的记忆" in reads[0]  # what it has now
    assert "身份" in reads[1]  # (its own L0 is readable too)
    assert bench.memory(bot)["arch_notes"] == "修订后的操作笔记。" and bench.memory(bot)["questions"] == "我之前还不确定的事。"  # (a file it left out stays)


@async_test
async def test_a_second_run_starts_from_what_the_first_staged_only_in_its_own_session(bench: Bench) -> None:
    """``read_source("my_memory")`` shows what the run itself has staged on top of what is on disk (a model that checks its own work reads it back)."""
    bot = bench.ids[0]
    seen: list[str] = []

    def script(call: Call) -> Any:
        if call.index == 0:
            return writes(("arch_notes", "暂存的操作笔记。")) + [("read_source", {"source": "my_memory", "part": "arch_notes"})]
        if call.index == 1:
            seen.extend(call.last_tool_results)
            return writes(("digest", DIGEST), ("playbook", PLAYBOOK))
        return [("finish", {})]

    await bench.init(bot, ScriptedLlm(script))
    assert any("暂存的操作笔记。" in text for text in seen)


# ------------------------------------------------------------------------------------------------------------------- hashes
@async_test
async def test_the_result_is_stamped_with_the_hashes_of_the_start_and_an_l0_edited_meanwhile_makes_it_stale(bench: Bench) -> None:
    bot = bench.ids[0]
    from app.bots.types import CoreFiles

    def script(call: Call) -> Any:
        if call.index == 2:  # the user edits the L0 while the bot is working
            core = bench.store.core(bot)
            bench.store.set_core(bot, CoreFiles(identity=core.identity + " 改动。", voice=core.voice, mind=core.mind, extras=core.extras))
        return good_init(call)

    outcome = await bench.init(bot, ScriptedLlm(script))
    assert outcome.state == "done"
    summary = bench.store.summary(bot, sources=bench.hashes)
    assert summary["init"]["status"] == "stale" and summary["init"]["stale"] == ["core"]  # (the run began with the old L0)
    assert bench.memory(bot)["arch_notes"] == ARCH_NOTES  # ... but what it wrote is there


@async_test
async def test_a_contract_or_scenario_that_changes_afterwards_makes_the_bot_stale(bench: Bench) -> None:
    from app.bots.types import SourceHashes

    bot = bench.ids[0]
    await bench.init(bot, ScriptedLlm(good_init))
    assert bench.status(bot) == "ready"
    other_contract = SourceHashes(contract="0" * 64, scenario=bench.hashes.scenario, materials="", scenario_id=bench.scenario_id)
    assert bench.status(bot, other_contract) == "stale"
    other_materials = SourceHashes(contract=bench.hashes.contract, scenario=bench.hashes.scenario, materials="f" * 64, scenario_id=bench.scenario_id)
    assert bench.store.summary(bot, sources=other_materials)["init"]["stale"] == ["materials"]


@async_test
async def test_the_study_materials_are_readable_and_the_rules_are_the_players_part_only(bench: Bench) -> None:
    bot = bench.ids[0]
    seen: dict[str, str] = {}

    def script(call: Call) -> Any:
        if call.index == 0:
            return [
                ("read_source", {"source": "rules"}),
                ("read_source", {"source": "materials"}),
                ("read_source", {"source": "materials", "part": "notes"}),
                ("read_source", {"source": "contract", "part": "must_read"}),
            ]
        if call.index == 1:
            for key, text in zip(("rules", "list", "notes", "contract"), call.last_tool_results):
                seen[key] = text
        return good_init(Call(call.agent_id, call.messages, call.tools, max(call.index - 1, 0) + 1)) if call.index >= 1 else []

    await bench.init(bot, ScriptedLlm(script), materials={"notes": "学习材料的正文。"})
    assert "最小测试剧本" in seen["rules"] and "主持人手册" not in seen["rules"] and "情境标签" in seen["rules"]  # (the manual is the moderator's)
    assert "notes" in seen["list"] and "学习材料的正文。" in seen["notes"] and seen["contract"].startswith("【平台契约·必读】")
