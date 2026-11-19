"""Prompts for the "one-shot" model calls that happen outside a game's runtime loop (v0.12.1; v0.13.1 G39–G43):

* **review** (E21, G39): after a game a player *revises his impression* of every other seat (and his own lessons)
  with his own model.  The seats are fixed (the same id in every game) while the identities are dealt anew, so an
  impression describes a style, never a role.  The answer is a JSON object; the roster store parses it
  (``app.storage.roster.parse_review_output``) and merges it into the memory;
* **refine** (E16, G42, G43): the moderator's model turns a few notes into a persona in a fixed format (positioning,
  speaking style, thinking, decisions, under pressure, catch-phrase), or into a memory (impressions and lessons).

These are plain prompt builders (no world access besides reading what the player saw); they know nothing about any
particular game.  The ids of the seats come from the world through the caller — this module is part of the
game-neutral engine packages and never spells one out.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.agents.context import Namer, render_history

MAX_REVIEW_HISTORY_CHARS = 24_000
#: A generous cap on what a model may answer with.  The answer is parsed and then every text in it is cut to its own limit
#: (the limits of the memory and of the persona live in ``app.storage.roster``; the caller hands them over).
RAW_OUTPUT_CHARS = 8_000
#: The limits the prompts quote when the caller does not hand over its own (``roster.LIMITS`` is the source of truth).
DEFAULT_LIMITS: dict[str, int] = {
    "impression_chars": 80,
    "experience_items": 3,
    "experience_chars": 300,
    "persona_chars": 500,
    "persona_item_chars": 40,
}

REVIEW_SYSTEM = (
    "你是 {id} {name}，刚刚结束了一局多人游戏。下面是你在这一局里的身份和结果、你能看到的全部记录，"
    "以及你赛前对其他玩家的印象和你自己的经验。\n"
    "请站在你自己的角度，根据这一局的表现更新你对每一位其他玩家的印象，并修订你自己的经验。要求：\n"
    "1. 印象是对“这个座位上的玩家”的长期判断：他说话和投票的风格、可信度、习惯，以及你下次该怎么对待他。"
    "座位号是固定的，名字只是标签；但每一局的身份都会重新分配，所以不要写“他是某某身份”“他属于哪个阵营”这类身份或阵营结论，"
    "它们下一局就不适用了。可以写条件化的习惯，例如“拿到某类身份时倾向于……”。\n"
    "2. 这一局没有给你新信息的玩家，印象保持原样（原文照抄，或者省略这一项）；有新信息的，把旧印象和这一局的观察合并成一条，"
    "不要只写这一局。\n"
    "3. 每条印象不超过 {impression_chars} 字。经验是你自己的打法心得（做对了什么、下次改什么），最多 {experience_items} 条，"
    "合计不超过 {experience_chars} 字；不要在经验里评价某个具体的玩家（那属于印象）。\n"
    "4. 只依据你看得到的记录，不要编造你不可能知道的信息；不管这一局是输是赢，印象都要客观。\n"
    "{legacy_rule}"
    "输出格式：只输出一个 JSON 对象，不要任何解释、标题、客套话或代码块标记：\n"
    "{example}\n"
    "impressions 的键是下面【其他玩家】里的座位号；experience 是字符串数组。"
    "{persona_line}"
)
LEGACY_RULE = (
    "5. 下面的【旧记忆（待整理）】是旧版本留下的零散笔记：把其中仍然有用的内容整理进印象和经验里，整理之后它会被删除。\n"
)

REFINE_PERSONA_SYSTEM = (
    "你是一名写作助手。用户会给你几句关于一名游戏玩家“性格”的要点，请把它们整理成一份固定格式的“性格与行为指引”，"
    "它会被直接放进这名玩家的提示词里。格式如下（每个标题单独成行，条目前加“- ”，没有内容的部分可以省略）：\n"
    "【定位】一句话概括这名玩家\n"
    "【说话风格】\n- …（最多 3 条）\n"
    "【思考方式】\n- …（最多 3 条）\n"
    "【决策偏好】\n- …（最多 3 条）\n"
    "【压力下】\n- …（最多 2 条）\n"
    "【口头禅】…（可以没有）\n"
    "要求：保持用户给的要点不变，只补充细节，不要与要点矛盾；用第二人称（“你……”）；"
    "每一条不超过 {item_chars} 字，全文不超过 {limit} 字；不要写游戏规则，不要提到“AI”或“模型”；"
    "直接输出指引正文，不要加解释、前言或代码块标记。"
)
REFINE_MEMORY_SYSTEM = (
    "你是一名写作助手。用户会给你一些关于一名游戏玩家“记忆”的要点（他对其他玩家的印象，或他自己的打法经验）。"
    "请把它们整理进固定的结构，只输出一个 JSON 对象，不要任何解释、标题或代码块标记：\n"
    "{example}\n"
    "要求：\n"
    "1. impressions 的键是座位号（见【其他玩家】），值是对那个座位上玩家的印象：说话和投票的风格、可信度、习惯；"
    "每条不超过 {impression_chars} 字。要点里没有提到的座位不要写（会保留原有印象）；"
    "要点和【当前印象】说的是同一个座位时，合并成一条。\n"
    "2. experience 是这名玩家自己的打法经验，最多 {experience_items} 条，合计不超过 {experience_chars} 字；"
    "要点里没有提到经验就不要写这一项（会保留原有经验）。\n"
    "3. 座位号是固定的，但每一局的身份都会重新分配：不要写“他是某某身份”“他属于哪个阵营”这类身份或阵营结论，写风格、可信度和习惯。\n"
    "4. 保持用户给的事实不变，不要编造用户没有提到的具体战绩或事件；用简短的陈述句。"
)
#: Kept for callers that look the prompts up by kind (``persona`` | ``memory``).
REFINE_SYSTEM = {"persona": REFINE_PERSONA_SYSTEM, "memory": REFINE_MEMORY_SYSTEM}


def _limits(limits: Mapping[str, int] | None) -> dict[str, int]:
    return {**DEFAULT_LIMITS, **(limits or {})}


def answer_example(first_id: str | None) -> str:
    """The JSON a review (or "complete the memory") answer should look like, with the seat id of the first other player."""
    return json.dumps({"impressions": {first_id or "<座位号>": "…"}, "experience": ["…"]}, ensure_ascii=False)


def _impression_lines(impressions: Mapping[str, str], labels: Mapping[str, str]) -> str:
    return "\n".join(f"- {labels.get(sid, sid)}：{text or '（暂无）'}" for sid, text in impressions.items())


def build_review_messages(
    world: Any,
    player_id: str,
    *,
    outcome_text: str,
    role_text: str,
    persona: str = "",
    impressions: Mapping[str, str] | None = None,
    experience: Sequence[str] = (),
    legacy: str = "",
    limits: Mapping[str, int] | None = None,
) -> list[dict[str, str]]:
    """The conversation of one player's review call.  ``outcome_text`` / ``role_text`` are ready-made sentences
    ("你的阵营获胜" / "你的身份是 …"), the history is exactly what the player could see.  ``impressions`` (another seat's
    id -> what he thinks of that seat now, ``""`` = nothing yet), ``experience`` and ``legacy`` are his memory as it stands:
    the review revises them instead of starting from scratch."""
    lim = _limits(limits)
    board = world.board(player_id)
    channels = {str(c.get("id")): str(c.get("name")) for c in (board.get("channels") or [])}
    namer = Namer(world.display_name, channels)
    entries = world.entries_visible(player_id)
    history, omitted = render_history(entries, namer, viewer=player_id, max_chars=MAX_REVIEW_HISTORY_CHARS)
    me = board.get("me") or {}
    others = [
        (str(p.get("id")), f"{p.get('id')} {p.get('name')}") for p in (board.get("players") or []) if str(p.get("id")) != player_id
    ]
    labels = dict(others)
    persona_line = f"\n你的性格与行为指引：\n{persona.strip()}" if persona and persona.strip() else ""
    legacy = (legacy or "").strip()
    system = REVIEW_SYSTEM.format(
        id=player_id,
        name=me.get("name") or player_id,
        impression_chars=lim["impression_chars"],
        experience_items=lim["experience_items"],
        experience_chars=lim["experience_chars"],
        legacy_rule=LEGACY_RULE if legacy else "",
        example=answer_example(others[0][0] if others else None),
        persona_line=persona_line,
    )
    current = {sid: (impressions or {}).get(sid, "") for sid, _label in others}
    lessons = "\n".join(f"- {item}" for item in experience if str(item).strip())
    parts = [
        f"【结果】{outcome_text}",
        f"【你的身份】{role_text}",
        f"【其他玩家】{'、'.join(label for _sid, label in others) or '（无）'}",
        "【你目前的印象】\n" + (_impression_lines(current, labels) or "（无）"),
        "【你目前的经验】\n" + (lessons or "（还没有）"),
    ]
    if legacy:
        parts.append("【旧记忆（待整理）】\n" + legacy)
    parts += [
        "【你能看到的记录】" + (f"（更早的 {omitted} 条因篇幅已省略）" if omitted else ""),
        history or "（没有记录）",
        "现在请按要求输出 JSON。",
    ]
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(parts)}]


def build_refine_messages(
    kind: str,
    text: str,
    *,
    name: str = "",
    others: Sequence[tuple[str, str]] = (),
    impressions: Mapping[str, str] | None = None,
    experience: Sequence[str] = (),
    limits: Mapping[str, int] | None = None,
) -> list[dict[str, str]]:
    """The conversation of the "complete this for me" call (``kind``: ``persona`` | ``memory``).

    ``persona``: the notes become the fixed-format persona.  ``memory``: the notes become impressions and lessons;
    ``others`` is ``[(seat id, name)]`` of the other seats and ``impressions`` / ``experience`` what the memory says now,
    so that notes about a seat are merged into what is already there."""
    if kind not in REFINE_SYSTEM:
        raise ValueError("kind 必须是 persona 或 memory")
    lim = _limits(limits)
    who = f"（玩家名：{name}）" if name else ""
    if kind == "persona":
        system = REFINE_PERSONA_SYSTEM.format(item_chars=lim["persona_item_chars"], limit=lim["persona_chars"])
        return [{"role": "system", "content": system}, {"role": "user", "content": f"{who}要点：\n{text.strip()}"}]
    labels = {sid: f"{sid} {label}" if label else sid for sid, label in others}
    system = REFINE_MEMORY_SYSTEM.format(
        example=answer_example(others[0][0] if others else None),
        impression_chars=lim["impression_chars"],
        experience_items=lim["experience_items"],
        experience_chars=lim["experience_chars"],
    )
    current = {sid: (impressions or {}).get(sid, "") for sid, _label in others}
    lessons = "\n".join(f"- {item}" for item in experience if str(item).strip())
    user = (
        f"{who}\n"
        f"【其他玩家】{'、'.join(labels.values()) or '（无）'}\n"
        f"【当前印象】\n{_impression_lines(current, labels) or '（无）'}\n"
        f"【当前经验】\n{lessons or '（还没有）'}\n"
        f"要点：\n{text.strip()}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user.lstrip()}]


def clean_generated_text(text: str, *, limit: int) -> str:
    """The model's answer, tidied for saving: no surrounding code fences, no leading chatter lines, capped."""
    value = (text or "").strip()
    if value.startswith("```"):
        value = value.strip("`").strip()
        if value.lower().startswith(("markdown", "md", "text", "json")):
            value = value.split("\n", 1)[1] if "\n" in value else ""
    value = value.strip()
    return value[:limit].rstrip()
