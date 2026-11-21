"""Shared types and constants of the v0.14 bot (docs/v0.12-contracts.md §13.2; decisions H1-H24 of docs/v0.14-plan.md).

Pure data, no I/O.  Four layers of memory (H6-H10):

* **L0 core** - who the bot is, how it talks, how it reasons (``CoreFiles``; only the user changes it);
* **L1 architecture** - the official contract (``ContractDoc``, global) + the bot's own operating notes (``BotMind.arch_notes``);
* **L2 rules** - the official scenario rules (read-only, from the scenario file) + the bot's own digest, playbook,
  open questions (``RulesMemory``) and its impressions of the others (``BotMind.people``);
* **L3 game** - what the world shows during one game (state board, facts, visible history, notes); not stored here.

``BotMind`` is the frozen *snapshot* a game is played with: it is taken when the game starts and saved in the game
(``Participant.memory_snapshot``), so a resumed game and the benchmark rebuild exactly the same prompts.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

CONTRACT_VERSION = "v0.14.0"

BOT_ID_RE = re.compile(r"^b-[0-9a-f]{8}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# ---- L0 (H6, C15) --------------------------------------------------------------------------------------------------
CORE_FORMAT = "arena-core"
CORE_FORMAT_VERSION = 1
CORE_SECTIONS = ("identity", "voice", "mind")  # the three required files: identity.md / voice.md / mind.md
CORE_HEADINGS = {"identity": "身份", "voice": "说话方式", "mind": "分析方式"}  # the headings of the merged file (§13.2)
CORE_LIMITS = {"identity": 300, "voice": 600, "mind": 400}
CORE_EXTRA_MAX = 3  # optional extra sections (``extra-<slug>.md``)
CORE_EXTRA_CHARS = 300
CORE_EXTRA_PREFIX = "附加："  # an extra section of the merged file is ``# 附加：<title>``

# ---- L1 / L2 (H7-H9) -----------------------------------------------------------------------------------------------
ARCH_NOTES_CHARS = 800
DIGEST_CHARS = 800
QUESTIONS_CHARS = 400
PLAYBOOK_MAX = 20
PLAYBOOK_ENTRY_CHARS = 80
PEOPLE_MAX = 30
IMPRESSION_CHARS = 80
CONTRACT_MUST_READ_CHARS = 1200
CONTRACT_MAX_CHARS = 20_000
CONTRACT_MUST_READ_HEADING = "必读"
CONTRACT_REFERENCE_HEADING = "参考"

#: The memory files a user / a learning job can write (``PUT /api/bots/{id}/memory/{part}``, ``write_memory``):
#: ``arch_notes`` is L1 (per bot), ``digest`` / ``playbook`` / ``questions`` are L2 (per scenario), ``people`` is the
#: impressions (per bot, keyed by the other bot's name).
MEMORY_PARTS = ("arch_notes", "digest", "playbook", "questions", "people")
MEMORY_PART_LIMITS = {"arch_notes": ARCH_NOTES_CHARS, "digest": DIGEST_CHARS, "questions": QUESTIONS_CHARS}

# ---- learning (H11, H19-H24, H31-H34; C16) -------------------------------------------------------------------------
INIT_TIMEOUT_S = 120  # wall clock, per bot; all or nothing
REVIEW_TIMEOUT_S = 60  # wall clock, per bot; what was written by then stays
JOT_MAX = 10  # per game
JOT_CHARS = 120

# ---- initialization status (H22) -----------------------------------------------------------------------------------
INIT_UNINITIALIZED = "uninitialized"
INIT_RUNNING = "initializing"
INIT_READY = "ready"
INIT_STALE = "stale"  # the core, the contract, the scenario or the materials changed since the bot was initialized
INIT_FAILED = "failed"
INIT_STATUSES = (INIT_UNINITIALIZED, INIT_RUNNING, INIT_READY, INIT_STALE, INIT_FAILED)
#: What an initialization depends on (``bot.json: init.<name>_hash``); a mismatch makes the status ``stale``.
INIT_HASHES = ("core", "contract", "scenario", "materials")

Confidence = Literal["低", "中", "高"]
CONFIDENCES: tuple[str, ...] = ("低", "中", "高")


@dataclass(frozen=True)
class SourceHashes:
    """The hashes of the things an initialization depends on, apart from the bot's own L0 (which the store hashes itself):
    the contract, the scenario file and the study materials of ``scenario_id`` (``materials`` is ``""`` when there are none).
    The orchestrator computes them from the contract / rules stores and hands them to ``BotStore`` to decide
    ``ready`` / ``stale`` and to stamp a fresh initialization."""

    contract: str = ""
    scenario: str = ""
    materials: str = ""
    scenario_id: str = ""


#: A bot's "think about this after the game" notes (``jot``, H11) are appended to the world note with this title, so they
#: are saved with the game and the review reads them from there.
JOT_NOTE_TITLE = "赛后线索"


def sha256_text(text: str) -> str:
    """The content hash used everywhere (init records, mount manifests, source files): sha256 of the UTF-8 text."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def core_hash(core: "CoreFiles") -> str:
    """``bot.json: init.core_hash`` (H22): the hash of the L0 text - the three required sections (stripped) and the extra
    sections as ``title\\ntext`` - joined by newlines.  Editing the L0 in any way changes it; moving the same text between the
    merged file and the separate files does not."""
    parts = [core.identity.strip(), core.voice.strip(), core.mind.strip()]
    parts += [f"{title.strip()}\n{text.strip()}" for title, text in core.extras]
    return sha256_text("\n".join(parts))


