"""The roster: who sits at the table (v0.12.1, E13–E16, E21, E24).

One entry per seat: an id (``N1``…), a name (default Alice, Bob, …; the user may rename), a pixel-avatar
description, and two text files — a *persona* ("how this player talks and thinks") and a *memory* ("what
this player remembers about the others and about his own experience").  Everything lives in plain files
in the data directory so it can be edited outside the app::

    <data>/roster/roster.json          names, avatars, the memory switch
    <data>/roster/N1.persona.md        free text, empty by default
    <data>/roster/N1.memory.md         "## 战绩" (engine-kept) / "## 对局记忆" / "## 经验与印象"

The roster is a *template* for the next game: ``snapshot()`` copies it into the game when it starts, so
changing a name or a memory never affects a game in progress and the system prompts stay cacheable.
"""

from __future__ import annotations

import io
import json
import re
import threading
import zipfile
from pathlib import Path
from typing import Any

from app.storage._atomic import atomic_write, ensure_private_directory
from app.storage.errors import StorageError
from app.storage.paths import get_data_dir

DEFAULT_NAMES = ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo"]
MODERATOR_ID = "moderator"  # the moderator is not a seat: it only has a (random) avatar of its own
MODERATOR_NAME = "主持人"
MAX_NAME_CHARS = 20
MAX_TEXT_CHARS = 20_000  # persona / memory
MAX_ZIP_BYTES = 2 * 1024 * 1024
MAX_ZIP_MEMBERS = 80
ROSTER_FILE = "roster.json"
SECTION_RECORD = "战绩"
SECTION_MEMORY = "对局记忆"
SECTION_EXPERIENCE = "经验与印象"
SECTIONS = (SECTION_RECORD, SECTION_MEMORY, SECTION_EXPERIENCE)
_NAME_BAD = re.compile(r"[\x00-\x1f\x7f<>\"'`\\]")
_ID_RE = re.compile(r"^N([1-9]|[1-9][0-9])$")
_ZIP_NAME_RE = re.compile(r"^N([1-9]|[1-9][0-9])\.(persona|memory)\.md$")


class RosterError(ValueError):
    """A roster change is invalid (the message is safe to show to the user)."""


def seat_id(seat: int) -> str:
    return f"N{int(seat)}"


def default_name(seat: int) -> str:
    return DEFAULT_NAMES[(seat - 1) % len(DEFAULT_NAMES)] if seat <= len(DEFAULT_NAMES) else f"玩家{seat}"


def clean_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise RosterError("名字不能为空。")
    if len(text) > MAX_NAME_CHARS:
        raise RosterError(f"名字最多 {MAX_NAME_CHARS} 个字符。")
    if _NAME_BAD.search(text):
        raise RosterError("名字不能包含控制字符、引号、反斜杠或尖括号。")
    return text


def clean_text(value: Any, what: str) -> str:
    text = str(value if value is not None else "").replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_TEXT_CHARS:
        raise RosterError(f"{what}最多 {MAX_TEXT_CHARS} 个字符。")
    if "\x00" in text:
        raise RosterError(f"{what}不能包含空字符。")
    return text.strip("\n") if text.strip() else ""


# ------------------------------------------------------------------------------- memory sections


def split_memory(text: str) -> dict[str, str]:
    """``{section: body}`` of a memory file; text before the first ``## `` heading belongs to the
    experience section (a hand-written file without headings still works)."""
    sections: dict[str, list[str]] = {name: [] for name in SECTIONS}
    current = SECTION_EXPERIENCE
    extra: dict[str, list[str]] = {}
    for line in (text or "").splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            title = match.group(1)
            current = title
            if title not in sections:
                extra.setdefault(title, [])
            continue
        (sections.get(current) if current in sections else extra.setdefault(current, [])).append(line)
    out = {name: "\n".join(lines).strip("\n") for name, lines in sections.items()}
    out.update({name: "\n".join(lines).strip("\n") for name, lines in extra.items()})
    return out


def join_memory(sections: dict[str, str]) -> str:
    order = [*SECTIONS, *[k for k in sections if k not in SECTIONS]]
    parts = []
    for name in order:
        body = (sections.get(name) or "").strip("\n")
        if body.strip():
            parts.append(f"## {name}\n{body}")
    return "\n\n".join(parts)


