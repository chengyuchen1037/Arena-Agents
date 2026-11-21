"""``bench.report`` and ``python -m bench report``: the numbers of every suite with intervals, the gates, the states of a gate, the files.

Everything is made of hand-written rows (the shape ``bench.runner`` writes) in pytest's ``tmp_path``; no model is called and nothing is written below
``bench/``.  The numbers are worked out on paper in the comments: an L1 bias of +0.25 is 6 of 8 votes for the fluent side, and so on.
"""

from __future__ import annotations

import dataclasses
import json
import math
import random
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from bench import judging, report, runner, safety, stats
from bench.__main__ import main
from bench.bots import ABLATIONS
from bench.report import FAIL, NO_BASELINE, NOT_RUN, PASS, REFERENCE, UNCALIBRATED, UNJUDGED, AWAITING
from bench.runner import blind_id
from bench.suite import load_suites

ROOT = Path(__file__).resolve().parents[2]
N = 300  # bootstrap resamples: plenty for the tests, fast
TODAY = "2026-01-01"
SIDES = {"A": {"fluent": "N1", "blunt": "N6"}, "B": {"fluent": "N6", "blunt": "N1"}}  # (N1 is the wolf in both versions: the right vote)


def approx(value: float) -> Any:
    """The numbers of the report are rounded to 6 decimals (the same digits on every machine)."""
    return pytest.approx(value, abs=1e-5)


# ---------------------------------------------------------------------------------------------------------------------- builders
def row(bot: str, situation: str, *, suite: str, seat: str = "N3", sample: int = 0, tags: list[str] | None = None, meta: dict[str, Any] | None = None,
        truth: dict[str, Any] | None = None, status: str = "ok", stop: str | None = None, tool: str = "respond", value: str | None = None, text: str | None = None,
        speech: dict[str, Any] | None = None, runtime: dict[str, Any] | None = None, attempts: list[dict[str, Any]] | None = None, mode: str = "real",
        run: str = "r1", model: str = "m1") -> dict[str, Any]:
    """One line of ``results.jsonl`` (the fields the report reads, and a few more)."""
    decision = {"tool": tool, "value": value, "text": text, "channel_id": "public", "attempt": 0} if status == "ok" else None
    score = None
    if truth and truth.get("answer") and tool == "respond" and status == "ok":
        score = {"kind": "choice", "request": "r1", "value": value, "correct": value in truth["answer"], "wrong": False}
    return {
        "format": "arena-bench-result", "version": 1, "run": run, "bot": bot, "brain": "BotBrain", "situation": situation, "seat": seat, "sample": sample,
        "scenario": "werewolf_12p", "seed": 1, "mode": mode, "model": model, "tags": tags or [suite], "meta": meta or {}, "truth": truth or {}, "status": status,
        "stop": stop or ("decision" if status == "ok" else "slept"), "decision": decision, "correct": score["correct"] if score else None, "score": score,
        "attempts": attempts or [], "refusals": {}, "speech": speech, "integrity": {"speech_verbatim": True if text else None}, "runtime": runtime,
        "usage": {"llm_calls": 1}, "name": bot.split(":")[-1].split("@")[0], "elapsed_ms": 10,
    }


def item(source: dict[str, Any], rubric: str, **truth: Any) -> dict[str, Any]:
    """The judge item of a row (``bench.runner.judge_items``)."""
    item_id = f"{source['run']}/{source['situation']}/{source['seat']}/{source['bot']}/{source['sample']}/{rubric}"
    return {
        "format": "arena-judge-item", "version": 1, "item_id": item_id, "blind_id": blind_id(item_id), "rubric": rubric, "run": source["run"], "situation": source["situation"],
        "seat": source["seat"], "bot": source["bot"], "sample": source["sample"], "title": "t", "brief": "b", "tags": source["tags"], "context": {}, "subject": {},
        "truth": {"answer": source["truth"].get("answer", []), "correct": source["correct"], **truth}, "meta": source["meta"],
    }


def verdict(it: dict[str, Any], body: dict[str, Any], version: int = 1) -> dict[str, Any]:
    """The line ``judge-merge`` writes for an item."""
    return {
        "format": "arena-bench-judgment", "version": 1, "blind_id": it["blind_id"], "item_id": it["item_id"], "rubric": it["rubric"], "rubric_version": version,
        "verdict": body, "judge": {"source": "dsh-subagent", "attempts": 1},
    }


def write_run(base: Path, name: str, rows: list[dict[str, Any]], items: list[dict[str, Any]] = (), judgments: list[dict[str, Any]] = ()) -> Path:  # type: ignore[assignment]
    directory = base / name
    directory.mkdir(parents=True)
    (directory / "results.jsonl").write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in rows), encoding="utf-8")
    for file, lines in (("judge_items.jsonl", items), ("judgments.jsonl", judgments)):
        if lines:  # (a run that has no judge items has no such file)
            (directory / file).write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")
    (directory / "run.json").write_text(json.dumps({"format": "arena-bench-run", "mode": "real"}), encoding="utf-8")
    return directory


def read_rows(run: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (run / "results.jsonl").read_text(encoding="utf-8").splitlines()]


def gates_of(*entries: dict[str, Any]) -> list[report.Gate]:
    return report.parse_gates({"format": "arena-bench-gates", "version": 1, "gates": list(entries)})


def build(dirs: list[Path], gates: list[report.Gate] | None = None, **kwargs: Any) -> report.Report:
    kwargs.setdefault("resamples", N)
    kwargs.setdefault("generated", TODAY)
    return report.build_report(dirs, gates=gates if gates is not None else report.load_gates(), **kwargs)


def gate_of(rep: report.Report, gate_id: str) -> report.GateResult:
    return next(g for g in rep.gates if g.gate.id == gate_id)


def arm_of(rep: report.Report, gate_id: str, arm: str) -> report.ArmResult:
    return next(a for a in gate_of(rep, gate_id).arms if a.arm == arm)


def measure(rep: report.Report, suite: str, metric: str, arm: str) -> report.Estimate:
    result = next(s for s in rep.suites if s.id == suite)
    found = next(m for m in result.metrics if m.spec.name == metric)
    return next(r.measured.estimate for r in found.rows if r.arm == arm)


def l1_run(base: Path, name: str, behaviours: dict[str, Any], *, pairs: int = 3, samples: int = 2, edits: dict[tuple[str, int, str, int], str] | None = None) -> Path:
    """A bot per entry of ``behaviours`` (a function from the two sides of the pair to the vote) on ``pairs`` pairs × both versions × ``samples``.
    ``edits`` replaces single votes: ``{(bot, pair, "B", sample): "abstain"}``."""
    rows = []
    for bot, vote in behaviours.items():
        for p in range(pairs):
            for variant in ("A", "B"):
                for s in range(samples):
                    value = (edits or {}).get((bot, p, variant, s), vote(SIDES[variant]))
                    truth = {"answer": ["N1"], "sides": SIDES[variant], "kind": "x"}
                    rows.append(row(
                        bot, f"l1-p{p}-{variant.lower()}", suite="L1", sample=s, tags=["L1", f"pair:l1-p{p}", variant], meta={"pair": f"l1-p{p}", "variant": variant},
                        truth=truth, value=value,
                    ))
    return write_run(base, name, rows)


def right(sides: dict[str, str]) -> str:
    return "N1"  # (the wolf, in A and in B)


def trusts_fluent(sides: dict[str, str]) -> str:
    return sides["blunt"]  # (a vote for the blunt one = believing the fluent one)


def distrusts_fluent(sides: dict[str, str]) -> str:
    return sides["fluent"]


def quiz_rows(bot: str, correct: int, total: int, *, suite: str = "M3", prefix: str = "m3", samples: int = 1) -> list[dict[str, Any]]:
    """``total`` questions × ``samples``; the first ``correct`` answers (counted over all rows in order) are right."""
    rows = []
    done = 0
    for q in range(total):
        for s in range(samples):
            truth = {"answer": ["b"], "kind": "rules"}
            rows.append(row(bot, f"{prefix}-q{q:02d}", suite=suite, sample=s, truth=truth, value="b" if done < correct else "a"))
            done += 1
    return rows


# ---------------------------------------------------------------------------------------------------------------------- the forms of a gate
def test_the_value_of_a_gate_is_a_number_or_one_of_three_forms_and_nothing_is_evaluated() -> None:
    t = report.parse_threshold
    assert (t(0.9).kind, t(0.9).amount, t("0.9").kind, t(0).amount) == ("const", 0.9, "const", 0.0)
    assert (t("0.5 * baseline").kind, t("0.5 * baseline").amount, t("baseline*0.5").amount, t("baseline").amount) == ("scale", 0.5, 0.5, 1.0)
    assert (t("baseline + 0.20").kind, t("baseline + 0.20").amount, t("0.2 + baseline").amount, t("baseline - 0.1").amount) == ("offset", 0.2, 0.2, -0.1)
    assert t("0.5 * baseline").resolve(0.4) == approx(0.2) and t("baseline + 0.20").resolve(0.4) == approx(0.6) and t("baseline - 0.1").resolve(0.4) == approx(0.3)
    assert t(0.9).resolve(None) == 0.9 and t(0.9).needs_baseline is False
    assert t("0.5 * baseline").resolve(None) is None and t("0.5 * baseline").resolve(math.nan) is None and t("0.5 * baseline").needs_baseline is True
    for bad in ("__import__('os').getcwd()", "baseline ** 2", "2 * 3", "baseline + baseline", "0.5 * Baseline", "min(baseline, 1)", "1e3", "", True, None, math.inf):
        with pytest.raises(report.ReportError, match="门槛"):
            t(bad)


