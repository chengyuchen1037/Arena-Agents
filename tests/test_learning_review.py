"""The review of ONE bot after a game (v0.14, H31-H34; contract §13.6): ``app.learning.review_runner`` over a real bot store, a real finished world and the
real learning brain, with a scripted model.

What is tested: every write lands in the bot's memory at once and stays when the clock runs out (C16), a change of the L0 is only a proposal (H34), the game
is counted in the record (a win, a loss, or nothing when nobody knows), the facts the engine tells the bot (result, identity, everybody's identities, its
own ``jot`` notes), and the note about a person who played its seat - for THAT bot only.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.agents.context import Namer
from app.bots.types import JOT_NOTE_TITLE
from app.game.session import HumanSpan
from app.learning.review_runner import (
    build_review_job,
    human_note,
    jots_of,
    others_text,
    outcome_text,
    role_text,
    run_review,
)
from app.runtime.clock import GameClock
from app.world.types import ENGINE_ID, MODERATOR_ID
from app.world.world import World
from game_fakes import async_test
from learning_fakes import PEOPLE, REVIEW_PLAYBOOK, Bench, Call, ScriptedLlm, good_review

SEATS = ["N1", "N2", "N3", "N4"]


@pytest.fixture()
def bench(tmp_path: Path) -> Bench:
    return Bench(tmp_path / "data")


class Game:
    """A finished game of the small scenario for the four bots of the bench: ``world`` (dealt, with a few records and a winner), who sat where."""

    def __init__(self, bench: Bench, winner: str = "红队") -> None:
        self.bench = bench
        names = [bench.store.name_of(bot) for bot in bench.ids]
        players = [{"id": seat, "seat": str(i), "name": name} for i, (seat, name) in enumerate(zip(SEATS, names), start=1)]
        self.world = World.new_game(bench.spec, players, GameClock(), seed=5)
        self.bot_of = dict(zip(SEATS, bench.ids))
        self.name_of = dict(zip(SEATS, names))
        world = self.world
        world.commit("channel.created", ENGINE_ID, {"channel_id": "public", "name": "公共频道", "type": "public", "members": SEATS}, visible_to="*", channel_id="public")
        self.say("N1", "我先说一句：今天的票我看不准。")
        self.say("N2", "我是真人在说话，我觉得 N3 可疑。")
        self.say("N3", "我不同意，N2 说得太快了。")
        self.say("N4", "再听一轮。")
        world.state.winner = winner
        world.state.status = "finished"
        self.last_seq = world.seq

    def say(self, seat: str, text: str) -> int:
        entry = self.world.commit(
            "message", seat, {"content": text, "kind": "speech"}, visible_to="*", channel_id="public"
        )
        return entry.seq

    def faction(self, seat: str) -> str:
        return str(self.world.player(seat).faction)

    async def review(self, seat: str, llm: Any, **kwargs: Any) -> Any:
        params: dict[str, Any] = dict(
            store=self.bench.store, bot_id=self.bot_of[seat], seat_id=seat, session_id="game-1", scenario_id=self.bench.scenario_id,
            world=self.world, spec=self.bench.spec, contract=self.bench.contract, materials={}, spans=[], learn_style=False, llm=llm,
            agent_id=seat, deadline_s=5,
        )
        params.update(kwargs)
        return await run_review(**params)


@pytest.fixture()
def game(bench: Bench) -> Game:
    for bot in bench.ids:
        bench.ready(bot)
    return Game(bench)


# ---------------------------------------------------------------------------------------------------------------- the truth
def test_the_engine_tells_the_bot_how_the_game_ended_who_it_was_and_who_everybody_was(game: Game) -> None:
    spec, world = game.bench.spec, game.world
    for seat in SEATS:
        faction = game.faction(seat)
        won = faction == "red"
        assert outcome_text(spec, world, seat) == ("你所在的阵营获胜了。" if won else "你所在的阵营失败了。")
        assert role_text(spec, world, seat) == f"{'红队' if won else '蓝队'}（{'红队' if won else '蓝队'}）"
    lines = others_text(spec, world).splitlines()
    assert len(lines) == 4
    for seat, line in zip(SEATS, lines):
        won = game.faction(seat) == "red"
        assert line == f"- {seat} {game.name_of[seat]}：{'红队' if won else '蓝队'}（{'红队' if won else '蓝队'}）· {'获胜' if won else '失败'}"
    world.state.winner = "没人赢"  # an outcome nobody can match to a faction
    assert outcome_text(spec, world, "N1") == "这局的结果是：没人赢。" and all("结果不明" in line for line in others_text(spec, world).splitlines())


def test_what_the_bot_noted_with_jot_is_read_from_the_world(game: Game) -> None:
    assert jots_of(game.world, "N1") == ()
    game.world.write_note("N1", JOT_NOTE_TITLE, "N3 说话和投票对不上", "append")
    game.world.write_note("N1", JOT_NOTE_TITLE, "N2 抢先表态", "append")
    assert jots_of(game.world, "N1") == ("N3 说话和投票对不上", "N2 抢先表态") and jots_of(game.world, "N2") == ()


def namer_for(game: Game) -> Namer:
    return Namer(game.world.display_name, {"public": "公共频道"})


def test_only_the_bot_of_a_seat_a_person_played_is_told_and_what_the_person_did(game: Game) -> None:
    world = game.world
    spans = [HumanSpan(seat="N2", from_seq=1, to_seq=None)]
    note = human_note(world, spans, "N2", namer_for(game))
    assert "人类玩家代你出场" in note and "记录 #1 到 #" in note and "我是真人在说话，我觉得 N3 可疑。" in note and "他做了 1 件事" in note
    for seat in ("N1", "N3", "N4"):
        assert human_note(world, spans, seat, namer_for(game)) == ""  # nobody else is told who the person was
    # a stretch in which the person did nothing is said too; nobody played: nothing
    quiet = human_note(world, [HumanSpan(seat="N4", from_seq=game.last_seq, to_seq=game.last_seq)], "N4", namer_for(game))
    assert "没有以你的名义发过言或作答" in quiet
    assert human_note(world, [], "N2", namer_for(game)) == ""
    # a closed stretch ends where it ended
    first = game.world.ledger[2].seq
    closed = human_note(world, [HumanSpan(seat="N1", from_seq=0, to_seq=first)], "N1", namer_for(game))
    assert f"#0 到 #{first}" in closed


def test_the_job_carries_the_style_switch_only_when_there_is_a_person_to_learn_from(game: Game) -> None:
    common: dict[str, Any] = dict(
        spec=game.bench.spec, scenario_id=game.bench.scenario_id, contract=game.bench.contract, world=game.world, namer=namer_for(game), deadline_s=60,
    )
    played = build_review_job(
        mind=game.bench.store.load_mind(game.bot_of["N2"], game.bench.scenario_id), seat_id="N2", spans=[HumanSpan(seat="N2", from_seq=1)],
        learn_style=True, **common,
    )
    assert played.human_note and played.learn_style is True and played.kind == "review" and played.deadline_s == 60
    other = build_review_job(
        mind=game.bench.store.load_mind(game.bot_of["N3"], game.bench.scenario_id), seat_id="N3", spans=[HumanSpan(seat="N2", from_seq=1)],
        learn_style=True, **common,
    )
    assert other.human_note == "" and other.learn_style is False  # (the switch means nothing for a bot nobody played)
    assert other.outcome_text and other.role_text and other.others_text and other.seat_id == "N3"


# ------------------------------------------------------------------------------------------------------------------ the run
@async_test
async def test_a_good_review_writes_at_once_counts_the_game_and_reports_what_changed(game: Game) -> None:
    bench = game.bench
    seat = "N1"
    bot = game.bot_of[seat]
    game.world.write_note(seat, JOT_NOTE_TITLE, "N3 说话和投票对不上", "append")
    before = bench.memory(bot)
    llm = ScriptedLlm(good_review)
    outcome = await game.review(seat, llm)
    assert outcome.state == "done" and "复盘完成" in outcome.message and outcome.rounds == 3
    after = bench.memory(bot)
    assert after["people"].strip() == PEOPLE.strip() and after["people"] != before["people"]
    assert all(line in after["playbook"] for line in REVIEW_PLAYBOOK.splitlines())
    assert {change["file"] for change in outcome.changes["files"]} == {"people", "playbook"} and outcome.changes["proposals"] == 0
    people = next(change for change in outcome.changes["files"] if change["file"] == "people")
    assert people["before"] == "" and people["after"].strip() == PEOPLE.strip()
    won = game.faction(seat) == "red"
    assert f"场次 1 · 胜 {1 if won else 0}" in bench.store.summary(bot, sources=bench.hashes)["memory_stats"]["record"]
    first = llm.requests[0][1][1]["content"]
    assert "【结果】" in first and "【你的身份】" in first and "【大家的身份与结果】" in first and "N3 说话和投票对不上" in first
    assert "有人代你出场" not in first  # (nobody played this seat)
    assert bench.status(bot) == "ready"  # a review is not an initialization: the bot stays what it was


@async_test
async def test_what_was_written_before_the_clock_ran_out_stays_and_the_game_is_counted(game: Game) -> None:
    bench = game.bench
    seat = "N2"
    bot = game.bot_of[seat]

    class Hangs(ScriptedLlm):
        async def complete(self, agent_id: str, messages: Any, tools: Any, *, max_tokens: int | None = None) -> Any:
            if self.counts.get(agent_id, 0) >= 2:
                self.counts[agent_id] += 1
                await asyncio.sleep(30)
            return await super().complete(agent_id, messages, tools, max_tokens=max_tokens)

    outcome = await game.review(seat, Hangs(good_review), deadline_s=1.5)
    assert outcome.state == "timeout" and "时间到了" in outcome.message and "保留" in outcome.message
    assert bench.memory(bot)["people"].strip() == PEOPLE.strip()  # written in round 2: it is there
    assert {change["file"] for change in outcome.changes["files"]} == {"people", "playbook"}
    assert "场次 1" in bench.store.summary(bot)["memory_stats"]["record"]  # (a review that ran out of time still counts the game)


@async_test
async def test_a_model_that_cannot_be_reached_changes_and_counts_nothing_so_the_review_can_be_asked_for_again(game: Game) -> None:
    bench = game.bench
    seat = "N3"
    bot = game.bot_of[seat]
    before = bench.memory(bot)
    outcome = await game.review(seat, ScriptedLlm(good_review, fail_for=(seat,)))
    assert outcome.state == "error" and "调用模型失败" in outcome.message and "没有记入" in outcome.message
    assert bench.memory(bot) == before and outcome.changes == {"files": [], "proposals": 0}
    assert "场次" not in bench.store.summary(bot)["memory_stats"]["record"]
    again = await game.review(seat, ScriptedLlm(good_review))
    assert again.state == "done" and "场次 1" in bench.store.summary(bot)["memory_stats"]["record"]


@async_test
async def test_the_record_counts_a_win_a_loss_and_nothing_when_nobody_knows_who_won(game: Game) -> None:
    bench = game.bench
    winners = {seat for seat in SEATS if game.faction(seat) == "red"}
    for seat in SEATS:
        await game.review(seat, ScriptedLlm(lambda call: [("finish", {})]))
    for seat in SEATS:
        record = bench.store.summary(game.bot_of[seat])["memory_stats"]["record"]
        assert f"场次 1 · 胜 {1 if seat in winners else 0}" in record, (seat, record)
    game.world.state.winner = "没人赢"
    seat = "N1"
    await game.review(seat, ScriptedLlm(lambda call: [("finish", {})]))
    assert "场次 1" in bench.store.summary(game.bot_of[seat])["memory_stats"]["record"]  # (an outcome nobody knows leaves the record alone)


@async_test
async def test_a_change_of_the_l0_is_only_a_proposal_for_the_user(game: Game) -> None:
    bench = game.bench
    seat = "N2"
    bot = game.bot_of[seat]
    core_before = bench.store.core(bot)
    voice = core_before.voice.strip() + "\n（学到的：句子更短。）"
    llm = ScriptedLlm(
        lambda call: [("propose_core", {"section": "voice", "content": voice, "reason": "那段里的人说话更干脆"})] if call.index == 0 else [("finish", {})]
    )
    outcome = await game.review(seat, llm, spans=[HumanSpan(seat="N2", from_seq=1)], learn_style=True)
    assert outcome.state == "done" and outcome.changes["proposals"] == 1 and outcome.changes["files"] == []
    assert bench.store.core(bot) == core_before  # never applied: the user decides
    (proposal,) = bench.store.staging(bot)
    assert proposal["section"] == "voice" and proposal["after"].strip() == voice.strip() and "那段里的人" in proposal["reason"]
    assert proposal["source"] == "复盘 game-1"
    system, first = llm.requests[0][1][0]["content"], llm.requests[0][1][1]["content"]
    assert "学习那个人说话的方式" in system and "【有人代你出场】" in first  # the person's stretch is in its message, and it was invited to learn from it
    refused = await game.review("N3", ScriptedLlm(lambda call: [("propose_core", {"section": "voice", "content": "字" * 700, "reason": "x"})] if call.index == 0 else [("finish", {})]))
    assert refused.changes["proposals"] == 0 and bench.store.staging(game.bot_of["N3"]) == []  # (too long for the section: refused with the limit)


@async_test
async def test_the_record_can_be_read_in_pages_and_searched(game: Game) -> None:
    import re

    pages: list[str] = []

    def script(call: Call) -> Any:
        if call.index == 0:
            return [
                ("read_record", {"limit": 2}),
                ("read_record", {"query": "不同意"}),
                ("read_record", {"query": "没有这个词"}),
                ("read_source", {"source": "my_memory", "part": "people"}),
            ]
        if call.index == 1:
            pages.extend(call.last_tool_results)
            follow = re.search(r"start=(\d+)", pages[0])
            assert follow is not None  # (the first page says how to go on)
            return [("read_record", {"start": int(follow.group(1)), "limit": 20})]
        pages.extend(call.last_tool_results)
        return [("finish", {})]

    await game.review("N1", ScriptedLlm(script))
    first, found, nothing, people, rest = pages
    assert "共" in first and "后面还有" in first and "我先说一句" not in first  # (two records, and how to go on)
    assert "不同意" in found and "我先说一句" not in found
    assert "没有符合的记录" in nothing
    assert "（这一项是空的。）" in people
    assert "我先说一句" in rest and "再听一轮" in rest and "后面还有" not in rest  # (the rest of it, from where the first page stopped)


@async_test
async def test_a_review_of_a_game_without_a_record_for_the_bot_still_works_and_the_log_has_no_prompt(game: Game) -> None:
    class Sink:
        def __init__(self) -> None:
            self.lines: list[Any] = []

        def write(self, event: str, agent_id: str, **fields: Any) -> None:
            self.lines.append((event, agent_id, fields))

    sink = Sink()
    outcome = await game.review("N4", ScriptedLlm(good_review), trace=sink)
    assert outcome.state == "done"
    dumped = json.dumps(sink.lines, ensure_ascii=False)
    assert "【大家的身份与结果】" not in dumped and PEOPLE not in dumped and "先把别人说过的话" not in dumped  # no prompt, no memory text
    (write,) = [fields for event, _, fields in sink.lines if event == "tool_call" and fields["name"] == "write_memory" and fields["args"]["file"] == "playbook"]
    assert write["args"]["content"].startswith("（") and write["args"]["content"].endswith(" 字）")  # (what was written: its length only)
    assert [event for event, _, _ in sink.lines].count("llm_call") == 3