def memory_for_prompt(text: str) -> str:
    """The memory as the agent sees it (record, then what it remembers, then experience)."""
    return join_memory(split_memory(text)) if (text or "").strip() else ""


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
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_json(self, data: dict[str, Any]) -> None:
        atomic_write(self._path(ROSTER_FILE), json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))

    def _read_text(self, name: str) -> str:
        try:
            return self._path(name).read_text(encoding="utf-8")
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

    def get(self) -> dict[str, Any]:
        """``{"memory_enabled": bool, "seats": [entry…], "moderator": {id, name, avatar}}`` with defaults filled
        in (nothing is written).  The moderator only has an avatar to choose (name and texts are fixed)."""
        with self._lock:
            raw = self._read_json()
            stored = raw.get("seats") if isinstance(raw.get("seats"), dict) else {}
            seats = []
            for index, sid in enumerate(self._seat_ids(), start=1):
                row = stored.get(sid) if isinstance(stored.get(sid), dict) else {}
                name = row.get("name")
                try:
                    name = clean_name(name) if name else default_name(index)
                except RosterError:
                    name = default_name(index)
                avatar = row.get("avatar") if isinstance(row.get("avatar"), dict) else None
                seats.append(
                    {
                        "seat": index,
                        "id": sid,
                        "name": name,
                        "default_name": default_name(index),
                        "avatar": avatar,
                        "persona": self._read_text(f"{sid}.persona.md").strip("\n"),
                        "memory": self._read_text(f"{sid}.memory.md").strip("\n"),
                    }
                )
            return {
                "memory_enabled": bool(raw.get("memory_enabled", True)),
                "seats": seats,
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
        """What a starting game copies: names, avatars, persona and memory (memory only when enabled)."""
        data = self.get()
        enabled = data["memory_enabled"]
        return {
            "memory_enabled": enabled,
            "seats": [
                {
                    **row,
                    "memory": memory_for_prompt(row["memory"]) if enabled else "",
                }
                for row in data["seats"]
            ],
            "moderator": data["moderator"],
        }

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

    def set_name(self, agent_id: str, name: Any) -> None:
        with self._lock:
            self._check_id(agent_id)
            value = clean_name(name)
            for row in self.get()["seats"]:
                if row["id"] != agent_id and row["name"].casefold() == value.casefold():
                    raise RosterError(f"名字「{value}」已被 {row['id']} 使用，名字不能重复。")
            self._save_seat_fields(agent_id, name=value)

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
        with self._lock:
            self._check_id(agent_id)
            self._write_text(f"{agent_id}.persona.md", clean_text(text, "性格设定"))

    def set_memory(self, agent_id: str, text: Any) -> None:
        with self._lock:
            self._check_id(agent_id)
            self._write_text(f"{agent_id}.memory.md", clean_text(text, "记忆"))

    def set_memory_enabled(self, enabled: bool) -> None:
        with self._lock:
            raw = self._read_json()
            raw["memory_enabled"] = bool(enabled)
            self._write_json(raw)

    def _check_id(self, agent_id: str) -> None:
        if agent_id not in self._seat_ids():
            raise RosterError(f"没有这个座位：{agent_id}")

    # --------------------------------------------------------------------------- bulk memory
    def export_zip(self) -> bytes:
        """Every persona / memory file as a zip (plain text, ``N1.persona.md`` …)."""
        with self._lock:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for row in self.get()["seats"]:
                    for kind in ("persona", "memory"):
                        if row[kind].strip():
                            archive.writestr(f"{row['id']}.{kind}.md", row[kind] + "\n")
            return buffer.getvalue()

    def parse_zip(self, data: bytes) -> dict[str, dict[str, str]]:
        """``{agent_id: {"persona"?: text, "memory"?: text}}`` of a memory pack; raises ``RosterError`` for a
        pack that is too large, not a zip or holds anything but ``N<k>.persona.md`` / ``N<k>.memory.md``."""
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise RosterError("记忆包是空的。")
        if len(data) > MAX_ZIP_BYTES:
            raise RosterError("记忆包过大（上限 2 MB）。")
        try:
            archive = zipfile.ZipFile(io.BytesIO(bytes(data)))
        except zipfile.BadZipFile:
            raise RosterError("这不是有效的 zip 文件。") from None
        out: dict[str, dict[str, str]] = {}
        with archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_MEMBERS:
                raise RosterError("记忆包里的文件太多。")
            total = 0
            for info in infos:
                if info.is_dir():
                    continue
                match = _ZIP_NAME_RE.match(info.filename)
                if match is None:
                    raise RosterError(f"记忆包里有不认识的文件：{info.filename[:60]}（只接受 N1.persona.md / N1.memory.md 这样的文件名）。")
                if info.file_size > MAX_TEXT_CHARS * 4:
                    raise RosterError(f"{info.filename} 太大。")
                total += info.file_size
                if total > MAX_ZIP_BYTES:
                    raise RosterError("记忆包解压后过大。")
                try:
                    text = archive.read(info).decode("utf-8")
                except UnicodeDecodeError:
                    raise RosterError(f"{info.filename} 不是 UTF-8 文本。") from None
                seat = f"N{match.group(1)}"
                if seat not in self._seat_ids():
                    continue  # a pack of a bigger table: the extra seats are ignored
                kind = match.group(2)
                out.setdefault(seat, {})[kind] = clean_text(text, "记忆包里的文本")
        return out

    def preview_import(self, data: bytes) -> list[dict[str, Any]]:
        """What importing ``data`` would overwrite (``dry_run``)."""
        pack = self.parse_zip(data)
        current = {row["id"]: row for row in self.get()["seats"]}
        changes = []
        for agent_id in self._seat_ids():
            if agent_id not in pack:
                continue
            row = current[agent_id]
            persona = "persona" in pack[agent_id] and pack[agent_id]["persona"] != row["persona"]
            memory = "memory" in pack[agent_id] and pack[agent_id]["memory"] != row["memory"]
            if persona or memory:
                changes.append({"id": agent_id, "name": row["name"], "persona": persona, "memory": memory})
        return changes

    def import_zip(self, data: bytes) -> list[dict[str, Any]]:
        """Load a memory pack: files present in it replace the seat's text, others are left alone."""
        with self._lock:
            changes = self.preview_import(data)
            pack = self.parse_zip(data)
            for change in changes:
                if change["persona"]:
                    self.set_persona(change["id"], pack[change["id"]]["persona"])
                if change["memory"]:
                    self.set_memory(change["id"], pack[change["id"]]["memory"])
            return changes

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
    def apply_review(self, agent_id: str, *, role: str, won: bool, review_text: str) -> str:
        """Record a finished game for one seat: update the ``## 战绩`` counters and append the player's own
        review to ``## 对局记忆``.  Returns the new memory text."""
        with self._lock:
            self._check_id(agent_id)
            sections = split_memory(self._read_text(f"{agent_id}.memory.md"))
            tally = RecordTally.parse(sections.get(SECTION_RECORD, ""))
            tally.add(role or "未知身份", won)
            sections[SECTION_RECORD] = tally.render()
            review = clean_text(review_text, "复盘")
            if review:
                previous = sections.get(SECTION_MEMORY, "").strip("\n")
                sections[SECTION_MEMORY] = (previous + "\n\n" if previous else "") + review
            text = join_memory(sections)
            if len(text) > MAX_TEXT_CHARS:
                # keep the record and the newest reviews: drop the oldest paragraphs of the game memory
                paragraphs = sections.get(SECTION_MEMORY, "").split("\n\n")
                while len(text) > MAX_TEXT_CHARS and len(paragraphs) > 1:
                    paragraphs.pop(0)
                    sections[SECTION_MEMORY] = "\n\n".join(paragraphs)
                    text = join_memory(sections)
                text = text[-MAX_TEXT_CHARS:]
            self._write_text(f"{agent_id}.memory.md", text)
            return text


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
    "DEFAULT_NAMES",
    "MODERATOR_ID",
    "MODERATOR_NAME",
    "RecordTally",
    "RosterError",
    "RosterStore",
    "clean_name",
    "default_name",
    "get_roster_store",
    "join_memory",
    "memory_for_prompt",
    "reset_roster_store",
    "seat_id",
    "split_memory",
]
