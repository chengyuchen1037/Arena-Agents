"""Tool execution: world tools become commit-gate intents, shared tools touch notes / documents."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from app.agents import docs
from app.agents.schemas import MODERATOR_TOOL_INTENTS, PLAYER_TOOL_INTENTS, WAIT_TOOL
from app.engine.toolargs import ARGS_ERROR_KEY, args_error_result
from app.runtime.ports import ToolOutcome
from app.world.port import WorldPort
from app.world.types import MODERATOR_ID, Code, Intent, Receipt

MAX_NOTE_CHARS = 4000


def _dump(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


def _rejected(code: str, message: str) -> ToolOutcome:
    receipt = Receipt.reject(code, message)
    return ToolOutcome(text=receipt.tool_json(), receipt=receipt)


class ToolExecutor:
    def __init__(self, world: WorldPort, *, docs_dir: Path | None = None) -> None:
        self.world = world
        self.docs_dir = docs_dir

    def tool_names(self, agent_id: str) -> set[str]:
        world_tools = MODERATOR_TOOL_INTENTS if agent_id == MODERATOR_ID else PLAYER_TOOL_INTENTS
        return {*world_tools, WAIT_TOOL, "list_documents", "read_document", "write_note", "read_notes", "compact_context"}

    def execute(
        self, agent_id: str, name: str, args: dict[str, Any], *, gen_id: int, seen_seq: int
    ) -> ToolOutcome:
        if not isinstance(args, dict):
            return _rejected(Code.INVALID_PARAMS, f"工具 {name} 的参数必须是 JSON 对象。")
        if ARGS_ERROR_KEY in args or set(args) == {"_raw"}:
            reason = str(args.get(ARGS_ERROR_KEY) or "工具参数不是合法的 JSON 对象")
            receipt = Receipt.reject(Code.INVALID_PARAMS, json.loads(args_error_result(name, reason))["error"])
            return ToolOutcome(text=receipt.tool_json(), receipt=receipt)
        if name not in self.tool_names(agent_id):
            return _rejected(Code.INVALID_PARAMS, f"未知工具：{name}。可用工具见工具列表。")

        if name == WAIT_TOOL:
            return ToolOutcome(text=_dump(ok=True, message="本次行动结束，等待下一次唤醒。"), ends_run=True)
        if name == "list_documents":
            return ToolOutcome(text=json.dumps(docs.list_documents(self.docs_dir), ensure_ascii=False), shared=True)
        if name == "read_document":
            result = docs.read_document(self.docs_dir, str(args.get("doc_name", "")))
            return ToolOutcome(text=json.dumps(result, ensure_ascii=False), shared=True)
        if name == "write_note":
            return self._write_note(agent_id, args)
        if name == "read_notes":
            return ToolOutcome(text=_dump(ok=True, notes=self.world.read_notes_text(agent_id)), shared=True)
        if name == "compact_context":
            return self._compact(agent_id, args)

        intent_type = (
            MODERATOR_TOOL_INTENTS[name] if agent_id == MODERATOR_ID else PLAYER_TOOL_INTENTS[name]
        )
        intent = Intent(
            intent_id=uuid.uuid4().hex[:10],
            actor_id=agent_id,
            gen_id=gen_id,
            based_on_seq=seen_seq,
            type=intent_type,
            payload=args,
        )
        receipt = self.world.submit(intent)
        return ToolOutcome(text=receipt.tool_json(), receipt=receipt)

    # ------------------------------------------------------------------ shared tools --
    def _write_note(self, agent_id: str, args: dict[str, Any]) -> ToolOutcome:
        title = str(args.get("title") or "笔记").strip()[:60] or "笔记"
        content = str(args.get("content") or "").strip()
        if not content:
            return ToolOutcome(text=_dump(ok=False, error="content 不能为空"), shared=True)
        mode = "append" if str(args.get("mode") or "overwrite").strip() == "append" else "overwrite"
        clipped = len(content) > MAX_NOTE_CHARS
        updated = self.world.write_note(agent_id, title, content[:MAX_NOTE_CHARS], mode)
        extra = {"truncated": True} if clipped else {}
        return ToolOutcome(text=_dump(ok=True, title=title, mode=mode, current_note=updated, **extra), shared=True)

    def _compact(self, agent_id: str, args: dict[str, Any]) -> ToolOutcome:
        summary = str(args.get("summary_markdown") or "").strip()
        if not summary:
            return ToolOutcome(
                text=_dump(ok=False, error="summary_markdown 不能为空，请提供对早期历史的结构化摘要。"), shared=True
            )
        try:
            keep = int(args.get("keep_recent_count", 10))
        except (TypeError, ValueError):
            keep = 10
        keep = max(4, min(25, keep))
        entry = self.world.compact(agent_id, summary, keep)
        return ToolOutcome(
            text=_dump(ok=True, compactions=entry.get("compactions"), message="早期历史已替换为你的摘要。"),
            rebuild=True,
            shared=True,
        )
