"""The output gate of a bot's speech (v0.14 H15; docs/v0.12-contracts.md §13.4).

A bot's ``speak`` is public talk.  Text that reads like a *report* - lines of a list, a heading, bold, or a line that starts with
who is speaking - is sent back to the bot with the receipt code ``FORMAT`` and the reasons; nothing is written to the channel,
the floor is kept, and the bot decides for itself how to say it again.  This module only *looks*: it finds the problems of a text
and never changes a character of it (H18: nobody rewrites what a bot says).  ``app.agents.mind`` calls it once per speaking turn.

The checks (the contract says the same in words, ``prompts/contract.md``):

* ``list``    two or more consecutive lines that start with a number (``1.`` ``1)`` ``1、`` ``(1)`` ``①`` ``一、``) or a bullet
  (``-`` ``*`` ``+`` ``•`` ...).  A voice that really talks in enumerations says so with a line ``允许口头列举`` in its
  ``voice.md``: this check is then skipped (the others stay);
* ``heading`` a markdown heading line (``# 标题``), bold (``**x**``) or a code fence;
* ``prefix``  the text starts with the bot's own seat id or name followed by a colon (``<id>：...``, ``<name>：...``,
  ``<id> <name>：...``, ``<name>（<id>）：...``, ``<name> 说：...``) or in brackets (``【<id>】...``, ``[<id> <name>] ...``).

Game-neutral: the id and the name come from the caller.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

#: A line of ``voice.md`` that is exactly this turns the ``list`` check off for that bot.
ORAL_LIST_LINE = "允许口头列举"

RULES = ("prefix", "heading", "list")

_BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|[•·●○▪■◆▶►‣]\s*|[–—]\s+)")
_NUMBER_RE = re.compile(
    r"^\s*(?:"
    r"\d{1,2}\s*[.)）、．](?!\d)"  # 1.  1)  1）  1、        (not 2.5)
    r"|[（(]\s*\d{1,2}\s*[）)]"  # (1)  （1）
    r"|[①-⑳]"
    r"|[一二三四五六七八九十]{1,3}\s*[、.．)）]"  # 一、  二）
    r"|[（(]\s*[一二三四五六七八九十]{1,3}\s*[）)]"  # （一）
    r")"
)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}[ \t]+\S")
_BOLD_RE = re.compile(r"\*\*[^*\n]+\*\*")
_FENCE_RE = re.compile(r"^\s*```")


@dataclass(frozen=True)
class FormatProblem:
    """One thing that makes a speech read like a report: ``rule`` is one of :data:`RULES`, ``detail`` says what was found (Chinese)."""

    rule: str
    detail: str


def allows_oral_lists(voice: str) -> bool:
    """Whether the bot's ``voice.md`` has the line ``允许口头列举`` (a line of its own)."""
    return any(line.strip() == ORAL_LIST_LINE for line in (voice or "").splitlines())


def _is_list_line(line: str) -> bool:
    return bool(_BULLET_RE.match(line) or _NUMBER_RE.match(line))


def _list_problem(text: str) -> FormatProblem | None:
    lines = [(number, line) for number, line in enumerate(text.splitlines(), start=1) if line.strip()]
    run: list[int] = []
    for number, line in lines:
        if _is_list_line(line):
            run.append(number)
            if len(run) >= 2:
                return FormatProblem("list", f"第 {run[0]} 行起连着几行以编号或项目符号开头（分条列项）")
        else:
            run = []
    return None


def _heading_problem(text: str) -> FormatProblem | None:
    for number, line in enumerate(text.splitlines(), start=1):
        if _HEADING_RE.match(line):
            return FormatProblem("heading", f"第 {number} 行是标题（以 # 开头）")
        if _FENCE_RE.match(line):
            return FormatProblem("heading", f"第 {number} 行是代码块标记")
    if _BOLD_RE.search(text):
        return FormatProblem("heading", "用了加粗（** 包着的字）")
    return None


def _prefix_regex(labels: Iterable[str]) -> re.Pattern[str] | None:
    parts = sorted({label.strip() for label in labels if label and label.strip()}, key=len, reverse=True)
    if not parts:
        return None
    who = "(?:" + "|".join(re.escape(p) for p in parts) + ")"
    pair = rf"{who}(?:\s*[/／、]?\s*{who})?"  # "<id>", "<name>", "<id> <name>", "<id>/<name>"
    bracketed = rf"[\[【(（]\s*{pair}\s*[\]】)）]"  # "【<id>】", "[<id> <name>]"
    colon = rf"[「『“\"']?\s*{pair}\s*(?:[（(][^）)\n]{{0,24}}[）)])?\s*[」』”\"']?\s*(?:说|說)?\s*[:：]"  # "<name>（<id>）：", "<id> 说："
    return re.compile(rf"^\s*(?:{bracketed}|{colon})")


def _prefix_problem(text: str, labels: Iterable[str]) -> FormatProblem | None:
    pattern = _prefix_regex(labels)
    match = pattern.match(text) if pattern else None
    if match is None:
        return None
    shown = match.group(0).strip()
    return FormatProblem("prefix", f"开头写了自己的编号或名字（“{shown}”）")


def check_speech(
    text: str,
    *,
    ids: Iterable[str] = (),
    names: Iterable[str] = (),
    allow_lists: bool = False,
) -> list[FormatProblem]:
    """Every problem of ``text`` as a speech, in the order of :data:`RULES` (an empty list = it reads like talk).

    ``ids`` / ``names`` are what the bot is called (its seat id, its name); ``allow_lists`` skips the ``list`` check
    (:func:`allows_oral_lists`).  The text is only read."""
    body = text if isinstance(text, str) else ""
    if not body.strip():
        return []
    found = [
        _prefix_problem(body, [*ids, *names]),
        _heading_problem(body),
        None if allow_lists else _list_problem(body),
    ]
    return [problem for problem in found if problem is not None]


def format_message(problems: list[FormatProblem]) -> str:
    """The sentence of the ``FORMAT`` receipt: what is wrong, and that nothing was sent and the floor is still the bot's."""
    reasons = "；".join(problem.detail for problem in problems)
    return (
        f"这是当众说的话，读起来却像报告：{reasons}。这一条没有发出，发言权还在。"
        "请改成连贯的口语，像当着所有人的面开口那样把想说的一次说完，再发一次。"
    )


__all__ = [
    "ORAL_LIST_LINE",
    "RULES",
    "FormatProblem",
    "allows_oral_lists",
    "check_speech",
    "format_message",
]
