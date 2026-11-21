"""bench.judging: the rubric files, the packs of the DSH workflow, the verdicts that come back (docs/v0.14-plan.md H48).  No model is called: the workflow
script is run by node against a scripted stand-in for the sub-agents."""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bench import judging
from bench.__main__ import main
from bench.judging import (
    BATCH, JUDGES_DIR, JUDGMENT_FORMAT, PACK_FORMAT, VERDICTS_FORMAT, JudgingError, RubricError, forbidden_words, judge_prompt, load_rubric, load_rubrics,
    merge_verdicts, missing_items, pack_items, parse_rubric, rubric_examples, schema_problems, validate_verdict,
)

WORKFLOW = JUDGES_DIR / "judge.workflow.js"
HARNESS = Path(__file__).with_name("judge_workflow_harness.js")
NODE = shutil.which("node")

RUBRIC_TEXT = """---
rubric: Z9
version: 3
title: 试验
kind: basis
primary: basis
subject: [kind, value]
schema:
  type: object
  additionalProperties: false
  required: [basis, reason]
  properties:
    basis: {type: string, enum: [a, b]}
    reason: {type: string}
---
# Z9

评分标准的正文。
"""


def make_item(n: int, rubric: str = "L3", **over: Any) -> dict[str, Any]:
    item = {
        "format": "arena-bench-judge-item", "version": 1, "blind_id": f"{n:012x}", "item_id": f"20261120-aaaa/sit-{n}/N5/bot:Alice/0/{rubric}",
        "rubric": rubric, "run": "20261120-aaaa", "situation": f"sit-{n}", "seat": "N5", "bot": "bot:Alice", "brain": "BotBrain", "sample": 0, "tags": ["L3"],
        "title": "一道题", "brief": "被测座位是 N5。",
        "context": {"decisive": "N4 自称猎人，公告里开枪的猎人是 N2。", "trap": "N4 说得很有条理。"},
        "truth": {"answer": ["N4"], "wrong": ["N6"], "sides": {"fluent": "N4", "blunt": "N12"}},
        "subject": {"kind": "choice", "tool": "respond", "value": "N4", "reason": "N4 自称猎人，公告对不上。", "thought": "", "text": None, "attempts": [{"secret": "x"}]},
    }
    item.update(over)
    return item


# ------------------------------------------------------------------------------------------------------------------------- the rubric files
def test_every_rubric_of_the_repository_loads_and_fits_the_workflow_tool() -> None:
    rubrics = load_rubrics()
    assert set(rubrics) == {"E2", "L1", "L2", "L3", "M2", "S2"}
    for rubric in rubrics.values():
        assert schema_problems(rubric.schema) == [], rubric.id
        assert rubric.primary in rubric.schema["properties"] and rubric.version >= 1 and rubric.body
        assert rubric.path is not None and rubric.path.name == f"{rubric.id}.md"


def test_the_worked_examples_of_every_rubric_are_valid_verdicts_of_its_own_schema() -> None:
    for rubric in load_rubrics().values():
        examples = rubric_examples(rubric)
        assert len(examples) >= 3, f"{rubric.id}: at least three example cases"
        for number, example in enumerate(examples, start=1):
            assert validate_verdict(rubric.schema, example["verdict"]) == [], f"{rubric.id} example {number}"


def test_the_blind_subject_of_a_rubric_never_names_who_played() -> None:
    for rubric in load_rubrics().values():
        assert not {"bot", "brain", "run", "sample", "situation", "truth", "item_id", "attempts"} & set(rubric.subject), rubric.id


