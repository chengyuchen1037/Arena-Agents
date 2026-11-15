"""Concrete participant turns and the orchestrator-level tasks built on them.

Each ``run_*`` function implements one moderator-facing capability (speak, concurrent
speech, yes/no inquiry, planning, voting, interruption).  They share the generic turn
machinery in :mod:`app.engine.participant_loop`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.engine.context import (
    build_participant_interrupt_messages,
    build_participant_messages,
    build_participant_planning_messages,
    build_participant_vote_messages,
    build_participant_yes_no_messages,
)
from app.engine.llm import LLMResponse
from app.engine.participant_loop import ParticipantTurn, run_batch, tool_json
from app.engine.sanitize import wrap_untrusted_block
from app.engine.state import GameSession
from app.engine.texts import scenario_text
from app.engine.toolargs import arg_str, arg_str_list
from app.engine.tools import get_participant_tools_schema

logger = logging.getLogger(__name__)

ABSTAIN_TOKENS = ("abstain", "pass", "none", "弃票", "弃权")
_SENTENCE_END = ("。", "！", "？", "!", "?", "”", "」", "）", ")", "*", "…")

_YES_TOKENS = {"yes", "y", "true", "1", "是"}
_NO_TOKENS = {"no", "n", "false", "0", "否"}


# =============================================================================
# yes / no parsing (no default answer, no substring accidents)
# =============================================================================

def parse_yes_no(raw: Any, yes_label: str = "", no_label: str = "") -> str | None:
    """Parse the ``decision`` argument; None means "not a valid yes/no"."""
    text = str(raw if raw is not None else "").strip().lower()
    if not text:
        return None
    yes_l, no_l = yes_label.strip().lower(), no_label.strip().lower()
    if yes_l == no_l:
        yes_l = no_l = ""
    if text in _YES_TOKENS or (yes_l and text == yes_l):
        return "yes"
    if text in _NO_TOKENS or (no_l and text == no_l):
        return "no"
    return None


def parse_yes_no_text(raw: str, yes_label: str = "", no_label: str = "") -> str | None:
    """Best-effort reading of a free-text answer; ambiguous text yields None.

    ``yes``/``no`` only count as whole words (so "yesterday", "know", "not" do not
    match), and an answer mentioning both sides is ambiguous rather than "no".
    """
    low = raw.strip().lower()
    if not low:
        return None
    yes_l, no_l = yes_label.strip().lower(), no_label.strip().lower()
    if yes_l == no_l:
        yes_l = no_l = ""
    yes_hit = bool(re.search(r"(?<![a-z])yes(?![a-z])", low)) or bool(yes_l and yes_l in low)
    no_hit = bool(re.search(r"(?<![a-z])no(?![a-z])", low)) or bool(no_l and no_l in low)
    if yes_hit == no_hit:
        return None
    return "yes" if yes_hit else "no"


# =============================================================================
# Concrete turns
# =============================================================================

class SpeakTurn(ParticipantTurn):
    """A free-form speech in a channel (also used for concurrent speeches)."""

    budget_kind = "speak"
    action_tag = "spk"
    action_type = "speak"
    round_start_event = "player_speak_start"
    tool_events = True
    done_event = "player_speak_done"
    stage_content = "发言生成中"
    status_prefix = True
    last_round_without_tools = True
    claim_active_action = True
    complete_truncated = True
    pass_key = "pass_speech"

    def __init__(self, orch: Any, player_id: str, channel_id: str, instruction: str, **kw: Any) -> None:
        cfg = orch.cfg
        self.session = orch.session  # needed by the message builders before base init
        super().__init__(
            orch,
            player_id,
            channel_id,
            build_participant_messages(orch.session, cfg, player_id, channel_id, instruction),
            get_participant_tools_schema("speak"),
            rebuild=lambda: build_participant_messages(
                self.session, cfg, player_id, channel_id, instruction
            ),
            **kw,
        )
        self.text = ""

    async def on_no_tool_calls(
        self, resp: LLMResponse, round_idx: int, is_last: bool
    ) -> tuple[str, str] | None:
        text = resp.content.strip()
        if not text and resp.thought:
            text = await recover_empty_speech(
                self.orch, self.session, self.pid, self.messages, resp.thought
            )
        elif (
            text
            and self.complete_truncated
            and resp.finish_reason == "length"
            and text[-1] not in _SENTENCE_END
        ):
            text = await complete_truncated_speech(
                self.orch, self.session, self.pid, self.messages, text
            )
        self.text = text
        return None

    async def finalize(self) -> None:
        self.text = self.text or scenario_text(self.session, self.pass_key)
        self.emit_record(
            stream="channel",
            channel_id=self.channel_id,
            kind="message",
            actor_id=self.pid,
            content=self.text,
        )

    def degrade(self, exc: BaseException) -> None:
        self.text = scenario_text(self.session, self.pass_key)
        self.emit_record(
            stream="channel",
            channel_id=self.channel_id,
            kind="message",
            actor_id=self.pid,
            content=self.text,
        )


class ConcurrentSpeakTurn(SpeakTurn):
    action_tag = "conc"
    action_type = "concurrent"
    round_start_event = None
    tool_events = False
    done_event = "concurrent_item_done"
    status_prefix = False
    claim_active_action = False
    complete_truncated = False
    pass_key = "concurrent_abstain"


class YesNoTurn(ParticipantTurn):
    """A private binary inquiry answered through ``answer_yes_or_no``."""

    budget_kind = "decision"
    action_tag = "yn"
    action_type = "yes_no"
    done_event = "yes_no_item_done"
    stage_thought = "私聊问讯思考中"

    def __init__(
        self,
        orch: Any,
        player_id: str,
        dm_channel_id: str,
        question: str,
        yes_label: str,
        no_label: str,
        **kw: Any,
    ) -> None:
        cfg = orch.cfg
        self.session = orch.session  # needed by the message builders before base init

        def build() -> list[dict[str, str]]:
            return build_participant_yes_no_messages(
                self.session,
                cfg,
                player_id,
                dm_channel_id,
                question=question,
                yes_label=yes_label,
                no_label=no_label,
            )

        super().__init__(
            orch,
            player_id,
            dm_channel_id,
            build(),
            get_participant_tools_schema("yes_no"),
            rebuild=build,
            **kw,
        )
        self.yes_label = yes_label
        self.no_label = no_label
        self.decision: str | None = None  # None = never answered (no default!)

    @property
    def meaning(self) -> str:
        return self.yes_label if self.decision == "yes" else self.no_label

    async def handle_tool(self, name: str, args: dict[str, Any]) -> str | None:
        if name != "answer_yes_or_no":
            return None
        if self.finished:
            return tool_json(ok=False, error="已提交答复，重复调用被忽略")
        decision = parse_yes_no(args.get("decision"), self.yes_label, self.no_label)
        if decision is None:
            return tool_json(ok=False, error="decision 只能是 \"yes\" 或 \"no\"")
        self.decision = decision
        self.finished = True
        return tool_json(ok=True, player_id=self.pid, decision=decision, meaning=self.meaning)

    async def on_no_tool_calls(
        self, resp: LLMResponse, round_idx: int, is_last: bool
    ) -> tuple[str, str] | None:
        raw = resp.content.strip()
        if is_last:
            if raw:
                self.decision = parse_yes_no_text(raw, self.yes_label, self.no_label)
            self.finished = True
            return None
        return (
            raw or "（未调用问讯答复工具）",
            "请立即调用 `answer_yes_or_no(decision)` 工具提交你的二元决定（`decision` 仅填 `\"yes\"` 或 `\"no\"`，不要附带任何备注，文字回复无效）：",
        )

    async def finalize(self) -> None:
        if self.decision is None:
            content = "⚠️ 问讯未作答（不视为 YES，也不视为 NO）"
        else:
            icon = "✅" if self.decision == "yes" else "❌"
            content = f"{icon} 问讯答复：【{self.meaning}】 ({self.decision.upper()})"
        self.emit_record(
            stream="channel",
            channel_id=self.channel_id,
            kind="message",
            actor_id=self.pid,
            content=content,
        )

    def degrade(self, exc: BaseException) -> None:
        self.decision = None
        self.emit_record(
            stream="channel",
            channel_id=self.channel_id,
            kind="message",
            actor_id=self.pid,
            content="⚠️ 问讯未作答（执行失败）",
        )


class PlanTurn(ParticipantTurn):
    """Private planning / review ending in ``submit_strategic_plan``."""

    budget_kind = "plan"
    action_tag = "plan"
    action_type = "plan"
    done_event = "planning_item_done"
    stage_thought = "深度战略思考中"

    def __init__(
        self,
        orch: Any,
        player_id: str,
        dm_channel_id: str,
        phase_topic: str,
        instruction: str,
        **kw: Any,
    ) -> None:
        cfg = orch.cfg
        self.session = orch.session  # needed by the message builders before base init

        def build() -> list[dict[str, str]]:
            return build_participant_planning_messages(
                self.session, cfg, player_id, phase_topic=phase_topic, instruction=instruction
            )

        super().__init__(
            orch, player_id, dm_channel_id, build(), get_participant_tools_schema("plan"), rebuild=build, **kw
        )
        self.phase_topic = phase_topic
        self.summary = ""
        self.focus = ""

    def _save_note(self) -> None:
        self.session.write_agent_note(
            self.pid,
            f"【{self.phase_topic}】战略预案",
            f"**核心纲要**: {self.summary}\n**行动焦点**: {self.focus}",
        )

    async def handle_tool(self, name: str, args: dict[str, Any]) -> str | None:
        if name != "submit_strategic_plan":
            return None
        self.summary = arg_str(args, "plan_summary")
        self.focus = arg_str(args, "current_focus")
        self.finished = True
        self._save_note()
        return tool_json(
            ok=True,
            message="战略规划提交成功，已牢固记录到个人战略认知与笔记中！",
            plan_summary=self.summary,
            current_focus=self.focus,
        )

    async def on_no_tool_calls(
        self, resp: LLMResponse, round_idx: int, is_last: bool
    ) -> tuple[str, str] | None:
        raw = resp.content.strip()
        if is_last and not self.finished:
            self.summary = raw or scenario_text(self.session, "plan_fallback_summary")
            self.focus = scenario_text(self.session, "plan_fallback_focus")
            self._save_note()
            self.finished = True
            return None
        return (raw or "（正在进行宏观战略构思）", scenario_text(self.session, "plan_nudge"))

    async def finalize(self) -> None:
        if not self.summary:
            self.summary = scenario_text(self.session, "plan_fallback_summary")
            self.focus = scenario_text(self.session, "plan_fallback_focus")
            self._save_note()
        self.emit_record(
            stream="channel",
            channel_id=self.channel_id,
            kind="message",
            actor_id=self.pid,
            content=(
                f"📋 【{self.phase_topic} · 战略预案已确立】\n"
                f"- **核心纲要**: {self.summary}\n- **行动焦点**: {self.focus}"
            ),
        )

    def degrade(self, exc: BaseException) -> None:
        self.summary = "（本次规划执行失败，未形成预案）"
        self.focus = ""
        self.emit_record(
            stream="channel",
            channel_id=self.channel_id,
            kind="message",
            actor_id=self.pid,
            content=f"⚠️ 【{self.phase_topic}】规划执行失败，未形成预案",
        )


class VoteTurn(ParticipantTurn):
    """Tool-only voting; free text never counts."""

    budget_kind = "decision"
    action_tag = "vote"
    action_type = "vote"
    done_event = "vote_item_done"
    stage_thought = "投票思考中"

    def __init__(
        self,
        orch: Any,
        player_id: str,
        channel_id: str,
        vote_topic: str,
        candidate_ids: list[str],
        allow_abstain: bool,
        **kw: Any,
    ) -> None:
        cfg = orch.cfg
        self.session = orch.session  # needed by the message builders before base init

        def build() -> list[dict[str, str]]:
            return build_participant_vote_messages(
                self.session,
                cfg,
                player_id,
                channel_id,
                vote_topic=vote_topic,
                candidate_ids=candidate_ids or None,
                allow_abstain=allow_abstain,
            )

        super().__init__(
            orch, player_id, channel_id, build(), get_participant_tools_schema("vote"), rebuild=build, **kw
        )
        self.candidate_ids = candidate_ids
        self.allow_abstain = allow_abstain
        self.target = ""
        self.reason = ""

    async def handle_tool(self, name: str, args: dict[str, Any]) -> str | None:
        if name != "cast_vote":
            return None
        if self.finished:
            return tool_json(ok=False, error="已提交选票，重复调用被忽略")
        raw = arg_str(args, "target_id")
        is_abstain = raw.lower() in ABSTAIN_TOKENS
        invalid = ""
        if not raw:
            invalid = "target_id 不能为空"
        elif is_abstain and not self.allow_abstain:
            invalid = "本轮不允许弃票"
        elif not is_abstain and self.candidate_ids and raw not in self.candidate_ids:
            invalid = "目标不在本轮候选人列表中"
        elif not is_abstain and not self.session.get_participant(raw):
            invalid = "目标参与者不存在"
        if invalid:
            return tool_json(ok=False, error=invalid, candidate_ids=self.candidate_ids)
        self.target = "abstain" if is_abstain else raw
        self.reason = arg_str(args, "reason")
        self.finished = True
        return tool_json(ok=True, voter_id=self.pid, target_id=self.target)

    async def on_no_tool_calls(
        self, resp: LLMResponse, round_idx: int, is_last: bool
    ) -> tuple[str, str] | None:
        if is_last:
            return None
        return (
            resp.content or "（未调用投票工具）",
            "请立即调用 `cast_vote(target_id, reason)` 工具提交你的选票（文字发言无效）：",
        )

    async def finalize(self) -> None:
        target_p = self.session.get_participant(self.target)
        if not self.target:
            message = "⚠️ 未提交有效选票"
        elif target_p:
            message = f"🗳️ 投票给 {target_p.seat} {target_p.name}"
        elif self.target.lower() in ABSTAIN_TOKENS:
            message = "🗳️ 选择弃票"
        else:
            message = f"🗳️ 投票给 {self.target}"
        self.emit_record(
            stream="channel",
            channel_id=self.channel_id,
            kind="message",
            actor_id=self.pid,
            content=message,
        )

    def degrade(self, exc: BaseException) -> None:
        self.target = ""
        self.reason = ""
        self.emit_record(
            stream="channel",
            channel_id=self.channel_id,
            kind="message",
            actor_id=self.pid,
            content="⚠️ 未提交有效选票",
        )


class InterruptTurn(ParticipantTurn):
    """The execution phase of an authorized interruption (``trigger_interrupt``)."""

    budget_kind = "plan"
    action_tag = "int"
    action_type = "interrupt"
    round_start_event = "interrupt_exec_start"
    stage_content = "插话宣言生成中"
    stage_thought = "插话目标思考中"
    claim_active_action = True

    def __init__(
        self,
        orch: Any,
        player_id: str,
        channel_id: str,
        instruction: str,
        candidate_ids: list[str],
        **kw: Any,
    ) -> None:
        cfg = orch.cfg
        self.session = orch.session  # needed by the message builders before base init

        def build() -> list[dict[str, str]]:
            return build_participant_interrupt_messages(
                self.session, cfg, player_id, channel_id, instruction
            )

        super().__init__(
            orch, player_id, channel_id, build(), get_participant_tools_schema("interrupt"), rebuild=build, **kw
        )
        self.candidate_ids = candidate_ids
        self.speech = ""
        self.target = ""

    async def handle_tool(self, name: str, args: dict[str, Any]) -> str | None:
        if name != "trigger_interrupt":
            return None
        if self.finished:
            return tool_json(ok=False, error="已提交插话，重复调用被忽略")
        text = arg_str(args, "action_text")
        target_id = arg_str(args, "target_id")
        target = self.session.get_participant(target_id) if target_id else None
        invalid = ""
        if not text:
            invalid = "action_text 不能为空"
        elif target_id and not target:
            invalid = "目标参与者不存在"
        elif target is not None and target.data.get("alive") is False:
            invalid = "目标参与者当前不可选"
        elif target_id and self.candidate_ids and target_id not in self.candidate_ids:
            invalid = "目标不在合法候选中"
        if invalid:
            return tool_json(ok=False, error=invalid)
        self.speech = text
        self.target = target_id
        self.finished = True
        return tool_json(
            ok=True,
            status="interrupted",
            player_id=self.pid,
            action_text=text,
            target_id=target_id,
        )

    async def on_no_tool_calls(
        self, resp: LLMResponse, round_idx: int, is_last: bool
    ) -> tuple[str, str] | None:
        if is_last:
            return None
        return (
            resp.content.strip() or "（尚未调用 trigger_interrupt 工具）",
            "请立即调用 `trigger_interrupt(action_text, target_id)` 工具正式提交你的插话宣言与技能目标：",
        )


# =============================================================================
# Fallback helpers for speeches
# =============================================================================

async def recover_empty_speech(
    orch: Any, session: GameSession, actor_id: str, original: list[dict[str, Any]], thought_text: str
) -> str:
    """If a participant spent all tokens in reasoning, request a concise final speech."""
    followup = [
        *original,
        {"role": "assistant", "content": f"<think>{thought_text[-1200:]}</think>"},
        {
            "role": "user",
            "content": "你刚才完成了思考但尚未输出正式发言正文。请立即用 250~450 字直接输出你的正式发言内容（无需再长篇思考）：",
        },
    ]
    try:
        resp = await orch.llm.chat_stream(
            followup,
            is_moderator=False,
            max_tokens_override=1536,
            binding=session.get_model_binding(actor_id),
        )
        session.record_response_usage(actor_id, resp)
        return resp.content.strip()
    except Exception:  # noqa: BLE001 - best effort; the caller falls back to the pass text
        logger.warning("Empty-speech recovery failed", exc_info=True)
        return ""


async def complete_truncated_speech(
    orch: Any, session: GameSession, actor_id: str, original: list[dict[str, Any]], partial: str
) -> str:
    """Finish a speech that was cut off mid-sentence by the token limit."""
    followup = [
        *original,
        {"role": "assistant", "content": partial},
        {"role": "user", "content": scenario_text(session, "truncated_followup")},
    ]
    try:
        resp = await orch.llm.chat_stream(
            followup,
            is_moderator=False,
            max_tokens_override=512,
            binding=session.get_model_binding(actor_id),
        )
        session.record_response_usage(actor_id, resp)
        tail = resp.content.strip()
        if tail:
            return f"{partial}{tail}"
    except Exception:  # noqa: BLE001
        logger.warning("Truncated-speech completion failed", exc_info=True)
    return partial


# =============================================================================
# Tasks (called from the moderator tool handlers / the scenario director)
# =============================================================================

def _player_ids(args: dict[str, Any]) -> list[str]:
    ids = arg_str_list(args, "player_ids")
    single = arg_str(args, "player_id")
    if single and single not in ids:
        ids.append(single)
    return list(dict.fromkeys(ids))


def _status_names(names: list[str]) -> str:
    return ", ".join(names[:4]) + ("..." if len(names) > 4 else "")


async def run_speak(orch: Any, player_id: str, channel_id: str, instruction: str) -> tuple[str, str | None]:
    """A single participant speaks in a channel (errors propagate and pause the game)."""
    session = orch.session
    participant = session.get_participant(player_id)
    if not participant:
        return "（参与者不存在）", None
    session.set_generating(
        player_id, f"{participant.name} 思考与发言中", stage_text="思维链思考中"
    )
    turn = SpeakTurn(orch, player_id, channel_id, instruction)
    await turn.run()
    return turn.text, None


async def run_concurrent(orch: Any, name: str, args: dict[str, Any]) -> tuple[str, str | None]:
    session = orch.session
    requests = [r for r in (args.get("requests") or []) if isinstance(r, dict)]
    if not requests:
        return "无有效并发请求。", None

    plan: list[tuple[str, str, str]] = []
    for request in requests:
        cid = arg_str(request, "channel_id", "public")
        pid = arg_str(request, "player_id")
        instruction = arg_str(request, "instruction")
        channel = session.get_channel(cid)
        participant = session.get_participant(pid)
        if not channel or not participant:
            return orch._tool_error(name, args, f"错误：未找到频道 `{cid}` 或参与者 `{pid}`。")
        if channel.status != "open":
            return orch._tool_error(name, args, f"错误：频道 `{cid}` 未开放。")
        if pid not in channel.members:
            return orch._tool_error(name, args, f"错误：参与者 `{pid}` 不是频道 `{cid}` 的成员。")
        plan.append((pid, cid, instruction))

    # Every snapshot is built BEFORE any participant speaks (fair blind polling).
    turns = [
        ConcurrentSpeakTurn(orch, pid, cid, instruction, defer_records=True)
        for pid, cid, instruction in plan
    ]
    names = [t.name for t in turns]
    for turn in turns:
        session.set_participant_stage(turn.pid, True, "并发思考中")
    session.active_status_text = f"并发采集中 ({len(turns)}人): {_status_names(names)}"
    session.set_moderator_state(True, f"⚡ 并发收集 {len(turns)} 名参与者响应中...")
    await orch.broadcast_state("concurrent_start")

    await run_batch(orch, turns, label="并发发言")

    lines = [f"- [{t.channel_id}] {t.name} ({t.pid}): {t.text}" for t in turns]
    await orch.broadcast_state("concurrent_done")
    res = "\n".join(lines)
    orch._log_tool(name, args, res, None)
    return res, None


async def run_ask_yes_no(orch: Any, args: dict[str, Any]) -> tuple[str, str | None]:
    """Private yes/no inquiry in each participant's own DM channel."""
    session = orch.session
    player_ids = _player_ids(args)
    question = arg_str(args, "question", "请做出『是』或『否』的二元决定。")
    yes_label = arg_str(args, "yes_label") or "是 (YES)"
    no_label = arg_str(args, "no_label") or "否 (NO)"
    if not player_ids:
        return orch._tool_error("ask_yes_or_no", args, "错误：player_ids 不能为空。")

    valid = [pid for pid in player_ids if session.get_participant(pid)]
    if not valid:
        return orch._tool_error("ask_yes_or_no", args, "错误：未匹配到有效的参与者。")

    turns = [
        YesNoTurn(
            orch, pid, orch._ensure_player_dm_channel(pid), question, yes_label, no_label,
            defer_records=True,
        )
        for pid in valid
    ]
    for turn in turns:
        session.set_participant_stage(turn.pid, True, "私聊问讯思考中")
    session.active_status_text = f"私聊问讯中 ({len(turns)}人): {_status_names([t.name for t in turns])}"
    session.set_moderator_state(
        True, f"❓ 私聊问讯 {len(turns)} 名参与者 ({yes_label} / {no_label})..."
    )
    await orch.broadcast_state("yes_no_start")

    await run_batch(orch, turns, label="问讯")

    yes_players: list[str] = []
    no_players: list[str] = []
    unanswered: list[str] = []
    detail_lines: list[str] = []
    for turn in turns:
        p = session.get_participant(turn.pid)
        label = f"{p.seat} {p.name} ({turn.pid})" if p else turn.pid
        if turn.decision == "yes":
            yes_players.append(label)
            detail_lines.append(f"- [{turn.channel_id}] {label}: **YES（{yes_label}）**")
        elif turn.decision == "no":
            no_players.append(label)
            detail_lines.append(f"- [{turn.channel_id}] {label}: **NO（{no_label}）**")
        else:
            unanswered.append(label)
            detail_lines.append(f"- [{turn.channel_id}] {label}: **未作答**")

    await orch.broadcast_state("yes_no_done")
    res = (
        f"【私聊二元问讯完成】问题：{question}\n"
        f"- ✅ 选择『YES（{yes_label}）』({len(yes_players)}人): {', '.join(yes_players) or '无'}\n"
        f"- ❌ 选择『NO（{no_label}）』({len(no_players)}人): {', '.join(no_players) or '无'}\n"
        + (
            f"- ⚠️ 未作答 ({len(unanswered)}人，不视为 YES 或 NO): {', '.join(unanswered)}\n"
            if unanswered
            else ""
        )
        + "\n### 私聊问讯明细（仅主持人可见，其他玩家不可见）\n"
        + "\n".join(detail_lines)
    )
    orch._log_tool("ask_yes_or_no", args, res, turns[0].channel_id if len(turns) == 1 else None)
    return res, None


