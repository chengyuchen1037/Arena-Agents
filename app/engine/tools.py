from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from app.config import BASE_DIR
from app.engine.state import GameSession
from app.engine.toolargs import safe_int

# =============================================================================
# 1. Shared Knowledge Base & Private Notebook Tools (Moderator + Participants)
# =============================================================================

SHARED_KNOWLEDGE_AND_NOTE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": "列出当前配置的参考文档目录（knowledge_base）中可查阅的文档及标题摘要。",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "读取参考文档目录中指定文档的完整内容。可先调用 list_documents 获取可用文件名。",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_name": {
                        "type": "string",
                        "description": "要读取的文档文件名",
                    }
                },
                "required": ["doc_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_note",
            "description": "在你的私人笔记本中记录或更新信息整理、决策依据、投票记录或后续计划。笔记仅你自己可见。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "笔记条目标题，如 '信息整理' 或 '阶段记录'",
                    },
                    "content": {
                        "type": "string",
                        "description": "笔记正文内容",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "description": "写入模式：overwrite（覆盖同名条目，默认）或 append（追加到同名条目末尾）",
                    },
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_notes",
            "description": "读取你的私人笔记本中的全部历史笔记。",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compact_context",
            "description": (
                "当早期历史占用过多上下文空间时，将其压缩为结构化摘要，"
                "并保留指定数量的最近原始消息，为后续任务释放上下文空间。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary_markdown": {
                        "type": "string",
                        "description": "对早期消息、关键事件、决定、投票与待核实信息的 Markdown 摘要；该摘要将替代被压缩的原始记录。",
                    },
                    "keep_recent_count": {
                        "type": "integer",
                        "description": "压缩后保留的最近原始消息条数（默认为 10，范围 4 ~ 25）。",
                    },
                },
                "required": ["summary_markdown"],
            },
        },
    },
]

# =============================================================================
# 2. Participant Action Tools (Voting & Interruption)
# =============================================================================

PARTICIPANT_VOTE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "cast_vote",
        "description": "正式提交当前投票任务中的选票。普通文本不会计入选票，必须调用本工具完成投票。",
        "parameters": {
            "type": "object",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": "目标参与者 ID；当前任务允许弃票时填 'abstain'",
                },
                "reason": {
                    "type": "string",
                    "description": "内部投票理由（仅记录在工具调用流中，不会作为频道消息广播）",
                },
            },
            "required": ["target_id"],
        },
    },
}

PARTICIPANT_INTERRUPT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "trigger_interrupt",
            "description": (
                "在已获得插话机会并进入插话执行环节后，消耗一次插话机会，"
                "在目标频道发布插话内容；如当前任务涉及目标，可同时提供目标参与者 ID。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action_text": {
                        "type": "string",
                        "description": "要发布到目标频道的插话内容",
                    },
                    "target_id": {
                        "type": "string",
                        "description": "可选：与本次插话相关的目标参与者 ID；无目标时留空",
                    },
                },
                "required": ["action_text"],
            },
        },
    },
]


PARTICIPANT_YES_NO_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "answer_yes_or_no",
        "description": (
            "回答主持人发起的是 / 否问讯。作答前可按需查阅文档、记录笔记或压缩上下文；"
            "最终必须调用本工具提交二元决定，不附加备注或频道发言。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["yes", "no"],
                    "description": "二元决定：'yes' 选择肯定项，'no' 选择否定项；具体含义以当前问题为准",
                },
            },
            "required": ["decision"],
        },
    },
}

PARTICIPANT_SUBMIT_PLAN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_strategic_plan",
        "description": (
            "在规划或复盘回合中提交计划摘要与近期关注重点。"
            "提交前可按需查阅参考文档，并用私人笔记保存详细分析。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "plan_summary": {
                    "type": "string",
                    "description": "当前计划的核心目标、可选路径与应对原则",
                },
                "current_focus": {
                    "type": "string",
                    "description": "接下来最需要关注的信息或行动",
                },
            },
            "required": ["plan_summary", "current_focus"],
        },
    },
}