def test_a_rubric_file_is_checked_when_it_is_read() -> None:
    assert parse_rubric(RUBRIC_TEXT).id == "Z9"
    bad = {
        "no front matter": RUBRIC_TEXT.split("---\n", 2)[2],
        "a kind that does not exist": RUBRIC_TEXT.replace("kind: basis", "kind: vibes"),
        "a primary that is not a field": RUBRIC_TEXT.replace("primary: basis", "primary: nothing"),
        "a version that is not a number": RUBRIC_TEXT.replace("version: 3", "version: three"),
        "an id that is not a name": RUBRIC_TEXT.replace("rubric: Z9", "rubric: my rubric"),
        "no subject": RUBRIC_TEXT.replace("subject: [kind, value]", "subject: []"),
        "a schema the workflow tool does not accept": RUBRIC_TEXT.replace("reason: {type: string}", "reason: {type: string, minLength: 3}"),
        "a schema that is not an object": RUBRIC_TEXT.replace("type: object", "type: array"),
    }
    for what, text in bad.items():
        with pytest.raises(RubricError):
            parse_rubric(text)
            pytest.fail(what)


def test_the_file_name_of_a_rubric_is_its_name(tmp_path: Path) -> None:
    (tmp_path / "Q1.md").write_text(RUBRIC_TEXT, encoding="utf-8")  # (says Z9)
    with pytest.raises(RubricError):
        load_rubric("Q1", directory=tmp_path)
    (tmp_path / "Z9.md").write_text(RUBRIC_TEXT, encoding="utf-8")
    (tmp_path / "README.md").write_text("# not a rubric", encoding="utf-8")
    assert load_rubric("Z9", directory=tmp_path).version == 3


# ------------------------------------------------------------------------------------------------------------------------- schemas and verdicts
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["basis", "ok", "reason"],
    "properties": {"basis": {"type": "string", "enum": ["a", "b"]}, "ok": {"type": "boolean"}, "reason": {"type": "string"}},
}


@pytest.mark.parametrize(
    ("verdict", "fragment"),
    [
        ({"basis": "a", "ok": True, "reason": "r"}, None),
        ({"basis": "c", "ok": True, "reason": "r"}, "不在"),
        ({"basis": "a", "ok": "yes", "reason": "r"}, "true / false"),
        ({"basis": "a", "ok": True}, "缺 reason"),
        ({"basis": "a", "ok": True, "reason": "r", "extra": 1}, "多出 extra"),
        ({"basis": "a", "ok": True, "reason": 5}, "字符串"),
        ("a", "对象"),
        (None, "对象"),
    ],
)
def test_a_verdict_must_fit_the_schema(verdict: Any, fragment: str | None) -> None:
    problems = validate_verdict(SCHEMA, verdict)
    if fragment is None:
        assert problems == []
    else:
        assert any(fragment in problem for problem in problems), problems


def test_the_checks_of_the_allowed_subset() -> None:
    assert validate_verdict({"type": "integer"}, 3) == [] and validate_verdict({"type": "integer"}, True) != [] and validate_verdict({"type": "integer"}, 1.5) != []
    assert validate_verdict({"type": "number"}, 1.5) == [] and validate_verdict({"type": "number"}, "1") != []
    assert validate_verdict({"type": "array", "items": {"type": "string"}}, ["a", "b"]) == []
    assert validate_verdict({"type": "array", "items": {"type": "string"}}, ["a", 2]) != []
    assert validate_verdict({"const": "x"}, "x") == [] and validate_verdict({"const": "x"}, "y") != []
    one_of = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
    assert validate_verdict(one_of, "s") == [] and validate_verdict(one_of, 2) == [] and validate_verdict(one_of, 1.5) != []


@pytest.mark.parametrize("key", ["minLength", "description", "$ref", "pattern", "format", "default"])
def test_a_schema_outside_the_workflow_subset_is_refused(key: str) -> None:
    assert schema_problems({"type": "object", "properties": {"a": {"type": "string", key: "x"}}})
    assert schema_problems({"type": "string"})  # (the root must be an object)


