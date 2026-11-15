from __future__ import annotations

import json
from typing import Any

from app.config import load_prompt_template
from app.engine.bindings import context_window_for
from app.engine.llm import estimate_text_tokens
from app.engine.sanitize import neutralize_untrusted_text
from app.engine.state import GameSession, Participant
from app.engine.tools import list_knowledge_docs_summary


def _render_template(template: str, variables: dict[str, str]) -> str:
    result = template
    for key, val in variables.items():
        result = result.replace(f"{{{{{key}}}}}", str(val))
    return result


def _get_session_prompt_template(session: GameSession, rel_path: str) -> str:
    """
    Cache prompt templates per GameSession so disk edits during development
    do not mid-game mutate the prompt contract of an already-running session.
    """
    if rel_path not in session.prompt_cache:
        template = load_prompt_template(rel_path)
        scenario_body = str(session.scenario_snapshot.get("body", "")).strip()
        if scenario_body:
            template = (
                f"{template}\n\n---\n\n# 当前剧本的权威规则说明\n"
                "以下内容由当前对局快照中的可执行剧本携带；流程与结算以其机器定义为准。\n\n"
                f"{scenario_body}"
            )
        session.prompt_cache[rel_path] = template
    return session.prompt_cache[rel_path]


def _extract_record_seq_num(rec_id: str) -> int:
    try:
        return int(rec_id.replace("rec_", ""))
    except Exception:
        return 0


def _build_context_window_status_text(
    session: GameSession,
    current_prompt_blob: str,
    actor_id: str | None = None,
) -> str:
    """
    Compute estimated prompt tokens vs. the context window of *actor_id*'s model binding
    (the legacy session-wide window when the actor has no binding).
    Emit an explicit compaction alert when approaching (>=75%) or exceeding the limit.
    """
    est_tokens = estimate_text_tokens(current_prompt_blob)
    window_limit = max(2000, context_window_for(session, actor_id))
    pct = round((est_tokens / window_limit) * 100.0, 1)

    if pct >= 75.0:
        return (
            "### ⚠️ 上下文窗口水位告警（Context Window Alert）\n"
            f"- **当前上下文占用**: 约 `{est_tokens} / {window_limit}` tokens（**{pct}%**，已接近或超过设定的上下文窗口上限！）\n"
            "- 🚨 **重要行动建议**：请在本轮**优先调用 `compact_context(summary_markdown, keep_recent_count)` 工具**，将早期历史记录提炼压缩为结构化摘要以释放上下文空间，然后再继续执行当前发言/问讯/主持任务！"
        )
    return (
        "### 📊 上下文窗口水位（Context Window Status）\n"
        f"- **当前上下文占用**: 约 `{est_tokens} / {window_limit}` tokens（`{pct}%`）—— 水位正常（当占用接近或超过窗口上限的 75% 时，请考虑调用 `compact_context` 压缩上下文）。"
    )


def get_public_tags_for_player(
    session: GameSession,
    participant: Participant,
) -> list[str]:
    """Return only public status/badge tags (slot == 'right') visible to all players."""
    tags = participant.tags
    for event in reversed(session.scenario_runtime.get("events", [])):
        if event.get("player_id") == participant.id and isinstance(event.get("previous_tags"), list) and not event.get("announced"):
            tags = list(event["previous_tags"])
            break
    public_tags: list[str] = []
    for tag in tags:
        style = session.tag_styles.get(tag, {})
        if style.get("slot") == "right" or style.get("public_visible") is True:
            public_tags.append(tag)
    return public_tags


