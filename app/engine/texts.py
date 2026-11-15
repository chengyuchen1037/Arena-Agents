"""Game-neutral engine texts that a scenario may override.

The engine itself must stay game-agnostic, so every phrase that carries game
flavour (what a skipped turn is called, what a truncated speech should conclude
with, ...) is looked up here.  A scenario can override any key through a
``texts:`` mapping in its front matter; the defaults below are deliberately
neutral.
"""

from __future__ import annotations

from typing import Any

DEFAULT_TEXTS: dict[str, str] = {
    # Recorded when a participant ends a speaking turn without saying anything.
    "pass_speech": "（本轮未发言）",
    # Recorded for an empty concurrent-speech turn.
    "concurrent_abstain": "（弃权/不表态）",
    # Follow-up asking a participant to finish a speech cut off by the token limit.
    "truncated_followup": (
        "你的发言最后一句因长度上限被截断了。请紧接上面的末尾残句，用 1~2 句话（不超过 80 字）"
        "把最后一句补完并给出明确的结论（只输出补完的收尾文字，不要重复前文）："
    ),
    # Nudge sent to a participant that answered a planning turn without submitting.
    "plan_nudge": (
        "你可以调用 `read_document` 查阅参考文档、调用 `write_note` 把多套预案记录到私人笔记本中。"
        "规划完成后，请务必调用 **`submit_strategic_plan(plan_summary, current_focus)`** "
        "提交你本阶段的核心预案与行动焦点！"
    ),
    # Fallbacks when the planning budget runs out.
    "plan_fallback_summary": "依据自身目标与当前局势既定规划行动",
    "plan_fallback_focus": "观察场上局势与信息变化",
}


def scenario_text(session: Any, key: str) -> str:
    """Return the scenario's override for *key*, or the neutral default."""
    definition = {}
    snapshot = getattr(session, "scenario_snapshot", None)
    if isinstance(snapshot, dict):
        definition = snapshot.get("definition") or {}
    overrides = definition.get("texts") if isinstance(definition, dict) else None
    if isinstance(overrides, dict):
        value = overrides.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return DEFAULT_TEXTS[key]


def participant_error_policy(session: Any) -> str:
    """``degrade`` (default) or ``abort`` — what a failed batch participant does."""
    snapshot = getattr(session, "scenario_snapshot", None)
    definition = snapshot.get("definition") if isinstance(snapshot, dict) else None
    limits = definition.get("limits") if isinstance(definition, dict) else None
    policy = limits.get("on_participant_error") if isinstance(limits, dict) else None
    return "abort" if policy == "abort" else "degrade"