# ------------------------------------------------------------------------------------------------------------------------- packs
def test_items_are_packed_per_rubric_in_batches_of_at_most_forty() -> None:
    items = [make_item(n, "L3") for n in range(95)] + [make_item(1000 + n, "L1") for n in range(3)]
    packs = pack_items(items)
    assert [(p["rubric"], p["batch"], p["batches"], len(p["items"])) for p in packs] == [("L1", 1, 1, 3), ("L3", 1, 3, 40), ("L3", 2, 3, 40), ("L3", 3, 3, 15)]
    assert BATCH == 40 and all(p["format"] == PACK_FORMAT and p["version"] == 1 for p in packs)
    rubrics = load_rubrics()
    for pack in packs:
        assert pack["schema"] == rubrics[pack["rubric"]].schema and pack["rubric_version"] == rubrics[pack["rubric"]].version
    assert len(pack_items(items, batch=10)) == 1 + 10  # (3 -> 1, 95 -> 10)
    only = pack_items(items, only=["L1"])
    assert [p["rubric"] for p in only] == ["L1"]


def test_the_same_item_twice_is_judged_once_and_an_unknown_rubric_is_an_error() -> None:
    packs = pack_items([make_item(1), make_item(1), make_item(2)])
    assert [len(p["items"]) for p in packs] == [2]
    with pytest.raises(JudgingError):
        pack_items([make_item(1, "Q7")])
    with pytest.raises(JudgingError):
        pack_items([{"rubric": "L3"}])
    with pytest.raises(JudgingError):
        pack_items([make_item(1)], batch=0)


def test_a_judge_never_sees_the_bot_the_run_the_situation_or_the_truth() -> None:
    items = [make_item(n) for n in range(5)]
    words = forbidden_words(items)
    assert "bot:Alice" in words and "sit-3" in words and "20261120-aaaa" in words
    for pack in pack_items(items):
        for entry in pack["items"]:
            prompt = entry["prompt"]
            assert [w for w in words if w in prompt] == []
            assert "fluent" not in prompt and "blunt" not in prompt and "secret" not in prompt  # (the truth and the attempts of the subject)
            assert "请写一段发言" not in prompt
            assert entry["blind_id"] in prompt
            assert "N4 自称猎人，公告里开枪的猎人是 N2。" in prompt  # (what the rubric lets the judge know: the decisive fact)
            assert "不要使用任何工具" in prompt and "不要读取任何文件" in prompt


def test_the_prompt_is_the_instructions_the_rubric_the_schema_and_the_view() -> None:
    rubric = load_rubric("L3")
    prompt = judge_prompt(rubric, {"blind_id": "abc", "subject": {"value": "N4"}})
    assert prompt.index("你是一名评审") < prompt.index(rubric.body.splitlines()[0]) < prompt.index("# 判定格式") < prompt.index("# 待评内容")
    assert f"L3 v{rubric.version}" in prompt and '"abc"' in prompt


def test_the_pack_of_s2_shows_only_the_words_of_a_speech() -> None:
    item = make_item(1, "S2", subject={"kind": "speech", "text": "我先听听。", "value": "N4", "reason": "r", "tool": "speak", "attempts": []})
    prompt = pack_items([item])[0]["items"][0]["prompt"]
    assert "我先听听。" in prompt and '"kind": "speech"' in prompt
    assert '"value"' not in prompt and '"reason"' not in prompt and '"tool"' not in prompt  # (the style is judged from the words alone)


# ------------------------------------------------------------------------------------------------------------------------- verdicts
def doc(rubric: str, entries: list[tuple[str, dict[str, Any] | None]], **over: Any) -> dict[str, Any]:
    version = load_rubric(rubric).version
    body = {
        "format": VERDICTS_FORMAT, "rubric": rubric, "rubric_version": version, "batch": 1,
        "verdicts": [{"blind_id": b, "verdict": v, "attempts": 1} for b, v in entries if v is not None],
        "errors": [b for b, v in entries if v is None], "notes": [],
    }
    body.update(over)
    return body


GOOD = {"basis": "decisive_fact", "cites_decisive": True, "reason": "引用了公告"}