def build_moderator_system_prompt(
    session: GameSession,
    cfg: dict[str, Any],
    history_blob: str = "",
) -> str:
    """Build the full system prompt + live state snapshot for the Moderator Agent."""
    prompts_cfg = cfg.get("prompts", {})
    base_prompt = _get_session_prompt_template(
        session, prompts_cfg.get("moderator_system", "prompts/moderator.md")
    )

    roster_lines: list[str] = []
    for p in session.participants:
        init_info = session.initial_assignments.get(p.id, {})
        init_role = init_info.get("role", "未指定")
        init_faction = init_info.get("faction", "未指定")
        tags_str = "、".join(p.tags) if p.tags else "（尚未赋予标签）"
        roster_lines.append(
            f"- {p.seat} | ID: `{p.id}` | 姓名: **{p.name}** | 初始分配参考: `{init_role}` ({init_faction}) | 当前UI标签: [{tags_str}]"
        )

    channel_lines: list[str] = []
    for ch in session.channels:
        member_names = [session.get_actor_display_name(m) for m in ch.members]
        quotas_desc = ", ".join(
            f"{q.player_name}({q.used}/{q.max_count})"
            for q in ch.interrupt_quotas.values()
        ) or "无"
        channel_lines.append(
            f"- 频道 `{ch.id}` (类型: {ch.type}, 状态: {ch.status}) | 成员({len(ch.members)}): {', '.join(member_names)} | 插话权: {quotas_desc}"
        )
    channels_block = (
        "\n".join(channel_lines)
        if channel_lines
        else "- （当前尚未创建任何频道，可按任务需要调用 `create_channel`）"
    )

    faction_lines = [
        f"- {f.name}: {f.alive} / {f.total}" for f in session.dashboard.factions
    ]
    stat_lines = [
        f"- {t.name}: {t.count}" for t in session.dashboard.tag_stats
    ]
    dashboard_block = (
        "\n".join(faction_lines + stat_lines)
        if (faction_lines or stat_lines)
        else "- （当前状态面板为空，可调用 `update_dashboard` 初始化分组与标签统计）"
    )

    docs_list = list_knowledge_docs_summary(cfg)
    docs_block = (
        "\n".join(f"- `{d['doc_name']}`: {d['title']}" for d in docs_list)
        if docs_list
        else "- （暂无文档）"
    )
    mod_notes_block = session.get_agent_notes_text("moderator")
    engine_notes_block = session.get_engine_notes_text()
    mod_compacted = session.get_agent_compacted_summary("moderator")
    compacted_section = (
        f"\n\n### 7. 主持人已压缩的历史上下文摘要\n{mod_compacted}"
        if mod_compacted
        else ""
    )

    draft_snapshot = (
        "\n\n---\n"
        "## 三、当前实时全局状态快照（仅主持人可见）\n\n"
        f"- **当前顶栏阶段文本**: `{session.phase_text}`\n\n"
        "### 1. 参与者座位、初始分配参考与当前 UI 标签\n"
        + "\n".join(roster_lines)
        + "\n\n### 2. 当前已创建的频道与插话权列表\n"
        + channels_block
        + "\n\n### 3. 右侧「对局状态」面板数据\n"
        + dashboard_block
        + "\n\n### 4. 可查阅的参考文档（调用 `read_document` 读取）\n"
        + docs_block
        + "\n\n### 5. 主持人内部处理日志（`record_engine_note` 常驻归档，不受滑窗截断影响）\n"
        + engine_notes_block
        + "\n\n### 6. 主持人私人笔记本（调用 `write_note` 更新）\n"
        + mod_notes_block
        + compacted_section
    )
    window_banner = _build_context_window_status_text(
        session, base_prompt + draft_snapshot + history_blob, "moderator"
    )
    return base_prompt + draft_snapshot + "\n\n" + window_banner + "\n"


def _build_player_own_tool_memory_text(
    session: GameSession,
    player_id: str,
) -> str:
    """
    Build a complete memory log of every tool call (`kind == 'tool_call'`)
    made by `player_id` itself so the player agent ALWAYS knows every tool
    call it has executed across the game.
    """
    own_tools: list[str] = []
    for rec in session.records:
        if rec.kind != "tool_call" or rec.actor_id != player_id:
            continue
        t_name = rec.tool_name or "tool"
        t_args_str = json.dumps(rec.tool_args or {}, ensure_ascii=False)
        t_res_str = (rec.tool_result or "").strip()
        if len(t_res_str) > 220:
            t_res_str = t_res_str[:220] + " ...[结果已节选]"
        ch_tag = f"频道:{rec.channel_id}" if rec.channel_id else "个人"
        own_tools.append(
            f"- [{rec.timestamp}] [{ch_tag}] `{t_name}({t_args_str})` -> {t_res_str}"
        )

    if not own_tools:
        return "（你到目前为止尚未调用过任何工具）"
    return "\n".join(own_tools[-35:])


