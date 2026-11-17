"""A minimal in-memory ``WorldPort`` for testing the agent layer in isolation."""

from __future__ import annotations

from typing import Any

from app.world.spec import ScenarioSpec
from app.world.types import AgentMemory, Intent, LedgerEntry, Receipt, ENGINE_ID, MODERATOR_ID


class FakeClock:
    def now(self) -> float:
        return 12.0


def make_spec(**overrides: Any) -> ScenarioSpec:
    data: dict[str, Any] = {
        "id": "fake",
        "version": "1",
        "setup": {"roles": {"a": {"faction": "x"}, "b": {"faction": "y"}}},
        "templates": {
            "death": {"text": "{{players}} 出局。", "params": {"players": "player_ids"}, "users": "moderator", "sensitive": True},
            "vote": {"text": "我投 {{target}}", "params": {"target": "player_id", "reason": "text(40)?"}, "users": "players"},
        },
        "abilities": {"boom": {"holders": "${[]}", "params": {"target": "player_id"}, "description": "自爆能力"}},
        "checks": {"over": "${False}"},
        "rules_text": "规则正文",
        "manual_text": "手册正文",
    }
    data.update(overrides)
    return ScenarioSpec.model_validate(data)


class FakeWorld:
    def __init__(self, spec: ScenarioSpec | None = None) -> None:
        self.spec = spec or make_spec()
        self.clock = FakeClock()
        self.ledger: list[LedgerEntry] = []
        self.submitted: list[Intent] = []
        self.next_receipt: Receipt = Receipt.accept(1)
        self.boards: dict[str, dict[str, Any]] = {}
        self._memory: dict[str, AgentMemory] = {}
        self._notes: dict[str, dict[str, str]] = {}
        self.names = {"p1": "Alex", "p2": "Blair", MODERATOR_ID: "主持人", ENGINE_ID: "系统"}
        self.running = True

    @property
    def seq(self) -> int:
        return self.ledger[-1].seq if self.ledger else 0

    def add(self, type_: str, actor: str, data: dict[str, Any] | None = None, *, channel: str | None = None,
            visible: list[str] | None = None) -> LedgerEntry:
        entry = LedgerEntry(
            seq=self.seq + 1, ts="t", type=type_, actor_id=actor, channel_id=channel,
            visible_to_ids=visible if visible is not None else [MODERATOR_ID, "p1", "p2"], data=data or {},
        )
        self.ledger.append(entry)
        return entry

    # -- WorldPort subset ------------------------------------------------------
    def submit(self, intent: Intent) -> Receipt:
        self.submitted.append(intent)
        return self.next_receipt

    def entries_visible(self, agent_id: str, *, after_seq: int = 0, limit: int | None = None) -> list[LedgerEntry]:
        out = [e for e in self.ledger if e.seq > after_seq and agent_id in e.visible_to_ids]
        return out[-limit:] if limit else out

    def board(self, agent_id: str) -> dict[str, Any]:
        if agent_id in self.boards:
            return self.boards[agent_id]
        is_mod = agent_id == MODERATOR_ID
        return {
            "me": {"id": agent_id, "name": self.names.get(agent_id, agent_id), "seat": "S1", "tags": ["存活"],
                   "attributes": {}, "brief": "你是甲方。", "role": "a", "faction": "x", "is_moderator": is_mod},
            "phase": "测试阶段", "status": "running", "now": 12.0,
            "players": [{"id": "p1", "seat": "S1", "name": "Alex", "tags": ["存活"], "out": False},
                        {"id": "p2", "seat": "S2", "name": "Blair", "tags": ["存活"], "out": False}],
            "channels": [{"id": "pub", "name": "公共频道", "type": "public", "mode": "closed", "members": [MODERATOR_ID, "p1", "p2"],
                          "readonly": [], "frozen": False, "system": False, "can_write": False,
                          "why_not": "NO_FLOOR：频道已关闭", "floor": {"kind": "none"}}],
            "requests": [], "abilities": [], "tickets": [], "dashboard": {"factions": [], "tag_stats": []},
        }

    def display_name(self, agent_id: str) -> str:
        return self.names.get(agent_id, agent_id)

    def memory(self, agent_id: str) -> AgentMemory:
        return self._memory.setdefault(agent_id, AgentMemory())

    def write_note(self, agent_id: str, title: str, content: str, mode: str = "overwrite") -> str:
        notes = self._notes.setdefault(agent_id, {})
        notes[title] = f"{notes[title]}\n{content}" if mode == "append" and title in notes else content
        self.memory(agent_id).notes = dict(notes)
        return notes[title]

    def read_notes_text(self, agent_id: str) -> str:
        notes = self._notes.get(agent_id) or {}
        if not notes:
            return "（笔记为空）"
        return "\n".join(f"#### 📌 {k}\n{v}" for k, v in notes.items())

    def compact(self, agent_id: str, summary: str, keep_recent_count: int = 10) -> dict[str, Any]:
        mem = self.memory(agent_id)
        mem.summary, mem.compactions = summary, mem.compactions + 1
        visible = self.entries_visible(agent_id)
        mem.summary_upto_seq = visible[-keep_recent_count].seq - 1 if len(visible) > keep_recent_count else mem.summary_upto_seq
        return {"compactions": mem.compactions, "keep": keep_recent_count}
