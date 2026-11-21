"""``bench.situation``: the YAML situation scripts - parsing, the macros, the checks that name the line that is wrong."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from bench.situation import (
    DECISION_TOOLS,
    REF_RE,
    Situation,
    SituationError,
    expand_steps,
    load_situation,
    load_situations,
    parse_situation,
    situation_files,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "bench" / "fixtures" / "situations"

BASE: dict[str, Any] = {
    "format": "arena-situation",
    "version": 1,
    "id": "t-one",
    "scenario": "minimal_test",
    "steps": [{"as": "moderator", "tool": "set_phase", "args": {"text": "第 1 轮"}}],
    "decision": {"seat": "N3"},
}


def make(**over: Any) -> dict[str, Any]:
    data = {**BASE, **over}
    return {k: v for k, v in data.items() if v is not None}


def fails(data: Any, text: str) -> str:
    with pytest.raises(SituationError) as info:
        parse_situation(data, source="f.yaml")
    message = str(info.value)
    assert text in message, message
    assert message.startswith("f.yaml"), message  # (the file is named in every error)
    return message


# ------------------------------------------------------------------------------------------------ the shipped examples


def test_the_examples_in_the_fixtures_directory_are_valid_and_documented() -> None:
    files = sorted(FIXTURES.glob("_example*.yaml"))
    assert [f.name for f in files] == ["_example.yaml", "_example_pinned.yaml", "_example_speak.yaml"]
    for path in files:
        situation = load_situation(path)
        assert situation.is_example and situation.path == str(path)
        assert situation.scenario == "minimal_test" and situation.steps and situation.decision.seats == ("N3",)
    vote = load_situation(FIXTURES / "_example.yaml")
    assert vote.decision.expect == ("respond",) and vote.score[0]["correct"] == ["N1", "N2"] and vote.labels() == ["vote"]
    assert vote.judge["L1"]["blunt_side"] == ["N4"] and vote.meta == {"pair": "example", "variant": "A"} and vote.roles == {"N1": "red", "N2": "red", "N3": "blue", "N4": "blue"}
    assert vote.bots == {}
    speak = load_situation(FIXTURES / "_example_speak.yaml")
    assert speak.deal == "creation" and speak.roles == ("red", "red", "blue", "blue") and speak.judge == {"S2": {}} and speak.decision.expect == ("speak", "pass")
    pinned = load_situation(FIXTURES / "_example_pinned.yaml")
    assert list(pinned.bots) == ["N3"] and pinned.bots["N3"]["name"] == "Zed" and pinned.bots["N3"]["core"]["voice"].endswith("篇幅：20–60 字")
    assert pinned.bots["N3"]["playbook"].startswith("- [把握:高|支持:3|反例:0|来源:复盘|#投票]") and pinned.bots["N3"]["people"] == {"Alice": "话少，爱反问"}
    assert pinned.score[0]["correct"] == ["again"] and pinned.judge == {"M3": {}} and pinned.decision.expect == ("respond",)


def test_files_that_start_with_an_underscore_are_skipped_unless_asked_for(tmp_path: Path) -> None:
    """(On a directory of the test's own: what is committed to ``bench/fixtures/situations`` changes, this test does not.)"""
    for name in ("_example.yaml", "_example_speak.yaml"):
        (tmp_path / name).write_text((FIXTURES / name).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "real.yaml").write_text(yaml.safe_dump(make(id="real-one", tags=["L2"])), encoding="utf-8")
    assert [s.id for s in load_situations(tmp_path)] == ["real-one"]  # (an example is not run, nor listed, unless asked for)
    found = load_situations(tmp_path, include_examples=True)
    assert [s.id for s in found] == ["example-vote", "example-speak", "real-one"]
    assert [s.id for s in load_situations(tmp_path / "_example.yaml")] == ["example-vote"]  # (a file named outright is always taken)
    assert [s.id for s in load_situations(tmp_path, include_examples=True, ids=["example-speak"])] == ["example-speak"]
    assert [s.id for s in load_situations(tmp_path, include_examples=True, tags=["L1"])] == ["example-vote"]
    assert [s.id for s in load_situations([tmp_path / "_example.yaml", tmp_path / "real.yaml"])] == ["example-vote", "real-one"]  # (a list of files, in the order given)
    with pytest.raises(SituationError, match="找不到这些题目：nope"):
        load_situations(tmp_path, include_examples=True, ids=["nope"])
    with pytest.raises(SituationError, match="不存在"):
        situation_files(tmp_path / "missing.yaml")
    assert all(not Path(s.path).name.startswith("_") for s in load_situations(FIXTURES))  # (whatever is committed next to the examples runs by default)


def test_two_files_with_one_id_are_refused(tmp_path: Path) -> None:
    for name in ("a.yaml", "b.yaml"):
        (tmp_path / name).write_text(yaml.safe_dump(make()), encoding="utf-8")
    with pytest.raises(SituationError, match="id 't-one' 与 a.yaml 重复"):
        load_situations(tmp_path)


# ------------------------------------------------------------------------------------------------ the shape of a file


def test_a_minimal_situation_gets_its_defaults() -> None:
    situation = parse_situation(make())
    assert isinstance(situation, Situation)
    assert (situation.seed, situation.deal, situation.clock, situation.player_count, situation.names, situation.roles) == (1, "start", 0.0, None, (), None)
    assert situation.decision.seats == ("N3",) and situation.decision.seat == "N3" and situation.decision.expect == DECISION_TOOLS
    assert situation.decision.wake.mode == "redeliver" and situation.tags == () and situation.judge == {} and situation.score == ()
    step = situation.steps[0]
    assert (step.kind, step.actor, step.tool, dict(step.args), step.index, step.source) == ("tool", "moderator", "set_phase", {"text": "第 1 轮"}, 0, "steps[0]")


@pytest.mark.parametrize(
    ("change", "text"),
    [
        ({"format": "x"}, "format 必须是 arena-situation"),
        ({"version": 2}, "version 必须是 1"),
        ({"id": "Bad Id"}, "id 必须是小写字母"),
        ({"id": None}, "id 必须是小写字母"),
        ({"scenario": "../x"}, "scenario 必须是剧本的 id"),
        ({"seed": 0}, "seed 必须是正整数"),
        ({"seed": True}, "seed 必须是正整数"),
        ({"deal": "later"}, "deal 只能是 start、creation"),
        ({"clock": -1}, "clock 不能小于 0"),
        ({"clock": "soon"}, "clock 必须是数字"),
        ({"players": {"count": 1}}, "count 必须是 2 到 64 之间的整数"),
        ({"players": {"count": 4, "names": ["A", "a"]}}, "names 里有重名"),
        ({"players": {"count": 2, "names": ["A", "B", "C"]}}, "names 比 count 多"),
        ({"players": {"size": 4}}, "不认识的键：size"),
        ({"roles": []}, "roles 不能是空的"),
        ({"surprise": 1}, "不认识的键：surprise"),
    ],
)
def test_a_wrong_top_level_value_is_named(change: dict[str, Any], text: str) -> None:
    fails(make(**change), text)


def test_the_top_level_must_be_a_dictionary() -> None:
    fails(["not", "a", "dict"], "文件的顶层 必须是一个字典")
    fails(make(steps="oops"), "steps 必须是一个列表")


def test_a_file_that_is_not_yaml_or_not_there_is_a_clear_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("id: [unclosed\n", encoding="utf-8")
    with pytest.raises(SituationError, match="broken.yaml：不是合法的 YAML"):
        load_situation(broken)
    with pytest.raises(SituationError, match="nope.yaml：读不了这个文件"):
        load_situation(tmp_path / "nope.yaml")


# ------------------------------------------------------------------------------------------------ steps and macros


def test_a_tool_step_is_checked_against_the_tools_of_its_actor() -> None:
    steps = expand_steps([{"as": "moderator", "tool": "announce", "args": {"channel_id": "public", "template_id": "x"}}, {"as": "N1", "tool": "pass", "args": {"channel_id": "public"}}])
    assert [(s.actor, s.tool) for s in steps] == [("moderator", "announce"), ("N1", "pass")]
    with pytest.raises(SituationError, match=r"steps\[0\]：主持人没有工具 'speak'"):
        expand_steps([{"as": "moderator", "tool": "speak"}])
    with pytest.raises(SituationError, match=r"steps\[0\]：玩家没有工具 'open_request'"):
        expand_steps([{"as": "N1", "tool": "open_request"}])
    with pytest.raises(SituationError, match="每一步要么是 say"):
        expand_steps([{"tool": "speak"}])
    with pytest.raises(SituationError, match="不认识的键：colour"):
        expand_steps([{"as": "N1", "tool": "pass", "colour": "red"}])
    with pytest.raises(SituationError, match="as 必须是 moderator 或一个座位号"):
        expand_steps([{"as": "n 1", "tool": "pass"}])


def test_say_expands_into_one_speak_per_seat_in_the_order_written() -> None:
    steps = expand_steps([{"say": {"N2": "第二个", "N1": "第一个"}, "channel": "public", "at": 5, "label": "round"}])
    assert [(s.actor, s.tool, dict(s.args), s.at, s.label) for s in steps] == [
        ("N2", "speak", {"channel_id": "public", "content": "第二个"}, 5.0, ""),
        ("N1", "speak", {"channel_id": "public", "content": "第一个"}, None, "round"),  # (the label is the last one's: the floor is then N1's receipt)
    ]
    assert [s.source for s in steps] == ["steps[0].say[N2]", "steps[0].say[N1]"]
    for bad, text in (({"say": {"N1": ""}, "channel": "p"}, "发言内容必须是非空的字符串"), ({"say": {"N1": "x"}}, "say 需要 channel"), ({"say": {}, "channel": "p"}, "至少要有一个座位")):
        with pytest.raises(SituationError, match=text):
            expand_steps([bad])


def test_answer_expands_into_responds_and_resolves_labels() -> None:
    steps = expand_steps([
        {"as": "moderator", "tool": "open_request", "label": "vote", "args": {"to": ["N1"], "question": "q", "options": [{"id": "a"}, {"id": "b"}]}},
        {"answer": "@vote", "values": {"N1": "a", "N2": "b"}, "reasons": {"N2": "因为"}},
    ])
    assert [(s.actor, s.tool, dict(s.args)) for s in steps[1:]] == [
        ("N1", "respond", {"request_id": "@vote", "value": "a"}), ("N2", "respond", {"request_id": "@vote", "value": "b", "reason": "因为"}),
    ]
    with pytest.raises(SituationError, match="@later 指向的 label 还没有出现"):
        expand_steps([{"answer": "@later", "values": {"N1": "a"}}])
    with pytest.raises(SituationError, match="answer 必须是请求 id"):
        expand_steps([{"answer": "@bad label", "values": {"N1": "a"}}])
    with pytest.raises(SituationError, match="answer 需要 values"):
        expand_steps([{"answer": "r1"}])


def test_labels_must_be_unique_and_written_before_they_are_used() -> None:
    with pytest.raises(SituationError, match="label 'x' 重复了"):
        expand_steps([{"as": "moderator", "tool": "set_phase", "label": "x", "args": {"text": "a"}}, {"as": "moderator", "tool": "set_phase", "label": "x", "args": {"text": "b"}}])
    with pytest.raises(SituationError, match="@y 指向的 label 还没有出现"):
        expand_steps([{"as": "N1", "tool": "respond", "args": {"request_id": "@y", "value": "a"}}])
    with pytest.raises(SituationError, match="label '1x' 只能用字母"):
        expand_steps([{"as": "moderator", "tool": "set_phase", "label": "1x", "args": {"text": "a"}}])
    # a mention in a speech is plain text, not a reference
    steps = expand_steps([{"as": "N1", "tool": "speak", "args": {"channel_id": "p", "content": "@N3 你怎么看"}}])
    assert steps[0].args["content"] == "@N3 你怎么看"
    assert REF_RE.match("@vote") and REF_RE.match("@vote.request_id") and not REF_RE.match("@N3 你好") and not REF_RE.match("vote")


def test_clock_and_gate_steps() -> None:
    steps = expand_steps([{"advance": 30}, {"tick": True, "at": 12}, {"running": False, "reason": "user"}, {"running": True}, {"untimed": ["N1"]}, {"untimed": []}])
    assert [(s.kind, s.seconds, s.at, s.running, s.reason, s.seats) for s in steps] == [
        ("advance", 30.0, None, True, "", ()), ("tick", 0.0, 12.0, True, "", ()), ("running", 0.0, None, False, "user", ()),
        ("running", 0.0, None, True, "", ()), ("untimed", 0.0, None, True, "", ("N1",)), ("untimed", 0.0, None, True, "", ()),
    ]
    assert not steps[0].is_action
    for bad, text in (({"advance": -1}, "advance 不能小于 0"), ({"advance": "x"}, "advance 必须是数字"), ({"running": "yes"}, "running 必须是 true 或 false"),
                      ({"tick": True, "at": -2}, "at 不能小于 0")):
        with pytest.raises(SituationError, match=text):
            expand_steps([bad])


# ------------------------------------------------------------------------------------------------ the decision, the score, the judge


def test_the_decision_names_one_seat_or_several() -> None:
    assert parse_situation(make(decision={"seats": ["N1", "N2"]})).decision.seats == ("N1", "N2")
    fails(make(decision={"seat": "N1", "seats": ["N2"]}), "二选一")
    fails(make(decision={}), "二选一")
    fails(make(decision={"seats": ["N1", "N1"]}), "不能有重复的座位")
    fails(make(decision={"seat": "N1", "expect": ["sleep"]}), "expect 里的 'sleep' 不是玩家的工具")
    assert parse_situation(make(decision={"seat": "N1", "expect": ["respond"]})).decision.expect == ("respond",)


def test_the_wake_of_the_decision() -> None:
    steps = [{"as": "moderator", "tool": "set_phase", "label": "go", "args": {"text": "a"}}]
    assert parse_situation(make(steps=steps, decision={"seat": "N1", "wake": {"since": "go"}})).decision.wake.label == "go"
    explicit = parse_situation(make(decision={"seat": "N1", "wake": [{"event": "message.new", "channel": "talk", "sender": "N2"}]})).decision.wake
    assert explicit.mode == "explicit" and explicit.envelopes[0]["channel"] == "talk"
    fails(make(decision={"seat": "N1", "wake": {"since": "nope"}}), "wake.since 指向的 label 'nope' 不存在")
    fails(make(decision={"seat": "N1", "wake": [{"channel": "x"}]}), "需要 event")
    fails(make(decision={"seat": "N1", "wake": []}), "wake 列表不能是空的")
    fails(make(decision={"seat": "N1", "wake": "later"}), "wake 只能是")
    fails(make(decision={"seat": "N1", "wake": [{"event": "x", "colour": 1}]}), "不认识的键：colour")


def test_score_and_judge_sections() -> None:
    steps = [{"as": "moderator", "tool": "open_request", "label": "vote", "args": {"to": ["N1"], "question": "q", "options": [{"id": "a"}, {"id": "b"}]}}]
    situation = parse_situation(make(steps=steps, score={"kind": "choice", "request": "@vote", "correct": ["a"]}, judge=["S2", "L2"], meta={"pair": "p1"}, brief="  简介  "))
    assert situation.score == ({"kind": "choice", "request": "@vote", "correct": ["a"], "wrong": []},)
    assert situation.judge == {"S2": {}, "L2": {}} and situation.meta == {"pair": "p1"} and situation.brief == "简介"
    assert parse_situation(make(judge={"L3": {"decisive": "x"}})).judge == {"L3": {"decisive": "x"}}
    assert parse_situation(make(judge="S2")).judge == {"S2": {}}
    fails(make(score=[{"kind": "choice", "request": "@nope", "correct": ["a"]}]), "@nope 指向的 label 还没有出现")
    fails(make(score=[{"kind": "choice", "request": "r1"}]), "choice 需要 correct")
    fails(make(score=[{"kind": "speech", "request": "r1", "correct": ["a"]}]), "score.kind 只能是 choice")
    fails(make(judge={"bad name": {}}), "rubric 名 'bad name' 不合法")
    fails(make(judge={"L1": "text"}), "每个 rubric 的内容 必须是一个字典")


def test_every_error_says_where() -> None:
    message = fails(make(steps=[BASE["steps"][0], {"as": "moderator", "tool": "teleport"}]), "主持人没有工具 'teleport'")
    assert "steps[1]" in message and "可用的工具：announce" in message


# ------------------------------------------------------------------------------------------------ the bots a situation brings


INLINE = {
    "name": "Zed",
    "core": {"identity": "一个爱列点的老玩家。", "voice": "你说话短。\n篇幅：30–90 字", "mind": "先看事实。"},
}


def test_a_situation_can_put_a_bot_on_a_decision_seat_as_a_spec_or_as_a_snapshot() -> None:
    assert parse_situation(make()).bots == {}
    spec = parse_situation(make(bots={"N3": "  bot:Alice@no-people  "})).bots
    assert spec == {"N3": {"spec": "bot:Alice@no-people"}}
    legacy = parse_situation(make(decision={"seats": ["N3", "N4"]}, bots={"N4": "legacy:Zed"})).bots
    assert legacy == {"N4": {"spec": "legacy:Zed"}}  # (a seat without a bot is played by every bot of the run)
    full = {**INLINE, "label": "zed-v2", "notes": "我的笔记。", "digest": "平票再投。", "questions": "还不确定的事。", "record": "场次 3", "people": {"Alice": "话少"},
            "playbook": ["- [把握:高|支持:3|反例:0|来源:复盘|#投票] 先看事实。", "- [把握:低|支持:0|反例:0|来源:初始化] 想法。"]}
    inline = parse_situation(make(bots={"N3": full})).bots["N3"]
    assert inline == {
        "name": "Zed", "core": INLINE["core"], "label": "zed-v2", "notes": "我的笔记。", "digest": "平票再投。", "questions": "还不确定的事。", "record": "场次 3",
        "playbook": "- [把握:高|支持:3|反例:0|来源:复盘|#投票] 先看事实。\n- [把握:低|支持:0|反例:0|来源:初始化] 想法。", "people": {"Alice": "话少"},
    }
    assert parse_situation(make(bots={"N3": {**INLINE, "playbook": "- 一条没有方括号的打法"}})).bots["N3"]["playbook"] == "- 一条没有方括号的打法"  # (a bullet is an entry, as in playbook.md)


@pytest.mark.parametrize(
    ("bots", "text"),
    [
        ({"N9": "legacy"}, "座位 N9 不是 decision 的座位（N3）：只有被测的座位会运行"),
        ({"moderator": "legacy"}, "不是 decision 的座位"),
        ({"N3": ""}, "bot 的写法 不能是空的"),
        ({"N3": "alice"}, "bot 的种类只能是 legacy 或 bot"),
        ({"N3": "bot:"}, "bot: 后面要写默认库里的名字或一个目录"),
        ({"N3": "bot:Alice@sparkles"}, "不认识的选项 sparkles"),
        ({"N3": "legacy@nomem"}, "legacy 没有可以拿掉的层"),
        ({"N3": 7}, "bot（写成 spec 字符串，或写成一份快照） 必须是一个字典"),
        ({"N3": {**INLINE, "surprise": 1}}, "不认识的键：surprise"),
        ({"N3": {"core": INLINE["core"]}}, "name 不能是空的"),
        ({"N3": {**INLINE, "name": "x" * 33}}, "name 太长了"),
        ({"N3": {"name": "Zed"}}, "core 必须是一个字典"),
        ({"N3": {"name": "Zed", "core": {"identity": "身份", "voice": "声音"}}}, "mind 不能是空的"),
        ({"N3": {"name": "Zed", "core": {**INLINE["core"], "extra": "x"}}}, "不认识的键：extra"),
        ({"N3": {**INLINE, "playbook": "这是一段说明文字，不是打法条目"}}, "playbook 里有 1 行不是打法条目"),
        ({"N3": {**INLINE, "people": ["Alice"]}}, "people 必须是一个字典"),
        ({"N3": {**INLINE, "notes": 5}}, "notes 必须是文字"),
    ],
)
def test_a_bot_a_situation_brings_that_cannot_be_made_is_named(bots: dict[str, Any], text: str) -> None:
    message = fails(make(bots=bots), text)
    assert "bots" in message


def test_the_bots_of_a_situation_survive_a_trip_through_yaml(tmp_path: Path) -> None:
    from bench.replay import situation_to_yaml

    original = parse_situation(make(decision={"seats": ["N3", "N4"]}, bots={"N3": "bot:Alice@nomem", "N4": {**INLINE, "playbook": ["- [把握:中|支持:1|反例:0|来源:复盘|#发言] 先说结论。"], "people": {"Bob": "话多"}}}))
    again = parse_situation(yaml.safe_load(situation_to_yaml(original)), source="roundtrip")
    assert again.bots == original.bots
    path = tmp_path / "s.yaml"
    path.write_text(situation_to_yaml(original), encoding="utf-8")
    assert load_situation(path).bots == original.bots
