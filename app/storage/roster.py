"""The roster: who sits at the table (v0.12.1, E13–E16, E21, E24; v0.13.1, G37–G44).

One entry per seat: an id (``N1``…), a name (default Alice, Bob, …; the user may rename), a pixel-avatar
description, and two text files — a *persona* ("how this player talks and thinks": a short, fixed-format set of
behaviour guidelines) and a *memory* (what the player carries from game to game: the engine-kept record, an
impression of every other seat, a few lessons of his own).  Everything lives in plain files in the data
directory so it can be edited outside the app::

    <data>/roster/roster.json          names, avatars, the memory switch
    <data>/roster/N1.persona.md        the persona, empty by default
    <data>/roster/N1.memory.md         "## 战绩" (engine-kept) / "## 印象" / "## 经验" / "## 旧记忆（待整理）"

The seats are fixed (``N<k>`` is seat *k* in every game; only the identities are dealt anew), so the impressions
are keyed by the seat id and the name is just a label that follows the roster.  A review does not append a
story any more: it *revises* the impressions (see ``app.agents.review``) and the file is overwritten.
Memory files written by v0.13.0 (``## 对局记忆`` / ``## 经验与印象``) are migrated when they are read (in memory,
no model call; the file itself is rewritten by the next save) — their text lands in ``## 旧记忆（待整理）``, which
the next review hands to the model to tidy up and then deletes.

The roster is a *template* for the next game: ``snapshot()`` copies it into the game when it starts, so
changing a name or a memory never affects a game in progress and the system prompts stay cacheable.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from app.storage._atomic import atomic_write, ensure_private_directory
from app.storage.errors import StorageError
from app.storage.paths import get_data_dir

DEFAULT_NAMES = ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo"]
MODERATOR_ID = "moderator"  # the moderator is not a seat: it only has a (random) avatar of its own
MODERATOR_NAME = "主持人"
MAX_NAME_CHARS = 20
MAX_TEXT_CHARS = 20_000  # a sanity cap on any text that is parsed (the real limits are the ones below)
ROSTER_FILE = "roster.json"

# ---- limits (G38, G43, G44; contract 12.5).  ``GET /api/roster`` hands them to the UI as ``limits``. ----------
IMPRESSION_CHARS = 80  # one impression (the text, not its ``- N5 Eve：`` prefix)
EXPERIENCE_ITEMS = 3  # lessons
EXPERIENCE_CHARS = 300  # all the lessons together
MEMORY_CHARS = 1500  # the "## 印象" and "## 经验" sections as written in the file (prefixes, bullets and headings count)
LEGACY_CHARS = 600  # the temporary "## 旧记忆（待整理）" section
PERSONA_CHARS = 500
PERSONA_ITEM_CHARS = 40  # every line of the fixed-format persona
RECORD_PROMPT_CHARS = 800  # the engine-kept record is not limited in the file, but what a prompt gets of it is
LIMITS: dict[str, int] = {
    "impression_chars": IMPRESSION_CHARS,
    "experience_items": EXPERIENCE_ITEMS,
    "experience_chars": EXPERIENCE_CHARS,
    "memory_chars": MEMORY_CHARS,
    "legacy_chars": LEGACY_CHARS,
    "persona_chars": PERSONA_CHARS,
    "persona_item_chars": PERSONA_ITEM_CHARS,
}

# ---- the sections of a memory file ----------------------------------------------------------------------------
SECTION_RECORD = "战绩"
SECTION_IMPRESSIONS = "印象"
SECTION_LESSONS = "经验"
SECTION_LEGACY = "旧记忆（待整理）"
OLD_SECTION_GAMES = "对局记忆"  # v0.13.0: one appended story per game
OLD_SECTION_EXPERIENCE = "经验与印象"  # v0.13.0: free text
NO_IMPRESSION = "（暂无）"  # what an empty impression looks like in the file
ELLIPSIS = "…"

_NAME_BAD = re.compile(r"[\x00-\x1f\x7f<>\"'`\\]")
_ID_RE = re.compile(r"^N([1-9]|[1-9][0-9])$")


class RosterError(ValueError):
    """A roster change is invalid (the message is safe to show to the user)."""


class RosterInputError(RosterError):
    """The request is well-formed JSON but asks for something that makes no sense (HTTP 422 instead of 400):
    both ``memory`` and ``memory_parts``, an impression about oneself or about a seat that does not exist."""


def seat_id(seat: int) -> str:
    return f"N{int(seat)}"


def default_name(seat: int) -> str:
    return DEFAULT_NAMES[(seat - 1) % len(DEFAULT_NAMES)] if seat <= len(DEFAULT_NAMES) else f"玩家{seat}"


_COLON_RE = re.compile(r"[:：]")


def check_text(text: str, what: str) -> None:
    """Refuse a text that cannot be written to a file: a NUL, or a lone surrogate (it cannot be encoded as UTF-8 and would
    fail *after* other parts of the same request were written).  Everything that is going to be saved goes through here
    first."""
    if "\x00" in text:
        raise RosterError(f"{what}不能包含空字符。")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise RosterError(f"{what}含有无法保存的字符（不成对的 Unicode 代理字符）。") from None


def clean_name(value: Any, *, allow_colon: bool = False) -> str:
    """A name the user typed.  A colon (``:`` or ``：``) is refused: the name is the label in front of an impression
    (``- N5 Eve：…``) and a colon in it would end the label early when the line is read back.  ``allow_colon`` is for
    reading a name that is already stored (it still has to be shown, and the labels written from it are cleaned)."""
    text = str(value or "").strip()
    if not text:
        raise RosterError("名字不能为空。")
    if len(text) > MAX_NAME_CHARS:
        raise RosterError(f"名字最多 {MAX_NAME_CHARS} 个字符。")
    if _NAME_BAD.search(text):
        raise RosterError("名字不能包含控制字符、引号、反斜杠或尖括号。")
    check_text(text, "名字")
    if not allow_colon and _COLON_RE.search(text):
        raise RosterError("名字不能包含冒号（: 或 ：）。")
    return text


def clean_text(value: Any, what: str, *, limit: int = MAX_TEXT_CHARS) -> str:
    """Text for a file: a leading byte-order mark and the line ends normalized, surrounding blank lines dropped, ``limit``
    characters at most; a NUL or a lone surrogate is refused (:func:`check_text`)."""
    text = str(value if value is not None else "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    check_text(text, what)
    text = text.strip("\n") if text.strip() else ""
    if len(text) > limit:
        raise RosterError(f"{what}最多 {limit} 个字符（现在 {len(text)} 个），请精简。")
    return text


# ------------------------------------------------------------------------------- small text helpers

_BULLET_RE = re.compile(r"^\s*(?:[-*•·]+|\d{1,2}[.)、．]|[（(]\d{1,2}[）)])\s*")
_MARKDOWN_NOISE = re.compile(r"\*\*|__|`")
_PLACEHOLDERS = (NO_IMPRESSION, "(暂无)", "暂无")


def _one_line(value: Any) -> str:
    """One line of plain text: every run of white space (a line break included) becomes a single space."""
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def _unmark(value: str) -> str:
    """What a model puts around words in markdown (bold, code) is not part of the text."""
    return _MARKDOWN_NOISE.sub("", value)


def _clip(text: str, limit: int) -> str:
    """At most ``limit`` characters; a text that had to be cut ends with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + ELLIPSIS


