"""v0.14: the editors of the bot panel say the same thing as the backend (static/js/botlib.js vs app/storage/botfiles.py).

The editors check what is typed *before* they send anything: the counters, the "over the limit" hints, a Save that stays off, the list
of faults under an L0 that cannot be saved, the preview of a draft the moderator's model wrote.  If these drift from the server's own rules
the page shows a green counter for a text that is then refused, or refuses a text the server would take.  The pure helpers of
``botlib.js`` run under node here, on a table of inputs, and are compared with the backend's real functions (docs/v0.12-contracts.md
§13.2 / §13.7):

* the limits the page assumes until ``GET /api/bots`` tells it better (``limits_payload()``);
* ``validate_core`` (every fault of an L0, in the server's words and order), ``render_core_markdown`` and ``parse_core_markdown`` (the merged
  L0 file: header, sections, the faults of its structure; the faults of the limits are what the page shows as warnings of a draft);
* ``parse_playbook_ex`` / ``parse_people_ex`` (what is read out of a text typed in the memory editor);
* ``normalize_part`` (what makes a text for a part of the memory refused) and ``clean_name`` (a name).

Skipped when node is missing, like ``tests/test_frontend_js.py``.  (It replaces ``test_roster_drift.py`` of v0.13.1: the persona / memory text
that file compared with ``app/storage/roster.py`` no longer exists in the page - the bots are files now.)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.bots.types import CoreFiles
from app.storage import botfiles
from app.storage.bots import limits_payload
from app.storage.roster import RosterError, clean_name

ROOT = Path(__file__).resolve().parent.parent.parent
BOTLIB_JS = ROOT / "static" / "js" / "botlib.js"

_WINDOWS_NODE = Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"


def _find_node() -> str | None:
    env = os.environ.get("NODE_BIN")
    if env and Path(env).exists():
        return env
    return shutil.which("node") or (str(_WINDOWS_NODE) if _WINDOWS_NODE.exists() else None)


NODE = _find_node()
pytestmark = pytest.mark.skipif(NODE is None, reason="node executable not found")


def _run_node(script: str, payload: Any) -> Any:
    """Evaluate ``script`` (it sees ``Lib`` and ``input``) under node and return the JSON it prints."""
    program = (
        "const Lib = require(process.argv[1]);\n"
        "const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
        f"const out = (() => {{ {script} }})();\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    result = subprocess.run(
        [NODE, "-e", program, str(BOTLIB_JS)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _core(identity: str = "", voice: str = "", mind: str = "", extras: tuple[tuple[str, str], ...] = ()) -> CoreFiles:
    return CoreFiles(identity=identity, voice=voice, mind=mind, extras=extras)


def _model(core: CoreFiles) -> dict[str, Any]:
    return {"identity": core.identity, "voice": core.voice, "mind": core.mind, "extras": [{"title": t, "text": x} for t, x in core.extras]}


# ------------------------------------------------------------------------------------------------ limits
def test_the_limits_the_page_assumes_are_the_backends() -> None:
    """Every number the page counts against until the server sends its own is the server's number."""
    page = _run_node("return Lib.normalizeLimits(null);", None)
    server = limits_payload()
    for key, value in server.items():
        assert page[key] == value, key
    assert set(page) == set(server), "the page knows exactly the limits the server sends (a new limit has to be mirrored)"
    # what the server sends is taken over as it is (and a broken value falls back to the default)
    assert _run_node("return Lib.normalizeLimits(input);", server) == server
    broken = _run_node("return Lib.normalizeLimits(input);", {"arch_notes_chars": "many", "core_limits": {"voice": -4}, "playbook_max": 0})
    assert broken["arch_notes_chars"] == server["arch_notes_chars"] and broken["core_limits"] == server["core_limits"] and broken["playbook_max"] == server["playbook_max"]


# ------------------------------------------------------------------------------------------------ L0
LONG = lambda n: "字" * n  # noqa: E731  (n characters)

