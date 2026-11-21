"""The learning jobs of the orchestrator (v0.14, H19-H24, H31-H34; contract §13.6 - §13.8): ``app.learning.jobs.LearningHub`` and its mixin.

A real orchestrator over fake stores (and a real bot store in the temporary data directory), the learning runs driven by a scripted model: when a job
may start, that every bot of it works at the same time, what the page is told (``bot.init.progress`` / ``review.progress``, ``bots.updated``,
``init_job``, the status label), cancelling, the clock, the books (``bots/usage.json``, the error log, ``logs/learning``), and what a job keeps others
from doing.  Nothing here calls a model.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import app.learning.jobs as jobs_module
from app.learning.jobs import LearningHub, LearningRefused
from game_fakes import (
    PROVIDER,
    FakeProviders,
    async_test,
    bots_for,
    events_of,
    idle_moderator,
    make_table_orch,
    wait_for,
)
from learning_fakes import ARCH_NOTES, ScriptedLlm, by_kind, good_init, good_review
from test_call_log_api import end_at_once, play_with

IDS = ["N1", "N2", "N3", "N4"]


def table(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Any:
    """An orchestrator whose four bots were never initialized."""
    kwargs.setdefault("ready", False)
    return make_table_orch(monkeypatch, **kwargs)


def use(orch: Any, llm: ScriptedLlm) -> ScriptedLlm:
    orch.llm_port_factory = lambda o: llm
    return llm


def progress(orch: Any, *, bot: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
    rows = [e.data for e in events_of(orch, "bot.init.progress")]
    return [r for r in rows if (bot is None or r["bot_id"] == bot) and (state is None or r["state"] == state)]


def statuses(orch: Any) -> dict[str, str]:
    return {row["name"]: row["init"]["status"] for row in orch.bot_summaries()}


async def settled(orch: Any) -> None:
    await wait_for(lambda: not orch.init_running_ids() and orch.init_job_view() is None)
    await asyncio.sleep(0.02)


# ======================================================================================================== initialization
@async_test
async def test_every_bot_of_a_job_works_at_the_same_time_and_the_page_is_told_how_each_is_doing(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = table(monkeypatch)
    gate = asyncio.Event()
    llm = use(orch, ScriptedLlm(by_kind(), gate=gate))
    bot_ids = orch._bots().lineup()
    assert set(statuses(orch).values()) == {"uninitialized"} and orch.system_status()["code"] == "uninitialized"

    started = await orch.start_init()
    assert set(started) == {"job_id", "ids", "deadline_s"} and started["ids"] == bot_ids and started["deadline_s"] == 120
    await wait_for(lambda: llm.in_flight == 4)  # all four are asking their model at once
    assert llm.peak == 4 and orch.init_running_ids() == set(bot_ids) and orch.init_busy_ids() == set(bot_ids)
    view = orch.init_job_view()
    assert view is not None and view["job_id"] == started["job_id"] and view["state"] == "running" and view["ids"] == bot_ids
    assert view["deadline_s"] == 120 and isinstance(view["started_at"], int)
    assert orch.bots_payload()["init_job"] == view
    assert orch.system_status() == {"code": "initializing", "text": "初始化中", "reason": ""}
    assert {s["init"]["status"] for s in orch.bot_summaries()} == {"initializing"}
    problems = orch.check_ready()
    assert [item["code"] for p in problems if p["scope"] == "bot" for item in p["items"]] == ["init_running"] * 4  # a start is refused meanwhile
    assert {row["state"] for row in progress(orch)} == {"queued", "running"}
    running = progress(orch, state="running")[0]
    assert set(running) == {"job_id", "bot_id", "name", "state", "seconds_left", "deadline_s", "files_written", "error"}
    assert running["job_id"] == started["job_id"] and running["seconds_left"] in range(115, 121) and running["deadline_s"] == 120

    gate.set()
    await settled(orch)
    assert statuses(orch) == {"Alice": "ready", "Bob": "ready", "Carol": "ready", "David": "ready"}
    done = progress(orch, state="done")
    assert sorted(row["bot_id"] for row in done) == sorted(bot_ids)
    assert all(row["files_written"] == ["arch_notes", "digest", "playbook", "questions"] and row["error"] is None and row["seconds_left"] is None for row in done)
    assert orch.init_job_view() is None and orch.system_status()["code"] == "before_game" and orch.check_ready() == []
    assert events_of(orch, "bots.updated")  # the cards were told each time one changed
    assert [e.data["system_status"]["code"] for e in events_of(orch, "session.updated")][-1] == "before_game"
    await orch.shutdown()


@async_test
async def test_a_bot_is_stamped_with_its_own_model_and_the_hashes_of_the_start_of_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = table(monkeypatch)
    use(orch, ScriptedLlm(good_init))
    await orch.start_init(ids=[orch._bots().lineup()[1]])
    await settled(orch)
    summary = orch.bot_summaries()[1]
    assert summary["init"]["status"] == "ready" and summary["init"]["model"] == "prov / big" and summary["init"]["duration_s"] is not None
    assert summary["init"]["stale"] == [] and summary["binding"] == {"ok": True, "label": "prov / big"}
    assert [s["init"]["status"] for s in orch.bot_summaries()] == ["uninitialized", "ready", "uninitialized", "uninitialized"]  # only the one asked for
    hashes = orch.source_hashes()
    record = orch._bots().init_record(orch._bots().lineup()[1])
    assert record["hashes"]["contract"] == hashes.contract and record["hashes"]["scenario"] == hashes.scenario and record["scenario_id"] == "minimal_test"
    await orch.shutdown()


@async_test
async def test_the_default_is_every_bot_that_is_not_ready_and_rebuild_does_all_of_them_again(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = table(monkeypatch)
    llm = use(orch, ScriptedLlm(good_init))
    ids = orch._bots().lineup()
    await orch.start_init(ids=ids[:2])
    await settled(orch)
    again = await orch.start_init()  # the other two
    assert again["ids"] == ids[2:]
    await settled(orch)
    assert set(statuses(orch).values()) == {"ready"}
    with pytest.raises(LearningRefused) as nothing:
        await orch.start_init()
    assert nothing.value.status == 400 and nothing.value.code == "nothing_to_do" and "全部重新初始化" in str(nothing.value)
    calls = sum(llm.counts.values())
    revised = await orch.start_init(rebuild=True)  # a revision of everybody
    assert revised["ids"] == ids
    await settled(orch)
    assert sum(llm.counts.values()) == calls + 16 and set(statuses(orch).values()) == {"ready"}
    first_message = next(m for who, m in reversed(llm.requests) if who == ids[0])[1]["content"]
    assert "这次是修订" in first_message  # (a bot that has a memory is asked to revise it)
    await orch.shutdown()


@async_test
async def test_a_request_is_refused_with_its_reason_and_nothing_is_started(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = table(monkeypatch)
    llm = use(orch, ScriptedLlm(good_init))
    with pytest.raises(LearningRefused) as unknown:
        await orch.start_init(ids=["b-00000000"])
    assert unknown.value.status == 404 and unknown.value.code == "not_found"
    assert llm.requests == [] and orch.init_job_view() is None and orch.init_running_ids() == set()
    await orch.shutdown()


@async_test
async def test_a_bot_without_a_model_keeps_the_whole_request_from_starting_and_the_problems_say_who(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = table(monkeypatch, bound_all=False)
    llm = use(orch, ScriptedLlm(good_init))
    with pytest.raises(LearningRefused) as refused:
        await orch.start_init()
    error = refused.value
    assert error.status == 409 and error.code == "no_binding" and "还没有可用的模型" in str(error)
    assert sorted(p["name"] for p in error.problems) == ["Alice", "Bob", "Carol", "David"]
    first = error.problems[0]
    assert first["scope"] == "binding" and first["bot_id"] and first["seat"] == "N1" and first["items"][0]["code"] == "no_binding"
    assert first["items"][0]["fix"] == "bind_model" and "未绑定模型" in first["items"][0]["message"]
    assert llm.requests == [] and set(statuses(orch).values()) == {"uninitialized"}
    # (a model that is gone is the same: the provider of the binding does not exist any more)
    orch2 = table(monkeypatch)
    orch2.provider_store = FakeProviders()
    with pytest.raises(LearningRefused) as gone:
        await orch2.start_init(ids=[orch2._bots().lineup()[0]])
    assert gone.value.code == "no_binding" and "已不存在" in gone.value.problems[0]["items"][0]["message"]
    await orch.shutdown()
    await orch2.shutdown()


@async_test
async def test_only_one_job_at_a_time_and_only_before_a_game_or_after_it(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = table(monkeypatch)
    gate = asyncio.Event()
    use(orch, ScriptedLlm(good_init, gate=gate))
    await orch.start_init()
    with pytest.raises(LearningRefused) as busy:
        await orch.start_init()
    assert busy.value.status == 409 and busy.value.code == "init_busy"
    gate.set()
    await settled(orch)

    bots_for(orch, idle_moderator)
    orch.llm_port_factory = lambda o: ScriptedLlm(good_init)
    ready = table(monkeypatch)  # (a game that can start: its bots are initialized)
    for status in ("running", "paused"):
        ready.session.status = status
        with pytest.raises(LearningRefused) as active:
            await ready.start_init()
        assert active.value.status == 409 and active.value.code == "game_active" and "开始之前" in str(active.value)
    for status in ("idle", "finished"):  # a game that has not started and one that is over leave the bots free
        ready.session.status = status
        ready.llm_port_factory = lambda o: ScriptedLlm(good_init)
        assert (await ready.start_init(rebuild=True))["ids"]  # (the first orchestrator initialized the bots of the same data directory already)
        await settled(ready)
    await orch.shutdown()
    await ready.shutdown()


@async_test
async def test_cancelling_gives_every_bot_back_as_it_was_and_nothing_is_left_running(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = table(monkeypatch)
    ids = orch._bots().lineup()
    orch._bots()  # (the first bot has a finished initialization: it is revised, and the revision is cancelled)
    from learning_fakes import PLAYBOOK, ARCH_NOTES as NOTES, DIGEST

    orch._bots().commit_init(ids[0], "minimal_test", {"arch_notes": NOTES, "digest": DIGEST, "playbook": PLAYBOOK}, model="old", duration_s=1.0, sources=orch.source_hashes())
    before = orch._bots().read_part(ids[0], "arch_notes", "minimal_test")
    gate = asyncio.Event()  # never set: the models hang
    llm = use(orch, ScriptedLlm(good_init, gate=gate))
    await orch.start_init(rebuild=True)
    await wait_for(lambda: llm.in_flight == 4)
    assert await orch.cancel_init() is True
    assert llm.in_flight == 0 and orch.init_running_ids() == set() and orch.init_job_view() is None
    assert {row["state"] for row in progress(orch) if row["state"] not in ("queued", "running")} == {"cancelled"}
    assert len(progress(orch, state="cancelled")) == 4
    assert statuses(orch) == {"Alice": "ready", "Bob": "uninitialized", "Carol": "uninitialized", "David": "uninitialized"}  # as before
    assert orch._bots().read_part(ids[0], "arch_notes", "minimal_test") == before and orch.system_status()["code"] == "uninitialized"
    assert await orch.cancel_init() is False  # nothing is running any more
    assert not [t for t in asyncio.all_tasks() if (t.get_name() or "").startswith("init-")]
    await orch.shutdown()


@async_test
async def test_a_job_that_runs_out_of_its_time_fails_its_bots_whole_and_the_others_are_not_held_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs_module, "INIT_TIMEOUT_S", 2.0)
    orch = table(monkeypatch)
    slow = orch._bots().lineup()[0]

    class Slow(ScriptedLlm):
        async def complete(self, agent_id: str, messages: Any, tools: Any, *, max_tokens: int | None = None) -> Any:
            if agent_id == slow and self.counts.get(agent_id, 0) >= 2:
                self.counts[agent_id] += 1
                await asyncio.sleep(30)
            return await super().complete(agent_id, messages, tools, max_tokens=max_tokens)

    use(orch, Slow(good_init))
    started = await orch.start_init()
    assert started["deadline_s"] == 2.0
    await settled(orch)
    assert progress(orch, bot=slow, state="timeout") and not progress(orch, bot=slow, state="done")
    summaries = {row["id"]: row for row in orch.bot_summaries()}
    assert summaries[slow]["init"]["status"] == "failed" and "时间到了" in summaries[slow]["init"]["error"]
    assert [row["init"]["status"] for bot, row in summaries.items() if bot != slow] == ["ready"] * 3  # (the others were done long before)
    assert progress(orch, bot=slow, state="timeout")[0]["error"] == summaries[slow]["init"]["error"]
    assert orch._bots().read_part(slow, "arch_notes", "minimal_test") == ""  # all or nothing
    await orch.shutdown()


@async_test
async def test_a_model_that_cannot_be_reached_fails_that_bot_with_a_reason_that_carries_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = table(monkeypatch)
    bots = orch._bots().lineup()
    use(orch, ScriptedLlm(good_init, fail_for=(bots[2],), fail_text="401 Incorrect API key provided: sk-live-ABCDEFGH12345678"))
    await orch.start_init()
    await settled(orch)
    states = {row["bot_id"]: row for row in progress(orch) if row["state"] in ("done", "failed")}
    assert states[bots[2]]["state"] == "failed" and "401" in states[bots[2]]["error"] and "sk-live" not in json.dumps(states[bots[2]])
    assert [states[b]["state"] for b in bots if b != bots[2]] == ["done"] * 3
    card = orch.bot_summaries()[2]["init"]
    assert card["status"] == "failed" and "调用模型失败" in card["error"] and "sk-live" not in json.dumps(orch.bots_payload())
    await orch.shutdown()


@async_test
async def test_the_running_progress_of_a_bot_is_sent_at_most_once_a_second_and_every_second_while_it_works(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = table(monkeypatch)
    bot = orch._bots().lineup()[0]

    def chatter(call: Any) -> Any:  # thirty rounds in a blink, then the files
        if call.index < 30:
            return [("read_source", {"source": "contract", "part": "must_read"})]
        return good_init(type(call)(call.agent_id, call.messages, call.tools, call.index - 30))

    use(orch, ScriptedLlm(chatter))
    await orch.start_init(ids=[bot])
    await settled(orch)
    assert len(progress(orch, bot=bot, state="running")) <= 2  # (the first one, and at most one more when a second has passed)
    assert progress(orch, bot=bot)[-1]["state"] == "done"

    # while a bot is thinking, the page hears from it every tick (its countdown): here every 50 ms
    orch2 = table(monkeypatch)
    orch2._learning_hub = LearningHub(orch2, tick_s=0.05)
    bot2 = orch2._bots().lineup()[0]
    use(orch2, ScriptedLlm(good_init, delay=0.3))
    await orch2.start_init(ids=[bot2])
    await settled(orch2)
    running = progress(orch2, bot=bot2, state="running")
    lefts = [row["seconds_left"] for row in running]
    assert len(running) >= 4 and lefts == sorted(lefts, reverse=True) and all(isinstance(n, int) and 0 <= n <= 120 for n in lefts)
    await orch.shutdown()
    await orch2.shutdown()


@async_test
async def test_the_number_of_bots_at_work_can_be_capped_and_the_others_wait_their_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = table(monkeypatch)
    orch.cfg.setdefault("runtime", {})["learning_concurrency"] = 2
    gate = asyncio.Event()
    llm = use(orch, ScriptedLlm(good_init, gate=gate))
    await orch.start_init()
    await wait_for(lambda: llm.in_flight == 2)
    await asyncio.sleep(0.05)
    assert llm.peak == 2 and len(progress(orch, state="running")) == 2  # two work, two are still queued (their clock has not started)
    assert len({row["bot_id"] for row in progress(orch, state="queued")}) == 4
    gate.set()
    await settled(orch)
    assert set(statuses(orch).values()) == {"ready"} and llm.peak == 2
    await orch.shutdown()


# ------------------------------------------------------------------------------------------------------------ the books
@async_test
async def test_an_initialization_is_in_the_error_log_and_in_the_learning_usage_and_never_in_a_games_budget(monkeypatch: pytest.MonkeyPatch, isolated_external_storage: Path) -> None:
    orch = table(monkeypatch)
    use(orch, ScriptedLlm(good_init))
    assert orch.learning_usage() == {"calls": 0, "tokens": 0, "cost_usd": 0.0}
    await orch.start_init()
    await settled(orch)
    usage = orch.learning_usage()
    assert usage["calls"] == 16 and usage["tokens"] == 16 * 140 and usage["cost_usd"] == pytest.approx(16 * (100 * 1.0 + 40 * 2.0) / 1e6)
    assert orch.bots_payload()["learning_usage"] == usage
    stored = json.loads((isolated_external_storage / "bots" / "usage.json").read_text(encoding="utf-8"))
    assert stored == {"calls": 16, "tokens": usage["tokens"], "cost_usd": usage["cost_usd"]}
    assert orch.usage.total_tokens == 0 and orch.budget_payload()["used_tokens"] == 0  # the game's own books are untouched
    # a new process reads the totals again; the next job adds to them
    fresh = LearningHub(orch)
    assert fresh.usage_view() == usage
    lines = [c for c in orch.call_log().entries() if c["purpose"] == "init"]
    assert len(lines) == 16 and {c["status"] for c in lines} == {"ok"}
    bots = {row["id"]: row["name"] for row in orch.bot_summaries()}
    assert {c["actor_id"] for c in lines} == set(bots) and all(c["actor_name"] == bots[c["actor_id"]] for c in lines)  # the bot's own name, not an id
    assert all((c["provider_id"], c["model_id"]) == ("fake", "scripted") or c["provider_id"] for c in lines)
    await orch.shutdown()


@async_test
async def test_every_job_has_a_log_with_what_was_done_and_no_prompt_and_no_key(monkeypatch: pytest.MonkeyPatch, isolated_external_storage: Path) -> None:
    secret = "sk-learning-test-SECRET-1234567890"
    orch = table(monkeypatch)
    orch.provider_store = FakeProviders(PROVIDER.model_copy(update={"api_key": secret}))
    bots = orch._bots().lineup()
    use(orch, ScriptedLlm(good_init, fail_for=(bots[1],), fail_text=f"gateway said no ({secret})"))
    started = await orch.start_init()
    await settled(orch)
    log = isolated_external_storage / "logs" / "learning" / f"{started['job_id']}.jsonl"
    assert log.is_file()
    text = log.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines()]
    kinds = [row["event"] for row in rows]
    assert kinds[0] == "job" and kinds[-1] == "end" and kinds.count("result") == 4 and "llm_call" in kinds and "tool_call" in kinds
    assert rows[0]["ids"] == bots and rows[0]["scenario_id"] == "minimal_test" and rows[-1]["bots"][bots[1]] == "failed"
    assert {row["agent_id"] for row in rows if row["event"] == "result"} == set(bots)
    assert secret not in text and "sk-learning" not in text  # no key, in any form
    assert ARCH_NOTES not in text and "【初始化】" not in text and "现在开始初始化" not in text  # no memory text, no prompt
    assert all(set(row) >= {"ts", "event", "agent_id"} for row in rows)
    await orch.shutdown()


@async_test
async def test_shutting_down_cancels_what_is_running(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = table(monkeypatch)
    ids = orch._bots().lineup()
    llm = use(orch, ScriptedLlm(good_init, gate=asyncio.Event()))
    await orch.start_init()
    await wait_for(lambda: llm.in_flight == 4)
    await orch.shutdown()
    assert llm.in_flight == 0 and orch.init_running_ids() == set()
    assert not [t for t in asyncio.all_tasks() if (t.get_name() or "").startswith(("init-", "review-"))]
    assert {orch._bots().summary(i)["init"]["status"] for i in ids} == {"uninitialized"}  # (a cancelled run writes nothing)


# =========================================================================================================== review
async def finished_game(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Any:
    orch = make_table_orch(monkeypatch, **kwargs)
    play_with(orch, moderator=end_at_once)
    await orch.start_or_resume()
    await wait_for(lambda: orch.session.status == "finished")
    return orch


def review_rows(orch: Any, **match: Any) -> list[dict[str, Any]]:
    rows = [e.data for e in events_of(orch, "review.progress")]
    return [r for r in rows if all(r[k] == v for k, v in match.items())]


@async_test
async def test_a_finished_game_is_reviewed_by_every_seat_at_once_each_with_its_own_model_and_once(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = await finished_game(monkeypatch)
    assert orch.review_view() == {"available": True, "done": [], "running": False, "supported": True, "deadline_s": 60, "seconds_left": None}
    gate = asyncio.Event()
    llm = use(orch, ScriptedLlm(by_kind(), gate=gate))
    result = await orch.start_review()
    assert result == {"started": IDS, "deadline_s": 60}
    await wait_for(lambda: llm.in_flight == 4)
    assert llm.peak == 4 and orch.review_running_ids() == set(IDS)
    view = orch.review_view()
    assert view["running"] is True and view["done"] == [] and view["seconds_left"] in range(55, 61) and view["supported"] is True
    assert orch.system_status()["code"] == "reviewing" and orch.session.status == "finished"
    running = review_rows(orch, state="running")[0]
    assert set(running) == {"id", "bot_id", "name", "state", "message", "seconds_left", "deadline_s", "changes"}
    assert running["id"] in IDS and running["bot_id"].startswith("b-") and running["deadline_s"] == 60 and running["changes"] is None
    gate.set()
    await wait_for(lambda: not orch.review_running_ids())
    await asyncio.sleep(0.02)
    done = review_rows(orch, state="done")
    assert sorted(row["id"] for row in done) == IDS
    assert all(row["changes"]["proposals"] == 0 and {c["file"] for c in row["changes"]["files"]} == {"people", "playbook"} for row in done)
    assert all(row["name"] == orch.session.participants[IDS.index(row["id"])].name and row["seconds_left"] is None for row in done)
    assert orch.review_view()["done"] == IDS and orch.review_view()["running"] is False and orch.system_status()["code"] == "finished"
    assert set(llm.counts) == set(IDS)  # the model of each seat was asked as the seat
    assert orch.usage.actor("N1").calls >= 3 and orch.learning_usage()["calls"] == 0  # a review is part of THAT game's usage, not of the learning use
    # every seat is reviewed once per game
    assert (await orch.start_review()) == {"started": [], "deadline_s": 60}
    assert (await orch.start_review(ids=["N2"])) == {"started": [], "deadline_s": 60}
    assert orch.session_payload()["review_done"] == IDS  # (and the save keeps it: a restart does not offer it again)
    await orch.shutdown()


@async_test
async def test_a_part_of_the_seats_can_be_reviewed_and_the_rest_later_and_an_unknown_seat_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = await finished_game(monkeypatch)
    llm = use(orch, ScriptedLlm(by_kind()))
    with pytest.raises(LearningRefused) as bad:
        await orch.start_review(ids=["N1", "N9"])
    assert bad.value.status == 422 and "无效" in str(bad.value)
    assert (await orch.start_review(ids=["N3", "N1"]))["started"] == ["N3", "N1"]
    await wait_for(lambda: not orch.review_running_ids())
    assert orch.review_view()["done"] == ["N3", "N1"] and set(llm.counts) == {"N1", "N3"}
    assert (await orch.start_review())["started"] == ["N2", "N4"]  # "all" means the ones that are not done yet
    await wait_for(lambda: not orch.review_running_ids())
    assert sorted(orch.review_view()["done"]) == IDS
    await orch.shutdown()


@async_test
async def test_a_review_is_only_for_a_finished_game_of_this_version(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    use(orch, ScriptedLlm(by_kind()))
    for status in ("idle", "running", "paused"):
        orch.session.status = status
        with pytest.raises(LearningRefused) as early:
            await orch.start_review()
        assert early.value.status == 409 and early.value.code == "game_not_finished" and "结束后" in str(early.value)
    orch.session.status = "finished"
    orch.session.schema_version = 5  # a game of v0.13: its seats have no bots
    assert orch.review_view()["supported"] is False and orch.review_view()["available"] is True
    with pytest.raises(LearningRefused) as old:
        await orch.start_review()
    assert old.value.status == 409 and old.value.code == "review_unsupported" and "旧版本" in str(old.value)
    await orch.shutdown()


@async_test
async def test_a_seat_without_a_model_fails_its_review_with_a_message_and_the_others_go_on(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = await finished_game(monkeypatch)
    llm = use(orch, ScriptedLlm(by_kind()))
    orch.session.model_bindings.pop("N3")
    await orch.start_review()
    await wait_for(lambda: not orch.review_running_ids())
    (error,) = review_rows(orch, state="error")
    assert error["id"] == "N3" and "没有可用的模型" in error["message"] and error["changes"] is None
    assert sorted(row["id"] for row in review_rows(orch, state="done")) == ["N1", "N2", "N4"] and "N3" not in llm.counts
    assert orch.review_view()["done"] == ["N1", "N2", "N4"]  # (N3 can be asked for again once it has a model)
    orch.session.model_bindings["N3"] = orch.session.model_bindings["N1"]
    assert (await orch.start_review())["started"] == ["N3"]
    await wait_for(lambda: not orch.review_running_ids())
    assert sorted(orch.review_view()["done"]) == IDS
    await orch.shutdown()


@async_test
async def test_a_review_that_runs_out_of_its_time_keeps_what_was_written_and_counts_as_done(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = await finished_game(monkeypatch)
    monkeypatch.setattr(jobs_module, "REVIEW_TIMEOUT_S", 2.0)

    class Slow(ScriptedLlm):
        async def complete(self, agent_id: str, messages: Any, tools: Any, *, max_tokens: int | None = None) -> Any:
            if agent_id == "N2" and self.counts.get(agent_id, 0) >= 2:
                self.counts[agent_id] += 1
                await asyncio.sleep(30)
            return await super().complete(agent_id, messages, tools, max_tokens=max_tokens)

    use(orch, Slow(by_kind()))
    await orch.start_review()
    await wait_for(lambda: not orch.review_running_ids())
    (slow,) = review_rows(orch, state="timeout")
    assert slow["id"] == "N2" and "时间到了" in slow["message"] and {c["file"] for c in slow["changes"]["files"]} == {"people", "playbook"}
    bot = orch.session.participants[1].bot_id
    assert "Bob" in orch._bots().read_part(bot, "people")  # what it wrote is its memory
    assert sorted(orch.review_view()["done"]) == IDS  # (in the order they were done: N2 was the last)
    await orch.shutdown()


@async_test
async def test_while_a_review_is_being_written_the_game_is_not_replaced_continued_or_loaded_and_no_init_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.game.learning_runtime import LearningBusyError

    orch = await finished_game(monkeypatch)
    gate = asyncio.Event()
    llm = use(orch, ScriptedLlm(by_kind(), gate=gate))
    saved = orch.session.session_id
    orch.game_store.save(saved, json.loads(json.dumps(orch.session_payload())))
    await orch.start_review()
    await wait_for(lambda: llm.in_flight == 4)
    for what, call in (
        ("new", lambda: orch.new_game()),
        ("continue", lambda: orch.start_or_resume()),
        ("load", lambda: orch.load_game("another")),
        ("import", lambda: orch.load_session_data(orch.session_payload())),
    ):
        with pytest.raises(LearningBusyError) as busy:
            await call()
        assert busy.value.status == 409 and "复盘" in str(busy.value), what
    with pytest.raises(LearningRefused) as init:
        await orch.start_init()
    assert init.value.code == "game_active" or init.value.code == "init_busy"
    assert orch.session.session_id == saved and orch.session.status == "finished"
    gate.set()
    await wait_for(lambda: not orch.review_running_ids())
    assert (await orch.new_game()).session_id != saved  # and afterwards it can
    await orch.shutdown()


@async_test
async def test_no_review_starts_while_an_initialization_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = await finished_game(monkeypatch)
    gate = asyncio.Event()
    use(orch, ScriptedLlm(by_kind(), gate=gate))
    await orch.start_init(rebuild=True)
    with pytest.raises(LearningRefused) as busy:
        await orch.start_review()
    assert busy.value.status == 409 and busy.value.code == "init_busy"
    gate.set()
    await settled(orch)
    assert (await orch.start_review())["started"] == IDS
    await wait_for(lambda: not orch.review_running_ids())
    await orch.shutdown()


@async_test
async def test_the_bot_of_the_seat_a_person_played_is_the_only_one_told_and_the_style_switch_is_per_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = make_table_orch(monkeypatch)
    play_with(orch, moderator=idle_moderator)
    await orch.takeover("N2")
    await orch.start_or_resume()
    await asyncio.sleep(0.05)
    orch.session.status = "running"
    await orch.pause("user")
    orch.world.state.status = "finished"  # (the game is over; the person never gave the seat back)
    orch.world.state.winner = "红队"
    orch.session.status = "finished"
    llm = use(orch, ScriptedLlm(by_kind()))
    await orch.start_review(learn_style={"N2": True, "N3": True})
    await wait_for(lambda: not orch.review_running_ids())
    first = {seat: next(m for who, m in llm.requests if who == seat)[1] for seat in IDS}
    assert "【有人代你出场】" in first["N2"]["content"] and all("【有人代你出场】" not in first[s]["content"] for s in ("N1", "N3", "N4"))
    systems = {seat: next(m for who, m in llm.requests if who == seat)[0]["content"] for seat in IDS}
    assert "学习那个人说话的方式" in systems["N2"] and all("学习那个人说话的方式" not in systems[s] for s in ("N1", "N3", "N4"))  # (N3's switch means nothing)
    await orch.shutdown()


# ===================================================================================================== the demo bots
@async_test
async def test_the_scripted_demo_bots_initialize_and_review_through_the_same_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Demo mode (R4): the demo model answers an initialization and a review with fixed tool calls - valid files, no model - and the whole chain
    (job, events, store, review, proposals) is the production one."""
    from app.game.demo import DemoLlm, install_demo

    orch = table(monkeypatch)
    install_demo(orch, delay=0.0, seed=3, prepare=False)
    assert set(statuses(orch).values()) == {"uninitialized"}
    started = await orch.start_init()
    await settled(orch)
    assert started["ids"] == orch._bots().lineup() and set(statuses(orch).values()) == {"ready"}
    assert {row["init"]["model"] for row in orch.bot_summaries()} == {"演示模式"}
    bot = orch._bots().lineup()[0]
    assert "来源:初始化" in orch._bots().read_part(bot, "playbook", "minimal_test") and "#发言" in orch._bots().read_part(bot, "playbook", "minimal_test")
    assert isinstance(orch.llm_port_factory(orch), DemoLlm)
    await orch.shutdown()