def _label_name(name: Any) -> str:
    """A name as it stands in a label (``- N5 Eve：…``): a colon would end the label early when the line is read back, so it
    becomes a space.  (New names cannot have one - :func:`clean_name` - but a stored name may.)"""
    return _one_line(_COLON_RE.sub(" ", str(name or "")))


def clean_impression(value: Any) -> str:
    """One impression as it is stored: a single line; the ``（暂无）`` placeholder means "none yet"."""
    text = _one_line(value)
    return "" if text in _PLACEHOLDERS else text


def clean_experience(items: Any) -> list[str]:
    """The lessons as one-line strings (a list of strings, or a text with one lesson per line); bullet marks in front of a
    line are dropped and empty lines are not lessons.  Only strings are lessons: a ``None``, a number or an object in a
    list is dropped, never turned into the text ``"None"`` / ``"5"`` / ``"{'a': 1}"``."""
    if isinstance(items, str):
        items = items.splitlines()
    if not isinstance(items, (list, tuple)):
        return []
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        text = _one_line(_BULLET_RE.sub("", item, count=1))
        if text and text not in _PLACEHOLDERS:
            out.append(text)
    return out


#: What a model writes when it has nothing to say about a seat (or copies the ``…`` of the prompt's example): words that
#: mean "no change" / "none".  A text that is made of nothing else is not an impression (``is_no_change``).
_NO_CHANGE_PHRASES = tuple(
    sorted(
        {
            "保持原样", "保持不变", "保持现状", "维持原样", "维持不变", "维持现状", "沿用旧印象", "沿用原印象", "沿用原样", "沿用",
            "无新信息", "没有新信息", "无新增信息", "没有新增信息", "暂无新信息", "无新增", "没有新增", "无新内容", "没有新内容",
            "无变化", "没有变化", "无更新", "没有更新", "无修改", "没有修改", "无改动", "没有改动", "无调整", "没有调整", "无补充", "没有补充",
            "同上", "同前", "如上", "不变", "照旧", "原样", "暂无", "没有", "无", "略",
            "nochange", "nochanges", "nonewinformation", "nonewinfo", "nonewinformations", "unchanged", "sameasabove", "same",
            "asbefore", "asis", "keepasis", "keep", "noupdate", "noupdates", "none", "na", "nil",
        },
        key=len,
        reverse=True,
    )
)


def is_no_change(text: Any) -> bool:
    """``True`` for a text that says nothing: empty, only punctuation (``…``, ``...``, ``—``), or only words like
    ``保持原样`` / ``无变化`` / ``同上`` / ``无`` / ``无新信息`` (what a model answers for a seat it has nothing new about, or
    copies from the ``…`` of the example in the prompt).  Such a text must never be saved as an impression or a lesson."""
    letters = re.sub(r"[\W_]+", "", str(text if text is not None else "").casefold())
    for phrase in _NO_CHANGE_PHRASES:
        letters = letters.replace(phrase, "")
    return not letters


# ------------------------------------------------------------------------------- the memory file


@dataclass(frozen=True, eq=False)
class MemoryContext:
    """Whose memory this is: the file belongs to seat ``owner`` (no impression of oneself), ``ids`` are all the seats
    in seat order, ``names`` the current names (labels only: the impressions are keyed by the id)."""

    owner: str
    ids: tuple[str, ...]
    names: dict[str, str] = field(default_factory=dict)

    @property
    def others(self) -> list[str]:
        return [sid for sid in self.ids if sid != self.owner]

    def label(self, sid: str) -> str:
        """``N5 Eve``: what stands in front of an impression.  A colon in a (stored) name becomes a space: the first colon
        of the line ends the label when it is read back, so a name with a colon would cut the impression."""
        name = _label_name(self.names.get(sid))
        return f"{sid} {name}" if name else sid


@dataclass
class MemoryParts:
    """What ``N<k>.memory.md`` says, structured."""

    record: str = ""  # the engine-kept "## 战绩" body, verbatim
    impressions: dict[str, str] = field(default_factory=dict)  # another seat's id -> impression ("" = none yet)
    experience: list[str] = field(default_factory=list)  # lessons, one line each
    legacy: str = ""  # the "## 旧记忆（待整理）" body: v0.13.0 text waiting for the next review to tidy it up

    def copy(self) -> "MemoryParts":
        return MemoryParts(self.record, dict(self.impressions), list(self.experience), self.legacy)

    @property
    def empty(self) -> bool:
        return not (self.record.strip() or any(self.impressions.values()) or self.experience or self.legacy.strip())

    def view(self) -> dict[str, Any]:
        """The ``memory_parts`` of ``GET /api/roster``."""
        return {
            "record": self.record,
            "impressions": dict(self.impressions),
            "experience": list(self.experience),
            "legacy": self.legacy,
        }


_HEADING_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$")
_IMPRESSION_LINE_RE = re.compile(r"^\s*(?:[-*•·]+\s*|\d{1,2}[.)、．]\s*)?[Nn]([1-9][0-9]?)(?![0-9])(.*)$")


def split_memory(text: str) -> dict[str, str]:
    """``{heading: body}`` of a memory file in file order; text before the first ``## `` heading is filed under ``""``.
    A byte-order mark in front of the text (a file saved by an editor that puts one there) is not part of it: with it the
    first heading — usually ``## 战绩`` — would not be recognised and the record would be lost."""
    chunks: dict[str, list[str]] = {}
    current = ""
    for line in (text or "").lstrip("\ufeff").splitlines():
        match = _HEADING_RE.match(line)
        if match:
            current = match.group(1)
            chunks.setdefault(current, [])
        else:
            chunks.setdefault(current, []).append(line)
    bodies = {title: "\n".join(lines).strip("\n") for title, lines in chunks.items()}
    return {title: body for title, body in bodies.items() if title or body.strip()}


def join_memory(sections: dict[str, str]) -> str:
    """The inverse of :func:`split_memory`; a section with an empty body is left out."""
    blocks = []
    for title, body in sections.items():
        body = (body or "").strip("\n")
        if body.strip():
            blocks.append(f"## {title}\n{body}" if title else body)
    return "\n\n".join(blocks)


def _after_label(rest: str, sid: str, ctx: MemoryContext) -> str | None:
    """The text of an impression line after ``N5 Eve：`` (``rest`` is what follows the id); ``None`` without a colon.
    The seat's own name is recognised first (as it is stored, and as a label is written: without colons), so that a name
    with a colon in it — a label written before names could not have one — does not eat the text."""
    name = ctx.names.get(sid) or ""
    for known in dict.fromkeys((name, _label_name(name))):
        if known:
            match = re.match(r"\s*" + re.escape(known) + r"\s*[：:]\s*(.*)$", rest)
            if match:
                return match.group(1)
    match = re.match(r"\s*[^：:\n]{0,24}[：:]\s*(.*)$", rest)
    return match.group(1) if match else None


