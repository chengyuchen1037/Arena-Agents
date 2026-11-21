"""The prompt of "complete this L0 for me" (E16 / H30, contract §13.7 ``POST /api/bots/{id}/core:refine``).

The moderator's model turns a few notes of the user into the merged L0 file (``<名字>.core.md``, plan §2.2).  The draft is only
shown to the user - it is saved when he confirms it (``PUT /api/bots/{id}/core``) - and L0 stays the user's: the model
writes who the bot is, how it talks and how it thinks, never rules or tactics.

Pure text: no I/O, no game vocabulary (like the rest of ``app.bots``).  The marker ``REFINE_MARKER`` is in the system prompt so
that a scripted demo model can recognise this call.
"""

from __future__ import annotations

import re

from app.bots.types import (
    CORE_EXTRA_CHARS,
    CORE_EXTRA_MAX,
    CORE_EXTRA_PREFIX,
    CORE_FORMAT,
    CORE_FORMAT_VERSION,
    CORE_HEADINGS,
    CORE_LIMITS,
)

REFINE_MARKER = "【整理底层设定】"
MAX_NOTES_CHARS = 6000  # what the user may hand over
MAX_ANSWER_CHARS = 12_000  # what is read of a model's answer

REFINE_SYSTEM = (
    REFINE_MARKER
    + "你是一名写作助手。用户会给你几句关于一名游戏玩家“底层设定”的要点，请把它们整理进下面这个固定格式的文件。"
    "如果给了【当前设定】，就在它的基础上修改：保留用户没有提到的内容，只改要点涉及的地方。"
    "这份设定会被直接放进玩家的提示词里，它只描述这个人是谁、怎么说话、怎么想，不写游戏规则，也不写具体打法。\n\n"
    "格式（头部和每个标题都逐字照抄，标题单独成行）：\n"
    "---\n"
    f"format: {CORE_FORMAT}\n"
    f"version: {CORE_FORMAT_VERSION}\n"
    "name: {name}\n"
    "---\n"
    f"# {CORE_HEADINGS['identity']}\n"
    "一两句话说清这个人是谁，再加一点背景（不超过 {identity} 字）。\n\n"
    f"# {CORE_HEADINGS['voice']}\n"
    "一段连贯的散文，写语气、节奏、口头习惯；然后给 3–5 句用「」标出的原话样本；最后单独一行写篇幅区间，"
    "例如“篇幅：60–150 字”（全部不超过 {voice} 字）。\n\n"
    f"# {CORE_HEADINGS['mind']}\n"
    "这个人怎么看问题、怎么对待自己看错的时候（不超过 {mind} 字）。\n\n"
    f"# {CORE_EXTRA_PREFIX}<标题>\n"
    "（可选，最多 {extra_max} 个，每个不超过 {extra_chars} 字；用户没有要求就不要写。）\n\n"
    "要求：\n"
    "1. 用第二人称（“你……”）；保持用户给的要点不变，只补充细节，不要与要点矛盾。\n"
    "2. 说话方式写成连贯的散文，不分点、不编号；原话样本互不重复；篇幅区间的两端都要写，最大不超过 280 字。\n"
    "3. 不写游戏规则和具体打法，不写某个具体游戏的专有词汇，不提“AI”或“模型”。\n"
    "4. 直接输出文件内容，不要加解释、前言或代码块标记。"
)


def build_core_refine_messages(name: str, notes: str, current_markdown: str = "") -> list[dict[str, str]]:
    """The conversation of one "complete it" call: ``name`` goes into the header (only a hint), ``notes`` are the user's points,
    ``current_markdown`` the bot's L0 as it is now (``""``: it has none yet)."""
    system = REFINE_SYSTEM.format(
        name=(name or "").strip() or "名字",
        identity=CORE_LIMITS["identity"],
        voice=CORE_LIMITS["voice"],
        mind=CORE_LIMITS["mind"],
        extra_max=CORE_EXTRA_MAX,
        extra_chars=CORE_EXTRA_CHARS,
    )
    who = f"（玩家名：{name.strip()}）\n" if name and name.strip() else ""
    current = (current_markdown or "").strip()
    user = f"{who}【当前设定】\n{current or '（还没有）'}\n【要点】\n{notes.strip()}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


_FENCE_RE = re.compile(r"^\s*```[A-Za-z]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


def clean_answer(text: str) -> str:
    """The model's answer as the file it was asked for: surrounding code fences and a sentence of chatter in front of the file are
    dropped (the file starts at its first ``---`` line, else at its first ``# 身份`` heading), capped at ``MAX_ANSWER_CHARS``."""
    value = (text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()
    fenced = _FENCE_RE.match(value)
    if fenced:
        value = fenced.group(1).strip()
    lines = value.split("\n")
    for index, line in enumerate(lines):
        if line.strip() == "---":
            value = "\n".join(lines[index:])
            break
    else:
        heading = f"# {CORE_HEADINGS['identity']}"
        for index, line in enumerate(lines):
            if line.strip().startswith(heading):
                value = "\n".join(lines[index:])
                break
    return value.strip()[:MAX_ANSWER_CHARS].rstrip()


__all__ = ["MAX_ANSWER_CHARS", "MAX_NOTES_CHARS", "REFINE_MARKER", "REFINE_SYSTEM", "build_core_refine_messages", "clean_answer"]