def get_participant_tools_schema(
    mode: Literal["speak", "vote", "interrupt", "yes_no", "plan"] = "speak",
) -> list[dict[str, Any]]:
    """Return the tool schema list available to a participant in the given turn mode."""
    tools = list(SHARED_KNOWLEDGE_AND_NOTE_TOOLS)
    if mode == "vote":
        tools.append(PARTICIPANT_VOTE_TOOL)
    elif mode == "interrupt":
        tools.extend(PARTICIPANT_INTERRUPT_TOOLS)
    elif mode == "yes_no":
        tools.append(PARTICIPANT_YES_NO_TOOL)
    elif mode == "plan":
        tools.append(PARTICIPANT_SUBMIT_PLAN_TOOL)
    return tools


# =============================================================================
# 3. Moderator Tools Schema
# =============================================================================

MODERATOR_ORCHESTRATION_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "set_phase",
            "description": "更新顶栏显示的对局当前阶段文本。",
            "parameters": {
                "type": "object",
                "properties": {
                    "phase_text": {
                        "type": "string",
                        "description": "新的对局阶段文本",
                    }
                },
                "required": ["phase_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_player_tags",
            "description": "更新单个参与者的完整标签列表（运行时动态赋予身份、状态或徽标标签）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_id": {
                        "type": "string",
                        "description": "参与者 ID",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "该参与者的完整最新标签列表",
                    },
                },
                "required": ["player_id", "tags"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_update_player_tags",
            "description": "批量更新多名参与者的标签列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "updates": {
                        "type": "array",
                        "description": "批量更新项列表，每项包含 player_id 与 tags",
                        "items": {
                            "type": "object",
                            "properties": {
                                "player_id": {"type": "string"},
                                "tags": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["player_id", "tags"],
                        },
                    }
                },
                "required": ["updates"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_dashboard",
            "description": "更新状态面板中的分组数量与标签统计。",
            "parameters": {
                "type": "object",
                "properties": {
                    "factions": {
                        "type": "array",
                        "description": "分组统计列表，每项包含名称、当前数量、总数量与可选展示样式",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "alive": {"type": "integer"},
                                "total": {"type": "integer"},
                                "variant": {
                                    "type": "string",
                                    "enum": [
                                        "danger",
                                        "neutral",
                                        "purple",
                                        "success",
                                        "warning",
                                    ],
                                },
                            },
                            "required": ["name", "alive", "total"],
                        },
                    },
                    "tag_stats": {
                        "type": "array",
                        "description": "标签计数列表，如 [{'name': '...', 'count': 12, 'variant': 'success'}]",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "count": {"type": "integer"},
                                "variant": {
                                    "type": "string",
                                    "enum": [
                                        "danger",
                                        "neutral",
                                        "purple",
                                        "success",
                                        "warning",
                                    ],
                                },
                            },
                            "required": ["name", "count"],
                        },
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_channel",
            "description": "创建公共、群组或私聊频道并指定可见成员。只有频道成员能看到其中的消息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "频道唯一 ID，由当前规则或任务指定",
                    },
                    "name": {
                        "type": "string",
                        "description": "频道显示名称",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["public", "group", "dm"],
                        "description": "频道类型",
                    },
                    "members": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "频道成员 ID 列表（通常包含 'moderator' 以及对应玩家 ID）",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["open", "closed"],
                        "description": "频道初始状态，默认为 open",
                    },
                },
                "required": ["channel_id", "type", "members"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_channel",
            "description": "修改已有频道的开放/关闭状态（open/closed）或增删频道成员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "目标频道 ID",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["open", "closed"],
                        "description": "可选：更新频道状态为 open 或 closed",
                    },
                    "add_members": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选：新增成员 ID 列表",
                    },
                    "remove_members": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选：移除成员 ID 列表",
                    },
                },
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "以主持人（Moderator）身份向指定频道发送系统广播、规则提示或私密反馈。",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "目标频道 ID",
                    },
                    "content": {
                        "type": "string",
                        "description": "主持人发送的消息正文",
                    },
                },
                "required": ["channel_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grant_speaking_turn",
            "description": (
                "将指定频道的当前发言权直接交给一名参与者，不附加公告、复述或指导。"
                "需要发起发言时调用本工具，不要用 send_message 代替。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "发言所在的频道 ID",
                    },
                    "player_id": {
                        "type": "string",
                        "description": "被赋予发言权的参与者 ID",
                    },
                },
                "required": ["channel_id", "player_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_player_speak",
            "description": "将指定频道的发言权交给一名参与者；grant_speaking_turn 的兼容别名。",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "发言所在的频道 ID",
                    },
                    "player_id": {
                        "type": "string",
                        "description": "被调度发言的参与者 ID",
                    },
                },
                "required": ["channel_id", "player_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_yes_or_no",
            "description": (
                "向一名或多名参与者发起私聊是 / 否问讯，并通过 answer_yes_or_no 收集结构化答复。"
                "问讯不会授予频道发言权；yes_label 与 no_label 用于说明两个选项在当前问题中的含义。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "player_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "被问讯的参与者 ID 列表",
                    },
                    "question": {
                        "type": "string",
                        "description": "需要参与者回答的完整二元问题",
                    },
                    "yes_label": {
                        "type": "string",
                        "description": "选择 'yes' 在当前问题中代表的决定",
                    },
                    "no_label": {
                        "type": "string",
                        "description": "选择 'no' 在当前问题中代表的决定",
                    },
                },
                "required": ["player_ids", "question", "yes_label", "no_label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_player_interrupt",
            "description": (
                "为已确认插话的参与者启动插话执行环节，并挂载 trigger_interrupt 工具。"
                "参与者在目标频道提交插话内容；如任务需要，可提供一个目标参与者 ID。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "插话所在的目标频道 ID",
                    },
                    "player_id": {
                        "type": "string",
                        "description": "已通过 ask_yes_or_no 确认插话的参与者 ID",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "可选：本次插话的任务要求与目标约束",
                    },
                },
                "required": ["channel_id", "player_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_players_strategic_planning",
            "description": (
                "调度指定参与者并发进行规划或复盘。每位参与者可查阅参考文档、记录私人笔记、"
                "压缩上下文，并通过 submit_strategic_plan 提交计划摘要与近期关注重点。"
                "规划过程在各自的私密频道中进行，不在其他频道广播。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "player_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "参与本次规划或复盘的参与者 ID 列表",
                    },
                    "phase_topic": {
                        "type": "string",
                        "description": "本次规划或复盘的通用主题",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "可选：补充本次规划的目标、约束或关注重点",
                    },
                },
                "required": ["player_ids", "phase_topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_players_vote",
            "description": (
                "向指定参与者并发发起一轮投票。应先在目标频道公布投票主题、候选范围与相关规则，"
                "再调用本工具；参与者通过 cast_vote 提交选票，由引擎汇总结果。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "投票所属频道 ID",
                    },
                    "voter_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "参与本轮投票的参与者 ID 列表",
                    },
                    "vote_topic": {
                        "type": "string",
                        "description": "可选：投票主题标识（详细公告须先通过 send_message 发布）",
                    },
                    "candidate_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选：合法候选被投票人 ID 列表",
                    },
                    "allow_abstain": {
                        "type": "boolean",
                        "description": "是否允许弃票（默认为 true）",
                    },
                },
                "required": ["channel_id", "voter_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_interrupt_quota",
            "description": "在指定频道为参与者设置插话机会。需要确认是否插话时使用 ask_yes_or_no，确认后用 request_player_interrupt 进入插话执行环节。",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "频道 ID",
                    },
                    "player_id": {
                        "type": "string",
                        "description": "获得插话权的参与者 ID",
                    },
                    "max_count": {
                        "type": "integer",
                        "description": "最大可插话次数，默认为 1",
                    },
                    "reason": {
                        "type": "string",
                        "description": "插话权说明",
                    },
                },
                "required": ["channel_id", "player_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_interrupt_quota",
            "description": "移除某频道中指定参与者（或全部参与者）的插话权。",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "player_id": {
                        "type": "string",
                        "description": "可选，若不填则清空该频道所有插话权",
                    },
                },
                "required": ["channel_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_engine_note",
            "description": "在主持人处理流中记录内部笔记，所有参与者均不可见。",
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "内部处理记录内容",
                    }
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_game",
            "description": "当满足当前任务的终止条件时宣告结束，并发布终局总结。",
            "parameters": {
                "type": "object",
                "properties": {
                    "winner_faction": {
                        "type": "string",
                        "description": "获胜方或分组的名称",
                    },
                    "summary": {
                        "type": "string",
                        "description": "终局复盘总结公告",
                    },
                },
                "required": ["winner_faction", "summary"],
            },
        },
    },
]