def parse_impression_lines(text: str, ctx: MemoryContext, *, unmark: bool = False) -> dict[str, str]:
    """``- N5 Eve：…`` / ``N5：…`` lines -> ``{seat id: impression}`` (both colons are accepted; the name is only a
    label; unknown seats and the owner's own seat are dropped; a later line for the same seat wins; a plain line
    below an impression continues it).  ``unmark`` drops markdown emphasis (a model's answer)."""
    found: dict[str, str] = {}
    last: str | None = None
    others = set(ctx.others)
    for raw in (text or "").splitlines():
        line = _unmark(raw) if unmark else raw
        match = _IMPRESSION_LINE_RE.match(line)
        if match:
            sid = f"N{int(match.group(1))}"
            body = _after_label(match.group(2), sid, ctx)
            if body is None or sid not in others:
                last = None
                continue
            found[sid] = clean_impression(body)
            last = sid
        elif last is not None and line.strip() and not (_BULLET_RE.match(line) or _EXPERIENCE_HEAD_RE.match(line.strip()) or _ANY_HEAD_RE.match(line.strip())):
            found[last] = clean_impression(f"{found[last]} {line}")
        else:
            last = None
    return found


def parse_memory(text: str, ctx: MemoryContext) -> MemoryParts:
    """A memory file -> its parts.  Lenient and lossless: nothing is cut (see :func:`fit_memory` for reading and
    :func:`check_memory` for a save).  Anything that is not the record, the impressions or the lessons — the v0.13.0
    ``## 对局记忆`` / ``## 经验与印象``, text without a heading, any other section — becomes the legacy text
    (this is the whole migration of an old file: it needs no model call and parsing the result again changes nothing)."""
    sections = split_memory(text)
    parts = MemoryParts(record=sections.get(SECTION_RECORD, ""))
    others = ctx.others
    found = parse_impression_lines(sections.get(SECTION_IMPRESSIONS, ""), ctx)
    parts.impressions = {sid: found.get(sid, "") for sid in others}
    parts.experience = clean_experience(sections.get(SECTION_LESSONS, ""))
    pieces: list[str] = []
    for title, body in sections.items():
        if title in (SECTION_RECORD, SECTION_IMPRESSIONS, SECTION_LESSONS) or not body.strip():
            continue
        plain = title in ("", OLD_SECTION_GAMES, OLD_SECTION_EXPERIENCE, SECTION_LEGACY)
        pieces.append(body.strip("\n") if plain else f"【{title}】\n{body.strip(chr(10))}")
    parts.legacy = "\n\n".join(pieces)
    return parts


def render_impressions(parts: MemoryParts, ctx: MemoryContext) -> str:
    return "\n".join(f"- {ctx.label(sid)}：{parts.impressions.get(sid) or NO_IMPRESSION}" for sid in ctx.others)


def render_experience(parts: MemoryParts) -> str:
    return "\n".join(f"- {item}" for item in parts.experience)


def memory_body(parts: MemoryParts, ctx: MemoryContext) -> str:
    """The ``## 印象`` and ``## 经验`` sections as they are written: this is what ``MEMORY_CHARS`` counts."""
    experience = render_experience(parts)
    return f"## {SECTION_IMPRESSIONS}\n{render_impressions(parts, ctx)}\n\n## {SECTION_LESSONS}" + (f"\n{experience}" if experience else "")


def memory_chars(parts: MemoryParts, ctx: MemoryContext) -> int:
    return len(memory_body(parts, ctx))


def render_memory(parts: MemoryParts, ctx: MemoryContext) -> str:
    """The canonical text of a memory file (``""`` when there is nothing to remember): record, one impression line per
    other seat in seat order, the lessons, and the legacy text when there is any."""
    if parts.empty:
        return ""
    blocks = []
    if parts.record.strip():
        blocks.append(f"## {SECTION_RECORD}\n{parts.record.strip(chr(10))}")
    blocks.append(memory_body(parts, ctx))
    if parts.legacy.strip():
        blocks.append(f"## {SECTION_LEGACY}\n{parts.legacy.strip(chr(10))}")
    return "\n\n".join(blocks)


#: Said once above the impressions in a prompt: the seat ids stay, the identities do not.
PROMPT_IMPRESSION_NOTE = "（座位号是固定的，但每一局的身份都会重新分配：这些印象只记他的风格和习惯，不代表他这一局的身份。）"


def memory_for_prompt(parts: MemoryParts, ctx: MemoryContext) -> str:
    """The memory as the agent reads it in its system prompt: only what there is (no ``（暂无）`` rows).  The record is the
    engine's and has no limit in the file, but the prompt gets at most ``RECORD_PROMPT_CHARS`` of it (a hand-edited huge
    record must not become a huge system prompt)."""
    blocks = []
    if parts.record.strip():
        blocks.append(f"## {SECTION_RECORD}\n{_clip(parts.record.strip(chr(10)), RECORD_PROMPT_CHARS)}")
    lines = [f"- {ctx.label(sid)}：{parts.impressions[sid]}" for sid in ctx.others if parts.impressions.get(sid)]
    if lines:
        blocks.append(f"## {SECTION_IMPRESSIONS}\n{PROMPT_IMPRESSION_NOTE}\n" + "\n".join(lines))
    if parts.experience:
        blocks.append(f"## {SECTION_LESSONS}\n{render_experience(parts)}")
    if parts.legacy.strip():
        blocks.append(f"## {SECTION_LEGACY}\n{parts.legacy.strip(chr(10))}")
    return "\n\n".join(blocks)


def _fit_experience(items: list[str]) -> list[str]:
    """At most ``EXPERIENCE_ITEMS`` lessons and ``EXPERIENCE_CHARS`` characters: the later ones are cut first."""
    out: list[str] = []
    left = EXPERIENCE_CHARS
    for item in items[:EXPERIENCE_ITEMS]:
        if left <= 0:
            break
        item = _clip(item, left)
        out.append(item)
        left -= len(item)
    return out


def fit_memory(parts: MemoryParts, ctx: MemoryContext) -> MemoryParts:
    """A copy of ``parts`` that is within every limit, cut where it has to be (reading an old or hand-edited file, the
    output of a model).  Cutting never fails: a file that is too long still opens and can be saved again."""
    out = parts.copy()
    out.record = out.record.strip("\n")
    out.impressions = {sid: _clip(clean_impression(parts.impressions.get(sid, "")), IMPRESSION_CHARS) for sid in ctx.others}
    out.experience = _fit_experience(clean_experience(parts.experience))
    legacy = out.legacy.strip()
    if len(legacy) > LEGACY_CHARS:  # the newer text is at the end: keep that
        legacy = ELLIPSIS + legacy[-(LEGACY_CHARS - 1):].lstrip()
    out.legacy = legacy
    _fit_total(out, ctx)
    return out


