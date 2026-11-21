"""bench.calibrate: the sample a person labels (``calibration-set``) and the agreement of the judges with the labels (``calibrate``), H48.

No model is called.  The kappa numbers are worked out by hand in the tests (and compared with ``bench.stats``, whose formulas ``calibrate`` uses)."""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import shlex
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from bench import calibrate, stats
from bench.__main__ import main
from bench.calibrate import CalibrationError, build_report, build_set, calibrate_rubric, category_of, draw_order, guide_of, label_schema
from bench.judging import JUDGMENT_FORMAT, VERDICTS_FORMAT, blind_view_of, load_rubrics, merge_verdicts, validate_verdict
from calibration_helpers import (
    BOT_NAMES, TS, VALUES, from_matrix, judged_items, judgment_of, label_row, make_e2, make_item, make_s2, read_jsonl_file, verdict_l3, write_jsonl,
)

RUBRICS = load_rubrics()


def entry_of(labels: list[dict[str, Any]], judgments: list[dict[str, Any]], rubric: str = "L3", **options: Any) -> dict[str, Any]:
    return build_report(labels, judgments, **options)["rubrics"][rubric]


# ------------------------------------------------------------------------------------------------------------------------- calibration-set: a row
def test_a_row_of_the_set_has_the_documented_keys_and_shows_what_a_judge_is_shown() -> None:
    items = [make_item(n) for n in range(40)]
    result = build_set(items, n=10)
    assert len(result.rows) == 10
    rubric = RUBRICS["L3"]
    by_blind = {item["blind_id"]: item for item in items}
    for row in result.rows:
        assert list(row) == ["format", "version", "blind_id", "rubric", "rubric_version", "title", "kind", "primary", "schema", "guide", "view"]
        assert (row["format"], row["version"], row["rubric"], row["rubric_version"]) == ("arena-bench-label-item", 1, "L3", rubric.version)
        assert (row["title"], row["kind"], row["primary"]) == (rubric.title, rubric.kind, rubric.primary)
        assert row["schema"] == rubric.schema
        assert row["view"] == blind_view_of(by_blind[row["blind_id"]], rubric), "a person sees exactly what a judge sees"
        assert row["view"]["subject"].keys() == set(rubric.subject)


def test_the_guide_is_the_how_to_judge_part_of_the_rubric_without_its_title_and_examples() -> None:
    for rubric in RUBRICS.values():
        guide = guide_of(rubric)
        assert guide and not guide.startswith("#") or guide.startswith("##"), rubric.id
        assert "```" not in guide and "### 示例" not in guide and "\n## 示例" not in guide, f"{rubric.id}: the worked examples stay in the rubric file"
        assert "怎么判" in guide, rubric.id
    assert guide_of(RUBRICS["L3"]).splitlines()[0].startswith("你在评"), "the title line is gone, the text starts"
    assert guide_of(RUBRICS["S2"]).count("# S2") == 0
    bare = RUBRICS["L3"].__class__(**{**RUBRICS["L3"].__dict__, "body": "只有正文，没有示例。"})
    assert guide_of(bare) == "只有正文，没有示例。"