def test_verdicts_are_joined_to_the_items_by_blind_id() -> None:
    items = [make_item(n) for n in range(3)]
    rows, problems = merge_verdicts(items, [doc("L3", [(i["blind_id"], dict(GOOD)) for i in items])])
    assert problems == [] and len(rows) == 3
    assert rows[0] == {
        "format": JUDGMENT_FORMAT, "version": 1, "blind_id": items[0]["blind_id"], "item_id": items[0]["item_id"], "rubric": "L3",
        "rubric_version": load_rubric("L3").version, "verdict": GOOD, "judge": {"source": "dsh-subagent", "attempts": 1},
    }
    assert missing_items(items, rows) == [] and missing_items(items, rows[:1]) == [items[1]["blind_id"], items[2]["blind_id"]]
    assert missing_items(items, [], only=["L1"]) == []


def test_a_verdict_that_does_not_fit_is_a_problem_and_is_not_kept() -> None:
    items = [make_item(n) for n in range(4)]
    bad_enum = {**GOOD, "basis": "vibes"}
    bad_extra = {**GOOD, "confidence": "high"}
    entries = [(items[0]["blind_id"], dict(GOOD)), (items[1]["blind_id"], bad_enum), (items[2]["blind_id"], bad_extra), (items[3]["blind_id"], None)]
    rows, problems = merge_verdicts(items, [doc("L3", entries)])
    assert [r["blind_id"] for r in rows] == [items[0]["blind_id"]]
    assert len(problems) == 3
    assert any("不符合 L3 的格式" in p and items[1]["blind_id"] in p for p in problems)
    assert any("多出 confidence" in p for p in problems)
    assert any("没有得到判定" in p and items[3]["blind_id"] in p for p in problems)


def test_the_other_ways_a_verdict_file_can_be_wrong() -> None:
    items = [make_item(1)]
    blind = items[0]["blind_id"]
    _, problems = merge_verdicts(items, [doc("L3", [("zzzz", dict(GOOD))])])
    assert any("不在裁判条目里" in p for p in problems)
    _, problems = merge_verdicts(items, [doc("L1", [(blind, dict(GOOD))])])
    assert any("属于 rubric L3" in p for p in problems)
    rows, problems = merge_verdicts(items, [doc("L3", [(blind, dict(GOOD))]), doc("L3", [(blind, {**GOOD, "basis": "trivia"})])])
    assert len(rows) == 1 and rows[0]["verdict"]["basis"] == "decisive_fact" and any("重复" in p for p in problems)
    _, problems = merge_verdicts(items, [doc("L3", [(blind, dict(GOOD))], rubric_version=99)])
    assert any("重新打包" in p for p in problems)
    unknown = {"format": VERDICTS_FORMAT, "rubric": "Q7", "rubric_version": 1, "verdicts": [], "errors": []}
    _, problems = merge_verdicts(items, [{"format": "something else"}, unknown])
    assert len(problems) == 2 and any("未知的 rubric" in p for p in problems)


def test_the_example_verdicts_of_a_rubric_merge_as_they_are() -> None:
    """For every rubric the examples of its own text are accepted - a verdict in the shape the examples show is a verdict."""
    for rubric in load_rubrics().values():
        items = [make_item(n, rubric.id) for n, _ in enumerate(rubric_examples(rubric))]
        entries = [(item["blind_id"], example["verdict"]) for item, example in zip(items, rubric_examples(rubric))]
        rows, problems = merge_verdicts(items, [doc(rubric.id, entries)])
        assert problems == [] and len(rows) == len(entries), rubric.id


# ------------------------------------------------------------------------------------------------------------------------- the command line
def write_items(path: Path, items: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items), encoding="utf-8")


def test_judge_check_passes_on_the_rubrics_of_the_repository(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["judge-check"]) == 0
    out = capsys.readouterr().out
    assert "共 6 个 rubric，0 个问题" in out


