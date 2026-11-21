"""The ports of the learning runs: what the tools of a learning brain really do (v0.14, H20, H21, H32, H34; contract §13.6).

``app.agents.learning_brain`` turns the tool calls of a model into calls of a :class:`~app.bots.types.LearningPort`; the ports here are
that interface over the bot store and - for a review - the record of the game that is over.  The tools never run a model and never touch
a file themselves.

* :class:`InitPort` - an **initialization is all or nothing** (C16).  Every write is checked at once (so the model is told what is wrong
  and rewrites), but only *staged* in memory; the runner commits the staged files together when the run has ended and everything required
  is there (``BotStore.commit_init``), else nothing on disk changes.  Reading ``my_memory`` gives what the bot already has - and what it has
  staged in this run, so a revision sees its own edits;
* :class:`ReviewPort` - a **review writes as it goes**: every write is checked and written atomically at once and stays when the clock runs
  out (``BotStore.write_part``); a change of the L0 is only ever a *proposal* for the user (``BotStore.stage_core``, H34); ``read_record`` pages
  through what the bot could see in the game.

Both read the same sources (``read_source``): the contract (the part that is always read, the reference), the scenario's rules for players
(never the moderator's manual), the study materials, the bot's own L0 and memory.  Nothing under ``bench/`` is reachable from here (H50).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from app.agents.context import BOT, Namer, render_entry
from app.agents.learning_brain import FILE_LABELS, INIT_FILES, INIT_REQUIRED
from app.bots.types import (
    CORE_HEADINGS,
    MEMORY_PARTS,
    BotMind,
    ContractDoc,
    CoreFiles,
    WriteResult,
)
from app.storage import botfiles
from app.storage.botfiles import BotFileError

logger = logging.getLogger(__name__)

__all__ = ["InitPort", "ReviewPort", "render_core", "render_record", "RECORD_MAX_CHARS"]

#: One ``read_record`` answer is at most this long (the model asks again from where it stopped).
RECORD_MAX_CHARS = 12_000


def render_core(core: CoreFiles) -> str:
    """The L0 as the bot reads it back (``read_source("my_core")``): its sections, each under its heading."""
    parts = [f"# {CORE_HEADINGS[name]}\n{getattr(core, name).strip()}" for name in ("identity", "voice", "mind") if getattr(core, name).strip()]
    parts += [f"# {title.strip()}\n{text.strip()}" for title, text in core.extras if text.strip()]
    return "\n\n".join(parts)


class _SourcePort:
    """``read_source`` of both ports (the sources do not depend on what kind of run it is)."""

    def __init__(
        self,
        *,
        mind: BotMind,
        contract: ContractDoc,
        rules_text: str,
        situations: str,
        materials: Mapping[str, str],
        stored: Callable[[str], str],
    ) -> None:
        self.mind = mind
        self.contract = contract
        self.rules_text = rules_text
        self.situations = situations
        self.materials = dict(materials)
        self._stored = stored  # part -> the text as it is stored now

    def _memory(self, part: str) -> str:
        return self._stored(part)

    def read_source(self, source: str, part: str | None = None) -> str:
        if source == "contract":
            if part == "must_read":
                return self.contract.must_read.strip()
            if part == "reference":
                return self.contract.reference.strip()
            return self.contract.markdown.strip()
        if source == "rules":
            text = self.rules_text.strip()
            return text + (f"\n\n{self.situations}" if self.situations else "")
        if source == "materials":
            if part is None:
                if not self.materials:
                    return ""
                return "\n".join(f"- {slug}（{len(text)} 字）" for slug, text in sorted(self.materials.items()))
            if part in self.materials:
                return self.materials[part].strip()
            available = "、".join(sorted(self.materials)) or "没有任何学习材料"
            return f"没有叫「{part}」的学习材料。可读的：{available}。"
        if source == "my_core":
            return render_core(self.mind.core)
        if source == "my_memory":
            parts = (part,) if part else self._memory_parts()
            if len(parts) == 1:
                return self._memory(parts[0]).strip()
            blocks = [f"## {FILE_LABELS[name]}\n{self._memory(name).strip() or '（空）'}" for name in parts]
            return "\n\n".join(blocks)
        return ""

    def _memory_parts(self) -> tuple[str, ...]:
        return INIT_FILES


class InitPort(_SourcePort):
    """The port of an initialization: writes are staged in memory and committed together at the end - or not at all (C16, H21).

    ``stored(part)`` reads the file as it is on disk now.  ``known_tags``: the situation tags of the scenario (a playbook entry with another
    tag is staged all the same, and the answer says it will not be found by it)."""

    def __init__(
        self,
        *,
        mind: BotMind,
        contract: ContractDoc,
        rules_text: str,
        situations: str,
        materials: Mapping[str, str],
        stored: Callable[[str], str],
        known_tags: Iterable[str] = (),
    ) -> None:
        super().__init__(mind=mind, contract=contract, rules_text=rules_text, situations=situations, materials=materials, stored=stored)
        self.known_tags = tuple(known_tags)
        self.staged: dict[str, str] = {}

    def _memory(self, part: str) -> str:
        return self.staged.get(part) if part in self.staged else self._stored(part)  # type: ignore[return-value]

    def write_memory(self, file: str, content: str) -> WriteResult:
        if file not in INIT_FILES:
            return WriteResult(False, f"初始化时只能写：{'、'.join(INIT_FILES)}", file=file)
        try:
            text, note = botfiles.normalize_part(file, content, known_tags=self.known_tags)
        except BotFileError as exc:
            return WriteResult(False, str(exc), file=file)
        if not text:
            return WriteResult(False, "内容是空的", file=file)
        self.staged[file] = text  # (replaces what was staged before: the whole file is written each time)
        return WriteResult(True, note, file=file, chars=len(text))

    def propose_core(self, section: str, content: str, reason: str) -> WriteResult:
        return WriteResult(False, "初始化时不能提出底层设定的修改（它只在赛后复盘里提）", file=section)

    def read_record(self, query: str | None = None, start: int | None = None, limit: int = 40) -> str:
        return "初始化时没有对局记录。"

    # ------------------------------------------------------------------------------------------------ the end of the run
    @property
    def missing(self) -> list[str]:
        """The required files that have not been written."""
        return [name for name in INIT_REQUIRED if name not in self.staged]

    @property
    def written(self) -> list[str]:
        return [name for name in INIT_FILES if name in self.staged]


class ReviewPort(_SourcePort):
    """The port of a review: every write is checked and made at once and stays; what the bot wants to change of itself is a proposal.

    ``write_part(part, text) -> WriteResult`` and ``stage_core(section, text, reason) -> id`` are the bot store's (the runner binds the bot and
    the scenario); ``entries()`` are the records of the game the bot could see (``read_record`` pages through them)."""

    def __init__(
        self,
        *,
        mind: BotMind,
        contract: ContractDoc,
        rules_text: str,
        situations: str,
        materials: Mapping[str, str],
        stored: Callable[[str], str],
        write_part: Callable[[str, str], WriteResult],
        stage_core: Callable[[str, str, str], str],
        entries: Callable[[], list[Any]],
        namer: Callable[[], Namer],
        viewer: str,
    ) -> None:
        super().__init__(mind=mind, contract=contract, rules_text=rules_text, situations=situations, materials=materials, stored=stored)
        self._write_part = write_part
        self._stage_core = stage_core
        self._entries = entries
        self._namer = namer
        self.viewer = viewer
        self._changes: dict[str, dict[str, str]] = {}
        self.proposals: list[str] = []  # the sections the bot proposed a change of

    def _memory_parts(self) -> tuple[str, ...]:
        return MEMORY_PARTS

    def write_memory(self, file: str, content: str) -> WriteResult:
        before = self._changes[file]["before"] if file in self._changes else self._stored(file)
        result = self._write_part(file, content)
        if result.ok:
            self._changes[file] = {"file": file, "before": before, "after": self._stored(file)}
        return result

    def propose_core(self, section: str, content: str, reason: str) -> WriteResult:
        try:
            self._stage_core(section, content, reason)
        except BotFileError as exc:
            return WriteResult(False, str(exc), file=section)
        except Exception as exc:  # noqa: BLE001 - a bot that is gone, a store that cannot write: the model is told, the run goes on
            logger.warning("A proposal of a review could not be staged", exc_info=True)
            return WriteResult(False, f"没有保存（{type(exc).__name__}）", file=section)
        self.proposals.append(section)
        return WriteResult(True, "", file=section)

    def read_record(self, query: str | None = None, start: int | None = None, limit: int = 40) -> str:
        return render_record(self._entries(), self._namer(), viewer=self.viewer, query=query, start=start, limit=limit)

    # ------------------------------------------------------------------------------------------------ what the review changed
    def changes(self) -> dict[str, Any]:
        """``review.progress.changes`` (§13.8): ``{"files": [{"file", "before", "after"}], "proposals": n}`` - a file that ends up as it
        was is not a change."""
        files = [dict(change) for change in self._changes.values() if change["before"].strip() != change["after"].strip()]
        return {"files": files, "proposals": len(self.proposals)}

    @property
    def written(self) -> list[str]:
        return list(self._changes)


def render_record(
    entries: list[Any], namer: Namer, *, viewer: str, query: str | None = None, start: int | None = None, limit: int = 40
) -> str:
    """The records of the game a bot could see, one line each (``#<seq> ...``), oldest first: from record number ``start`` on, only those
    that mention ``query``, at most ``limit`` of them and :data:`RECORD_MAX_CHARS` characters; says how to go on when there is more."""
    lines: list[tuple[int, str]] = []
    for entry in entries:
        text = render_entry(entry, namer, viewer=viewer, style=BOT)
        if text:
            lines.append((int(entry.seq), text))
    total = len(lines)
    needle = (query or "").strip().casefold()
    if needle:
        lines = [(seq, text) for seq, text in lines if needle in text.casefold()]
    matched = len(lines)
    first = int(start) if start else 0
    lines = [(seq, text) for seq, text in lines if seq >= first]
    page: list[str] = []
    used = 0
    taken = 0
    for seq, text in lines:
        if taken >= max(1, int(limit)) or (page and used + len(text) + 1 > RECORD_MAX_CHARS):
            break
        page.append(text)
        used += len(text) + 1
        taken += 1
    if not page:
        return "（没有符合的记录。）" if total else "（这一局没有你能看到的记录。）"
    more = lines[taken:]
    head = (
        f"（你在这一局里能看到的记录共 {total} 条"
        + (f"，其中含「{(query or '').strip()}」的有 {matched} 条" if needle else "")
        + f"；下面是从 #{lines[0][0]} 起的 {taken} 条）"
    )
    tail = f"\n……后面还有 {len(more)} 条，用 start={more[0][0]} 接着读。" if more else ""
    return head + "\n" + "\n".join(page) + tail