async def run_planning(orch: Any, args: dict[str, Any]) -> tuple[str, str | None]:
    """Concurrent private planning; results stay in each participant's DM and notes."""
    session = orch.session
    name = "request_players_strategic_planning"
    player_ids = _player_ids(args)
    phase_topic = arg_str(args, "phase_topic", "开局宏观战略规划与推演")
    instruction = arg_str(args, "instruction")
    if not player_ids:
        return orch._tool_error(name, args, "错误：player_ids 不能为空。")
    valid = [pid for pid in player_ids if session.get_participant(pid)]
    if not valid:
        return orch._tool_error(name, args, "错误：未匹配到有效的参与者。")

    turns = [
        PlanTurn(
            orch, pid, orch._ensure_player_dm_channel(pid), phase_topic, instruction,
            defer_records=True,
        )
        for pid in valid
    ]
    for turn in turns:
        session.set_participant_stage(turn.pid, True, f"战略推演中({phase_topic})")
    session.active_status_text = (
        f"战略规划中 ({len(turns)}人并发): {_status_names([t.name for t in turns])}"
    )
    session.set_moderator_state(True, f"🎯 调度 {len(turns)} 名参与者并发进行【{phase_topic}】...")
    await orch.broadcast_state("planning_start")

    await run_batch(orch, turns, label="规划")

    detail_lines: list[str] = []
    for turn in turns:
        p = session.get_participant(turn.pid)
        label = f"{p.seat} {p.name} ({turn.pid})" if p else turn.pid
        detail_lines.append(f"- **{label}**: 纲要: {turn.summary} | 焦点: {turn.focus}")
    res = (
        f"🎯 【{phase_topic} · 并发推演完成】已调度 {len(turns)} 名参与者完成推演与笔记记录！\n"
        "所有预案均已保存在各参与者私密通道与笔记本中，未在公频透露任何信息。\n\n"
        "### 各玩家纲要概览（仅主持人可见，对其他玩家绝对隐蔽）\n"
        + "\n".join(detail_lines)
        + "\n\n请调用方根据当前编排继续推进下一步骤！"
    )
    orch._log_tool(name, args, res, None)
    return res, None


