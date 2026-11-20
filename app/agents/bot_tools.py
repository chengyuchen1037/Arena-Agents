"""The tool executor of a v0.14 bot (H53): the world tools and ``sleep`` as before, notes and compaction as before, plus

* ``jot(text)``          a clue for the review after the game, appended to the world note ``JOT_NOTE_TITLE`` (one line each), at most
  ``JOT_MAX`` per game and ``JOT_CHARS`` per clue - a clue that is too long is refused, never cut;
* ``read_contract(part)``  the "reference" part of the official contract, whole or one of its small headings.

and *without* ``list_documents`` / ``read_document``: what a bot studies, it studies when it is initialized, not in the middle of
a game.  Everything else is :class:`app.agents.tools.ToolExecutor`; speech is not special here - ``speak`` is a world tool like
the others and its only gate is the output gate of ``app.agents.mind`` (which sends text back, never changes it).
"""

from __future__ import annotations

import json
from typing import Any

from app.agents.contract import read_reference
from app.agents.schemas import (
    BOT_SHARED_TOOL_NAMES,
    JOT_TOOL,
    LEGACY_WAIT_TOOL,
    PLAYER_TOOL_INTENTS,
    READ_CONTRACT_TOOL,
    SLEEP_TOOL,
)
from app.agents.tools import ToolExecutor
from app.bots.types import JOT_CHARS, JOT_MAX, JOT_NOTE_TITLE, ContractDoc
from app.engine.toolargs import ARGS_ERROR_KEY
from app.runtime.ports import ToolOutcome
from app.world.port import WorldPort


def _dump(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


class BotToolExecutor(ToolExecutor):
    """:class:`ToolExecutor` for a bot: its tool names, ``jot`` and ``read_contract``."""

    def __init__(self, world: WorldPort, *, contract: ContractDoc) -> None:
        super().__init__(world, docs_dir=None)
        self.contract = contract

    def tool_names(self, agent_id: str) -> set[str]:
        # ``wait`` (v0.12.0) is still executed, as a sleep without a timer, but is not advertised any more.
        return {*PLAYER_TOOL_INTENTS, SLEEP_TOOL, LEGACY_WAIT_TOOL, *BOT_SHARED_TOOL_NAMES}

    def execute(self, agent_id: str, name: str, args: dict[str, Any], *, gen_id: int, seen_seq: int) -> ToolOutcome:
        usable = isinstance(args, dict) and ARGS_ERROR_KEY not in args and set(args) != {"_raw"}
        if usable and name == JOT_TOOL:
            return self._jot(agent_id, args)
        if usable and name == READ_CONTRACT_TOOL:
            return self._read_contract(args)
        return super().execute(agent_id, name, args, gen_id=gen_id, seen_seq=seen_seq)

    # ----------------------------------------------------------------------------------------------- jot
    def _jots(self, agent_id: str) -> list[str]:
        note = self.world.memory(agent_id).notes.get(JOT_NOTE_TITLE, "")
        return [line for line in str(note).splitlines() if line.strip()]

    def _jot(self, agent_id: str, args: dict[str, Any]) -> ToolOutcome:
        raw = args.get("text")
        if not isinstance(raw, str) or not raw.strip():
            return ToolOutcome(text=_dump(ok=False, error="text 不能为空：写一句留到赛后再想的线索。"), shared=True)
        clue = " ".join(raw.split())  # one line each: the review reads the note line by line
        if len(clue) > JOT_CHARS:
            return ToolOutcome(
                text=_dump(
                    ok=False,
                    error=f"这条线索 {len(clue)} 字，超过了 {JOT_CHARS} 字的上限，没有记下。请缩短后再记（不会替你截断）。",
                    chars=len(clue),
                    limit=JOT_CHARS,
                ),
                shared=True,
            )
        count = len(self._jots(agent_id))
        if count >= JOT_MAX:
            return ToolOutcome(
                text=_dump(ok=False, error=f"本局最多记 {JOT_MAX} 条线索，已经记满，这一条没有记下。", count=count, limit=JOT_MAX),
                shared=True,
            )
        self.world.write_note(agent_id, JOT_NOTE_TITLE, clue, "append")
        count += 1
        return ToolOutcome(
            text=_dump(ok=True, count=count, left=JOT_MAX - count, message=f"已记下第 {count} 条线索，还可以记 {JOT_MAX - count} 条。"),
            shared=True,
        )

    # ------------------------------------------------------------------------------------ read_contract
    def _read_contract(self, args: dict[str, Any]) -> ToolOutcome:
        part = args.get("part")
        ok, text = read_reference(self.contract, None if part is None else str(part))
        if not ok:
            return ToolOutcome(text=_dump(ok=False, error=text), shared=True)
        return ToolOutcome(text=_dump(ok=True, part=(str(part).strip() or "全部") if part is not None else "全部", content=text), shared=True)


__all__ = ["BotToolExecutor"]
