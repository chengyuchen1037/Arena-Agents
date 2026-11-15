"""Legacy free-form LLM-moderator loop (sessions imported without a scenario snapshot).

New games are driven by the declarative ``ScenarioDirector``; this path only runs for
v0.7-style saves.  It is kept as a mixin so ``orchestrator.py`` stays small.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from app.engine.context import build_moderator_system_prompt
from app.engine.history import (
    complete_interrupted_tool_calls,
    normalize_tool_history,
    trim_moderator_history,
)
from app.engine.participant_loop import assistant_tool_message
from app.engine.tools import MODERATOR_TOOLS_SCHEMA

logger = logging.getLogger(__name__)

_NO_TOOL_NUDGE = (
    "请务必调用工具（如 `set_phase`、`create_channel`、`batch_update_player_tags`、"
    "`send_message`、`request_player_speak` 等）来实际推进对局；如果游戏已经分出胜负，请调用 `end_game`。"
)


def get_safe_moderator_history(
    messages: list[dict[str, Any]],
    max_messages: int = 80,
) -> list[dict[str, Any]]:
    """
    Return a suffix of moderator_messages that NEVER starts with `role == 'tool'`.
    Ensures every `role: 'tool'` message is preceded by its parent `assistant`
    message containing `tool_calls`.
    """
    if len(messages) <= max_messages:
        return list(messages)

    start_idx = len(messages) - max_messages
    while start_idx < len(messages) and messages[start_idx].get("role") == "tool":
        start_idx += 1

    sliced = list(messages[start_idx:])
    if messages and messages[0].get("role") == "user" and (
        not sliced or sliced[0].get("role") != "user"
    ):
        return [messages[0], *sliced]
    return sliced


class LegacyModeratorMixin:
    """Methods of ``GameOrchestrator`` that implement the LLM-moderator loop."""

    async def _run_loop(self) -> None:
        """Main autonomous orchestration loop driven by Moderator Agent tool calls."""

        async def body() -> None:
            if not self.session.moderator_messages:
                prompts_cfg = self.cfg.get("prompts", {})
                if self.session.records:
                    kickoff = str(
                        prompts_cfg.get(
                            "resume_instruction",
                            f"当前处于『{self.session.phase_text}』。请结合最新频道消息记录，立即调用工具继续主持推进进程。",
                        )
                    ).strip()
                else:
                    kickoff = str(
                        prompts_cfg.get(
                            "kickoff_instruction",
                            "对局正式开始！当前频道列表、玩家UI标签与右侧面板均为空。请先阅读游戏规则与工具列表，在第一轮通过工具完成：1. 设置阶段；2. 为全员赋予初始身份与存活标签；3. 初始化右侧面板统计；4. 创建 public 及所需阵营/私聊频道并发送开局信息，然后逐步推进游戏。",
                        )
                    ).strip()
                self.session.moderator_messages = [{"role": "user", "content": kickoff}]

            while not self._pause_requested and self.session.status == "running":
                await self._moderator_step()
                if self._pause_requested or self.session.status != "running":
                    break
                await asyncio.sleep(0.15)

        await self._guarded_loop(body, "Orchestrator loop error")

    async def _moderator_step(self) -> None:
        """Execute one reasoning + tool-calling cycle for the Moderator Agent."""
        session = self.session
        session.moderator_messages, history_report = normalize_tool_history(
            session.moderator_messages
        )
        if any(history_report.values()):
            logger.warning("Repaired moderator tool history: %s", history_report)
        session.moderator_messages = trim_moderator_history(session.moderator_messages)
        safe_history = get_safe_moderator_history(session.moderator_messages, max_messages=80)
        sys_prompt = build_moderator_system_prompt(
            session, self.cfg, history_blob=json.dumps(safe_history, ensure_ascii=False)
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": sys_prompt}, *safe_history]

        session.set_generating("moderator", "主持人 · 思维链推理中")
        session.set_moderator_state(True, "🧠 思维链推理与编排规划中...")

        action_id = f"act_mod_{uuid.uuid4().hex[:8]}"
        session.active_action_id = action_id
        session.active_action_type = "moderator"
        session.update_streaming_draft(
            actor_id="moderator",
            actor_name="主持人",
            channel_id=None,
            action_id=action_id,
            action_type="moderator",
        )
        await self.broadcast_state("step_start")

        async def on_mod_chunk(thought: str, content: str) -> None:
            session.update_streaming_draft(
                actor_id="moderator",
                actor_name="主持人",
                channel_id=None,
                thought_text=thought,
                content_text=content,
                action_id=action_id,
                action_type="moderator",
            )
            if content:
                session.set_moderator_state(True, "✍️ 生成编排指令中...")
            elif thought:
                session.set_moderator_state(True, "🧠 深度思考与规则推演中...")
            await self._throttled_stream_broadcast()

        try:
            resp = await self.llm.chat_stream(
                messages,
                is_moderator=True,
                tools=MODERATOR_TOOLS_SCHEMA,
                on_chunk=on_mod_chunk,
                binding=session.get_model_binding("moderator"),
            )
            session.record_response_usage("moderator", resp)
            session.clear_streaming_draft("moderator")

            if resp.thought:
                session.add_record(
                    stream="engine",
                    kind="thought",
                    actor_id="moderator",
                    content=resp.thought,
                    action_id=action_id,
                    action_type="moderator",
                )

            if not resp.tool_calls:
                if resp.content:
                    session.add_record(
                        stream="engine",
                        kind="note",
                        actor_id="moderator",
                        content=resp.content,
                        action_id=action_id,
                        action_type="moderator",
                    )
                session.moderator_messages.append(
                    {"role": "assistant", "content": resp.content or "（无工具调用）"}
                )
                session.moderator_messages.append({"role": "user", "content": _NO_TOOL_NUDGE})
                await self.broadcast_state("step_done")
                return

            session.moderator_messages.append(assistant_tool_message(resp))

            interrupted_event: str | None = None
            for idx, tc in enumerate(resp.tool_calls, start=1):
                if interrupted_event:
                    session.moderator_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": f"[已跳过] 因需优先处理插话问讯或插话事件（{interrupted_event}），该工具调用已延后。",
                        }
                    )
                    continue

                session.set_moderator_state(
                    True, f"🛠️ 调用工具 ({idx}/{len(resp.tool_calls)}): {tc.name}"
                )
                session.active_status_text = f"主持人 · 执行 {tc.name}"
                await self.broadcast_state("tool_start")

                try:
                    tool_result, interrupt_msg = await self._execute_moderator_tool(tc)
                except asyncio.CancelledError:
                    session.moderator_messages = complete_interrupted_tool_calls(
                        session.moderator_messages,
                        resp.tool_calls,
                        idx,
                        "[工具执行已取消] 当前及后续调用未执行，请在恢复后重新决策。",
                    )
                    raise
                except Exception as exc:
                    session.moderator_messages = complete_interrupted_tool_calls(
                        session.moderator_messages,
                        resp.tool_calls,
                        idx,
                        f"[工具执行异常] {type(exc).__name__}: {exc}",
                    )
                    raise
                session.moderator_messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": tool_result}
                )
                if interrupt_msg:
                    interrupted_event = interrupt_msg

                # Remaining tool_calls of this assistant turn still need placeholder results
                # so the OpenAI tool_call_id contract is never broken.
                if self._pause_requested:
                    skipped = "[因用户请求暂停，该工具调用已延后，请在恢复后重新决策]"
                elif session.status != "running":
                    skipped = "[对局已结束，工具调用跳过]"
                else:
                    continue
                for rem_tc in resp.tool_calls[idx:]:
                    session.moderator_messages.append(
                        {"role": "tool", "tool_call_id": rem_tc.id, "content": skipped}
                    )
                break

            await self.broadcast_state("step_done")
        finally:
            session.clear_streaming_draft("moderator")
            session.active_action_id = None
            session.active_action_type = None
