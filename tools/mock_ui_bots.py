"""The v0.14 bots / contract / rules model of the UI mock (docs/v0.12-contracts.md §13.2, §13.7; botstore's code is the spec).

``tools/mock_ui_server.py`` serves the page and replays one scripted game; this module is the part of it that mirrors what
``app/storage/bots.py`` + ``botfiles.py`` (the bot store: L0, the memory, the freezing rules, the proposals, the ``.arenabot`` file) and
the contract / rules stores of the files stream do.  Pure Python (no FastAPI, no event loop): ``tools/mock_ui_hub.py`` puts it behind the
HTTP API and the events, and ties it to the game.

Everything is in memory; nothing touches the disk.  The limits, the Chinese refusals, the canonical texts (the merged L0 file, the
playbook, the impressions) and the derived states (``stale`` ...) are the real ones, so a page built against the mock sees what the
real server will send.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import random
import re
import time
import unicodedata
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

# --------------------------------------------------------------------------------------------------
# the numbers and names of app/bots/types.py (C15 ...) and BotStore.limits_payload()
# --------------------------------------------------------------------------------------------------

CONTRACT_VERSION = "v0.14.0"
DEFAULT_SCENARIO_ID = "werewolf_12p"
CORE_SECTIONS = ("identity", "voice", "mind")
CORE_HEADINGS = {"identity": "身份", "voice": "说话方式", "mind": "分析方式"}
CORE_LIMITS = {"identity": 300, "voice": 600, "mind": 400}
CORE_EXTRA_MAX = 3
CORE_EXTRA_CHARS = 300
CORE_EXTRA_PREFIX = "附加："
EXTRA_TITLE_CHARS = 20
NAME_CHARS = 20
ARCH_NOTES_CHARS = 800
DIGEST_CHARS = 800
QUESTIONS_CHARS = 400
PLAYBOOK_MAX = 20
PLAYBOOK_ENTRY_CHARS = 80
PEOPLE_MAX = 30
IMPRESSION_CHARS = 80
CONTRACT_MUST_READ_CHARS = 1200
CONTRACT_MAX_CHARS = 20_000
INIT_TIMEOUT_S = 120
REVIEW_TIMEOUT_S = 60
JOT_MAX = 10
JOT_CHARS = 120
MAX_STAGING = 30
MAX_CORE_MARKDOWN_CHARS = 20_000
MAX_PART_INPUT_CHARS = 20_000
MAX_ARENABOT_BYTES = 20 * 1024 * 1024
MEMORY_PARTS = ("arch_notes", "digest", "playbook", "questions", "people")
PART_LABELS = {"arch_notes": "操作笔记", "digest": "规则要点", "playbook": "打法", "questions": "待解问题", "people": "印象"}
MEMORY_PART_LIMITS = {"arch_notes": ARCH_NOTES_CHARS, "digest": DIGEST_CHARS, "questions": QUESTIONS_CHARS}
SCENARIO_PARTS = ("digest", "playbook", "questions")  # the parts of the memory that belong to one scenario
INIT_HASHES = ("core", "contract", "scenario", "materials")
SOURCE_LABELS = {"core": "底层设定", "contract": "官方契约", "scenario": "剧本", "materials": "学习材料"}
WILL_CLEAR = ("初始化结果", "操作笔记", "规则要点", "打法", "待解问题", "印象", "战绩")
RESET_CONFIRM_TEXT = (
    "修改名字或头像会清空这个玩家的初始化结果和全部记忆（操作笔记、规则要点、打法、待解问题、印象、战绩），"
    "底层设定保留。修改后需要重新初始化才能开局。是否确认？"
)
CONFIDENCES = ("低", "中", "高")
AVATAR_FIELDS = ("face", "skin", "hair", "hair_color", "eyes", "mouth", "accessory", "accessory_color")
AVATAR_LETTERS = dict(zip(AVATAR_FIELDS, "fshcemak"))
#: the situation tags of the scenario's ``situations`` (what a playbook entry may be filed under)
SITUATION_TAGS = ("警上", "发言", "投票", "夜晚", "遗言", "狼队", "平票")


def limits_payload() -> dict[str, Any]:
    """``BotsPayload.limits`` (``BotStore.limits_payload()``): the numbers the UI counts against."""
    return {
        "name_chars": NAME_CHARS,
        "core_limits": dict(CORE_LIMITS),
        "core_extra_max": CORE_EXTRA_MAX,
        "core_extra_chars": CORE_EXTRA_CHARS,
        "extra_title_chars": EXTRA_TITLE_CHARS,
        "arch_notes_chars": ARCH_NOTES_CHARS,
        "digest_chars": DIGEST_CHARS,
        "questions_chars": QUESTIONS_CHARS,
        "playbook_max": PLAYBOOK_MAX,
        "playbook_entry_chars": PLAYBOOK_ENTRY_CHARS,
        "people_max": PEOPLE_MAX,
        "impression_chars": IMPRESSION_CHARS,
        "contract_must_read_chars": CONTRACT_MUST_READ_CHARS,
        "init_timeout_s": INIT_TIMEOUT_S,
        "review_timeout_s": REVIEW_TIMEOUT_S,
        "jot_max": JOT_MAX,
        "jot_chars": JOT_CHARS,
        "memory_parts": list(MEMORY_PARTS),
        "part_labels": dict(PART_LABELS),
    }


# --------------------------------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------------------------------


class Refusal(Exception):
    """A request the server refuses: the HTTP ``status`` and the body ``{"detail": "<中文>", "code"?, ...}``."""

    def __init__(self, status: int, detail: str, code: str | None = None, **extra: Any) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code
        self.extra = extra

    @property
    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"detail": self.detail}
        if self.code:
            out["code"] = self.code
        out.update(self.extra)
        return out


class FileProblems(ValueError):
    """A text (a merged L0 file, a memory part ...) that is not acceptable; ``problems`` are Chinese sentences (botfiles.BotFileError)."""

    def __init__(self, problems: Sequence[str] | str) -> None:
        items = [problems] if isinstance(problems, str) else [str(p) for p in problems]
        items = [p for p in items if p] or ["内容无效。"]
        super().__init__(" ".join(items))
        self.problems = items


def problems_detail(prefix: str, exc: FileProblems) -> str:
    """app/api/bots.py ``_problems_detail``: one sentence for a refusal that lists every fault in ``problems``."""
    return f"{prefix}{exc}" if len(exc.problems) == 1 else f"{prefix}共 {len(exc.problems)} 处问题：{exc}"


def not_found(what: str = "没有这个玩家（bot）。") -> Refusal:
    return Refusal(404, what, "not_found")


# --------------------------------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------------------------------


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def shown(value: str, limit: int = 30) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


def normalize_newlines(text: str) -> str:
    return text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def checked_text(value: Any, what: str) -> str:
    text = normalize_newlines("" if value is None else str(value))
    if "\x00" in text:
        raise FileProblems(f"{what}不能包含空字符。")
    return text


def over(label: str, limit: int, now: int, unit: str = "字") -> str:
    return f"「{label}」最多 {limit} {unit}（现在 {now} {unit}）。"


_NAME_BAD = re.compile(r"[\x00-\x1f\x7f<>\"'`\\]")
_COLON = re.compile("[:\uff1a]")


def clean_name(value: Any) -> str:
    """app.storage.roster ``clean_name``: 1-20 characters, no control character, quote, backslash, angle bracket or colon."""
    text = str(value or "").strip()
    if not text:
        raise Refusal(400, "名字不能为空。")
    if len(text) > NAME_CHARS:
        raise Refusal(400, f"名字最多 {NAME_CHARS} 个字符。")
    if _NAME_BAD.search(text):
        raise Refusal(400, "名字不能包含控制字符、引号、反斜杠或尖括号。")
    if _COLON.search(text):
        raise Refusal(400, "名字不能包含冒号（: 或 ：）。")
    return text


def core_hash(core: dict[str, Any]) -> str:
    """``app.bots.types.core_hash``: the L0 text (the three sections stripped, the extras as ``title\\ntext``) joined by newlines."""
    parts = [core["identity"].strip(), core["voice"].strip(), core["mind"].strip()]
    for extra in core["extras"]:  # ((title, text) as the parser makes them, or {"title", "text"} as the bots keep them)
        title, text = (extra["title"], extra["text"]) if isinstance(extra, dict) else extra
        parts.append(f"{title.strip()}\n{text.strip()}")
    return sha256_text("\n".join(parts))


def materials_hash(materials: dict[str, str]) -> str:
    if not materials:
        return ""
    return sha256_text("\n".join(f"{slug}:{sha256_text(text)}" for slug, text in sorted(materials.items())))


def avatar_code(desc: Any) -> str | None:
    """The compact code of an avatar description (contract §12.6): the eight part ids joined; ``None`` when it cannot be encoded."""
    if not isinstance(desc, dict) or not desc:
        return None
    out: list[str] = []
    for field in AVATAR_FIELDS:
        value = desc.get(field)
        if field == "accessory" and value == "none":
            value = "a00"
        if field == "accessory_color" and value in (None, ""):
            value = "k00"
        if not isinstance(value, str) or not value or value[0] != AVATAR_LETTERS[field]:
            return None
        out.append(value)
    return "".join(out)


# --------------------------------------------------------------------------------------------------
# L0: slugs and the merged markdown (botfiles.py)
# --------------------------------------------------------------------------------------------------

_SLUG_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
SLUG_MAX_CHARS = 32


def slug_for_title(title: str, taken: Iterable[str] = (), index: int = 1) -> str:
    used = set(taken)
    base = "".join(ch for ch in str(title).lower() if ch in _SLUG_CHARS).lstrip("-")[:SLUG_MAX_CHARS].rstrip("-")
    if not base:
        base = f"x{max(1, int(index))}"
    slug, number = base, 2
    while slug in used:
        suffix = f"-{number}"
        slug = base[: SLUG_MAX_CHARS - len(suffix)] + suffix
        number += 1
    return slug


def assign_slugs(titles: Sequence[str], existing: dict[str, str] | None = None) -> list[str]:
    existing = existing or {}
    out: list[str | None] = [None] * len(titles)
    taken: set[str] = set()
    for index, title in enumerate(titles):
        slug = existing.get(title.strip().casefold())
        if slug and slug not in taken:
            out[index] = slug
            taken.add(slug)
    for index, title in enumerate(titles):
        if out[index] is None:
            slug = slug_for_title(title, taken, index + 1)
            out[index] = slug
            taken.add(slug)
    return [str(s) for s in out]


_H1_RE = re.compile(r"^ {0,3}#(?!#)(?:[ \t\u3000]+(.*?))?[ \t\u3000]*$")
_EXTRA_HEADING_RE = re.compile(r"^" + re.escape(CORE_EXTRA_PREFIX.rstrip("：:")) + r"\s*[:：]\s*(.*)$")
_KEY_VALUE_RE = re.compile(r"^\s*([^:：]+?)\s*[:：]\s*(.*?)\s*$")
_COMMENT_RE = re.compile(r"(?:^|\s)#")
_VERSION_RE = re.compile(r"^1(?:\.0+)?$")
_HEADING_OF = {heading: key for key, heading in CORE_HEADINGS.items()}


def _header_value(raw: str) -> str:
    match = _COMMENT_RE.search(raw)
    if match:
        raw = raw[: match.start()]
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def name_hint(name: Any) -> str:
    return _header_value(one_line(name))


def _has_heading_line(text: str) -> bool:
    return any(_H1_RE.match(line) for line in text.split("\n"))


def empty_core() -> dict[str, Any]:
    """A core as the parser works with it: ``{"identity", "voice", "mind", "extras": [(title, text), ...]}``."""
    return {"identity": "", "voice": "", "mind": "", "extras": []}


def validate_core(core: dict[str, Any], *, require_complete: bool = False) -> list[str]:
    """Every fault of an L0 as Chinese sentences (``[]`` = fine) - botfiles.validate_core."""
    problems: list[str] = []
    for key in CORE_SECTIONS:
        body = core[key].strip()
        label = CORE_HEADINGS[key]
        if not body:
            if require_complete:
                problems.append(f"「{label}」不能为空。")
        elif len(body) > CORE_LIMITS[key]:
            problems.append(over(label, CORE_LIMITS[key], len(body)))
        elif _has_heading_line(body):
            problems.append(f"「{label}」里不能有以“# ”开头的行（会被当成分区标题），请去掉 # 或改用 ##。")
    extras = list(core["extras"])
    if len(extras) > CORE_EXTRA_MAX:
        problems.append(f"附加分区最多 {CORE_EXTRA_MAX} 份（现在 {len(extras)} 份）。")
    seen: set[str] = set()
    for title, text in extras:
        name = str(title).strip()
        label = f"{CORE_EXTRA_PREFIX}{name}"
        if not name:
            problems.append("附加分区需要标题。")
        elif len(name) > EXTRA_TITLE_CHARS:
            problems.append(f"附加分区的标题最多 {EXTRA_TITLE_CHARS} 字（现在 {len(name)} 字）：{shown(name)}")
        elif "\n" in name:
            problems.append("附加分区的标题只能有一行。")
        elif name.casefold() in seen:
            problems.append(f"附加分区「{name}」重复了。")
        seen.add(name.casefold())
        body = str(text).strip()
        if not body:
            problems.append(f"「{label}」不能为空。")
        elif len(body) > CORE_EXTRA_CHARS:
            problems.append(over(label, CORE_EXTRA_CHARS, len(body)))
        elif _has_heading_line(body):
            problems.append(f"「{label}」里不能有以“# ”开头的行（会被当成分区标题），请去掉 # 或改用 ##。")
    return problems


def _parse_header(lines: list[str]) -> tuple[dict[str, str], list[str]]:
    fields: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _KEY_VALUE_RE.match(line)
        if not match:
            continue
        key = unicodedata.normalize("NFKC", match.group(1)).strip().casefold()
        fields.setdefault(key, match.group(2))
    problems: list[str] = []
    fmt = unicodedata.normalize("NFKC", _header_value(fields.get("format", ""))).strip()
    if not fmt:
        problems.append("文件头缺少 format: arena-core。")
    elif fmt.casefold() != "arena-core":
        problems.append(f"文件头的 format 必须是 arena-core（现在是「{shown(fmt)}」）。")
    version = unicodedata.normalize("NFKC", _header_value(fields.get("version", ""))).strip()
    if not version:
        problems.append("文件头缺少 version: 1。")
    elif not _VERSION_RE.match(version):
        problems.append(f"文件头的 version 目前只能是 1（现在是「{shown(version)}」）。")
    return fields, problems


def parse_core_markdown(text: Any, *, require_complete: bool = True, lenient_limits: bool = False) -> tuple[dict[str, Any], str | None, list[str]]:
    """The merged L0 file -> ``(core, name hint or None, warnings)``; :class:`FileProblems` (every fault) when it is not acceptable.
    (botfiles.parse_core_markdown: the header, the ``# 身份`` / ``# 说话方式`` / ``# 分析方式`` / ``# 附加：<标题>`` sections.)"""
    raw = checked_text(text, "文件")
    if len(raw) > MAX_CORE_MARKDOWN_CHARS:
        raise FileProblems(f"文件太大了（最多 {MAX_CORE_MARKDOWN_CHARS} 字，现在 {len(raw)} 字）。")
    lines = raw.split("\n")
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or lines[start].strip() != "---":
        raise FileProblems("缺少文件头：文件开头要有用 --- 包住的 format: arena-core 和 version: 1。")
    end = start + 1
    while end < len(lines) and lines[end].strip() != "---":
        end += 1
    if end >= len(lines):
        raise FileProblems("文件头没有结束：缺少第二行 ---。")
    fields, problems = _parse_header(lines[start + 1 : end])
    sections: list[tuple[str, list[str]]] = []
    preamble: list[str] = []
    for line in lines[end + 1 :]:
        match = _H1_RE.match(line)
        if match:
            sections.append(((match.group(1) or "").strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    if any(line.strip() for line in preamble):
        problems.append("第一个分区标题之前有多余的内容：分区要用一级标题开头，如「# 身份」。")
    required: dict[str, str] = {}
    counts: dict[str, int] = {}
    extras: list[tuple[str, str]] = []
    extra_counts: dict[str, int] = {}
    for heading, body_lines in sections:
        body = "\n".join(body_lines).strip()
        key = _HEADING_OF.get(re.sub(r"\s+", "", heading))
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
            required.setdefault(key, body)
            continue
        extra = _EXTRA_HEADING_RE.match(heading)
        if extra is None:
            problems.append(
                f"不认识的分区标题「{shown(heading) or '（空）'}」：只能是 "
                + "、".join(f"「{CORE_HEADINGS[k]}」" for k in CORE_SECTIONS)
                + f" 或「{CORE_EXTRA_PREFIX}<标题>」。"
            )
            continue
        title = extra.group(1).strip()
        if not title:
            problems.append(f"附加分区需要标题，写成「# {CORE_EXTRA_PREFIX}<标题>」。")
            continue
        folded = title.casefold()
        extra_counts[folded] = extra_counts.get(folded, 0) + 1
        if extra_counts[folded] == 1:
            extras.append((title, body))
    for key in CORE_SECTIONS:
        if counts.get(key, 0) > 1:
            problems.append(f"「{CORE_HEADINGS[key]}」分区出现了 {counts[key]} 次，只能有 1 个。")
    for title, _body in extras:
        if extra_counts[title.casefold()] > 1:
            problems.append(f"「{CORE_EXTRA_PREFIX}{title}」分区出现了 {extra_counts[title.casefold()]} 次，只能有 1 个。")
    if require_complete:
        for key in CORE_SECTIONS:
            if key not in required:
                problems.append(f"缺少「{CORE_HEADINGS[key]}」分区。")
            elif not required[key]:
                problems.append(f"「{CORE_HEADINGS[key]}」不能为空。")
    core = {"identity": required.get("identity", ""), "voice": required.get("voice", ""), "mind": required.get("mind", ""), "extras": extras}
    warnings: list[str] = []
    for problem in validate_core(core, require_complete=False):
        if lenient_limits:
            warnings.append(problem)
        elif problem not in problems:
            problems.append(problem)
    if problems:
        raise FileProblems(problems)
    return core, (_header_value(fields.get("name", "")) or None), warnings


def render_core_markdown(core: dict[str, Any], name: Any = None) -> str:
    """The canonical merged L0 file (header, 身份, 说话方式, 分析方式, the extras; sections stripped, one blank line between, one final newline)."""
    head = ["---", "format: arena-core", "version: 1"]
    hint = name_hint(name)
    if hint:
        head.append(f"name: {hint}")
    head.append("---")
    blocks: list[str] = []
    for key in CORE_SECTIONS:
        body = core[key].strip()
        blocks.append(f"# {CORE_HEADINGS[key]}\n{body}" if body else f"# {CORE_HEADINGS[key]}")
    for title, text in core["extras"]:
        body = text.strip()
        heading = f"# {CORE_EXTRA_PREFIX}{title.strip()}"
        blocks.append(f"{heading}\n{body}" if body else heading)
    return "\n".join(head) + "\n" + "\n\n".join(blocks) + "\n"


# --------------------------------------------------------------------------------------------------
# the memory parts (botfiles.py): free text, the playbook, the impressions
# --------------------------------------------------------------------------------------------------

_BULLET_RE = re.compile(r"^\s*(?:[-*•·]+|\d{1,2}[.)、．])\s*")
_HEAD_RE = re.compile(r"^[\[［]([^\]］]*)[\]］]\s*(.*)$")
_H2_RE = re.compile(r"^ {0,3}#{1,6}[ \t\u3000]+(.*?)[ \t\u3000]*#*[ \t\u3000]*$")
_CONFIDENCE_WORDS = {"low": "低", "mid": "中", "medium": "中", "high": "高"}
_TAG_BAD_RE = re.compile(r"[\s|\[\]［］#＃]")
SOURCE_CHARS = 40
TAG_CHARS = 20
PLAYBOOK_HEADING = "假设"
NO_IMPRESSION = "（暂无）"


def _confidence(value: str) -> str:
    text = value.strip()
    for char in text[:3]:
        if char in CONFIDENCES:
            return char
    return _CONFIDENCE_WORDS.get(text.casefold(), "低")


def _count(value: Any) -> int:
    match = re.match(r"\s*(\d+)", str(value))
    return int(match.group(1)) if match else 0


def _clean_source(value: Any) -> str:
    text = one_line(value).replace("|", "/").replace("[", "(").replace("]", ")").replace("［", "(").replace("］", ")")
    return text[:SOURCE_CHARS].strip()


def _clean_tag(value: Any) -> str:
    return _TAG_BAD_RE.sub("", str(value))[:TAG_CHARS]


def make_entry(text: str, confidence: str = "低", support: int = 0, counter: int = 0, source: str = "", tags: Sequence[str] = ()) -> dict[str, Any]:
    """One playbook entry as ``PlaybookEntry.to_dict()`` (normalized the way the file is read back)."""
    clean_tags: list[str] = []
    for tag in tags:
        cleaned = _clean_tag(tag)
        if cleaned and cleaned not in clean_tags:
            clean_tags.append(cleaned)
    return {
        "text": one_line(text),
        "confidence": confidence if confidence in CONFIDENCES else "低",
        "support": max(0, int(support or 0)),
        "counter": max(0, int(counter or 0)),
        "source": _clean_source(source),
        "tags": clean_tags,
    }


def _parse_head(head: str) -> dict[str, Any]:
    fields: dict[str, Any] = {"tags": []}
    for piece in head.replace("｜", "|").replace("：", ":").replace("＃", "#").split("|"):
        piece = piece.strip()
        if not piece:
            continue
        if piece.startswith("#"):
            tag = _clean_tag(piece[1:])
            if tag and tag not in fields["tags"]:
                fields["tags"].append(tag)
            continue
        key, colon, value = piece.partition(":")
        if not colon:
            if piece in CONFIDENCES:
                fields["confidence"] = piece
            continue
        key, value = key.strip(), value.strip()
        if key == "把握":
            fields["confidence"] = _confidence(value)
        elif key == "支持":
            fields["support"] = _count(value)
        elif key == "反例":
            fields["counter"] = _count(value)
        elif key == "来源":
            fields["source"] = value
    return fields


def parse_playbook_ex(text: Any) -> tuple[list[dict[str, Any]], int]:
    """``playbook.md`` -> ``(entries, ignored lines)``: ``- [把握:中|支持:3|反例:1|来源:复盘|#对跳] 一句话`` per line."""
    entries: list[dict[str, Any]] = []
    ignored = 0
    section = ""
    for raw in normalize_newlines("" if text is None else str(text)).split("\n"):
        line = raw.strip()
        if not line:
            continue
        heading = _H2_RE.match(line) if line.startswith("#") else None
        if heading:
            section = heading.group(1).strip()
            continue
        if section not in ("", PLAYBOOK_HEADING):
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            line = line[bullet.end() :]
        elif not line.startswith(("[", "［")):
            ignored += 1
            continue
        match = _HEAD_RE.match(line)
        fields: dict[str, Any] = {"tags": []}
        body = line
        if match:
            fields = _parse_head(match.group(1))
            body = match.group(2)
        body = one_line(body)
        if not body:
            ignored += 1
            continue
        entries.append(
            make_entry(body, fields.get("confidence", "低"), fields.get("support", 0), fields.get("counter", 0), fields.get("source", ""), fields["tags"])
        )
    return entries, ignored


def parse_playbook(text: Any) -> list[dict[str, Any]]:
    return parse_playbook_ex(text)[0]


def render_entry(entry: dict[str, Any]) -> str:
    entry = make_entry(**{k: entry.get(k) for k in ("text", "confidence", "support", "counter", "source", "tags") if k in entry})
    head = [f"把握:{entry['confidence']}", f"支持:{entry['support']}", f"反例:{entry['counter']}"]
    if entry["source"]:
        head.append(f"来源:{entry['source']}")
    head.extend(f"#{tag}" for tag in entry["tags"])
    return f"- [{'|'.join(head)}] {entry['text']}"


def render_playbook(entries: Sequence[dict[str, Any]]) -> str:
    if not entries:
        return ""
    return f"## {PLAYBOOK_HEADING}\n" + "\n".join(render_entry(e) for e in entries) + "\n"


def check_playbook(entries: Sequence[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    if len(entries) > PLAYBOOK_MAX:
        problems.append(f"打法最多 {PLAYBOOK_MAX} 条（现在 {len(entries)} 条）。")
    for index, entry in enumerate(entries, start=1):
        text = one_line(entry["text"])
        if not text:
            problems.append(f"打法第 {index} 条没有内容。")
        elif len(text) > PLAYBOOK_ENTRY_CHARS:
            problems.append(f"打法第 {index} 条最多 {PLAYBOOK_ENTRY_CHARS} 字（现在 {len(text)} 字）：{shown(text, 16)}")
    return problems


def clean_impression(value: Any) -> str:
    text = one_line(value)
    return "" if text in (NO_IMPRESSION, "(暂无)", "暂无") else text


def parse_people_ex(text: Any) -> tuple[dict[str, str], int]:
    """``people.md`` -> ``({name: impression}, ignored)``: ``- <名字>：<印象>`` per line."""
    found: dict[str, str] = {}
    folded: dict[str, str] = {}
    ignored = 0
    for raw in normalize_newlines("" if text is None else str(text)).split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            line = line[bullet.end() :]
        match = re.match(r"^([^:：]+?)\s*[:：]\s*(.*)$", line)
        if not match:
            ignored += 1
            continue
        name = match.group(1).strip()
        impression = clean_impression(match.group(2))
        if not name or not impression:
            if not impression and name:
                continue
            ignored += 1
            continue
        key = name.casefold()
        if key in folded:
            found[folded[key]] = impression
        else:
            folded[key] = name
            found[name] = impression
    return found, ignored


def parse_people(text: Any) -> dict[str, str]:
    return parse_people_ex(text)[0]


def render_people(people: dict[str, str]) -> str:
    lines = []
    for name, impression in people.items():
        value = clean_impression(impression)
        if str(name).strip() and value:
            lines.append(f"- {one_line(name)}：{value}")
    return "\n".join(lines) + "\n" if lines else ""


def check_people(people: dict[str, str]) -> list[str]:
    problems: list[str] = []
    if len(people) > PEOPLE_MAX:
        problems.append(f"印象最多 {PEOPLE_MAX} 人（现在 {len(people)} 人）。")
    for name, impression in people.items():
        try:
            clean_name(name)
        except Refusal as exc:
            problems.append(f"印象里的名字「{shown(str(name), 12)}」不合法：{exc.detail}")
            continue
        n = len(clean_impression(impression))
        if n > IMPRESSION_CHARS:
            problems.append(f"对「{name}」的印象最多 {IMPRESSION_CHARS} 字（现在 {n} 字）。")
    return problems


def normalize_part(part: str, content: Any) -> tuple[str, str]:
    """A text meant for ``part`` -> ``(canonical text, note)`` or :class:`FileProblems` (botfiles.normalize_part)."""
    if part not in MEMORY_PARTS:
        raise FileProblems(f"不认识的记忆部分「{part}」，只能是 " + "、".join(MEMORY_PARTS) + "。")
    label = PART_LABELS[part]
    raw = checked_text(content, label)
    if len(raw) > MAX_PART_INPUT_CHARS:
        raise FileProblems(f"「{label}」太长了（最多 {MAX_PART_INPUT_CHARS} 字，现在 {len(raw)} 字）。")
    if part == "playbook":
        entries, ignored = parse_playbook_ex(raw)
        if raw.strip() and not entries:
            raise FileProblems("没有读出任何一条打法：每条写成一行「- [把握:中|支持:0|反例:0|来源:复盘|#标签] 一句话」。")
        problems = check_playbook(entries)
        if problems:
            raise FileProblems(problems)
        notes = [f"有 {ignored} 行没有读出，已忽略。"] if ignored else []
        missing = [t for e in entries for t in e["tags"] if t not in SITUATION_TAGS]
        missing = list(dict.fromkeys(missing))
        if missing:
            notes.append("有标签不在这个剧本的情境标签里，按它检索不到：" + "、".join(f"#{t}" for t in missing) + "。")
        return render_playbook(entries).strip(), "".join(notes)
    if part == "people":
        people, ignored = parse_people_ex(raw)
        if raw.strip() and not people and ignored:
            raise FileProblems("没有读出任何一条印象：每条写成一行「- 名字：一句话印象」。")
        problems = check_people(people)
        if problems:
            raise FileProblems(problems)
        return render_people(people).strip(), (f"有 {ignored} 行没有读出，已忽略。" if ignored else "")
    text = raw.strip()
    limit = MEMORY_PART_LIMITS.get(part)
    if limit is not None and len(text) > limit:
        raise FileProblems(over(label, limit, len(text)))
    return text, ""


# --------------------------------------------------------------------------------------------------
# the derived state of an initialization (BotStore: derive_init)
# --------------------------------------------------------------------------------------------------


def empty_init() -> dict[str, Any]:
    return {"status": "uninitialized", "at": None, "model": None, "duration_s": None, "error": None, "scenario_id": None, "hashes": None}


def derive_init(init: dict[str, Any], current_core_hash: str, sources: dict[str, str], running: bool = False) -> dict[str, Any]:
    """The status the outside sees: ``initializing`` while a job runs; ``stale`` when a ``ready`` record's hashes do not match the
    current L0 / contract / scenario / materials (``stale`` lists the ones that differ); else the stored status."""
    status = init["status"]
    stale: list[str] = []
    if status == "ready":
        stored = init.get("hashes")
        current = {"core": current_core_hash, "contract": sources.get("contract", ""), "scenario": sources.get("scenario", ""), "materials": sources.get("materials", "")}
        for key in INIT_HASHES:
            if stored is None or str(stored.get(key, "")) != str(current[key]):
                stale.append(key)
        wanted = sources.get("scenario_id")
        if wanted and init.get("scenario_id") and init["scenario_id"] != wanted and "scenario" not in stale:
            stale.append("scenario")
            stale.sort(key=INIT_HASHES.index)
        if stale:
            status = "stale"
    if running:
        status = "initializing"
    return {
        "status": status,
        "at": init.get("at"),
        "model": init.get("model"),
        "duration_s": init.get("duration_s"),
        "error": init.get("error") if init["status"] == "failed" else None,
        "stale": stale,
    }


def record_text(tally: dict[str, Any]) -> str:
    """``BotMind.record``: ``场次 7，胜 4，负 3，胜率 57%`` + one line per role; ``""`` for no games."""
    games = int(tally.get("games") or 0)
    if not games:
        return ""
    wins = min(int(tally.get("wins") or 0), games)
    lines = [f"场次 {games}，胜 {wins}，负 {games - wins}，胜率 {round(100 * wins / games)}%"]
    for role, pair in (tally.get("by_role") or {}).items():
        played, won = int(pair[0]), int(pair[1])
        lines.append(f"{role}：{played} 局，胜 {won}")
    return "\n".join(lines)


def record_summary(tally: dict[str, Any]) -> str:
    games = int(tally.get("games") or 0)
    if not games:
        return ""
    wins = min(int(tally.get("wins") or 0), games)
    return f"场次 {games} · 胜 {wins} · 胜率 {round(100 * wins / games)}%"


# --------------------------------------------------------------------------------------------------
# the default library: twelve bots with an L0 of their own (the content is made up for the mock)
# --------------------------------------------------------------------------------------------------

DEFAULT_NAMES = ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo"]

# (identity, voice, mind, extra title, extra text)
_DEFAULT_CORES: dict[str, tuple[str, str, str, str, str]] = {
    "Alice": (
        "打了二十多年线下局的老玩家，话不多，一开口就是结论。你是来赢的，最烦绕圈子和反复表态的人。",
        "你说话短，通常两三句就停，很少超过一百字。爱反问，喜欢直接点名。\n「你急什么？」「这票你自己信吗？」「我只看他投了谁。」\n篇幅：40–110 字",
        "你先看发生了什么：谁出局、谁投了谁、谁改了主意；然后才听谁说了什么。说得漂亮不等于是真的。你的判断都只是暂时的，被结果打脸就认，改得很快。",
        "口头禅",
        "「行，听你的，出了再说。」",
    ),
    "Bob": (
        "热心的话痨，爱把大家的话串起来，觉得多聊几句总能聊出真相。你想赢，但更怕冷场。",
        "你说话热络，爱用“咱们”“你们看啊”，一口气能说好几句，喜欢先复述再补一句自己的看法。\n「咱们捋一捋啊，刚才三个人的说法其实有个共同点。」\n篇幅：120–260 字",
        "你习惯把每个人的发言排成一条时间线，找前后对不上的地方。你也知道话多容易漏重点，所以发言前先挑出最硬的一条放在最后。",
        "",
        "",
    ),
    "Carol": (
        "稳健的老好人，不愿意得罪人，但心里有杆秤。你更看重“不冤枉好人”。",
        "你说话慢条斯理，常用“我觉得”“可能”，很少下断言，但会把顾虑说清楚。\n「我先说我的顾虑，不一定对。」「这一票我想再看一轮。」\n篇幅：80–180 字",
        "你偏向先排除而不是先指认：把最不可能的人划掉，剩下的再比较。证据不足时你宁可弃票，也不乱投。被反驳时你会认真回头检查自己漏了什么。",
        "",
        "",
    ),
    "David": (
        "凭直觉打牌的人，说不清为什么，但觉得谁“不对劲”就会直说。你相信第一感觉，也敢认错。",
        "你说话直接，句子很短，带点情绪，爱用“我感觉”“就是不对”。\n「我感觉他在编。」「别绕，就说你投谁。」\n篇幅：30–90 字",
        "你把直觉当成线索而不是结论：先说出你的感觉，再去找一条能支持或推翻它的事实。找不到支持，你就把感觉降级成“待观察”。",
        "口头禅",
        "「我就问一句，你怎么解释？」",
    ),
    "Eve": (
        "只信票型和记录的计算派。你不在乎谁说得动听，只在乎谁投了谁、谁在什么时候改了口。",
        "你说话克制，像在念记录，爱用数字和顺序。\n「三号第一轮投了五号，第二轮弃票。」「前后矛盾的只有两个人。」\n篇幅：60–140 字",
        "你把每一轮的投票和发言顺序记下来，做成对照表，先找矛盾，再谈立场。你不轻易相信“我是好人”这类表态，除非它能和记录对上。",
        "",
        "",
    ),
    "Frank": (
        "强势的带队者，习惯替大家做决定。你相信一个明确的方向比十个犹豫更有价值。",
        "你说话斩钉截铁，爱下结论、点名归票，偶尔带点命令口吻，但不骂人。\n「今天就出这个人，理由两条。」「没有更好的方案，就按我说的投。」\n篇幅：100–200 字",
        "你先给出一个明确的怀疑对象和两条理由，再听别人反驳；如果被有力地反驳，你会当场换人，不死撑。你评估别人也看他们是不是敢表态。",
        "",
        "",
    ),
    "Grace": (
        "和事佬，擅长缓和气氛，把争吵的人拉回到事实上。你相信吵架解决不了问题。",
        "你说话温和，常用“大家先别急”，爱总结双方的共同点，再提出一个小问题。\n「我听下来你们其实只差一个细节。」\n篇幅：70–150 字",
        "你留意谁在制造对立、谁在降温。你不急着站队，但会在关键时刻明确说出自己的倾向和理由。",
        "口头禅",
        "「我们先把事实摆出来。」",
    ),
    "Henry": (
        "阴阳怪气的老油条，爱用反话和调侃试探别人，实际上观察得很细。",
        "你说话带刺，爱用反问和夸张的赞美，从不正面骂人。\n「哇，这个理由真是太新鲜了。」「好好好，你说得都对。」\n篇幅：50–120 字",
        "你用调侃逼对方多说两句，再从他的反应里找破绽：谁急着辩解，谁回避细节。你的怀疑要落在具体行为上，不凭语气定人。",
        "",
        "",
    ),
    "Ivy": (
        "认真的新手，每一步都想弄清规则，爱问“为什么”。你很在意自己有没有理解对。",
        "你说话认真，偶尔先确认规则再发言，语气诚恳，句子完整。\n「我想确认一下，这个能力今天可以用吗？」\n篇幅：90–200 字",
        "你把能确认的规则和事实写在前面，把猜测单独标出来。遇到没把握的情况，你会直接说明“我不确定”，并说出自己的倾向。",
        "",
        "",
    ),
    "Jack": (
        "爱讲故事的人，喜欢用一个连贯的叙述把线索串起来。你相信故事讲得通，才算真的懂了。",
        "你说话生动，爱用比喻和场景，句子偏长，但每段都落到一个结论上。\n「想象一下，如果他是狼，昨晚他会怎么做？」\n篇幅：140–280 字",
        "你为每个可疑对象写一个“如果他是狼”的故事，再拿记录去检验：哪一步对不上，故事就作废。你警惕自己被故事说服。",
        "",
        "",
    ),
    "Kate": (
        "谨慎的复盘派，每一轮都在整理已知信息。你宁可慢一点，也不想漏掉关键线索。",
        "你说话条理清楚，常用“目前已知”“待确认”，不抢话，也不绕弯。\n「目前能确认的有三点。」「这一点还不能下结论。」\n篇幅：90–190 字",
        "你把信息分成“已确认”“待确认”“猜测”三类，只用前两类做判断。出现新事实时，你会主动更新结论并说明改了什么。",
        "口头禅",
        "「先把已知的列出来。」",
    ),
    "Leo": (
        "冲动派，爱第一个表态，节奏很快。你相信行动比犹豫更能逼出破绽。",
        "你说话快，句子短，常常一连串发问，带点急躁，但不人身攻击。\n「谁先说？」「别拖，直接站边。」\n篇幅：30–100 字",
        "你先抛出一个观点逼大家回应，再根据回应修正。你知道冲动容易出错，所以对自己的第一反应保持怀疑，被打脸就立刻收回。",
        "",
        "",
    ),
}


def default_core(name: str) -> dict[str, Any]:
    identity, voice, mind, extra_title, extra_text = _DEFAULT_CORES[name]
    core = empty_core()
    core.update(identity=identity, voice=voice, mind=mind)
    if extra_title:
        core["extras"] = [(extra_title, extra_text)]
    return core


def short_identity(text: str, limit: int = 40) -> str:
    """``BotsPayload.defaults[i].identity``: the first sentence of the identity (at most ``limit`` characters)."""
    first = re.split(r"[。！？\n]", text.strip(), maxsplit=1)[0].strip()
    return first if len(first) <= limit else first[: limit - 1] + "…"


DEFAULT_ARCH_NOTES = (
    "我每次被叫醒，先看新动态和唤醒原因，判断有没有轮到我的事；没有就记一笔然后睡。发言前先想清楚此刻最关键的一两件事，一次说完。"
    "回执写着 TOO_LONG 就删减后重发，写着 FORMAT 就改成连贯的口语。"
)
DEFAULT_DIGEST = (
    "这个游戏分两个阵营：好人要找出狼，狼要藏起来并推动放逐好人。白天先发言再投票放逐，夜里各个身份分别行动。"
    "我不确定的是：同时有两个人声称同一个身份时，该以谁的后续行为为准。"
)
DEFAULT_PLAYBOOK = [
    ("两个人都说自己是同一个身份时，先看谁的结论和之后的行动对得上。", "中", 3, 1, "初始化", ("发言",)),
    ("投票前先把已经确认的事实列出来，再决定投谁。", "中", 2, 0, "初始化", ("投票",)),
    ("警上发言先听完全部再站边，除非有人明显在带节奏。", "低", 0, 0, "初始化", ("警上",)),
]
DEFAULT_QUESTIONS = "平票的时候，被 PK 的两个人该怎么发言才最有说服力？"


# --------------------------------------------------------------------------------------------------
# the model of the bot store
# --------------------------------------------------------------------------------------------------


class AvatarPicker:
    """What the store needs of the avatar library; the server fills it with real descriptions (this one has none: avatars are ``None``)."""

    def batch(self, n: int, taken: Sequence[dict[str, Any]]) -> list[dict[str, Any] | None]:
        return [None] * n

    def one(self, taken: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
        return None

    def problems(self, desc: Any) -> list[str]:
        return [] if desc is None or isinstance(desc, dict) else ["头像描述不合法"]


class MockBots:
    """The twelve bots (a bot = a name, an avatar, L0, what it learned, an initialization record, pending proposals), the line-up and
    the moderator's avatar.  ``sources()`` returns the hashes of the contract, the scenario and its materials
    (``{"contract", "scenario", "materials", "scenario_id"}``)."""

    def __init__(self, seed: int = 7, *, sources: Callable[[], dict[str, str]] | None = None, avatars: AvatarPicker | None = None,
                 clock: Callable[[], float] | None = None, preset: str = "ready") -> None:
        self.rng = random.Random(seed)
        self.sources = sources or (lambda: {"contract": "", "scenario": "", "materials": "", "scenario_id": DEFAULT_SCENARIO_ID})
        self.avatars = avatars or AvatarPicker()
        self.clock = clock or time.time
        self.bots: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self.memory_enabled = True
        self.moderator: dict[str, Any] = {"id": "moderator", "name": "主持人", "avatar": None}
        self.usage: dict[str, Any] = {"calls": 0, "tokens": 0, "cost_usd": 0.0}
        self.apply_preset(preset)

    # ------------------------------------------------------------------------------------ building
    def _now(self) -> int:
        return int(self.clock())

    def _new_id(self) -> str:
        while True:
            candidate = "b-" + "".join(self.rng.choice("0123456789abcdef") for _ in range(8))
            if candidate not in self.bots:
                return candidate

    def _blank(self, bot_id: str, name: str, avatar: dict[str, Any] | None, origin: str = "default") -> dict[str, Any]:
        return {
            "id": bot_id,
            "name": name,
            "avatar": avatar,
            "origin": origin,
            "created_at": self._now(),
            "core": empty_core(),
            "core_updated_at": None,
            "init": empty_init(),
            "mem": {"arch_notes": "", "people": "", "record": {"games": 0, "wins": 0, "by_role": {}}, "rules": {}},
            "staging": [],
        }

    def library(self) -> list[dict[str, Any]]:
        """The default bot library: ``[{"name", "avatar", "identity"}]`` (``BotsPayload.defaults``)."""
        return [{"name": n, "avatar": copy.deepcopy(self._library_avatars()[i]), "identity": short_identity(default_core(n)["identity"])} for i, n in enumerate(DEFAULT_NAMES)]

    def _library_avatars(self) -> list[dict[str, Any] | None]:
        if not hasattr(self, "_lib_avatars"):
            self._lib_avatars = self.avatars.batch(len(DEFAULT_NAMES), [])
        return self._lib_avatars

    def default_sources(self) -> dict[str, str]:
        """The hashes a library bot was initialized under (the contract and scenario the program ships)."""
        return dict(self._default_sources) if hasattr(self, "_default_sources") else self.sources()

    def pin_default_sources(self) -> None:
        """Remember the contract / scenario / materials hashes in force now as 'the ones the program ships' (call once at start)."""
        self._default_sources = dict(self.sources())

    def _initialized(self, bot: dict[str, Any], sources: dict[str, str] | None = None, *, model: str = "deepseek-flash", duration: float | None = None) -> None:
        """Give a bot a finished initialization: the three required files (and the optional one), stamped with the hashes of ``sources``."""
        sources = sources or self.sources()
        sid = sources.get("scenario_id") or DEFAULT_SCENARIO_ID
        entries = [make_entry(t, c, s, k, src, tg) for (t, c, s, k, src, tg) in DEFAULT_PLAYBOOK]
        bot["mem"]["arch_notes"] = DEFAULT_ARCH_NOTES
        bot["mem"]["rules"][sid] = {"digest": DEFAULT_DIGEST, "playbook": render_playbook(entries).strip(), "questions": DEFAULT_QUESTIONS}
        bot["init"] = {
            "status": "ready", "at": self._now() - 3600, "model": model,
            "duration_s": duration if duration is not None else round(60 + self.rng.random() * 40, 1), "error": None, "scenario_id": sid,
            "hashes": {"core": core_hash(self._core_dict(bot)), "contract": sources.get("contract", ""), "scenario": sources.get("scenario", ""), "materials": sources.get("materials", "")},
        }

    def load_defaults(self, *, initialized: bool = True) -> None:
        """Twelve bots from the default library, seated in order; initialized (✔) when asked for - the way a first run looks."""
        self.bots, self.order = {}, []
        avatars = self._library_avatars()
        for i, name in enumerate(DEFAULT_NAMES):
            bot = self._blank(self._new_id(), name, copy.deepcopy(avatars[i]))
            core = default_core(name)
            self._set_core(bot, core)
            if initialized:
                self._initialized(bot)
            self.bots[bot["id"]] = bot
            self.order.append(bot["id"])
        # a little history, so that the memory and the record have something to show
        for i, bot_id in enumerate(self.order[:5]):
            bot = self.bots[bot_id]
            names = [self.bots[o]["name"] for o in self.order if o != bot_id]
            bot["mem"]["people"] = render_people({names[0]: "发言短，站边快，可信度中。", names[1]: "逻辑细，抓细节准，值得多听他的。"}).strip()
            games = 7 - i
            bot["mem"]["record"] = {"games": games, "wins": max(1, games // 2), "by_role": {"平民": [games - 2, max(1, games // 2)], "狼人": [2, 0]}}
        self.moderator = {"id": "moderator", "name": "主持人", "avatar": self.avatars.one([b["avatar"] for b in self.bots.values() if b["avatar"]])}

    def apply_preset(self, preset: str) -> None:
        """``ready`` (every bot ✔: the way the library comes), ``fresh`` (L0 only: after a migration, nobody initialized), ``mixed`` (a bit of
        everything: ✔ ⚠ ✖ and a bot without an L0), ``empty`` (no L0 at all)."""
        if preset not in ("ready", "fresh", "mixed", "empty"):
            raise Refusal(422, "preset 必须是 ready / fresh / mixed / empty 之一。")
        self.load_defaults(initialized=preset in ("ready", "mixed"))
        if preset == "fresh":
            for bot in self.bots.values():
                bot["origin"] = "migrated"
        elif preset == "empty":
            for bot in self.bots.values():
                self._set_core(bot, empty_core())
        elif preset == "mixed":
            ids = self.order
            # N2: edited L0 -> stale (⚠); N3: failed (✖); N4 / N5: never initialized; N6: no L0; N7: the contract changed under it
            core = self._core_dict(self.bots[ids[1]])
            core["mind"] = core["mind"] + "（刚改过）"
            self._set_core(self.bots[ids[1]], core)
            self.bots[ids[2]]["init"].update(status="failed", error="模型调用失败：HTTP 429（模拟）", hashes=None)
            for i in (3, 4):
                self.bots[ids[i]]["init"] = empty_init()
                self.bots[ids[i]]["mem"].update(arch_notes="", rules={})
            self._set_core(self.bots[ids[5]], empty_core())
            self.bots[ids[6]]["init"]["hashes"]["contract"] = "0" * 64

    # ------------------------------------------------------------------------------------ lookups
    def lineup(self) -> list[str]:
        return list(self.order)

    def require(self, bot_id: str) -> dict[str, Any]:
        bot = self.bots.get(str(bot_id))
        if bot is None:
            raise not_found()
        return bot

    def seat_of(self, bot_id: str) -> str | None:
        return f"N{self.order.index(bot_id) + 1}" if bot_id in self.order else None

    def bot_at(self, seat: str) -> str | None:
        m = re.fullmatch(r"N([0-9]+)", str(seat))
        if not m:
            return None
        index = int(m.group(1)) - 1
        return self.order[index] if 0 <= index < len(self.order) else None

    def name_of(self, bot_id: str) -> str:
        return self.require(bot_id)["name"]

    def bot_id_of(self, name: str) -> str | None:
        key = str(name or "").strip().casefold()
        for bot in self.bots.values():
            if bot["name"].casefold() == key:
                return bot["id"]
        return None

    def names(self) -> dict[str, str]:
        return {i: self.bots[i]["name"] for i in self.order}

    # ------------------------------------------------------------------------------------ L0
    def _core_dict(self, bot: dict[str, Any]) -> dict[str, Any]:
        c = bot["core"]
        return {"identity": c["identity"], "voice": c["voice"], "mind": c["mind"], "extras": [(e["title"], e["text"]) for e in c["extras"]]}

    def _set_core(self, bot: dict[str, Any], core: dict[str, Any]) -> None:
        """Store an L0 (stripped); the extras keep the slug of a title that already had one."""
        existing = {e["title"].strip().casefold(): e["slug"] for e in bot["core"]["extras"]}
        titles = [t.strip() for t, _ in core["extras"]]
        slugs = assign_slugs(titles, existing)
        bot["core"] = {
            "identity": core["identity"].strip(), "voice": core["voice"].strip(), "mind": core["mind"].strip(),
            "extras": [{"title": t.strip(), "text": x.strip(), "slug": s} for (t, x), s in zip(core["extras"], slugs)],
        }
        bot["core_updated_at"] = self._now() if any((core["identity"].strip(), core["voice"].strip(), core["mind"].strip(), core["extras"])) else None

    def core_markdown(self, bot_id: str) -> str:
        bot = self.require(bot_id)
        return render_core_markdown(self._core_dict(bot), bot["name"])

    def set_core_markdown(self, bot_id: str, markdown: Any) -> str | None:
        """Replace L0 by a merged file (all of it is checked first); returns the name in the file header (a hint: nothing is renamed)."""
        bot = self.require(bot_id)
        core, hint, _warnings = parse_core_markdown(markdown)
        self._set_core(bot, core)
        return hint

    def clear_core(self, bot_ids: Iterable[str] | None = None) -> list[str]:
        ids = self._pick(bot_ids)
        for bot_id in ids:
            self._set_core(self.bots[bot_id], empty_core())
        return ids

    def _pick(self, bot_ids: Iterable[str] | None) -> list[str]:
        if bot_ids is None:
            return list(self.order)
        out: list[str] = []
        for bot_id in bot_ids:
            self.require(bot_id)
            if bot_id not in out:
                out.append(bot_id)
        return out

    # ------------------------------------------------------------------------------------ memory
    def _rules(self, bot: dict[str, Any], scenario_id: str, *, create: bool = False) -> dict[str, str]:
        rules = bot["mem"]["rules"]
        if scenario_id not in rules and create:
            rules[scenario_id] = {"digest": "", "playbook": "", "questions": ""}
        return rules.get(scenario_id, {"digest": "", "playbook": "", "questions": ""})

    def read_part(self, bot_id: str, part: str, scenario_id: str = "") -> str:
        bot = self.require(bot_id)
        if part == "arch_notes":
            return bot["mem"]["arch_notes"]
        if part == "people":
            return bot["mem"]["people"]
        if part in SCENARIO_PARTS:
            return self._rules(bot, scenario_id)[part]
        raise FileProblems(f"不认识的记忆部分「{part}」，只能是 " + "、".join(MEMORY_PARTS) + "。")

    def write_part(self, bot_id: str, part: str, content: Any, scenario_id: str = "") -> tuple[bool, str]:
        """Validate and write a part (the user's edit and a review's write are the same thing); ``(ok, message)``.  It does not touch
        the initialization record (C9: edits take effect at once)."""
        bot = self.require(bot_id)
        label = PART_LABELS.get(part, part)
        try:
            text, note = normalize_part(part, content)
        except FileProblems as exc:
            return False, f"{exc} 没有保存，请改好后重新写入。"
        if part == "arch_notes":
            bot["mem"]["arch_notes"] = text
        elif part == "people":
            bot["mem"]["people"] = text
        else:
            self._rules(bot, scenario_id, create=True)[part] = text
        if not text:
            return True, f"已清空{label}。"
        detail = ""
        if part == "playbook":
            detail = f"{len(parse_playbook(text))} 条，"
        elif part == "people":
            detail = f"{len(parse_people(text))} 人，"
        return True, f"已写入{label}（{detail}{len(text)} 字）。" + note

    def clear_all_parts(self, bot_id: str) -> None:
        """Everything the bot learned (notes, every scenario's digest / playbook / questions, impressions, record) and with it the
        initialization result; L0 and the proposals stay."""
        bot = self.require(bot_id)
        bot["mem"] = {"arch_notes": "", "people": "", "record": {"games": 0, "wins": 0, "by_role": {}}, "rules": {}}
        bot["init"] = empty_init()

    def clear_memory(self, bot_ids: Iterable[str] | None = None) -> list[str]:
        ids = self._pick(bot_ids)
        for bot_id in ids:
            self.clear_all_parts(bot_id)
        return ids

    def add_record(self, bot_id: str, role_label: str, won: bool | None) -> None:
        tally = self.require(bot_id)["mem"]["record"]
        tally["games"] += 1
        if won:
            tally["wins"] += 1
        pair = tally["by_role"].setdefault(role_label, [0, 0])
        pair[0] += 1
        if won:
            pair[1] += 1

    def memory_view(self, bot: dict[str, Any], scenario_id: str) -> dict[str, Any]:
        rules = self._rules(bot, scenario_id)
        playbook_md = rules["playbook"]
        people_md = bot["mem"]["people"]
        return {
            "scenario_id": scenario_id, "arch_notes": bot["mem"]["arch_notes"], "digest": rules["digest"],
            "playbook": parse_playbook(playbook_md), "playbook_md": playbook_md, "questions": rules["questions"],
            "people": parse_people(people_md), "people_md": people_md, "record": record_text(bot["mem"]["record"]),
        }

    def memory_stats(self, bot: dict[str, Any], scenario_id: str) -> dict[str, Any]:
        rules = self._rules(bot, scenario_id)
        return {
            "arch_notes": len(bot["mem"]["arch_notes"]), "digest": len(rules["digest"]), "playbook": len(parse_playbook(rules["playbook"])),
            "questions": len(rules["questions"]), "people": len(parse_people(bot["mem"]["people"])), "record": record_summary(bot["mem"]["record"]),
        }

    # ------------------------------------------------------------------------------------ freezing, names, avatars
    def frozen(self, bot_id: str) -> bool:
        """The bot has initialization output (``ready``, or notes / digest / playbook / questions of any scenario): its name and avatar
        need a confirmed reset (H23).  Impressions and the record do not count."""
        bot = self.require(bot_id)
        if bot["init"]["status"] == "ready" or bot["mem"]["arch_notes"].strip():
            return True
        return any(r[p].strip() for r in bot["mem"]["rules"].values() for p in SCENARIO_PARTS)

    def validate_name(self, name: Any, *, exclude: str | None = None) -> str:
        value = clean_name(name)
        for bot in self.bots.values():
            if bot["id"] != exclude and bot["name"].casefold() == value.casefold():
                raise Refusal(409, f"名字「{value}」已被 {self.seat_of(bot['id'])} 使用，名字不能重复。", "name_taken")
        return value

    def _taken_avatars(self, *, skip: Iterable[str] = ()) -> list[dict[str, Any]]:
        skip = set(skip)
        out = [b["avatar"] for b in self.bots.values() if b["avatar"] and b["id"] not in skip]
        if self.moderator.get("avatar"):
            out.append(self.moderator["avatar"])
        return out

    def update_bot(self, bot_id: str, *, name: Any = None, avatar: Any = None, confirm_reset: bool = False) -> str:
        """Change the name and / or the avatar; returns the id of the bot afterwards.  Everything is validated first.  A bot that was not
        initialized is changed in place; a frozen one needs ``confirm_reset`` (409 ``needs_confirm``), then it starts a new identity:
        only L0 is kept, it gets a new id (line-up, proposals dropped), the status is ``uninitialized``."""
        bot = self.require(bot_id)
        new_name = bot["name"]
        renamed = False
        if name is not None:
            try:
                new_name = self.validate_name(name, exclude=bot_id)
            except Refusal as exc:
                if exc.code == "name_taken":
                    raise Refusal(400, exc.detail, "name_taken") from None
                raise
            renamed = new_name != bot["name"]
        new_avatar = bot["avatar"]
        re_avatar = False
        if avatar is not None:
            problems = self.avatars.problems(avatar)
            if problems or not isinstance(avatar, dict):
                raise Refusal(400, "头像描述不合法：" + "；".join(problems or ["必须是对象"]))
            new_avatar = avatar
            re_avatar = new_avatar != bot["avatar"]
        if not renamed and not re_avatar:
            return bot_id
        if not self.frozen(bot_id):
            bot["name"], bot["avatar"] = new_name, new_avatar
            return bot_id
        if not confirm_reset:
            raise Refusal(409, RESET_CONFIRM_TEXT, "needs_confirm", will_clear=list(WILL_CLEAR))
        return self._reset_identity(bot, new_name, new_avatar)

    def _reset_identity(self, bot: dict[str, Any], new_name: str, new_avatar: dict[str, Any] | None) -> str:
        new_id = self._new_id()
        fresh = self._blank(new_id, new_name, new_avatar, bot["origin"])
        fresh["core"] = copy.deepcopy(bot["core"])
        fresh["core_updated_at"] = bot["core_updated_at"]
        self.bots[new_id] = fresh
        self.order = [new_id if i == bot["id"] else i for i in self.order]
        del self.bots[bot["id"]]
        return new_id

    def regenerate_avatar(self, bot_id: str, *, confirm_reset: bool = False) -> str:
        self.require(bot_id)
        picked = self.avatars.one(self._taken_avatars())
        if picked is None:
            raise Refusal(503, "头像素材库不可用，请先运行 tools/avatar_lab.py 生成。", "avatars_unavailable")
        return self.update_bot(bot_id, avatar=picked, confirm_reset=confirm_reset)

    def regenerate_avatars(self) -> tuple[list[str], list[str]]:
        """New avatars for every bot that is not frozen; returns ``(changed ids, skipped ids)``."""
        skipped = [i for i in self.order if self.frozen(i)]
        targets = [i for i in self.order if i not in skipped]
        taken = [self.bots[i]["avatar"] for i in skipped if self.bots[i]["avatar"]]
        if self.moderator.get("avatar"):
            taken.append(self.moderator["avatar"])
        picked = self.avatars.batch(len(targets), taken)
        for bot_id, avatar in zip(targets, picked):
            if avatar is not None:
                self.bots[bot_id]["avatar"] = avatar
        return targets, skipped

    def set_moderator_avatar(self, avatar: Any) -> dict[str, Any]:
        problems = self.avatars.problems(avatar)
        if problems or not isinstance(avatar, dict):
            raise Refusal(400, "头像描述不合法：" + "；".join(problems or ["必须是对象"]))
        self.moderator["avatar"] = avatar
        return copy.deepcopy(self.moderator)

    def regenerate_moderator_avatar(self) -> dict[str, Any]:
        picked = self.avatars.one(self._taken_avatars())
        if picked is None:
            raise Refusal(503, "头像素材库不可用，请先运行 tools/avatar_lab.py 生成。", "avatars_unavailable")
        self.moderator["avatar"] = picked
        return copy.deepcopy(self.moderator)

    def set_lineup(self, order: Any) -> list[str]:
        """Seat the bots in this order (a permutation of the bots there are, else 422 ``bad_lineup``)."""
        if not isinstance(order, (list, tuple)) or any(not isinstance(i, str) for i in order):
            raise Refusal(422, "阵容必须是玩家 id 的列表。", "bad_lineup")
        if len(order) != len(self.order) or sorted(order) != sorted(self.order):
            raise Refusal(422, f"阵容必须是现在这 {len(self.order)} 个玩家的一个排列：不能多、不能少、不能重复。", "bad_lineup")
        self.order = list(order)
        return list(self.order)

    # ------------------------------------------------------------------------------------ proposals (L0 changes a review suggests)
    def _split_section(self, section: str) -> tuple[str, str]:
        if section in CORE_SECTIONS:
            return section, ""
        if section.startswith("extra:") and section[6:].strip():
            return "extra", section[6:].strip()
        raise FileProblems("提案的分区必须是 identity / voice / mind / extra:<标题>。")

    def _section_text(self, bot: dict[str, Any], section: str) -> str:
        kind, ident = self._split_section(section)
        if kind != "extra":
            return bot["core"][kind]
        for e in bot["core"]["extras"]:
            if ident == e["slug"] or ident.casefold() == e["title"].strip().casefold():
                return e["text"]
        return ""

    def _apply_section(self, bot: dict[str, Any], section: str, text: str) -> dict[str, Any]:
        core = self._core_dict(bot)
        kind, ident = self._split_section(section)
        text = text.strip()
        if kind != "extra":
            core[kind] = text
            return core
        extras = list(core["extras"])
        for index, (title, _old) in enumerate(extras):
            slug = bot["core"]["extras"][index]["slug"]
            if ident == slug or ident.casefold() == title.strip().casefold():
                extras[index] = (title, text)
                break
        else:
            if len(extras) >= CORE_EXTRA_MAX:
                raise FileProblems(f"附加分区最多 {CORE_EXTRA_MAX} 份，已经满了，不能再新增「{shown(ident)}」。")
            extras.append((ident, text))
        core["extras"] = extras
        return core

    def stage_core(self, bot_id: str, section: str, content: str, reason: str, source: str) -> str:
        """A proposal to change one L0 section, for the user to decide (a review may only propose, H34)."""
        bot = self.require(bot_id)
        text = checked_text(content, "提案内容").strip()
        if not text:
            raise FileProblems("提案内容不能为空。")
        candidate = self._apply_section(bot, section, text)
        problems = validate_core(candidate)
        if problems:
            raise FileProblems(problems)
        if len(bot["staging"]) >= MAX_STAGING:
            raise FileProblems(f"待确认的提案已经有 {MAX_STAGING} 条，请先处理掉一些。")
        kind, ident = self._split_section(section)
        normalized = kind
        if kind == "extra":
            normalized = f"extra:{ident}"
            for e in bot["core"]["extras"]:
                if ident == e["slug"] or ident.casefold() == e["title"].strip().casefold():
                    normalized = f"extra:{e['slug']}"
                    break
        pid = "p-" + "".join(self.rng.choice("0123456789abcdef") for _ in range(8))
        bot["staging"].append({
            "id": pid, "section": normalized, "before": self._section_text(bot, section), "after": text,
            "reason": one_line(reason)[:200], "source": one_line(source)[:80], "created_at": self._now(),
        })
        return pid

    def staging(self, bot_id: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self.require(bot_id)["staging"])

    def _proposal(self, bot: dict[str, Any], pid: str) -> dict[str, Any]:
        for p in bot["staging"]:
            if p["id"] == pid:
                return p
        raise not_found("没有这条待确认的提案。")

    def accept_staging(self, bot_id: str, pid: str) -> None:
        bot = self.require(bot_id)
        proposal = self._proposal(bot, pid)
        core = self._apply_section(bot, proposal["section"], proposal["after"])
        problems = validate_core(core)
        if problems:
            raise FileProblems(problems)
        self._set_core(bot, core)
        bot["staging"].remove(proposal)

    def reject_staging(self, bot_id: str, pid: str) -> None:
        bot = self.require(bot_id)
        bot["staging"].remove(self._proposal(bot, pid))

    # ------------------------------------------------------------------------------------ initialization records
    def commit_init(self, bot_id: str, scenario_id: str, files: dict[str, str], *, model: str, duration_s: float, sources: dict[str, str]) -> None:
        """All or nothing: ``arch_notes`` / ``digest`` / ``playbook`` must be there and legal, ``questions`` is optional; a key left out keeps
        the old file, an empty text clears it.  Writes ``init: ready`` stamped with the hashes of ``sources``."""
        bot = self.require(bot_id)
        problems: list[str] = []
        staged: dict[str, str] = {}
        for part in ("arch_notes", "digest", "playbook", "questions"):
            if part in files:
                try:
                    staged[part] = normalize_part(part, files[part])[0]
                except FileProblems as exc:
                    problems.extend(exc.problems)
        current = {"arch_notes": bot["mem"]["arch_notes"], **self._rules(bot, scenario_id)}
        for part in ("arch_notes", "digest", "playbook"):
            if not (staged.get(part, current.get(part, "")) or "").strip():
                problems.append(f"缺少必需的记忆文件：{PART_LABELS[part]}。")
        if problems:
            raise FileProblems(problems)
        if "arch_notes" in staged:
            bot["mem"]["arch_notes"] = staged["arch_notes"]
        rules = self._rules(bot, scenario_id, create=True)
        for part in SCENARIO_PARTS:
            if part in staged:
                rules[part] = staged[part]
        bot["init"] = {
            "status": "ready", "at": self._now(), "model": model, "duration_s": round(float(duration_s), 1), "error": None,
            "scenario_id": scenario_id,
            "hashes": {"core": core_hash(self._core_dict(bot)), "contract": sources.get("contract", ""), "scenario": sources.get("scenario", ""), "materials": sources.get("materials", "")},
        }

    def fail_init(self, bot_id: str, error: str, *, model: str = "", duration_s: float = 0.0) -> None:
        """A failed initialization: the old files stay as they were; the record says why."""
        bot = self.require(bot_id)
        bot["init"].update(status="failed", at=self._now(), model=model or None, duration_s=round(float(duration_s), 1), error=str(error), hashes=None)

    # ------------------------------------------------------------------------------------ views
    def summary(self, bot_id: str, *, sources: dict[str, str] | None = None, running: bool = False, scenario_id: str | None = None) -> dict[str, Any]:
        """``BotSummary`` without ``seat`` and ``binding`` (the hub adds those)."""
        bot = self.require(bot_id)
        sources = sources or self.sources()
        sid = sources.get("scenario_id", "") if scenario_id is None else scenario_id
        core = self._core_dict(bot)
        chars = {k: len(core[k].strip()) for k in CORE_SECTIONS}
        missing = [k for k in CORE_SECTIONS if not core[k].strip()]
        return {
            "id": bot["id"], "name": bot["name"], "avatar": copy.deepcopy(bot["avatar"]), "avatar_code": avatar_code(bot["avatar"]),
            "origin": bot["origin"],
            "core": {
                "complete": not missing, "missing": missing, "chars": chars,
                "extras": [{"title": e["title"], "slug": e["slug"], "chars": len(e["text"].strip())} for e in bot["core"]["extras"]],
                "updated_at": bot["core_updated_at"],
            },
            "init": derive_init(bot["init"], core_hash(core), sources, running),
            "frozen": self.frozen(bot_id),
            "memory_stats": self.memory_stats(bot, sid),
            "staging": len(bot["staging"]),
        }

    def detail(self, bot_id: str, scenario_id: str = "", *, sources: dict[str, str] | None = None, running: bool = False) -> dict[str, Any]:
        """``BotDetail`` without ``seat`` and ``binding``: the summary plus L0 (merged file, sections, extras with their texts), the memory
        of ``scenario_id`` and the pending proposals (the list here, not the count)."""
        bot = self.require(bot_id)
        sources = sources or self.sources()
        sid = scenario_id or sources.get("scenario_id", "")
        data = self.summary(bot_id, sources=sources, running=running, scenario_id=sid)
        core = dict(data["core"])
        core["markdown"] = self.core_markdown(bot_id)
        core["sections"] = {k: bot["core"][k] for k in CORE_SECTIONS}
        core["extras"] = [{"title": e["title"], "slug": e["slug"], "chars": len(e["text"].strip()), "text": e["text"]} for e in bot["core"]["extras"]]
        data["core"] = core
        data["memory"] = self.memory_view(bot, sid)
        data["staging"] = self.staging(bot_id)
        return data

    def problems(self, bot_id: str, *, sources: dict[str, str] | None = None, running: bool = False) -> list[dict[str, str]]:
        """What keeps this bot out of a game (the start check, H25): the ``items`` of a ``Problem``."""
        bot = self.require(bot_id)
        sources = sources or self.sources()
        core = self._core_dict(bot)
        missing = [k for k in CORE_SECTIONS if not core[k].strip()]
        items: list[dict[str, str]] = []
        if len(missing) == len(CORE_SECTIONS) and not core["extras"]:
            items.append({"code": "core_missing", "message": "底层设定还没有写：身份、说话方式、分析方式都是空的。", "fix": "import_core"})
        else:
            faults = validate_core(core)
            if missing:
                faults.insert(0, "缺少" + "、".join(f"「{CORE_HEADINGS[k]}」" for k in missing) + "。")
            if faults:
                items.append({"code": "core_incomplete", "message": "底层设定不完整：" + " ".join(faults), "fix": "edit_core"})
        derived = derive_init(bot["init"], core_hash(core), sources, running)
        status = derived["status"]
        if status == "initializing":
            items.append({"code": "init_running", "message": "正在初始化，请等它结束。", "fix": "init"})
        elif status == "uninitialized":
            items.append({"code": "not_initialized", "message": "还没有初始化。", "fix": "init"})
        elif status == "failed":
            items.append({"code": "init_failed", "message": f"上次初始化没有成功：{derived['error'] or '原因不明'}", "fix": "init"})
        elif status == "stale":
            changed = "、".join(SOURCE_LABELS[k] for k in derived["stale"])
            items.append({"code": "stale", "message": f"初始化之后{changed}变了，需要重新初始化。", "fix": "init"})
        if bot["init"]["status"] == "ready" and sources.get("scenario_id"):
            gone = [PART_LABELS[p] for p, text in (("arch_notes", bot["mem"]["arch_notes"]), *((q, self._rules(bot, sources["scenario_id"])[q]) for q in ("digest", "playbook"))) if not text.strip()]
            if gone:
                items.append({"code": "not_initialized", "message": "缺少必需的记忆文件：" + "、".join(gone) + "，请重新初始化。", "fix": "init"})
        return items

    def problem_entry(self, bot_id: str, *, sources: dict[str, str] | None = None, running: bool = False) -> dict[str, Any] | None:
        items = self.problems(bot_id, sources=sources, running=running)
        if not items:
            return None
        return {"scope": "bot", "bot_id": bot_id, "seat": self.seat_of(bot_id), "name": self.name_of(bot_id), "items": items}

    # ------------------------------------------------------------------------------------ .arenabot (a small zip)
    def bot_files(self, bot_id: str) -> list[tuple[str, str]]:
        """``(relative path, text)`` of every file of the bot (what a ``.arenabot`` carries)."""
        bot = self.require(bot_id)
        meta = {
            "format": 1, "id": bot["id"], "name": bot["name"], "avatar": bot["avatar"], "created_at": bot["created_at"], "origin": bot["origin"],
            "init": bot["init"],
        }
        files: list[tuple[str, str]] = [("bot.json", json.dumps(meta, ensure_ascii=False, indent=1) + "\n")]
        for key in CORE_SECTIONS:
            if bot["core"][key]:
                files.append((f"core/{key}.md", bot["core"][key] + "\n"))
        for e in bot["core"]["extras"]:
            files.append((f"core/extra-{e['slug']}.md", f"# {e['title']}\n\n{e['text']}\n"))
        if bot["mem"]["arch_notes"]:
            files.append(("arch/notes.md", bot["mem"]["arch_notes"] + "\n"))
        for sid, rules in sorted(bot["mem"]["rules"].items()):
            for part in SCENARIO_PARTS:
                if rules[part]:
                    files.append((f"rules/{sid}/{part}.md", rules[part] + "\n"))
        if bot["mem"]["people"]:
            files.append(("people.md", bot["mem"]["people"] + "\n"))
        if bot["mem"]["record"]["games"]:
            files.append(("record.json", json.dumps(bot["mem"]["record"], ensure_ascii=False, indent=1) + "\n"))
        for p in bot["staging"]:
            files.append((f"staging/{p['id']}.json", json.dumps(p, ensure_ascii=False, indent=1) + "\n"))
        return files

    def export_zip(self, bot_id: str) -> bytes:
        bot = self.require(bot_id)
        files = self.bot_files(bot_id)
        manifest = {
            "format": "arenabot", "format_version": 1, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "app_version": "v0.14.0-mock", "bot": {"name": bot["name"], "origin": bot["origin"]},
            "files": [{"path": f"bot/{rel}", "size": len(text.encode("utf-8")), "sha256": sha256_text(text)} for rel, text in files],
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
            for rel, text in files:
                z.writestr(f"bot/{rel}", text)
        return buf.getvalue()

    def _unpack(self, data: bytes) -> dict[str, str]:
        """The files (relative to ``bot/``) of a ``.arenabot``; :class:`FileProblems` when it is none or has been tampered with."""
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise FileProblems("这不是有效的 .arenabot 文件（不是 zip，或者文件已损坏）。") from None
        with z:
            try:
                manifest = json.loads(z.read("manifest.json").decode("utf-8"))
            except (KeyError, ValueError, UnicodeDecodeError):
                raise FileProblems("缺少 manifest.json，或者它不是有效的 JSON。") from None
            if not isinstance(manifest, dict) or manifest.get("format") != "arenabot":
                raise FileProblems("manifest.json 的 format 必须是 arenabot。")
            if manifest.get("format_version") != 1:
                raise FileProblems("这个 .arenabot 文件使用了不支持的版本。")
            listed = {str(f.get("path")): f for f in manifest.get("files") or [] if isinstance(f, dict)}
            out: dict[str, str] = {}
            for path, entry in listed.items():
                if not path.startswith("bot/") or ".." in path.split("/"):
                    raise FileProblems(f"文件路径不合法：{shown(path, 40)}")
                try:
                    text = z.read(path).decode("utf-8")
                except (KeyError, UnicodeDecodeError):
                    raise FileProblems(f"清单里的 {shown(path, 40)} 在文件里读不出来。") from None
                if sha256_text(text) != entry.get("sha256"):
                    raise FileProblems(f"{shown(path, 40)} 的 SHA-256 校验值与清单不符：文件已损坏或被改动。")
                out[path[len("bot/") :]] = text
            return out

    def _bot_from_files(self, files: dict[str, str], bot_id: str) -> dict[str, Any]:
        """Check the files of a bot directory and build the bot (id ``bot_id``); :class:`FileProblems` lists every fault."""
        try:
            raw = json.loads(files.get("bot.json") or "null")
        except ValueError:
            raw = None
        if not isinstance(raw, dict):
            raise FileProblems("缺少 bot.json，或者它不是有效的 JSON。")
        problems: list[str] = []
        name = ""
        try:
            name = clean_name(raw.get("name"))
        except Refusal as exc:
            problems.append(f"bot.json 里的名字不合法：{exc.detail}")
        core = empty_core()
        for key in CORE_SECTIONS:
            core[key] = (files.get(f"core/{key}.md") or "").strip()
        titles: list[tuple[str, str]] = []
        for rel in sorted(files):
            m = re.fullmatch(r"core/extra-([a-z0-9][a-z0-9-]{0,31})\.md", rel)
            if m:
                lines = files[rel].split("\n")
                head = _H1_RE.match(lines[0]) if lines else None
                title = ((head.group(1) or "").strip() if head else "") or m.group(1)
                titles.append((title, "\n".join(lines[1:] if head else lines).strip()))
        core["extras"] = titles
        problems.extend(validate_core(core))
        bot = self._blank(bot_id, name or "?", raw.get("avatar") if isinstance(raw.get("avatar"), dict) else None, "import")
        for rel, text in files.items():
            if rel == "arch/notes.md":
                part, sid = "arch_notes", ""
            elif rel == "people.md":
                part, sid = "people", ""
            else:
                m = re.fullmatch(r"rules/([A-Za-z0-9][A-Za-z0-9_-]{0,63})/(digest|playbook|questions)\.md", rel)
                if not m:
                    continue
                part, sid = m.group(2), m.group(1)
            try:
                canonical = normalize_part(part, text)[0]
            except FileProblems as exc:
                problems.extend(f"{rel}：{p}" for p in exc.problems)
                continue
            if part == "arch_notes":
                bot["mem"]["arch_notes"] = canonical
            elif part == "people":
                bot["mem"]["people"] = canonical
            else:
                self._rules(bot, sid, create=True)[part] = canonical
        if problems:
            raise FileProblems(problems)
        try:
            record = json.loads(files.get("record.json") or "null")
        except ValueError:
            record = None
        if isinstance(record, dict):
            games = max(0, int(record.get("games") or 0))
            bot["mem"]["record"] = {"games": games, "wins": min(max(0, int(record.get("wins") or 0)), games), "by_role": record.get("by_role") if isinstance(record.get("by_role"), dict) else {}}
        for rel, text in files.items():
            if rel.startswith("staging/") and rel.endswith(".json"):
                try:
                    p = json.loads(text)
                except ValueError:
                    continue
                if isinstance(p, dict) and isinstance(p.get("after"), str) and p.get("section"):
                    bot["staging"].append({k: p.get(k, "") for k in ("id", "section", "before", "after", "reason", "source")} | {"created_at": int(p.get("created_at") or self._now())})
        self._set_core(bot, core)
        init = raw.get("init") if isinstance(raw.get("init"), dict) else {}
        bot["init"] = {**empty_init(), **{k: init.get(k) for k in empty_init() if k in init}}
        if bot["init"]["status"] not in ("uninitialized", "ready", "failed"):
            bot["init"] = empty_init()
        bot["created_at"] = int(raw.get("created_at") or self._now())
        return bot

    def install_bot_files(self, bot_id: str, files: dict[str, str], *, origin: str = "import") -> None:
        """Replace everything of ``bot_id`` (name, avatar, L0, memory, record, proposals) by the files of a bot directory; the id in the
        file is ignored.  Checked completely first (:class:`FileProblems`; 409 ``name_taken`` when another bot has the name)."""
        self.require(bot_id)
        fresh = self._bot_from_files(files, bot_id)
        self.validate_name(fresh["name"], exclude=bot_id)
        if fresh["avatar"] is None:
            fresh["avatar"] = self.avatars.one(self._taken_avatars(skip=[bot_id]))
        fresh["origin"] = origin if origin in ("default", "user", "import", "migrated") else "import"
        self.bots[bot_id] = fresh

    def import_zip(self, bot_id: str, data: bytes) -> None:
        self.install_bot_files(bot_id, self._unpack(data), origin="import")

    def import_default(self, bot_id: str, name: str) -> None:
        """Replace ``bot_id`` by the default-library bot called ``name`` (any case): its L0, its name and avatar, and what it comes with
        (initialized under the contract and scenario the program ships); 404 when the library has none, 409 ``name_taken`` when another bot
        at the table has that name."""
        self.require(bot_id)
        key = str(name or "").strip().casefold()
        entry = next((n for n in DEFAULT_NAMES if n.casefold() == key), None)
        if entry is None:
            raise not_found(f"默认库里没有「{str(name)[:20]}」。")
        self.validate_name(entry, exclude=bot_id)
        fresh = self._blank(bot_id, entry, copy.deepcopy(self._library_avatars()[DEFAULT_NAMES.index(entry)]), "default")
        self._set_core(fresh, default_core(entry))
        self._initialized(fresh, self.default_sources())
        self.bots[bot_id] = fresh


# --------------------------------------------------------------------------------------------------
# the official contract (L1, files stream: app/storage/contract.py + app/agents/contract.py)
# --------------------------------------------------------------------------------------------------

#: every tool a bot has and every receipt code (app.agents.contract.required_terms())
TOOL_NAMES = ["speak", "speak_template", "pass", "respond", "use_ability", "challenge_moderator", "sleep", "write_note", "read_notes", "compact_context", "jot", "read_contract"]
RECEIPT_CODES = [
    "NO_FLOOR", "NOT_MEMBER", "CHANNEL_CLOSED", "QUOTA_EXCEEDED", "TOO_LONG", "FORMAT", "STALE", "REQUEST_CLOSED", "ABILITY_DISABLED", "GAME_NOT_RUNNING",
    "INVALID_PARAMS", "INVALID_TARGET", "UNKNOWN_CHANNEL", "UNKNOWN_TEMPLATE", "UNKNOWN_ABILITY", "UNKNOWN_REQUEST", "UNKNOWN_PLAYER", "UNKNOWN_TICKET",
    "FORBIDDEN", "CONSTRAINT", "ALREADY_EXISTS", "INTERNAL_ERROR", "NOT_FINISHED",
]
REQUIRED_TERMS = [*TOOL_NAMES, *RECEIPT_CODES]
CONTRACT_FORMAT = "arena-contract"
CUSTOM_VERSION = "custom"
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


def default_contract_markdown() -> str:
    """A contract that passes the check (the mock's own text: short, but it mentions every tool and every receipt code)."""
    tools = "、".join(f"`{t}`" for t in TOOL_NAMES)
    codes = "、".join(f"`{c}`" for c in RECEIPT_CODES)
    return (
        f"---\nformat: {CONTRACT_FORMAT}\nversion: {CONTRACT_VERSION}\n---\n# 平台契约\n\n"
        "这份契约说明一名玩家怎样通过工具与这个平台互动。它只讲平台怎么运作，不涉及任何打法。\n\n"
        "## 必读\n\n"
        "你只能通过工具影响这个世界。你直接写出的文字是内心独白，别人看不到；要让别人听见，必须调用 `speak`，而且只有状态板里标着“你现在可以发言”的频道才发得出去。\n\n"
        "被唤醒不等于必须行动：先看新动态和唤醒原因，判断有没有轮到你的事；没有，就想一想，必要时记一笔，然后调用 `sleep`。发言权不想用或者说完了，就调用 `pass`。\n\n"
        "发言是当众说话：用连贯的口语，把想说的一次说完，不要分条列项，也不要以自己的编号或名字开头。\n\n"
        "每次调用都有回执，先读其中的 `message`。`TOO_LONG` 表示这条超过了单条上限，没有发出，发言权还在，删减后重发；`FORMAT` 表示这条读起来像报告，改成口语后重发。\n\n"
        "## 参考\n\n"
        f"### 工具\n\n玩家的工具有：{tools}。\n\n"
        f"### 回执码\n\n每个会改变世界的工具都会返回回执。回执码有：{codes}。失败时先读 `message`，核对后改正，只重试一次。\n"
    )


def read_header(markdown: str) -> tuple[dict[str, str] | None, str]:
    """``(fields, body)``: the front matter of a contract file and the text after it (``None`` fields: no front matter)."""
    text = (markdown or "").lstrip("\ufeff")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or lines[start].strip() != "---":
        return None, text
    fields: dict[str, str] = {}
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            return fields, "\n".join(lines[index + 1 :])
        content = re.sub(r"(^|\s)#.*$", "", line).strip()
        if not content or ":" not in content:
            continue
        key, _, value = content.partition(":")
        fields[key.strip().lower()] = value.strip().strip("\"'")
    return None, text


def contract_version(markdown: str) -> str:
    fields, _ = read_header(markdown)
    version = (fields or {}).get("version", "")
    return version if VERSION_RE.match(version) else CUSTOM_VERSION


def split_contract(markdown: str) -> tuple[str, str]:
    """``(must_read, reference)``: the texts of the ``## 必读`` and ``## 参考`` sections."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in (markdown or "").splitlines():
        m = re.match(r"^##[ \t]+(.+?)\s*$", line)
        if m:
            current = m.group(1).strip()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return "\n".join(sections.get("必读", [])).strip(), "\n".join(sections.get("参考", [])).strip()


def mentions(markdown: str, term: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", markdown or "") is not None


def validate_contract(markdown: str) -> list[str]:
    """The problems of a contract file, in Chinese (``[]`` = fine): app.agents.contract.validate_contract."""
    text = markdown or ""
    if not text.strip():
        return ["契约是空的。"]
    problems: list[str] = []
    if len(text) > CONTRACT_MAX_CHARS:
        problems.append(f"契约最多 {CONTRACT_MAX_CHARS} 字（现在 {len(text)} 字）。")
    fields, _body = read_header(text)
    if fields is None:
        problems.append(f"缺少格式头：文件开头需要用 --- 包住的一段，其中写 format: {CONTRACT_FORMAT}。")
    else:
        if fields.get("format") != CONTRACT_FORMAT:
            problems.append(f"格式头里的 format 必须是 {CONTRACT_FORMAT}（现在是 {fields.get('format') or '空'}）。")
        version = fields.get("version", "")
        if version and not VERSION_RE.match(version):
            problems.append("格式头里的 version 只能用字母、数字、点、下划线和连字符，最多 32 个字符。")
    headings = [m.group(1).strip() for m in re.finditer(r"(?m)^##[ \t]+(.+?)\s*$", text)]
    for heading in ("必读", "参考"):
        count = headings.count(heading)
        if count == 0:
            problems.append(f"缺少“## {heading}”一节。")
        elif count > 1:
            problems.append(f"“## {heading}”出现了 {count} 次，只能有一节。")
    must_read, reference = split_contract(text)
    if "必读" in headings and not must_read:
        problems.append("“必读”一节是空的。")
    if len(must_read) > CONTRACT_MUST_READ_CHARS:
        problems.append(f"“必读”最多 {CONTRACT_MUST_READ_CHARS} 字（现在 {len(must_read)} 字）。")
    if "参考" in headings and not reference:
        problems.append("“参考”一节是空的。")
    tools = [t for t in TOOL_NAMES if not mentions(text, t)]
    codes = [c for c in RECEIPT_CODES if not mentions(text, c)]
    if tools:
        problems.append("契约没有提到这些工具：" + "、".join(tools) + "。每个工具的名字都要出现在契约里。")
    if codes:
        problems.append("契约没有提到这些回执码：" + "、".join(codes) + "。每个回执码都要出现在契约里。")
    return problems


def utc_stamp(value: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if value is None else value).strftime("%Y%m%d")


class MockContract:
    """``ContractStore``: the markdown in force and where it came from (``source``).  ``broken`` is a switch for the start check
    (``missing`` / ``invalid``): the store itself only ever holds a contract that passed :func:`validate_contract`."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self.clock = clock or time.time
        self.markdown = default_contract_markdown()
        self.origin = "default"
        self.imported_at: int | None = None
        self.broken: str | None = None

    @property
    def sha256(self) -> str:
        return sha256_text(self.markdown)

    def source(self) -> dict[str, Any]:
        return {"origin": self.origin, "version": contract_version(self.markdown), "imported_at": self.imported_at, "sha256": self.sha256}

    def problems(self) -> list[str]:
        """What the contract in force does not pass: ``[]`` normally; a contract edited by hand in the data directory (or made unusable by
        ``/api/mock/files``) lists its faults here (``GET /api/contract`` -> ``problems``)."""
        if self.broken == "missing":
            return ["契约是空的。"]
        if self.broken == "invalid":
            return ["契约没有提到这些工具：jot、read_contract。每个工具的名字都要出现在契约里。"]
        return validate_contract(self.markdown)

    def payload(self) -> dict[str, Any]:
        must_read, _ref = split_contract(self.markdown)
        return {
            "markdown": self.markdown, "source": self.source(), "chars": len(self.markdown), "must_read_chars": len(must_read),
            "required_terms": list(REQUIRED_TERMS), "problems": self.problems(),
        }

    def validate(self, markdown: str) -> list[str]:
        return validate_contract(markdown)

    def set(self, markdown: str) -> None:
        problems = validate_contract(markdown)
        if problems:
            raise FileProblems(problems)
        self.markdown = normalize_newlines(markdown)
        self.origin = "imported"
        self.imported_at = int(self.clock())
        self.broken = None

    def reset(self) -> None:
        self.markdown = default_contract_markdown()
        self.origin = "default"
        self.imported_at = None
        self.broken = None

    def download_name(self) -> str:
        version = contract_version(self.markdown)
        return f"contract-{version}.md" if version != CUSTOM_VERSION else f"contract-custom-{utc_stamp(self.imported_at)}.md"


# --------------------------------------------------------------------------------------------------
# the rules (L2, files stream: app/storage/rules.py): scenario files and study materials
# --------------------------------------------------------------------------------------------------


def default_scenario_markdown(version: str = "2.2.0") -> str:
    return (
        f"---\nschema_version: 2\nid: {DEFAULT_SCENARIO_ID}\nversion: {version}\ntitle: 12 人标准局狼人杀（暗牌局，模拟）\nmanual_heading: 主持人手册\n---\n"
        "# 12 人标准局\n\n本剧本是模拟服务的示例：机制、玩家规则和主持人手册合在一个文件里。\n\n"
        "## 玩家规则\n\n12 名玩家分成好人和狼人两个阵营。白天先发言再投票放逐，夜里各个身份分别行动。好人阵营放逐全部狼人获胜；狼人阵营屠边获胜。\n\n"
        "## 主持人手册\n\n按流程主持：天黑、夜间行动、天亮公布结果、发言、投票放逐。\n"
    )


SCENARIO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_MATERIAL_BYTES = 100 * 1024  # one study material
MAX_MATERIALS = 20  # per scenario
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def parse_scenario(markdown: str, name: str = "scenario.md") -> dict[str, str]:
    """The front matter of a scenario file (``schema_version``, ``id``, ``version``, ``title``); :class:`FileProblems` (Chinese, like the
    loader, the file is called ``name``) when the file cannot be played."""
    text = normalize_newlines(markdown or "")
    if not text.strip():
        raise FileProblems(f"剧本文件 {name} 是空的。")
    lines = text.split("\n")
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or lines[start].strip() != "---":
        raise FileProblems(f"剧本文件 {name} 缺少文件头：开头需要用 --- 包住的 schema_version、id、version、title。")
    end = start + 1
    while end < len(lines) and lines[end].strip() != "---":
        end += 1
    if end >= len(lines):
        raise FileProblems(f"剧本文件 {name} 的文件头没有结束：缺少第二行 ---。")
    fields: dict[str, str] = {}
    for line in lines[start + 1 : end]:
        content = re.sub(r"(^|\s)#.*$", "", line).strip()
        if content and ":" in content and not line.startswith((" ", "\t")):
            key, _, value = content.partition(":")
            fields[key.strip()] = value.strip().strip("\"'")
    problems: list[str] = []
    if fields.get("schema_version") != "2":
        problems.append(f"schema_version 必须是 2（现在是 {fields.get('schema_version') or '空'}）。")
    sid = fields.get("id", "")
    if not SCENARIO_ID_RE.match(sid):
        problems.append("id 只能由字母、数字、下划线和连字符组成，最多 64 个字符。")
    if not fields.get("title"):
        problems.append("缺少 title。")
    if not fields.get("version"):
        problems.append("缺少 version。")
    if "主持人手册" not in text:
        problems.append("缺少“主持人手册”一节（manual_heading 指向的标题）。")
    if problems:
        raise FileProblems(f"剧本文件 {name} 有问题：" + " ".join(problems))
    return fields


class MockRules:
    """``RulesStore``: the scenarios (the file, the study materials, where it came from)."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self.clock = clock or time.time
        self.scenarios: dict[str, dict[str, Any]] = {}
        self.broken: dict[str, str] = {}
        self._install_default()

    def _install_default(self) -> None:
        self.scenarios[DEFAULT_SCENARIO_ID] = {"markdown": default_scenario_markdown(), "origin": "default", "imported_at": None, "materials": {}}

    def ids(self) -> list[str]:
        return sorted(self.scenarios)

    def require(self, sid: str) -> dict[str, Any]:
        if sid not in self.scenarios:
            raise not_found(f"没有这个剧本：{shown(str(sid), 40)}")
        return self.scenarios[sid]

    def scenario_text(self, sid: str) -> str:
        return self.require(sid)["markdown"]

    def materials(self, sid: str) -> dict[str, str]:
        return {slug: m["markdown"] for slug, m in self.require(sid)["materials"].items()}

    def scenario_hash(self, sid: str) -> str:
        return sha256_text(self.scenario_text(sid)) if sid in self.scenarios else ""

    def materials_hash(self, sid: str) -> str:
        return materials_hash(self.materials(sid)) if sid in self.scenarios else ""

    def item(self, sid: str) -> dict[str, Any]:
        """``RulesItem``."""
        s = self.require(sid)
        problem = None
        try:
            fields = parse_scenario(s["markdown"], f"{sid}.md")
        except FileProblems as exc:  # (a file edited by hand in the data directory: the item still lists it, and says why it cannot be played)
            fields, problem = {}, str(exc)
        if self.broken.get(sid) == "invalid":
            problem = problem or f"剧本 {sid}.md 无法加载：身份池共有 11 个身份，但对局有 12 名玩家，无法发牌。"
        elif self.broken.get(sid) == "missing":
            problem = problem or f"读不到剧本 {sid}：这份剧本不见了。"
        return {
            "id": sid, "version": fields.get("version", ""), "title": fields.get("title", sid), "origin": s["origin"], "imported_at": s["imported_at"],
            "sha256": sha256_text(s["markdown"]), "chars": len(s["markdown"]),
            "materials": [{"slug": slug, "title": m["title"], "chars": len(m["markdown"]), "sha256": sha256_text(m["markdown"])} for slug, m in sorted(s["materials"].items())],
            "problem": problem,
        }

    def payload(self) -> dict[str, Any]:
        return {"scenarios": [self.item(i) for i in self.ids()]}

    def set_scenario(self, sid: str, markdown: str) -> None:
        s = self.require(sid)
        fields = parse_scenario(markdown, f"{sid}.md")
        if fields["id"] != sid:
            raise FileProblems(f"剧本文件里的 id 是「{fields['id']}」，与这个剧本的 id「{sid}」不同。")
        s["markdown"] = normalize_newlines(markdown)
        s["origin"] = "imported"
        s["imported_at"] = int(self.clock())
        self.broken.pop(sid, None)

    def reset(self, sid: str) -> None:
        s = self.require(sid)
        if sid != DEFAULT_SCENARIO_ID:
            raise Refusal(400, "这个剧本没有默认版本，不能恢复默认。", "no_default")
        s["markdown"] = default_scenario_markdown()
        s["origin"] = "default"
        s["imported_at"] = None
        self.broken.pop(sid, None)

    def set_material(self, sid: str, slug: str, markdown: str, title: str | None = None) -> None:
        s = self.require(sid)
        if not SLUG_RE.match(slug):
            raise Refusal(400, "材料的标识只能用小写字母、数字和连字符（最多 32 个字符，以字母或数字开头）。")
        text = checked_text(markdown, "材料")
        if not text.strip():
            raise Refusal(400, "材料内容不能为空。")
        if len(text.encode("utf-8")) > MAX_MATERIAL_BYTES:
            raise Refusal(413, f"材料太大了：单份最多 {MAX_MATERIAL_BYTES // 1024} KB。", "too_large")
        if slug not in s["materials"] and len(s["materials"]) >= MAX_MATERIALS:
            raise Refusal(400, f"每个剧本最多 {MAX_MATERIALS} 份学习材料。", "too_many")
        first = next((ln.strip().lstrip("#").strip() for ln in text.split("\n") if ln.strip()), slug)
        s["materials"][slug] = {"title": (title or first or slug)[:60], "markdown": text}

    def delete_material(self, sid: str, slug: str) -> None:
        s = self.require(sid)
        if slug not in s["materials"]:
            raise not_found("没有这份学习材料。")
        del s["materials"][slug]

    def material(self, sid: str, slug: str) -> dict[str, Any]:
        s = self.require(sid)
        if slug not in s["materials"]:
            raise not_found("没有这份学习材料。")
        return s["materials"][slug]

    def download_name(self, sid: str) -> str:
        return f"{sid}-{parse_scenario(self.scenario_text(sid)).get('version', 'custom')}.scenario.md"


def safe_filename(name: str, fallback: str = "file") -> str:
    """H42: a name as part of a download file name: ``\\ / : * ? " < > |`` and control characters removed."""
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f\x7f]", "", str(name or "")).strip().strip(".")
    return cleaned or fallback