def test_a_gates_file_is_refused_with_the_name_of_the_gate_that_is_wrong() -> None:
    ok = {"id": "A", "suite": "M3", "metric": "rules_quiz.accuracy", "op": ">=", "value": 0.9}
    assert report.parse_gates({"format": "arena-bench-gates", "version": 1, "gates": [ok]})[0].threshold.amount == 0.9  # type: ignore[union-attr]
    for change, message in [
        ({"op": "=>"}, "op"), ({"colour": 1}, "不认识的键"), ({"value": "baseline * baseline"}, "看不懂"), ({"scope": "everyone"}, "scope"), ({"abs": "yes"}, "abs"),
        ({"baseline": "high"}, "baseline"), ({"metric": ""}, "metric"),
    ]:
        with pytest.raises(report.ReportError, match=message) as caught:
            report.parse_gates({"gates": [{**ok, **change}]})
        assert "门槛 A" in str(caught.value)
    with pytest.raises(report.ReportError, match="id 重复"):
        report.parse_gates({"gates": [ok, ok]})
    with pytest.raises(report.ReportError, match="gates"):
        report.parse_gates({"format": "arena-bench-gates"})
    special = report.parse_gates({"gates": [{"id": "E1", "suite": "E1", "metric": "x", "op": "no_regression_and_one_significant_gain"}]})[0]
    assert special.threshold is None  # (an op that is not a comparison needs no value)


def test_the_committed_gates_suites_and_metrics_agree_with_each_other_and_with_the_report() -> None:
    gates = report.load_gates()  # (bench/gates.yml)
    suites = load_suites()
    ids = {g.id for g in gates}
    for suite in suites.values():
        assert set(suite.gates) <= ids, (suite.id, set(suite.gates) - ids)  # (every gate a suite file promises is in gates.yml)
        for metric in suite.metrics:
            assert (suite.id, metric) in report.METRICS, f"{suite.id}: the report cannot make {metric}"
        for ablation in suite.ablations:
            assert (ablation["suite"], ablation["metric"]) in report.METRICS, ablation
    for gate in gates:
        assert gate.suite in suites, gate.id
        if gate.suite in ("E1", "E2", "G", "M1"):
            continue  # (not made by `run`, or one p-value per layer: the metric names its layer in M1.yml)
        assert (gate.suite, gate.metric) in report.METRICS, f"{gate.id}: {gate.suite}.{gate.metric}"
        assert gate.metric in suites[gate.suite].metrics, f"{gate.id}: {gate.metric} is not among the metrics {gate.suite}.yml declares"
    assert {g.id for g in gates if g.scope == "pooled"} == {"S2", "S3a", "S3b"} and [g.abs for g in gates if g.id == "L1"] == [True]
    # the formats and the layers the report relies on are the ones the other modules write
    assert report.RESULT_FORMAT == runner.RESULT_FORMAT and report.JUDGMENT_FORMAT == judging.JUDGMENT_FORMAT and set(ABLATIONS) >= {"nomem", "no-notes", "blank-core"}


# ---------------------------------------------------------------------------------------------------------------------- bots
def test_the_baseline_is_a_family_of_bots_and_a_bot_without_a_layer_is_neither_baseline_nor_candidate() -> None:
    labels = ["legacy", "legacy:Alice", "legacy@history=6000", "bot:Alice", "bot:Alice@history=6000", "bot:Alice@no-people", "bot:Alice@history=6000,no-notes", "legacyx"]
    assert report.classify(labels, "legacy") == {
        "baseline": ["legacy", "legacy:Alice", "legacy@history=6000"],
        "candidates": ["bot:Alice", "bot:Alice@history=6000", "legacyx"],
        "ablated": ["bot:Alice@history=6000,no-notes", "bot:Alice@no-people"],
    }
    assert report.classify(labels, "legacy@history=6000")["baseline"] == ["legacy@history=6000"]  # (spelled with options: only that one)
    assert report.split_label("bot:Alice@no-people,history=6000") == ("bot:Alice", ("no-people", "history=6000"))
    assert report.ablations_of("bot:Alice@no-people,history=6000") == ("no-people",) and report.plain_options("bot:Alice@no-people,history=6000") == ("history=6000",)
    assert report.suite_of(["example", "L1"], "example-vote", {"L1"}) == "L1" and report.suite_of(["A", "x"], "l3-dead-voter", {"L3"}) == "L3" and report.suite_of([], "x-1", {"L3"}) is None


# ---------------------------------------------------------------------------------------------------------------------- reading a row
def test_an_answer_is_right_wrong_none_or_left_out() -> None:
    truth = {"answer": ["b"]}
    assert report.answer_state(row("x", "q", suite="M3", truth=truth, value="b")) == "correct"
    assert report.answer_state(row("x", "q", suite="M3", truth=truth, value="a")) == "wrong"
    assert report.answer_state(row("x", "q", suite="M3", truth=truth, value="abstain")) == "abstain"
    assert report.answer_state(row("x", "q", suite="M3", truth=truth, status="no_decision")) == "none"
    assert report.answer_state(row("x", "q", suite="M3", truth=truth, tool="pass")) == "none"  # (it did something else than answer)
    assert report.answer_state(row("x", "q", suite="M3", truth=truth, status="error", stop="scene")) == "skip"  # (a failure of the run says nothing about the bot)
    assert report.answer_state(row("x", "q", suite="M3", truth=truth, status="incomplete", stop="budget")) == "skip"
    assert report.answer_state(row("x", "q", suite="M3", truth=truth, status="incomplete", stop="timeout")) == "none"  # (it did not make its move in time)
    assert report.answer_state(row("x", "q", suite="S2", tool="speak", text="hi")) == "skip"  # (nothing to score against)


def test_the_pair_a_situation_belongs_to_is_read_from_the_meta_a_tag_or_the_id() -> None:
    base = row("x", "l1-dead-voter-a", suite="L1")
    assert report.pair_of({**base, "meta": {"pair": "p1"}}) == "p1"
    assert report.pair_of({**base, "meta": {}, "tags": ["L1", "pair:p2", "A"]}) == "p2"
    assert report.pair_of({**base, "meta": {}, "tags": ["L1"]}) == "l1-dead-voter" and report.pair_of(row("x", "m3-abstain", suite="M3")) is None
    assert report.variant_of({"meta": {"variant": "b"}}) == "B" and report.variant_of({"tags": ["L1", "A"]}) == "A" and report.variant_of({"situation": "l1-x-b"}) == "B"


# ---------------------------------------------------------------------------------------------------------------------- L1
def test_a_bot_that_always_trusts_the_fluent_side_has_bias_plus_half_one_that_votes_right_has_zero(tmp_path: Path) -> None:
    run = l1_run(tmp_path, "L1", {"legacy": trusts_fluent, "bot:Alice": right, "bot:Bob": distrusts_fluent})
    rep = build([run])
    legacy, alice, bob = (measure(rep, "L1", "expression_bias", b) for b in ("legacy", "bot:Alice", "bot:Bob"))
    assert (legacy.value, alice.value, bob.value) == (0.5, 0.0, -0.5)
    assert legacy.n == alice.n == bob.n == 12 and legacy.items == 3  # (3 pairs × 2 samples = 6 complete pairs = 12 votes, spread over 3 pairs)
    # nothing varies, so the bootstrap interval would be a single point: the report widens it (Wilson for as many observations as there are pairs)
    assert legacy.method == alice.method == bob.method == "wilson-items"
    assert legacy.lo < 0.5 == legacy.hi and alice.lo < 0.0 < alice.hi and bob.lo == -0.5 < bob.hi
    assert alice.extra["complete"] == 6 and alice.extra["abstain"] == 0 and alice.extra["votes"] == 12
    # the vote of the fluent side - side-taking accuracy: only the bot that votes for the wolf is right
    assert measure(rep, "L1", "accuracy", "bot:Alice").value == 1.0 and measure(rep, "L1", "accuracy", "legacy").value == 0.5 and measure(rep, "L1", "accuracy", "bot:Bob").value == 0.5


def test_a_bias_of_a_quarter_is_six_votes_of_eight_for_the_fluent_side(tmp_path: Path) -> None:
    """4 pairs, one sample: pairs 0 and 1 are voted for the blunt one in A and in B (trust, trust), pairs 2 and 3 right (distrust in A, trust in B): 2 + 2 + 1 + 1 = 6 of 8."""
    edits = {("bot:X", p, "A", 0): "N6" for p in (0, 1)}  # (in A the blunt one is N6; in B it is N1, the right vote: the default)
    run = l1_run(tmp_path, "L1", {"bot:X": right}, pairs=4, samples=1, edits=edits)
    est = measure(build([run]), "L1", "expression_bias", "bot:X")
    assert est.value == approx(0.25) and est.n == 8 and est.items == 4 and est.lo < 0.25 < est.hi
    assert est.method == "bootstrap"  # (the pairs came out differently: there is something to resample)