# ============================================================================================ a bot that is not there any more
@async_test
async def test_a_seat_whose_bot_is_gone_fails_its_review_with_a_message_and_the_others_go_on(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = await finished_game(monkeypatch)
    store = orch._bots()
    old = orch.session.participants[0].bot_id
    store.update_bot(old, name="Zed", confirm_reset=True)  # a bot that has learned is frozen: renaming it makes another bot (a new id)
    assert old not in store.bot_ids()
    use(orch, ScriptedLlm(by_kind()))
    await orch.start_review()
    await wait_for(lambda: not orch.review_running_ids())
    (error,) = review_rows(orch, state="error")
    assert error["id"] == "N1" and "已经不在了" in error["message"] and error["changes"] is None
    assert sorted(row["id"] for row in review_rows(orch, state="done")) == ["N2", "N3", "N4"]
    await orch.shutdown()


@async_test
async def test_a_bot_that_disappears_during_its_initialization_is_a_failure_with_a_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.storage.bots import BotNotFound

    orch = table(monkeypatch)
    bot = orch._bots().lineup()[0]

    async def gone(**kwargs: Any) -> Any:
        raise BotNotFound(f"没有这个玩家：{bot}")

    monkeypatch.setattr(jobs_module, "run_init", gone)
    use(orch, ScriptedLlm(good_init))
    await orch.start_init(ids=[bot])
    await settled(orch)
    (row,) = progress(orch, bot=bot, state="failed")
    assert "已经不在了" in row["error"] and orch.init_running_ids() == set()
    await orch.shutdown()
