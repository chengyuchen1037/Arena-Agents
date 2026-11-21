"""``bench.runner``: bot × situation × sample on the REAL ``Agent`` / worker loop, with a fake model (0 calls to a real one).

What these tests are about is H18 and what the benchmark must be able to see: the speech of a bot comes from the model's own ``speak`` call - sent back
(``FORMAT`` / ``TOO_LONG``) and rewritten by the bot itself, never written, cut or asked for by the runner - and every limit (rounds, time, budget) stops
a sample without a model call that was not counted."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from bench_helpers import ROOT, bots, example, examples, one, run_fake, scene_of, spoken_by

from app.llm.types import LLMResponse
from bench.fakes import LIST_TEXT, FakeLlm, Plan, listy, prose, scripted
from bench.play import build_scene
from bench.runner import (
    BLIND_SUBJECT,
    Budget,
    Job,
    RunConfig,
    SampleTrace,
    attempts_of,
    blind_id,
    blind_view,
    bots_of,
    decision_of,
    extract_calls,
    judge_items,
    jobs_for,
    read_results,
    run_info,
    run_sample,
    run_suite,
    score_of,
    speech_metrics,
)
from bench.situation import load_situation, parse_situation

LEGACY, ALICE = "legacy", "bot:Alice"
PINNED = ROOT / "bench" / "fixtures" / "situations" / "_example_pinned.yaml"


# ------------------------------------------------------------------------------------------------ the real loop, both brains


def test_both_brains_run_through_the_real_agent_loop_on_both_examples(tmp_path: Path) -> None:
    report, seen = run_fake(examples(), bots(LEGACY, ALICE), "prose", tmp_path=tmp_path)
    assert len(report.rows) == 4 and report.by_status() == {"ok": 4} and report.skipped == 0
    for row in report.rows:
        assert row["format"] == "arena-bench-result" and row["status"] == "ok" and row["stop"] == "decision" and row["rounds"] == 1
        assert row["mode"] == "fake" and row["model"] == "fake"  # (the mark that says this row measured nothing about a model)
        assert row["scene"] and row["seat"] == "N3" and row["scenario"] == "minimal_test"
        assert row["name"] == ("Alice" if row["bot"] == ALICE else "Carol")  # (a bot gives its name to the seat; the v0.13.1 player keeps the script's)
    vote, speak = one(report, LEGACY, "vote"), one(report, LEGACY, "speak")
    assert vote["decision"]["tool"] == "respond" and vote["decision"]["request_id"] == "r1" and vote["decision"]["value"] == "N1"
    assert speak["decision"]["tool"] == "speak" and speak["decision"]["channel_id"] == "public" and speak["decision"]["text"]
    # the real Agent opened a conversation with the brain's FULL message: the two brains showed the model different things
    assert one(report, LEGACY, "speak")["prompt"]["system_sha"] != one(report, ALICE, "speak")["prompt"]["system_sha"]
    assert "list_documents" in one(report, LEGACY, "speak")["prompt"]["tools"] and "jot" in one(report, ALICE, "speak")["prompt"]["tools"]
    assert "list_documents" not in one(report, ALICE, "speak")["prompt"]["tools"] and "jot" not in one(report, LEGACY, "speak")["prompt"]["tools"]
    # a bot's brain has the trace hooks (the engine writes them into the trace): the v0.13.1 player has none
    bot_row, legacy_row = one(report, ALICE, "speak"), one(report, LEGACY, "speak")
    assert len(bot_row["mounts"]) >= 12 and {"core.identity", "core.voice", "anchor", "l3.board"} <= {m["id"] for m in bot_row["mounts"]}
    assert bot_row["hints"] and bot_row["hints"][0]["kind"] == "speak" and legacy_row["mounts"] == [] and legacy_row["hints"] == []
    assert len(seen) == 4


def test_the_rows_name_the_run_the_bot_and_the_situation_and_the_files_hold_them(tmp_path: Path) -> None:
    report, _ = run_fake(examples(), bots(LEGACY, ALICE), "prose", tmp_path=tmp_path, run_id="r-1")
    row = one(report, ALICE, "vote")
    assert (row["run"], row["bot"], row["brain"], row["situation"], row["sample"], row["seed"]) == ("r-1", "bot:Alice", "BotBrain", "example-vote", 0, 7)
    assert row["tags"] == ["example", "L1"] and row["meta"] == {"pair": "example", "variant": "A"}
    assert one(report, LEGACY, "vote")["brain"] == "LegacyBrain"
    written = [json.loads(line) for line in (tmp_path / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert written == json.loads(json.dumps(report.rows, ensure_ascii=False, default=str))
    items = [json.loads(line) for line in (tmp_path / "judge_items.jsonl").read_text(encoding="utf-8").splitlines()]
    assert items == json.loads(json.dumps(report.items, ensure_ascii=False, default=str)) and len(items) == 3  # (each example asks for one rubric; legacy has no voice: no S2 item for it)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["judge_items.jsonl", "results.jsonl"]  # (nothing else is made: no temporary directory)


def test_the_same_run_twice_is_the_same_world_and_the_same_decisions() -> None:
    first, _ = run_fake(examples(), bots(LEGACY, ALICE), "prose")
    second, _ = run_fake(examples(), bots(LEGACY, ALICE), "prose")
    key = lambda r: (r["situation"], r["bot"], r["scene"], json.dumps(r["decision"], sort_keys=True), r["prompt"]["first_sha"])  # noqa: E731
    assert sorted(map(key, first.rows)) == sorted(map(key, second.rows))


def test_samples_are_numbered_and_the_fake_model_varies_with_them() -> None:
    report, _ = run_fake([example("speak")], bots(ALICE), "mixed", samples=4)
    assert sorted(r["sample"] for r in report.rows) == [0, 1, 2, 3]
    attempts = {r["sample"]: [(a["tool"], a["code"]) for a in r["attempts"]] for r in report.rows}
    assert attempts[0] == [("speak", None)] and attempts[1] == [("speak", "FORMAT"), ("speak", None)]  # (prose, then a list the gate sends back)
    assert attempts[2] == [("speak", "TOO_LONG"), ("speak", None)] and attempts[3] == [("speak", "FORMAT"), ("speak", None)]


# ------------------------------------------------------------------------------------------------ H15 / H16 / H18: refused, rewritten, never touched


def test_a_report_is_sent_back_to_a_bot_but_not_to_the_v0_13_1_player() -> None:
    report, seen = run_fake([example("speak")], bots(LEGACY, ALICE), "listy")
    bot, legacy = one(report, ALICE, "speak"), one(report, LEGACY, "speak")
    assert [(a["ok"], a["code"]) for a in bot["attempts"]] == [(False, "FORMAT"), (True, None)] and bot["refusals"] == {"FORMAT": 1}
    assert bot["attempts"][0]["rules"] == ["list"] and bot["attempts"][0]["text"] == LIST_TEXT and "没有发出，发言权还在" in bot["attempts"][0]["message"]
    assert bot["decision"]["attempt"] == 1 and bot["decision"]["rewrote"] is True and bot["decision"]["text"] != LIST_TEXT and bot["rounds"] == 2
    assert [(a["ok"], a["code"]) for a in legacy["attempts"]] == [(True, None)] and legacy["refusals"] == {}
    assert legacy["decision"]["text"] == LIST_TEXT and legacy["decision"]["attempt"] == 0 and legacy["speech"]["problems"] == ["list"]
    assert bot["speech"]["problems"] == []


def test_the_bot_wrote_its_own_second_text_and_the_ledger_holds_only_what_the_model_wrote() -> None:
    """H18: the seat under test has exactly one message in the world, it is the text of the model's accepted ``speak`` call, byte for byte (the commit gate
    trims the whitespace around a speech; nothing else happens to it) - the refused first attempt is nowhere."""
    for label in (LEGACY, ALICE):
        report, seen = run_fake([example("speak")], bots(label), "listy")
        row, scene = one(report, label, "speak"), scene_of(seen, label, "speak")
        calls = [c for c in row["tool_calls"] if c["name"] == "speak"]
        accepted = [c for c in calls if c["ok"]]
        assert len(accepted) == 1 and spoken_by(scene) == [accepted[0]["args"]["content"].strip()] == [row["decision"]["text"]]
        assert row["integrity"] == {"speech_verbatim": True}
        if label == ALICE:
            assert LIST_TEXT not in spoken_by(scene) and len(calls) == 2  # the refused one never reached the channel


def test_a_text_that_is_not_spoken_by_the_model_does_not_exist(tmp_path: Path) -> None:
    """A model that answers with plain text and no tool call has said nothing (its text is a monologue): there is no speech, no decision, and the
    runner does not make one up from the text."""
    only_text = lambda ctx: Plan(content="我想说：大家好，我是新来的。")  # noqa: E731
    report, seen = run_fake([example("speak")], bots(ALICE), only_text, tmp_path=tmp_path)
    row = one(report, ALICE, "speak")
    # (the brain nudges once - "your plain text goes nowhere, call speak or sleep" - and the model answers with plain text again: the run ends)
    assert row["status"] == "no_decision" and row["stop"] == "no_tool" and row["decision"] is None and row["attempts"] == [] and row["rounds"] == 2
    assert row["monologues"] == ["我想说：大家好，我是新来的。"] * 2
    assert spoken_by(scene_of(seen, ALICE, "speak")) == []  # (the seat has said nothing in the world)


def test_a_speech_over_the_limit_is_sent_back_by_the_world_for_both_brains_and_rewritten() -> None:
    report, seen = run_fake([example("speak")], bots(LEGACY, ALICE), "long")
    for label in (LEGACY, ALICE):
        row = one(report, label, "speak")
        assert [(a["ok"], a["code"]) for a in row["attempts"]] == [(False, "TOO_LONG"), (True, None)] and row["refusals"] == {"TOO_LONG": 1}
        first = row["attempts"][0]
        assert first["chars"] > 120 and "超过了单条上限 120 字" in first["message"] and row["decision"]["rewrote"] is True
        assert row["decision"]["text"] != first["text"] and len(row["decision"]["text"]) <= 120  # the second text is the bot's own
        assert spoken_by(scene_of(seen, label, "speak")) == [row["decision"]["text"]]  # (and the long one was not cut into the channel)
        runtime = row["runtime"]  # (R, counted from the ledger and the trace of the sample: not assumed)
        assert (runtime["agent_speeches"], runtime["truncated_speeches"], runtime["truncation_rate"]) == (1, 0, 0.0)
        assert runtime["speak"] == {"attempts": 2, "accepted": 1, "too_long": 1, "format": 0, "no_floor": 0, "rewrite_success_rate": 1.0}
        assert runtime["floor_ends"] == {"used": 1} and runtime["floor_timeout_rate"] == 0.0 and runtime["tokens"]["calls"] == 2
        tool_result = next(c for c in row["tool_calls"] if c["name"] == "speak")["result"]
        assert '"code":"TOO_LONG"' in tool_result and "truncated" not in tool_result


def test_a_bot_that_does_not_act_makes_no_decision_and_writes_nothing() -> None:
    report, seen = run_fake(examples(), bots(LEGACY, ALICE), "sleepy")
    assert report.by_status() == {"no_decision": 4}
    for row in report.rows:
        assert row["stop"] == "slept" and row["decision"] is None and row["attempts"] == [] and row["correct"] is None and row["score"] is None
    for label in (LEGACY, ALICE):
        assert spoken_by(scene_of(seen, label, "speak")) == []  # (the situation's own speeches are N1's and N2's)


def test_a_seat_without_a_wake_up_reason_is_an_error_row_and_the_rest_goes_on(tmp_path: Path) -> None:
    broken = parse_situation({
        "format": "arena-situation", "version": 1, "id": "broken", "scenario": "minimal_test", "players": {"count": 4}, "roles": ["red", "red", "blue", "blue"],
        "steps": [{"as": "moderator", "tool": "announce", "args": {"channel_id": "nowhere", "template_id": "x"}}], "decision": {"seat": "N3"},
    })
    report, _ = run_fake([broken, example("vote")], bots(LEGACY), "prose", tmp_path=tmp_path)
    by_situation = {r["situation"]: r for r in report.rows}
    assert by_situation["broken"]["status"] == "error" and by_situation["broken"]["stop"] == "scene" and "UNKNOWN_CHANNEL" in by_situation["broken"]["error"]
    assert by_situation["example-vote"]["status"] == "ok"
    assert [r["status"] for r in read_results(tmp_path / "results.jsonl")] == ["error", "ok"] or len(read_results(tmp_path / "results.jsonl")) == 2
    assert report.scene_errors == 1 and (tmp_path / "judge_items.jsonl").read_text(encoding="utf-8").count("\n") == 1  # (no items for a row without a decision)


def test_a_bot_that_cannot_be_made_fails_its_own_samples_only() -> None:
    good, bad = bots(LEGACY)[0], bots(LEGACY)[0]

    def explode(seat: str, world: Any, spec: Any) -> Any:
        raise RuntimeError("底层设定读不出来")

    from dataclasses import replace

    bad = replace(bad, label="bot:broken", factory=explode)
    report, _ = run_fake([example("vote")], [bad, good], "prose")
    broken = one(report, "bot:broken", "vote")
    assert broken["status"] == "error" and broken["stop"] == "bot" and "底层设定读不出来" in broken["error"]
    assert one(report, LEGACY, "vote")["status"] == "ok"


# ------------------------------------------------------------------------------------------------ limits: rounds, time, budget, failures


def test_the_budget_is_a_hard_stop_and_a_call_it_did_not_allow_is_never_made() -> None:
    budget = Budget(1)
    seen_llms: list[FakeLlm] = []

    def factory(job: Job, scene: Any) -> FakeLlm:
        llm = FakeLlm(listy, world=scene.world, seat=scene.seat, bot=job.bot.label, situation=job.situation.id)
        seen_llms.append(llm)
        return llm

    report = asyncio.run(run_suite([example("speak")], bots(ALICE), RunConfig(run_id="b"), factory, budget=budget))
    row = report.rows[0]
    assert row["status"] == "incomplete" and row["stop"] == "budget" and row["decision"] is None and row["rounds"] == 1
    assert budget.calls == 1 and report.calls == 1 and len(seen_llms[0].calls) == 1  # the bot's rewrite would have been the second call: it was not made
    assert [(a["ok"], a["code"]) for a in row["attempts"]] == [(False, "FORMAT")]


def test_a_budget_sets_a_call_aside_for_every_sample_it_admits() -> None:
    budget = Budget(2)
    first, second = budget.reserve(), budget.reserve()
    assert first is not None and second is not None and budget.reserve() is None and budget.exhausted and budget.take() is False  # (reservations count against the cap)
    first.use()
    assert (budget.calls, budget.reserved) == (1, 1) and budget.take() is False  # (the second reservation is still the second sample's)
    first.use()
    first.release()  # (nothing happens twice)
    assert (budget.calls, budget.reserved) == (1, 1)
    second.release()
    second.release()
    assert (budget.calls, budget.reserved) == (1, 0) and not budget.exhausted and budget.take() is True and budget.exhausted and budget.calls == 2
    unlimited = Budget(None)
    reservation = unlimited.reserve()
    assert reservation is not None and not unlimited.exhausted and unlimited.take() is True
    reservation.use()
    assert unlimited.calls == 2 and Budget(0).reserve() is None and Budget(0).take() is False


def test_samples_that_start_side_by_side_cannot_all_start_on_the_last_calls() -> None:
    """With four samples running at once and calls for two, two are admitted and made; the other three are not started - none starts, builds its world and
    then stops at the gate."""
    report, seen = run_fake([example("vote")], bots(LEGACY), "prose", samples=5, max_calls=2, concurrency=4)
    assert (len(report.rows), report.skipped, report.calls, len(seen)) == (2, 3, 2, 2) and report.by_status() == {"ok": 2}
    wide, _ = run_fake(examples(), bots(LEGACY, ALICE), "prose", samples=3, max_calls=5, concurrency=12)  # (twelve jobs, calls for five)
    assert (len(wide.rows), wide.skipped, wide.calls) == (5, 7, 5) and wide.by_status() == {"ok": 5}


def test_a_sample_that_makes_no_call_gives_its_call_back() -> None:
    broken = parse_situation({
        "format": "arena-situation", "version": 1, "id": "broken", "scenario": "minimal_test", "players": {"count": 4}, "roles": ["red", "red", "blue", "blue"],
        "steps": [{"as": "moderator", "tool": "announce", "args": {"channel_id": "nowhere", "template_id": "x"}}], "decision": {"seat": "N3"},
    })
    report, _ = run_fake([broken, example("vote")], bots(LEGACY), "prose", max_calls=1)  # (the broken one never gets to call: the one call is for the next sample)
    assert {r["situation"]: r["status"] for r in report.rows} == {"broken": "error", "example-vote": "ok"} and report.calls == 1 and report.skipped == 0


def test_samples_the_budget_cannot_pay_for_are_not_started() -> None:
    report, seen = run_fake(examples(), bots(LEGACY, ALICE), "prose", samples=3, max_calls=5)
    assert report.calls == 5 and len(report.rows) == 5 and report.skipped == 7 and report.by_status() == {"ok": 5}
    assert len(seen) == 5  # (no world was built and no brain made for a sample that was not started)
    none, _ = run_fake(examples(), bots(LEGACY), "prose", max_calls=0)
    assert none.rows == [] and none.skipped == 2 and none.calls == 0


def test_max_rounds_stops_a_bot_that_keeps_busy_without_moving() -> None:
    busy = scripted(*[[("read_notes", {})]] * 20)
    report, seen = run_fake([example("speak")], bots(ALICE), busy, max_rounds=3)
    row = report.rows[0]
    assert row["status"] == "incomplete" and row["stop"] == "max_rounds" and row["rounds"] == 3 and len(seen[0][2].calls) == 3
    assert [c["name"] for c in row["tool_calls"]] == ["read_notes"] * 3


def test_a_model_that_hangs_is_cut_off_by_the_timeout_and_nothing_is_left_running() -> None:
    class Hanging:
        calls = 0

        async def complete(self, agent_id: str, messages: Any, tools: Any, *, max_tokens: int | None = None) -> LLMResponse:
            Hanging.calls += 1
            await asyncio.sleep(60)
            raise AssertionError("not reached")

    async def go() -> Any:
        job = Job(example("vote"), bots(LEGACY)[0], "N3", 0)
        row = await run_sample(job, RunConfig(timeout_s=0.3, run_id="h"), lambda j, s: Hanging(), Budget())
        await asyncio.sleep(0)
        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        return row, leftovers

    row, leftovers = asyncio.run(go())
    assert row["status"] == "incomplete" and row["stop"] == "timeout" and row["decision"] is None and Hanging.calls == 1 and leftovers == []


def test_a_model_that_fails_is_retried_by_the_agent_then_given_up_and_a_key_never_shows() -> None:
    secret = "sk-test-0123456789abcdef"

    class Failing:
        calls = 0

        async def complete(self, agent_id: str, messages: Any, tools: Any, *, max_tokens: int | None = None) -> LLMResponse:
            Failing.calls += 1
            raise RuntimeError(f"401 Unauthorized for key {secret}")

    async def go() -> dict[str, Any]:
        job = Job(example("vote"), bots(LEGACY)[0], "N3", 0)
        return await run_sample(job, RunConfig(agent_retries=1, run_id="f"), lambda j, s: Failing(), Budget(), secrets=(secret,))

    row = asyncio.run(go())
    assert row["status"] == "error" and row["stop"] == "error" and Failing.calls == 2  # the agent's own retry, then it gave up
    assert row["errors"] and "401 Unauthorized" in row["errors"][0]["message"] and "***" in row["errors"][0]["message"]
    assert secret not in json.dumps(row, ensure_ascii=False)


def test_resume_skips_what_is_done_and_redoes_what_failed(tmp_path: Path) -> None:
    first, _ = run_fake(examples(), bots(LEGACY), "prose", tmp_path=tmp_path, samples=2)
    assert len(first.rows) == 4
    # one sample "failed" in the first run: the file says error, a resumed run does it again
    rows = read_results(tmp_path / "results.jsonl")
    lines = (tmp_path / "results.jsonl").read_text(encoding="utf-8").splitlines()
    victim = json.loads(lines[0])
    victim.update(status="error", stop="error", decision=None)
    lines[0] = json.dumps(victim, ensure_ascii=False)
    (tmp_path / "results.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    seen_jobs: list[Job] = []

    def factory(job: Job, scene: Any) -> FakeLlm:
        seen_jobs.append(job)
        return FakeLlm(prose, world=scene.world, seat=scene.seat, bot=job.bot.label, situation=job.situation.id, sample=job.sample)

    second = asyncio.run(run_suite(examples(), bots(LEGACY), RunConfig(samples=2, run_id="t"), factory, out_dir=tmp_path, resume=True))
    assert [j.key for j in seen_jobs] == [(victim["situation"], victim["seat"], victim["bot"], victim["sample"])] and second.skipped == 3 and len(second.rows) == 1
    final = read_results(tmp_path / "results.jsonl")
    assert len(final) == 4 and {r["status"] for r in final} == {"ok"} and len(rows) == 4  # (the last row of a key wins: one row per sample)


def test_concurrency_does_not_change_what_a_sample_does() -> None:
    serial, _ = run_fake(examples(), bots(LEGACY, ALICE), "mixed", samples=4)
    parallel, _ = run_fake(examples(), bots(LEGACY, ALICE), "mixed", samples=4, concurrency=8)
    key = lambda r: (r["situation"], r["bot"], r["sample"], r["status"], json.dumps(r["decision"], sort_keys=True), [(a["tool"], a["code"]) for a in r["attempts"]])  # noqa: E731
    assert sorted(map(key, serial.rows), key=str) == sorted(map(key, parallel.rows), key=str)


def test_dumped_prompts_hold_the_whole_conversation_a_model_saw(tmp_path: Path) -> None:
    run_fake([example("speak")], bots(ALICE), "listy", tmp_path=tmp_path, dump_prompts=True)
    files = list((tmp_path / "prompts").glob("*.md"))
    assert len(files) == 1 and files[0].name == "example-speak__N3__bot-Alice__0.md"
    text = files[0].read_text(encoding="utf-8")
    assert text.startswith("## system") and "你是 Alice" in text and "【状态板】" in text and "## tool" in text and '"code":"FORMAT"' in text
    assert re.search(r"## assistant", text) and "【此刻的提醒】" in text


# ------------------------------------------------------------------------------------------------ pieces


def _trace(*rows: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"agent_id": "N3", "t": 0.0, **row} for row in rows]


def test_the_trace_is_read_into_calls_with_their_receipts_and_into_attempts() -> None:
    rows = _trace(
        {"event": "llm_call", "round": 0, "tool_calls": ["write_note", "speak"]},
        {"event": "tool_call", "name": "write_note", "args": {"title": "t", "content": "c"}, "result": json.dumps({"ok": True}), "ends_run": False},
        {"event": "tool_call", "name": "speak", "args": {"channel_id": "public", "content": "我先说"},
         "result": json.dumps({"ok": False, "code": "FORMAT", "message": "像报告", "data": {"rule": "list", "rules": ["list", "prefix"]}}), "ends_run": False},
        {"event": "receipt", "name": "speak", "ok": False, "code": "FORMAT", "seq": None, "message": "像报告"},
        {"event": "llm_call", "round": 1, "tool_calls": ["speak"]},
        {"event": "tool_call", "name": "speak", "args": {"channel_id": "public", "content": "好的，我说完整的话"}, "result": json.dumps({"ok": True, "seq": 9}), "ends_run": False},
        {"event": "receipt", "name": "speak", "ok": True, "code": None, "seq": 9, "message": "已发送"},
        {"event": "tool_call", "name": "pass", "args": {"channel_id": "public"}, "result": json.dumps({"ok": True, "seq": 10}), "ends_run": False},
        {"event": "receipt", "name": "pass", "ok": True, "code": None, "seq": 10, "message": ""},
    )
    calls = extract_calls(rows)
    assert [(c["i"], c["round"], c["name"], c["ok"], c["code"], c["receipt"]) for c in calls] == [
        (0, 0, "write_note", True, None, False), (1, 0, "speak", False, "FORMAT", True), (2, 1, "speak", True, None, True), (3, 1, "pass", True, None, True),
    ]
    attempts = attempts_of(calls)
    assert [(a["tool"], a["ok"], a["code"]) for a in attempts] == [("speak", False, "FORMAT"), ("speak", True, None), ("pass", True, None)]  # (a note is no attempt)
    assert attempts[0]["rules"] == ["list", "prefix"] and attempts[0]["chars"] == 3 and attempts[1]["text"] == "好的，我说完整的话"


def test_a_note_or_a_refused_move_is_not_a_decision_and_the_first_accepted_expected_move_is() -> None:
    scene = build_scene(example("speak"))
    rows = _trace(
        {"event": "tool_call", "name": "speak", "args": {"channel_id": "public", "content": "x" * 200}, "result": json.dumps({"ok": False, "code": "TOO_LONG"}), "ends_run": False},
        {"event": "receipt", "name": "speak", "ok": False, "code": "TOO_LONG", "seq": None, "message": "太长"},
        {"event": "tool_call", "name": "pass", "args": {"channel_id": "public"}, "result": json.dumps({"ok": True, "seq": 21}), "ends_run": False},
        {"event": "receipt", "name": "pass", "ok": True, "code": None, "seq": 21, "message": ""},
    )
    calls = extract_calls(rows)
    assert decision_of(calls, ("speak",), scene) is None  # (the one move that went through is a pass, which this situation does not wait for)
    decision = decision_of(calls, ("speak", "pass"), scene)
    assert decision is not None and decision["tool"] == "pass" and decision["attempt"] == 1 and decision["rewrote"] is True and decision["text"] is None


def test_a_choice_is_scored_against_the_script_and_a_label_stands_for_the_request() -> None:
    scene = build_scene(example("vote"))
    decision = {"tool": "respond", "request_id": "r1", "value": "N1"}
    assert score_of(decision, scene) == {"kind": "choice", "request": "r1", "value": "N1", "correct": True, "wrong": False}
    assert score_of({**decision, "value": "N4"}, scene)["correct"] is False
    assert score_of({**decision, "request_id": "r9"}, scene) is None and score_of(None, scene) is None
    assert score_of({"tool": "speak", "request_id": None, "value": None}, scene) is None


def test_the_s1_facts_of_a_speech_use_the_gate_and_the_bots_declared_band() -> None:
    bot = bots(ALICE)[0]
    assert speech_metrics(None, bot, "N3", "Alice") is None and speech_metrics("", bot, "N3", "Alice") is None
    listy = speech_metrics("我的看法：\n1. 一\n2. 二", bot, "N3", "Alice")
    assert listy["problems"] == ["list"] and listy["inline_enum"] is True and listy["paragraphs"] == 3 and listy["band"] == [40, 110] and listy["in_band"] is False
    prefixed = speech_metrics("N3：我先说两句，今天没有什么特别的怀疑，听听大家的。", bot, "N3", "Alice")
    assert prefixed["problems"] == ["prefix"]
    fine = speech_metrics("你急什么？这票你自己信吗？我只看他昨天到底投了谁，投了谁就是谁的事，别的先放一放，轮到我再说。", bot, "N3", "Alice")
    assert fine["problems"] == [] and fine["in_band"] is True and fine["chars"] > 40
    legacy = speech_metrics("hello", bots(LEGACY)[0], "N3", "Alice")
    assert legacy["band"] is None and legacy["in_band"] is None


def test_the_sample_trace_ends_on_the_first_accepted_move_of_an_expected_tool() -> None:
    async def go() -> list[str]:
        trace = SampleTrace(["speak"])
        trace.write("receipt", "N3", name="speak", ok=False, code="FORMAT")
        trace.write("receipt", "N3", name="respond", ok=True)
        stops = [str(trace.done.is_set())]
        trace.write("receipt", "N3", name="speak", ok=True)
        stops += [str(trace.done.is_set()), trace.stop]
        trace.write("run_end", "N3", reason="ends_run")  # (after the end: ignored)
        return stops + [trace.stop]

    assert asyncio.run(go()) == ["False", "True", "decision", "decision"]


def test_judge_items_carry_what_a_judge_may_see_and_nothing_of_the_brain() -> None:
    report, _ = run_fake(examples(), bots(LEGACY, ALICE), "listy")
    by_id = {item["item_id"]: item for item in report.items}
    assert set(by_id) == {"t/example-vote/N3/legacy/0/L1", "t/example-vote/N3/bot:Alice/0/L1", "t/example-speak/N3/bot:Alice/0/S2"}  # (legacy has no voice: no S2 item)
    s2 = by_id["t/example-speak/N3/bot:Alice/0/S2"]
    assert s2["format"] == "arena-judge-item" and s2["rubric"] == "S2" and s2["bot"] == "bot:Alice" and s2["situation"] == "example-speak"
    voice = bots(ALICE)[0].style_card
    assert s2["truth"]["bot"] == "bot:Alice" and s2["truth"]["cards"] == {"A": "bot:Alice"} and s2["context"]["cards"] == [{"id": "A", "text": voice}]
    assert "Alice" not in voice and "爱反问" in voice and "篇幅：" in voice and len(voice.splitlines()) == 3  # (the voice only - three lines - and no name)
    assert "truth" not in s2["context"]
    assert s2["subject"]["kind"] == "speech" and s2["subject"]["text"] and [a["code"] for a in s2["subject"]["attempts"]] == ["FORMAT", None]
    assert set(s2["subject"]) == {"kind", "tool", "text", "value", "reason", "thought", "attempts"} and "system" not in json.dumps(s2).lower().replace("system_", "")
    vote = by_id["t/example-vote/N3/legacy/0/L1"]
    assert vote["subject"]["kind"] == "choice" and vote["subject"]["value"] == "N1" and vote["context"]["blunt_side"] == ["N4"] and "cards" not in vote["context"]
    assert vote["brief"].startswith("N1、N2 是红队") and vote["meta"] == {"pair": "example", "variant": "A"}
    assert all(item["blind_id"] == blind_id(item["item_id"]) and re.fullmatch(r"[0-9a-f]{12}", item["blind_id"]) for item in report.items)
    assert len({item["blind_id"] for item in report.items}) == 3


def test_a_judge_is_given_the_blind_view_of_an_item_and_nothing_that_names_the_bot() -> None:
    report, _ = run_fake(examples(), bots(LEGACY, ALICE), "listy")
    for item in report.items:
        view = blind_view(item)
        assert set(view) == {"blind_id", "rubric", "title", "brief", "context", "subject"} and view["blind_id"] == item["blind_id"]
        assert set(view["subject"]) == set(BLIND_SUBJECT) and "attempts" not in view["subject"] and "truth" not in view["context"]  # (a refused try tells which brain it was)
        text = json.dumps(view, ensure_ascii=False)
        for name in ("truth", item["item_id"], item["situation"], "meta", "pair", item["bot"], "Alice"):
            assert name not in text, (name, item["item_id"])  # (no bot, no run, no situation id - a judge picks a card by its letter, the truth is kept apart)
    s2 = next(i for i in report.items if i["rubric"] == "S2" and i["bot"] == ALICE)
    assert set(blind_view(s2, subject=("kind", "text"))["subject"]) == {"kind", "text"} and [c["id"] for c in blind_view(s2)["context"]["cards"]] == ["A"]
    assert s2["truth"] == {"answer": [], "correct": None, "bot": ALICE, "cards": {"A": ALICE}} and s2["bot"] == ALICE  # (the item itself keeps the truth, for the step that scores the verdicts)


def test_a_row_without_a_decision_has_no_judge_items_and_a_situation_without_rubrics_has_none() -> None:
    sleepy, _ = run_fake(examples(), bots(ALICE), "sleepy")
    assert sleepy.items == []
    plain = parse_situation({"format": "arena-situation", "version": 1, "id": "p", "scenario": "minimal_test", "decision": {"seat": "N3"}, "steps": []})
    row = {"decision": {"tool": "speak", "text": "x"}, "run": "t", "situation": "p", "seat": "N3", "bot": "legacy", "sample": 0, "thoughts": [], "attempts": []}
    assert judge_items(row, plain, bots(LEGACY)) == []


def test_run_info_records_what_was_run_without_a_secret_or_a_memory_text(tmp_path: Path) -> None:
    sits, bot_list = examples(), bots(LEGACY, ALICE)
    report, _ = run_fake(sits, bot_list, "prose", tmp_path=tmp_path, max_calls=50)
    info = run_info(report, RunConfig(run_id="t", max_calls=50), sits, bot_list, max_calls=50)
    assert info["format"] == "arena-bench-run" and info["mode"] == "fake" and info["model"] == "fake" and info["real"] is None
    assert info["rows"] == 4 and info["by_status"] == {"ok": 4} and info["model_calls"] == 4 and info["config"]["max_calls"] == 50
    assert [s["id"] for s in info["situations"]] == ["example-vote", "example-speak"] and all(len(s["sha"]) == 12 for s in info["situations"])
    assert [b["label"] for b in info["bots"]] == ["legacy", "bot:Alice"] and info["bots"][1]["brain"] == "BotBrain" and len(info["bots"][1]["core_sha"]) == 12
    text = json.dumps(info, ensure_ascii=False)
    assert "打了二十多年线下局" not in text and "identity" not in text  # (the L0 of a bot is its own: a run records its hash, not its text)


def test_a_seat_a_situation_puts_a_bot_on_is_played_by_that_bot_alone_and_the_other_seats_by_every_bot_of_the_run() -> None:
    pinned = load_situation(PINNED)
    two_seats = parse_situation({
        "format": "arena-situation", "version": 1, "id": "two", "scenario": "minimal_test", "decision": {"seats": ["N3", "N4"]}, "steps": [], "bots": {"N4": "legacy:Zed"},
    })
    jobs = jobs_for([pinned, example("vote"), two_seats], bots(LEGACY, ALICE), 2)
    assert len(jobs) == 2 + 4 + 6 and len({job.key for job in jobs}) == len(jobs)
    assert [(j.situation.id, j.seat, j.bot.label) for j in jobs if j.sample == 0] == [
        ("example-pinned", "N3", "bot:Zed"), ("example-vote", "N3", "legacy"), ("example-vote", "N3", "bot:Alice"),
        ("two", "N3", "legacy"), ("two", "N3", "bot:Alice"), ("two", "N4", "legacy:Zed"),
    ]
    assert [b.label for b in bots_of(jobs)] == ["bot:Zed", "legacy", "bot:Alice", "legacy:Zed"]
    assert jobs_for([pinned], [], 3) and len(jobs_for([pinned], [], 3)) == 3  # (a situation that chooses every bot needs none from the run)
    assert jobs_for([example("vote")], [], 3) == []


def test_a_pinned_bot_is_what_the_model_sees_and_what_the_row_and_the_report_say(tmp_path: Path) -> None:
    pinned = load_situation(PINNED)
    report, seen = run_fake([pinned, example("speak")], bots(LEGACY), "prose", tmp_path=tmp_path)
    row = one(report, "bot:Zed", "pinned")
    assert (row["bot"], row["name"], row["brain"], row["status"], row["situation"]) == ("bot:Zed", "Zed", "BotBrain", "ok", "example-pinned")
    assert row["decision"]["tool"] == "respond" and row["decision"]["value"] == "again" and row["correct"] is True and row["tags"] == ["example", "M3"]
    assert [r["bot"] for r in report.rows if r["situation"] == "example-speak"] == [LEGACY]  # (the pinned seat is Zed's alone; the other situation is --bots')
    _job, scene, llm = next(t for t in seen if t[0].bot.label == "bot:Zed")
    system = llm.calls[0]["messages"][0]["content"]
    assert scene.seat_name == "Zed" and scene.world.display_name("N3") == "Zed"
    assert system.startswith("你是 Zed（本局编号 N3）。一个爱把规则记清楚的老玩家。") and "我记得：平票时，平票的人各再发言一轮" in system
    assert "规则里没写的事，先问主持人" in system and "Alice：话少，爱反问。" in system  # (the snapshot of the file, mounted as a game mounts a bot's memory)
    assert [b.label for b in report.bots] == ["bot:Zed", LEGACY]
    info = run_info(report, RunConfig(run_id="t"), [pinned, example("speak")])
    assert [(b["label"], b["brain"]) for b in info["bots"]] == [("bot:Zed", "BotBrain"), (LEGACY, "LegacyBrain")]
    items = [json.loads(line) for line in (tmp_path / "judge_items.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(i["rubric"], i["bot"]) for i in items] == [("M3", "bot:Zed")]  # (the legacy bot has no voice: no S2 item for it)


def test_jobs_are_every_situation_seat_bot_and_sample() -> None:
    sits = examples()
    two_seats = parse_situation({"format": "arena-situation", "version": 1, "id": "two", "scenario": "minimal_test", "decision": {"seats": ["N3", "N4"]}, "steps": []})
    jobs = jobs_for([*sits, two_seats], bots(LEGACY, ALICE), 3)
    assert len(jobs) == (2 + 2) * 2 * 3 and len({job.key for job in jobs}) == len(jobs)
    assert [j.key for j in jobs[:3]] == [("example-vote", "N3", "legacy", 0), ("example-vote", "N3", "legacy", 1), ("example-vote", "N3", "legacy", 2)]


# ------------------------------------------------------------------------------------------------ nobody speaks for a bot (H18)


def test_no_code_of_the_runner_can_speak_for_a_bot() -> None:
    """The commit gate is reached by the benchmark in one place only: ``StepRunner.run`` applies the *script's* steps (the situation: what the moderator
    and the other players did before the decision point).  The runner, the bots, the fake model, the summary, the commands and the real-model port have no
    ``submit`` and build no intent - the seat under test speaks through the tool calls of its own agent loop."""
    for name in ("runner", "bots", "fakes", "summary", "cli", "realllm", "safety", "situation", "__main__", "loaders", "metrics"):
        source = (ROOT / "bench" / f"{name}.py").read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        assert ".submit(" not in code and "Intent(" not in code, name
    import ast

    tree = ast.parse((ROOT / "bench" / "play.py").read_text(encoding="utf-8"))
    owners: list[str] = []
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for method in (n for n in ast.walk(cls) if isinstance(n, ast.FunctionDef)):
            if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "submit" for n in ast.walk(method)):
                owners.append(f"{cls.name}.{method.name}")
    assert owners == ["StepRunner.run"]
    replay = (ROOT / "bench" / "replay.py").read_text(encoding="utf-8")
    assert ".submit(" not in replay and "Intent(" not in replay  # (a replay goes through StepRunner too)


def test_the_runner_never_asks_a_model_for_a_speech_on_its_own() -> None:
    """Every model call of a sample is the agent's own round; the only prompts a model sees are the brain's messages (FULL, increments, nudges, tool results)."""
    report, seen = run_fake([example("speak")], bots(ALICE), "listy")
    _job, _scene, llm = seen[0]
    assert len(llm.calls) == 2
    roles = [[m["role"] for m in call["messages"]] for call in llm.calls]
    assert roles[0] == ["system", "user"] and roles[1] == ["system", "user", "assistant", "tool"]  # one conversation, extended by the tool result
    for call in llm.calls:
        text = "\n".join(str(m.get("content") or "") for m in call["messages"])
        assert "请写一段发言" not in text and "请代" not in text  # (nothing but the brain's own words)
    first, second = llm.calls
    assert first["messages"][0] == second["messages"][0] and first["messages"][1] == second["messages"][1]  # (one append-only conversation: the cache-friendly prefix)
    assert first["tools"] == second["tools"] and [t["function"]["name"] for t in first["tools"]][:3] == ["speak", "speak_template", "pass"]
