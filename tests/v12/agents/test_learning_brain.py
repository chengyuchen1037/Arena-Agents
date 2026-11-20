"""The learning brain (v0.14 H20, H21, H32, H34; contract §13.6): the prompts and the tools of an initialization and of a review,
against a fake ``LearningPort`` (no model, no file)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

import app.agents.learning_brain as learning_module
from app.agents.learning_brain import (
    INIT_FILES,
    INIT_REQUIRED,
    READ_RECORD_DEFAULT,
    READ_RECORD_MAX,
    LearningBrain,
    build_learning_brain,
    core_rule,
    file_rule,
    fill_prompt,
    load_prompt,
    prompt_path,
)
from app.bots.types import (
    ARCH_NOTES_CHARS,
    CORE_LIMITS,
    DIGEST_CHARS,
    MEMORY_PARTS,
    PLAYBOOK_ENTRY_CHARS,
    PLAYBOOK_MAX,
    QUESTIONS_CHARS,
    BotMind,
    ContractDoc,
    LearningJob,
    WriteResult,
)

from ..test_neutrality import FIXED_PARTICIPANT_ID_RE, FORBIDDEN_TERMS
from .bot_fakes import EXTRA, IDENTITY, MIND, VOICE, make_mind

ROOT = Path(__file__).resolve().parents[3]
LIMITS = {"arch_notes": ARCH_NOTES_CHARS, "digest": DIGEST_CHARS, "questions": QUESTIONS_CHARS}


class FakePort:
    """A ``LearningPort`` over a dict: it keeps what is written, refuses what is over a limit (the way the real port will) and records
    every call."""

    def __init__(self, **sources: str) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.files: dict[str, str] = {}
        self.proposals: list[tuple[str, str, str]] = []
        self.sources = {"rules": "官方规则正文（玩家能看到的部分）", "my_core": "我的底层设定全文", "records": "#1 记录一\n#2 记录二", **sources}
        self.refuse: dict[str, str] = {}
        self.fail: str | None = None  # a method that raises

    def read_source(self, source: str, part: str | None = None) -> str:
        self.calls.append(("read_source", source, part))
        if self.fail == "read_source":
            raise RuntimeError("数据目录不可读")
        return self.sources.get(f"{source}:{part}", self.sources.get(source, ""))

    def write_memory(self, file: str, content: str) -> WriteResult:
        self.calls.append(("write_memory", file, content))
        if self.fail == "write_memory":
            raise RuntimeError("磁盘已满")
        if file in self.refuse:
            return WriteResult(False, self.refuse[file], file, 0)
        if file in LIMITS and len(content.strip()) > LIMITS[file]:
            return WriteResult(False, f"最多 {LIMITS[file]} 字（现在 {len(content.strip())} 字）", file, 0)
        self.files[file] = content
        return WriteResult(True, "", file, len(content.strip()))

    def propose_core(self, section: str, content: str, reason: str) -> WriteResult:
        self.calls.append(("propose_core", section, content, reason))
        if section in CORE_LIMITS and len(content) > CORE_LIMITS[section]:
            return WriteResult(False, f"「{section}」最多 {CORE_LIMITS[section]} 字（现在 {len(content)} 字）", section, 0)
        self.proposals.append((section, content, reason))
        return WriteResult(True, "", section, len(content))

    def read_record(self, query: str | None = None, start: int | None = None, limit: int = 40) -> str:
        self.calls.append(("read_record", query, start, limit))
        return self.sources["records"] if query != "没有的词" else ""


def init_job(**over: Any) -> LearningJob:
    data: dict[str, Any] = {
        "kind": "init", "mind": BotMind(bot_id="b-1a2b3c4d", name="Alex", core=make_mind().core),  # (an L0 and nothing learned yet)
        "scenario_id": "fake", "scenario_title": "测试剧本", "contract": ContractDoc(), "deadline_s": 120,
    }
    data.update(over)
    return LearningJob(**data)


def review_job(**over: Any) -> LearningJob:
    data: dict[str, Any] = {
        "kind": "review", "mind": make_mind(), "scenario_id": "fake", "scenario_title": "测试剧本", "contract": ContractDoc(),
        "deadline_s": 60, "seat_id": "p1", "outcome_text": "你所在的阵营获胜了。", "role_text": "你的身份是甲（甲方）。",
        "others_text": "p2 Blair：乙（乙方），未获胜\np3 Casey：甲（甲方），获胜",
    }
    data.update(over)
    return LearningJob(**data)


def brain_for(job: LearningJob | None = None, port: FakePort | None = None) -> tuple[LearningBrain, FakePort]:
    port = port or FakePort()
    return build_learning_brain(job or init_job(), port), port


def run(brain: LearningBrain, name: str, **args: Any):
    return brain.execute_tool(name, dict(args))


def names(tools: list[dict]) -> list[str]:
    return [tool["function"]["name"] for tool in tools]


# ------------------------------------------------------------------------------------------------ the interface (§13.6)


def test_a_learning_brain_has_the_interface_the_runner_uses() -> None:
    brain, _ = brain_for()
    assert isinstance(brain, LearningBrain)
    for member in ("system_prompt", "first_message", "tool_schemas", "execute_tool", "no_tool_nudge"):
        assert callable(getattr(brain, member))
    assert brain.written == () and brain.proposed == () and brain.finished is False and brain.summary == ""
    assert isinstance(brain.system_prompt(), str) and isinstance(brain.first_message(), str) and isinstance(brain.tool_schemas(), list)
    with pytest.raises(ValueError, match="init 或 review"):
        build_learning_brain(init_job(kind="play"), FakePort())  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------ the prompts


def test_the_prompt_files_are_game_neutral_and_have_no_stale_mechanics() -> None:
    for kind in ("init", "review"):
        text = prompt_path(kind).read_text(encoding="utf-8")
        assert not [term for term in FORBIDDEN_TERMS if term in text], kind
        assert not FIXED_PARTICIPANT_ID_RE.search(text) and "\ufffd" not in text
        for stale in ("debounce", "free_limits", "ambient", "你的思考不会被保留", "SEAT "):
            assert stale not in text, (kind, stale)
        assert not re.search(r"`wait`|\bwait\(", text) and not re.search(r"(?<![A-Za-z0-9_])p(?:0[1-9]|1[0-2])(?![A-Za-z0-9_])", text)
        assert "简短" not in text  # nobody is asked to think less
        assert prompt_path(kind).is_file() and load_prompt(kind) == text


def test_an_initialization_asks_for_understanding_and_hypotheses_in_the_bots_own_words_and_gives_no_play() -> None:
    brain, _ = brain_for()
    system = brain.system_prompt()
    for asked in ("用你自己的话", "它们都是你的假设", "以后对局的结果来检验", "我理解", "我打算", "我还不确定", "不要照抄"):
        assert asked in system, asked
    assert "不要分条列项" in system and "不要用标题或加粗" in system  # (what it writes is mounted as it is: the shape is copied)
    assert "原样保留" in system and "修订，不是重来" in system  # (re-initializing keeps what the games taught)
    for number in (str(ARCH_NOTES_CHARS), str(DIGEST_CHARS), str(PLAYBOOK_MAX), str(PLAYBOOK_ENTRY_CHARS), str(QUESTIONS_CHARS), "120 秒"):
        assert number in system, number
    assert "- [把握:低|支持:0|反例:0|来源:初始化|#标签] 正文" in system  # the line of a playbook entry (contract §13.2)
    assert "操作笔记、规则要点、打法这三个文件必须都写" in system and "原来的记忆不会有任何改动" in system
    for tool in ("read_source", "write_memory", "finish"):
        assert tool in system
    assert "propose_core" not in system and "read_record" not in system and "people" in system  # (people: "not written here")
    assert "{{" not in system and "}}" not in system
    # the strategy is the bot's to find: nothing in the prompt says how to play
    assert "你打算怎么玩" in system and "应该" not in system.split("【初始化】")[1].replace("不应该", "")


def test_a_review_holds_the_memory_against_the_truth_and_cuts_the_loop_of_trusting_tidy_speech() -> None:
    brain, _ = brain_for(review_job())
    system = brain.system_prompt()
    for asked in ("真相已经公开", "把你当时做出的判断一个一个对照真相", "哪些对了、哪些错了", "各自靠的是什么", "据此修订你的记忆", "都是假设"):
        assert asked in system, asked
    # the rule about impressions (G39) and the new one: the tidiness of a speech is no ground for trust
    assert "印象只写一个人说话和行动的风格、可信度和习惯" in system and "不写“他是什么身份”" in system and "身份每一局都会重新分配" in system
    assert "可信度的依据只能是" in system and "有没有被事实证实或推翻" in system
    assert "话说得有没有条理、语气稳不稳、写得整不整齐，和话的真假没有关系，不要把这些当成理由" in system
    assert "write_memory 是整份覆盖" in system and "先用 read_source 读出这个文件现在的内容" in system
    assert "propose_core" in system and "只有使用者能改" in system and "不会自动生效" in system
    assert "已经写好的会保留" in system and "60 秒" in system
    assert "支持数加一" in system and "反例数加一" in system and "把握先写低" in system
    assert "{{" not in system and "}}" not in system
    # no human, no style: the two paragraphs are not there
    assert "代你出场" not in system and "辱骂" not in system and "说话的方式" not in system.split("【复盘】")[1]


def test_the_paragraph_about_a_person_who_played_the_seat_is_only_for_that_seat() -> None:
    plain, _ = brain_for(review_job())
    assert "代你出场" not in plain.system_prompt() and "代你出场" not in plain.first_message() and "有人" not in plain.first_message()
    human, _ = brain_for(review_job(human_note="第 12–30 条记录是别人代你出场的。"))
    system = human.system_prompt()
    assert "这一局有一段不是你自己在说话，而是别人代你出场" in system and "对你有用的做法可以写进打法" in system
    assert "辱骂" not in system  # (no style requested: nothing to learn of the voice)
    assert "【有人代你出场】\n第 12–30 条记录是别人代你出场的。" in human.first_message()


def test_learning_a_style_may_only_be_proposed_and_keeps_no_abuse() -> None:
    brain, _ = brain_for(review_job(human_note="第 12–30 条记录是别人代你出场的。", learn_style=True))
    system = brain.system_prompt()
    assert "从那一段里学习那个人说话的方式" in system
    assert "这只能通过 propose_core 提出：section 填 voice" in system and "最多 600 字" in system
    assert "建议要使用者确认才会生效" in system
    assert "辱骂、人身攻击和粗口一律不要保留，样本里也不要出现" in system and "学的是说话的方式，不是脏字" in system
    assert "用「」标出的原话样本" in system and "篇幅区间" in system  # (the format of a voice stays)
    off, _ = brain_for(review_job(human_note="x"))
    assert "propose_core 提出：section 填 voice" not in off.system_prompt()


def test_the_bot_studies_as_itself_its_l0_comes_first() -> None:
    brain, _ = brain_for(review_job())
    system = brain.system_prompt()
    assert system.startswith("你是 Alex（本局编号 p1）。" + IDENTITY)
    for part in (VOICE.strip(), MIND.strip(), f"{EXTRA[0]}：{EXTRA[1]}"):
        assert part in system
    assert system.index(MIND.strip()) < system.index("【复盘】")
    init, _ = brain_for(init_job())
    assert init.system_prompt().startswith("你是 Alex。" + IDENTITY) and "本局编号" not in init.system_prompt()  # (no seat in an initialization)
    assert init.system_prompt().index("【初始化】") > init.system_prompt().index(IDENTITY)


def test_a_bot_without_an_l0_still_gets_the_task() -> None:
    bare = BotMind(bot_id="b-00000000", name="")
    brain, _ = brain_for(init_job(mind=bare))
    assert brain.system_prompt().startswith("【初始化】")


def test_the_variables_are_filled_once_and_what_is_put_in_is_not_scanned_again() -> None:
    brain, _ = brain_for(init_job(scenario_title="第{{seconds}}号\n剧本"))
    system = brain.system_prompt()
    assert "《第{{seconds}}号 剧本》" in system  # the title is text, not a template (and is one line)
    text = fill_prompt("甲{{a}}乙{{b}}{{#x}}丙{{a}}{{/x}}{{#y}}丁{{/y}}", {"x": True}, {"a": "{{b}}"})
    assert text == "甲{{b}}乙{{b}}丙{{b}}\n"
    assert fill_prompt("a\n\n\n\nb", {}, {}) == "a\n\nb\n"


def test_a_missing_prompt_file_falls_back_on_a_short_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.config as app_config

    monkeypatch.setattr(app_config, "BASE_DIR", tmp_path)
    assert load_prompt("init").startswith("【初始化】") and "120" not in load_prompt("init")  # (the number is a variable)
    brain, _ = brain_for()
    assert "write_memory" in brain.system_prompt() and "你只有 120 秒" in brain.system_prompt() and "{{" not in brain.system_prompt()
    (tmp_path / "prompts" / "learning").mkdir(parents=True)
    (tmp_path / "prompts" / "learning" / "init.md").write_text("  \n", encoding="utf-8")
    assert load_prompt("init").startswith("【初始化】")  # (an empty file is a missing one)
    (tmp_path / "prompts" / "learning" / "review.md").write_text("我的复盘提示，{{seconds}} 秒", encoding="utf-8")
    assert brain_for(review_job())[0].system_prompt().endswith("我的复盘提示，60 秒\n")


def test_the_example_lines_of_the_prompts_are_what_the_store_reads() -> None:
    """The line format the prompts teach is the one ``app.storage.botfiles`` parses (contract §13.2): a bot that follows the
    prompt writes entries the store understands."""
    botfiles = pytest.importorskip("app.storage.botfiles")
    init = prompt_path("init").read_text(encoding="utf-8")
    example = re.search(r"`(- \[把握:[^`]+)`", init)
    assert example is not None
    entries, ignored = botfiles.parse_playbook_ex(example.group(1))
    assert ignored == 0 and len(entries) == 1
    assert (entries[0].confidence, entries[0].support, entries[0].counter, entries[0].source, entries[0].tags, entries[0].text) == (
        "低", 0, 0, "初始化", ("标签",), "正文"
    )
    review = prompt_path("review").read_text(encoding="utf-8")
    people = re.search(r"`(- 名字：印象)`", review)
    assert people is not None
    parsed, ignored_people = botfiles.parse_people_ex(people.group(1))
    assert parsed == {"名字": "印象"} and ignored_people == 0


# ------------------------------------------------------------------------------------------------ the first message


def test_the_first_message_of_an_initialization_says_what_the_bot_has_already() -> None:
    fresh = init_job(mind=BotMind(bot_id="b-1", name="Alex", core=make_mind().core))
    assert brain_for(fresh)[0].first_message() == "现在开始初始化《测试剧本》，你有 120 秒。你现在还没有任何记忆。"
    rich = brain_for(init_job(mind=make_mind()))[0].first_message()
    assert rich.startswith("现在开始初始化《测试剧本》，你有 120 秒。你现在已经有这些记忆：")
    assert "操作笔记 19 字" in rich and "规则要点 " in rich and "打法 5 条" in rich and "待解问题 " in rich
    assert "印象" not in rich  # (an initialization does not touch the impressions)
    assert rich.endswith("这次是修订，请先用 read_source 读出来再改。")


def test_the_first_message_of_a_review_has_the_truth_the_jots_and_the_memory() -> None:
    brain, _ = brain_for(review_job(jots=("p2 的票和话对不上", "  ", "首夜为什么那样走"), human_note="第 12–30 条记录是别人代你出场的。"))
    text = brain.first_message()
    parts = text.split("\n\n")
    assert parts[0] == "这局《测试剧本》结束了，你有 60 秒复盘。"
    assert "【结果】你所在的阵营获胜了。" in parts and "【你的身份】你的身份是甲（甲方）。" in parts
    assert "【大家的身份与结果】\np2 Blair：乙（乙方），未获胜\np3 Casey：甲（甲方），获胜" in parts
    assert "【你在对局中记下的线索】\np2 的票和话对不上\n首夜为什么那样走" in parts
    assert "【有人代你出场】\n第 12–30 条记录是别人代你出场的。" in parts
    assert parts[-1].startswith("你现在已经有这些记忆：") and "印象 2 人" in parts[-1] and "先用 read_record 看看这一局你能看到的记录" in parts[-1]
    bare, _ = brain_for(review_job(outcome_text="", role_text="", others_text="", mind=BotMind(bot_id="b-1", name="Alex", core=make_mind().core)))
    assert bare.first_message().split("\n\n")[0] == "这局《测试剧本》结束了，你有 60 秒复盘。" and "【结果】" not in bare.first_message()
    assert "你现在还没有任何记忆。先用 read_record" in bare.first_message()


# ------------------------------------------------------------------------------------------------ the tools


def test_an_initialization_has_three_tools_and_a_review_five() -> None:
    init, _ = brain_for()
    review, _ = brain_for(review_job())
    assert names(init.tool_schemas()) == ["read_source", "write_memory", "finish"]
    assert names(review.tool_schemas()) == ["read_source", "write_memory", "finish", "read_record", "propose_core"]
    write_init = next(t["function"] for t in init.tool_schemas() if t["function"]["name"] == "write_memory")
    write_review = next(t["function"] for t in review.tool_schemas() if t["function"]["name"] == "write_memory")
    assert write_init["parameters"]["properties"]["file"]["enum"] == list(INIT_FILES) == ["arch_notes", "digest", "playbook", "questions"]
    assert write_review["parameters"]["properties"]["file"]["enum"] == list(MEMORY_PARTS) and "people" in write_review["parameters"]["properties"]["file"]["enum"]
    assert "初始化结束时统一检查" in write_init["description"] and "立即生效" in write_review["description"]
    for brain in (init, review):
        text = json.dumps(brain.tool_schemas(), ensure_ascii=False)
        for forbidden in ("$defs", "$ref", "anyOf"):
            assert forbidden not in text
        for tool in brain.tool_schemas():
            function = tool["function"]
            assert tool["type"] == "function" and function["description"] and function["parameters"]["type"] == "object"
        assert "整份" in text
    sources = next(t["function"] for t in init.tool_schemas() if t["function"]["name"] == "read_source")["parameters"]
    assert sources["required"] == ["source"] and sources["properties"]["source"]["enum"] == ["contract", "rules", "materials", "my_core", "my_memory"]
    propose = next(t["function"] for t in review.tool_schemas() if t["function"]["name"] == "propose_core")["parameters"]
    assert propose["required"] == ["section", "content", "reason"]


def test_the_tools_a_run_does_not_have_are_unknown_to_it() -> None:
    init, port = brain_for()
    for name in ("read_record", "propose_core", "speak", "list_documents"):
        text = run(init, name).text
        assert f"未知工具：{name}" in text and "read_source、write_memory、finish" in text
    assert port.calls == []
    assert "参数必须是 JSON 对象" in init.execute_tool("write_memory", ["x"]).text  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------ read_source


def test_read_source_asks_the_port_and_labels_the_answer() -> None:
    port = FakePort(**{"contract:must_read": "必读正文", "contract:reference": "参考正文", "contract": "整份契约", "materials": "- a\n- b",
                       "materials:a": "材料 a 的内容", "my_memory": "全部记忆", "my_memory:playbook": "打法文件"})
    brain, _ = brain_for(port=port)
    assert run(brain, "read_source", source="contract", part="must_read").text == "【平台契约·必读】\n必读正文"
    assert run(brain, "read_source", source="contract", part="reference").text == "【平台契约·参考】\n参考正文"
    assert run(brain, "read_source", source="contract").text == "【平台契约】\n整份契约"
    assert run(brain, "read_source", source="rules").text == "【官方规则】\n官方规则正文（玩家能看到的部分）"
    assert run(brain, "read_source", source="materials").text == "【学习材料列表】\n- a\n- b"
    assert run(brain, "read_source", source="materials", part="a").text == "【学习材料·a】\n材料 a 的内容"
    assert run(brain, "read_source", source="my_core").text == "【我的底层设定】\n我的底层设定全文"
    assert run(brain, "read_source", source="my_memory").text == "【我的记忆】\n全部记忆"
    assert run(brain, "read_source", source="my_memory", part="playbook").text == "【我的记忆·打法】\n打法文件"
    assert port.calls == [
        ("read_source", "contract", "must_read"), ("read_source", "contract", "reference"), ("read_source", "contract", None),
        ("read_source", "rules", None), ("read_source", "materials", None), ("read_source", "materials", "a"),
        ("read_source", "my_core", None), ("read_source", "my_memory", None), ("read_source", "my_memory", "playbook"),
    ]
    assert run(brain, "read_source", source="rules", part="  ").text.startswith("【官方规则】") and port.calls[-1] == ("read_source", "rules", None)


def test_read_source_refuses_what_it_does_not_offer_without_asking_the_port() -> None:
    brain, port = brain_for()
    assert "source 必须是：contract、rules、materials、my_core、my_memory" in run(brain, "read_source", source="../etc/passwd").text
    assert "source 必须是" in run(brain, "read_source").text and "source 必须是" in run(brain, "read_source", source="fixtures").text
    assert "part 只能是 must_read" in run(brain, "read_source", source="contract", part="全部").text
    assert "part 只能是：arch_notes、digest、playbook、questions、people" in run(brain, "read_source", source="my_memory", part="secrets").text
    assert port.calls == []  # (nothing outside the five sources, and no part outside the memory files, ever reaches the port)


def test_an_empty_source_is_said_to_be_empty_and_a_failing_port_does_not_end_the_run() -> None:
    brain, port = brain_for()
    assert run(brain, "read_source", source="materials").text == "【学习材料列表】\n（这一项是空的。）"
    port.fail = "read_source"
    out = run(brain, "read_source", source="rules")
    assert "工具 read_source 没有成功" in out.text and "数据目录不可读" in out.text and not out.ends_run


# ------------------------------------------------------------------------------------------------ write_memory


def test_a_write_is_stored_by_the_port_and_the_bot_is_told_what_is_still_missing() -> None:
    brain, port = brain_for()
    first = run(brain, "write_memory", file="arch_notes", content="我打算先看状态板，再决定要不要说话。")
    assert first.text == "已写入 arch_notes（操作笔记，18 字）。还没写的必需文件：digest、playbook。" and not first.ends_run
    second = run(brain, "write_memory", file="playbook", content="- [把握:低|支持:0|反例:0|来源:初始化|#发言] 先听完再说。")
    assert second.text.endswith("还没写的必需文件：digest。")
    third = run(brain, "write_memory", file="digest", content="我理解这个游戏。")
    assert third.text.endswith("三个必需的文件都写好了，可以再检查一遍，或者调用 finish 结束。")
    assert brain.written == ("arch_notes", "playbook", "digest") and brain.missing == ()
    assert port.files["arch_notes"] == "我打算先看状态板，再决定要不要说话。"
    run(brain, "write_memory", file="arch_notes", content="第二版。")  # (a file written again is one file)
    assert brain.written == ("arch_notes", "playbook", "digest") and port.files["arch_notes"] == "第二版。"
    optional = run(brain, "write_memory", file="questions", content="我还不确定平局怎么处理。")
    assert "questions" in brain.written and "三个必需的文件都写好了" in optional.text


def test_a_refused_write_says_why_what_the_limit_is_and_what_to_do() -> None:
    brain, port = brain_for()
    out = run(brain, "write_memory", file="digest", content="字" * (DIGEST_CHARS + 20))
    assert out.text == (
        f"没有写入 digest：最多 {DIGEST_CHARS} 字（现在 {DIGEST_CHARS + 20} 字）。digest 的要求：最多 {DIGEST_CHARS} 字，自由文本。请改好后整份重写一次。"
    )
    assert brain.written == () and "digest" not in port.files and not out.ends_run
    port.refuse["playbook"] = "一条也没有读出来：每行要写成 - [把握:…] 正文"
    bad = run(brain, "write_memory", file="playbook", content="- 先听完再说")
    assert "没有写入 playbook：一条也没有读出来" in bad.text and f"最多 {PLAYBOOK_MAX} 条，一条一行" in bad.text and f"正文不超过 {PLAYBOOK_ENTRY_CHARS} 字" in bad.text
    port.refuse["questions"] = ""
    assert "没有写入 questions：没有说明原因" in run(brain, "write_memory", file="questions", content="x").text
    assert brain.written == ()
    # the real store's refusal is a whole sentence already (what is wrong, the limit, that nothing was saved): it is passed on as it is
    port.refuse["digest"] = "「规则要点」最多 800 字（现在 900 字）。 没有保存，请改好后重新写入。"
    real = run(brain, "write_memory", file="digest", content="x")
    assert real.text == "「规则要点」最多 800 字（现在 900 字）。 没有保存，请改好后重新写入。（digest 的要求：最多 800 字，自由文本。）"
    assert file_rule("people") == "最多 30 人，一人一行，写成 - 名字：印象，每条不超过 80 字，名字里不能有冒号"
    assert file_rule("questions") == f"最多 {QUESTIONS_CHARS} 字，自由文本"


def test_the_ports_own_sentence_about_an_accepted_write_is_what_the_bot_reads() -> None:
    class Chatty(FakePort):
        def write_memory(self, file: str, content: str) -> WriteResult:
            super().write_memory(file, content)
            return WriteResult(True, "已写入打法（1 条，12 字）。有 1 行没有读出，已忽略。", file, 12)

    brain, _ = brain_for(port=Chatty())
    out = run(brain, "write_memory", file="playbook", content="x")
    assert out.text == "已写入打法（1 条，12 字）。有 1 行没有读出，已忽略。还没写的必需文件：arch_notes、digest。"
    assert brain.written == ("playbook",)


def test_a_write_has_to_name_a_file_of_this_run_and_have_content() -> None:
    brain, port = brain_for()
    assert "file 必须是：arch_notes、digest、playbook、questions。初始化时不写印象（people）" in run(brain, "write_memory", file="people", content="x").text
    assert "file 必须是：arch_notes、digest、playbook、questions。" in run(brain, "write_memory", file="../x", content="x").text
    assert "file 必须是" in run(brain, "write_memory", content="x").text
    for content in ("", "  \n ", None, 5):
        assert "没有写入 digest：content 不能是空的" in run(brain, "write_memory", file="digest", content=content).text
    assert port.calls == [] and brain.written == ()
    review, review_port = brain_for(review_job())
    assert "已经生效" in run(review, "write_memory", file="people", content="- Blair：话多，爱站边").text
    assert review.written == ("people",) and review_port.files["people"].startswith("- Blair")
    assert "file 必须是：arch_notes、digest、playbook、questions、people" in run(review, "write_memory", file="record").text


def test_a_review_has_no_required_files_and_a_write_is_in_force_at_once() -> None:
    brain, _ = brain_for(review_job())
    out = run(brain, "write_memory", file="digest", content="我对规则的理解修订了。")
    assert out.text == "已写入 digest（规则要点，11 字）。已经生效。" and brain.missing == ()


def test_a_port_that_fails_on_a_write_is_told_to_the_bot_and_nothing_counts_as_written() -> None:
    brain, port = brain_for()
    port.fail = "write_memory"
    out = run(brain, "write_memory", file="digest", content="x")
    assert "工具 write_memory 没有成功：RuntimeError：磁盘已满" in out.text and brain.written == ()


# ------------------------------------------------------------------------------------------------ finish


def test_finishing_an_initialization_too_early_is_pointed_out_once() -> None:
    brain, _ = brain_for()
    run(brain, "write_memory", file="arch_notes", content="笔记")
    early = run(brain, "finish", summary="写完了")
    assert not early.ends_run and not brain.finished
    assert "先别结束：这几个必需的文件还没有写进去：digest、playbook。" in early.text and "这次初始化会因为缺文件而作废，你原来的记忆不会有任何改动" in early.text
    second = run(brain, "finish", summary="  还是结束吧\n  谢谢  ")
    assert second.ends_run and second.text == "已结束。" and brain.finished and brain.summary == "还是结束吧 谢谢"
    assert brain.missing == ("digest", "playbook")  # (the run fails as a whole: that is the port's to do)


def test_finishing_with_everything_written_ends_at_once() -> None:
    brain, _ = brain_for()
    for file in INIT_REQUIRED:
        run(brain, "write_memory", file=file, content="内容")
    out = run(brain, "finish")
    assert out.ends_run and brain.finished and brain.summary == ""
    long = brain_for()[0]
    for file in INIT_REQUIRED:
        run(long, "write_memory", file=file, content="内容")
    run(long, "finish", summary="字" * 500)
    assert len(long.summary) == 200


def test_a_review_may_finish_with_nothing_written() -> None:
    brain, _ = brain_for(review_job())
    out = run(brain, "finish", summary="这一局没有什么要改的")
    assert out.ends_run and brain.finished and brain.summary == "这一局没有什么要改的"


# ------------------------------------------------------------------------------------------------ several calls in one answer


def test_several_tool_calls_in_one_answer_are_run_one_after_the_other() -> None:
    """The runner executes the calls of an answer in order: everything the model wants done in one breath is done."""
    brain, port = brain_for()
    answer = [
        ("read_source", {"source": "rules"}),
        ("write_memory", {"file": "arch_notes", "content": "笔记"}),
        ("write_memory", {"file": "digest", "content": "要点"}),
        ("write_memory", {"file": "playbook", "content": "- [把握:低|支持:0|反例:0|来源:初始化] 先听完再说"}),
        ("finish", {"summary": "一次写完"}),
    ]
    outcomes = [brain.execute_tool(name, args) for name, args in answer]
    assert [o.ends_run for o in outcomes] == [False, False, False, False, True]
    assert brain.written == INIT_REQUIRED and brain.finished and set(port.files) == set(INIT_REQUIRED)
    assert "三个必需的文件都写好了" in outcomes[3].text


def test_a_finish_in_front_of_the_writes_of_the_same_answer_only_costs_a_reminder() -> None:
    brain, _ = brain_for()
    first = [("finish", {}), ("write_memory", {"file": "arch_notes", "content": "笔记"}), ("write_memory", {"file": "digest", "content": "要点"}),
             ("write_memory", {"file": "playbook", "content": "- [把握:低] 先听完再说"})]
    outcomes = [brain.execute_tool(name, args) for name, args in first]
    assert not outcomes[0].ends_run and "先别结束" in outcomes[0].text and brain.missing == ()
    assert brain.execute_tool("finish", {}).ends_run  # (the next round of the run: the files are there)


# ------------------------------------------------------------------------------------------------ read_record (review)


def test_read_record_passes_the_query_and_the_page_and_clamps_the_page() -> None:
    brain, port = brain_for(review_job())
    assert run(brain, "read_record").text == "#1 记录一\n#2 记录二"
    run(brain, "read_record", query=" 投票 ", start=30, limit=10)
    run(brain, "read_record", query="", start="5", limit="abc")
    run(brain, "read_record", start=-4, limit=5000)
    run(brain, "read_record", limit=0)
    assert [c for c in port.calls if c[0] == "read_record"] == [
        ("read_record", None, None, READ_RECORD_DEFAULT), ("read_record", "投票", 30, 10), ("read_record", None, 5, READ_RECORD_DEFAULT),
        ("read_record", None, 0, READ_RECORD_MAX), ("read_record", None, None, 1),
    ]
    assert run(brain, "read_record", query="没有的词").text == "（没有符合的记录。）"


# ------------------------------------------------------------------------------------------------ propose_core (review)


def test_a_proposal_goes_to_the_port_and_is_not_in_force() -> None:
    brain, port = brain_for(review_job(human_note="x", learn_style=True))
    out = run(brain, "propose_core", section="voice", content="新的说话方式……", reason="从代打的那一段里学到了短句")
    assert out.text == "已提交对「说话方式」的修改建议。它不会自动生效，要使用者确认后才会改。" and not out.ends_run
    assert port.proposals == [("voice", "新的说话方式……", "从代打的那一段里学到了短句")] and brain.proposed == ("voice",)
    run(brain, "propose_core", section="voice", content="又一版", reason="再想想")
    ok_extra = run(brain, "propose_core", section="extra:x1", content="新的口头禅", reason="学来的")
    assert "附加分区 x1" in ok_extra.text and brain.proposed == ("voice", "extra:x1") and len(port.proposals) == 3
    assert brain.written == ()  # (a proposal is no memory file)


def test_a_proposal_that_does_not_make_sense_or_is_refused_says_so() -> None:
    brain, port = brain_for(review_job())
    for section in ("", "persona", "extra:", "extra:Bad Slug", "extra:-x", "memory"):
        assert "section 必须是 identity、voice、mind，或者 extra:<附加分区的标识>" in run(brain, "propose_core", section=section, content="x", reason="y").text
    assert "content 要写这个分区修改后的完整文字" in run(brain, "propose_core", section="voice", content=" ", reason="y").text
    assert "reason 要说明为什么" in run(brain, "propose_core", section="voice", content="x", reason="").text
    assert port.proposals == []
    long = run(brain, "propose_core", section="identity", content="字" * (CORE_LIMITS["identity"] + 1), reason="想改")
    assert long.text == (
        f"没有提交这条建议：「identity」最多 {CORE_LIMITS['identity']} 字（现在 {CORE_LIMITS['identity'] + 1} 字）。"
        f"这个分区最多 {CORE_LIMITS['identity']} 字，要写修改后的完整文字，不是增量。请改好后再提一次。"
    )
    assert brain.proposed == () and core_rule("extra:x1").startswith("这个分区最多 300 字")


# ------------------------------------------------------------------------------------------------ the end of a run


def test_an_answer_without_a_tool_is_nudged_once_then_the_run_may_end() -> None:
    brain, _ = brain_for()
    run(brain, "write_memory", file="arch_notes", content="笔记")
    first = brain.no_tool_nudge("我已经想好了，下面是我的笔记……", 0)
    assert first is not None and "只写在回答里的话不会被保存" in first and "还没写的必需文件：digest、playbook。" in first and first.endswith("调用 finish。")
    assert brain.no_tool_nudge("还有话", 1) is None  # (the second time: nothing, the run ends)
    review, _ = brain_for(review_job())
    nudge = review.no_tool_nudge("", 0)
    assert nudge is not None and "write_memory" in nudge and "propose_core" in nudge and "调用 finish 结束" in nudge


def test_after_finish_there_is_nothing_to_nudge() -> None:
    brain, _ = brain_for(review_job())
    run(brain, "finish")
    assert brain.no_tool_nudge("", 0) is None


def test_the_brain_describes_a_run_without_a_word_of_the_memory() -> None:
    brain, _ = brain_for(review_job())
    run(brain, "write_memory", file="people", content="- 机密的印象：不应该出现在日志里")
    run(brain, "finish")
    line = brain.describe()
    assert json.loads(line) == {"kind": "review", "written": ["people"], "proposed": [], "finished": True} and "机密" not in line


# ------------------------------------------------------------------------------------------------ neutrality and reach


def test_the_module_is_game_neutral() -> None:
    text = Path(learning_module.__file__).read_text(encoding="utf-8")
    assert not [term for term in FORBIDDEN_TERMS if term in text] and not FIXED_PARTICIPANT_ID_RE.search(text)


def test_the_brain_reaches_nothing_but_its_port() -> None:
    """No path, no file, no model: the five sources and the memory files are all a learning run can name, and the benchmark's
    fixtures (H50) are not among them."""
    source = Path(learning_module.__file__).read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    for word in ("open(", ".write_text(", "read_bytes", "subprocess", "requests", "httpx", "bench", "fixtures", ".complete("):
        assert word not in code, word
    brain, port = brain_for(review_job())
    for source_name in ("bench", "fixtures", "bench/fixtures", "../bench", "situations"):
        assert "source 必须是" in run(brain, "read_source", source=source_name).text
    assert port.calls == []