def _fit_total(parts: MemoryParts, ctx: MemoryContext) -> None:
    """Bring ``## 印象`` + ``## 经验`` under ``MEMORY_CHARS`` by shortening the longest text first (the long ones lose the
    most, a short impression stays whole).  Only extreme names get here: eleven full impressions and full lessons
    leave room for names of about ten characters."""
    while True:
        over = memory_chars(parts, ctx) - MEMORY_CHARS
        if over <= 0:
            break
        units: list[tuple[str, Any, str]] = [("i", sid, text) for sid, text in parts.impressions.items()]
        units += [("e", index, text) for index, text in enumerate(parts.experience)]
        units.sort(key=lambda unit: len(unit[2]), reverse=True)  # (stable: equally long ones keep their order)
        if not units or not units[0][2]:
            break
        second = len(units[1][2]) if len(units) > 1 else 0
        cut = min(over, max(1, len(units[0][2]) - second))
        kind, key, text = units[0]
        shorter = text[:-cut].rstrip()
        if kind == "i":
            parts.impressions[key] = shorter
        else:
            parts.experience[key] = shorter
    parts.experience = [item for item in parts.experience if item]


def check_memory(parts: MemoryParts, ctx: MemoryContext) -> None:
    """A *save* does not cut anything: raise :class:`RosterError` that says which part is too long, how long it is
    now and what the limit is (the first one found)."""
    for sid in ctx.others:
        n = len(parts.impressions.get(sid, ""))
        if n > IMPRESSION_CHARS:
            raise RosterError(f"{ctx.label(sid)} 的印象最多 {IMPRESSION_CHARS} 字（现在 {n} 字），请精简。")
    if len(parts.experience) > EXPERIENCE_ITEMS:
        raise RosterError(f"经验最多 {EXPERIENCE_ITEMS} 条（现在 {len(parts.experience)} 条），请精简。")
    n = sum(len(item) for item in parts.experience)
    if n > EXPERIENCE_CHARS:
        raise RosterError(f"经验合计最多 {EXPERIENCE_CHARS} 字（现在 {n} 字），请精简。")
    n = len(parts.legacy.strip())
    if n > LEGACY_CHARS:
        raise RosterError(f"旧记忆最多 {LEGACY_CHARS} 字（现在 {n} 字），请精简，或交给主持人模型整理。")
    n = memory_chars(parts, ctx)
    if n > MEMORY_CHARS:
        raise RosterError(f"整份记忆（印象和经验两段，含序号与符号）最多 {MEMORY_CHARS} 字（现在 {n} 字），请精简。")


class AppliedReview(NamedTuple):
    """What a finished review did, for the caller and the UI."""

    text: str  # the memory file as it is now
    changes: dict[str, Any]  # {"impressions": [{"id", "before", "after"}…], "experience": {"before", "after"} | None}


def memory_changes(before: MemoryParts, after: MemoryParts) -> dict[str, Any]:
    """What changed between two states of the same memory (only the impressions that differ; ``experience`` is ``None``
    when the lessons are the same) — the ``changes`` of a ``review.progress`` event."""
    impressions = [
        {"id": sid, "before": before.impressions.get(sid, ""), "after": text}
        for sid, text in after.impressions.items()
        if before.impressions.get(sid, "") != text
    ]
    experience = (
        {"before": list(before.experience), "after": list(after.experience)} if before.experience != after.experience else None
    )
    return {"impressions": impressions, "experience": experience}


# ------------------------------------------------------------------------------- the output of a review model


class ReviewOutput(NamedTuple):
    """What a review (or "complete the memory") answer says: ``impressions`` only has the seats the model wrote about,
    ``experience`` is ``None`` when it wrote no lessons; ``mode`` is ``"json"`` or ``"lines"``."""

    impressions: dict[str, str]
    experience: list[str] | None
    mode: str


_EXPERIENCE_HEAD_RE = re.compile(r"^[#>\s*【\[]*(?:我的)?经验(?:教训|心得|与教训)?\s*[】\]]*\s*(?:[：:]\s*(.*))?$")
_ANY_HEAD_RE = re.compile(r"^(?:#{1,6}\s|[【\[][^】\]]{1,12}[】\]]\s*$)")


_IMPRESSION_KEYS = ("impressions", "impression", "印象")
_EXPERIENCE_KEYS = ("experience", "experiences", "lessons", "经验")
_MAX_JSON_CANDIDATES = 100  # how many ``{`` of an answer are tried as the start of its JSON object


