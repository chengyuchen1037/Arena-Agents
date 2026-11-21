"""The Composer (v0.14 H12-H15; docs/v0.12-contracts.md §13.5): what a bot is shown, where, and when.

A bot is four layers of memory (``app.bots.types``): **L0** who it is, how it talks, how it reasons (only its user writes it);
**L1** the official contract and its own operating notes; **L2** the official rules and its own understanding, playbook and
impressions; **L3** the game itself (state board, facts, history, notes).  The Composer makes every layer *count* without
drowning the ones that matter - above all the L0, which the models used to forget behind pages of manual:

* the **system prompt** (``system_prompt()``) is built once and never changes during a game, so a provider's prefix cache keeps
  hitting.  Order (H12): who I am and how I talk (L0) - how I reason (L0, with its extra sections) - what I understand of this game
  (L2: my digest and the playbook hypotheses I have some confidence in, with my record) - what I think of the others (L2) - the
  official rules (reference) - how the platform works (the contract's "必读" part, then my own operating notes) - and at the very
  end an *anchor*: who I am and how I talk, in a sentence each (L0 again).  The L0 is never shortened.  When the learned parts
  are over their budgets the Composer drops the weakest first: playbook entries of middle confidence with the least support, then
  impressions; the digest, the contract and the rules stay whole;
* a bot's **own memory is rendered as first-person prose** (H13) - "我很有把握的：…；我觉得多半成立、但还要继续验证的：…" -
  never as a list: a model imitates the shape of what it reads, and a list in the prompt teaches list-shaped speech.  The
  contract and the rules are official statements and stay as they are;
* at the moment a decision is due, **a hint is appended to the message** (``event_hint``, H14), never to the system prompt: the
  voice of the bot (first sentence + length range of its ``voice``), one or two of its own playbook entries found by the *situation
  tags* the scenario declares (``spec.situations``) - an entry it is not sure of is introduced as "还没验证过" - and one question
  that belongs to the moment ("你认定的最硬的事实是什么？" before a choice).

The Composer only *prepares text*.  It has no world, no tool and no way to say anything on a bot's behalf: speech comes from the
bot's own worker calling ``speak`` after it has thought (H18).  It knows no game: the words come from the bot's files, the
contract and the scenario.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.agents.contract import must_read_text
from app.bots.types import (
    ARCH_NOTES_CHARS,
    CONTRACT_MUST_READ_CHARS,
    BotMind,
    ContractDoc,
    MountBlock,
    PlaybookEntry,
    sha256_text,
)

#: What the hint is about (``event_hint(kind, ...)``): a turn to speak, a choice every player makes together (a vote), an action
#: asked of some players only, a question to answer in words.
HINT_KINDS = ("speak", "choose", "act", "answer")
_CONFIDENCE_RANK = {"高": 2, "中": 1, "低": 0}
_SENTENCE_ENDERS = "。！？!?"
_SENTENCE_TAIL = "。！？!?」』”’）)…"  # what may follow the end of a sentence and still belong to it: more stops, a closing quote
_RANGE_RE = re.compile(r"篇幅\s*[:：]?\s*(\d{1,4})\s*[–—\-~～到至]\s*(\d{1,4})\s*字")


@dataclass(frozen=True)
class ComposerBudgets:
    """Character budgets of the blocks (H12).  The L0 has none: it is never cut."""

    understanding: int = 1_600  # my digest + the playbook entries that are mounted
    people: int = 1_000  # my impressions of the others
    notes: int = ARCH_NOTES_CHARS  # my own operating notes
    contract: int = CONTRACT_MUST_READ_CHARS  # the contract's "必读" part
    anchor_sentence: int = 48  # one sentence of the identity / the voice in the closing anchor
    hint_sentence: int = 80  # the voice sentence of a hint
    hint_entries: int = 2  # playbook entries in one hint (H14: one or two)


# --------------------------------------------------------------------------------------------------- small helpers
def sha12(text: str) -> str:
    """The first 12 hex digits of the sha256 of ``text``: what the manifests carry instead of a body."""
    return sha256_text(text)[:12]


def _flat(text: str) -> str:
    return " ".join((text or "").split())


def _sentence(text: str) -> str:
    """``text`` as one sentence: whitespace collapsed, ending with a full stop unless it ends with some punctuation already."""
    flat = _flat(text)
    if not flat:
        return ""
    return flat if flat[-1] in "。！？!?…」』）)”\"" else flat + "。"


def first_sentence(text: str, limit: int = 48) -> str:
    """The first sentence of the first non-empty line of ``text`` (a long one is cut at a comma and ends with ``…``)."""
    line = next((l for l in (text or "").splitlines() if l.strip()), "")
    flat = _flat(line)
    if not flat:
        return ""
    end = next((i for i, ch in enumerate(flat) if ch in _SENTENCE_ENDERS), None)
    if end is not None:
        while end + 1 < len(flat) and flat[end + 1] in _SENTENCE_TAIL:  # (the quote that closes the sentence belongs to it)
            end += 1
    sentence = flat[: end + 1] if end is not None else flat
    if len(sentence) > limit:
        cut = max(sentence.rfind(mark, 0, limit) for mark in "，；、,;")
        sentence = (sentence[:cut] if cut >= limit // 2 else sentence[: limit - 1]).rstrip("，；、,; ") + "…"
    return _sentence(sentence)


def length_range(voice: str) -> tuple[int, int] | None:
    """The length range a ``voice`` states (``篇幅：60–150 字``), or ``None``."""
    match = _RANGE_RE.search(voice or "")
    if not match:
        return None
    low, high = int(match.group(1)), int(match.group(2))
    return (low, high) if 0 < low <= high else None


def _voice_line(voice: str, limit: int) -> str:
    """The first sentence of the voice and its length range: what a bot is reminded of when it is about to talk."""
    sentence = first_sentence(voice, limit)
    span = length_range(voice)
    return sentence + (f"篇幅 {span[0]}–{span[1]} 字。" if span else "")


def _get(rule: Any, key: str) -> Any:
    return rule.get(key) if isinstance(rule, Mapping) else getattr(rule, key, None)


def _confidence_phrase(entry: PlaybookEntry) -> str:
    evidence = f"，支持 {entry.support} 次，反例 {entry.counter} 次" if (entry.support or entry.counter) else ""
    return f"把握：{entry.confidence}{evidence}"


def _plain_entry(entry: PlaybookEntry) -> str:
    return _flat(entry.text).rstrip("。.；;，,")


_EVENT_WORDS = {
    "floor.granted": "轮到发言",
    "floor.revoked": "发言权被收回",
    "request.opened": "收到请求",
    "request.answer": "同组有人作答",
    "message.new": "有新消息",
    "challenge.resolved": "质询有了答复",
    "timer.elapsed": "休眠时间到",
}
_REQUEST_WORDS = {"choice": "选择", "text": "文字"}


def describe_situations(spec: Any) -> str:
    """The situation tags a scenario declares, in words (``""`` when it declares none).  For the places that tell a bot which tags
    it may put on a playbook entry (``#标签``) so that the Composer can find the entry again at the moment it fits: the learning
    runs (``read_source("rules")``) show this text next to the rules.  A tag several rules give is listed once, with every
    moment it stands for."""
    moments: dict[str, list[str]] = {}
    for rule in getattr(spec, "situations", None) or []:
        tag = str(_get(rule, "tag") or "").strip()
        if not tag:
            continue
        when: list[str] = []
        events = [str(e) for e in (_get(rule, "events") or [])]
        if events:
            when.append("、".join(_EVENT_WORDS.get(e, e) for e in events))
        kind = str(_get(rule, "request_kind") or "")
        if kind:
            when.append(f"{_REQUEST_WORDS.get(kind, kind)}类请求")
        if _get(rule, "phase_contains"):
            when.append(f"阶段含“{_get(rule, 'phase_contains')}”")
        if _get(rule, "channel_type"):
            when.append(f"频道类型 {_get(rule, 'channel_type')}")
        if _get(rule, "channel_id"):
            when.append(f"频道 {_get(rule, 'channel_id')}")
        moments.setdefault(tag, []).append("，".join(when))
    if not moments:
        return ""
    shown = [f"{tag}（{' 或 '.join(w for w in whens if w)}）" if any(whens) else tag for tag, whens in moments.items()]
    return "这个剧本声明的情境标签：" + "；".join(shown) + "。"


# --------------------------------------------------------------------------------------------------- the Composer
@dataclass(frozen=True)
class _Section:
    id: str
    layer: str  # "L0" | "L1" | "L2"
    text: str


def _core_sections(mind: BotMind, seat_id: str = "") -> list[_Section]:
    """The L0 at the head of a prompt: who the bot is (and, in a game, which seat it plays), how it talks, how it reasons, its
    extra sections - each as the user wrote it, none of them shortened."""
    core = mind.core
    name = (mind.name or "").strip()
    who = f"你是 {name}" + (f"（本局编号 {seat_id}）" if seat_id else "") + "。" if name else ""
    sections = [_Section("core.identity", "L0", (who + core.identity.strip()).strip())]
    if core.voice.strip():
        sections.append(_Section("core.voice", "L0", core.voice.strip()))
    if core.mind.strip():
        sections.append(_Section("core.mind", "L0", core.mind.strip()))
    extras = [f"{title.strip()}：{text.strip()}" if title.strip() else text.strip() for title, text in core.extras if text.strip()]
    if extras:
        sections.append(_Section("core.extra", "L0", "\n\n".join(extras)))
    return [section for section in sections if section.text]


def core_text(mind: BotMind, seat_id: str = "") -> str:
    """The L0 of ``mind`` as the head of a prompt (the learning runs start with it too: the bot studies as itself)."""
    return "\n\n".join(section.text for section in _core_sections(mind, seat_id))


class Composer:
    """``Composer(mind, contract, *, spec, memory_enabled=True)`` (contract §13.5).

    ``seat_id`` is the seat the bot plays (its id in this game); ``seated`` the names of the *other* players, when known -
    impressions of anybody else are not mounted (H9); without it the impressions the snapshot holds are all mounted (the store
    that made the snapshot already left the absent ones out).  ``memory_enabled=False`` mounts only the L0, the contract and the
    official rules (no digest, playbook, impressions, record or operating notes - and no playbook entries in the hints).
    """

    def __init__(
        self,
        mind: BotMind,
        contract: ContractDoc,
        *,
        spec: Any,
        memory_enabled: bool = True,
        seat_id: str = "",
        seated: Sequence[str] | None = None,
        budgets: ComposerBudgets | None = None,
    ) -> None:
        self.mind = mind
        self.contract = contract
        self.spec = spec
        self.memory_enabled = bool(memory_enabled)
        self.seat_id = seat_id
        self.seated = None if seated is None else tuple(seated)
        self.budgets = budgets or ComposerBudgets()
        self._sections = self._build()
        self._system = "\n\n".join(section.text for section in self._sections)

    # ------------------------------------------------------------------------------------------ the system prompt
    def system_prompt(self) -> str:
        """The system prompt: fixed for the whole game (the same string every time)."""
        return self._system

    def mounts(self) -> list[MountBlock]:
        """The blocks of the system prompt in order: id, layer, length, hash of its text - never the text (H15a)."""
        return [MountBlock(s.id, s.layer, len(s.text), sha12(s.text)) for s in self._sections]

    def _build(self) -> list[_Section]:
        # ① who I am, how I talk  ② how I reason (L0: never shortened)
        sections: list[_Section] = _core_sections(self.mind, self.seat_id)

        if self.memory_enabled:
            sections.extend(self._understanding())
            people = self._people()
            if people:
                sections.append(_Section("l2.people", "L2", people))

        rules = (getattr(self.spec, "rules_text", "") or "").strip()
        if rules:
            sections.append(_Section("rules", "L2", "【这局游戏的官方规则】\n" + rules))

        frame = "你是这场由主持人主持的游戏里的一名玩家。你的身份、阵营和目标写在状态板里，只有你自己知道。"
        must_read = must_read_text(self.contract, limit=self.budgets.contract)
        sections.append(_Section("l1.contract", "L1", "【平台怎么运作】\n" + frame + ("\n\n" + must_read if must_read else "")))
        if self.memory_enabled:
            notes = self._notes()
            if notes:
                sections.append(_Section("l1.notes", "L1", notes))

        anchor = self._anchor()
        if anchor:
            sections.append(_Section("anchor", "L0", anchor))
        return sections

    # --------------------------------------------------------------------------------------- the learned parts (L2, L1)
    def _understanding(self) -> list[_Section]:
        """"我对这个游戏的理解": my digest, the playbook entries I have some confidence in, my record - as prose (H13)."""
        rules = self.mind.rules
        digest = rules.digest.strip()
        playbook = self._playbook_prose(len(digest))
        record = _flat(self.mind.record).rstrip("。.")
        parts: list[tuple[str, str]] = []
        if digest:
            parts.append(("l2.digest", digest))
        if playbook:
            parts.append(("l2.playbook", playbook))
        if record:
            parts.append(("l2.record", f"我以往的战绩：{record}。"))
        if not parts:
            return []
        head = "【我对这个游戏的理解】\n下面是我自己对这个游戏的理解，和我打算怎么玩的想法。它们都只是我目前的假设，要拿这一局的事实和结果去检验，对不上就改。\n"
        first_id, first_text = parts[0]
        parts[0] = (first_id, head + first_text)
        return [_Section(part_id, "L2", text) for part_id, text in parts]

    def _playbook_prose(self, digest_chars: int) -> str:
        """The entries of middle and high confidence, as two sentences ("我很有把握的：…" / "我觉得多半成立、但还要继续验证的：…").
        The ones with the most support first; what does not fit the budget is left out, the weakest first (H12)."""
        entries = [e for e in self.mind.rules.playbook if e.confidence in ("中", "高") and _plain_entry(e)]
        ordered = sorted(
            enumerate(entries), key=lambda pair: (-_CONFIDENCE_RANK[pair[1].confidence], -(pair[1].support - pair[1].counter), pair[0])
        )
        room = self.budgets.understanding - digest_chars - 40  # (the two lead-in phrases)
        chosen: list[PlaybookEntry] = []
        for _index, entry in ordered:
            cost = len(_plain_entry(entry)) + 1
            if cost > room:
                break
            room -= cost
            chosen.append(entry)
        # (back in the order the user wrote them)
        keep = {id(e) for e in chosen}
        chosen = [e for e in entries if id(e) in keep]
        high = [_plain_entry(e) for e in chosen if e.confidence == "高"]
        middle = [_plain_entry(e) for e in chosen if e.confidence == "中"]
        sentences = []
        if high:
            sentences.append("我很有把握的：" + "；".join(high) + "。")
        if middle:
            sentences.append("我觉得多半成立、但还要继续验证的：" + "；".join(middle) + "。")
        return "".join(sentences)

    def _people(self) -> str:
        wanted = None if self.seated is None else {n.strip().casefold() for n in self.seated if n and n.strip()}
        own = (self.mind.name or "").strip().casefold()
        items: list[str] = []
        for person, impression in self.mind.people.items():
            label = (person or "").strip()
            text = _flat(impression)
            if not label or not text or label.casefold() == own:
                continue
            if wanted is not None and label.casefold() not in wanted:
                continue
            items.append(f"{label}：{_sentence(text)}")
        used = 0
        kept: list[str] = []
        for item in items:  # over the budget the later ones go (nothing says which of them matters more)
            if used + len(item) > self.budgets.people:
                break
            used += len(item)
            kept.append(item)
        if not kept:
            return ""
        return (
            "【我对在座各位的印象】\n我以前对他们的印象，只是他们的风格、可信度和习惯，不是谁是什么身份的结论，这一局他们怎么做才算数。\n"
            + "".join(kept)
        )

    def _notes(self) -> str:
        notes = self.mind.arch_notes.strip()
        if not notes:
            return ""
        if len(notes) > self.budgets.notes:
            cut = notes[: self.budgets.notes]
            paragraph = max(cut.rfind("\n\n"), cut.rfind("。"))
            notes = (cut[: paragraph + 1] if paragraph > self.budgets.notes // 2 else cut).rstrip() + "…"
        return "我自己整理的平台操作笔记：\n" + notes

    def _anchor(self) -> str:
        core = self.mind.core
        name = (self.mind.name or "").strip()
        limit = self.budgets.anchor_sentence
        identity = first_sentence(core.identity, limit)
        voice = _voice_line(core.voice, limit)
        if not (identity or voice):
            return ""
        text = "【再提醒一次】" + (f"你是 {name}。" if name else "") + identity
        if voice:
            text += "说话的方式：" + voice
        return text

    # -------------------------------------------------------------------------------------------- the hint (H14)
    def situation_tags(
        self,
        *,
        event: str = "",
        channel_id: str = "",
        channel_type: str = "",
        phase: str = "",
        request_kind: str = "",
    ) -> list[str]:
        """The tags of the scenario's ``situations`` that fit this moment, in the order the scenario lists them.  Every condition
        of a rule that is set must hold (an empty one means "any"); a scenario without ``situations`` has no tags."""
        tags: list[str] = []
        for rule in getattr(self.spec, "situations", None) or []:
            tag = str(_get(rule, "tag") or "").strip()
            if not tag or tag in tags:
                continue
            phase_part = str(_get(rule, "phase_contains") or "")
            if phase_part and phase_part not in phase:
                continue
            wanted = {
                "channel_type": channel_type,
                "channel_id": channel_id,
                "request_kind": request_kind,
            }
            if any(str(_get(rule, key) or "") and str(_get(rule, key)) != value for key, value in wanted.items()):
                continue
            events = _get(rule, "events") or []
            if events and event not in events:
                continue
            tags.append(tag)
        return tags

    def retrieve(self, tags: Sequence[str], *, identity_tags: Sequence[str] = (), limit: int | None = None) -> list[PlaybookEntry]:
        """The playbook entries that fit the moment, best first: an entry tagged with a situation tag counts twice, one tagged with
        one of the bot's own identity tags once; more confidence, then more net support, then the user's order break ties.  Nothing
        is retrieved when memory is off."""
        if not self.memory_enabled:
            return []
        situation, identity = set(tags), set(identity_tags)
        scored: list[tuple[int, int, int, int, PlaybookEntry]] = []
        for index, entry in enumerate(self.mind.rules.playbook):
            if not _plain_entry(entry):
                continue
            own = set(entry.tags)
            score = 2 * len(own & situation) + len(own & identity)
            if score:
                scored.append((-score, -_CONFIDENCE_RANK.get(entry.confidence, 0), -(entry.support - entry.counter), index, entry))
        scored.sort(key=lambda row: row[:4])
        return [row[4] for row in scored[: self.budgets.hint_entries if limit is None else limit]]

    def _entries_sentence(self, entries: Sequence[PlaybookEntry]) -> str:
        """The retrieved entries, in the bot's own words and with the honesty about how far they are verified (H14)."""
        sure: list[str] = []
        unsure: list[str] = []
        for entry in entries:
            text = _plain_entry(entry)
            if entry.confidence == "低":
                unsure.append(text)
            else:
                sure.append(f"{text}（{_confidence_phrase(entry)}）")
        parts: list[str] = []
        if sure:
            parts.append("你之前自己总结过：" + "；".join(sure) + "。")
        if unsure:
            parts.append("你还有一个想法，还没验证过：" + "；".join(unsure) + "。")
        if parts:
            parts.append("这些只是你的假设，和眼前的事实对不上就不用。")
        return "".join(parts)

    def event_hint(
        self, kind: str, tags: Sequence[str] = (), *, identity_tags: Sequence[str] = ()
    ) -> tuple[str, dict[str, Any]]:
        """``(text, manifest)``: what to append to the message of a moment of ``kind`` (``HINT_KINDS``); ``("", {})`` when there
        is nothing to say.  ``tags`` are the situation tags of the moment (:meth:`situation_tags`); ``identity_tags`` - the bot's own
        tags on the board, without the status ones - only count for a choice (``act`` / ``choose``: what the bot has learned about
        being who it is when it has to decide something; a speech or an answer in words is not asked in the name of a role).  The
        manifest has the kind, the tags and the hashes of the entries - no text."""
        if kind not in HINT_KINDS:
            return "", {}
        unique = list(dict.fromkeys(tag for tag in tags if tag))
        entries = self.retrieve(unique, identity_tags=identity_tags if kind in ("act", "choose") else ())
        lines: list[str] = []
        if kind == "speak":
            voice = _voice_line(self.mind.core.voice, self.budgets.hint_sentence)
            if voice:
                lines.append("你的说话方式：" + voice)
        elif kind == "choose":
            lines.append("作答之前先问自己：你认定的最硬的事实是什么？")
        recalled = self._entries_sentence(entries)
        if recalled:
            lines.append(recalled)
        if kind == "speak":
            lines.append("先想清楚此刻最关键的一两件事，再一次说完整。")
        if not lines:
            return "", {}
        manifest = {"kind": kind, "tags": unique, "entries": [sha12(entry.text) for entry in entries]}
        return "【此刻的提醒】\n" + "\n".join(lines), manifest


__all__ = [
    "Composer",
    "ComposerBudgets",
    "HINT_KINDS",
    "core_text",
    "describe_situations",
    "first_sentence",
    "length_range",
    "sha12",
]