def materials_hash(materials: dict[str, str]) -> str:
    """``init.materials_hash`` (H22): ``""`` when there are no study materials, else the hash of ``slug:sha256`` lines in slug
    order (so renaming or editing any material changes it)."""
    if not materials:
        return ""
    return sha256_text("\n".join(f"{slug}:{sha256_text(text)}" for slug, text in sorted(materials.items())))


# ---- the bot, as the agent layer sees it ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CoreFiles:
    """L0.  ``extras`` is ``((title, text), ...)`` in file order (at most ``CORE_EXTRA_MAX``)."""

    identity: str = ""
    voice: str = ""
    mind: str = ""
    extras: tuple[tuple[str, str], ...] = ()

    @property
    def missing(self) -> tuple[str, ...]:
        """The required sections that are empty (blank text counts as empty)."""
        return tuple(name for name in CORE_SECTIONS if not getattr(self, name).strip())

    @property
    def complete(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "voice": self.voice,
            "mind": self.mind,
            "extras": [{"title": t, "text": x} for t, x in self.extras],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CoreFiles":
        data = data or {}
        extras = tuple(
            (str(item.get("title", "")), str(item.get("text", "")))
            for item in (data.get("extras") or [])
            if isinstance(item, dict)
        )
        return cls(
            identity=str(data.get("identity") or ""),
            voice=str(data.get("voice") or ""),
            mind=str(data.get("mind") or ""),
            extras=extras,
        )


@dataclass(frozen=True)
class PlaybookEntry:
    """One hypothesis of the bot's own playbook (H8, §13.2 ``playbook.md``): never a rule, always a bet that the
    next games confirm (``support``) or contradict (``counter``)."""

    text: str
    confidence: str = "低"  # ``Confidence``
    support: int = 0
    counter: int = 0
    source: str = ""  # "初始化" / "复盘 2026-11-20" / "用户"
    tags: tuple[str, ...] = ()  # situation tags, matched against the scenario's ``situations`` (H14), without the leading ``#``

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "support": self.support,
            "counter": self.counter,
            "source": self.source,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlaybookEntry":
        confidence = str(data.get("confidence") or "低")
        return cls(
            text=str(data.get("text") or ""),
            confidence=confidence if confidence in CONFIDENCES else "低",
            support=int(data.get("support") or 0),
            counter=int(data.get("counter") or 0),
            source=str(data.get("source") or ""),
            tags=tuple(str(t) for t in (data.get("tags") or [])),
        )


@dataclass(frozen=True)
class RulesMemory:
    """The bot's own L2 for ONE scenario (H8)."""

    scenario_id: str = ""
    digest: str = ""  # its rule points, in its own words
    playbook: tuple[PlaybookEntry, ...] = ()
    questions: str = ""  # what it is not sure about yet

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "digest": self.digest,
            "playbook": [e.to_dict() for e in self.playbook],
            "questions": self.questions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RulesMemory":
        data = data or {}
        return cls(
            scenario_id=str(data.get("scenario_id") or ""),
            digest=str(data.get("digest") or ""),
            playbook=tuple(PlaybookEntry.from_dict(e) for e in (data.get("playbook") or []) if isinstance(e, dict)),
            questions=str(data.get("questions") or ""),
        )


@dataclass(frozen=True)
class BotMind:
    """Everything a bot carries into a game (the snapshot of H11 / §13.2).  Empty learned parts are fine: a bot whose
    memory is turned off (or that has none yet) is just its L0 plus the official contract and rules."""

    bot_id: str
    name: str
    core: CoreFiles = field(default_factory=CoreFiles)
    arch_notes: str = ""  # L1, the bot's own
    rules: RulesMemory = field(default_factory=RulesMemory)  # L2, for the scenario of the game
    people: dict[str, str] = field(default_factory=dict)  # impressions: the other bot's NAME -> impression (H9)
    record: str = ""  # the engine-kept record ("场次 7 · 胜 4 · …"), shown to the bot as it is

    SNAPSHOT_VERSION = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.SNAPSHOT_VERSION,
            "bot_id": self.bot_id,
            "name": self.name,
            "core": self.core.to_dict(),
            "arch_notes": self.arch_notes,
            "rules": self.rules.to_dict(),
            "people": dict(self.people),
            "record": self.record,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BotMind":
        people = data.get("people") or {}
        return cls(
            bot_id=str(data.get("bot_id") or ""),
            name=str(data.get("name") or ""),
            core=CoreFiles.from_dict(data.get("core")),
            arch_notes=str(data.get("arch_notes") or ""),
            rules=RulesMemory.from_dict(data.get("rules")),
            people={str(k): str(v) for k, v in people.items()} if isinstance(people, dict) else {},
            record=str(data.get("record") or ""),
        )


# ---- the official contract (H7) ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ContractDoc:
    """The official architecture contract: ``## 必读`` is mounted into every bot's prompt, ``## 参考`` is read on
    demand (``read_contract``).  ``markdown`` is the whole file."""

    version: str = CONTRACT_VERSION
    markdown: str = ""
    must_read: str = ""
    reference: str = ""

    @property
    def sha256(self) -> str:
        return sha256_text(self.markdown)


_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def split_contract(markdown: str, version: str = CONTRACT_VERSION) -> ContractDoc:
    """Split a contract file into its ``## 必读`` and ``## 参考`` sections (the text between a ``## `` heading and the next
    one, stripped).  A missing section is empty; the rest of the file (title, preface) is kept in ``markdown`` only."""
    text = markdown or ""
    matches = list(_H2_RE.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1).strip()] = text[match.end():end].strip()
    return ContractDoc(
        version=version,
        markdown=text,
        must_read=sections.get(CONTRACT_MUST_READ_HEADING, ""),
        reference=sections.get(CONTRACT_REFERENCE_HEADING, ""),
    )