def _build_isolated_history_text(
    session: GameSession,
    player_id: str,
    max_messages: int = 60,
) -> tuple[str, str]:
    """
    Strictly derive visible channels, channel messages, and the player's OWN
    tool calls from session records, respecting any `compact_context` summary.
    Returns (channels_summary_text, messages_history_text).
    """
    member_channels = [
        ch for ch in session.channels if player_id in ch.members
    ]
    member_channel_ids = {ch.id for ch in member_channels}

    ch_lines: list[str] = []
    for ch in member_channels:
        member_names = [
            f"{session.get_actor_display_name(m)}({m})" for m in ch.members
        ]
        ch_lines.append(
            f"- 频道 `{ch.id}` ({ch.type}, 状态: {ch.status}) | 频道成员: {', '.join(member_names)}"
        )
    channels_summary = "\n".join(ch_lines) if ch_lines else "- 暂无加入的频道"

    compacted_info = session.agent_compacted_state.get(player_id)
    cutoff_seq = (
        int(compacted_info.get("compacted_record_seq", 0))
        if compacted_info
        else 0
    )
    keep_recent_at_compact = (
        int(compacted_info.get("keep_recent_count", 10))
        if compacted_info
        else 10
    )

    before_cutoff_items: list[str] = []
    after_cutoff_items: list[str] = []

    for rec in session.records:
        seq_num = _extract_record_seq_num(rec.id)
        line_str: str | None = None

        # Channel messages require current membership AND membership at record time.
        # The legacy visible_to name list is used only for pre-migration saves.
        viewer_name = session.get_actor_display_name(player_id)
        visible_at_record_time = (
            player_id in rec.visible_to_ids
            if rec.visible_to_ids
            else viewer_name in rec.visible_to
            if rec.visible_to
            else True
        )
        if (
            rec.stream == "channel"
            and rec.kind == "message"
            and rec.channel_id in member_channel_ids
            and visible_at_record_time
        ):
            msg_body = rec.content.strip()
            if len(msg_body) > 900:
                msg_body = msg_body[:900] + " ...[长发言已节选]"
            line_str = (
                f"[{rec.timestamp}] [频道:{rec.channel_id}] {rec.actor_name}: "
                f"{neutralize_untrusted_text(msg_body)}"
            )

        # 2. The player's OWN tool calls (strictly invisible to any other player)
        elif rec.kind == "tool_call" and rec.actor_id == player_id:
            t_name = rec.tool_name or "tool"
            t_args_str = json.dumps(rec.tool_args or {}, ensure_ascii=False)
            t_res_str = (rec.tool_result or "").strip()
            if len(t_res_str) > 200:
                t_res_str = t_res_str[:200] + " ...[已节选]"
            line_str = f"[{rec.timestamp}] 🛠️ [你自己的工具调用] `{t_name}({t_args_str})` -> {t_res_str}"

        if not line_str:
            continue

        if cutoff_seq > 0 and seq_num <= cutoff_seq:
            before_cutoff_items.append(line_str)
        else:
            after_cutoff_items.append(line_str)

    if cutoff_seq > 0 and compacted_info:
        compacted_header = session.get_agent_compacted_summary(player_id)
        retained_tail = before_cutoff_items[-keep_recent_at_compact:]
        combined = [compacted_header, *retained_tail, *after_cutoff_items]
        history_text = "\n".join(combined[-(max_messages + 1) :])
    else:
        all_visible = before_cutoff_items + after_cutoff_items
        history_text = (
            "\n".join(all_visible[-max_messages:])
            if all_visible
            else "（当前你所在的频道尚无历史发言记录）"
        )

    return channels_summary, history_text