MODERATOR_TOOLS_SCHEMA: list[dict[str, Any]] = (
    MODERATOR_ORCHESTRATION_TOOLS + SHARED_KNOWLEDGE_AND_NOTE_TOOLS
)


# =============================================================================
# 4. Shared Tool Execution Helper (Knowledge Docs & Private Notes)
# =============================================================================

def get_knowledge_dir(cfg: dict[str, Any]) -> Path:
    kb_cfg = cfg.get("knowledge_base", {})
    rel_dir = kb_cfg.get("docs_dir", "knowledge")
    # Read-only helper: never create the directory as a side effect of a lookup.
    return (BASE_DIR / rel_dir).resolve()


def list_knowledge_docs_summary(cfg: dict[str, Any]) -> list[dict[str, str]]:
    kb_dir = get_knowledge_dir(cfg)
    docs: list[dict[str, str]] = []
    if not kb_dir.is_dir():
        return docs
    for f in sorted(kb_dir.glob("*.md")):
        first_line = ""
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                s = line.strip().lstrip("#").strip()
                if s:
                    first_line = s
                    break
        except Exception:
            first_line = f.name
        docs.append({"doc_name": f.name, "title": first_line or f.name})
    return docs


def execute_shared_tool(
    actor_id: str,
    tool_name: str,
    args: dict[str, Any],
    session: GameSession,
    cfg: dict[str, Any],
) -> str | None:
    """
    Execute a shared knowledge-base or private-notebook tool.
    Returns JSON result string if tool_name is handled here, else None.
    """
    if tool_name == "list_documents":
        docs = list_knowledge_docs_summary(cfg)
        return json.dumps(
            {
                "ok": True,
                "docs_dir": cfg.get("knowledge_base", {}).get("docs_dir", "knowledge"),
                "documents": docs,
            },
            ensure_ascii=False,
        )

    if tool_name == "read_document":
        doc_name = str(args.get("doc_name", "")).strip()
        if not doc_name:
            return json.dumps({"ok": False, "error": "doc_name 不能为空"}, ensure_ascii=False)
        kb_dir = get_knowledge_dir(cfg)
        target = (kb_dir / Path(doc_name).name).resolve()
        if not target.exists() or not target.is_file():
            available = [d["doc_name"] for d in list_knowledge_docs_summary(cfg)]
            return json.dumps(
                {
                    "ok": False,
                    "error": f"文档 '{doc_name}' 不存在",
                    "available_documents": available,
                },
                ensure_ascii=False,
            )
        content = target.read_text(encoding="utf-8")
        return json.dumps(
            {"ok": True, "doc_name": target.name, "content": content},
            ensure_ascii=False,
        )

    if tool_name == "write_note":
        title = str(args.get("title", "笔记")).strip() or "笔记"
        content = str(args.get("content", "")).strip()
        mode = str(args.get("mode", "overwrite")).strip()
        updated = session.write_agent_note(actor_id, title, content, mode=mode)
        return json.dumps(
            {
                "ok": True,
                "actor_id": actor_id,
                "title": title,
                "mode": mode,
                "current_note": updated,
            },
            ensure_ascii=False,
        )

    if tool_name == "read_notes":
        notes = session.agent_notes.get(actor_id, {})
        return json.dumps(
            {
                "ok": True,
                "actor_id": actor_id,
                "notes": notes,
            },
            ensure_ascii=False,
        )

    if tool_name == "compact_context":
        summary_md = str(args.get("summary_markdown", "")).strip()
        keep_recent = safe_int(args.get("keep_recent_count", 10), 10) or 10
        if not summary_md:
            return json.dumps(
                {"ok": False, "error": "summary_markdown 不能为空，请提供对过往历史的结构化压缩摘要。"},
                ensure_ascii=False,
            )
        entry = session.compact_agent_context(
            actor_id, summary_md, keep_recent_count=keep_recent
        )
        return json.dumps(
            {
                "ok": True,
                "actor_id": actor_id,
                "compaction_count": entry["count"],
                "compacted_at": entry["compacted_at"],
                "keep_recent_count": entry["keep_recent_count"],
                "message": "上下文已成功压缩！早期历史记录已替换为你的高密度摘要。",
            },
            ensure_ascii=False,
        )

    return None