def test_abstentions_votes_for_a_third_player_and_missing_answers_are_left_out_with_the_other_half_of_their_pair(tmp_path: Path) -> None:
    edits = {("bot:Alice", 0, "B", 0): "abstain", ("bot:Alice", 1, "A", 0): "N9"}  # (4 pairs, one sample, otherwise right)
    rows = read_rows(l1_run(tmp_path, "made", {"bot:Alice": right}, pairs=4, samples=1, edits=edits))
    rows.append(row("bot:Alice", "l1-p9-a", suite="L1", tags=["L1", "pair:l1-p9", "A"], meta={"pair": "l1-p9", "variant": "A"}, truth={"answer": ["N1"], "sides": SIDES["A"]}, status="no_decision"))
    rep = build([write_run(tmp_path, "L1", rows)])
    est = measure(rep, "L1", "expression_bias", "bot:Alice")
    # pairs 2 and 3 are complete (right in both: bias 0); pair 0 (B abstained), pair 1 (A voted a third player) and pair 9 (B missing, A no decision) are out
    assert est.value == 0.0 and est.n == 4 and est.extra["complete"] == 2 and est.extra["incomplete"] == 3
    assert est.extra["abstain"] == 2 and est.extra["other"] == 1  # (the rows that took no side: 2, the vote for a third player: 1)
    assert "没站边 2 行" in est.notes[0] and "投给第三人 1 行" in est.notes[0]
    acc = measure(rep, "L1", "accuracy", "bot:Alice")
    # 7 votes were cast (the abstention and the missing decision are not votes): 6 for the wolf, 1 for N9
    assert acc.n == 7 and acc.value == approx(6 / 7) and acc.extra["abstain"] == 2
    assert measure(rep, "L1", "abstain_rate", "bot:Alice").value == approx(2 / 9)


def test_a_bot_that_is_right_but_abstains_on_one_version_does_not_look_biased(tmp_path: Path) -> None:
    """Counting the votes that were cast, it would trust the fluent side in every B and never in an A it abstained on: a fake bias.  Pairs count when whole."""
    edits = {("bot:Alice", p, "A", 0): "abstain" for p in range(4)}
    run = l1_run(tmp_path, "L1", {"bot:Alice": right}, pairs=5, samples=1, edits=edits)
    est = measure(build([run]), "L1", "expression_bias", "bot:Alice")
    assert est.value == 0.0 and est.extra["complete"] == 1 and est.extra["incomplete"] == 4  # (pair 4 is the only whole one)
    only_b = [r for r in read_rows(run) if r["situation"].endswith("-b")]
    naive_run = write_run(tmp_path, "naive", only_b)
    assert measure(build([naive_run]), "L1", "expression_bias", "bot:Alice").value is None  # (no pair is whole: no number, not +0.5)


def test_a_failed_sample_is_not_an_abstention(tmp_path: Path) -> None:
    rows = read_rows(l1_run(tmp_path, "L1", {"bot:Alice": right}, pairs=2, samples=1))
    rows[0] = {**rows[0], "status": "error", "stop": "scene", "decision": None}
    est = measure(build([write_run(tmp_path, "again", rows)]), "L1", "expression_bias", "bot:Alice")
    assert est.extra["complete"] == 1 and est.extra["abstain"] == 0  # (the error is skipped; its partner has no pair left: one whole pair, no abstention)


# ---------------------------------------------------------------------------------------------------------------------- accuracy: M3, L4, L2
def test_the_accuracy_of_a_quiz_is_right_answers_over_questions_asked_with_an_interval_around_it(tmp_path: Path) -> None:
    rows = quiz_rows("legacy", 12, 8, samples=2) + quiz_rows("bot:Alice", 16, 8, samples=2)  # 12 of 16 right: 75 %; 16 of 16: 100 %
    rep = build([write_run(tmp_path, "M3", rows)])
    legacy, alice = measure(rep, "M3", "rules_quiz.accuracy", "legacy"), measure(rep, "M3", "rules_quiz.accuracy", "bot:Alice")
    assert legacy.value == 0.75 and legacy.n == 16 and legacy.items == 8 and legacy.lo < 0.75 < legacy.hi and legacy.method == "bootstrap"
    assert alice.value == 1.0 and alice.n == 16 and alice.method == "wilson-items" and alice.lo == approx(stats.wilson_interval(8, 8)[0]) and alice.hi == 1.0
    # an answer that was not given counts as wrong, a failure of the run is not counted at all
    rows = quiz_rows("bot:Alice", 10, 10) + [row("bot:Alice", "m3-late", suite="M3", truth={"answer": ["b"]}, status="no_decision"),
                                             row("bot:Alice", "m3-broken", suite="M3", truth={"answer": ["b"]}, status="error", stop="scene")]
    est = measure(build([write_run(tmp_path, "M3b", rows)]), "M3", "rules_quiz.accuracy", "bot:Alice")
    assert est.value == approx(10 / 11) and est.n == 11 and est.extra["no_answer"] == 1 and est.extra["left_out"] == 1


def test_the_vote_accuracy_of_l2_and_l4_and_the_recall_of_m2_use_the_same_rule(tmp_path: Path) -> None:
    rows = quiz_rows("bot:Alice", 3, 4, suite="L4", prefix="l4") + quiz_rows("bot:Alice", 1, 4, suite="L2", prefix="l2") + quiz_rows("bot:Alice", 2, 4, suite="M2", prefix="m2")
    rep = build([write_run(tmp_path, "mix", rows)])
    assert measure(rep, "L4", "side_accuracy", "bot:Alice").value == 0.75
    assert measure(rep, "L2", "accuracy", "bot:Alice").value == 0.25
    assert measure(rep, "M2", "recall.accuracy", "bot:Alice").value == 0.5


# ---------------------------------------------------------------------------------------------------------------------- the judges: S2, L2, L3
def judged_run(base: Path, name: str, spec: list[tuple[str, str, str, dict[str, Any]]], *, rubric: str, suite: str, cards: dict[str, str] | None = None,
               judged: bool = True) -> Path:
    """``spec``: ``(bot, situation, sample-as-text, verdict)``: a row, its item and (``judged``) the judgment."""
    rows, items, verdicts = [], [], []
    for bot, situation, sample, body in spec:
        truth = {"answer": ["N2"]} if rubric != "S2" else {}
        r = row(bot, situation, suite=suite, sample=int(sample), truth=truth, value="N2", tool="respond" if rubric != "S2" else "speak", text="我先听听。" if rubric == "S2" else None)
        rows.append(r)
        extra = {"bot": bot, "cards": cards} if rubric == "S2" else {}
        it = item(r, rubric, **extra)
        items.append(it)
        if judged:
            verdicts.append(verdict(it, body))
    return write_run(base, name, rows, items, verdicts)


CARDS = {"A": "bot:Alice", "B": "bot:Bob", "C": "bot:Carol", "D": "bot:David"}


def test_the_style_of_a_speech_is_identified_when_the_judge_names_the_card_of_the_bot_that_spoke(tmp_path: Path) -> None:
    """Alice's eight speeches: the judge says A five times (right), C three times; the runner-up is A in two of those three: top-1 5/8, top-2 7/8, chance 1/4 (four cards)."""
    spec = [("bot:Alice", f"s2-q{i}", "0", {"match": "A", "runner_up": "B", "confidence": "high", "cues": "x"}) for i in range(5)]
    spec += [("bot:Alice", f"s2-q{i}", "0", {"match": "C", "runner_up": "A" if i < 7 else "B", "confidence": "low", "cues": "x"}) for i in range(5, 8)]
    cal = {"rubrics": {"S2": {"n": 40, "kappa": 0.72, "ci": [0.55, 0.85], "counts_for_acceptance": True}}}
    rep = build([judged_run(tmp_path, "S2", spec, rubric="S2", suite="S2", cards=CARDS)], calibration=cal)
    top1, top2 = measure(rep, "S2", "style.top1_accuracy", "bot:Alice"), measure(rep, "S2", "style.top2_accuracy", "bot:Alice")
    assert (top1.value, top1.n, top1.items) == (0.625, 8, 8) and top1.extra["chance"] == 0.25 and top1.lo < 0.625 < top1.hi
    assert top2.value == 0.875 and top2.extra["chance"] == 0.5
    assert "随机水平 25.0%" in top1.notes and rep.instruments["S2"].state == "counts"


def test_the_judges_numbers_of_l2_and_l3_are_the_shares_of_their_verdicts(tmp_path: Path) -> None:
    trivia = [("bot:Alice", f"l2-q{i}", "0", {"basis": "trivia" if i < 2 else "decisive_fact", "cites_decisive": i >= 2, "reason": "r"}) for i in range(6)]  # 2 of 6
    legacy = [("legacy", f"l2-q{i}", "0", {"basis": "trivia" if i < 4 else "expression", "cites_decisive": False, "reason": "r"}) for i in range(6)]  # 4 of 6
    rep = build([judged_run(tmp_path, "L2", trivia + legacy, rubric="L2", suite="L2")])
    assert measure(rep, "L2", "trivia_hit_rate", "bot:Alice").value == approx(1 / 3) and measure(rep, "L2", "trivia_hit_rate", "legacy").value == approx(2 / 3)
    # L3: "hit" = the verdict says cites_decisive OR the basis is the decisive fact; the strict reading (the main basis alone) is a number of its own
    verdicts = [
        {"basis": "decisive_fact", "cites_decisive": True, "reason": "r"},  # hit, main basis
        {"basis": "expression", "cites_decisive": True, "reason": "r"},  # hit: names the fact, does not rest on it
        {"basis": "decisive_fact", "cites_decisive": False, "reason": "r"},  # hit: main basis (the judge may leave cites_decisive false)
        {"basis": "trivia", "cites_decisive": False, "reason": "r"},  # miss
    ]
    rep = build([judged_run(tmp_path, "L3", [("bot:Alice", f"l3-q{i}", "0", v) for i, v in enumerate(verdicts)], rubric="L3", suite="L3")])
    assert measure(rep, "L3", "decisive_hit_rate", "bot:Alice").value == 0.75 and measure(rep, "L3", "decisive_main_rate", "bot:Alice").value == 0.5