CORES: list[CoreFiles] = [
    _core(),
    _core("身份", "说话", "分析"),
    _core(LONG(300), LONG(600), LONG(400)),
    _core(LONG(301), LONG(601), LONG(401)),
    _core("😀" * 300, "x", "y"),  # an emoji is one character
    _core("😀" * 301, "x", "y"),
    _core("第一行\n# 不是标题吗\n第三行", "说话", "分析"),  # a first-level heading line inside a section
    _core("## 二级标题可以", "  # 带缩进的一级标题  ", "分析"),
    _core("身份", "说话", "分析", (("口头禅", "好的"),)),
    _core("身份", "说话", "分析", (("a", "1"), ("b", "2"), ("c", "3"), ("d", "4"))),
    _core("身份", "说话", "分析", (("", "没有标题"),)),
    _core("身份", "说话", "分析", (("标" * 21, "标题太长"),)),
    _core("身份", "说话", "分析", (("AB", "x"), ("ab", "y"))),
    _core("身份", "说话", "分析", (("空", "  "),)),
    _core("身份", "说话", "分析", (("长", LONG(301)),)),
    _core("身份", "说话", "分析", (("长", LONG(300)),)),
    _core("身份", "说话", "分析", (("井", "# 标题行"),)),
    _core("身份", "说话", "分析", (("两\n行", "x"),)),
    _core("", "说话", "", (("口头禅", "好的"),)),
]


@pytest.mark.parametrize("require_complete", [False, True])
def test_every_fault_of_an_l0_is_worded_and_ordered_like_the_servers(require_complete: bool) -> None:
    models = [_model(c) for c in CORES]
    page = _run_node(
        "return input.models.map((m) => Lib.coreProblems(m, Lib.DEFAULT_LIMITS, { requireComplete: input.complete }));",
        {"models": models, "complete": require_complete},
    )
    for core, got in zip(CORES, page):
        assert got == botfiles.validate_core(core, require_complete=require_complete), core


def test_the_counters_of_the_editor_count_what_the_server_counts() -> None:
    models = [_model(c) for c in CORES]
    page = _run_node("return input.map((m) => Lib.coreStats(m, Lib.DEFAULT_LIMITS));", models)
    for core, stats in zip(CORES, page):
        for key in ("identity", "voice", "mind"):
            assert stats["sections"][key]["n"] == len(getattr(core, key).strip()), (core, key)
        assert [e["n"] for e in stats["extras"]] == [len(text.strip()) for _t, text in core.extras]
        assert stats["missing"] == list(core.missing)
        assert stats["problems"] == botfiles.validate_core(core, require_complete=False)


@pytest.mark.parametrize("name", [None, "", "Alice", "A # B", " Bob ", "“引号”", "'x'", '"y"', "名字 # 注释"])
def test_the_merged_l0_file_is_rendered_like_the_servers(name: str | None) -> None:
    models = [_model(c) for c in CORES]
    page = _run_node("return input.models.map((m) => Lib.renderCoreMarkdown(m, input.name));", {"models": models, "name": name})
    for core, text in zip(CORES, page):
        assert text == botfiles.render_core_markdown(core, name), (core, name)


GOOD = botfiles.render_core_markdown(_core("身份一", "说话一\n第二行", "分析一", (("口头禅", "好的"),)), "Alice")

FILES: list[str] = [
    GOOD,
    GOOD.replace("\n", "\r\n"),
    "\ufeff" + GOOD,
    "\n\n" + GOOD,
    GOOD.replace("format: arena-core", "format: other"),
    GOOD.replace("format: arena-core\n", ""),
    GOOD.replace("version: 1", "version: 2"),
    GOOD.replace("version: 1\n", ""),
    GOOD.replace("format:", "ｆｏｒｍａｔ："),  # full-width key and colon
    GOOD.replace("name: Alice", "name: Alice # a comment"),
    GOOD.replace("name: Alice", 'name: "Alice B"'),
    GOOD.replace("---\n# 身份", "---\n前言\n# 身份"),
    GOOD.replace("---\n# 身份", "---\n前言\n更多前言\n# 身份"),
    GOOD.replace("# 分析方式\n分析一\n\n", ""),
    GOOD.replace("# 说话方式", "# 讲话方式"),
    GOOD + "\n# 身份\n又一份\n",
    GOOD + "\n# 附加：口头禅\n重复\n",
    GOOD + "\n# 附加：\n没有标题\n",
    GOOD.replace("身份一", ""),
    "没有文件头\n# 身份\nx",
    "---\nformat: arena-core\nversion: 1\n# 身份\nx",  # the header is never closed
    GOOD + "\n## 小标题\n属于上一个分区\n",
    GOOD.replace("分析一", "字" * 401),
    GOOD + "".join(f"\n# 附加：额外{i}\n内容{i}\n" for i in range(4)),
    GOOD.replace("# 身份", "# 身 份"),  # a space inside a known heading is ignored
    GOOD + "x" * 20_001,
    "---\nformat: arena-core\nversion: 1\n---\n",
    "",
    "身份\x00",
]


