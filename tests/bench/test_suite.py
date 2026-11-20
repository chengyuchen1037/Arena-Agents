"""Suites (``bench/suites/*.yml``), their tiers and plans; ``run --suite``; the ``truth:`` of a situation and the scoring of a written answer; the ``@history=N``
option of a bot spec (what M2 narrows the window with); ``fixtures`` / ``suites`` on the command line."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from bench_helpers import bots, examples, run_fake

from bench.__main__ import main
from bench.bots import BotSpecError, parse_bot_spec, split_spec_list
from bench.fakes import FakeContext, Plan, open_request
from bench.play import build_scene
from bench.runner import mentions, score_of
from bench.situation import SituationError, load_situation, parse_situation
from bench.suite import TIERS, SuiteError, check_suites, estimate, estimates, load_suites, parse_suite, plan, with_option

ROOT = Path(__file__).resolve().parents[2]
BANK = ROOT / "bench" / "fixtures" / "situations"
ORDER = ["R", "S1", "S2", "S3", "L1", "L2", "L3", "L4", "M1", "M2", "M3", "E1", "E2", "G"]


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = main(argv)
    return code, capsys.readouterr().out


# ---------------------------------------------------------------------------------------------------------------------- suite files
def test_the_fourteen_suites_of_the_plan_are_defined_in_the_order_of_the_plan() -> None:
    suites = load_suites()
    assert list(suites) == ORDER
    for sid, suite in suites.items():
        assert suite.id == sid and suite.title and suite.purpose
        if suite.runnable:
            assert set(suite.tiers) == set(TIERS) and suite.baseline and suite.candidate
        else:
            assert sid in ("E1", "E2", "G") and suite.manual and not suite.tiers  # (what `run` does not run says how it is done)
    assert {s for s, v in suites.items() if v.uses} == {"S1", "S3"} and all(suites[s].uses == "S2" for s in ("S1", "S3"))
    assert suites["M1"].ablations and not suites["M1"].fixtures  # (M1 is the other suites' questions with one layer taken away)
    assert suites["L1"].pairs and suites["L1"].judges == ("L1",) and suites["L1"].gates == ("L1",)


def test_every_suite_keeps_what_it_promises_the_files_of_its_globs_its_rubrics_and_its_gates() -> None:
    suites = load_suites()
    problems = check_suites(suites)
    assert problems == [], problems


def test_a_suite_file_that_cannot_be_used_is_refused_with_its_name() -> None:
    good = yaml.safe_load((ROOT / "bench" / "suites" / "L3.yml").read_text(encoding="utf-8"))
    assert parse_suite(good, source="L3.yml").id == "L3"
    for change, message in [
        ({"format": "x"}, "format"), ({"id": "lower"}, "套件名"), ({"colour": 1}, "不认识的键"), ({"tiers": {"huge": {"samples": 1, "bots": ["legacy"]}}}, "不认识的档位"),
        ({"tiers": {"smoke": {"samples": 0, "bots": ["legacy"]}, "quick": {}, "full": {}}}, "samples"), ({"fixtures": []}, "fixtures"), ({"runnable": False}, "manual"),
        ({"tiers": {"smoke": {"samples": 1, "bots": []}}}, "bots"),
    ]:
        with pytest.raises(SuiteError, match=message):
            parse_suite({**good, **change}, source="L3.yml")
    with pytest.raises(SuiteError, match="L3.yml"):
        parse_suite("not a mapping", source="L3.yml")


def test_a_plan_is_the_situations_of_the_tier_and_the_bots_of_the_tier_and_m1_is_one_group_per_ablation() -> None:
    suites = load_suites()
    smoke = plan(suites["L3"], "smoke", suites)
    assert smoke.samples == 1 and [len(g.situations) for g in smoke.groups] == [3] and smoke.specs() == ["legacy"]
    quick = plan(suites["L1"], "quick", suites)
    assert len(quick.situations()) == 14 and quick.specs() == ["legacy", "bot:Alice"]
    m1 = plan(suites["M1"], "quick", suites)
    assert [g.layer for g in m1.groups] == ["L0 底层设定", "L2 规则理解", "L2 打法", "L1 操作笔记", "L2 印象"]
    assert m1.groups[0].bots == ("bot:Alice", "bot:Alice@blank-core") and m1.groups[1].bots == ("bot:Alice", "bot:Alice@no-digest")
    assert with_option("bot:Alice", "no-people") == "bot:Alice@no-people" and with_option("bot:Alice@history=6000", "no-people") == "bot:Alice@history=6000,no-people"
    with pytest.raises(SuiteError, match="不是靠 run 跑的"):
        plan(suites["G"], "quick", suites)


def test_the_estimate_counts_samples_and_calls_and_the_quick_tier_is_about_three_hundred_calls_over_all_suites() -> None:
    suites = load_suites()
    one = estimate(suites["L3"], "quick", suites)
    assert (one.situations, one.bots, one.samples, one.jobs) == (9, 2, 1, 18) and one.calls == round(18 * suites["L3"].calls_per_sample)
    table = estimates(suites)
    assert set(table) == {sid for sid, s in suites.items() if s.runnable}
    own = {sid: row["quick"].calls for sid, row in table.items() if sid not in ("S1", "S3")}  # (S1 and S3 are numbers over what S2 collects: no calls of their own)
    assert 200 <= sum(own.values()) <= 330, own
    assert all(row["smoke"].calls <= 20 for row in table.values())  # smoke is for CI: a handful of fake-model calls
    assert all(row["full"].calls > row["quick"].calls for row in table.values())


# ---------------------------------------------------------------------------------------------------------------------- the command line
def test_suites_lists_every_suite_with_its_size_and_checks_the_repository(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(["suites", "--check"], capsys)
    assert code == 0 and "套件定义和仓库一致" in out
    for sid in ORDER:
        assert any(line.startswith(f"{sid} |") for line in out.splitlines()), sid
    assert "不由 run 运行" in out and "合计" in out


def test_fixtures_check_says_the_committed_bank_is_what_the_generators_write(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(["fixtures", "--check"], capsys)
    assert code == 0 and "一致" in out


def test_run_suite_runs_the_tier_of_the_suite_on_the_fake_model_and_writes_where_it_is_told(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    code, text = run(["run", "--suite", "L3", "--tier", "smoke", "--out", str(out), "--run-id", "t1"], capsys)
    assert code == 0 and "3 道题 × 1 个 bot × 1 次 = 3 个样本" in text and "假模型" in text
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["situation"], r["bot"], r["mode"], r["model"]) for r in rows] == [
        ("l3-checked-hunter", "legacy", "fake", "fake"), ("l3-dead-voter", "legacy", "fake", "fake"), ("l3-declared-vs-ballot", "legacy", "fake", "fake")]
    assert all(r["truth"].get("answer") and r["score"]["kind"] == "choice" for r in rows)  # (the row carries the answer and the objective score)
    assert {i["rubric"] for i in map(json.loads, (out / "judge_items.jsonl").read_text(encoding="utf-8").splitlines())} == {"L3"}


def test_run_suite_with_a_tier_that_asks_two_bots_and_several_samples_counts_them_all(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, text = run(["run", "--suite", "M3", "--tier", "quick", "--samples", "2", "--out", str(tmp_path / "o"), "--ids", "m3-abstain,m3-both-win"], capsys)
    assert code == 0 and "2 道题 × 2 个 bot × 2 次 = 8 个样本" in text


@pytest.mark.parametrize(
    ("argv", "text"),
    [
        (["run", "--suite", "Z9"], "没有套件 'Z9'"), (["run", "--suite", "G"], "不是靠 run 跑的"), (["run", "--suite", "L3", "--situations", str(BANK)], "不能同时给"),
        (["run", "--suite", "L3", "--ids", "l3-nonsense"], "找不到这些题目"),
    ],
)
def test_run_suite_refuses_what_cannot_be_run_before_anything_is_made(argv: list[str], text: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run([*argv, "--out", str(tmp_path / "never")], capsys)
    assert code == 2 and text in out and not (tmp_path / "never").exists()


def test_run_suite_keeps_the_rules_of_real_mode_a_real_run_still_needs_max_calls(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(["run", "--suite", "L3", "--tier", "quick", "--real", "--out", str(tmp_path / "never")], capsys)
    assert code == 2 and "--max-calls" in out and not (tmp_path / "never").exists()


# ---------------------------------------------------------------------------------------------------------------------- truth
def _vote_data() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "bench" / "fixtures" / "situations" / "_example.yaml").read_text(encoding="utf-8"))


def test_the_truth_of_a_situation_is_its_objective_score_and_is_checked() -> None:
    data = _vote_data()
    data.pop("score")
    data["truth"] = {"request": "@vote", "answer": ["N1"], "wrong": ["N4"], "kind": "demo", "decisive": "x", "evidence": ["N4"], "sides": {"fluent": "N1", "blunt": "N4"}}
    situation = parse_situation(data)
    assert situation.score == ({"kind": "choice", "request": "@vote", "correct": ["N1"], "wrong": ["N4"]},)  # (no `score:` needed)
    assert situation.truth["sides"] == {"fluent": "N1", "blunt": "N4"}
    data["truth"] = {"request": "@vote", "answer": ["N1"], "match": "text"}
    assert parse_situation(data).score == ({"kind": "text", "request": "@vote", "contains": ["N1"], "absent": []},)
    for truth, message in [
        ({"answer": ["N1"]}, "request"), ({"request": "@vote", "answer": ["N1"], "wrong": ["N1"]}, "交集"), ({"request": "@nobody", "answer": ["N1"]}, "nobody"),
        ({"request": "@vote", "answer": []}, "answer"), ({"request": "@vote", "answer": ["N1"], "match": "fuzzy"}, "match"), ({"colour": 1}, "colour"),
        ({"sides": {"fluent": "N1", "blunt": "N1"}}, "sides"), ({"sides": {"fluent": "N1"}}, "sides"),
    ]:
        with pytest.raises(SituationError, match=message):
            parse_situation({**data, "truth": truth})


def test_a_written_answer_must_say_the_right_thing_as_a_word_of_its_own_and_none_of_the_wrong_ones() -> None:
    assert mentions("是 N7", "N7") and mentions("N7、N8", "N7") and not mentions("N12", "N1") and not mentions("N1", "N11") and mentions("N1。", "N1")
    assert mentions("一共 7 个", "7") and mentions("7个", "7") and not mentions("17 个", "7") and not mentions("70", "7") and mentions("出局了7人", "7")
    situation = load_situation(BANK / "m2-seer-report.yaml")
    scene = build_scene(situation, seat_name="Alice")
    request = open_request(scene.world, situation.decision.seat)
    assert request is not None and request["kind"] == "text"

    def answer(text: str) -> dict[str, Any]:
        return {"tool": "respond", "request_id": request["id"], "value": text}

    right = situation.truth["answer"][0]
    wrong = situation.truth["wrong"][0]
    assert score_of(answer(f"我记得是 {right}。"), scene) == {"kind": "text", "request": request["id"], "value": f"我记得是 {right}。", "correct": True, "wrong": False}
    assert score_of(answer(f"是 {wrong}"), scene)["correct"] is False and score_of(answer(f"是 {wrong}"), scene)["wrong"] is True
    assert score_of(answer(f"{right} 或者 {wrong}"), scene)["correct"] is False  # (naming both is not an answer)
    assert score_of(answer("我不记得了"), scene)["correct"] is False and score_of({"tool": "speak", "text": "x"}, scene) is None


def test_a_text_question_is_answered_in_a_run_and_scored_by_the_machine(tmp_path: Path) -> None:
    situation = load_situation(BANK / "m2-dead-count.yaml")
    right = situation.truth["answer"][0]

    def knows(ctx: FakeContext) -> Plan:
        request = open_request(ctx.world, ctx.seat)
        assert request is not None
        return Plan(calls=[("respond", {"request_id": request["id"], "value": f"一共 {right} 个人。"})], thought="（假模型）数了一下。")

    report, _ = run_fake([situation], bots("legacy"), knows)
    assert [(r["status"], r["correct"], r["score"]["kind"]) for r in report.rows] == [("ok", True, "text")]
    report, _ = run_fake([situation], bots("legacy"), "prose")
    assert [(r["status"], r["correct"]) for r in report.rows] == [("ok", False)]  # (the fake model's "我没有特别的看法。" is not an answer)
    assert report.rows[0]["truth"]["answer"] == [right]


# ---------------------------------------------------------------------------------------------------------------------- @history=N
def test_a_bot_spec_may_narrow_the_history_window_for_legacy_and_for_a_bot_and_nothing_else_is_taken_for_it() -> None:
    assert split_spec_list("legacy@history=6000,bot:Alice@history=6000,no-people,bot:Bob") == ["legacy@history=6000", "bot:Alice@history=6000,no-people", "bot:Bob"]
    legacy = parse_bot_spec("legacy@history=6000")
    assert legacy.label == "legacy@history=6000" and legacy.options == ("history=6000",) and legacy.kind == "legacy"
    for bad, message in [("bot:Alice@history=50", "不认识的选项"), ("bot:Alice@history=lots", "不认识的选项"), ("legacy@no-people", "legacy 没有可以拿掉的层")]:
        with pytest.raises(BotSpecError, match=message):
            parse_bot_spec(bad)


def test_the_history_window_of_a_run_is_what_the_option_says_the_same_for_both_brains() -> None:
    situation = load_situation(BANK / "m2-day1-exile.yaml")
    from bench.bots import make_bots

    bot_list = make_bots(["legacy", "legacy@history=6000", "bot:Alice@history=6000"], scenario_id="werewolf_12p")
    _report, seen = run_fake([situation], bot_list, "sleepy")
    first = {job.bot.label: llm.calls[0]["messages"] for job, _scene, llm in seen}
    user = {label: next(m["content"] for m in messages if m["role"] == "user") for label, messages in first.items()}
    assert "更早的内容已省略" not in user["legacy"]  # (twelve thousand characters: the whole game fits)
    for label in ("legacy@history=6000", "bot:Alice@history=6000"):
        assert "更早的内容已省略" in user[label], label  # (six thousand: the first night, the election and day 1 have scrolled out)
    assert "【硬事实台】" in user["bot:Alice@history=6000"] and "【出局公告】N1" in user["bot:Alice@history=6000"]  # (...and the table of facts of v0.14 still has them)
    assert "【硬事实台】" not in user["legacy@history=6000"]  # (v0.13.1 has no such table: what scrolled out is gone)


def test_examples_still_run_with_the_new_row_fields() -> None:
    report, _ = run_fake(examples(), bots("legacy"), "prose")
    assert all("truth" in row for row in report.rows)
