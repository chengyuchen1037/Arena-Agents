"""``ArenaBrain``: the agent-specific half of a worker run (prompts, tools, context).

v0.12.1: an agent keeps ONE persistent conversation.  ``start_message`` builds the FULL first message
(and remembers the board it showed); ``inject_message`` builds a DELTA (new visible entries, what changed
on the board since the last message, wake reasons), so the conversation is an append-only prefix that
the provider's prompt cache can reuse.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections import deque
from pathlib import Path
from typing import Any

import app.config as app_config
from app.agents.context import Namer, _secs, render_board, render_history, render_memory, render_wake
from app.agents.delta import diff_board
from app.agents.schemas import moderator_tool_schemas, player_tool_schemas
from app.agents.tools import ToolExecutor
from app.runtime.ports import ToolOutcome, WakeContext
from app.world.port import WorldPort
from app.world.spec import ScenarioSpec
from app.world.types import MODERATOR_ID

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_CHARS = {"player": 12_000, "moderator": 20_000}
INJECT_HISTORY_CHARS = 8_000
RECENT_ACTIONS_LIMIT = 12

_FALLBACK_PLAYER = (
    "你是 {{id}} {{name}}（座位 {{seat}}），一名参与者。{{persona}}\n\n{{rules}}\n\n{{memory_block}}\n\n"
    "你会被每一条新消息唤醒；你的对话是连续的，但重要的结论请用 write_note 记下。发言只能通过 speak 工具；"
    "没有事情要做时调用 sleep。"
)
_FALLBACK_MODERATOR = (
    "你是主持人。你负责按规则推进对局：开放发言权、发起请求、裁决、公告。\n\n{{rules}}\n\n{{manual}}\n\n"
    "你会被每一条新消息唤醒；你的对话是连续的，但重要的结论请用 write_note 记下；没有事情要做时调用 sleep。"
)
NO_PERSONA = "（没有特别设定，按你自己的判断行事。）"

NUDGE_TEXT = (
    "（系统提示）你的正文不会发送到任何频道。如果要发言，请调用 speak（或 speak_template）；"
    "如果现在不需要做任何事，请调用 sleep 进入休眠。"
)


class ArenaBrain:
    """Implements :class:`app.runtime.ports.Brain`."""

    def __init__(
        self,
        world: WorldPort,
        spec: ScenarioSpec,
        cfg: dict[str, Any] | None = None,
        *,
        docs_dir: Path | None = None,
        personas: dict[str, str] | None = None,
        memories: dict[str, str] | None = None,
        history_chars: dict[str, int] | None = None,
    ) -> None:
        self.world = world
        self.spec = spec
        self.cfg = cfg or {}
        self.personas = personas or {}
        self.memories = memories or {}  # agent id -> long-term memory text (roster snapshot, may be empty)
        self._baseline: dict[str, dict[str, Any]] = {}  # agent id -> board of the last message of its conversation
        self.history_chars = {**DEFAULT_HISTORY_CHARS, **(history_chars or {})}
        if docs_dir is None:
            relative = (self.cfg.get("knowledge_base") or {}).get("docs_dir", "knowledge")
            docs_dir = Path(app_config.BASE_DIR) / str(relative)
        self.executor = ToolExecutor(world, docs_dir=docs_dir)
        self._tools_cache: dict[str, list[dict[str, Any]]] = {}
        self._prompt_cache: dict[str, str] = {}
        self._nudged: set[str] = set()
        self._recent: dict[str, deque[str]] = {}

    # ------------------------------------------------------------------ prompts / tools --
    def _prompt_template(self, kind: str) -> str:
        prompts = (self.cfg.get("prompts") or {})
        key = "moderator_system" if kind == "moderator" else "participant_system"
        relative = prompts.get(key) or f"prompts/{'moderator' if kind == 'moderator' else 'participant'}.md"
        path = Path(app_config.BASE_DIR) / str(relative)
        try:
            text = path.read_text(encoding="utf-8")
            if text.strip():
                return text
        except OSError:
            logger.warning("Prompt template %s not found; using the built-in fallback", path)
        return _FALLBACK_MODERATOR if kind == "moderator" else _FALLBACK_PLAYER

    def _persona(self, agent_id: str) -> str:
        if agent_id in self.personas:
            return self.personas[agent_id]
        getter = getattr(self.world, "player", None)
        player = getter(agent_id) if callable(getter) else None
        return str(getattr(player, "persona", "") or "")

    def system_prompt(self, agent_id: str) -> str:
        cached = self._prompt_cache.get(agent_id)
        if cached is not None:
            return cached
        kind = "moderator" if agent_id == MODERATOR_ID else "player"
        board = self.world.board(agent_id)
        me = board.get("me") or {}
        memory = (self.memories.get(agent_id) or "").strip()
        values = {
            "name": str(me.get("name") or agent_id),
            "seat": str(me.get("seat") or ""),
            "id": agent_id,
            "persona": self._persona(agent_id).strip() or NO_PERSONA,
            "memory_block": f"# 你的记忆与经验\n\n{memory}\n" if memory and kind != "moderator" else "",
            "memory": memory,
            "rules": self.spec.rules_text.strip(),
            "manual": self.spec.manual_text.strip(),
        }
        text = self._prompt_template(kind)
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        text = re.sub(r"\n{3,}", "\n\n", text)  # an empty {{memory_block}} must not leave a hole
        self._prompt_cache[agent_id] = text
        return text

    def tool_schemas(self, agent_id: str) -> list[dict[str, Any]]:
        cached = self._tools_cache.get(agent_id)
        if cached is None:
            cached = (
                moderator_tool_schemas(self.spec)
                if agent_id == MODERATOR_ID
                else player_tool_schemas(self.spec)
            )
            self._tools_cache[agent_id] = cached
        return cached

    # ------------------------------------------------------------------------- messages --
    def _namer(self, board: dict[str, Any]) -> Namer:
        channels = {str(c.get("id")): str(c.get("name")) for c in (board.get("channels") or [])}
        return Namer(self.world.display_name, channels)

    def _budget(self, agent_id: str) -> int:
        return self.history_chars["moderator" if agent_id == MODERATOR_ID else "player"]

    def _recent_text(self, agent_id: str) -> str:
        actions = self._recent.get(agent_id)
        if not actions:
            return "【我最近的行动】\n（暂无）"
        return "【我最近的行动】\n" + "\n".join(f"- {line}" for line in actions)

    def begin_run(self, agent_id: str) -> None:
        self._nudged.discard(agent_id)
        self.executor.begin_run(agent_id)

    def reset_conversation(self, agent_id: str) -> None:
        self._baseline.pop(agent_id, None)
        self._nudged.discard(agent_id)

    def start_message(self, agent_id: str, ctx: WakeContext) -> tuple[str, int]:
        """FULL message of a new conversation (also the baseline for the deltas that follow)."""
        self._nudged.discard(agent_id)
        self.executor.begin_run(agent_id)
        board = self.world.board(agent_id)
        self._baseline[agent_id] = copy.deepcopy(board)
        memory = self.world.memory(agent_id)
        entries = self.world.entries_visible(agent_id, after_seq=memory.summary_upto_seq)
        namer = self._namer(board)
        history, omitted = render_history(entries, namer, viewer=agent_id, max_chars=self._budget(agent_id))
        hint = (
            "可见历史较长，更早的内容已省略。请把关键信息写进笔记（write_note），必要时调用 compact_context 生成摘要。"
            if omitted
            else ""
        )
        parts = [
            render_board(board, namer, hint=hint),
            render_memory(memory, self.world.read_notes_text(agent_id)),
            self._recent_text(agent_id),
            "【可见历史】\n" + (history or "（还没有可见的历史。）"),
            render_wake(ctx.reasons, namer, agent_id=agent_id),
        ]
        return "\n\n".join(parts), self.world.seq

    def inject_message(self, agent_id: str, ctx: WakeContext) -> tuple[str, int] | None:
        """DELTA message: new visible entries + what changed on the board + why the agent was woken."""
        entries = self.world.entries_visible(agent_id, after_seq=ctx.after_seq)
        board = self.world.board(agent_id)
        namer = self._namer(board)
        history, _ = render_history(entries, namer, viewer=agent_id, max_chars=INJECT_HISTORY_CHARS)
        baseline = self._baseline.get(agent_id)
        if baseline is None:
            # The conversation outlived this brain's memory of it (e.g. a resumed game): one full refresh.
            changes = ["（状态板完整刷新）", render_board(board, namer)]
        else:
            changes = diff_board(baseline, board, namer)
        self._baseline[agent_id] = copy.deepcopy(board)
        if not history and not changes and not ctx.reasons:
            return None
        parts = [f"【游戏时间】{_secs(board.get('now'))}", "【新动态】", history or "（没有新的可见条目。）"]
        if changes:
            parts.append("【状态变化】\n" + "\n".join(changes))
        writable = [
            f"{c.get('id')}（{c.get('mode')}）"
            for c in (board.get("channels") or [])
            if c.get("can_write")
        ]
        if agent_id != MODERATOR_ID:
            parts.append("你当前可以发言的频道：" + ("、".join(writable) if writable else "（无）"))
        parts.append(render_wake(ctx.reasons, namer, agent_id=agent_id))
        return "\n".join(parts), self.world.seq

    def no_tool_nudge(self, agent_id: str, content: str, round_idx: int) -> str | None:
        if agent_id in self._nudged:
            return None
        duties = self.executor.duties_text(agent_id)
        if not (content or "").strip() and not duties:
            return None  # nothing was said and nothing is owed: a quiet end of the run
        self._nudged.add(agent_id)
        return NUDGE_TEXT + (f"\n你还有没处理完的事项：{duties}。" if duties else "")

    # ----------------------------------------------------------------------------- tools --
    def execute_tool(
        self, agent_id: str, name: str, args: dict[str, Any], *, gen_id: int, seen_seq: int
    ) -> ToolOutcome:
        outcome = self.executor.execute(agent_id, name, args, gen_id=gen_id, seen_seq=seen_seq)
        self._remember(agent_id, name, args, outcome)
        return outcome

    def _remember(self, agent_id: str, name: str, args: dict[str, Any], outcome: ToolOutcome) -> None:
        if name in ("sleep", "wait"):
            return
        shown = json.dumps(args, ensure_ascii=False)[:100] if isinstance(args, dict) else str(args)[:100]
        receipt = outcome.receipt
        if receipt is None:
            result = "完成"
        elif receipt.ok:
            result = f"成功（序号 #{receipt.seq}）" if receipt.seq else "成功"
            if receipt.truncated:
                result += f"，被截断 {receipt.truncated.get('dropped_chars', '?')} 字"
        else:
            result = f"被拒绝 {receipt.code}：{receipt.message}"
        actions = self._recent.setdefault(agent_id, deque(maxlen=RECENT_ACTIONS_LIMIT))
        actions.append(f"{name}({shown}) → {result}")