def test_the_merged_l0_file_is_read_like_the_servers() -> None:
    page = _run_node("return input.map((t) => Lib.parseCoreMarkdown(t));", FILES)
    for text, got in zip(FILES, page):
        try:
            parsed = botfiles.parse_core_markdown(text, require_complete=True, lenient_limits=True)
        except botfiles.BotFileError as exc:
            assert got["ok"] is False and got["problems"] == exc.problems, (text[:60], got, exc.problems)
        else:
            assert got["ok"] is True and got["problems"] == [], (text[:60], got)
            assert got["core"] == _model(parsed.core) | {"extras": [{"title": t, "text": x} for t, x in parsed.core.extras]}
            assert got["name"] == parsed.name


def test_a_file_that_is_rendered_and_read_again_is_the_same_l0() -> None:
    valid = [c for c in CORES if not botfiles.validate_core(c, require_complete=True)]  # (a text with a heading line in it cannot make the trip)
    assert len(valid) >= 5
    page = _run_node("return input.map((m) => Lib.parseCoreMarkdown(Lib.renderCoreMarkdown(m, 'X')));", [_model(c) for c in valid])
    for core, got in zip(valid, page):
        assert got["ok"] is True and got["core"] == _model(botfiles.clean_core(core)), core


# ------------------------------------------------------------------------------------------------ the memory parts
PLAYBOOKS = [
    "",
    "## 假设\n- [把握:中|支持:3|反例:1|来源:初始化|#发言|#投票] 先看结论和行动对不对得上。\n- [把握:低|支持:0|反例:0] 另一条。\n",
    "- 没有方括号的一条\n* 另一种符号\n1. 数字编号\n",
    "[把握:高|支持:2] 只有方括号\n",
    "［把握：中｜支持：3｜来源：复盘 11-20｜＃警上］ 全角符号\n",
    "## 别的小节\n- [把握:中] 不算\n## 假设\n- [把握:中] 算\n",
    "没有符号的话\n另一行没有符号的话\n",
    "- [把握:very high|支持:x|反例:2个|来源:a|b[c]|#带 空格|#重复|#重复] 奇怪的字段\n",
    "- [中] 只有一个字\n- [medium] 英文\n",
    "- [把握:中]\n- [把握:中] 有内容\n",
    "- " + "很长的一条" * 20 + "\n",
    "\n".join(f"- 第{i}条" for i in range(21)),
    "- [来源:" + "来" * 50 + "|#" + "标" * 30 + "] 来源和标签会被截短\n",
]

PEOPLE = [
    "",
    "- Bob：发言短，站边快。\n- Carol: 逻辑细。\n",
    "Bob：没有符号\n- Dave：（暂无）\n- Eve：\n",
    "- bob：第一次\n- Bob：第二次\n",
    "# 小标题\n- 没有冒号的行\n- Frank：有\n",
    "没有冒号的一行\n另一行\n",
    "- " + "名" * 21 + "：名字太长\n",
    "- <b>：尖括号\n- 'x'：引号\n",
    "- Grace：" + "评" * 81 + "\n",
    "\n".join(f"- 人{i}：印象{i}" for i in range(31)),
    "- Heidi：第一行\n  第二行不是新条目\n",
]