# ---- the mount manifest (H15a) -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class MountBlock:
    """One block the Composer put into a request: where it came from, how long it is, and its content hash - never its text
    (the trace keeps no prompts)."""

    id: str  # e.g. "core.identity", "core.voice", "l2.digest", "l2.playbook", "l2.people", "l1.contract", "l1.notes", "rules", "anchor"
    layer: str  # "L0" | "L1" | "L2" | "L3"
    chars: int
    sha: str  # first 12 hex digits of ``sha256_text``

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "layer": self.layer, "chars": self.chars, "sha": self.sha}


# ---- learning jobs (H19-H21, H31-H34) ------------------------------------------------------------------------------
@dataclass(frozen=True)
class LearningJob:
    """What a learning run is about (the agent layer builds the prompt and the tools from it; ``app.learning`` runs it)."""

    kind: Literal["init", "review"]
    mind: BotMind  # the bot as it stands now (current files, not a game snapshot)
    scenario_id: str
    scenario_title: str
    contract: ContractDoc
    deadline_s: int
    # review only ----------------------------------------------------------------------------------------------------
    seat_id: str = ""  # the seat the bot had in the game that is reviewed
    outcome_text: str = ""  # e.g. "你所在的阵营获胜了。" (produced by the engine from the scenario, never by the bot)
    role_text: str = ""  # e.g. "你的身份是 …（…）" - the identity is public once the game is over
    others_text: str = ""  # who was who (all identities and outcomes), as the engine lists it after the game
    human_note: str = ""  # non-empty when a person played this seat for a stretch (H34): which part of the record
    learn_style: bool = False  # the user asked the bot to learn the voice of that person (``propose_core``)
    jots: tuple[str, ...] = ()  # what the bot noted with ``jot`` during the game (H11)


