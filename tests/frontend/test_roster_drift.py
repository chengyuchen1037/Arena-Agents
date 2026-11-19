"""v0.13.1: the memory / persona editor of the page says the same thing as the backend (static/js/roster.js vs app/storage/roster.py).

The editor checks what is typed *before* it sends anything: the counters, the "over the limit" hint, a Save that stays
off, the toast that names the part that is too long.  If these drift from the server's own rules the page shows a green
counter for a text that is then refused, or refuses a text the server would take.  The pure helpers of ``roster.js`` run
under node here, on a table of inputs, and are compared with the backend's real functions (contract section 12.5):

* the limits the page assumes until ``GET /api/roster`` tells it better;
* how characters are counted (code points: an emoji is one), how a persona is cleaned before it is counted;
* ``memory_body`` / ``memory_chars`` (what ``limits.memory_chars`` counts) and the words of every refusal;
* how an impression and a list of lessons are cleaned (one line, placeholders, bullet marks).

Skipped when node is missing, like ``tests/test_frontend_js.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.storage import roster as backend

ROOT = Path(__file__).resolve().parent.parent.parent
ROSTER_JS = ROOT / "static" / "js" / "roster.js"
CORE_JS = ROOT / "static" / "js" / "core.js"

_WINDOWS_NODE = Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"


def _find_node() -> str | None:
    env = os.environ.get("NODE_BIN")
    if env and Path(env).exists():
        return env
    return shutil.which("node") or (str(_WINDOWS_NODE) if _WINDOWS_NODE.exists() else None)


NODE = _find_node()
pytestmark = pytest.mark.skipif(NODE is None, reason="node executable not found")

NAMES = ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo"]
IDS = tuple(f"N{i}" for i in range(1, 13))


def _context(owner: str = "N3", names: dict[str, str] | None = None) -> backend.MemoryContext:
    return backend.MemoryContext(owner=owner, ids=IDS, names=dict(zip(IDS, NAMES)) if names is None else names)


def _others(ctx: backend.MemoryContext) -> list[dict[str, str]]:
    return [{"id": sid, "name": ctx.names.get(sid, "")} for sid in ctx.others]


def _run_node(script: str, payload: Any) -> Any:
    """Evaluate ``script`` (it sees ``Roster``, ``Core`` and ``input``) under node and return the JSON it prints."""
    program = (
        "const Roster = require(process.argv[1]);\n"
        "const Core = require(process.argv[2]);\n"
        "const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
        f"const out = (() => {{ {script} }})();\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    result = subprocess.run(
        [NODE, "-e", program, str(ROSTER_JS), str(CORE_JS)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ------------------------------------------------------------------------------------------------ limits


def test_the_limits_the_page_assumes_are_the_backends() -> None:
    assert _run_node("return Roster.DEFAULT_LIMITS;", None) == backend.LIMITS
    # a limit the server changes is the one the page takes (and nonsense falls back to the default)
    got = _run_node("return Roster.normalizeLimits(input);", {**backend.LIMITS, "impression_chars": 60, "memory_chars": "1200", "persona_chars": -1})
    assert got == {**backend.LIMITS, "impression_chars": 60, "memory_chars": 1200}


# ------------------------------------------------------------------------------------------------ names
#
# A NEW name is checked by the page before it is sent (core.js ``validateNickname``) and by the server (``clean_name``; HTTP 400 with its
# ``detail``).  The page checks LESS than the server - quotes, backslashes and angle brackets are left to the server's own answer - but
# never more: it must not block a name the server takes.  The newest rule is the colon (the name is the label in front of an impression,
# ``- N5 Eve：…``): ``:`` and ``：`` only, no normalizing, refused after 'empty' and 'too long' and before 'taken' - in the server's words.

COLON_MESSAGE = "名字不能包含冒号（: 或 ：）。"

NAMES_BOTH_TAKE = ["Alice", " Alice ", "小明小红小刚", "a" * 20, "😀" * 20, "Bob", "A;B", "A.B", "A-B", "A,B",
                   "A\ua789B", "A\ufe55B", "A\u2236B", "A\u02d0B", "A\u0589B"]  # (the look-alikes of a colon are no colons)
NAMES_COLON_ONLY = ["A:B", "A：B", "A\uff1aB", ":", "：", " 名字: ", "：名字", "a" * 19 + ":", "😀:😀", "😀：" * 10, "Bob:"]
NAMES_BOTH_REFUSE = ["", "   ", "a" * 21, "😀" * 21, ":" * 21, "a\x00b", "a\nb", "a\tb", "\x7f"]
NAMES_THE_SERVER_REFUSES_ALONE = ["a<b", "a>b", 'a"b', "a'b", "a`b", "a\\b"]  # (the page leaves these to the server's answer)


def _server_name(name: str) -> dict[str, Any]:
    try:
        return {"ok": True, "value": backend.clean_name(name)}
    except backend.RosterError as exc:
        return {"ok": False, "error": str(exc)}


def _page_names(names: list[str]) -> list[dict[str, Any]]:
    return _run_node("return input.map((n) => Core.validateNickname(n, []));", names)


def test_the_colon_message_is_the_servers_words_exactly() -> None:
    assert _run_node("return Core.NICKNAME_COLON_ERROR;", None) == COLON_MESSAGE
    assert _server_name("A:B") == {"ok": False, "error": COLON_MESSAGE}, "(the backend still says it: pinned here so that the page cannot follow a typo)"


def test_a_new_name_is_taken_or_refused_by_the_page_the_way_the_server_does() -> None:
    names = NAMES_BOTH_TAKE + NAMES_COLON_ONLY + NAMES_BOTH_REFUSE + NAMES_THE_SERVER_REFUSES_ALONE
    page = dict(zip(names, _page_names(names)))
    for name in NAMES_BOTH_TAKE:
        assert page[name]["ok"] and _server_name(name)["ok"], repr(name)
        assert page[name]["value"] == _server_name(name)["value"], f"the page and the server store the same name: {name!r}"
    for name in NAMES_COLON_ONLY:  # refused for the colon alone: exactly the server's words, from both
        assert page[name] == {"ok": False, "error": COLON_MESSAGE} and _server_name(name) == {"ok": False, "error": COLON_MESSAGE}, repr(name)
    for name in NAMES_BOTH_REFUSE:  # (the words differ by design: the page has no full stop; what counts is that both say no)
        assert not page[name]["ok"] and not _server_name(name)["ok"], repr(name)
    for name in NAMES_THE_SERVER_REFUSES_ALONE:  # the page may take them (it leaves them to the server); the server refuses them
        assert not _server_name(name)["ok"], repr(name)
    # the order: too long is said before the colon (by both), a control character before it as well
    assert "最多 20" in page[":" * 21]["error"] and "最多 20" in _server_name(":" * 21)["error"]
    assert "控制字符" in page["a\x00b"]["error"] and "控制字符" in _server_name("a\x00b")["error"]


def test_the_page_never_refuses_a_name_the_server_takes_and_means_the_same_by_a_colon() -> None:
    """Every character of a wide range, in the middle of a name: whatever the page refuses the server refuses too, and the page
    says 'colon' for exactly the characters the server says it for (``:`` and ``：``)."""
    codes = [*range(0x20, 0x3100), *range(0xFE30, 0xFE70), *range(0xFF00, 0xFFF0), 0x1F600, 0x1F9D1, 0x20BB7, 0x10FFFF]
    names = [f"A{chr(c)}B" for c in codes]
    page = _page_names(names)
    colons = []
    for name, got in zip(names, page):
        server = _server_name(name)
        if not got["ok"]:
            assert not server["ok"], f"the page refuses {name!r} (U+{ord(name[1]):04X}) but the server takes it"
        elif server["ok"]:
            assert got["value"] == server["value"], repr(name)
        said_colon = got.get("error") == COLON_MESSAGE
        assert said_colon == (not server["ok"] and server["error"] == COLON_MESSAGE), f"colon verdicts differ for U+{ord(name[1]):04X}"
        if said_colon:
            colons.append(ord(name[1]))
    assert colons == [0x3A, 0xFF1A], [hex(c) for c in colons]


# ------------------------------------------------------------------------------------------------ persona

PERSONAS = [
    "",
    "   \n \t\n",
    "冷静的逻辑派",
    "\r\n\r\n  line one\r\nline two \n\n",
    "ab\rcd",
    "字" * backend.PERSONA_CHARS,
    "字" * (backend.PERSONA_CHARS + 1),
    "\n" + "字" * backend.PERSONA_CHARS + "\n\n",  # the blank lines around it are not counted
    "😀" * 300,  # an emoji is one character here, as in Python's len()
    "😀" * 501,
    "a\u0301" * 250,  # a combining mark is a character of its own for both
]


def _backend_persona(text: str) -> tuple[int, str]:
    """(characters the server counts once the text is cleaned, the refusal it answers with or "")."""
    counted = len(backend.clean_text(text, "性格设定"))  # (the default limit is a sanity cap: these texts are far below it)
    try:
        backend.clean_text(text, "性格设定", limit=backend.PERSONA_CHARS)
    except backend.RosterError as exc:
        return counted, str(exc)
    return counted, ""


def test_a_persona_is_counted_and_refused_the_way_the_server_does() -> None:
    page = _run_node(
        "return input.map((t) => ({n: Roster.personaLength(t), problem: Roster.personaProblem(t, Roster.DEFAULT_LIMITS)}));",
        PERSONAS,
    )
    for text, got in zip(PERSONAS, page):
        n, problem = _backend_persona(text)
        assert got == {"n": n, "problem": problem}, repr(text[:40])
    # the one that is over says what the server says
    assert page[PERSONAS.index("字" * (backend.PERSONA_CHARS + 1))]["problem"] == "性格设定最多 500 个字符（现在 501 个），请精简。"


# ------------------------------------------------------------------------------------------------ impressions / lessons

IMPRESSIONS = [
    "",
    "发言短",
    "  发言短，\n站边快  ",
    "（暂无）",
    "(暂无)",
    "暂无",
    "暂无消息",
    "a\tb\u3000c",  # tab and an ideographic space are white space for both
    "x\r\ny",
    "😀 好人",
]


def test_an_impression_is_cleaned_the_way_the_server_cleans_it() -> None:
    page = _run_node("return input.map((t) => Roster.cleanImpression(t));", IMPRESSIONS)
    assert page == [backend.clean_impression(t) for t in IMPRESSIONS]


LESSONS = [
    "",
    "- 先听完再站边",
    "- a\n2. b\n（3）c\n\n• d\n（暂无）\n  e  \n* f",
    "一\r\n二\r三",
    "1) 第一\n2、第二\n3．第三",
    "（暂无）\n暂无",
    "· 点\n·· 双点\n-- 双横",
]


def test_lessons_are_parsed_the_way_the_server_parses_them() -> None:
    page = _run_node("return input.map((t) => Roster.parseExperience(t));", LESSONS)
    assert page == [backend.clean_experience(t) for t in LESSONS]


# ------------------------------------------------------------------------------------------------ the whole file

MEMORY_CASES: list[dict[str, Any]] = [
    {"owner": "N3", "impressions": {}, "experience": []},
    {"owner": "N3", "impressions": {"N5": "爱悍跳，票型常对不上"}, "experience": ["先听完再站边"]},
    {"owner": "N1", "impressions": {f"N{i}": "印" * 80 for i in range(2, 13)}, "experience": ["经" * 100] * 3},  # (full)
    {"owner": "N12", "impressions": {"N2": "😀" * 80, "N3": "ａｂｃ" * 20}, "experience": ["emoji 😀 lesson"]},
    # names change the size of the file: long ones push a full memory over the 1500 of the whole file
    {"owner": "N3", "impressions": {f"N{i}": "印" * 80 for i in range(1, 13) if i != 3}, "experience": ["经" * 100] * 3,
     "names": {f"N{i}": f"长名字玩家{i:02d}" + "名" * 13 for i in range(1, 13)}},
    {"owner": "N3", "impressions": {"N5": "x"}, "experience": [], "names": {}},  # (a seat without a name is labelled by its id)
]


def _backend_memory(case: dict[str, Any]) -> dict[str, Any]:
    ctx = _context(case["owner"], case.get("names"))
    parts = backend.MemoryParts(impressions=dict(case["impressions"]), experience=list(case["experience"]))
    try:
        backend.check_memory(parts, ctx)
        problem = ""
    except backend.RosterError as exc:
        problem = str(exc)
    return {"body": backend.memory_body(parts, ctx), "chars": backend.memory_chars(parts, ctx), "problem": problem}


def test_the_whole_file_is_counted_and_refused_the_way_the_server_does() -> None:
    payload = [
        {"others": _others(_context(c["owner"], c.get("names"))), "impressions": c["impressions"], "experience": c["experience"]}
        for c in MEMORY_CASES
    ]
    page = _run_node(
        "return input.map((c) => ({"
        " body: Roster.memoryBodyText(c.others, c.impressions, c.experience),"
        " chars: Roster.memoryChars(c.others, c.impressions, c.experience),"
        " problem: Roster.memoryProblem(c.others, c.impressions, c.experience, Roster.DEFAULT_LIMITS)}));",
        payload,
    )
    for case, got in zip(MEMORY_CASES, page):
        want = _backend_memory(case)
        assert got == want, f"owner {case['owner']}: {got['problem']!r} vs {want['problem']!r}"
    # the table really exercises the refusals (a drift test that only ever sees "" would prove nothing)
    problems = [_backend_memory(c)["problem"] for c in MEMORY_CASES]
    assert any("整份记忆" in p for p in problems), problems


SINGLE_LIMITS = [
    ({"N5": "x" * 81}, [], "N5 Eve 的印象最多 80 字（现在 81 字），请精简。"),
    ({"N2": "y" * 90, "N5": "x" * 81}, [], "N2 Bob 的印象最多 80 字（现在 90 字），请精简。"),  # the first one found, in seat order
    ({}, ["a", "b", "c", "d"], "经验最多 3 条（现在 4 条），请精简。"),
    ({}, ["x" * 150, "y" * 151], "经验合计最多 300 字（现在 301 字），请精简。"),
]


@pytest.mark.parametrize("impressions, experience, message", SINGLE_LIMITS)
def test_every_single_limit_is_worded_like_the_servers(impressions: dict[str, str], experience: list[str], message: str) -> None:
    ctx = _context("N3")
    with pytest.raises(backend.RosterError) as raised:
        backend.check_memory(backend.MemoryParts(impressions=impressions, experience=experience), ctx)
    assert str(raised.value) == message  # (the backend still says it - pinned here, so that the page cannot follow a typo)
    got = _run_node(
        "return Roster.memoryProblem(input.others, input.impressions, input.experience, Roster.DEFAULT_LIMITS);",
        {"others": _others(ctx), "impressions": impressions, "experience": experience},
    )
    assert got == message