def test_a_judges_number_counts_only_when_its_rubric_is_calibrated(tmp_path: Path) -> None:
    spec = [("bot:Alice", f"l3-q{i}", "0", {"basis": "decisive_fact", "cites_decisive": True, "reason": "r"}) for i in range(8)]
    spec += [("legacy", f"l3-q{i}", "0", {"basis": "expression", "cites_decisive": False, "reason": "r"}) for i in range(8)]
    run = judged_run(tmp_path, "L3", spec, rubric="L3", suite="L3")
    gates = gates_of({"id": "L3", "suite": "L3", "metric": "decisive_hit_rate", "op": ">=", "value": "baseline + 0.20"})

    def state(cal: dict[str, Any] | None) -> report.ArmResult:
        return arm_of(build([run], gates, calibration=cal), "L3", "bot:Alice")

    good = state({"rubrics": {"L3": {"n": 30, "kappa": 0.7, "ci": [0.5, 0.9], "counts_for_acceptance": True}}})
    assert (good.status, good.value, good.baseline_value, good.limit) == (PASS, 1.0, 0.0, 0.2)
    none = state(None)  # no calibration at all
    assert (none.status, none.would_be) == (UNCALIBRATED, PASS) and "没有校准结果" in none.reason
    other = state({"rubrics": {"S2": {"n": 30, "kappa": 0.9, "ci": [0.8, 1.0], "counts_for_acceptance": True}}})  # (calibrated, but another rubric)
    assert other.status == UNCALIBRATED and "calibration.json 里没有 L3" in other.reason
    weak = state({"rubrics": {"L3": {"n": 30, "kappa": 0.52, "ci": [0.3, 0.7], "counts_for_acceptance": False}}})
    assert (weak.status, weak.would_be) == (REFERENCE, PASS) and "κ=0.52" in weak.reason
    lying = build([run], gates, calibration={"rubrics": {"L3": {"n": 30, "kappa": 0.52, "ci": [0.3, 0.7], "counts_for_acceptance": True}}})  # (flag says yes, kappa says no)
    assert arm_of(lying, "L3", "bot:Alice").status == REFERENCE and any("κ=0.52 < 0.6" in w for w in lying.warnings)
    stale = state({"rubrics": {"L3": {"n": 30, "kappa": 0.8, "ci": [0.6, 0.9], "counts_for_acceptance": True, "rubric_version": 2}}})  # (the judgments are of v1)
    assert stale.status == UNCALIBRATED and "v2" in stale.reason and "v1" in stale.reason
    # items without any judgment: the judge has not been run
    unjudged = build([judged_run(tmp_path, "L3-unjudged", spec, rubric="L3", suite="L3", judged=False)], gates)
    assert arm_of(unjudged, "L3", "bot:Alice").status == UNJUDGED and gate_of(unjudged, "L3").status == UNJUDGED


# ---------------------------------------------------------------------------------------------------------------------- R, S1, S3 from the rows
def test_the_runtime_numbers_come_from_the_ledger_counts_and_the_attempts_of_each_row(tmp_path: Path) -> None:
    ok, long_, bad = {"tool": "speak", "ok": True, "code": None}, {"tool": "speak", "ok": False, "code": "TOO_LONG"}, {"tool": "speak", "ok": False, "code": "FORMAT"}
    plans = [  # (attempts, truncated speeches, floor ends)
        ([long_, ok], 0, {"completed": 1}),  # refused once, rewritten: 1 retried, 1 recovered
        ([bad, long_], 0, {"completed": 1}),  # refused twice: the first was tried again and failed; the second was not tried again
        ([ok], 1, {"completed": 1}),  # (one speech the engine cut)
        ([long_, ok], 0, {"timeout": 1}),  # (a floor that ended by the clock)
        ([bad, ok, long_, ok], 0, {"completed": 2}),  # two rewrites, both went through
    ]
    rows = [
        row("bot:Alice", f"r-60-q{i}", suite="R", tool="speak", text="好的。", attempts=a, runtime={"agent_speeches": 1, "truncated_speeches": t, "floor_ends": ends})
        for i, (a, t, ends) in enumerate(plans)
    ]
    rep = build([write_run(tmp_path, "R", rows)])
    truncated = measure(rep, "R", "form.truncated_rate", "bot:Alice")
    rewrite = measure(rep, "R", "runtime.speak.rewrite_success_rate", "bot:Alice")
    timeout = measure(rep, "R", "runtime.floor_timeout_rate", "bot:Alice")
    assert (truncated.value, truncated.n) == (0.2, 5)  # 1 of 5 speeches
    assert (rewrite.value, rewrite.n, rewrite.extra["refused"]) == (0.8, 5, 6)  # 4 of the 5 refused speeches that were tried again got through (6 refused in all)
    assert (timeout.value, timeout.n) == (approx(1 / 6), 6)  # 1 of 6 floor ends
    # a bot that was never refused has no rewrite rate: no number, not 0 and not 100 %
    clean = [row("bot:Bob", f"r-60-q{i}", suite="R", tool="speak", text="好的。", attempts=[ok], runtime={"agent_speeches": 1, "truncated_speeches": 0}) for i in range(3)]
    rep = build([write_run(tmp_path, "R2", clean)])
    assert measure(rep, "R", "runtime.speak.rewrite_success_rate", "bot:Bob").value is None and measure(rep, "R", "form.truncated_rate", "bot:Bob").value == 0.0
    gates = gates_of({"id": "R2", "suite": "R", "metric": "runtime.speak.rewrite_success_rate", "op": ">=", "value": 0.9})
    assert arm_of(build([write_run(tmp_path, "R3", clean)], gates), "R2", "bot:Bob").status == NOT_RUN  # (nothing was refused, so nothing was rewritten)


def test_the_form_numbers_are_shares_of_the_speeches_the_runner_checked(tmp_path: Path) -> None:
    def speech(problems: list[str], enum: bool = False, in_band: bool | None = True) -> dict[str, Any]:
        return {"chars": 30, "problems": problems, "inline_enum": enum, "paragraphs": 1, "band": [10, 60], "in_band": in_band}

    plans = [speech(["list"]), speech(["list", "prefix"]), speech(["heading"]), speech([], enum=True), speech([]), speech([]), speech([], in_band=False), speech([], in_band=False)]
    rows = [row("bot:Alice", f"s2-q{i}", suite="S2", tool="speak", text="我先听听。", speech=sp) for i, sp in enumerate(plans)]
    rep = build([write_run(tmp_path, "S2", rows)])
    rate = lambda name: measure(rep, "S1", f"form.{name}", "bot:Alice")  # noqa: E731
    assert rate("list_rate").value == 0.25 and rate("markdown_rate").value == 0.125 and rate("self_prefix_rate").value == 0.125
    assert rate("report_rate").value == 0.5  # (a list, a list and a prefix, a heading, an inline enumeration)
    assert rate("in_band_rate").value == 0.75 and rate("list_rate").n == 8
    # a speech that has no band (the bot's voice declares none) is not in the in-band rate
    free = [row("bot:Bob", f"s2-q{i}", suite="S2", tool="speak", text="好。", speech={**speech([]), "band": None, "in_band": None}) for i in range(3)]
    assert measure(build([write_run(tmp_path, "S2b", free)]), "S1", "form.in_band_rate", "bot:Bob").value is None


def test_homogeneity_needs_four_bots_to_count_and_the_stock_phrases_are_counted_per_speech(tmp_path: Path) -> None:
    gates = gates_of(
        {"id": "S3a", "suite": "S3", "metric": "cliches.hits_per_speech", "op": "<=", "value": 0.96, "scope": "pooled"},
        {"id": "S3b", "suite": "S3", "metric": "homogeneity.shared_occurrence_share", "op": "<=", "value": 0.011, "scope": "pooled"},
    )
    shared = "这一票必须归票给他因为他的发言不可验证"  # (one stock phrase: 归票; "可验证" is one too: 2 hits)

    def speeches(bots: list[str]) -> list[dict[str, Any]]:
        return [
            row(bot, f"s2-q{q}", suite="S2", tool="speak", text=f"{shared}{q}{bot[-1]}", speech={"chars": 20, "problems": [], "inline_enum": False, "band": None, "in_band": None})
            for bot in bots for q in range(3)
        ]

    four = build([write_run(tmp_path, "S2", speeches(["bot:A", "bot:B", "bot:C", "bot:D"]))], gates)
    homog = measure(four, "S3", "homogeneity.shared_occurrence_share", report.POOLED)
    assert homog.value > 0.5 and homog.extra["bots"] == 4 and homog.extra["min_seats"] == 4 and homog.n == 12 and homog.items == 3
    assert measure(four, "S3", "cliches.hits_per_speech", report.POOLED).value == 2.0
    assert arm_of(four, "S3b", report.POOLED).status == FAIL and arm_of(four, "S3a", report.POOLED).status == FAIL
    two = build([write_run(tmp_path, "S2b", speeches(["bot:A", "bot:B"]))], gates)  # 2 bots share fewer seats: the 0.011 of the 12-seat baseline is not comparable
    arm = arm_of(two, "S3b", report.POOLED)
    assert arm.status == REFERENCE and arm.would_be == FAIL and "不可比" in "".join(measure_notes(two, "S3", "homogeneity.shared_occurrence_share", report.POOLED)) + arm.reason
    one = build([write_run(tmp_path, "S2c", speeches(["bot:A"]))], gates)  # (one bot: there is nothing to share with)
    assert arm_of(one, "S3b", report.POOLED).status == NOT_RUN and measure(one, "S3", "homogeneity.shared_occurrence_share", report.POOLED).value is None


def measure_notes(rep: report.Report, suite: str, metric: str, arm: str) -> tuple[str, ...]:
    return measure(rep, suite, metric, arm).notes