def test_nothing_that_names_a_bot_a_run_a_situation_the_truth_or_a_verdict_is_in_the_set() -> None:
    items, judgments = judged_items({"decisive_fact": 12, "trivia": 12, "expression": 12})
    items += [make_s2(500 + n) for n in range(10)]
    judgments += [judgment_of(item, {"match": "A", "runner_up": "B", "confidence": "high", "cues": "JUDGE-SAYS-SECRET"}, rubric="S2") for item in items[-10:]]
    for judgment in judgments[:36]:
        judgment["verdict"]["reason"] = "JUDGE-SAYS-SECRET"
    result = build_set(items, judgments, n=8)
    assert len(result.rows) == 16
    forbidden_keys = {"item_id", "bot", "bot_label", "brain", "run", "situation", "sample", "truth", "judge", "judgment", "verdict", "attempts", "meta", "tags", "seat"}

    def keys_of(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {k for v in value.values() for k in keys_of(v)}
        if isinstance(value, list):
            return {k for v in value for k in keys_of(v)}
        return set()

    for row in result.rows:
        text = json.dumps(row, ensure_ascii=False)
        assert not keys_of(row) & forbidden_keys, keys_of(row) & forbidden_keys
        for secret in [*BOT_NAMES, "20261120-aaaa", "sit-", "JUDGE-SAYS-SECRET", "秘密思考", "x/", "correct", "no-people"]:
            assert secret not in text, secret
        assert set(row["view"]) == {"blind_id", "rubric", "title", "brief", "context", "subject"}


def test_an_item_whose_blind_view_names_a_bot_a_run_or_a_situation_is_dropped_with_a_note() -> None:
    items = [make_item(n) for n in range(10)]
    items[3] = make_item(3, brief="这是 bot:Bob 的局面", bot="bot:Bob")
    items[4] = make_item(4, title="题目 20261120-aaaa")
    items[5] = make_item(5, situation="sit-special", context={"decisive": "来自 sit-special 的事实", "trap": ""})
    result = build_set(items, n=30)
    assert {row["blind_id"] for row in result.rows} == {item["blind_id"] for n, item in enumerate(items) if n not in (3, 4, 5)}
    assert any("3 条的盲化内容里出现了 bot / 运行 / 题目的名字" in note and note.startswith("L3：") for note in result.notes)
    allleak = [make_item(n, brief="20261120-aaaa") for n in range(5)]
    assert build_set(allleak, n=3).rows == []
    assert any("没有可抽的条目" in note for note in build_set(allleak, n=3).notes)
    short = [make_item(n, run="t") for n in range(5)]
    assert len(build_set(short, n=3).rows) == 3, "a name of a few characters is not looked for: it would be in every text"


# ------------------------------------------------------------------------------------------------------------------------- calibration-set: the draw
def test_with_judgments_every_value_the_judge_gives_is_in_the_sample_and_a_small_value_is_taken_whole() -> None:
    items, judgments = judged_items({"decisive_fact": 100, "trivia": 8, "expression": 2})
    result = build_set(items, judgments, n=30)
    by_blind = {j["blind_id"]: j["verdict"]["basis"] for j in judgments}
    picked = Counter(by_blind[row["blind_id"]] for row in result.rows)
    assert picked == {"decisive_fact": 20, "trivia": 8, "expression": 2}, "the small values are taken whole, the big one makes up for it"
    assert result.summary["L3"]["strata"] == dict(sorted(picked.items()))
    assert result.summary["L3"]["stratified_by"] == "裁判的取值"
    assert (result.summary["L3"]["pool"], result.summary["L3"]["picked"]) == (110, 30)


def test_equal_strata_get_equal_shares_and_fewer_slots_than_strata_still_cover_distinct_values() -> None:
    items, judgments = judged_items({value: 40 for value in VALUES})
    by_blind = {j["blind_id"]: j["verdict"]["basis"] for j in judgments}
    assert Counter(by_blind[r["blind_id"]] for r in build_set(items, judgments, n=30).rows) == {value: 6 for value in VALUES}
    few = Counter(by_blind[r["blind_id"]] for r in build_set(items, judgments, n=3).rows)
    assert sorted(few.values()) == [1, 1, 1], "three slots, five values: three different values"
    uneven = Counter(by_blind[r["blind_id"]] for r in build_set(items, judgments, n=12).rows)
    assert sorted(uneven.values()) == [2, 2, 2, 3, 3], "twelve slots, five values: as even as it goes"


def test_inside_a_value_the_bots_and_the_situations_take_turns() -> None:
    items, judgments = [], []
    n = 0
    for bot in BOT_NAMES:
        for situation in ("s-a", "s-b", "s-c", "s-d"):
            for _ in range(3):
                item = make_item(n, bot=bot, situation=situation)
                items.append(item)
                judgments.append(judgment_of(item, verdict_l3("trivia")))
                n += 1
    by_blind = {item["blind_id"]: item for item in items}
    rows = build_set(items, judgments, n=12).rows
    chosen = [by_blind[r["blind_id"]] for r in rows]
    assert Counter(item["bot"] for item in chosen) == {bot: 4 for bot in BOT_NAMES}
    for bot in BOT_NAMES:
        assert len({item["situation"] for item in chosen if item["bot"] == bot}) == 4, "one question each, not four of the same"


def test_without_judgments_the_strata_are_the_bots_and_inside_a_bot_the_situations() -> None:
    items = [make_item(n, bot=BOT_NAMES[n % 3], situation=f"s-{(n // 3) % 4}") for n in range(60)]
    by_blind = {item["blind_id"]: item for item in items}
    result = build_set(items, n=9)
    chosen = [by_blind[r["blind_id"]] for r in result.rows]
    assert Counter(item["bot"] for item in chosen) == {bot: 3 for bot in BOT_NAMES}
    assert result.summary["L3"]["stratified_by"] == "bot"
    for bot in BOT_NAMES:
        assert len({item["situation"] for item in chosen if item["bot"] == bot}) == 3


def test_the_same_seed_gives_the_same_set_whatever_the_order_of_the_file_and_another_seed_another_one() -> None:
    items, judgments = judged_items({"decisive_fact": 30, "trivia": 30, "expression": 30})
    first = build_set(items, judgments, n=20, seed=7)
    again = build_set(list(reversed(items)), list(reversed(judgments)), n=20, seed=7)
    assert first.rows == again.rows, "same items, same seed: the same rows in the same order"
    assert build_set(items, judgments, n=20, seed=8).rows != first.rows
    assert [r["blind_id"] for r in build_set(items, judgments, n=20, seed=7, only=["L3"]).rows] == [r["blind_id"] for r in first.rows]
    shuffled = random.Random(3)
    mixed = list(items)
    shuffled.shuffle(mixed)
    assert build_set(mixed, judgments, n=20, seed=7).rows == first.rows


def test_a_rubric_s_sample_does_not_change_when_another_rubric_comes_along() -> None:
    l3_items = [make_item(n) for n in range(60)]
    alone = build_set(l3_items, n=10, seed=5)
    with_s2 = build_set(l3_items + [make_s2(1000 + n) for n in range(40)], n=10, seed=5)
    only_l3 = lambda result: sorted(r["blind_id"] for r in result.rows if r["rubric"] == "L3")  # noqa: E731
    assert only_l3(alone) == only_l3(with_s2)
    assert len(with_s2.rows) == 20 and {r["rubric"] for r in with_s2.rows} == {"L3", "S2"}


def test_the_order_of_the_set_does_not_tell_which_stratum_an_item_is_from() -> None:
    items, judgments = judged_items({"decisive_fact": 10, "trivia": 10, "expression": 10})
    by_blind = {j["blind_id"]: j["verdict"]["basis"] for j in judgments}
    values = [by_blind[row["blind_id"]] for row in build_set(items, judgments, n=30).rows]
    assert len(values) == 30 and values != sorted(values), "not grouped by what the judge said"
    assert sum(1 for a, b in zip(values, values[1:]) if a != b) > 12, "the values alternate (a random order of three equal groups changes value about 19 times in 29)"
    mixed = build_set([make_item(n) for n in range(10)] + [make_s2(100 + n) for n in range(10)], n=10).rows
    rubrics = [row["rubric"] for row in mixed]
    assert rubrics != sorted(rubrics), "the rubrics are mixed too"


def test_fewer_items_than_n_are_all_taken_and_the_note_says_so() -> None:
    items = [make_item(n) for n in range(18)] + [make_s2(100 + n) for n in range(40)]
    result = build_set(items, n=30)
    counts = Counter(row["rubric"] for row in result.rows)
    assert counts == {"L3": 18, "S2": 30}
    shortage = [note for note in result.notes if note.startswith("L3：")]
    assert len(shortage) == 1 and "只有 18 条可抽，不够 30 条：全取了" in shortage[0] and "至少要 20 对" in shortage[0]
    assert not any(note.startswith("S2：") for note in result.notes)
    assert result.summary["L3"]["picked"] == result.summary["L3"]["pool"] == 18


def test_only_items_with_a_current_judgment_can_be_drawn_when_judgments_are_given() -> None:
    items = [make_item(n) for n in range(30)]
    judgments = [judgment_of(items[n], verdict_l3()) for n in range(10)]  # judged
    judgments += [judgment_of(items[n], verdict_l3(), version=2) for n in range(10, 15)]  # judged with another version of the rubric
    judgments += [judgment_of(items[n], {"basis": "vibes"}) for n in range(15, 18)]  # not a verdict of the rubric
    result = build_set(items, judgments, n=30)
    assert {r["blind_id"] for r in result.rows} == {items[n]["blind_id"] for n in range(10)}
    notes = "\n".join(result.notes)
    assert "L3：15 条没有裁判的判定" in notes
    assert "L3：5 条的判定不是现行 rubric 版本（v1）做的" in notes
    nothing = build_set(items, [], n=5)
    assert nothing.rows == [] and any("没有一条有现行版本的判定" in note for note in nothing.notes)
    assert len(build_set(items, None, n=5).rows) == 5, "without judgments every item can be drawn"


def test_a_rubric_with_no_judgment_is_skipped_but_the_others_are_drawn() -> None:
    items = [make_item(n) for n in range(10)] + [make_s2(100 + n) for n in range(10)]
    judgments = [judgment_of(items[n], verdict_l3()) for n in range(10)]
    result = build_set(items, judgments, n=5)
    assert {row["rubric"] for row in result.rows} == {"L3"}
    assert any(note.startswith("S2：没有一条有现行版本的判定") for note in result.notes)


# ------------------------------------------------------------------------------------------------------------------------- calibration-set: S2 and E2
def test_an_s2_item_offers_its_cards_as_the_choices_and_the_rubric_is_left_alone() -> None:
    before = json.dumps(RUBRICS["S2"].schema, sort_keys=True)
    result = build_set([make_s2(n, cards=("A", "B", "C", "D")) for n in range(6)], n=4)
    for row in result.rows:
        properties = row["schema"]["properties"]
        assert properties["match"]["enum"] == ["A", "B", "C", "D"]
        assert properties["runner_up"]["enum"] == ["A", "B", "C", "D", "none"]
        assert "enum" not in properties["cues"] and properties["confidence"]["enum"] == ["high", "medium", "low"]
        assert row["schema"]["required"] == RUBRICS["S2"].schema["required"]
        assert row["view"]["subject"] == {"kind": "speech", "text": row["view"]["subject"]["text"]}, "S2 judges the words alone"
        assert [c["id"] for c in row["view"]["context"]["cards"]] == ["A", "B", "C", "D"]
        label = {"match": "C", "runner_up": "none", "confidence": "low", "cues": ""}
        assert validate_verdict(row["schema"], label) == [] and validate_verdict(RUBRICS["S2"].schema, label) == []
        assert validate_verdict(row["schema"], {**label, "match": "Z"}) != [], "a card that is not there is no answer"
    assert json.dumps(RUBRICS["S2"].schema, sort_keys=True) == before, "the rubric's own schema was not touched"
    assert label_schema(RUBRICS["L3"], {"context": {}}) == RUBRICS["L3"].schema
    assert label_schema(RUBRICS["S2"], {"context": {"cards": []}}) == RUBRICS["S2"].schema


def test_an_e2_item_shows_the_two_candidates_and_what_the_person_wrote() -> None:
    result = build_set([make_e2(n) for n in range(4)], n=2)
    row = result.rows[0]
    assert row["view"]["subject"].keys() == {"kind", "x", "y"} and row["view"]["subject"]["kind"] == "pair"
    assert row["view"]["context"]["reference"][0].startswith("我昨天就说了")
    assert row["schema"]["properties"]["closer"]["enum"] == ["X", "Y", "tie"] and row["primary"] == "closer"


# ------------------------------------------------------------------------------------------------------------------------- calibration-set: errors and the command
def test_wrong_input_is_an_error_that_says_what_to_fix() -> None:
    with pytest.raises(CalibrationError, match="--n 要 ≥ 1"):
        build_set([make_item(1)], n=0)
    with pytest.raises(CalibrationError, match="没有这些 rubric 的评分标准.*Q7"):
        build_set([make_item(1)], only=["L3", "Q7"])
    with pytest.raises(CalibrationError, match="没有 blind_id"):
        build_set([{**make_item(1), "blind_id": ""}])
    with pytest.raises(CalibrationError, match="rubric 'Q7' 没有对应的 bench/judges/Q7.md"):
        build_set([make_item(1, "Q7")])
    assert len(build_set([make_item(1), make_item(1)], n=5).rows) == 1, "the same row twice is one item"


def test_the_command_writes_the_set_and_prints_what_it_drew(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    items, judgments = judged_items({"decisive_fact": 30, "trivia": 30, "expression": 30})
    items_file, judgments_file = write_jsonl(tmp_path / "judge_items.jsonl", items), write_jsonl(tmp_path / "judgments.jsonl", judgments)
    out = tmp_path / "deep" / "set.jsonl"
    assert main(["calibration-set", "--items", str(items_file), "--judgments", str(judgments_file), "--n", "12", "--seed", "4", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "L3  v1  取了 12 / 90 条  分层（裁判的取值）：decisive_fact 4 · expression 4 · trivia 4" in text
    assert f"写到 {out}（共 12 条，顺序已打乱，种子 4）" in text and "bench/label/index.html" in text
    assert "提示：每个 rubric 只抽 12 条，低于校准默认要的 20 对" in text
    raw = out.read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw, "one JSON per line, \\n only (the same bytes on every system)"
    rows = read_jsonl_file(out)
    assert len(rows) == 12 and {row["format"] for row in rows} == {"arena-bench-label-item"}
    assert "决定性事实" in raw.decode("utf-8"), "Chinese is written as it is"
    assert calibrate.HANDLERS.keys() == {"calibration-set", "calibrate"}


def test_the_command_filters_rubrics_and_refuses_what_it_cannot_do(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    items = [make_item(n) for n in range(10)] + [make_s2(100 + n) for n in range(10)]
    items_file = write_jsonl(tmp_path / "items.jsonl", items)
    out = tmp_path / "set.jsonl"
    assert main(["calibration-set", "--items", str(items_file), "--n", "30", "--rubrics", "S2", "--out", str(out)]) == 0
    assert {row["rubric"] for row in read_jsonl_file(out)} == {"S2"}
    assert "提示：S2：只有 10 条可抽，不够 30 条" in capsys.readouterr().out
    for argv, message in (
        (["--rubrics", "Q7"], "没有这些 rubric 的评分标准"),
        (["--n", "0"], "--n 要 ≥ 1"),
        (["--items", str(tmp_path / "missing.jsonl")], "抽样失败"),
    ):
        args = ["calibration-set", "--items", str(items_file), "--out", str(tmp_path / "x.jsonl"), *argv]
        if "--items" in argv:
            args = ["calibration-set", *argv, "--out", str(tmp_path / "x.jsonl")]
        assert main(args) == 1, argv
        assert message in capsys.readouterr().out
    assert not (tmp_path / "x.jsonl").exists(), "a failed draw writes nothing"
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json\n", encoding="utf-8")
    assert main(["calibration-set", "--items", str(bad), "--out", str(tmp_path / "x.jsonl")]) == 1
    assert "第 1 行不是合法的 JSON" in capsys.readouterr().out
    leaking = write_jsonl(tmp_path / "leaking.jsonl", [make_item(n, brief="20261120-aaaa") for n in range(3)])
    assert main(["calibration-set", "--items", str(leaking), "--out", str(tmp_path / "x.jsonl")]) == 1
    assert "没有抽到任何条目" in capsys.readouterr().out


def test_draw_order_balances_the_strata_at_every_level() -> None:
    items = [("a", "x", i) for i in range(10)] + [("a", "y", i) for i in range(10)] + [("b", "x", i) for i in range(3)]
    order = draw_order(items, [lambda it: it[0], lambda it: it[1]], random.Random(1))
    assert len(order) == 23 and sorted(order) == sorted(items)
    head = order[:6]
    assert Counter(it[0] for it in head) == {"a": 3, "b": 3}
    assert Counter(it[1] for it in head if it[0] == "a") in ({"x": 2, "y": 1}, {"x": 1, "y": 2})
    assert draw_order([], [lambda it: it], random.Random(1)) == []


# ------------------------------------------------------------------------------------------------------------------------- calibrate: kappa
def test_perfect_agreement_is_kappa_one_and_counts_for_acceptance() -> None:
    labels, judgments = from_matrix({("decisive_fact", "decisive_fact"): 10, ("trivia", "trivia"): 10, ("expression", "expression"): 10})
    entry = entry_of(labels, judgments)
    assert (entry["n"], entry["kappa"], entry["agreement"]) == (30, 1.0, 1.0)
    assert entry["ci"][0] <= 1.0 <= entry["ci"][1] and entry["ci"][0] > 0.9
    assert entry["counts_for_acceptance"] is True and entry["reason"] == "通过（κ 1 ≥ 0.6，n 30 ≥ 20）"
    assert entry["confusion"] == {v: {w: 10 if v == w else 0 for w in ("decisive_fact", "expression", "trivia")} for v in ("decisive_fact", "expression", "trivia")}
    assert (entry["primary"], entry["rubric_version"], entry["unpaired_labels"], entry["unpaired_judgments"]) == ("basis", 1, 0, 0)


def test_complete_disagreement_is_a_negative_kappa() -> None:
    labels, judgments = from_matrix({("trivia", "expression"): 15, ("expression", "trivia"): 15})
    entry = entry_of(labels, judgments)
    assert entry["kappa"] == -1.0 and entry["agreement"] == 0.0
    assert entry["counts_for_acceptance"] is False and entry["reason"] == "κ 太低（-1 < 0.6）"
    assert entry["ci"][1] < 0
    flipped = from_matrix({("trivia", "expression"): 20, ("expression", "trivia"): 5, ("trivia", "trivia"): 2, ("expression", "expression"): 3})
    assert entry_of(*flipped)["kappa"] < 0


def test_a_two_by_two_worked_out_by_hand() -> None:
    # person \ judge     trivia  expression        observed 35/50 = 0.7;  chance: (25*30 + 25*20) / 50^2 = 0.5;  kappa = (0.7 - 0.5) / (1 - 0.5) = 0.4
    #   trivia             20        5
    #   expression         10       15
    entry = entry_of(*from_matrix({("trivia", "trivia"): 20, ("trivia", "expression"): 5, ("expression", "trivia"): 10, ("expression", "expression"): 15}))
    assert entry["n"] == 50 and entry["kappa"] == 0.4 and entry["agreement"] == 0.7
    assert entry["confusion"] == {"expression": {"expression": 15, "trivia": 10}, "trivia": {"expression": 5, "trivia": 20}}
    assert entry["counts_for_acceptance"] is False and entry["reason"] == "κ 太低（0.4 < 0.6）"
    # the classic one: observed 0.6; chance 0.6 * 0.7 + 0.4 * 0.3 = 0.54;  kappa = 0.06 / 0.46 = 0.1304
    classic = entry_of(*from_matrix({("trivia", "trivia"): 45, ("trivia", "expression"): 15, ("expression", "trivia"): 25, ("expression", "expression"): 15}))
    assert (classic["n"], classic["kappa"], classic["agreement"]) == (100, 0.1304, 0.6)
    # three categories, 20 items:
    #   person \ judge    decisive_fact  trivia  expression | person's total
    #   decisive_fact           6           1        1      |  8
    #   trivia                  2           4        1      |  7
    #   expression              1           1        3      |  5
    #   judge's total           9           6        5      | 20
    # observed (6 + 4 + 3) / 20 = 0.65;  chance (8*9 + 7*6 + 5*5) / 400 = 139 / 400 = 0.3475;  kappa = 0.3025 / 0.6525 = 0.4636
    three = entry_of(*from_matrix({
        ("decisive_fact", "decisive_fact"): 6, ("decisive_fact", "trivia"): 1, ("decisive_fact", "expression"): 1,
        ("trivia", "decisive_fact"): 2, ("trivia", "trivia"): 4, ("trivia", "expression"): 1,
        ("expression", "decisive_fact"): 1, ("expression", "trivia"): 1, ("expression", "expression"): 3,
    }))
    assert (three["n"], three["agreement"], three["kappa"]) == (20, 0.65, 0.4636)
    assert three["confusion"]["decisive_fact"] == {"decisive_fact": 6, "expression": 1, "trivia": 1}
    assert three["reason"] == "κ 太低（0.4636 < 0.6）"


def test_kappa_interval_and_confusion_are_the_ones_of_bench_stats() -> None:
    rng = random.Random(11)
    human = [rng.choice(VALUES[:4]) for _ in range(40)]
    judge = [h if rng.random() < 0.7 else rng.choice(VALUES[:4]) for h in human]
    labels = [label_row(f"{i:012x}", verdict_l3(h)) for i, h in enumerate(human)]
    judgments = [judgment_of(f"{i:012x}", verdict_l3(j)) for i, j in enumerate(judge)]
    entry = entry_of(labels, judgments)
    point, low, high = stats.kappa_ci(human, judge, seed=stats.derive_seed(stats.DEFAULT_SEED, "calibrate", "L3"))
    assert entry["kappa"] == round(stats.cohens_kappa(human, judge), 4) == round(point, 4)
    assert entry["ci"] == [round(low, 4), round(high, 4)] and entry["ci"][0] < entry["kappa"] < entry["ci"][1]
    assert entry["confusion"] == stats.confusion(human, judge)
    assert entry["agreement"] == round(stats.agreement(human, judge), 4)
    assert entry_of(labels, judgments) == entry, "the same data, the same numbers (the interval has a fixed seed)"


def test_a_kappa_that_cannot_be_computed_never_counts() -> None:
    labels, judgments = from_matrix({("trivia", "trivia"): 30})
    entry = entry_of(labels, judgments)
    assert entry["n"] == 30 and entry["kappa"] is None and entry["ci"] == [None, None] and entry["agreement"] == 1.0
    assert entry["counts_for_acceptance"] is False
    assert entry["reason"] == "κ 无定义（人工和裁判都只用了一种取值，这批样本分不出好裁判和固执的裁判）"
    json.dumps(build_report(labels, judgments), allow_nan=False)  # (no NaN gets into the file)
    none = build_report([label_row("a" * 12, verdict_l3())], [])["rubrics"]["L3"]
    assert (none["n"], none["kappa"], none["ci"], none["agreement"], none["confusion"], none["fields"]) == (0, None, [None, None], None, {}, {})
    assert none["counts_for_acceptance"] is False and none["reason"] == "没有配上的样本（n = 0）" and none["unpaired_labels"] == 1
    both = entry_of(*from_matrix({("trivia", "trivia"): 6}), min_n=20)
    assert both["reason"].startswith("n 太少（6 < 20）；κ 无定义")


@pytest.mark.parametrize(
    ("counts", "options", "counts_for_it", "reason"),
    [
        ({("trivia", "trivia"): 10, ("expression", "expression"): 10}, {}, True, "通过（κ 1 ≥ 0.6，n 20 ≥ 20）"),
        ({("trivia", "trivia"): 10, ("expression", "expression"): 9}, {}, False, "n 太少（19 < 20）"),
        ({("trivia", "trivia"): 10, ("expression", "expression"): 9}, {"min_n": 19}, True, "通过（κ 1 ≥ 0.6，n 19 ≥ 19）"),
        ({("trivia", "trivia"): 5, ("expression", "expression"): 5}, {"min_n": 5}, True, "通过（κ 1 ≥ 0.6，n 10 ≥ 5）"),
        ({("trivia", "trivia"): 20, ("trivia", "expression"): 5, ("expression", "trivia"): 10, ("expression", "expression"): 15}, {}, False, "κ 太低（0.4 < 0.6）"),
        ({("trivia", "trivia"): 20, ("trivia", "expression"): 5, ("expression", "trivia"): 10, ("expression", "expression"): 15}, {"threshold": 0.4}, True, "通过（κ 0.4 ≥ 0.4，n 50 ≥ 20）"),
        ({("trivia", "trivia"): 4, ("trivia", "expression"): 1, ("expression", "trivia"): 1, ("expression", "expression"): 4}, {}, False, "n 太少（10 < 20）"),
        # observed 4/10, chance (7*7 + 3*3) / 100 = 0.58: kappa = (0.4 - 0.58) / 0.42 = -0.4286 - both reasons are given
        ({("trivia", "trivia"): 4, ("trivia", "expression"): 3, ("expression", "trivia"): 3, ("expression", "expression"): 0}, {}, False, "n 太少（10 < 20）；κ 太低（-0.4286 < 0.6）"),
        ({("trivia", "trivia"): 8, ("trivia", "expression"): 2, ("expression", "trivia"): 2, ("expression", "expression"): 8}, {}, True, "通过（κ 0.6 ≥ 0.6，n 20 ≥ 20）"),
        ({("trivia", "trivia"): 8, ("trivia", "expression"): 2, ("expression", "trivia"): 2, ("expression", "expression"): 8}, {"threshold": 0.61}, False, "κ 太低（0.6 < 0.61）"),
        ({("trivia", "trivia"): 8, ("trivia", "expression"): 2, ("expression", "trivia"): 2, ("expression", "expression"): 8}, {"threshold": 0.5}, True, "通过（κ 0.6 ≥ 0.5，n 20 ≥ 20）"),
    ],
)
def test_the_threshold_and_the_minimum_number_of_pairs_decide_together(counts: dict, options: dict, counts_for_it: bool, reason: str) -> None:
    entry = entry_of(*from_matrix(counts), **options)
    assert entry["counts_for_acceptance"] is counts_for_it
    assert entry["reason"] == reason


def test_the_threshold_and_min_n_of_the_report_are_written_into_it() -> None:
    report = build_report(*from_matrix({("trivia", "trivia"): 10, ("expression", "expression"): 10}), min_n=7, threshold=0.75)
    assert (report["threshold"], report["min_n"]) == (0.75, 7)
    default = build_report(*from_matrix({("trivia", "trivia"): 10, ("expression", "expression"): 10}))
    assert (default["threshold"], default["min_n"]) == (0.6, 20)
    with pytest.raises(CalibrationError, match="--min-n 要 ≥ 1"):
        build_report([], [], min_n=0)
    with pytest.raises(CalibrationError, match="--threshold"):
        build_report([], [], threshold=1.5)
    with pytest.raises(CalibrationError, match="--threshold"):
        build_report([], [], threshold=-1)


# ------------------------------------------------------------------------------------------------------------------------- calibrate: pairing
def test_only_what_both_sides_have_is_paired_and_the_rest_is_counted() -> None:
    labels = [label_row(f"{n:012x}", verdict_l3("trivia")) for n in range(1, 11)]  # 1..10
    judgments = [judgment_of(f"{n:012x}", verdict_l3("trivia")) for n in range(6, 16)]  # 6..15
    entry = entry_of(labels, judgments)
    assert (entry["n"], entry["unpaired_labels"], entry["unpaired_judgments"]) == (5, 5, 5)
    stranger = build_report(labels + [label_row("f" * 12, verdict_l3())], judgments)["rubrics"]["L3"]
    assert stranger["unpaired_labels"] == 6, "a label of an item nobody judged"


def test_of_two_labels_for_one_item_the_latest_is_used_and_it_is_said() -> None:
    first = label_row("a" * 12, verdict_l3("trivia"), ts="2026-11-20T10:00:00.000Z")
    second = label_row("a" * 12, verdict_l3("expression"), ts="2026-11-20T11:00:00.000Z")
    judgment = judgment_of("a" * 12, verdict_l3("expression"))
    for order in ([first, second], [second, first]):
        entry = entry_of(order, [judgment])
        assert (entry["n"], entry["agreement"], entry["duplicate_labels"]) == (1, 1.0, 1), "the later ts wins, whatever the order in the file"
    tie = entry_of([label_row("a" * 12, verdict_l3("trivia")), label_row("a" * 12, verdict_l3("expression"))], [judgment])
    assert tie["agreement"] == 1.0, "equal times: the later line"
    doubled = entry_of([first], [judgment_of("a" * 12, verdict_l3("trivia")), judgment_of("a" * 12, verdict_l3("expression"))])
    assert doubled["n"] == 1 and doubled["agreement"] == 0.0, "of two judgments the later line is used"


def test_pairs_of_different_versions_of_the_rubric_do_not_count() -> None:
    current = RUBRICS["L3"].version
    cases = {
        "label from an older version": (current - 1 or 99, current),
        "judgment from an older version": (current, current - 1 or 99),
        "both from another version": (current + 1, current + 1),
    }
    for name, (label_version, judgment_version) in cases.items():
        labels = [label_row(f"{n:012x}", verdict_l3(), version=label_version) for n in range(5)]
        judgments = [judgment_of(f"{n:012x}", verdict_l3(), version=judgment_version) for n in range(5)]
        report = build_report(labels, judgments)
        entry = report["rubrics"]["L3"]
        assert (entry["n"], entry["stale_pairs"], entry["unpaired_labels"], entry["unpaired_judgments"]) == (0, 5, 0, 0), name
        assert entry["counts_for_acceptance"] is False
        assert any("5 对的 rubric 版本不一致" in p and f"v{current}" in p for p in report["problems"]), name
    ok = build_report([label_row("a" * 12, verdict_l3(), version=current)], [judgment_of("a" * 12, verdict_l3(), version=current)])["rubrics"]["L3"]
    assert (ok["n"], ok["stale_pairs"], ok["rubric_version"]) == (1, 0, current)


def test_a_label_that_does_not_fit_the_rubric_is_left_out_and_reported_and_so_is_a_verdict() -> None:
    labels = [
        label_row("a" * 12, {"basis": "vibes", "cites_decisive": True, "reason": ""}),
        label_row("b" * 12, {"basis": "trivia", "reason": ""}),
        label_row("c" * 12, {"basis": "trivia", "cites_decisive": True, "reason": "", "extra": 1}),
        label_row("d" * 12, verdict_l3("trivia")),
        label_row("e" * 12, verdict_l3("trivia")),
    ]
    judgments = [judgment_of(blind, verdict_l3("trivia")) for blind in ("a" * 12, "b" * 12, "c" * 12, "d" * 12)] + [judgment_of("e" * 12, {"basis": "vibes"})]
    report = build_report(labels, judgments)
    entry = report["rubrics"]["L3"]
    assert (entry["n"], entry["invalid_labels"], entry["invalid_judgments"]) == (1, 3, 1)
    text = "\n".join(report["problems"])
    assert "L3 " + "a" * 12 + "：人工标注不符合 L3 的格式，没有计入" in text and "不在" in text
    assert "缺 cites_decisive" in text and "多出 extra" in text
    assert "L3 " + "e" * 12 + "：裁判的判定不符合 L3 的格式" in text
    assert (entry["unpaired_labels"], entry["unpaired_judgments"]) == (0, 0), "an invalid label is not 'unpaired': it is wrong"


def test_rows_that_are_not_labels_or_judgments_are_skipped_and_said() -> None:
    labels, judgments = from_matrix({("trivia", "trivia"): 3})
    junk_labels = [{"format": "arena-bench-judgment", "blind_id": "z"}, {"format": "arena-bench-label", "blind_id": 5, "label": {}}, {"format": "arena-bench-label", "blind_id": "q", "label": "x"}]
    junk_judgments = [{"format": "arena-judge-item", "blind_id": "z", "bot": "bot:Alice"}, {"format": JUDGMENT_FORMAT, "blind_id": "z", "verdict": "x"}]
    report = build_report(labels + junk_labels, judgments + junk_judgments)
    assert report["rubrics"]["L3"]["n"] == 3
    assert any("3 行不是 arena-bench-label" in p for p in report["problems"])
    assert any("2 行不是 arena-bench-judgment" in p for p in report["problems"])


def test_an_item_that_the_two_sides_put_under_different_rubrics_is_not_paired() -> None:
    labels = [label_row("a" * 12, verdict_l3("trivia"))]
    judgments = [judgment_of("a" * 12, verdict_l3("trivia"), rubric="L1")]
    report = build_report(labels, judgments)
    assert report["rubrics"]["L3"]["n"] == 0
    assert any("人工标注说它属于 L3，裁判的判定说它属于 L1" in p for p in report["problems"])


def test_a_rubric_nobody_labelled_is_not_in_the_report_and_an_unknown_one_is_a_problem() -> None:
    labels, judgments = from_matrix({("trivia", "trivia"): 3})
    judgments += [judgment_of(f"{900 + n:012x}", {"recall": "correct", "reason": "x"}, rubric="M2") for n in range(5)]
    report = build_report(labels + [label_row("a" * 12, {"x": 1}, rubric="Q7")], judgments)
    assert list(report["rubrics"]) == ["L3"], "M2 has judgments and no labels: it is not calibrated, and the report does not pretend"
    assert any("Q7：没有这个 rubric 的评分标准" in p for p in report["problems"])


def test_s2_compares_the_card_letters_whatever_their_case() -> None:
    schema_fields = {"match": "C", "runner_up": "A", "confidence": "high", "cues": ""}
    labels = [label_row(f"{n:012x}", {**schema_fields, "match": "C"}, rubric="S2") for n in range(3)]
    judgments = [judgment_of(f"{n:012x}", {**schema_fields, "match": " c "}, rubric="S2") for n in range(3)]
    entry = entry_of(labels, judgments, "S2")
    assert entry["agreement"] == 1.0 and entry["confusion"] == {"C": {"C": 3}}
    assert category_of("c", kind="match") == "C" and category_of("c", kind="basis") == "c"
    assert category_of(True) == "true" and category_of(False) == "false" and category_of(3) == "3" and category_of(["a"]) == '["a"]'


def test_the_other_choice_fields_report_their_agreement_and_free_texts_are_not_compared() -> None:
    labels, judgments = [], []
    for n in range(30):
        cites = n % 2 == 0
        labels.append(label_row(f"{n:012x}", verdict_l3("trivia", cites, "我的理由")))
        judgments.append(judgment_of(f"{n:012x}", verdict_l3("trivia" if n % 10 else "expression", cites if n % 5 else not cites, "它的理由")))
    entry = entry_of(labels, judgments)
    assert list(entry["fields"]) == ["cites_decisive"], "reason is a free text"
    cites = entry["fields"]["cites_decisive"]
    assert (cites["type"], cites["n"], cites["agreement"]) == ("boolean", 30, round(24 / 30, 4))
    assert cites["kappa"] == round(stats.cohens_kappa([str(n % 2 == 0).lower() for n in range(30)], [str((n % 2 == 0) if n % 5 else (n % 2 != 0)).lower() for n in range(30)]), 4)
    s2 = {"match": "A", "runner_up": "B", "confidence": "high", "cues": "x"}
    s2_labels = [label_row(f"{n:012x}", {**s2, "confidence": "high" if n % 3 else "low"}, rubric="S2") for n in range(9)]
    s2_judgments = [judgment_of(f"{n:012x}", s2, rubric="S2") for n in range(9)]
    fields = entry_of(s2_labels, s2_judgments, "S2")["fields"]
    assert list(fields) == ["confidence"], "runner_up and cues are strings: not compared"
    assert fields["confidence"]["type"] == "enum" and fields["confidence"]["agreement"] == round(6 / 9, 4)
    assert list(entry_of(*from_matrix({("trivia", "trivia"): 4}, rubric="L2"), "L2")["fields"]) == ["cites_decisive"]


def test_calibrate_rubric_can_be_used_on_its_own() -> None:
    labels = {f"{n:012x}": label_row(f"{n:012x}", verdict_l3("trivia")) for n in range(25)}
    judgments = {f"{n:012x}": judgment_of(f"{n:012x}", verdict_l3("trivia" if n % 5 else "unclear")) for n in range(25)}
    entry, problems = calibrate_rubric(RUBRICS["L3"], labels, judgments, min_n=10)
    assert problems == [] and entry["n"] == 25 and entry["confusion"]["trivia"]["unclear"] == 5
    assert entry["counts_for_acceptance"] is False and entry["kappa"] == 0.0, "the person says trivia every time: no better than chance"
    assert calibrate_rubric(RUBRICS["L3"], labels, judgments, min_n=10, threshold=0.0)[0]["counts_for_acceptance"] is True, "kappa 0 reaches a threshold of 0"


# ------------------------------------------------------------------------------------------------------------------------- calibrate: the files
def test_the_command_writes_calibration_json_in_the_documented_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = from_matrix({("decisive_fact", "decisive_fact"): 12, ("trivia", "trivia"): 12, ("expression", "expression"): 6}, rubric="L3")
    weak = from_matrix({("trivia", "trivia"): 6, ("expression", "trivia"): 9, ("trivia", "expression"): 9, ("expression", "expression"): 6}, rubric="L1", start=1000)
    labels = write_jsonl(tmp_path / "labels.jsonl", good[0] + weak[0])
    judgments = write_jsonl(tmp_path / "judgments.jsonl", good[1] + weak[1] + [judgment_of(f"{5000 + n:012x}", verdict_l3()) for n in range(7)])
    out = tmp_path / "out" / "calibration.json"
    assert main(["calibrate", "--labels", str(labels), "--judgments", str(judgments), "--out", str(out)]) == 0
    raw = out.read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw
    document = json.loads(raw.decode("utf-8"), parse_constant=lambda name: pytest.fail(f"{name} is not valid JSON"))
    assert list(document)[:4] == ["format", "version", "threshold", "min_n"]
    assert (document["format"], document["version"], document["threshold"], document["min_n"]) == ("arena-bench-calibration", 1, 0.6, 20)
    assert list(document["rubrics"]) == ["L1", "L3"]
    required = {"n", "unpaired_labels", "unpaired_judgments", "primary", "kappa", "ci", "confusion", "fields", "counts_for_acceptance", "reason", "rubric_version"}
    for entry in document["rubrics"].values():
        assert required <= set(entry)
        assert isinstance(entry["ci"], list) and len(entry["ci"]) == 2 and isinstance(entry["counts_for_acceptance"], bool) and isinstance(entry["reason"], str)
    l3, l1 = document["rubrics"]["L3"], document["rubrics"]["L1"]
    assert (l3["n"], l3["kappa"], l3["counts_for_acceptance"], l3["unpaired_judgments"]) == (30, 1.0, True, 7)
    assert (l1["n"], l1["counts_for_acceptance"]) == (30, False) and l1["kappa"] < 0
    assert "κ 太低" in l1["reason"] and "κ" in raw.decode("utf-8"), "Chinese and κ are written as they are, not as \\u escapes"
    assert document["problems"] == []
    shown = capsys.readouterr().out
    assert shown.splitlines()[0] == "校准：人工标注 ↔ 裁判（κ ≥ 0.6 且至少 20 对才计入验收）"
    assert f"写到 {out}" in shown


def test_the_terminal_table_is_in_chinese_and_shows_the_confusion_of_the_rubrics_that_do_not_count(capsys: pytest.CaptureFixture[str]) -> None:
    good = from_matrix({("decisive_fact", "decisive_fact"): 15, ("trivia", "trivia"): 15})
    weak = from_matrix({("trivia", "trivia"): 5, ("expression", "trivia"): 10, ("trivia", "expression"): 10, ("expression", "expression"): 5}, rubric="L1", start=1000)
    text = calibrate.format_report(build_report(good[0] + weak[0], good[1] + weak[1]))
    lines = text.splitlines()
    assert lines[0] == "校准：人工标注 ↔ 裁判（κ ≥ 0.6 且至少 20 对才计入验收）"
    assert lines[1].split()[:8] == ["rubric", "版本", "配对", "n", "κ", "95%", "区间", "一致率"] and "计入验收" in lines[1] and lines[1].rstrip().endswith("说明")
    l1 = next(line for line in lines if line.startswith("L1"))
    l3 = next(line for line in lines if line.startswith("L3"))
    assert "v1" in l3 and "  是  " in l3 and "通过（κ 1 ≥ 0.6，n 30 ≥ 20）" in l3 and "够" in l3
    assert "  否  " in l1 and "κ 太低（-0.3333 < 0.6）" in l1 and "弱" in l1 and "33%" in l1
    assert "L1 的混淆矩阵（行 = 人工标注，列 = 裁判）：" in text and "L3 的混淆矩阵" not in text, "only what does not count needs a look at its matrix"
    matrix = lines[lines.index("L1 的混淆矩阵（行 = 人工标注，列 = 裁判）：") + 1 :][:3]
    assert matrix[0].split() == ["expression", "trivia"] and matrix[1].split() == ["expression", "5", "10"] and matrix[2].split() == ["trivia", "10", "5"]
    assert calibrate.format_report(build_report([], [])).splitlines()[1] == "（没有任何 rubric 配上了样本）"
    wide = calibrate.format_report(build_report(*from_matrix({("trivia", "trivia"): 2})))
    assert "κ 无定义" in wide and "—" in wide
    capsys.readouterr()


def test_the_table_lists_what_was_left_out() -> None:
    labels, judgments = from_matrix({("trivia", "trivia"): 4})
    labels += [label_row("a" * 12, verdict_l3()), label_row("b" * 12, verdict_l3(), version=9), label_row("c" * 12, {"basis": "x"})]
    judgments += [judgment_of("b" * 12, verdict_l3(), version=9), judgment_of("d" * 12, verdict_l3())]
    text = calibrate.format_report(build_report(labels, judgments))
    assert "L3：人工标了、裁判没判 1，裁判判了、人工没标 1，版本不一致 1，人工标注不合格式 1" in text
    many = build_report(labels, judgments)
    many["problems"] = [f"问题 {i}" for i in range(15)]
    shown = calibrate.format_report(many)
    assert shown.count("问题：问题") == 12 and "……还有 3 条问题" in shown


def test_calibrate_says_so_when_nothing_could_be_paired_or_a_file_is_wrong(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    labels = write_jsonl(tmp_path / "labels.jsonl", [label_row("a" * 12, verdict_l3())])
    judgments = write_jsonl(tmp_path / "judgments.jsonl", [judgment_of("b" * 12, verdict_l3())])
    out = tmp_path / "calibration.json"
    assert main(["calibrate", "--labels", str(labels), "--judgments", str(judgments), "--out", str(out)]) == 1
    assert "一对样本也没有配上" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["rubrics"]["L3"]["n"] == 0, "the file is written all the same (the report reads 'not calibrated' from it)"
    assert main(["calibrate", "--labels", str(tmp_path / "nothing.jsonl"), "--judgments", str(judgments)]) == 1
    assert "校准失败" in capsys.readouterr().out
    broken = tmp_path / "broken.jsonl"
    broken.write_text('{"format": 1}\n{oops\n', encoding="utf-8")
    assert main(["calibrate", "--labels", str(broken), "--judgments", str(judgments)]) == 1
    assert "第 2 行不是合法的 JSON" in capsys.readouterr().out
    assert main(["calibrate", "--labels", str(labels), "--judgments", str(judgments), "--min-n", "0"]) == 1
    assert "--min-n 要 ≥ 1" in capsys.readouterr().out
    assert main(["calibrate", "--labels", str(labels), "--judgments", str(judgments), "--threshold", "3"]) == 1
    assert "--threshold" in capsys.readouterr().out
    good = from_matrix({("trivia", "trivia"): 25})
    both = write_jsonl(tmp_path / "both.jsonl", good[1])
    split = [write_jsonl(tmp_path / "l1.jsonl", good[0][:10]), write_jsonl(tmp_path / "l2.jsonl", good[0][10:])]
    assert main(["calibrate", "--labels", *map(str, split), "--judgments", str(both), "--out", str(out), "--min-n", "20", "--threshold", "0.0"]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["rubrics"]["L3"]["n"] == 25, "several label files are read as one"


# ------------------------------------------------------------------------------------------------------------------------- with the real rows of judge-merge
def test_the_whole_way_items_to_set_to_labels_to_calibration_with_judgments_from_judge_merge(tmp_path: Path) -> None:
    items, _ = judged_items({"decisive_fact": 14, "trivia": 14, "expression": 14, "intuition": 14})
    verdicts = {"verdicts": [], "errors": [], "notes": [], "format": VERDICTS_FORMAT, "rubric": "L3", "rubric_version": RUBRICS["L3"].version, "batch": 1}
    for index, item in enumerate(items):
        verdicts["verdicts"].append({"blind_id": item["blind_id"], "verdict": verdict_l3(VALUES[index // 14], index % 2 == 0, "裁判的话"), "attempts": 1})
    judgments, problems = merge_verdicts(items, [verdicts])
    assert problems == [] and len(judgments) == 56
    sample = build_set(items, judgments, n=24, seed=2)
    assert Counter(j["verdict"]["basis"] for j in judgments if j["blind_id"] in {r["blind_id"] for r in sample.rows}) == {v: 6 for v in VALUES[:4]}
    by_blind = {j["blind_id"]: j for j in judgments}
    labels = []
    for index, row in enumerate(sample.rows):
        verdict = dict(by_blind[row["blind_id"]]["verdict"])
        if index % 8 == 0:
            verdict["basis"] = "unclear"  # the person disagrees with the judge now and then
        assert validate_verdict(row["schema"], verdict) == []
        labels.append({"format": "arena-bench-label", "version": 1, "blind_id": row["blind_id"], "rubric": row["rubric"], "rubric_version": row["rubric_version"],
                       "label": verdict, "labeler": "user", "ts": TS})
    report = build_report(labels, judgments)
    entry = report["rubrics"]["L3"]
    assert (entry["n"], entry["unpaired_labels"], entry["unpaired_judgments"]) == (24, 0, 32)
    assert entry["agreement"] == round(21 / 24, 4) and entry["counts_for_acceptance"] is True and 0.7 < entry["kappa"] < 1.0
    assert entry["fields"]["cites_decisive"]["agreement"] == 1.0
    assert calibrate.build_report(labels, judgments, threshold=0.95)["rubrics"]["L3"]["counts_for_acceptance"] is False


# ------------------------------------------------------------------------------------------------------------------------- the acceptance report
def test_the_acceptance_report_reads_what_calibrate_writes() -> None:
    """The contract between ``bench.calibrate`` (writes calibration.json) and ``bench.report`` (reads it): what counts there is what counts here."""
    report_module = pytest.importorskip("bench.report")
    parse = getattr(report_module, "parse_calibration", None)
    if parse is None:
        pytest.skip("bench.report has no parse_calibration (yet)")
    good = from_matrix({("decisive_fact", "decisive_fact"): 15, ("trivia", "trivia"): 15}, rubric="L3")
    weak = from_matrix({("trivia", "trivia"): 5, ("expression", "trivia"): 10, ("trivia", "expression"): 10, ("expression", "expression"): 5}, rubric="L1", start=1000)
    single = from_matrix({("trivia", "trivia"): 30}, rubric="L2", start=2000)  # kappa is undefined: both only ever say trivia
    # M2: perfect agreement, but on 12 pairs only (the primary field of M2 is `recall`)
    m2_labels = [label_row(f"{3000 + n:012x}", {"recall": "correct" if n % 2 else "wrong", "reason": ""}, rubric="M2") for n in range(12)]
    m2_judgments = [judgment_of(f"{3000 + n:012x}", {"recall": "correct" if n % 2 else "wrong", "reason": ""}, rubric="M2") for n in range(12)]
    labels = good[0] + weak[0] + single[0] + m2_labels
    judgments = good[1] + weak[1] + single[1] + m2_judgments
    document = json.loads(json.dumps(build_report(labels, judgments), ensure_ascii=False, allow_nan=False))
    parsed = parse(document)
    assert sorted(parsed) == ["L1", "L2", "L3", "M2"]
    l3 = parsed["L3"]
    assert (l3.n, l3.kappa, l3.version, l3.flagged, l3.counts) == (30, 1.0, RUBRICS["L3"].version, True, True)
    assert (l3.lo, l3.hi) == tuple(document["rubrics"]["L3"]["ci"])
    assert (parsed["L1"].n, parsed["L1"].counts, parsed["L1"].kappa < 0) == (30, False, True)
    assert (parsed["L2"].n, parsed["L2"].kappa, parsed["L2"].lo, parsed["L2"].flagged, parsed["L2"].counts) == (30, None, None, False, False), "an undefined kappa never counts"
    m2 = parsed["M2"]
    assert (m2.n, m2.kappa, m2.flagged, m2.counts) == (12, 1.0, False, False), "kappa 1 on 12 pairs: this module says too few, and the report listens"
    relaxed = parse(json.loads(json.dumps(build_report(labels, judgments, min_n=10), allow_nan=False)))
    assert relaxed["M2"].counts is True


# ------------------------------------------------------------------------------------------------------------------------- the user guide
GUIDE = Path(__file__).resolve().parents[2] / "bench" / "judges" / "calibration" / "README.md"


def _guide() -> str:
    return GUIDE.read_text(encoding="utf-8").replace("\r\n", "\n")


def _guide_commands() -> list[str]:
    """The ``python -m bench calibration-set`` / ``calibrate`` commands of the guide's code blocks (continued lines joined)."""
    commands = []
    for block in re.findall(r"```bash\n(.*?)```", _guide(), re.S):
        for command in re.sub(r"\\\n\s*", " ", block).splitlines():
            if command.startswith(("python -m bench calibration-set", "python -m bench calibrate")):
                commands.append(command)
    return commands


def test_every_command_of_the_guide_is_one_the_program_accepts() -> None:
    parser = argparse.ArgumentParser()
    calibrate.add_commands(parser.add_subparsers(dest="command", required=True))
    commands = _guide_commands()
    assert len(commands) >= 2 and {c.split()[3] for c in commands} == {"calibration-set", "calibrate"}
    for command in commands:
        args = parser.parse_args(shlex.split(command)[3:])  # (a wrong flag or a missing required one exits with 2)
        assert args.command in {"calibration-set", "calibrate"}, command


def test_the_defaults_and_the_keys_the_guide_quotes_are_the_ones_of_the_code() -> None:
    guide = _guide()
    assert f"每个 rubric 抽几条（默认 {calibrate.DEFAULT_N}）" in guide
    assert f"至少几对才计入（默认 {calibrate.MIN_N}）" in guide
    assert f"κ 至少多少才计入（默认 {calibrate.THRESHOLD}）" in guide
    assert f"κ ≥ {calibrate.THRESHOLD} 且至少 {calibrate.MIN_N} 对才计入验收" in guide
    assert guide.count(calibrate.format_report(build_report([], [])).splitlines()[0]) == 1, "the example output starts like the real one"
    quoted = set(re.findall(r"`([^`\n]+)`", guide))
    row = build_set([make_item(n) for n in range(3)], n=2).rows[0]
    assert set(row) <= quoted, f"documented: the keys of a row of set.jsonl: {set(row) - quoted}"
    entry = entry_of(*from_matrix({("trivia", "trivia"): 3}))
    documented = {key for line in guide.splitlines() if line.startswith("| `") for key in re.findall(r"`([a-z_]+)`", line.split("|")[1])}
    assert set(entry) <= documented, f"documented: the keys of an entry of calibration.json: {set(entry) - documented}"
    for status in ("通过（", "n 太少（", "κ 太低（", "κ 无定义（"):
        assert status in guide
    for rubric in RUBRICS.values():
        assert f"`{rubric.primary}`" in guide, f"{rubric.id}'s primary field is named"


def test_the_links_and_the_files_the_guide_names_exist() -> None:
    guide = _guide()
    for target in re.findall(r"\]\(([^)#\s]+)\)", guide):
        assert (GUIDE.parent / target).resolve().exists(), target
    root = GUIDE.parents[3]
    assert (root / "bench" / "label" / "index.html").is_file() and (root / "bench" / "label" / "label.js").is_file()
    assert "bench/label/index.html" in guide
    assert not re.search(r"[A-Za-z]:\\|/Users/|/home/", guide), "no local path"


def test_math_is_not_lost_in_the_rounding() -> None:
    assert calibrate._round(float("nan")) is None and calibrate._round(float("inf")) is None and calibrate._round(None) is None and calibrate._round(True) is None
    assert calibrate._round(1 / 3) == 0.3333 and calibrate._round(-0.5) == -0.5
    assert [calibrate._fmt(v) for v in (None, 1.0, 0.6, 0.72, -1.0, 0.0, -0.0, 0.4286)] == ["—", "1", "0.6", "0.72", "-1", "0", "0", "0.4286"]
    assert not math.isnan(entry_of(*from_matrix({("trivia", "trivia"): 3, ("expression", "expression"): 3}))["kappa"])