def _build_public_seat_board_text(
    session: GameSession,
    viewer_id: str,
) -> str:
    """Build the table seat list showing only public tags for other players."""
    lines: list[str] = []
    for p in session.participants:
        if p.id == viewer_id:
            tags_str = "、".join(p.tags) if p.tags else "尚未公布"
            lines.append(
                f"- {p.seat} ({p.id}) **{p.name}** [你自己] — 标签: [{tags_str}]"
            )
        else:
            pub_tags = get_public_tags_for_player(session, p)
            tags_str = "、".join(pub_tags) if pub_tags else "状态未知"
            lines.append(
                f"- {p.seat} ({p.id}) {p.name} — 公开状态: [{tags_str}]"
            )
    return "\n".join(lines)


def build_participant_messages(
    session: GameSession,
    cfg: dict[str, Any],
    player_id: str,
    target_channel_id: str,
    instruction: str = "",
) -> list[dict[str, str]]:
    """Construct the isolated OpenAI message list for a participant speaking turn."""
    participant = session.get_participant(player_id)
    if not participant:
        raise ValueError(f"Unknown participant: {player_id}")

    prompts_cfg = cfg.get("prompts", {})
    raw_tpl = _get_session_prompt_template(
        session, prompts_cfg.get("participant_system", "prompts/participant.md")
    )

    system_content = _render_template(
        raw_tpl,
        {
            "id": participant.id,
            "seat": participant.seat,
            "name": participant.name,
            "scenario_name": session.scenario_name,
            "persona": participant.persona,
            "tags": "、".join(participant.tags) if participant.tags else "未知",
            "target_channel": target_channel_id,
        },
    )

    channels_summary, history_text = _build_isolated_history_text(
        session, player_id, max_messages=60
    )
    own_tools_memory = _build_player_own_tool_memory_text(session, player_id)
    seat_board_text = _build_public_seat_board_text(session, player_id)
    target_members = session.get_channel_member_names(target_channel_id)
    docs_list = list_knowledge_docs_summary(cfg)
    docs_str = ", ".join(f"`{d['doc_name']}`({d['title']})" for d in docs_list)
    notes_text = session.get_agent_notes_text(player_id)

    core_body = (
        f"### 当前流程状态\n`{session.phase_text}`\n\n"
        f"### 参与者座位与公开状态看板\n{seat_board_text}\n\n"
        f"### 你当前所在的频道列表（决定你的信息可见范围）\n{channels_summary}\n\n"
        f"### 你的私人笔记本（仅你自己可见，可调用 `write_note` 记录或更新）\n{notes_text}\n\n"
        f"### 你的历史工具调用记忆（仅你自己可见）\n{own_tools_memory}\n\n"
        f"### 可查阅的参考文档（可调用 `read_document` 读取）\n{docs_str}\n\n"
        f"### 你可见的历史频道消息与工具轨迹（按时间顺序）\n{history_text}\n\n"
    )
    window_status = _build_context_window_status_text(
        session, system_content + core_body, player_id
    )

    if instruction.strip():
        task_goal = instruction.strip()
    else:
        task_goal = "请依据当前可见信息与频道内的任务要求，在目标频道中作出合适回应。"
    task_instruction_text = (
        f"- **任务目标**: {task_goal}\n"
        "- **思考与输出约束**：思考聚焦当前任务与已知信息（800 字以内），正式回复简洁清晰（250 ~ 500 字以内）。"
    )

    user_prompt = (
        core_body
        + window_status
        + "\n\n---\n"
        f"### 当前频道任务\n"
        f"- **目标频道**: `{target_channel_id}`（该频道内可见成员：{', '.join(target_members)}）\n"
        f"{task_instruction_text}\n\n"
        "你可以先按需调用 `compact_context`（若上下文接近窗口上限）、`read_document` 或 `write_note`；完成工具调用后，请在正式回复正文中直接给出与当前任务相符的回应（不要添加身份名前缀）："
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_prompt},
    ]


