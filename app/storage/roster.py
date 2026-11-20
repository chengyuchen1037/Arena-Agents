"""What is left of the roster (v0.12.1 - v0.13.1) in v0.14: the way *into* the bots, and the rule for a name.

The roster - one entry per seat (``N1``…): a name, an avatar, a *persona* and a *memory* in plain files under ``<data>/roster/`` - is gone:
v0.14 keeps those players as **bots** (``app.storage.bots``, ``<data>/bots/``; docs/v0.14-plan.md H1, H45, H55).  No code writes a roster file
any more.  What stays is what the bots still need of it::

    <data>/roster/roster.json          names, avatars, the memory switch, the moderator's avatar
    <data>/roster/N1.persona.md        the persona (a fixed format of headings: 【定位】 【说话风格】 ...)
    <data>/roster/N1.memory.md         "## 战绩" / "## 印象" / "## 经验" (v0.13.0: "## 对局记忆" / "## 经验与印象")

* :func:`read_legacy_roster` reads such a directory - read only, lenient - into a :class:`LegacyRoster`, which
  :meth:`app.storage.bots.BotStore.import_legacy_roster` turns into bots (the first start of v0.14 on a v0.13 data directory, and the import of a
  v0.13 archive); :func:`parse_legacy_persona` splits a persona by its headings; :func:`parse_memory` / :func:`fit_memory` read a memory file
  (the impressions of the other seats, the lessons, the record, what v0.13.0 wrote as free text).  Nothing here is ever written back.
* :func:`clean_name` is the rule for the name of a player (1-20 characters, no control character, quote, backslash, angle bracket or colon) and
  :func:`check_text` the rule for any text that goes into a file (no NUL, no lone surrogate); the limits of the impressions
  (:func:`clean_impression`) and the record (:class:`RecordTally`) are used by the files of a bot (``app.storage.botfiles``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_NAMES = ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo"]
MAX_NAME_CHARS = 20
ROSTER_FILE = "roster.json"

# ---- limits of a memory as v0.13.1 kept it (G38, G43, G44): what the migration cuts a long old memory down to -------------
IMPRESSION_CHARS = 80  # one impression (the text, not its ``- N5 Eve：`` prefix)
EXPERIENCE_ITEMS = 3  # lessons
EXPERIENCE_CHARS = 300  # all the lessons together
MEMORY_CHARS = 1500  # the "## 印象" and "## 经验" sections as written in the file (prefixes, bullets and headings count)
LEGACY_CHARS = 600  # the temporary "## 旧记忆（待整理）" section
PERSONA_CHARS = 500

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


class RosterError(ValueError):
    """A name or a text of a player is not acceptable (the message is safe to show to the user)."""


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


_EXPERIENCE_HEAD_RE = re.compile(r"^[#>\s*【\[]*(?:我的)?经验(?:教训|心得|与教训)?\s*[】\]]*\s*(?:[：:]\s*(.*))?$")
_ANY_HEAD_RE = re.compile(r"^(?:#{1,6}\s|[【\[][^】\]]{1,12}[】\]]\s*$)")
_PERSONA_HEAD_RE = re.compile(r"^[\s#>*_-]*[【\[]\s*(定位|说话风格|思考方式|决策偏好|压力下|口头禅)\s*[】\]][\s*_]*[：:]?\s*(.*)$")
_PERSONA_BARE_HEAD_RE = re.compile(r"^[\s#>*_]*(定位|说话风格|思考方式|决策偏好|压力下|口头禅)[\s*_]*[：:][\s*_]*(.*)$")


def _strip_bullets(value: str) -> str:
    """Every bullet mark / number in front of a line (``- - x`` -> ``x``): what is rendered as ``- x`` must not start with one,
    or normalizing the draft again would take it off."""
    for _ in range(8):
        stripped = _BULLET_RE.sub("", value, count=1)
        if stripped == value:
            break
        value = stripped
    return value


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


@dataclass
class LegacySeat:
    """One seat of a v0.13 roster: its name and avatar, its persona text, and its memory file parsed
    (``parts.impressions`` are keyed by the seat id they are about)."""

    seat: int
    id: str
    name: str
    avatar: dict[str, Any] | None
    persona: str
    parts: MemoryParts


@dataclass
class LegacyRoster:
    memory_enabled: bool
    seats: list[LegacySeat]
    moderator_avatar: dict[str, Any] | None

    @property
    def names(self) -> dict[str, str]:
        return {row.id: row.name for row in self.seats}


def read_legacy_roster(roster_dir: Path, seats: int = 12) -> LegacyRoster:
    """A v0.13 roster directory (``roster.json``, ``N<k>.persona.md``, ``N<k>.memory.md``) -> :class:`LegacyRoster`.  Lenient and
    read-only: a missing or broken file is an empty one, a byte-order mark is ignored, a stored name that could not be typed
    today (a colon) is kept as it is, a memory file of the v0.13.0 format is read like the new one (:func:`parse_memory`)."""
    directory = Path(roster_dir)

    def read(name: str) -> str:
        try:
            return (directory / name).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return ""

    try:
        raw = json.loads(read(ROSTER_FILE) or "{}")
    except ValueError:
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    stored = raw.get("seats") if isinstance(raw.get("seats"), dict) else {}
    ids = [seat_id(index) for index in range(1, max(1, int(seats)) + 1)]
    names: dict[str, str] = {}
    avatars: dict[str, dict[str, Any] | None] = {}
    for index, sid in enumerate(ids, start=1):
        row = stored.get(sid) if isinstance(stored.get(sid), dict) else {}
        try:
            names[sid] = clean_name(row.get("name"), allow_colon=True) if row.get("name") else default_name(index)
        except RosterError:
            names[sid] = default_name(index)
        avatar = row.get("avatar")
        avatars[sid] = avatar if isinstance(avatar, dict) and avatar else None
    rows: list[LegacySeat] = []
    for index, sid in enumerate(ids, start=1):
        ctx = MemoryContext(owner=sid, ids=tuple(ids), names=names)
        parts = fit_memory(parse_memory(read(f"{sid}.memory.md"), ctx), ctx)
        rows.append(LegacySeat(index, sid, names[sid], avatars[sid], read(f"{sid}.persona.md").strip("\n"), parts))
    moderator = raw.get("moderator") if isinstance(raw.get("moderator"), dict) else {}
    moderator_avatar = moderator.get("avatar") if isinstance(moderator.get("avatar"), dict) and moderator.get("avatar") else None
    return LegacyRoster(bool(raw.get("memory_enabled", True)), rows, moderator_avatar)


def parse_legacy_persona(text: str) -> dict[str, list[str]]:
    """A v0.13 persona -> ``{heading: [line, ...]}`` for the headings of its fixed format (``【定位】`` ... ``【口头禅】``: what follows
    a heading on its line is its first line, bullets and markdown emphasis are dropped); text before the first heading, or all of a
    free-form persona, is filed under ``""``.  Nothing is cut."""
    sections: dict[str, list[str]] = {}
    current = ""
    for raw in (text or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        match = _PERSONA_HEAD_RE.match(line) or _PERSONA_BARE_HEAD_RE.match(line)
        if match:
            current = match.group(1)
            sections.setdefault(current, [])
            inline = _strip_bullets(_unmark(match.group(2).strip())).strip()
            if inline:
                sections[current].append(_one_line(inline))
            continue
        item = _one_line(_strip_bullets(_unmark(line)))
        if item:
            sections.setdefault(current, []).append(item)
    return sections


__all__ = [
    "DEFAULT_NAMES",
    "MAX_NAME_CHARS",
    "PERSONA_CHARS",
    "LegacyRoster",
    "LegacySeat",
    "MemoryContext",
    "MemoryParts",
    "RecordTally",
    "RosterError",
    "check_text",
    "clean_experience",
    "clean_impression",
    "clean_name",
    "default_name",
    "fit_memory",
    "parse_impression_lines",
    "parse_legacy_persona",
    "parse_memory",
    "read_legacy_roster",
    "seat_id",
    "split_memory",
]
