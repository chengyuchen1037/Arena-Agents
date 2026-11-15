from __future__ import annotations

import json
from typing import Any

from app.engine.context import (
    _build_context_window_status_text,
    _build_isolated_history_text,
    _build_player_own_tool_memory_text,
    _build_public_seat_board_text,
    _get_session_prompt_template,
    _render_template,
)
from app.engine.llm import LLMResponse
from app.engine.participant_loop import ParticipantTurn, run_batch, tool_json
from app.engine.state import GameSession
from app.engine.toolargs import arg_str
from app.engine.tools import SHARED_KNOWLEDGE_AND_NOTE_TOOLS


def build_choice_messages(
    session: GameSession,
    cfg: dict[str, Any],
    player_id: str,
    channel_id: str,
    question: str,
    options: list[dict[str, str]],
) -> list[dict[str, str]]:
    participant = session.get_participant(player_id)
    if participant is None:
        raise ValueError(f"Unknown participant: {player_id}")
    prompts_cfg = cfg.get("prompts", {})
    template = _get_session_prompt_template(
        session, prompts_cfg.get("participant_system", "prompts/participant.md")
    )
    system_content = _render_template(
        template,
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
    channels_summary, history_text = _build_isolated_history_text(session, player_id, max_messages=50)
    own_tools = _build_player_own_tool_memory_text(session, player_id)
    seat_board = _build_public_seat_board_text(session, player_id)
    notes = session.get_agent_notes_text(player_id)
    options_text = "\n".join(f"- {item['id']}: {item['label']}" for item in options)
    core = (
        f"### 当前阶段\n{session.phase_text}\n\n"
        f"### 公开座位状态\n{seat_board}\n\n"
        f"### 你的频道\n{channels_summary}\n\n"
        f"### 你的私人笔记\n{notes}\n\n"
        f"### 你的工具记录\n{own_tools}\n\n"
        f"### 可见历史\n{history_text}\n\n"
    )
    window = _build_context_window_status_text(
        session, system_content + core + question, player_id
    )
    user = (
        core
        + window
        + "\n\n---\n### 结构化选择任务\n"
        + f"问题：{question}\n私聊频道：`{channel_id}`\n合法选项：\n{options_text}\n\n"
        + "可以先使用笔记、文档或上下文压缩工具。最终必须调用 `submit_choice(option_id)`，"
        + "`option_id` 只能使用上述合法选项 ID；普通文本不视为已提交。"
    )
    return [{"role": "system", "content": system_content}, {"role": "user", "content": user}]


def _choice_tool(options: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "submit_choice",
            "description": "提交一次结构化选择；option_id 必须属于本题候选。",
            "parameters": {
                "type": "object",
                "properties": {
                    "option_id": {
                        "type": "string",
                        "enum": [item["id"] for item in options],
                    }
                },
                "required": ["option_id"],
            },
        },
    }