def build_participant_vote_messages(
    session: GameSession,
    cfg: dict[str, Any],
    player_id: str,
    channel_id: str,
    vote_topic: str,
    candidate_ids: list[str] | None = None,
    allow_abstain: bool = True,
) -> list[dict[str, str]]:
    """Construct the isolated OpenAI message list for a participant VOTING turn (tool-only)."""
    participant = session.get_participant(player_id)
    if not participant:
        raise ValueError(f"Unknown participant: {player_id}")

    prompts_cfg = cfg.get("prompts", {})
    raw_tpl = _get_session_prompt_template(
        session, prompts_cfg.get("participant_system", "prompts/participant.md")
    )

    system_content = _render_template(
        raw_tpl,
        {
            "id": participant.id,
            "seat": participant.seat,
            "name": participant.name,
            "scenario_name": session.scenario_name,
            "persona": participant.persona,
            "tags": "、".join(participant.tags) if participant.tags else "未知",
            "target_channel": channel_id,
        },
    )

    channels_summary, history_text = _build_isolated_history_text(
        session, player_id
    )
    own_tools_memory = _build_player_own_tool_memory_text(session, player_id)
    seat_board_text = _build_public_seat_board_text(session, player_id)
    notes_text = session.get_agent_notes_text(player_id)

    if candidate_ids:
        cand_desc = "、".join(
            f"`{cid}` ({session.get_actor_display_name(cid)})"
            for cid in candidate_ids
        )
    else:
        cand_desc = "当前任务与频道信息中给出的合法候选参与者 ID"

    abstain_hint = "（若决定弃票，请将 `target_id` 设为 `\"abstain\"`）" if allow_abstain else "（本轮不可弃票，必须投给一名有效候选人）"

    core_body = (
        f"### 当前对局阶段（专属投票环节）\n`{session.phase_text}` — **{vote_topic}**\n\n"
        f"### 场上座位与公开状态看板\n{seat_board_text}\n\n"
        f"### 你自己的私人笔记本\n{notes_text}\n\n"
        f"### 你自己的历史工具调用记忆\n{own_tools_memory}\n\n"
        f"### 你可见的历史频道消息与工具轨迹\n{history_text}\n\n"
    )
    window_status = _build_context_window_status_text(
        session, system_content + core_body + vote_topic, player_id
    )

    user_prompt = (
        core_body
        + window_status
        + "\n\n---\n"
        f"### 🗳️ 当前投票任务（严禁文字发言！）\n"
        f"- **投票主题**: {vote_topic}\n"
        f"- **可选被投票人范围**: {cand_desc} {abstain_hint}\n\n"
        "⚠️ **重要规则约束**：当前处于正式投票环节，为防止借投票发言破坏游戏规则，任何普通文本回复都不会被发送到频道中！\n"
        "请思考（可先按需调用 `compact_context` 压缩上下文或 `write_note` 记录笔记），然后**必须调用 `cast_vote(target_id, reason)` 工具**完成你的投票！"
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_prompt},
    ]