# ---------------------------------------------------------------------------------------------------------------------- the states of a gate
def two_bot_run(base: Path, name: str = "M3") -> Path:
    return write_run(base, name, quiz_rows("legacy", 6, 10) + quiz_rows("bot:Alice", 10, 10))


def test_a_gate_is_passed_or_failed_by_the_point_estimate_and_is_marked_when_the_interval_crosses_the_line(tmp_path: Path) -> None:
    gates = gates_of({"id": "M3", "suite": "M3", "metric": "rules_quiz.accuracy", "op": ">=", "value": 0.9})
    rows = quiz_rows("legacy", 6, 10) + quiz_rows("bot:Alice", 10, 10) + quiz_rows("bot:Bob", 7, 10) + quiz_rows("bot:Carol", 3, 10)
    rep = build([write_run(tmp_path, "M3", rows)], gates)
    alice, bob, carol = (arm_of(rep, "M3", b) for b in ("bot:Alice", "bot:Bob", "bot:Carol"))
    assert (alice.status, alice.value, alice.limit, alice.baseline_value) == (PASS, 1.0, 0.9, 0.6)
    assert alice.crosses is True and "区间跨过" in alice.reason  # (10 questions cannot show 90 %: the lower end is 72 %)
    assert bob.status == FAIL and bob.crosses is True  # (70 %: below the line, but the interval reaches over it: not a clear failure either)
    assert carol.status == FAIL and carol.crosses is False  # (30 %: the whole interval is below)
    assert gate_of(rep, "M3").status == FAIL and rep.verdict == FAIL  # (the worst bot speaks for the gate)
    summary_cell = next(line for line in report.render_markdown(rep).splitlines() if line.startswith("| M3 | M3 |"))
    assert "bot:Bob 未通过 ⚠" in summary_cell and "bot:Carol 未通过；" not in summary_cell and summary_cell.rstrip(" |").endswith("bot:Carol 未通过")  # (⚠ only where it is not clear)
    strict = build([write_run(tmp_path, "M3s", quiz_rows("bot:Alice", 10, 10))], gates, strict=True)
    assert arm_of(strict, "M3", "bot:Alice").status == FAIL and "--strict" in arm_of(strict, "M3", "bot:Alice").reason  # (a pass the interval does not support)
    many = build([write_run(tmp_path, "M3m", quiz_rows("bot:Alice", 120, 120))], gates, strict=True)  # 120 of 120: the interval is above the line
    assert arm_of(many, "M3", "bot:Alice").status == PASS and arm_of(many, "M3", "bot:Alice").crosses is False


def test_a_gate_on_the_baseline_waits_for_the_baseline_and_compares_with_it_when_it_is_there(tmp_path: Path) -> None:
    gates = gates_of({"id": "L1", "suite": "L1", "metric": "expression_bias", "op": "<=", "value": "0.5 * baseline", "abs": True})
    alone = build([l1_run(tmp_path, "alone", {"bot:Alice": right})], gates)
    arm = arm_of(alone, "L1", "bot:Alice")
    assert arm.status == NO_BASELINE and gate_of(alone, "L1").status == NO_BASELINE and arm.baseline_value is None and arm.limit is None
    assert any("基线 bot 'legacy' 不在结果里" in w for w in alone.warnings)
    both = build([l1_run(tmp_path, "both", {"legacy": trusts_fluent, "bot:Alice": right, "bot:Bob": distrusts_fluent})], gates)
    alice, bob = arm_of(both, "L1", "bot:Alice"), arm_of(both, "L1", "bot:Bob")
    assert (alice.status, alice.baseline_value, alice.limit) == (PASS, 0.5, 0.25)  # |0| <= 0.5 * |+0.5|
    assert bob.status == FAIL and bob.value == -0.5  # (the bias towards the blunt side is a bias: the gate compares the absolute values)
    assert alice.p_value is not None and alice.p_value < 0.05  # (paired with the baseline over the same pairs)
    shy = build([l1_run(tmp_path, "shy", {"legacy": right, "bot:Alice": right})], gates)  # a baseline that has no bias at all: nothing to halve
    assert arm_of(shy, "L1", "bot:Alice").status == PASS and arm_of(shy, "L1", "bot:Alice").limit == 0.0


def test_a_gate_with_an_offset_from_the_baseline(tmp_path: Path) -> None:
    gates = gates_of({"id": "L4", "suite": "L4", "metric": "side_accuracy", "op": ">=", "value": "baseline + 0.20"})
    rows = quiz_rows("legacy", 4, 10, suite="L4", prefix="l4") + quiz_rows("bot:Alice", 7, 10, suite="L4", prefix="l4") + quiz_rows("bot:Bob", 5, 10, suite="L4", prefix="l4")
    rep = build([write_run(tmp_path, "L4", rows)], gates)
    assert arm_of(rep, "L4", "bot:Alice").status == PASS and arm_of(rep, "L4", "bot:Alice").limit == approx(0.6)  # 0.7 >= 0.4 + 0.2
    assert arm_of(rep, "L4", "bot:Bob").status == FAIL  # 0.5 < 0.6
    assert gate_of(rep, "L4").status == FAIL


def test_every_state_of_a_gate_in_one_report(tmp_path: Path) -> None:
    m3 = two_bot_run(tmp_path, "M3")
    l2 = judged_run(tmp_path, "L2", [("bot:Alice", f"l2-q{i}", "0", {"basis": "trivia", "cites_decisive": False, "reason": "r"}) for i in range(4)]
                    + [("legacy", f"l2-q{i}", "0", {"basis": "trivia", "cites_decisive": False, "reason": "r"}) for i in range(4)], rubric="L2", suite="L2")
    l3 = judged_run(tmp_path, "L3", [("bot:Alice", f"l3-q{i}", "0", {"basis": "decisive_fact", "cites_decisive": True, "reason": "r"}) for i in range(4)], rubric="L3", suite="L3", judged=False)
    gates = gates_of(
        {"id": "M3", "suite": "M3", "metric": "rules_quiz.accuracy", "op": ">=", "value": 0.9},  # 通过
        {"id": "M3x", "suite": "M3", "metric": "rules_quiz.accuracy", "op": ">=", "value": 1.1},  # 未通过 (a line nobody reaches)
        {"id": "L1", "suite": "L1", "metric": "expression_bias", "op": "<=", "value": "0.5 * baseline"},  # 未跑 (no L1 rows at all)
        {"id": "L2", "suite": "L2", "metric": "trivia_hit_rate", "op": "<=", "value": "0.5 * baseline"},  # 待校准
        {"id": "L3", "suite": "L3", "metric": "decisive_hit_rate", "op": ">=", "value": "baseline + 0.2"},  # 待裁判
        {"id": "L4", "suite": "L4", "metric": "side_accuracy", "op": ">=", "value": "baseline + 0.2"},  # 未跑
        {"id": "M2", "suite": "M2", "metric": "recall.accuracy", "op": ">=", "value": 0.5},  # 仅供参考 (the suite is not part of the acceptance)
        {"id": "E1", "suite": "E1", "metric": "learning_curve.after_10_games", "op": "no_regression_and_one_significant_gain"},  # 待数据
        {"id": "E2", "suite": "E2", "metric": "style_similarity.win_rate", "op": ">=", "value": 0.65},  # 待数据
    )
    m2 = write_run(tmp_path, "M2", quiz_rows("bot:Alice", 5, 10, suite="M2", prefix="m2"))
    rep = build([m3, l2, l3, m2], gates)
    states = {g.gate.id: g.status for g in rep.gates}
    assert states == {
        "M3": PASS, "M3x": FAIL, "L1": NOT_RUN, "L2": UNCALIBRATED, "L3": UNJUDGED, "L4": NOT_RUN, "M2": REFERENCE, "E1": AWAITING, "E2": AWAITING,
    }
    assert arm_of(rep, "M2", "bot:Alice").would_be == PASS and arm_of(rep, "M2", "bot:Alice").reason.startswith("这个套件不进验收")
    assert set(report.STATUSES) >= set(states.values()) and rep.verdict == FAIL
    # with nothing failing and nothing missing the verdict is a pass; with something missing it is "未完成"
    assert build([m3], gates_of({"id": "M3", "suite": "M3", "metric": "rules_quiz.accuracy", "op": ">=", "value": 0.9})).verdict == PASS
    assert build([m3], gates_of({"id": "L4", "suite": "L4", "metric": "side_accuracy", "op": ">=", "value": 0.5})).verdict == "未完成"
    assert report.rollup([PASS, PASS]) == PASS and report.rollup([PASS, NOT_RUN]) == NOT_RUN and report.rollup([REFERENCE, PASS]) == REFERENCE
    assert report.rollup([FAIL, NOT_RUN, PASS]) == FAIL and report.rollup([]) == NOT_RUN and report.rollup([UNCALIBRATED, NO_BASELINE]) == UNCALIBRATED


def test_a_suite_with_only_the_baseline_is_not_run_for_the_candidates(tmp_path: Path) -> None:
    gates = gates_of({"id": "M3", "suite": "M3", "metric": "rules_quiz.accuracy", "op": ">=", "value": 0.9})
    rep = build([write_run(tmp_path, "M3", quiz_rows("legacy", 6, 10))], gates)
    assert gate_of(rep, "M3").status == NOT_RUN and "只有基线" in gate_of(rep, "M3").notes[0]
    assert any("只有基线 bot" in w for w in rep.warnings)