async def run_vote(orch: Any, args: dict[str, Any]) -> tuple[str, str | None]:
    """Concurrent tool-only voting; sets ``orch._last_tool_data`` to the ballots."""
    session = orch.session
    name = "request_players_vote"
    cid = arg_str(args, "channel_id", "public")
    voter_ids = list(dict.fromkeys(arg_str_list(args, "voter_ids")))
    vote_topic = arg_str(args, "vote_topic", "投票表决")
    candidate_ids = arg_str_list(args, "candidate_ids")
    allow_abstain = bool(args.get("allow_abstain", True))

    if not voter_ids:
        return orch._tool_error(name, args, "错误：voter_ids 不能为空。", cid)
    channel = session.get_channel(cid)
    if not channel:
        return orch._tool_error(name, args, f"错误：频道 `{cid}` 不存在。", cid)
    if channel.status != "open":
        return orch._tool_error(name, args, f"错误：频道 `{cid}` 未开放投票。", cid)
    for pid in voter_ids:
        if not session.get_participant(pid):
            return orch._tool_error(name, args, f"错误：投票人 `{pid}` 不存在。", cid)
        if pid not in channel.members:
            return orch._tool_error(name, args, f"错误：投票人 `{pid}` 不是频道 `{cid}` 的成员。", cid)

    turns = [
        VoteTurn(orch, pid, cid, vote_topic, candidate_ids, allow_abstain, defer_records=True)
        for pid in voter_ids
    ]
    for turn in turns:
        session.set_participant_stage(turn.pid, True, "投票思考中")
    session.active_status_text = f"投票采集中 ({len(turns)}人): {_status_names([t.name for t in turns])}"
    session.set_moderator_state(True, f"🗳️ 并发收集 {len(turns)} 名参与者选票 ({vote_topic})...")
    await orch.broadcast_state("vote_start")

    await run_batch(orch, turns, label="投票")

    tally: dict[str, list[str]] = {}
    ballot_lines: list[str] = []
    for turn in turns:
        voter = session.get_participant(turn.pid)
        voter_name = f"{voter.seat} {voter.name}" if voter else turn.pid
        if not turn.target:
            ballot_lines.append(f"- {voter_name} ({turn.pid}) -> 未提交有效选票")
            continue
        target_p = session.get_participant(turn.target)
        if target_p:
            target_label = f"{target_p.seat} ({target_p.name} / {target_p.id})"
            tally_key = f"{target_p.id} ({target_p.seat} {target_p.name})"
        elif turn.target.lower() in ABSTAIN_TOKENS:
            target_label = "弃票 (abstain)"
            tally_key = "abstain (弃票)"
        else:
            target_label = tally_key = turn.target
        tally.setdefault(tally_key, []).append(f"{voter_name}({turn.pid})")
        ballot_lines.append(f"- {voter_name} ({turn.pid}) -> {target_label}")

    await orch.broadcast_state("vote_done")
    tally_lines = [f"- {k}: 共 {len(v)} 票（投票人: {', '.join(v)}）" for k, v in tally.items()]
    res = (
        f"【{vote_topic} · 投票工具采集完成】\n"
        "### 选票明细\n"
        + "\n".join(ballot_lines)
        + "\n\n### 票数汇总（未加权）\n"
        + "\n".join(tally_lines)
    )
    ballots = [
        {"voter_id": t.pid, "target_id": t.target, "reason": t.reason} for t in turns if t.target
    ]
    raw_tally: dict[str, int] = {}
    for ballot in ballots:
        raw_tally[ballot["target_id"]] = raw_tally.get(ballot["target_id"], 0) + 1
    orch._last_tool_data = {"ballots": ballots, "tally": raw_tally}
    orch._log_tool(name, args, res, cid)
    return res, None