def build_participant_yes_no_messages(
    session: GameSession,
    cfg: dict[str, Any],
    player_id: str,
    dm_channel_id: str,
    question: str,
    yes_label: str = "是",
    no_label: str = "否",
) -> list[dict[str, str]]:
    """Construct an isolated message list for a private binary-choice turn.

    The participant may use reference, note, and compaction tools before
    submitting ``answer_yes_or_no(decision)``.
    """
    participant = session.get_participant(player_id)
    if not participant:
        raise ValueError(f"Unknown participant: {player_id}")

    prompts_cfg = cfg.get("prompts", {})
    raw_tpl = _get_session_prompt_template(
        session, prompts_cfg.get("participant_system", "prompts/participant.md")
    )

    system_content = _render_template(
        raw_tpl,
        {
            "id": participant.id,
            "seat": participant.seat,
            "name": participant.name,
            "scenario_name": session.scenario_name,
            "persona": participant.persona,
            "tags": "、".join(participant.tags) if participant.tags else "未知",
            "target_channel": dm_channel_id,
        },
    )

    channels_summary, history_text = _build_isolated_history_text(
        session, player_id, max_messages=50
    )
    own_tools_memory = _build_player_own_tool_memory_text(session, player_id)
    seat_board_text = _build_public_seat_board_text(session, player_id)
    docs_list = list_knowledge_docs_summary(cfg)
    docs_str = ", ".join(f"`{d['doc_name']}`({d['title']})" for d in docs_list)
    notes_text = session.get_agent_notes_text(player_id)

    core_body = (
        f"### 当前对局阶段（私聊二元问讯环节）\n`{session.phase_text}`\n\n"
        f"### 场上座位与公开状态看板\n{seat_board_text}\n\n"
        f"### 你自己的私人笔记本（可随时调用 `write_note` 记录推理）\n{notes_text}\n\n"
        f"### 你自己的历史工具调用记忆\n{own_tools_memory}\n\n"
        f"### 可查阅的参考文档（可调用 `read_document` 读取）\n{docs_str}\n\n"
        f"### 你可见的历史频道消息与工具轨迹\n{history_text}\n\n"
    )
    window_status = _build_context_window_status_text(
        session, system_content + core_body + question, player_id
    )

    user_prompt = (
        core_body
        + window_status
        + "\n\n---\n"
        f"### 🔘 主持人私聊二元问讯任务（严禁公开发言！仅对你与主持人可见）\n"
        f"- **专属私聊频道**: `{dm_channel_id}`（其他任何玩家均不可见）\n"
        f"- **主持人问讯问题**: **{question}**\n"
        f"- **二元选项含义**:\n"
        f"  - `decision=\"yes\"` 表示：**{yes_label}**\n"
        f"  - `decision=\"no\"` 表示：**{no_label}**\n\n"
        "💡 **深度思考与工具使用权限**：\n"
        "- 在做出最终二元决定前，你可以进行思考，并可按需调用 `read_document`（查阅参考文档）、`write_note` / `read_notes`（记录或读取私人分析，切勿发给主持人）或 `compact_context`（压缩上下文）。\n"
        "- 完成思考与辅助工具调用后，**必须调用 `answer_yes_or_no(decision)` 工具**（仅传 `\"yes\"` 或 `\"no\"`，无需也不允许附带任何备注文字）！"
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_prompt},
    ]


def build_participant_interrupt_messages(
    session: GameSession,
    cfg: dict[str, Any],
    player_id: str,
    channel_id: str,
    instruction: str = "",
) -> list[dict[str, str]]:
    """
    Construct the isolated OpenAI message list for the Interruption Execution Phase (`request_player_interrupt`),
    entered AFTER the player already answered `yes` in `ask_yes_or_no`.
    """
    participant = session.get_participant(player_id)
    if not participant:
        raise ValueError(f"Unknown participant: {player_id}")

    prompts_cfg = cfg.get("prompts", {})
    raw_tpl = _get_session_prompt_template(
        session, prompts_cfg.get("participant_system", "prompts/participant.md")
    )

    system_content = _render_template(
        raw_tpl,
        {
            "id": participant.id,
            "seat": participant.seat,
            "name": participant.name,
            "scenario_name": session.scenario_name,
            "persona": participant.persona,
            "tags": "、".join(participant.tags) if participant.tags else "未知",
            "target_channel": channel_id,
        },
    )

    channels_summary, history_text = _build_isolated_history_text(
        session, player_id, max_messages=50
    )
    own_tools_memory = _build_player_own_tool_memory_text(session, player_id)
    seat_board_text = _build_public_seat_board_text(session, player_id)
    notes_text = session.get_agent_notes_text(player_id)

    core_body = (
        f"### 当前流程状态（插话执行环节）\n`{session.phase_text}`\n\n"
        f"### 参与者座位与公开状态看板\n{seat_board_text}\n\n"
        f"### 你的私人笔记本\n{notes_text}\n\n"
        f"### 你的历史工具调用记忆\n{own_tools_memory}\n\n"
        f"### 你可见的历史频道消息与工具轨迹\n{history_text}\n\n"
    )
    window_status = _build_context_window_status_text(
        session, system_content + core_body + instruction, player_id
    )

    user_prompt = (
        core_body
        + window_status
        + "\n\n---\n"
        f"### 当前插话任务\n"
        f"- **目标频道**: `{channel_id}`\n"
        f"- **任务指令**: {instruction.strip() or '你已在二元问讯中确认需要插话，请依据当前可见信息完成本次插话。'}\n\n"
        "请调用 `trigger_interrupt(action_text, target_id)` 在目标频道发布插话内容。若任务要求指定目标，请通过 `target_id` 提供对应参与者 ID；无目标时可留空。"
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_prompt},
    ]