@dataclass(frozen=True)
class WriteResult:
    """The answer of a learning port to a write (the tool result the model sees is made from it)."""

    ok: bool
    message: str = ""
    file: str = ""
    chars: int = 0


class LearningPort(Protocol):
    """What the learning tools (``app/agents/learning_brain.py``) may do.  Implemented by ``app.learning`` (the "learn"
    stream) over the real files / the game record; the tests of the agent layer use a fake.  Every method is
    synchronous and fast; it never calls a model.

    ``source`` of :meth:`read_source`: ``"contract"`` (``part``: ``"must_read"`` | ``"reference"``), ``"rules"`` (the official
    scenario rules: the player-facing part of the scenario file), ``"materials"`` (``part``: a slug; ``None`` lists them),
    ``"my_core"``, ``"my_memory"`` (``part``: one of ``MEMORY_PARTS``; ``None`` = all).  ``file`` of :meth:`write_memory`:
    one of ``MEMORY_PARTS``.  ``section`` of :meth:`propose_core`: ``"identity"`` | ``"voice"`` | ``"mind"`` |
    ``"extra:<slug>"``.  :meth:`read_record` (review only): ``start`` / ``limit`` page through the ledger the bot could see,
    ``query`` filters by substring.
    """

    def read_source(self, source: str, part: str | None = None) -> str: ...

    def write_memory(self, file: str, content: str) -> WriteResult: ...

    def propose_core(self, section: str, content: str, reason: str) -> WriteResult: ...

    def read_record(self, query: str | None = None, start: int | None = None, limit: int = 40) -> str: ...


__all__ = [
    "ARCH_NOTES_CHARS",
    "BOT_ID_RE",
    "BotMind",
    "CONFIDENCES",
    "CONTRACT_MAX_CHARS",
    "CONTRACT_MUST_READ_CHARS",
    "CONTRACT_MUST_READ_HEADING",
    "CONTRACT_REFERENCE_HEADING",
    "CONTRACT_VERSION",
    "CORE_EXTRA_CHARS",
    "CORE_EXTRA_MAX",
    "CORE_EXTRA_PREFIX",
    "CORE_FORMAT",
    "CORE_FORMAT_VERSION",
    "CORE_HEADINGS",
    "CORE_LIMITS",
    "CORE_SECTIONS",
    "Confidence",
    "ContractDoc",
    "CoreFiles",
    "DIGEST_CHARS",
    "IMPRESSION_CHARS",
    "JOT_NOTE_TITLE",
    "INIT_FAILED",
    "INIT_HASHES",
    "INIT_READY",
    "INIT_RUNNING",
    "INIT_STALE",
    "INIT_STATUSES",
    "INIT_TIMEOUT_S",
    "INIT_UNINITIALIZED",
    "JOT_CHARS",
    "JOT_MAX",
    "LearningJob",
    "LearningPort",
    "MEMORY_PARTS",
    "MEMORY_PART_LIMITS",
    "MountBlock",
    "PEOPLE_MAX",
    "PLAYBOOK_ENTRY_CHARS",
    "PLAYBOOK_MAX",
    "PlaybookEntry",
    "QUESTIONS_CHARS",
    "REVIEW_TIMEOUT_S",
    "RulesMemory",
    "SLUG_RE",
    "SourceHashes",
    "WriteResult",
    "core_hash",
    "materials_hash",
    "sha256_text",
    "split_contract",
]
