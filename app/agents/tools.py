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
        self._warned: dict[str, set[str]] = {}  # per agent: duties already pointed out in the current run

    # ---------------------------------------------------------------- unfinished business --
    def begin_run(self, agent_id: str) -> None:
        """A new worker run starts: duties may be pointed out (once) again."""
        self._warned.pop(agent_id, None)

    def open_duties(self, agent_id: str) -> list[tuple[str, str]]:
        """What a *player* still owes right now, as ``(key, text)``: unanswered requests and a floor it holds.

        The moderator never owes anything (waiting for answers is its job).  A model that writes a note
        and calls ``wait`` — or answers with nothing at all — would otherwise silently let a request run
        into its deadline (a defaulted answer) or sit on the floor until its time is up.
        """
        if agent_id == MODERATOR_ID:
            return []
        try:
            board = self.world.board(agent_id)
        except Exception:  # noqa: BLE001 - a broken board must not break ``wait``
            return []
        duties: list[tuple[str, str]] = []
        for request in board.get("requests") or []:
            if request.get("answered"):
                continue
            left = request.get("seconds_left")
            if left is not None and left <= 2:
                continue  # closing right now anyway
            when = f"，约剩 {int(left)} 秒" if left is not None else ""
            duties.append(
                (f"request:{request.get('id')}", f"请求 {request.get('id')} 还没有作答{when}（请用 respond 作答，选项见状态板）")
            )
        for channel in board.get("channels") or []:
            floor = channel.get("floor") or {}
            mine = floor.get("mine")
            if not mine or floor.get("paused") or channel.get("frozen") or int(mine.get("messages_left") or 0) <= 0:
                continue
            left = mine.get("seconds_left")
            when = f"，约剩 {int(left)} 秒" if left is not None else ""
            duties.append(
                (
                    f"floor:{channel.get('id')}",
                    f"你仍持有 {channel.get('name') or channel.get('id')}（{channel.get('id')}）的发言权"
                    f"（还可发 {mine.get('messages_left')} 条{when}）：要发言请 speak；不想发言请 pass，"
                    "否则发言权会一直占着，后面的人要等到你的时限结束",
                )
            )
        return duties

    def duties_text(self, agent_id: str) -> str:
        return "；".join(text for _key, text in self.open_duties(agent_id))

    def _wait_warning(self, agent_id: str) -> str | None:
        """The first ``wait`` with unfinished business is answered with a reminder instead (once per duty)."""
        warned = self._warned.setdefault(agent_id, set())
        fresh = [(key, text) for key, text in self.open_duties(agent_id) if key not in warned]
        if not fresh:
            return None
        warned.update(key for key, _text in fresh)
        return "先别结束，你还有没处理完的事项：" + "；".join(text for _key, text in fresh) + "。如果你确实决定不作答 / 不发言，再调用一次 wait 即可。"

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
            reminder = self._wait_warning(agent_id)
            if reminder:
                return ToolOutcome(text=_dump(ok=False, code="NOT_FINISHED", message=reminder), ends_run=False)
            return ToolOutcome(text=_dump(ok=True, message="本次行动结束，等待下一次唤醒。"), ends_run=True)
        if name == "list_documents":
            listing = docs.list_documents(self.docs_dir, include_moderator_docs=agent_id == MODERATOR_ID)
            return ToolOutcome(text=json.dumps(listing, ensure_ascii=False), shared=True)
        if name == "read_document":
            result = docs.read_document(
                self.docs_dir, str(args.get("doc_name", "")), include_moderator_docs=agent_id == MODERATOR_ID
            )
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
