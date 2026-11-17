"""Prompts for the two "one-shot" model calls that happen outside a game's runtime loop (v0.12.1):

* **review** (E21): after a game a player writes a short first-person review that is appended to his memory;
* **refine** (E16): the moderator's model expands a few notes into a complete persona / memory text.

Both are plain prompt builders (no world access besides reading what the player saw); they know nothing
about any particular game.
"""

from __future__ import annotations

from typing import Any

from app.agents.context import Namer, render_history

MAX_REVIEW_HISTORY_CHARS = 24_000
REVIEW_MAX_CHARS = 1200
REFINE_MAX_CHARS = 1500

REVIEW_SYSTEM = (
    "你是 {id} {name}，刚刚结束了一局多人游戏。下面是你在这一局里的身份、结果，以及你当时能看到的全部记录。"
    "请站在你自己的角度，用第一人称写一份给“下一局的自己”看的复盘笔记，要求：\n"
    "1. 先用几句话总结这一局的走向和你的得失（做对了什么、做错了什么）；\n"
    "2. 然后对每一位其他玩家各写一句话印象（他的风格、可信度、你下次该怎么对待他）；\n"
    "3. 最后写一两条下次可以改进的打法。\n"
    "只依据你看得到的记录，不要编造你不可能知道的信息；不要重复原文；全文不超过 {limit} 字，直接输出笔记正文，不要加标题或客套话。"
    "{persona_line}"
)

REFINE_SYSTEM = {
    "persona": (
        "你是一名写作助手。用户会给你几句关于一名游戏玩家“性格设定”的要点，请把它们扩写成一份完整、具体、可直接放进"
        "提示词里的性格设定：说话风格、思维方式、做决定时的偏好、在压力下的反应、口头禅（如果合适）。"
        "保持用户给的要点不变，只补充细节，不要与要点矛盾；不要写游戏规则，不要提到“AI”或“模型”；"
        "用第二人称（“你……”）；全文不超过 {limit} 字；直接输出设定正文，不要加解释或标题。"
    ),
    "memory": (
        "你是一名写作助手。用户会给你几句关于一名游戏玩家“记忆与经验”的要点（例如他对其他玩家的印象、他过去的战绩和教训），"
        "请把它们整理成一份完整、有条理的记忆：分成“对其他玩家的印象”和“自己的经验与教训”两部分，用简短的条目。"
        "保持用户给的事实不变，不要编造用户没有提到的具体战绩或事件；用第二人称（“你……”）；全文不超过 {limit} 字；"
        "直接输出记忆正文，不要加解释。"
    ),
}


def build_review_messages(
    world: Any,
    player_id: str,
    *,
    outcome_text: str,
    role_text: str,
    persona: str = "",
) -> list[dict[str, str]]:
    """The conversation of one player's review call.  ``outcome_text`` / ``role_text`` are ready-made
    sentences ("你的阵营获胜" / "你的身份是 …"), the history is exactly what the player could see."""
    board = world.board(player_id)
    channels = {str(c.get("id")): str(c.get("name")) for c in (board.get("channels") or [])}
    namer = Namer(world.display_name, channels)
    entries = world.entries_visible(player_id)
    history, omitted = render_history(entries, namer, viewer=player_id, max_chars=MAX_REVIEW_HISTORY_CHARS)
    me = board.get("me") or {}
    persona_line = f"\n你的性格：{persona.strip()}" if persona and persona.strip() else ""
    system = REVIEW_SYSTEM.format(
        id=player_id, name=me.get("name") or player_id, limit=REVIEW_MAX_CHARS, persona_line=persona_line
    )
    others = [
        f"{p.get('id')} {p.get('name')}" for p in (board.get("players") or []) if str(p.get("id")) != player_id
    ]
    parts = [
        f"【结果】{outcome_text}",
        f"【你的身份】{role_text}",
        f"【其他玩家】{'、'.join(others) or '（无）'}",
        "【你能看到的记录】" + (f"（更早的 {omitted} 条因篇幅已省略）" if omitted else ""),
        history or "（没有记录）",
        "现在请写复盘笔记。",
    ]
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(parts)}]


def build_refine_messages(kind: str, text: str, *, name: str = "") -> list[dict[str, str]]:
    """The conversation of the "complete this for me" call (``kind``: ``persona`` | ``memory``)."""
    if kind not in REFINE_SYSTEM:
        raise ValueError("kind 必须是 persona 或 memory")
    who = f"（玩家名：{name}）" if name else ""
    return [
        {"role": "system", "content": REFINE_SYSTEM[kind].format(limit=REFINE_MAX_CHARS)},
        {"role": "user", "content": f"{who}要点：\n{text.strip()}"},
    ]


def clean_generated_text(text: str, *, limit: int) -> str:
    """The model's answer, tidied for saving: no surrounding code fences, no leading chatter lines, capped."""
    value = (text or "").strip()
    if value.startswith("```"):
        value = value.strip("`").strip()
        if value.lower().startswith(("markdown", "md", "text")):
            value = value.split("\n", 1)[1] if "\n" in value else ""
    value = value.strip()
    return value[:limit].rstrip()