def build_participant_planning_messages(
    session: GameSession,
    cfg: dict[str, Any],
    player_id: str,
    phase_topic: str,
    instruction: str = "",
) -> list[dict[str, str]]:
    """Construct an isolated message list for a private planning or review turn.

    The participant may read reference documents, maintain private notes, compact
    context, and submit a plan summary with an immediate focus.
    """
    participant = session.get_participant(player_id)
    if not participant:
        raise ValueError(f"Unknown participant: {player_id}")

    dm_channel_id = f"dm-{player_id}"
    prompts_cfg = cfg.get("prompts", {})
    raw_tpl = _get_session_prompt_template(
        session, prompts_cfg.get("participant_system", "prompts/participant.md")
    )

    system_content = _render_template(
        raw_tpl,
        {
            "id": participant.id,
            "seat": participant.seat,
            "name": participant.name,
            "scenario_name": session.scenario_name,
            "persona": participant.persona,
            "tags": "、".join(participant.tags) if participant.tags else "未知",
            "target_channel": dm_channel_id,
        },
    )

    channels_summary, history_text = _build_isolated_history_text(
        session, player_id, max_messages=50
    )
    own_tools_memory = _build_player_own_tool_memory_text(session, player_id)
    seat_board_text = _build_public_seat_board_text(session, player_id)
    docs_list = list_knowledge_docs_summary(cfg)
    docs_str = ", ".join(f"`{d['doc_name']}`({d['title']})" for d in docs_list)
    notes_text = session.get_agent_notes_text(player_id)

    core_body = (
        f"### 当前流程状态（规划与复盘环节）\n`{session.phase_text}`\n\n"
        f"### 参与者座位与公开状态看板\n{seat_board_text}\n\n"
        f"### 你的私人笔记本（规划记录）\n{notes_text}\n\n"
        f"### 你的历史工具调用记忆\n{own_tools_memory}\n\n"
        f"### 可查阅的参考文档\n{docs_str}\n\n"
        f"### 你可见的历史频道消息与工具轨迹\n{history_text}\n\n"
    )
    window_status = _build_context_window_status_text(
        session, system_content + core_body + instruction, player_id
    )

    user_prompt = (
        core_body
        + window_status
        + "\n\n---\n"
        f"### 【{phase_topic}】规划与复盘任务\n"
        f"- **专用频道**: `{dm_channel_id}`（仅当前频道成员可见）\n"
        f"- **任务提示**: {instruction.strip() or '请根据当前可见信息、任务目标和已有记录，制定或调整后续计划，并明确近期关注重点。'}\n\n"
        "### 行动指南\n"
        "1. 梳理已知信息、不确定性、可选行动及其约束，不要假定未在任务、频道或私人记录中出现的规则。\n"
        "2. 可按需调用 `list_documents` / `read_document(doc_name)` 查阅参考文档。\n"
        "3. 可调用 `write_note(title, content, mode)` 保存详细计划、依据和待核实事项；需要时调用 `compact_context` 压缩早期历史。\n"
        "4. 完成规划后，必须调用 `submit_strategic_plan(plan_summary, current_focus)` 提交计划摘要与近期关注重点。"
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_prompt},
    ]