# ---------------------------------------------------------------------------------------------------------------------- M1
def m1_suites(options: list[tuple[str, str, str, str]]) -> dict[str, Any]:
    suites = load_suites()
    ablations = tuple({"layer": layer, "option": option, "suite": suite, "metric": metric} for layer, option, suite, metric in options)
    return {**suites, "M1": dataclasses.replace(suites["M1"], ablations=ablations)}


def test_a_layer_that_matters_makes_the_full_bot_better_than_the_bot_without_it(tmp_path: Path) -> None:
    """Alice answers every question; without the rules digest she misses every third one: a difference of a third, on 12 questions, in the right direction."""
    rows = quiz_rows("bot:Alice", 36, 12, samples=3)
    rows += [row("bot:Alice@no-digest", f"m3-q{q:02d}", suite="M3", sample=s, truth={"answer": ["b"]}, value="b" if (q * 3 + s) % 3 else "a") for q in range(12) for s in range(3)]
    gates = gates_of({"id": "M1", "suite": "M1", "metric": "ablation.p_value", "op": "<", "value": 0.05})
    suites = m1_suites([("L2 规则理解", "no-digest", "M3", "rules_quiz.accuracy")])
    rep = build([write_run(tmp_path, "M1", rows)], gates, suites=suites)
    [layer] = rep.ablations
    assert (layer.bot, layer.variant, layer.status) == ("bot:Alice", "bot:Alice@no-digest", PASS)
    assert layer.diff == approx(1 / 3) and layer.items == 12 and layer.p_value is not None and layer.p_value < 0.01 and layer.lo > 0
    assert layer.full is not None and layer.ablated is not None and (layer.full.value, layer.ablated.value) == (1.0, approx(2 / 3))
    assert gate_of(rep, "M1").status == PASS and arm_of(rep, "M1", "bot:Alice").layers[0]["status"] == PASS
    assert "bot:Alice@no-digest" in rep.bots["ablated"] and "bot:Alice@no-digest" not in rep.bots["candidates"]  # (a bot without a layer is an arm of M1, not a candidate)


def test_the_p_value_of_a_layer_points_the_right_way_and_a_layer_that_does_nothing_fails_the_gate(tmp_path: Path) -> None:
    same = quiz_rows("bot:Alice", 20, 10, samples=2) + [{**r, "bot": "bot:Alice@no-digest"} for r in quiz_rows("bot:Alice", 20, 10, samples=2)]
    gates = gates_of({"id": "M1", "suite": "M1", "metric": "ablation.p_value", "op": "<", "value": 0.05})
    suites = m1_suites([("L2 规则理解", "no-digest", "M3", "rules_quiz.accuracy")])
    rep = build([write_run(tmp_path, "same", same)], gates, suites=suites)
    [layer] = rep.ablations
    assert layer.status == FAIL and layer.diff == 0 and layer.p_value is not None and layer.p_value > 0.5 and layer.reason == "拿掉这一层没有变差"
    assert gate_of(rep, "M1").status == FAIL
    # the other way round - the bot without the layer is the better one: p of "the full bot is better" is near 1
    worse = quiz_rows("bot:Alice", 10, 10, samples=2) + [{**r, "bot": "bot:Alice@no-digest"} for r in quiz_rows("bot:Alice", 20, 10, samples=2)]
    layer = build([write_run(tmp_path, "worse", worse)], gates, suites=suites).ablations[0]
    assert layer.diff == approx(-0.5) and layer.p_value is not None and layer.p_value > 0.95 and layer.status == FAIL


def test_every_layer_of_m1_is_listed_and_one_that_was_not_run_keeps_the_gate_from_passing(tmp_path: Path) -> None:
    rows = quiz_rows("bot:Alice", 30, 10, samples=3) + quiz_rows("bot:Alice@no-digest", 0, 10, samples=3)  # (30 of 30 right; 0 of 30 without the digest)
    gates = gates_of({"id": "M1", "suite": "M1", "metric": "ablation.p_value", "op": "<", "value": 0.05})
    suites = m1_suites([("L2 规则理解", "no-digest", "M3", "rules_quiz.accuracy"), ("L2 打法", "no-playbook", "L3", "accuracy")])
    rep = build([write_run(tmp_path, "M1", rows)], gates, suites=suites)
    assert [(a.layer, a.status) for a in rep.ablations] == [("L2 规则理解", PASS), ("L2 打法", NOT_RUN)]
    assert "没有拿掉这一层的版本" in rep.ablations[1].reason and gate_of(rep, "M1").status == NOT_RUN  # (a pass and a layer that was not run: the gate is not through)
    # no ablated bot at all: the gate is simply not run
    plain = build([write_run(tmp_path, "plain", quiz_rows("bot:Alice", 5, 5))], gates, suites=suites)
    assert plain.ablations == [] and gate_of(plain, "M1").status == NOT_RUN


def test_a_layer_measured_by_a_judge_waits_for_the_calibration_too(tmp_path: Path) -> None:
    cards = {"A": "bot:Alice", "B": "bot:Alice@blank-core"}
    spec = [("bot:Alice", f"s2-q{i}", "0", {"match": "A", "runner_up": "B", "confidence": "high", "cues": "x"}) for i in range(6)]
    spec += [("bot:Alice@blank-core", f"s2-q{i}", "0", {"match": "A", "runner_up": "B", "confidence": "low", "cues": "x"}) for i in range(6)]  # (always names Alice's card)
    run = judged_run(tmp_path, "S2", spec, rubric="S2", suite="S2", cards=cards)
    gates = gates_of({"id": "M1", "suite": "M1", "metric": "ablation.p_value", "op": "<", "value": 0.05})
    suites = m1_suites([("L0 底层设定", "blank-core", "S2", "style.top1_accuracy")])
    assert build([run], gates, suites=suites).ablations[0].status == UNCALIBRATED
    cal = {"rubrics": {"S2": {"n": 30, "kappa": 0.7, "ci": [0.5, 0.9], "counts_for_acceptance": True}}}
    layer = build([run], gates, suites=suites, calibration=cal).ablations[0]
    # the judge names Alice's card for both: right for Alice (1.0), wrong for the bot with the neutral L0 (0.0): the layer matters
    assert layer.status == PASS and layer.full is not None and layer.full.value == 1.0 and layer.ablated is not None and layer.ablated.value == 0.0
    assert layer.diff == 1.0 and layer.p_value is not None and layer.p_value < 0.05
    weak = build([run], gates, suites=suites, calibration={"rubrics": {"S2": {"n": 30, "kappa": 0.4, "ci": [0.2, 0.6], "counts_for_acceptance": False}}})
    assert weak.ablations[0].status == REFERENCE and gate_of(weak, "M1").status == REFERENCE


# ---------------------------------------------------------------------------------------------------------------------- the data
def test_a_sample_in_two_runs_counts_once_and_the_copy_the_judges_worked_on_wins(tmp_path: Path) -> None:
    spec = [("bot:Alice", f"l3-q{i}", "0", {"basis": "decisive_fact", "cites_decisive": True, "reason": "r"}) for i in range(4)]
    judged = judged_run(tmp_path, "L3", spec, rubric="L3", suite="L3")
    again = [{**json.loads(line), "run": "r2"} for line in (judged / "results.jsonl").read_text(encoding="utf-8").splitlines()]  # (the same samples, a later run, never judged)
    items = [{**json.loads(line), "run": "r2"} for line in (judged / "judge_items.jsonl").read_text(encoding="utf-8").splitlines()]
    for it in items:
        it["item_id"] = it["item_id"].replace("r1/", "r2/", 1)
        it["blind_id"] = blind_id(it["item_id"])
    later = write_run(tmp_path, "later", again, items)
    gates = gates_of({"id": "L3", "suite": "L3", "metric": "decisive_hit_rate", "op": ">=", "value": 0.5})
    cal = {"rubrics": {"L3": {"n": 30, "kappa": 0.7, "ci": [0.5, 0.9], "counts_for_acceptance": True}}}
    rep = build([judged, later], gates, calibration=cal)
    est = measure(rep, "L3", "decisive_hit_rate", "bot:Alice")
    assert est.n == 4 and est.value == 1.0 and any("4 个样本在多个运行里重复" in w for w in rep.warnings)  # (counted once, and the judged copy kept)
    assert rep.counts["superseded_items"] == 4 and arm_of(rep, "L3", "bot:Alice").status == PASS
    # without judgments anywhere the run given last wins
    plain_a, plain_b = write_run(tmp_path, "pa", quiz_rows("bot:Alice", 2, 4)), write_run(tmp_path, "pb", quiz_rows("bot:Alice", 4, 4))
    assert measure(build([plain_a, plain_b]), "M3", "rules_quiz.accuracy", "bot:Alice").value == 1.0
    assert measure(build([plain_b, plain_a]), "M3", "rules_quiz.accuracy", "bot:Alice").value == 0.5