async def run_interrupt(orch: Any, args: dict[str, Any]) -> tuple[str, str | None]:
    """Execute an interruption the caller has already authorized."""
    session = orch.session
    name = "request_player_interrupt"
    cid = arg_str(args, "channel_id", "public")
    pid = arg_str(args, "player_id")
    instruction = arg_str(
        args, "instruction", "请调用 `trigger_interrupt(action_text, target_id)` 提交插话内容与可选目标。"
    )
    channel = session.get_channel(cid)
    participant = session.get_participant(pid)
    if not channel or not participant:
        return orch._tool_error(name, args, f"错误：未找到频道 `{cid}` 或参与者 `{pid}`。", cid)
    if channel.status != "open" or participant.data.get("alive") is False:
        return orch._tool_error(name, args, f"错误：频道 `{cid}` 或参与者 `{pid}` 当前不允许插话。", cid)
    if pid not in channel.members:
        return orch._tool_error(name, args, f"错误：参与者 `{pid}` 不是频道 `{cid}` 的成员。", cid)
    quota = channel.interrupt_quotas.get(pid)
    if not quota or quota.used >= quota.max_count:
        return orch._tool_error(name, args, f"错误：参与者 `{pid}` 在频道 `{cid}` 没有剩余插话额度。", cid)

    session.set_generating(pid, f"{participant.name} 执行插话技能中", stage_text="插话技能执行中")
    session.set_moderator_state(True, f"💥 调度 {participant.name} ({pid}) 执行插话环节...")
    turn = InterruptTurn(orch, pid, cid, instruction, arg_str_list(args, "candidate_ids"))
    await turn.run()

    if not turn.finished or not turn.speech:
        res = "未提交有效插话；未消耗额度，也未产生频道消息。"
        orch._last_tool_data = {"triggered": False, "player_id": pid}
        orch._log_tool(name, args, res, cid)
        return res, None

    quota.used += 1
    full_msg = turn.speech
    target_summary = "无"
    if turn.target:
        target_p = session.get_participant(turn.target)
        target_label = f"{target_p.seat} {target_p.name} ({target_p.id})" if target_p else turn.target
        target_summary = target_label
        if turn.target not in full_msg:
            full_msg = f"{full_msg}（指定目标：{target_label}）"
    session.add_record(
        stream="channel",
        channel_id=cid,
        kind="message",
        actor_id=pid,
        content=full_msg,
        action_id=turn.action_id,
        action_type="interrupt",
    )
    await orch.broadcast_state("interrupt_triggered")
    res = (
        f"【插话执行完成】{participant.seat} {participant.name} ({pid}) 在频道 `{cid}` 提交了插话：\n"
        f"- 内容：{turn.speech}\n"
        f"- 目标 (`target_id`)：{target_summary}\n"
        "结构化结果已返回调用方，后续状态变化由当前编排定义处理。"
    )
    orch._last_tool_data = {
        "triggered": True,
        "player_id": pid,
        "target_id": turn.target,
        "action_text": turn.speech,
    }
    orch._log_tool(name, args, res, cid, action_id=turn.action_id, action_type="interrupt")
    return res, f"{participant.name} ({pid}) 已执行插话打断：{full_msg}"


def speech_tool_result(participant_name: str, player_id: str, channel_id: str, speech: str) -> str:
    """Tool result for the moderator after a participant spoke (speech wrapped as data)."""
    return (
        f"已将发言权移交给 {participant_name} ({player_id})，其在频道 `{channel_id}` 发言完毕：\n"
        + wrap_untrusted_block(speech, source=player_id, channel=channel_id)
    )