def test_the_playbook_is_read_like_the_servers() -> None:
    page = _run_node("return input.map((t) => Lib.parsePlaybook(t));", PLAYBOOKS)
    for text, got in zip(PLAYBOOKS, page):
        entries, ignored = botfiles.parse_playbook_ex(text)
        assert got["ignored"] == ignored, text[:60]
        assert got["entries"] == [e.to_dict() for e in entries], text[:60]


def test_the_impressions_are_read_like_the_servers() -> None:
    page = _run_node("return input.map((t) => Lib.parsePeople(t));", PEOPLE)
    for text, got in zip(PEOPLE, page):
        people, ignored = botfiles.parse_people_ex(text)
        assert got["ignored"] == ignored, text[:60]
        assert got["people"] == people, text[:60]


FREE = ["", "  一些字  ", "字" * 400, "字" * 401, "字" * 800, "字" * 801, "😀" * 800, "😀" * 801, "含\x00空字符", "多行\r\n文字\r\n"]


@pytest.mark.parametrize("part", list(botfiles.PART_LABELS))
def test_what_makes_a_text_refused_for_a_part_is_what_the_page_says(part: str) -> None:
    texts = FREE + (PLAYBOOKS if part == "playbook" else PEOPLE if part == "people" else [])
    page = _run_node("return input.texts.map((t) => Lib.partProblems(input.part, t, Lib.DEFAULT_LIMITS));", {"part": part, "texts": texts})
    for text, got in zip(texts, page):
        try:
            botfiles.normalize_part(part, text)
        except botfiles.BotFileError as exc:
            assert got == exc.problems, (part, text[:60])
        else:
            assert got == [], (part, text[:60], got)


def test_an_unknown_part_is_refused_in_the_servers_words() -> None:
    got = _run_node("return Lib.partProblems('rules', 'x', Lib.DEFAULT_LIMITS);", None)
    with pytest.raises(botfiles.BotFileError) as exc:
        botfiles.normalize_part("rules", "x")
    assert got == exc.value.problems


@pytest.mark.parametrize("part", ["arch_notes", "digest", "questions", "playbook", "people"])
def test_the_counters_of_a_part_count_what_the_server_counts(part: str) -> None:
    texts = [t for t in FREE + PLAYBOOKS + PEOPLE if "\x00" not in t]  # (a NUL is refused, not counted)
    page = _run_node("return input.texts.map((t) => Lib.countPart(input.part, t, Lib.DEFAULT_LIMITS));", {"part": part, "texts": texts})
    for text, got in zip(texts, page):
        if part == "playbook":
            assert got["n"] == len(botfiles.parse_playbook_ex(text)[0]), text[:40]
        elif part == "people":
            assert got["n"] == len(botfiles.parse_people_ex(text)[0]), text[:40]
        else:
            assert got["n"] == len(botfiles.checked_text(text, "x").strip()), text[:40]


# ------------------------------------------------------------------------------------------------ names
NAMES = ["", "   ", "Alice", "x" * 20, "x" * 21, "😀" * 20, "😀" * 21, "a\nb", 'q"uote', "back\\slash", "<tag>", "co:lon", "co：lon", "tab\there", "`tick`", "it's"]


def test_a_new_name_is_refused_by_the_page_the_way_the_server_refuses_it() -> None:
    page = _run_node("return input.map((n) => Lib.botNameProblem(n, [], Lib.DEFAULT_LIMITS));", NAMES)
    for name, got in zip(NAMES, page):
        try:
            clean_name(name)
        except RosterError as exc:
            assert got == str(exc), repr(name)
        else:
            assert got == "", repr(name)


def test_a_duplicate_name_is_refused_before_the_request_is_sent() -> None:
    got = _run_node("return [Lib.botNameProblem('bob', ['Alice', 'Bob'], Lib.DEFAULT_LIMITS), Lib.botNameProblem('Bobby', ['Alice', 'Bob'], Lib.DEFAULT_LIMITS)];", None)
    assert "已被其他玩家使用" in got[0] and "名字不能重复" in got[0] and got[1] == ""
