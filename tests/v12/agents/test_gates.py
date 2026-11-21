"""The output gate of a bot's speech (v0.14 H15): what reads like a report is found - and nothing is ever changed."""

from __future__ import annotations

from pathlib import Path

import pytest

import app.agents.gates as gates
from app.agents.gates import ORAL_LIST_LINE, allows_oral_lists, check_speech, format_message

from ..test_neutrality import FIXED_PARTICIPANT_ID_RE, FORBIDDEN_TERMS

IDS, NAMES = ["p1"], ["Alex"]


def rules(text: str, **kw: object) -> list[str]:
    return [p.rule for p in check_speech(text, ids=IDS, names=NAMES, **kw)]  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    [
        "我觉得他有问题，你们看他昨天那票。",
        "第一，他跳得太快；第二，票也不对；所以我投他。",  # an enumeration spoken in one breath is talk
        "1. 只有这一条",  # one numbered line is not a list
        "2.5 倍的差距\n3.5 倍的差距",  # numbers that are numbers
        "——我不同意\n——真的不同意",  # a dash that is a dash
        "你看 #12 那条，他说的和 #14 对不上。",  # a reference to a record is not a heading
        "#12 那条有问题\n#14 也有",
        "Blair：你昨天那票呢？",  # somebody else's name is how one addresses a person
        "我是 Alex，我先说两句。",  # an introduction is a sentence
        "Alex 觉得他有问题。",
        "我先说两句。Alex：这是我的看法",  # only the START of a text counts
        "",
        "   \n",
    ],
)
def test_text_that_reads_like_talk_is_left_alone(text: str) -> None:
    assert rules(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "我的看法：\n1. 他跳得太快\n2. 票也不对\n所以我投他。",
        "1) 一\n2) 二",
        "1、一\n2、二",
        "1．一\n2．二",
        "（1）他\n（2）她",
        "(1) he\n(2) she",
        "①他跳得太快\n②票也不对",
        "一、他跳得太快\n二、票也不对",
        "（一）他\n（二）她",
        "- 一\n- 二",
        "* 一\n* 二",
        "+ 一\n+ 二",
        "• 一\n• 二",
        "·一\n·二",
        "1. 一\n\n2. 二",  # blank lines between the items do not make them something else
        "先说结论\n- 一\n- 二\n再补一句",
    ],
)
def test_two_lines_in_a_row_that_start_like_a_list_are_a_list(text: str) -> None:
    assert rules(text) == ["list"]


def test_a_list_is_reported_with_the_line_it_starts_at_and_a_third_item_changes_nothing() -> None:
    problems = check_speech("开场白\n1. 一\n2. 二\n3. 三", ids=IDS, names=NAMES)
    assert [p.rule for p in problems] == ["list"] and "第 2 行" in problems[0].detail
    assert [p.rule for p in check_speech("1. 一\n说话\n2. 二", ids=IDS, names=NAMES)] == []  # not in a row


@pytest.mark.parametrize(
    "text",
    ["# 结论\n我投他", "### 小标题\n正文", "我觉得**他**有问题", "```\n代码\n```", "先说两句\n## 理由\n因为……"],
)
def test_a_heading_bold_or_a_code_block_is_a_report(text: str) -> None:
    assert rules(text) == ["heading"]


@pytest.mark.parametrize(
    "text",
    [
        "p1：我先说两句。",
        "p1 Alex：我先说两句。",
        "Alex：我先说两句。",
        "Alex（p1）：我先说两句。",
        "Alex 说：我先说两句。",
        "p1/Alex：我",
        "【p1】我先说两句",
        "[p1 Alex] 我先说两句：",
        "“p1：我先说”",
        "  Alex：前面有空格",
    ],
)
def test_the_seat_id_or_the_name_in_front_of_what_is_said_is_a_prefix(text: str) -> None:
    assert rules(text) == ["prefix"]


def test_every_problem_is_found_at_once_in_a_fixed_order() -> None:
    text = "Alex：\n# 标题\n- a\n- b **粗**"
    assert rules(text) == ["prefix", "heading", "list"]
    detail = format_message(check_speech(text, ids=IDS, names=NAMES))
    assert "Alex：" in detail and "标题" in detail and "编号或项目符号" in detail
    assert "没有发出，发言权还在" in detail and "连贯的口语" in detail


def test_the_oral_list_line_of_a_voice_switches_only_the_list_check_off() -> None:
    assert allows_oral_lists(f"你爱列点。\n{ORAL_LIST_LINE}\n篇幅：60–150 字")
    assert allows_oral_lists(f"  {ORAL_LIST_LINE}  ")  # a line of its own (spaces around it do not matter)
    assert not allows_oral_lists(f"我允许口头列举，大家都知道") and not allows_oral_lists("") and not allows_oral_lists(f"{ORAL_LIST_LINE}。")
    listy = "1. 一\n2. 二"
    assert rules(listy, allow_lists=True) == []
    assert rules("# 标题\n" + listy, allow_lists=True) == ["heading"]  # (the others stay)
    assert rules("Alex：\n" + listy, allow_lists=True) == ["prefix"]


def test_without_an_id_or_a_name_there_is_no_prefix_to_look_for() -> None:
    assert [p.rule for p in check_speech("Alex：我先说", ids=[], names=[])] == []
    assert [p.rule for p in check_speech("Alex：我先说", ids=["", " "], names=[None or ""])] == []  # type: ignore[list-item]
    assert [p.rule for p in check_speech("Mary Ann：我", ids=[], names=["Mary Ann"])] == ["prefix"]
    assert [p.rule for p in check_speech("a.b：我", ids=[], names=["a.b"])] == ["prefix"]  # a name is not a pattern
    assert [p.rule for p in check_speech("axb：我", ids=[], names=["a.b"])] == []


def test_the_gate_only_reads_it_never_changes_the_text() -> None:
    """Its functions return verdicts (``FormatProblem``) and the sentence of a receipt - nothing that returns a changed text."""
    text = "1. 一\n2. 二"
    assert all(isinstance(problem, gates.FormatProblem) for problem in check_speech(text, ids=IDS, names=NAMES))
    assert isinstance(format_message([]), str) and text not in format_message(check_speech(text, ids=IDS, names=NAMES))
    assert not [name for name in dir(gates) if any(word in name.lower() for word in ("rewrite", "clean", "sanitize", "shorten", "truncate"))]


def test_a_non_text_is_not_a_speech() -> None:
    assert check_speech(None, ids=IDS, names=NAMES) == []  # type: ignore[arg-type]
    assert check_speech(123, ids=IDS, names=NAMES) == []  # type: ignore[arg-type]


def test_the_module_is_game_neutral() -> None:
    text = Path(gates.__file__).read_text(encoding="utf-8")
    assert not [term for term in FORBIDDEN_TERMS if term in text] and not FIXED_PARTICIPANT_ID_RE.search(text)