def _first_key(data: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return None


def _json_object(text: str) -> dict[str, Any] | None:
    """The JSON object in a model's answer: the whole text, or the first object in it that has the keys of an answer (a
    code fence or a sentence of chatter around it, even a ``{…}`` in the chatter, does no harm)."""
    value = (text or "").strip()
    try:
        whole = json.loads(value)
    except (ValueError, RecursionError):  # (a deeply nested heap of brackets is not JSON we can read, not a crash)
        whole = None
    if isinstance(whole, dict):
        return whole
    decoder = json.JSONDecoder()
    first: dict[str, Any] | None = None
    pos = value.find("{")
    tried = 0
    while pos != -1 and tried < _MAX_JSON_CANDIDATES:  # (a heap of braces must not make this quadratic)
        tried += 1
        try:
            data, end = decoder.raw_decode(value, pos)
        except (ValueError, RecursionError):
            pos = value.find("{", pos + 1)
            continue
        if isinstance(data, dict):
            if _first_key(data, _IMPRESSION_KEYS + _EXPERIENCE_KEYS) is not None:
                return data
            first = first or data
        pos = value.find("{", end)
    return first


def _seat_of(key: Any) -> str | None:
    match = re.match(r"\s*[Nn]?(\d{1,2})(?!\d)", str(key))
    return f"N{int(match.group(1))}" if match and int(match.group(1)) > 0 else None


def _lines_experience(text: str) -> list[str]:
    items: list[str] = []
    active = False
    for raw in (text or "").splitlines():
        line = _unmark(raw).strip()
        if not line:
            continue
        head = _EXPERIENCE_HEAD_RE.match(line)
        if head:
            active = True
            if (head.group(1) or "").strip():
                items.append(head.group(1))
            continue
        if _IMPRESSION_LINE_RE.match(line) or _ANY_HEAD_RE.match(line):
            active = False
            continue
        if active:
            items.append(line)
    return clean_experience(items)


def parse_review_output(text: str, ctx: MemoryContext) -> ReviewOutput | None:
    """The answer of a review model -> impressions and lessons, or ``None`` when it says neither.

    The JSON object the prompt asks for (``{"impressions": {"N1": "…"}, "experience": ["…"]}``) comes first, also
    inside a code fence or between two sentences of chatter; when there is none, lines ``N1：…`` (and a heading
    ``经验`` followed by lines) are read instead.  Only the other seats count; everything else is dropped."""
    try:
        return _parse_review_output(text, ctx)
    except RecursionError:  # (nothing in the answer may make the review fail in a way that is not a plain "unusable answer")
        return None


def _parse_review_output(text: str, ctx: MemoryContext) -> ReviewOutput | None:
    data = _json_object(text)
    if data is not None:
        raw_impressions = _first_key(data, _IMPRESSION_KEYS)
        raw_experience = _first_key(data, _EXPERIENCE_KEYS)
        if isinstance(raw_impressions, (dict, list)) or isinstance(raw_experience, (list, str)):
            impressions: dict[str, str] = {}
            usable = False  # something of the right shape came out (a seat that is told "no change" counts: it is an answer)
            if isinstance(raw_impressions, dict):
                for key, value in raw_impressions.items():
                    sid = _seat_of(key)
                    if sid in ctx.others and isinstance(value, str):
                        usable = True
                        cleaned = clean_impression(_unmark(value))
                        if cleaned and not is_no_change(cleaned):
                            impressions[sid] = cleaned
            elif isinstance(raw_impressions, list):
                found = parse_impression_lines("\n".join(item for item in raw_impressions if isinstance(item, str)), ctx, unmark=True)
                usable = bool(found)
                impressions = {sid: body for sid, body in found.items() if body and not is_no_change(body)}
            lessons = raw_experience if isinstance(raw_experience, (list, str)) else []
            if isinstance(lessons, str):
                lessons = lessons.splitlines()
            usable = usable or any(isinstance(item, str) for item in lessons)
            experience = [item for item in clean_experience([_unmark(i) if isinstance(i, str) else i for i in lessons]) if not is_no_change(item)]
            # "nothing has changed" is an answer (empty impressions and lessons, or only "…" / "无变化" / "保持原样" in them);
            # a filled-in answer from which nothing of the right shape comes out (only unknown seats, only nested objects,
            # only numbers, ...) is not
            if (raw_impressions or raw_experience) and not usable:
                return None
            return ReviewOutput(impressions, experience or None, "json")
    found_lines = parse_impression_lines(text, ctx, unmark=True)
    lessons_lines = _lines_experience(text)
    if found_lines or lessons_lines:
        lines = {sid: body for sid, body in found_lines.items() if body and not is_no_change(body)}
        experience = [item for item in lessons_lines if not is_no_change(item)]
        return ReviewOutput(lines, experience or None, "lines")
    return None


def merge_review(
    current: MemoryParts,
    output: ReviewOutput,
    ctx: MemoryContext,
    *,
    base: MemoryParts | None = None,
    keep_legacy: bool = False,
) -> MemoryParts:
    """The memory after a review.  ``current`` is the memory as it is now; ``base`` the memory the model was shown (the
    default: the same, nobody could have changed anything meanwhile).  A review takes a while, and the user may edit or
    clear the memory in the meantime — the model's answer must not undo that:

    * an impression / the lessons the model gives back *as it was shown* are no change at all (a prompt allows "copy it"):
      ignored, so they cannot bring back what was edited or cleared since;
    * a new text is taken only when the seat's impression (the lessons) is still what the model was shown; if it was
      edited or cleared meanwhile, what is there now stays;
    * a text that says nothing (``…``, ``无变化``, ``保持原样`` …, see :func:`is_no_change`) and a seat the model did not write
      about change nothing; an unknown id is gone; every text is cut to its limit;
    * the legacy text, which the model was given to tidy up, is deleted - but only when the review really took at least one
      impression or lesson, and not when it was changed meanwhile (``keep_legacy``: a draft is being made, nothing is deleted).

    The record is the engine's and is not touched here."""
    base = current if base is None else base
    after = current.copy()
    adopted = False
    for sid, text in output.impressions.items():
        if sid not in ctx.others:
            continue
        new = _clip(clean_impression(text), IMPRESSION_CHARS)
        if not new or is_no_change(new) or new == base.impressions.get(sid, ""):
            continue
        if current.impressions.get(sid, "") != base.impressions.get(sid, ""):
            continue  # the user changed this one while the model was working: his version stays
        after.impressions[sid] = new
        adopted = True
    lessons = _fit_experience([item for item in clean_experience(output.experience or []) if not is_no_change(item)])
    if lessons and lessons != base.experience and current.experience == base.experience:
        after.experience = lessons
        adopted = True
    if adopted and not keep_legacy and current.legacy == base.legacy:
        after.legacy = ""
    return fit_memory(after, ctx)


# ------------------------------------------------------------------------------- the persona

PERSONA_HEADINGS = ("定位", "说话风格", "思考方式", "决策偏好", "压力下", "口头禅")
PERSONA_LIST_ITEMS = {"说话风格": 3, "思考方式": 3, "决策偏好": 3, "压力下": 2}  # headings that hold lines -> how many
_PERSONA_HEAD_RE = re.compile(r"^[\s#>*_-]*[【\[]\s*(定位|说话风格|思考方式|决策偏好|压力下|口头禅)\s*[】\]][\s*_]*[：:]?\s*(.*)$")
_PERSONA_BARE_HEAD_RE = re.compile(r"^[\s#>*_]*(定位|说话风格|思考方式|决策偏好|压力下|口头禅)[\s*_]*[：:][\s*_]*(.*)$")


_PERSONA_INLINE_HEAD_RE = re.compile(r"[【\[]\s*(?:定位|说话风格|思考方式|决策偏好|压力下|口头禅)\s*[】\]]")


def _strip_bullets(value: str) -> str:
    """Every bullet mark / number in front of a line (``- - x`` -> ``x``): what is rendered as ``- x`` must not start with one,
    or normalizing the draft again would take it off."""
    for _ in range(8):
        stripped = _BULLET_RE.sub("", value, count=1)
        if stripped == value:
            break
        value = stripped
    return value


def _persona_line(value: str) -> str:
    """One line of the persona: no bullet, no markdown, no template heading left inside it (it would be read as a heading
    when the draft is normalized again), cut to ``PERSONA_ITEM_CHARS``."""
    text = _strip_bullets(_unmark(value))
    text = _strip_bullets(_PERSONA_INLINE_HEAD_RE.sub("", text).strip())  # (what stood in front of an inline heading may leave a mark)
    return _clip(_one_line(text), PERSONA_ITEM_CHARS)


def _render_persona(single: dict[str, str], lists: dict[str, list[str]]) -> str:
    lines: list[str] = []
    for heading in PERSONA_HEADINGS:
        if heading in PERSONA_LIST_ITEMS:
            if lists.get(heading):
                lines.append(f"【{heading}】")
                lines.extend(f"- {item}" for item in lists[heading])
        elif single.get(heading):
            lines.append(f"【{heading}】{single[heading]}")
    return "\n".join(lines)


def normalize_persona(text: str) -> str:
    """The moderator model's persona draft -> the fixed format (``""`` when it has none of the headings)::

        【定位】one line
        【说话风格】/【思考方式】/【决策偏好】/【压力下】 followed by ``- …`` lines (3 / 3 / 3 / 2 at most)
        【口头禅】one line (may be left out)

    Everything outside the template is dropped, every line is cut to ``PERSONA_ITEM_CHARS`` and, when the whole is still
    over ``PERSONA_CHARS``, the last lines of the section that has the most of them go first."""
    raw: dict[str, list[str]] = {}
    current: str | None = None
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        match = _PERSONA_HEAD_RE.match(line) or _PERSONA_BARE_HEAD_RE.match(line)
        if match:
            current = match.group(1)
            raw.setdefault(current, [])
            inline = _strip_bullets(match.group(2).strip()).strip()
            if inline:
                raw[current].append(f"- {inline}")  # (what follows a heading on its line is its first line, whatever mark it has)
        elif current is not None:
            raw[current].append(line)
    single: dict[str, str] = {}
    lists: dict[str, list[str]] = {}
    for heading in PERSONA_HEADINGS:
        lines = raw.get(heading, [])
        if heading in PERSONA_LIST_ITEMS:
            bulleted = [item for item in lines if _BULLET_RE.match(item)]  # a bulleted list: the chatter around it is not
            chosen = [_persona_line(item) for item in (bulleted or lines)]
            lists[heading] = [item for item in chosen if item][: PERSONA_LIST_ITEMS[heading]]
        else:
            first = next((_persona_line(item) for item in lines if _persona_line(item)), "")
            single[heading] = first
    text_out = _render_persona(single, lists)
    while len(text_out) > PERSONA_CHARS:
        crowded = [h for h in PERSONA_LIST_ITEMS if len(lists[h]) > 1]
        if crowded:
            target = max(crowded, key=lambda h: (len(lists[h]), PERSONA_HEADINGS.index(h)))
            lists[target].pop()
        elif single.get("口头禅"):
            single["口头禅"] = ""
        else:
            filled = [h for h in PERSONA_LIST_ITEMS if lists[h]]
            if not filled:
                break
            lists[filled[-1]].pop()
        text_out = _render_persona(single, lists)
    return text_out


def persona_for_prompt(text: str) -> str:
    """The persona as the prompt gets it: an old, longer one is cut to the limit (the file is not touched)."""
    value = (text or "").strip()
    return _clip(value, PERSONA_CHARS)


class RecordTally:
    """The engine-kept ``## 战绩`` section: a few counters rendered as text and parsed back."""

    HEADER = "场次 {games}，胜 {wins}，负 {losses}，胜率 {rate}"

    def __init__(self, games: int = 0, wins: int = 0, by_role: dict[str, list[int]] | None = None) -> None:
        self.games = games
        self.wins = wins
        self.by_role: dict[str, list[int]] = by_role or {}  # role label -> [games, wins]

    @classmethod
    def parse(cls, text: str) -> "RecordTally":
        tally = cls()
        header = re.search(r"场次\s*(\d+)，胜\s*(\d+)", text or "")
        if header:
            tally.games, tally.wins = int(header.group(1)), int(header.group(2))
        for match in re.finditer(r"^- (.+?)：(\d+) 场，胜 (\d+)\s*$", text or "", re.M):
            tally.by_role[match.group(1)] = [int(match.group(2)), int(match.group(3))]
        return tally

    def add(self, role: str, won: bool) -> None:
        self.games += 1
        self.wins += 1 if won else 0
        games, wins = self.by_role.get(role, [0, 0])
        self.by_role[role] = [games + 1, wins + (1 if won else 0)]

    def render(self) -> str:
        losses = self.games - self.wins
        rate = f"{self.wins / self.games:.0%}" if self.games else "—"
        lines = [self.HEADER.format(games=self.games, wins=self.wins, losses=losses, rate=rate)]
        for role in sorted(self.by_role):
            games, wins = self.by_role[role]
            lines.append(f"- {role}：{games} 场，胜 {wins}")
        return "\n".join(lines)


# -------------------------------------------------------------------------------------- the store


class RosterStore:
    def __init__(self, data_dir: Path, *, seats: int = 12) -> None:
        self._data_dir = Path(data_dir).resolve()
        self._dir = self._data_dir / "roster"
        self._seats = max(1, int(seats))
        self._lock = threading.RLock()

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def seats(self) -> int:
        return self._seats

    # --------------------------------------------------------------------------- low level
    def _path(self, name: str) -> Path:
        return self._dir / name

    def _read_json(self) -> dict[str, Any]:
        path = self._path(ROSTER_FILE)
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))  # (an editor may have put a byte-order mark in front)
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_json(self, data: dict[str, Any]) -> None:
        atomic_write(self._path(ROSTER_FILE), json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))

    def _read_text(self, name: str) -> str:
        """A persona / memory file.  ``utf-8-sig``: a byte-order mark (Notepad and friends write one) is not part of the text -
        with it ``## 战绩`` on the first line is not a heading and the record is lost at the next review."""
        try:
            return self._path(name).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return ""

    def _write_text(self, name: str, text: str) -> None:
        target = self._path(name)
        if not text.strip():
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                raise StorageError("Unable to remove a roster file") from exc
            return
        atomic_write(target, (text.rstrip("\n") + "\n").encode("utf-8"))

    # --------------------------------------------------------------------------- reading
    def _seat_ids(self) -> list[str]:
        return [seat_id(i) for i in range(1, self._seats + 1)]

    def _moderator_entry(self, raw: dict[str, Any]) -> dict[str, Any]:
        row = raw.get("moderator") if isinstance(raw.get("moderator"), dict) else {}
        avatar = row.get("avatar") if isinstance(row.get("avatar"), dict) else None
        return {"id": MODERATOR_ID, "name": MODERATOR_NAME, "avatar": avatar}

    def _names(self, raw: dict[str, Any]) -> dict[str, str]:
        """The current name of every seat (a missing or broken one falls back to the default)."""
        stored = raw.get("seats") if isinstance(raw.get("seats"), dict) else {}
        names: dict[str, str] = {}
        for index, sid in enumerate(self._seat_ids(), start=1):
            row = stored.get(sid) if isinstance(stored.get(sid), dict) else {}
            name = row.get("name")
            try:
                name = clean_name(name, allow_colon=True) if name else default_name(index)  # (a stored name is shown as it is)
            except RosterError:
                name = default_name(index)
            names[sid] = name
        return names

    def _memory_context(self, owner: str, names: dict[str, str]) -> MemoryContext:
        return MemoryContext(owner=owner, ids=tuple(self._seat_ids()), names=names)

    def _load_memory(self, agent_id: str, names: dict[str, str]) -> tuple[MemoryContext, MemoryParts]:
        """The memory of a seat as it reads *now*: an old file is migrated (in memory, nothing is written), a file that is
        too long is cut to the limits, so what comes out can always be saved again."""
        ctx = self._memory_context(agent_id, names)
        return ctx, fit_memory(parse_memory(self._read_text(f"{agent_id}.memory.md"), ctx), ctx)

    def memory_context(self, agent_id: str) -> MemoryContext:
        """Who ``agent_id`` is for the memory functions (its own id, all the seats, the current names)."""
        with self._lock:
            self._check_id(agent_id)
            return self._memory_context(agent_id, self._names(self._read_json()))

    def load_memory(self, agent_id: str) -> tuple[MemoryContext, MemoryParts]:
        with self._lock:
            self._check_id(agent_id)
            return self._load_memory(agent_id, self._names(self._read_json()))

    def _seat_rows(self, raw: dict[str, Any]) -> list[tuple[dict[str, Any], MemoryContext, MemoryParts]]:
        stored = raw.get("seats") if isinstance(raw.get("seats"), dict) else {}
        names = self._names(raw)
        rows = []
        for index, sid in enumerate(self._seat_ids(), start=1):
            row = stored.get(sid) if isinstance(stored.get(sid), dict) else {}
            avatar = row.get("avatar") if isinstance(row.get("avatar"), dict) else None
            ctx, parts = self._load_memory(sid, names)
            entry = {
                "seat": index,
                "id": sid,
                "name": names[sid],
                "default_name": default_name(index),
                "avatar": avatar,
                "persona": self._read_text(f"{sid}.persona.md").strip("\n"),
                "memory": render_memory(parts, ctx),
                "memory_parts": parts.view(),
            }
            rows.append((entry, ctx, parts))
        return rows

    def get(self) -> dict[str, Any]:
        """``{"memory_enabled": bool, "limits": {…}, "seats": [entry…], "moderator": {id, name, avatar}}`` with defaults
        filled in (nothing is written).  An entry's ``memory`` is the memory in its canonical form and ``memory_parts``
        the same, structured (``record`` / ``impressions`` of the other seats / ``experience`` / ``legacy``); a file
        written by v0.13.0 is shown migrated.  The moderator only has an avatar to choose (name and texts are fixed)."""
        with self._lock:
            raw = self._read_json()
            return {
                "memory_enabled": bool(raw.get("memory_enabled", True)),
                "limits": dict(LIMITS),
                "seats": [entry for entry, _ctx, _parts in self._seat_rows(raw)],
                "moderator": self._moderator_entry(raw),
            }

    def entry(self, agent_id: str) -> dict[str, Any]:
        """One seat (or the moderator: ``{id, name, avatar}``)."""
        data = self.get()
        if agent_id == MODERATOR_ID:
            return data["moderator"]
        for row in data["seats"]:
            if row["id"] == agent_id:
                return row
        raise RosterError(f"没有这个座位：{agent_id}")

    def snapshot(self) -> dict[str, Any]:
        """What a starting game copies: names, avatars, persona and memory as the prompts get them (an old, too long
        persona is cut; the memory is what there is, without the empty rows, and only when it is enabled)."""
        with self._lock:
            raw = self._read_json()
            enabled = bool(raw.get("memory_enabled", True))
            seats = []
            for entry, ctx, parts in self._seat_rows(raw):
                seat = {key: value for key, value in entry.items() if key != "memory_parts"}
                seat["persona"] = persona_for_prompt(entry["persona"])
                seat["memory"] = memory_for_prompt(parts, ctx) if enabled else ""
                seats.append(seat)
            return {"memory_enabled": enabled, "seats": seats, "moderator": self._moderator_entry(raw)}

    # --------------------------------------------------------------------------- writing
    def ensure_avatars(self, make_avatar: Any) -> bool:
        """Give every seat — and the moderator — that has no avatar one
        (``make_avatar(seat:int, taken:list[dict]) -> dict``; the moderator is seat ``0``, picked last so that
        it differs from every player)."""
        with self._lock:
            raw = self._read_json()
            seats = raw.get("seats") if isinstance(raw.get("seats"), dict) else {}
            taken = [row["avatar"] for row in seats.values() if isinstance(row, dict) and isinstance(row.get("avatar"), dict)]
            changed = False
            for index, sid in enumerate(self._seat_ids(), start=1):
                row = seats.get(sid) if isinstance(seats.get(sid), dict) else {}
                if not isinstance(row.get("avatar"), dict):
                    avatar = make_avatar(index, taken)
                    taken.append(avatar)
                    seats[sid] = {**row, "avatar": avatar}
                    changed = True
            if self._moderator_entry(raw)["avatar"] is None:
                avatar = make_avatar(0, taken)
                raw["moderator"] = {**(raw.get("moderator") if isinstance(raw.get("moderator"), dict) else {}), "avatar": avatar}
                changed = True
            if changed:
                raw["seats"] = seats
                raw.setdefault("memory_enabled", True)
                self._write_json(raw)
            return changed

    def _save_seat_fields(self, agent_id: str, **fields: Any) -> None:
        raw = self._read_json()
        seats = raw.get("seats") if isinstance(raw.get("seats"), dict) else {}
        row = dict(seats.get(agent_id) or {})
        row.update(fields)
        seats[agent_id] = row
        raw["seats"] = seats
        raw.setdefault("memory_enabled", True)
        self._write_json(raw)

    def validate_name(self, agent_id: str, name: Any) -> str:
        """The name as it will be stored; :class:`RosterError` when it is not allowed (empty, too long, a forbidden character
        - a colon too, see :func:`clean_name` -, not UTF-8 encodable, or used by another seat), writing nothing."""
        with self._lock:
            self._check_id(agent_id)
            value = clean_name(name)
            for sid, taken in self._names(self._read_json()).items():
                if sid != agent_id and taken.casefold() == value.casefold():
                    raise RosterError(f"名字「{value}」已被 {sid} 使用，名字不能重复。")
            return value

    def set_name(self, agent_id: str, name: Any) -> None:
        with self._lock:
            self._save_seat_fields(agent_id, name=self.validate_name(agent_id, name))

    def _put_avatar(self, raw: dict[str, Any], agent_id: str, avatar: dict[str, Any]) -> None:
        """Set the avatar of a seat or of the moderator inside ``raw`` (not yet written)."""
        if agent_id == MODERATOR_ID:
            raw["moderator"] = {**(raw.get("moderator") if isinstance(raw.get("moderator"), dict) else {}), "avatar": avatar}
            return
        self._check_id(agent_id)
        seats = raw.get("seats") if isinstance(raw.get("seats"), dict) else {}
        seats[agent_id] = {**(seats.get(agent_id) or {}), "avatar": avatar}
        raw["seats"] = seats

    def set_avatar(self, agent_id: str, avatar: dict[str, Any]) -> None:
        """The avatar of a seat or of the moderator (``"moderator"``)."""
        with self._lock:
            if agent_id != MODERATOR_ID:
                self._check_id(agent_id)
            if not isinstance(avatar, dict) or not avatar:
                raise RosterError("头像描述无效。")
            raw = self._read_json()
            self._put_avatar(raw, agent_id, avatar)
            raw.setdefault("memory_enabled", True)
            self._write_json(raw)

    def set_all_avatars(self, avatars: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            raw = self._read_json()
            for agent_id, avatar in avatars.items():
                self._put_avatar(raw, agent_id, avatar)
            raw.setdefault("memory_enabled", True)
            self._write_json(raw)

    def set_persona(self, agent_id: str, text: Any) -> None:
        """Save a persona the user wrote (free text, at most ``PERSONA_CHARS``; longer is refused, nothing is cut)."""
        with self._lock:
            self._check_id(agent_id)
            self._write_text(f"{agent_id}.persona.md", clean_text(text, "性格设定", limit=PERSONA_CHARS))

    def _memory_from_text(self, agent_id: str, text: Any) -> str:
        """The canonical memory file for a whole text the user wrote: parsed (an old-format or free text becomes the
        legacy section), checked against the limits (too long: :class:`RosterError` naming the part)."""
        ctx = self._memory_context(agent_id, self._names(self._read_json()))
        parts = parse_memory(clean_text(text, "记忆"), ctx)
        check_memory(parts, ctx)
        return render_memory(parts, ctx)

    def _memory_from_parts(
        self,
        agent_id: str,
        impressions: dict[str, str] | None,
        experience: list[str] | None,
        legacy: str | None,
    ) -> str:
        """The canonical memory file after changing some parts of the current one (see :meth:`set_memory_parts`)."""
        ctx, parts = self._load_memory(agent_id, self._names(self._read_json()))
        for text in [*(impressions or {}).values(), *(experience or []), *([legacy] if legacy is not None else [])]:
            check_text(str(text), "记忆")  # (a NUL or a lone surrogate would fail at the write, after other parts were saved)
        for key in impressions or {}:
            check_text(str(key), "座位号")  # (the key is echoed in the message of an unknown seat: it must be text that can be sent)
        for sid, text in (impressions or {}).items():
            if sid == agent_id:
                raise RosterInputError(f"不能给自己（{sid}）写印象。")
            if sid not in ctx.ids:
                raise RosterInputError(f"没有这个座位：{sid}")
            parts.impressions[sid] = clean_impression(text)
        if experience is not None:
            parts.experience = clean_experience(experience)
        if legacy is not None:
            text = clean_text(legacy, "旧记忆")
            # A heading-like line (``## 印象``, ``## 战绩`` ...) in it would be a *section* when the file is read again and
            # overwrite the impressions or the record; text that came from a v0.13.0 file never has one (the sections of that
            # file ended at every such line), so refusing them loses nothing and a read-then-save of the text is unchanged.
            for line in text.splitlines():
                if _HEADING_RE.match(line):
                    raise RosterError(f"旧记忆里不能有以“## ”开头的行（会被当成小节标题）：{line.strip()[:24]}")
            parts.legacy = text.strip()
        check_memory(parts, ctx)
        return render_memory(parts, ctx)

    def set_memory(self, agent_id: str, text: Any) -> None:
        """Save a whole memory text (parsed, checked, written in the canonical form).  The ``## 战绩`` section is kept as it
        is written; an old-format or free text becomes the legacy section."""
        with self._lock:
            self._check_id(agent_id)
            self._write_text(f"{agent_id}.memory.md", self._memory_from_text(agent_id, text))

    def set_memory_parts(
        self,
        agent_id: str,
        *,
        impressions: dict[str, str] | None = None,
        experience: list[str] | None = None,
        legacy: str | None = None,
    ) -> None:
        """Change some parts of a memory and keep the rest: the impressions given (seat id -> text, ``""`` = none), the
        whole list of lessons, the legacy text (``""`` deletes it).  An impression of oneself or of a seat that does not
        exist is a :class:`RosterInputError`; too long is a :class:`RosterError` (nothing is cut)."""
        with self._lock:
            self._check_id(agent_id)
            self._write_text(f"{agent_id}.memory.md", self._memory_from_parts(agent_id, impressions, experience, legacy))

    def validate_texts(
        self,
        agent_id: str,
        *,
        persona: Any = None,
        memory: Any = None,
        memory_parts: dict[str, Any] | None = None,
    ) -> None:
        """Raise what :meth:`set_persona`, :meth:`set_memory` / :meth:`set_memory_parts` would raise, writing nothing: a
        request that changes several things can be refused as a whole before anything is applied."""
        with self._lock:
            self._check_id(agent_id)
            if persona is not None:
                clean_text(persona, "性格设定", limit=PERSONA_CHARS)
            if memory is not None:
                self._memory_from_text(agent_id, memory)
            if memory_parts is not None:
                self._memory_from_parts(
                    agent_id, memory_parts.get("impressions"), memory_parts.get("experience"), memory_parts.get("legacy")
                )

    def set_memory_enabled(self, enabled: bool) -> None:
        with self._lock:
            raw = self._read_json()
            raw["memory_enabled"] = bool(enabled)
            self._write_json(raw)

    def _check_id(self, agent_id: str) -> None:
        if agent_id not in self._seat_ids():
            raise RosterError(f"没有这个座位：{agent_id}")

    # --------------------------------------------------------------------------- bulk memory
    def clear(self, *, include_persona: bool = False) -> int:
        """Empty every seat's memory (and persona when asked); returns how many files were removed."""
        with self._lock:
            removed = 0
            for sid in self._seat_ids():
                for kind in ("memory", *(("persona",) if include_persona else ())):
                    path = self._path(f"{sid}.{kind}.md")
                    if path.exists():
                        try:
                            path.unlink()
                        except OSError as exc:
                            raise StorageError("Unable to clear a roster file") from exc
                        removed += 1
            return removed

    # --------------------------------------------------------------------------- review
    def apply_review(
        self,
        agent_id: str,
        *,
        role: str,
        won: bool | None,
        impressions: dict[str, str] | None = None,
        experience: list[str] | None = None,
        base: MemoryParts | None = None,
    ) -> AppliedReview:
        """Record a finished game for one seat (G39, G40): the ``## 战绩`` counters count it (``won=None``, an outcome
        nobody knows, leaves the record alone), the impressions the model wrote replace the old ones (a seat it left out
        keeps its impression, an unknown id is dropped, every text is cut to its limit), new lessons replace the old
        ones, the legacy text — which the model was given to tidy up — is deleted.  The file is overwritten, never
        appended to.  ``base`` is the memory the model was shown: the review took a while and the memory may have been
        edited or cleared meanwhile, and what is there now is merged with the answer by the rules of
        :func:`merge_review` (the user's edit wins, what the model only gave back as it was shown changes nothing).
        Returns the new text and what changed (against what was there when the review was applied)."""
        with self._lock:
            self._check_id(agent_id)
            ctx, current = self._load_memory(agent_id, self._names(self._read_json()))
            output = ReviewOutput(dict(impressions or {}), list(experience) if experience else None, "merge")
            merged = merge_review(current, output, ctx, base=base)
            if won is not None:
                tally = RecordTally.parse(merged.record)
                tally.add(role or "未知身份", won)
                merged.record = tally.render()
            text = render_memory(merged, ctx)
            self._write_text(f"{agent_id}.memory.md", text)
            return AppliedReview(text, memory_changes(current, merged))


_store: RosterStore | None = None
_store_lock = threading.Lock()


def get_roster_store(seats: int = 12) -> RosterStore:
    global _store
    with _store_lock:
        data_dir = Path(get_data_dir()).resolve()
        if _store is None or _store.data_dir != data_dir or _store.seats != seats:
            ensure_private_directory(data_dir / "roster")
            _store = RosterStore(data_dir, seats=seats)
        return _store


def reset_roster_store() -> None:
    global _store
    with _store_lock:
        _store = None


__all__ = [
    "AppliedReview",
    "DEFAULT_NAMES",
    "EXPERIENCE_CHARS",
    "EXPERIENCE_ITEMS",
    "IMPRESSION_CHARS",
    "LEGACY_CHARS",
    "LIMITS",
    "MEMORY_CHARS",
    "MODERATOR_ID",
    "MODERATOR_NAME",
    "MemoryContext",
    "MemoryParts",
    "PERSONA_CHARS",
    "PERSONA_ITEM_CHARS",
    "RECORD_PROMPT_CHARS",
    "RecordTally",
    "ReviewOutput",
    "RosterError",
    "RosterInputError",
    "RosterStore",
    "check_memory",
    "check_text",
    "clean_experience",
    "clean_impression",
    "clean_name",
    "clean_text",
    "default_name",
    "fit_memory",
    "get_roster_store",
    "is_no_change",
    "join_memory",
    "memory_body",
    "memory_changes",
    "memory_chars",
    "memory_for_prompt",
    "merge_review",
    "normalize_persona",
    "parse_impression_lines",
    "parse_memory",
    "parse_review_output",
    "persona_for_prompt",
    "render_memory",
    "reset_roster_store",
    "seat_id",
    "split_memory",
]