def test_judge_check_names_what_is_wrong(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "Z9.md").write_text(RUBRIC_TEXT, encoding="utf-8")  # (no examples, no workflow script)
    assert main(["judge-check", "--judges", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "至少要有 3 个示例" in out and "缺 judge.workflow.js" in out


def test_pack_and_merge_round_trip_through_the_command_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    items = [make_item(n) for n in range(45)] + [make_item(900, "L1")]
    items_file = tmp_path / "judge_items.jsonl"
    write_items(items_file, items)
    assert main(["judge-pack", "--items", str(items_file), "--out", str(tmp_path / "packs")]) == 0
    capsys.readouterr()
    files = sorted(p.name for p in (tmp_path / "packs").glob("*.json"))
    assert files == ["L1-01.json", "L3-01.json", "L3-02.json"]
    verdict_files = []
    for name in files:
        pack = json.loads((tmp_path / "packs" / name).read_text(encoding="utf-8"))
        assert len(pack["items"]) <= 40
        path = tmp_path / f"verdicts-{name}"
        path.write_text(json.dumps(doc(pack["rubric"], [(e["blind_id"], dict(GOOD)) for e in pack["items"]]), ensure_ascii=False), encoding="utf-8")
        verdict_files.append(str(path))
    out_file = tmp_path / "judgments.jsonl"
    assert main(["judge-merge", "--items", str(items_file), "--verdicts", *verdict_files, "--out", str(out_file)]) == 0
    rows = [json.loads(line) for line in out_file.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 46 and {r["rubric"] for r in rows} == {"L1", "L3"} and all(r["verdict"] == GOOD for r in rows)
    assert "合并了 46 条判定" in capsys.readouterr().out


def test_merge_reports_what_is_missing_and_what_is_wrong(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    items = [make_item(n) for n in range(3)]
    items_file = tmp_path / "items.jsonl"
    write_items(items_file, items)
    part = tmp_path / "v.json"
    part.write_text(json.dumps(doc("L3", [(items[0]["blind_id"], dict(GOOD))])), encoding="utf-8")
    assert main(["judge-merge", "--items", str(items_file), "--verdicts", str(part), "--out", str(tmp_path / "j.jsonl")]) == 1  # two have no judgment
    assert "还没有判定" in capsys.readouterr().out
    assert main(["judge-merge", "--items", str(items_file), "--verdicts", str(part), "--out", str(tmp_path / "j.jsonl"), "--allow-missing"]) == 0
    part.write_text(json.dumps(doc("L3", [(items[0]["blind_id"], {**GOOD, "basis": "vibes"})])), encoding="utf-8")
    assert main(["judge-merge", "--items", str(items_file), "--verdicts", str(part), "--allow-missing"]) == 1
    assert main(["judge-merge", "--items", str(items_file), "--verdicts", str(part), "--allow-missing", "--allow-problems"]) == 0
    assert main(["judge-merge", "--items", str(tmp_path / "nothing.jsonl"), "--verdicts", str(part)]) == 1


def test_packing_nothing_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty = tmp_path / "items.jsonl"
    empty.write_text("", encoding="utf-8")
    assert main(["judge-pack", "--items", str(empty), "--out", str(tmp_path / "p")]) == 1
    write_items(empty, [make_item(1, "Q7")])
    assert main(["judge-pack", "--items", str(empty), "--out", str(tmp_path / "p")]) == 1
    assert "打包失败" in capsys.readouterr().out


# ------------------------------------------------------------------------------------------------------------------------- the workflow script
def test_the_workflow_script_uses_only_what_the_workflow_tool_documents() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    for forbidden in ("require(", "import ", "export ", "fetch(", "setTimeout", "setInterval", "process.", "XMLHttpRequest", "Function(", "eval(", "readFile", "writeFile"):
        assert forbidden not in code, forbidden
    assert re.search(r"^return \{", code, re.M), "the script ends with a return of the result"
    used = set(re.findall(r"\b(agent|parallel|pipeline|phase|log|args)\b\s*[(.]", code))
    assert used <= {"agent", "parallel", "pipeline", "phase", "log", "args"} and {"agent", "parallel", "phase", "log"} <= used


def pack_for_script(n: int = 3, **over: Any) -> dict[str, Any]:
    pack = pack_items([make_item(i) for i in range(n)])[0]
    pack.update(over)
    return pack


def run_workflow(args: dict[str, Any], agent: str = "ok", **extra: Any) -> dict[str, Any]:
    scenario = {"args": args, "agent": agent, **extra}
    done = subprocess.run(
        [NODE, str(HARNESS), str(WORKFLOW)], input=json.dumps(scenario, ensure_ascii=False), capture_output=True, text=True, encoding="utf-8", timeout=60, check=True
    )
    return json.loads(done.stdout)


needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@needs_node
def test_the_workflow_judges_every_item_with_the_prompt_of_the_pack() -> None:
    pack = pack_for_script(5)
    out = run_workflow({"pack": pack})
    result = out["result"]
    assert result["format"] == VERDICTS_FORMAT and result["rubric"] == "L3" and result["rubric_version"] == pack["rubric_version"] and result["errors"] == []
    assert [v["blind_id"] for v in result["verdicts"]] == [e["blind_id"] for e in pack["items"]]  # (in the order of the pack)
    assert {c["prompt"] for c in out["calls"]} == {e["prompt"] for e in pack["items"]}  # (the script adds nothing to a prompt)
    assert all(c["schema"] == pack["schema"] for c in out["calls"]) and out["phases"] == ["judge"]
    assert [rubric_ok for rubric_ok in (merge_verdicts([make_item(i) for i in range(5)], [result])[1])] == []  # (and the verdicts merge)


@needs_node
def test_the_workflow_never_asks_for_more_children_than_it_was_told_to() -> None:
    pack = pack_for_script(12)
    assert run_workflow({"pack": pack})["maxActive"] == 2  # (the default)
    assert run_workflow({"pack": pack, "concurrency": 1})["maxActive"] == 1
    assert run_workflow({"pack": pack, "concurrency": 3})["maxActive"] == 3
    assert run_workflow({"pack": pack, "concurrency": 99})["maxActive"] == 3  # (the limit of DSH)
    assert run_workflow({"pack": pack, "concurrency": 0})["maxActive"] == 2


@needs_node
def test_an_agent_that_fails_is_asked_once_more_and_an_item_without_a_verdict_is_listed_not_invented() -> None:
    pack = pack_for_script(3)
    once = run_workflow({"pack": pack}, agent="null-once")["result"]
    assert once["errors"] == [] and all(v["attempts"] == 2 for v in once["verdicts"])
    never = run_workflow({"pack": pack}, agent="null")["result"]
    assert never["verdicts"] == [] and never["errors"] == [e["blind_id"] for e in pack["items"]]
    thrown = run_workflow({"pack": pack}, agent="throw")["result"]
    assert thrown["verdicts"] == [] and len(thrown["errors"]) == 3 and len(thrown["notes"]) == 3 and "boom" in thrown["notes"][0]


@needs_node
def test_a_capacity_error_costs_the_item_not_the_run() -> None:
    pack = pack_for_script(6)
    out = run_workflow({"pack": pack, "concurrency": 3}, maxActive=1)  # (the stand-in allows one child: the others get "subagent limit reached")
    result = out["result"]
    assert len(result["verdicts"]) + len(result["errors"]) == 6 and result["errors"] and any("subagent limit reached" in n for n in result["notes"])
    assert len(result["verdicts"]) >= 1  # (what could be judged is in the result and merges)


@needs_node
def test_a_pack_that_is_not_a_pack_or_too_big_stops_the_script() -> None:
    assert "args.pack" in run_workflow({})["error"]
    assert "args.pack" in run_workflow({"pack": {**pack_for_script(1), "format": "nope"}})["error"]
    big = copy.deepcopy(pack_for_script(1))
    big["items"] = big["items"] * 41
    assert "at most 40" in run_workflow({"pack": big})["error"]


def test_the_module_has_one_handler_per_command() -> None:
    assert set(judging.HANDLERS) == {"judge-check", "judge-pack", "judge-merge"}