class ChoiceTurn(ParticipantTurn):
    """A private structured choice that must end in ``submit_choice(option_id)``."""

    budget_kind = "decision"
    action_tag = "choice"
    action_type = "choice"
    done_event = "choice_item_done"
    stage_thought = None  # the caller already shows "结构化选择中"
    broadcast_thoughts = False

    def __init__(
        self,
        orch: Any,
        player_id: str,
        channel_id: str,
        question: str,
        options: list[dict[str, str]],
        messages: list[dict[str, str]],
        **kw: Any,
    ) -> None:
        super().__init__(
            orch,
            player_id,
            channel_id,
            messages,
            [*SHARED_KNOWLEDGE_AND_NOTE_TOOLS, _choice_tool(options)],
            **kw,
        )
        self.question = question
        self.by_id = {item["id"]: item["label"] for item in options}
        self.selected: str | None = None

    async def handle_tool(self, name: str, args: dict[str, Any]) -> str | None:
        if name != "submit_choice":
            return None
        if self.selected is not None:
            return tool_json(ok=False, error="已提交选择，重复调用被忽略")
        candidate = arg_str(args, "option_id")
        if candidate not in self.by_id:
            return tool_json(ok=False, error="option_id 不在合法候选中", valid_options=list(self.by_id))
        self.selected = candidate
        self.finished = True
        return tool_json(ok=True, player_id=self.pid, option_id=candidate)

    async def on_no_tool_calls(
        self, resp: LLMResponse, round_idx: int, is_last: bool
    ) -> tuple[str, str] | None:
        if is_last:
            return None
        return (
            resp.content.strip() or "（未提交选择）",
            "请立即调用 submit_choice(option_id) 提交合法选项。",
        )

    async def finalize(self) -> None:
        label = self.by_id.get(self.selected or "", "未提交有效选择")
        self.emit_record(
            stream="channel",
            channel_id=self.channel_id,
            kind="message",
            actor_id=self.pid,
            content=f"结构化选择：{label}",
        )

    def degrade(self, exc: BaseException) -> None:
        self.selected = None
        self.emit_record(
            stream="channel",
            channel_id=self.channel_id,
            kind="message",
            actor_id=self.pid,
            content="结构化选择：未提交有效选择",
        )

    def detail(self) -> dict[str, Any]:
        return {
            "player_id": self.pid,
            "channel_id": self.channel_id,
            "question": self.question,
            "option_id": self.selected,
            "label": self.by_id.get(self.selected or ""),
            "submitted": self.selected is not None,
        }


def _parse_options(player_id: str, raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        raise ValueError(f"Choices for {player_id} require a list of options")
    options: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("id") in (None, ""):
            raise ValueError(f"Choices for {player_id} require options with an id")
        options.append({"id": str(item["id"]), "label": str(item.get("label", item["id"]))})
    if not options or len({item["id"] for item in options}) != len(options):
        raise ValueError(f"Choices for {player_id} require unique non-empty option ids")
    return options


async def execute_choice_requests(orchestrator: Any, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    requests = [item for item in (args.get("requests") or []) if isinstance(item, dict)]
    if not requests:
        raise ValueError("request_choices.requests cannot be empty")
    session = orchestrator.session

    # Validate every request before touching any state (no half-started batches).
    seen: set[str] = set()
    plan: list[tuple[str, str, list[dict[str, str]]]] = []
    for request in requests:
        player_id = arg_str(request, "player_id")
        if not player_id or player_id in seen:
            raise ValueError(f"Duplicate or empty choice actor: {player_id!r}")
        seen.add(player_id)
        if session.get_participant(player_id) is None:
            raise ValueError(f"Unknown choice actor: {player_id}")
        plan.append(
            (
                player_id,
                arg_str(request, "question", "请做出选择"),
                _parse_options(player_id, request.get("options") or []),
            )
        )

    turns: list[ChoiceTurn] = []
    for player_id, question, options in plan:
        channel_id = orchestrator._ensure_player_dm_channel(player_id)
        messages = build_choice_messages(
            session, orchestrator.cfg, player_id, channel_id, question, options
        )
        turns.append(
            ChoiceTurn(
                orchestrator, player_id, channel_id, question, options, messages, defer_records=True
            )
        )
    for turn in turns:
        session.set_participant_stage(turn.pid, True, "结构化选择中")
    await orchestrator.broadcast_state("choice_start")

    await run_batch(orchestrator, turns, label="结构化选择")

    details = [turn.detail() for turn in turns]
    decisions = {item["player_id"]: item["option_id"] for item in details}
    data = {"decisions": decisions, "details": details}
    orchestrator._log_tool(
        "request_choices",
        args,
        json.dumps(data, ensure_ascii=False),
        turns[0].channel_id if len(turns) == 1 else None,
    )
    await orchestrator.broadcast_state("choice_done")
    return json.dumps(data, ensure_ascii=False), data