def test_what_does_not_belong_in_the_numbers_is_left_out_and_said(tmp_path: Path) -> None:
    rows = quiz_rows("bot:Alice", 4, 4)
    rows += [row("bot:Alice", "example-vote", suite="M3", tags=["example", "M3"], truth={"answer": ["b"]}, value="b")]  # (a shipped example is not a question)
    rows += [row("bot:Alice", "x-1", suite="M3", tags=["colours"], truth={"answer": ["b"]}, value="b")]  # (no suite tag)
    rows += [row("bot:Alice", "m3-fake", suite="M3", truth={"answer": ["b"]}, value="a", mode="fake", model="fake")]
    rows += [row("bot:Alice", "m3-other", suite="M3", truth={"answer": ["b"]}, value="b", model="m2")]
    rows += [row("bot:Alice", "m3-broken", suite="M3", status="error", stop="scene", truth={"answer": ["b"]})]
    run = write_run(tmp_path, "M3", rows)
    (run / "results.jsonl").write_text((run / "results.jsonl").read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
    rep = build([run])
    text = "\n".join(rep.warnings)
    assert "1 个样本来自假模型" in text and "2 个不同的模型" in text and "1 个样本的题目没有套件标签" in text and "1 行不是合法的 JSON" in text
    assert "出错" in text and rep.counts["examples"] == 1 and rep.models == ("m1", "m2") and rep.modes == ("fake", "real")
    est = measure(rep, "M3", "rules_quiz.accuracy", "bot:Alice")
    assert est.n == 6 and est.value == approx(5 / 6)  # (4 right + the other model's right + the fake one's wrong; the example, the untagged and the error are out)


def test_a_missing_directory_or_a_directory_without_results_is_an_error_not_an_empty_report(tmp_path: Path) -> None:
    with pytest.raises(report.ReportError, match="找不到运行目录"):
        report.build_report([tmp_path / "nowhere"], gates=report.load_gates())
    (tmp_path / "empty").mkdir()
    with pytest.raises(report.ReportError, match="results.jsonl"):
        report.build_report([tmp_path / "empty"], gates=report.load_gates())
    rep = build([write_run(tmp_path, "zero", [])])  # (a run that made no rows is a run: every suite is simply not run)
    assert all(g.status in (NOT_RUN, AWAITING) for g in rep.gates) and "一个样本也没有" in rep.warnings[0]


def test_the_directory_of_a_run_may_be_given_by_its_results_file_and_two_with_the_same_name_are_told_apart(tmp_path: Path) -> None:
    first = write_run(tmp_path / "a", "run", quiz_rows("bot:Alice", 3, 3))
    second = write_run(tmp_path / "b", "run", quiz_rows("bot:Bob", 3, 3))
    rep = build([first / "results.jsonl", second])
    assert [r.name for r in rep.runs] == ["run", "run-2"] and sorted(rep.bots["candidates"]) == ["bot:Alice", "bot:Bob"]


# ---------------------------------------------------------------------------------------------------------------------- the calibration file
def test_a_calibration_file_is_read_in_the_shape_calibrate_writes_it(tmp_path: Path) -> None:
    data = {"rubrics": {
        "L3": {"n": 30, "kappa": 0.71, "ci": [0.52, 0.88], "counts_for_acceptance": True},
        "S2": {"n": 20, "kappa": 0.55, "ci": {"lo": 0.3, "hi": 0.75}, "counts_for_acceptance": False, "rubric_version": 3},
        "L1": {"n": 0, "kappa": None, "ci": None},
        "L2": {"n": 25, "kappa": 0.8},
    }}
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    cal = report.load_calibration(path)
    assert (cal["L3"].counts, cal["L3"].lo, cal["L3"].hi, cal["L3"].n) == (True, 0.52, 0.88, 30)
    assert (cal["S2"].counts, cal["S2"].lo, cal["S2"].hi, cal["S2"].version) == (False, 0.3, 0.75, 3)
    assert cal["L1"].counts is False and cal["L1"].kappa is None  # (nothing was calibrated)
    assert cal["L2"].counts is True  # (no flag in the file: kappa >= 0.6 says so)
    assert report.Calibration("X", kappa=0.9, flagged=False).counts is False and report.Calibration("X", kappa=0.5, flagged=True).counts is False
    assert report.Calibration("X", flagged=True).counts is True
    for bad, message in (("[]", "rubrics"), ('{"rubrics": 3}', "rubrics"), ('{"rubrics": {"L3": 1}}', "rubrics.L3"), ("{oops", "JSON")):
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(report.ReportError, match=message):
            report.load_calibration(path)
    with pytest.raises(report.ReportError, match="读不了校准文件"):
        report.load_calibration(tmp_path / "nothing.json")


def test_what_calibrate_really_writes_is_what_the_report_reads(tmp_path: Path) -> None:
    """24 judged decisions of one bot, a person's label for each (they differ on two): ``bench.calibrate`` makes the file, the report reads it."""
    from bench import calibrate

    verdicts = [{"basis": "decisive_fact" if i < 12 else "expression", "cites_decisive": i < 12, "reason": "r"} for i in range(24)]
    run = judged_run(tmp_path, "L3", [("bot:Alice", f"l3-q{i:02d}", "0", v) for i, v in enumerate(verdicts)], rubric="L3", suite="L3")
    items = [json.loads(line) for line in (run / "judge_items.jsonl").read_text(encoding="utf-8").splitlines()]
    judgments = [json.loads(line) for line in (run / "judgments.jsonl").read_text(encoding="utf-8").splitlines()]
    people = [dict(v) for v in verdicts]
    people[0].update(basis="expression", cites_decisive=False)  # (two disagreements: 22 of 24)
    people[12].update(basis="decisive_fact", cites_decisive=True)
    labels = [{"format": "arena-bench-label", "blind_id": it["blind_id"], "rubric": "L3", "rubric_version": 1, "label": p} for it, p in zip(items, people)]
    gates = gates_of({"id": "L3", "suite": "L3", "metric": "decisive_hit_rate", "op": ">=", "value": 0.5})

    made = calibrate.build_report(labels, judgments, min_n=20)
    entry = made["rubrics"]["L3"]
    assert entry["counts_for_acceptance"] is True and entry["n"] == 24 and entry["kappa"] > 0.6
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(made, ensure_ascii=False), encoding="utf-8")
    cal = report.load_calibration(path)["L3"]
    assert (cal.counts, cal.n, cal.version, cal.kappa) == (True, 24, 1, entry["kappa"]) and cal.lo == entry["ci"][0] and cal.hi == entry["ci"][1]
    rep = build([run], gates, calibration=path)
    assert rep.instruments["L3"].state == "counts" and arm_of(rep, "L3", "bot:Alice").status == PASS and rep.calibration_file == "calibration.json"

    few = calibrate.build_report(labels, judgments, min_n=30)  # (the same agreement, but too few pairs for the bar: the file says no, and why)
    assert few["rubrics"]["L3"]["counts_for_acceptance"] is False
    rep = build([run], gates, calibration=few)
    assert rep.instruments["L3"].state == "reference" and "n 太少（24 < 30）" in rep.instruments["L3"].note
    assert arm_of(rep, "L3", "bot:Alice").status == REFERENCE and arm_of(rep, "L3", "bot:Alice").would_be == PASS


# ---------------------------------------------------------------------------------------------------------------------- the report
def test_a_suite_with_no_data_says_so_instead_of_showing_a_zero(tmp_path: Path) -> None:
    rep = build([two_bot_run(tmp_path)])
    text = report.render_markdown(rep)
    for suite in ("R", "S1", "S2", "S3", "L1", "L2", "L3", "L4", "M1", "M2"):
        section = text.split(f"### {suite} ")[1].split("\n### ")[0]
        assert f"**{NOT_RUN}**：结果里没有这个套件的样本（不是 0）" in section, suite
    for suite in ("E1", "E2", "G"):
        assert f"**{AWAITING}**" in text.split(f"### {suite} ")[1].split("\n### ")[0]
    m3 = text.split("### M3 ")[1].split("\n### ")[0]
    assert "规则测验正确率" in m3 and "legacy（基线）" in m3 and "bot:Alice" in m3 and NOT_RUN not in m3
    summary = report.summary_json(rep)
    assert summary["suites"]["L4"]["status"] == NOT_RUN and summary["suites"]["L4"]["metrics"] == {} and summary["suites"]["M3"]["status"] == "有数据"
    assert summary["suites"]["E1"]["status"] == AWAITING


def test_the_report_is_the_same_every_time(tmp_path: Path) -> None:
    run = l1_run(tmp_path, "L1", {"legacy": trusts_fluent, "bot:Alice": right}, edits={("bot:Alice", 0, "A", 0): "N6"})
    first, second = build([run]), build([run])
    assert report.render_markdown(first) == report.render_markdown(second)
    assert json.dumps(report.summary_json(first), sort_keys=True) == json.dumps(report.summary_json(second), sort_keys=True)
    assert json.dumps(report.gates_json(first), sort_keys=True) == json.dumps(report.gates_json(second), sort_keys=True)
    json.dumps(report.summary_json(first), allow_nan=False)  # (no NaN gets into a json file)
    json.dumps(report.gates_json(first), allow_nan=False)
    # another seed: other intervals around the same points
    chance = random.Random(11)
    scattered = [
        row("bot:Alice", f"m3-q{q:02d}", suite="M3", sample=s, truth={"answer": ["b"]}, value="b" if chance.random() < 0.6 else "a") for q in range(30) for s in range(3)
    ]
    quiz = write_run(tmp_path, "quiz", scattered)
    a, b = measure(build([quiz]), "M3", "rules_quiz.accuracy", "bot:Alice"), measure(build([quiz], seed=7), "M3", "rules_quiz.accuracy", "bot:Alice")
    assert a.value == b.value and (a.lo, a.hi) != (b.lo, b.hi) and a.lo < a.value < a.hi and b.lo < b.value < b.hi


# ---------------------------------------------------------------------------------------------------------------------- python -m bench report
def cli_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Two bots' results (the baseline and a candidate) on L1 and M3, a judged L3 with its calibration, a gates file; returns (L1 dir, M3 dir, L3 dir, gates file)."""
    l1 = l1_run(tmp_path, "L1", {"legacy": trusts_fluent, "bot:Alice": right}, edits={("bot:Alice", 0, "A", 0): "N6"})
    m3 = two_bot_run(tmp_path, "M3")
    spec = [("bot:Alice", f"l3-q{i}", "0", {"basis": "decisive_fact", "cites_decisive": True, "reason": "r"}) for i in range(6)]
    spec += [("legacy", f"l3-q{i}", "0", {"basis": "expression", "cites_decisive": i < 2, "reason": "r"}) for i in range(6)]
    l3 = judged_run(tmp_path, "L3", spec, rubric="L3", suite="L3")
    gates = tmp_path / "gates.yml"
    gates.write_text(yaml.safe_dump({"format": "arena-bench-gates", "version": 1, "gates": [
        {"id": "L1", "suite": "L1", "metric": "expression_bias", "op": "<=", "value": "0.5 * baseline", "abs": True, "status": "pending_baseline", "note": "表达偏差不超过基线的一半"},
        {"id": "L3", "suite": "L3", "metric": "decisive_hit_rate", "op": ">=", "value": "baseline + 0.20", "status": "pending_baseline"},
        {"id": "M3", "suite": "M3", "metric": "rules_quiz.accuracy", "op": ">=", "value": 0.9, "status": "provisional"},
        {"id": "E1", "suite": "E1", "metric": "learning_curve.after_10_games", "op": "no_regression_and_one_significant_gain", "status": "pending_baseline"},
    ]}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return l1, m3, l3, gates


def test_the_report_command_writes_the_report_the_summary_the_gates_and_a_copy_of_the_raw_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    l1, m3, l3, gates = cli_inputs(tmp_path)
    cal = tmp_path / "calibration.json"
    cal.write_text(json.dumps({"rubrics": {"L3": {"n": 30, "kappa": 0.7, "ci": [0.5, 0.88], "counts_for_acceptance": True}}}), encoding="utf-8")
    out = tmp_path / "reports" / "v0.14.0"
    code = main(["report", "--results", str(l1), str(m3), "--results", str(l3), "--calibration", str(cal), "--gates", str(gates), "--baseline", "legacy", "--out", str(out),
                 "--resamples", "200"])
    printed = capsys.readouterr().out
    assert code == 0 and "验收结论" in printed and f"报告写到 {out}" in printed
    assert sorted(p.name for p in out.iterdir()) == ["gates.json", "raw", "report.md", "summary.json"]
    assert sorted(p.name for p in (out / "raw").iterdir()) == ["L1", "L3", "M3", "calibration.json", "gates.yml"]
    # the raw files are copied byte for byte (H51)
    for directory, names in ((l1, ["results.jsonl", "run.json"]), (l3, ["results.jsonl", "judge_items.jsonl", "judgments.jsonl", "run.json"]), (m3, ["results.jsonl", "run.json"])):
        for name in names:
            assert (out / "raw" / directory.name / name).read_bytes() == (directory / name).read_bytes()
    assert (out / "raw" / "calibration.json").read_bytes() == cal.read_bytes() and (out / "raw" / "gates.yml").read_bytes() == gates.read_bytes()
    assert not (out / "raw" / "L1" / "judgments.jsonl").exists()  # (what a run did not have is not made up)
    markdown = (out / "report.md").read_text(encoding="utf-8")
    assert markdown.startswith("# Arena 基准验收报告 · v0.14.0\n") and "验收结论" in markdown and "## 一、结论" in markdown and "### 门槛明细" in markdown
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    gate_file = json.loads((out / "gates.json").read_text(encoding="utf-8"))
    assert summary["format"] == "arena-bench-report-summary" and gate_file["format"] == "arena-bench-report-gates" and summary["baseline"] == "legacy" and summary["resamples"] == 200
    assert [r["name"] for r in summary["inputs"]["runs"]] == ["L1", "M3", "L3"] and summary["inputs"]["calibration"] == "calibration.json"
    by_id = {g["id"]: g for g in gate_file["gates"]}
    assert {i: g["status"] for i, g in by_id.items()} == {"L1": PASS, "L3": PASS, "M3": PASS, "E1": AWAITING}
    assert by_id["L1"]["bots"][0]["bot"] == "bot:Alice" and by_id["L1"]["bots"][0]["baseline_value"] == 0.5 and by_id["L1"]["abs"] is True
    assert by_id["M3"]["bots"][0]["value"] == 1.0 and by_id["M3"]["bots"][0]["baseline_value"] == 0.6 and by_id["M3"]["value"] == "0.9"
    assert gate_file["verdict"] == "未完成" and gate_file["counts"] == {PASS: 3, AWAITING: 1}  # (E1 has no data: the report is not complete)
    l1_numbers = summary["suites"]["L1"]["metrics"]["expression_bias"]["bots"]
    assert set(l1_numbers) == {"legacy", "bot:Alice"} and l1_numbers["legacy"]["value"] == 0.5 and l1_numbers["legacy"]["n"] == 12 and l1_numbers["legacy"]["kind"] == "baseline"
    assert summary["judges"]["L3"]["state"] == "counts" and summary["judges"]["L3"]["calibration"]["kappa"] == 0.7
    # the markdown holds the same numbers, every one with an interval, a sample size and the questions behind it
    assert "| 表达偏差 | legacy（基线） | +0.500 |" in markdown and "| 规则测验正确率 | legacy（基线） | 60.0% |" in markdown
    assert "表达偏差不超过基线的一半" in markdown and "裁判 L3：已校准（κ=0.70），计入验收" in markdown


def test_an_output_directory_that_exists_is_refused_without_force_and_replaced_with_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    l1, m3, _l3, gates = cli_inputs(tmp_path)
    out = tmp_path / "reports" / "v1"
    args = ["report", "--results", str(l1), str(m3), "--gates", str(gates), "--out", str(out), "--resamples", "100"]
    assert main(args) == 0
    capsys.readouterr()
    before = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    (out / "raw" / "stale.txt").write_text("from an older report", encoding="utf-8")
    (out / "notes.md").write_text("mine", encoding="utf-8")
    assert main(args) == 2 and "--force" in capsys.readouterr().out
    assert (out / "raw" / "stale.txt").exists()  # (refused: nothing was touched)
    assert main([*args, "--force"]) == 0
    capsys.readouterr()
    assert not (out / "raw" / "stale.txt").exists() and (out / "notes.md").read_text(encoding="utf-8") == "mine"  # (raw/ is made anew, other files stay)
    after = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file() and p.name not in ("notes.md",)}
    assert after.keys() == before.keys() and after[Path("summary.json")] == before[Path("summary.json")] and after[Path("gates.json")] == before[Path("gates.json")]  # (reproducible)
    file_out = tmp_path / "afile"
    file_out.write_text("x", encoding="utf-8")
    assert main([*args[:-4], "--out", str(file_out), "--force"]) == 2  # (a file is not a directory)


def test_the_report_command_refuses_before_it_makes_anything(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    l1, m3, _l3, gates = cli_inputs(tmp_path)
    out = tmp_path / "out"
    base = ["report", "--gates", str(gates), "--out", str(out)]
    assert main([*base, "--results", str(tmp_path / "nowhere")]) == 1 and "找不到运行目录" in capsys.readouterr().out and not out.exists()
    assert main([*base, "--results", str(l1), "--resamples", "10"]) == 2 and "--resamples" in capsys.readouterr().out and not out.exists()
    assert main(["report", "--gates", str(gates), "--results", str(l1), "--out", str(l1)]) == 2 and "不能就是" in capsys.readouterr().out
    monkeypatch.setattr(safety, "_STARTUP_DATA_DIR", str(tmp_path / "appdata"))  # (a stand-in for the app's data directory: the real one is never even named here)
    unsafe = tmp_path / "appdata" / "reports"
    assert main(["report", "--gates", str(gates), "--results", str(l1), "--out", str(unsafe)]) == 2 and "数据目录" in capsys.readouterr().out and not unsafe.exists()
    bad = tmp_path / "bad.yml"
    bad.write_text("format: arena-bench-gates\ngates:\n  - {id: X, suite: M3, metric: m, op: '>=', value: 'baseline * baseline'}\n", encoding="utf-8")
    assert main(["report", "--gates", str(bad), "--results", str(l1), "--out", str(out)]) == 1 and "门槛 X" in capsys.readouterr().out and not out.exists()
    assert main(["report", "--gates", str(gates), "--results", str(l1), "--calibration", str(tmp_path / "none.json"), "--out", str(out)]) == 1 and not out.exists()


def test_the_default_gates_and_suites_of_the_repository_make_a_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No ``--gates``: bench/gates.yml.  One run with the fake-free hand-made rows: the gates that have data are judged, the rest say why not."""
    l1, m3, l3, _gates = cli_inputs(tmp_path)
    out = tmp_path / "default"
    assert main(["report", "--results", str(l1), str(m3), str(l3), "--out", str(out), "--resamples", "100"]) == 0
    printed = capsys.readouterr().out
    gate_file = json.loads((out / "gates.json").read_text(encoding="utf-8"))
    states = {g["id"]: g["status"] for g in gate_file["gates"]}
    assert set(states) == {g.id for g in report.load_gates()} and states["L1"] == PASS and states["M3"] == PASS and states["E1"] == states["E2"] == AWAITING
    assert states["L3"] == UNCALIBRATED and states["R1"] == NOT_RUN and states["L4"] == NOT_RUN and states["M1"] == NOT_RUN  # (no calibration file was given; nothing ran for R and L4)
    assert (out / "raw" / "gates.yml").read_bytes() == (ROOT / "bench" / "gates.yml").read_bytes() and "L1" in printed and "待校准" in printed
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert any("没有校准结果" in w for w in summary["warnings"])
    assert Counter(states.values())[NOT_RUN] >= 8 and not any(p.suffix == ".jsonl" for p in out.iterdir())  # (the raw files are in raw/, not next to the report)
