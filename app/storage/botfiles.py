"""The files of a bot: parsing, rendering, validating and atomic reading / writing (v0.14, contract §13.2).

A bot keeps its state in a directory (``<data>/bots/<bot_id>/``; the layout is in docs/v0.14-plan.md §2.1)::

    core/identity.md  voice.md  mind.md  [extra-<slug>.md ...]      L0, only the user changes it
    arch/notes.md                                                     L1, the bot's own operating notes
    rules/<scenario_id>/digest.md  playbook.md  questions.md          L2, the bot's own, per scenario
    people.md                                                         impressions, keyed by the other bot's NAME
    record.json                                                       the engine-kept record

This module knows the *formats* and nothing about which bot or which game: every function takes a path or a text.
:mod:`app.storage.bots` (``BotStore``) builds the bot on top of it (ids, line-up, freezing, the initialization record,
the hashes).  Rules shared by everything here:

* **reading is lenient, writing is canonical**: a text the user wrote by hand (BOM, CRLF, full-width colons, blank lines,
  unknown fields) is understood; what is written back is the canonical text, and ``render(parse(x)) == x`` for canonical
  text;
* **validation reports every fault, in Chinese, naming the part and the numbers**: ``「说话方式」最多 600 字（现在 612 字）。``
  (:class:`BotFileError` carries the list in ``problems``);
* **every write is atomic** (:func:`app.storage._atomic.atomic_write`); nothing is ever cut to fit - a text that is too
  long is refused (the learning tools tell the model, the API tells the user);
* **no history is kept** (C9): a write replaces the file.
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.bots.types import (
    CONFIDENCES,
    CORE_EXTRA_CHARS,
    CORE_EXTRA_MAX,
    CORE_EXTRA_PREFIX,
    CORE_FORMAT,
    CORE_FORMAT_VERSION,
    CORE_HEADINGS,
    CORE_LIMITS,
    CORE_SECTIONS,
    IMPRESSION_CHARS,
    MEMORY_PART_LIMITS,
    MEMORY_PARTS,
    PEOPLE_MAX,
    PLAYBOOK_ENTRY_CHARS,
    PLAYBOOK_MAX,
    SLUG_RE,
    CoreFiles,
    PlaybookEntry,
    WriteResult,
    sha256_text,
)
from app.storage._atomic import atomic_write
from app.storage.errors import StorageError
from app.storage.roster import RecordTally, RosterError, check_text, clean_impression, clean_name

# ---- names of the files and of the parts (Chinese labels are what the user / the model reads in a message) ---------------
CORE_FILE_NAMES = {"identity": "identity.md", "voice": "voice.md", "mind": "mind.md"}
EXTRA_PREFIX = "extra-"
EXTRA_SUFFIX = ".md"
EXTRA_SECTION_PREFIX = "extra:"  # a section of ``stage_core`` / a proposal: ``extra:<slug or title>``
EXTRA_TITLE_CHARS = 20
SLUG_MAX_CHARS = 32
#: A sanity cap on a merged L0 file (the limits of the sections add up to far less; this only stops absurd uploads early).
MAX_CORE_MARKDOWN_CHARS = 20_000
#: A sanity cap on the text handed in for one memory part (the real limits are far smaller; this only stops absurd input early).
MAX_PART_INPUT_CHARS = 20_000

PART_LABELS = {
    "arch_notes": "操作笔记",
    "digest": "规则要点",
    "playbook": "打法",
    "questions": "待解问题",
    "people": "印象",
}
PLAYBOOK_HEADING = "假设"
RECORD_FILE = "record.json"
SCENARIO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: Every read and write of a bot file in this process goes through this lock.  The files are tiny, so nothing is lost; what is
#: gained: nobody reads a half-written set of files (an L0 is written file by file), and on Windows an ``os.replace`` over a
#: file that another thread is just reading cannot fail with "access denied".  Re-entrant: the operations nest.
_IO_LOCK = threading.RLock()


class BotFileError(ValueError):
    """A bot file, or a text meant for one, is not acceptable.  ``problems`` are Chinese sentences (one per fault, in the order
    of the file); ``str(error)`` is all of them, space separated, so it can be shown as it is."""

    def __init__(self, problems: Sequence[str] | str) -> None:
        items = [problems] if isinstance(problems, str) else [str(item) for item in problems]
        items = [item for item in items if item] or ["内容无效。"]
        super().__init__(" ".join(items))
        self.problems: list[str] = items


# ======================================================================================================================
# text helpers
# ======================================================================================================================
def normalize_newlines(text: str) -> str:
    """A byte-order mark in front and every kind of line end -> plain ``\\n`` (what an editor on any system may have written)."""
    return text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def checked_text(value: Any, what: str) -> str:
    """``value`` as text with normalized line ends; a NUL or a lone surrogate (it could not be saved) is a :class:`BotFileError`."""
    text = normalize_newlines("" if value is None else str(value))
    try:
        check_text(text, what)
    except RosterError as exc:
        raise BotFileError(str(exc)) from None
    return text


def _over(label: str, limit: int, now: int, unit: str = "字") -> str:
    return f"「{label}」最多 {limit} {unit}（现在 {now} {unit}）。"


def _one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def read_text(path: Path) -> str:
    """A file as text (``""`` when it is not there): a BOM is dropped, line ends are normalized, bytes that are not UTF-8 are
    replaced rather than failing the whole read.  Anything else that goes wrong is a :class:`StorageError`."""
    try:
        with _IO_LOCK:
            raw = path.read_bytes()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise StorageError("Unable to read a bot file") from exc
    return normalize_newlines(raw.decode("utf-8-sig", errors="replace"))


def remove_file(path: Path) -> None:
    try:
        with _IO_LOCK:
            path.unlink(missing_ok=True)
    except OSError as exc:
        raise StorageError("Unable to remove a bot file") from exc


def file_sha256(path: Path) -> str:
    """The content hash (:func:`app.bots.types.sha256_text` of :func:`read_text`) of a file, ``""`` when it is not there.  Cheap enough
    to be asked for on every request (hashing a 70 KB file takes a fraction of a millisecond): nothing is cached, so an edit
    made outside the program is seen at once."""
    try:
        with _IO_LOCK:
            raw = path.read_bytes()
    except OSError:
        return ""
    return sha256_text(normalize_newlines(raw.decode("utf-8-sig", errors="replace")))


def write_bytes(path: Path, data: bytes) -> None:
    """Atomically replace ``path`` with ``data`` (the directory is created when it is not there)."""
    with _IO_LOCK:
        atomic_write(path, data)


def write_text(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text`` (stripped, one final newline); a blank text removes the file."""
    body = (text or "").strip()
    if not body:
        remove_file(path)
        return
    try:
        data = (body + "\n").encode("utf-8")
    except UnicodeEncodeError:
        raise BotFileError("内容里含有无法保存的字符（不成对的 Unicode 代理字符）。") from None
    write_bytes(path, data)


# ======================================================================================================================
# slugs of the extra sections of L0
# ======================================================================================================================
_SLUG_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


def slug_for_title(title: str, taken: Iterable[str] = (), index: int = 1) -> str:
    """The file slug of an extra section (``core/extra-<slug>.md``): the ASCII letters, digits and hyphens of the title
    (lower-cased; the other characters are dropped); a title without any gets ``x<index>`` (``index`` = its place among the
    extras, from 1); a slug that is taken gets ``-2``, ``-3`` ... and always matches :data:`app.bots.types.SLUG_RE`."""
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
    """A slug for every title, in order.  A title that already has a file (``existing``: case-folded title -> slug) keeps its
    slug, so renaming nothing never renames a file; the others get :func:`slug_for_title`."""
    existing = existing or {}
    out: list[str | None] = [None] * len(titles)
    taken: set[str] = set()
    for index, title in enumerate(titles):
        slug = existing.get(title.strip().casefold())
        if slug and SLUG_RE.match(slug) and slug not in taken:
            out[index] = slug
            taken.add(slug)
    for index, title in enumerate(titles):
        if out[index] is None:
            slug = slug_for_title(title, taken, index + 1)
            out[index] = slug
            taken.add(slug)
    return [str(slug) for slug in out]


# ======================================================================================================================
# L0: the merged markdown (import / export, H41)
# ======================================================================================================================
@dataclass(frozen=True)
class ParsedCore:
    """What :func:`parse_core_markdown` found: the L0 and the name in the file header (a hint only: an import never renames).
    ``warnings`` are the faults of the limits that were not refused (``lenient_limits``)."""

    core: CoreFiles
    name: str | None = None
    warnings: tuple[str, ...] = ()


_H1_RE = re.compile(r"^ {0,3}#(?!#)(?:[ \t\u3000]+(.*?))?[ \t\u3000]*$")
_EXTRA_HEADING_RE = re.compile(r"^" + re.escape(CORE_EXTRA_PREFIX.rstrip("：:")) + r"\s*[:：]\s*(.*)$")
_KEY_VALUE_RE = re.compile(r"^\s*([^:：]+?)\s*[:：]\s*(.*?)\s*$")
_COMMENT_RE = re.compile(r"(?:^|\s)#")
_VERSION_RE = re.compile(r"^1(?:\.0+)?$")
_HEADING_OF = {heading: key for key, heading in CORE_HEADINGS.items()}


def _header_value(raw: str) -> str:
    """A value of the file header: an inline comment (``#`` at the start or after a space) is cut off, matching quotes dropped."""
    match = _COMMENT_RE.search(raw)
    if match:
        raw = raw[: match.start()]
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def _shown(value: str, limit: int = 30) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


def name_hint(name: Any) -> str:
    """The name as it is written in the file header: one line, without anything that would be read as a comment."""
    value = _header_value(_one_line(name))
    return value


def _has_heading_line(text: str) -> bool:
    return any(_H1_RE.match(line) for line in text.split("\n"))


def _parse_header(lines: list[str]) -> tuple[dict[str, str], list[str]]:
    """The ``key: value`` lines of the file header -> (fields, problems).  A key is compared case-folded and NFKC-normalized
    (``ｆｏｒｍａｔ`` works), both colons are accepted, unknown keys are ignored."""
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
        problems.append(f"文件头缺少 format: {CORE_FORMAT}。")
    elif fmt.casefold() != CORE_FORMAT:
        problems.append(f"文件头的 format 必须是 {CORE_FORMAT}（现在是「{_shown(fmt)}」）。")
    version = unicodedata.normalize("NFKC", _header_value(fields.get("version", ""))).strip()
    if not version:
        problems.append(f"文件头缺少 version: {CORE_FORMAT_VERSION}。")
    elif not _VERSION_RE.match(version):
        problems.append(f"文件头的 version 目前只能是 {CORE_FORMAT_VERSION}（现在是「{_shown(version)}」）。")
    return fields, problems


def parse_core_markdown(text: Any, *, require_complete: bool = True, lenient_limits: bool = False) -> ParsedCore:
    """The merged L0 file (``<名字>.core.md``, plan §2.2) -> :class:`ParsedCore`; :class:`BotFileError` (with every fault in
    ``problems``) when it is not acceptable.

    The file starts with a header between two ``---`` lines (``format: arena-core``, ``version: 1``, ``name: …`` which is only a
    hint) and goes on in sections that start with a first-level heading: ``# 身份``, ``# 说话方式``, ``# 分析方式`` and up to
    ``CORE_EXTRA_MAX`` of ``# 附加：<标题>``.  Second-level and deeper headings belong to the text of their section.  Refused:
    no / wrong header, text before the first heading, an unknown or repeated heading, a missing or (``require_complete``) empty
    required section, a section over its limit, too many extra sections.  Lenient: a BOM, CRLF, blank lines in front, ``：`` for
    ``:``, comments and unknown keys in the header.

    ``lenient_limits`` (for a draft a model wrote, which is only shown to the user): the faults of the limits - a section that is
    too long, more than three extras, an empty extra - do not refuse the file; they come back as ``ParsedCore.warnings``."""
    raw = checked_text(text, "文件")
    if len(raw) > MAX_CORE_MARKDOWN_CHARS:
        raise BotFileError(f"文件太大了（最多 {MAX_CORE_MARKDOWN_CHARS} 字，现在 {len(raw)} 字）。")
    lines = raw.split("\n")
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or lines[start].strip() != "---":
        raise BotFileError(
            f"缺少文件头：文件开头要有用 --- 包住的 format: {CORE_FORMAT} 和 version: {CORE_FORMAT_VERSION}。"
        )
    end = start + 1
    while end < len(lines) and lines[end].strip() != "---":
        end += 1
    if end >= len(lines):
        raise BotFileError("文件头没有结束：缺少第二行 ---。")
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
                f"不认识的分区标题「{_shown(heading) or '（空）'}」：只能是 "
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
    core = CoreFiles(
        identity=required.get("identity", ""),
        voice=required.get("voice", ""),
        mind=required.get("mind", ""),
        extras=tuple(extras),
    )
    warnings: list[str] = []
    for problem in validate_core(core, require_complete=False):  # (limits, the number of extras, an empty extra ...)
        if lenient_limits:
            warnings.append(problem)
        elif problem not in problems:
            problems.append(problem)
    if problems:
        raise BotFileError(problems)
    hint = _header_value(fields.get("name", ""))
    return ParsedCore(core=core, name=hint or None, warnings=tuple(warnings))


def render_core_markdown(core: CoreFiles, name: Any = None) -> str:
    """The canonical merged L0 file: header (``format``, ``version`` and, when a name is given, ``name``), then 身份, 说话方式,
    分析方式 and the extra sections in that order, the text of each stripped, one blank line between sections, one final
    newline.  A missing section is written as its bare heading (a template to fill in; importing it again is refused until
    it is filled in).  ``render_core_markdown(parse_core_markdown(x).core, parse_core_markdown(x).name) == x`` for canonical ``x``."""
    head = ["---", f"format: {CORE_FORMAT}", f"version: {CORE_FORMAT_VERSION}"]
    hint = name_hint(name)
    if hint:
        head.append(f"name: {hint}")
    head.append("---")
    blocks: list[str] = []
    for key in CORE_SECTIONS:
        body = getattr(core, key).strip()
        blocks.append(f"# {CORE_HEADINGS[key]}\n{body}" if body else f"# {CORE_HEADINGS[key]}")
    for title, text in core.extras:
        body = text.strip()
        heading = f"# {CORE_EXTRA_PREFIX}{title.strip()}"
        blocks.append(f"{heading}\n{body}" if body else heading)
    return "\n".join(head) + "\n" + "\n\n".join(blocks) + "\n"


def validate_core(core: CoreFiles, *, require_complete: bool = False) -> list[str]:
    """Every fault of an L0 as Chinese sentences (``[]`` = fine): a section over its limit, a heading-like line inside a text
    (it would split the section when the merged file is read again), more than ``CORE_EXTRA_MAX`` extra sections, an extra
    without a title / with a title that is too long or used twice / empty or too long.  ``require_complete`` also asks for
    the three required sections to have text."""
    problems: list[str] = []
    for key in CORE_SECTIONS:
        body = getattr(core, key).strip()
        label = CORE_HEADINGS[key]
        if not body:
            if require_complete:
                problems.append(f"「{label}」不能为空。")
        elif len(body) > CORE_LIMITS[key]:
            problems.append(_over(label, CORE_LIMITS[key], len(body)))
        elif _has_heading_line(body):
            problems.append(f"「{label}」里不能有以“# ”开头的行（会被当成分区标题），请去掉 # 或改用 ##。")
    if len(core.extras) > CORE_EXTRA_MAX:
        problems.append(f"附加分区最多 {CORE_EXTRA_MAX} 份（现在 {len(core.extras)} 份）。")
    seen: set[str] = set()
    for title, text in core.extras:
        name = str(title).strip()
        label = f"{CORE_EXTRA_PREFIX}{name}"
        if not name:
            problems.append("附加分区需要标题。")
        elif len(name) > EXTRA_TITLE_CHARS:
            problems.append(f"附加分区的标题最多 {EXTRA_TITLE_CHARS} 字（现在 {len(name)} 字）：{_shown(name)}")
        elif "\n" in name:
            problems.append("附加分区的标题只能有一行。")
        elif name.casefold() in seen:
            problems.append(f"附加分区「{name}」重复了。")
        seen.add(name.casefold())
        body = str(text).strip()
        if not body:
            problems.append(f"「{label}」不能为空。")
        elif len(body) > CORE_EXTRA_CHARS:
            problems.append(_over(label, CORE_EXTRA_CHARS, len(body)))
        elif _has_heading_line(body):
            problems.append(f"「{label}」里不能有以“# ”开头的行（会被当成分区标题），请去掉 # 或改用 ##。")
    return problems


def clean_core(core: CoreFiles) -> CoreFiles:
    """The same L0 with normalized line ends and every text stripped (what is written to the files)."""
    return CoreFiles(
        identity=checked_text(core.identity, "身份").strip(),
        voice=checked_text(core.voice, "说话方式").strip(),
        mind=checked_text(core.mind, "分析方式").strip(),
        extras=tuple((checked_text(title, "附加分区的标题").strip(), checked_text(text, "附加分区").strip()) for title, text in core.extras),
    )


def core_char_counts(core: CoreFiles, slugs: Sequence[str] | None = None) -> dict[str, Any]:
    """``BotSummary.core.chars`` / ``extras``: ``{"chars": {"identity": n, "voice": n, "mind": n}, "extras": [{"title", "slug", "chars"}]}``."""
    slug_list = list(slugs) if slugs is not None else assign_slugs([title for title, _ in core.extras])
    return {
        "chars": {key: len(getattr(core, key).strip()) for key in CORE_SECTIONS},
        "extras": [
            {"title": title, "slug": slug_list[index] if index < len(slug_list) else "", "chars": len(text.strip())}
            for index, (title, text) in enumerate(core.extras)
        ],
    }


# ======================================================================================================================
# L0: the files of ``core/``
# ======================================================================================================================
@dataclass(frozen=True)
class CoreOnDisk:
    """``core/`` as it is on disk: the L0, the slug of every extra (same order as ``core.extras``), the newest modification
    time of its files (``None`` when there are none)."""

    core: CoreFiles
    slugs: tuple[str, ...] = ()
    updated_at: float | None = None


def extra_files(core_dir: Path) -> list[tuple[str, Path]]:
    """``(slug, path)`` of the ``extra-<slug>.md`` files whose slug is valid, by slug."""
    try:
        paths = sorted(core_dir.glob(f"{EXTRA_PREFIX}*{EXTRA_SUFFIX}"), key=lambda p: p.name)
    except OSError as exc:
        raise StorageError("Unable to list the core directory") from exc
    out: list[tuple[str, Path]] = []
    for path in paths:
        slug = path.name[len(EXTRA_PREFIX) : -len(EXTRA_SUFFIX)]
        if SLUG_RE.match(slug) and path.is_file():
            out.append((slug, path))
    return sorted(out, key=lambda item: item[0])


def parse_extra_file(text: str, slug: str = "") -> tuple[str, str]:
    """The text of an ``extra-<slug>.md`` -> ``(title, text)``: the first line is ``# <title>``, then one blank line and the
    text.  A file that does not start that way is read as text with the slug as its title."""
    lines = normalize_newlines(text).split("\n")
    first = 0
    while first < len(lines) and not lines[first].strip():
        first += 1
    if first < len(lines):
        match = _H1_RE.match(lines[first])
        if match and (match.group(1) or "").strip():
            return (match.group(1) or "").strip(), "\n".join(lines[first + 1 :]).strip()
    return slug, "\n".join(lines).strip()


def render_extra_file(title: str, text: str) -> str:
    return f"# {title.strip()}\n\n{text.strip()}\n"


def read_core_on_disk(core_dir: Path) -> CoreOnDisk:
    """Read ``core/`` leniently (a missing file is an empty section; nothing is validated here: see :func:`validate_core`)."""
    with _IO_LOCK:
        values: dict[str, str] = {}
        newest: float | None = None

        def seen(path: Path) -> None:
            nonlocal newest
            try:
                mtime = path.stat().st_mtime
            except OSError:
                return
            newest = mtime if newest is None else max(newest, mtime)

        for key, filename in CORE_FILE_NAMES.items():
            path = core_dir / filename
            values[key] = read_text(path).strip()
            if values[key]:
                seen(path)
        extras: list[tuple[str, str]] = []
        slugs: list[str] = []
        for slug, path in extra_files(core_dir):
            title, text = parse_extra_file(read_text(path), slug)
            extras.append((title, text))
            slugs.append(slug)
            seen(path)
        return CoreOnDisk(
            core=CoreFiles(identity=values["identity"], voice=values["voice"], mind=values["mind"], extras=tuple(extras)),
            slugs=tuple(slugs),
            updated_at=newest,
        )


def read_core(core_dir: Path) -> CoreFiles:
    return read_core_on_disk(core_dir).core


def write_core(core_dir: Path, core: CoreFiles) -> tuple[str, ...]:
    """Replace ``core/`` with ``core`` (an empty section removes its file; extra files that are no longer wanted are removed).
    Everything is validated first (:func:`validate_core`, not completeness) so a refused L0 changes nothing; the files are
    then written one by one, each atomically.  An extra whose title already has a file keeps that file.  Returns the slugs of
    the extras, in order."""
    cleaned = clean_core(core)
    problems = validate_core(cleaned)
    if problems:
        raise BotFileError(problems)
    with _IO_LOCK:
        existing = {parse_extra_file(read_text(path), slug)[0].strip().casefold(): slug for slug, path in extra_files(core_dir)}
        slugs = assign_slugs([title for title, _ in cleaned.extras], existing)
        for extra_slug, (title, text) in zip(slugs, cleaned.extras):
            write_text(core_dir / f"{EXTRA_PREFIX}{extra_slug}{EXTRA_SUFFIX}", render_extra_file(title, text))
        for key, filename in CORE_FILE_NAMES.items():
            write_text(core_dir / filename, getattr(cleaned, key))
        keep = set(slugs)
        for old_slug, path in extra_files(core_dir):
            if old_slug not in keep:
                remove_file(path)
        return tuple(slugs)


def clear_core(core_dir: Path) -> None:
    """Remove every L0 file (``core:clear``, H37); other files in the directory are left alone."""
    with _IO_LOCK:
        for filename in CORE_FILE_NAMES.values():
            remove_file(core_dir / filename)
        for _slug, path in extra_files(core_dir):
            remove_file(path)


def split_section(section: str) -> tuple[str, str]:
    """``"voice"`` -> ``("voice", "")``; ``"extra:口头禅"`` -> ``("extra", "口头禅")``.  :class:`BotFileError` for anything else."""
    value = str(section or "").strip()
    if value in CORE_SECTIONS:
        return value, ""
    if value.startswith(EXTRA_SECTION_PREFIX):
        ident = value[len(EXTRA_SECTION_PREFIX) :].strip()
        if ident:
            return "extra", ident
    raise BotFileError(
        "分区必须是 " + "、".join(CORE_SECTIONS) + f" 之一，或 {EXTRA_SECTION_PREFIX}<名称>（附加分区）；现在是「{_shown(value)}」。"
    )


def apply_section(on_disk: CoreOnDisk, section: str, text: str) -> CoreFiles:
    """The L0 after replacing one section (no file is touched): a required section by name, an extra by ``extra:<slug>`` or
    ``extra:<title>`` - one that does not exist yet is added (the title is what follows ``extra:``).  Refused (:class:`BotFileError`)
    when that would make more than ``CORE_EXTRA_MAX`` extra sections; the result is not validated further."""
    kind, ident = split_section(section)
    core = on_disk.core
    body = checked_text(text, "分区内容").strip()
    if kind != "extra":
        return CoreFiles(**{**{k: getattr(core, k) for k in CORE_SECTIONS}, kind: body, "extras": core.extras})
    extras = list(core.extras)
    for index, (title, _old) in enumerate(extras):
        slug = on_disk.slugs[index] if index < len(on_disk.slugs) else ""
        if ident == slug or ident.casefold() == title.strip().casefold():
            extras[index] = (title, body)
            break
    else:
        if len(extras) >= CORE_EXTRA_MAX:
            raise BotFileError(f"附加分区最多 {CORE_EXTRA_MAX} 份，已经满了，不能再新增「{_shown(ident)}」。")
        extras.append((ident, body))
    return CoreFiles(identity=core.identity, voice=core.voice, mind=core.mind, extras=tuple(extras))


def core_section_text(on_disk: CoreOnDisk, section: str) -> str:
    """The current text of a section (``""`` for a required section that is empty and for an extra that does not exist)."""
    kind, ident = split_section(section)
    if kind != "extra":
        return str(getattr(on_disk.core, kind)).strip()
    for index, (title, text) in enumerate(on_disk.core.extras):
        slug = on_disk.slugs[index] if index < len(on_disk.slugs) else ""
        if ident == slug or ident.casefold() == title.strip().casefold():
            return text.strip()
    return ""


# ======================================================================================================================
# free-text parts: arch notes, digest, questions
# ======================================================================================================================
def clean_part_text(part: str, value: Any) -> str:
    """``arch_notes`` / ``digest`` / ``questions``: free text, stripped, within ``MEMORY_PART_LIMITS`` (refused, never cut)."""
    label = PART_LABELS.get(part, part)
    text = checked_text(value, label).strip()
    limit = MEMORY_PART_LIMITS.get(part)
    if limit is not None and len(text) > limit:
        raise BotFileError(_over(label, limit, len(text)))
    return text


# ======================================================================================================================
# playbook.md
# ======================================================================================================================
_BULLET_RE = re.compile(r"^\s*(?:[-*•·]+|\d{1,2}[.)、．])\s*")
_HEAD_RE = re.compile(r"^[\[［]([^\]］]*)[\]］]\s*(.*)$")
_H2_RE = re.compile(r"^ {0,3}#{1,6}[ \t\u3000]+(.*?)[ \t\u3000]*#*[ \t\u3000]*$")
_CONFIDENCE_WORDS = {"low": "低", "mid": "中", "medium": "中", "high": "高"}
_TAG_BAD_RE = re.compile(r"[\s|\[\]［］#＃]")
SOURCE_CHARS = 40
TAG_CHARS = 20


def _confidence(value: str) -> str:
    """``中`` / ``高`` / ``低``; a short answer around one of them (``很高``, ``中等``, ``偏低``) counts, a word like ``medium`` too;
    anything else is the lowest confidence."""
    text = value.strip()
    for char in text[:3]:
        if char in CONFIDENCES:
            return char
    return _CONFIDENCE_WORDS.get(text.casefold(), "低")


def _count(value: Any) -> int:
    match = re.match(r"\s*(\d+)", str(value))
    return int(match.group(1)) if match else 0


def _clean_source(value: Any) -> str:
    text = _one_line(value).replace("|", "/").replace("[", "(").replace("]", ")").replace("［", "(").replace("］", ")")
    return text[:SOURCE_CHARS].strip()


def _clean_tag(value: Any) -> str:
    return _TAG_BAD_RE.sub("", str(value))[:TAG_CHARS]


def normalize_entry(entry: PlaybookEntry) -> PlaybookEntry:
    """The entry as it is written and read back: one-line text, a valid confidence, non-negative counters, a clean source, tags
    without spaces / brackets / ``|`` / ``#`` (each once)."""
    tags: list[str] = []
    for tag in entry.tags:
        cleaned = _clean_tag(tag)
        if cleaned and cleaned not in tags:
            tags.append(cleaned)
    confidence = entry.confidence if entry.confidence in CONFIDENCES else "低"
    return PlaybookEntry(
        text=_one_line(entry.text),
        confidence=confidence,
        support=max(0, int(entry.support or 0)),
        counter=max(0, int(entry.counter or 0)),
        source=_clean_source(entry.source),
        tags=tuple(tags),
    )


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


def parse_playbook_ex(text: Any) -> tuple[list[PlaybookEntry], int]:
    """``playbook.md`` -> ``(entries, ignored)``; ``ignored`` counts the lines that looked like content but gave no entry.

    The canonical line is ``- [把握:中|支持:3|反例:1|来源:复盘 2026-11-20|#对跳|#投票] 一句话``.  Lenient: ``[`` / ``]`` may be
    full-width, so may ``：`` ``｜`` ``＃``; a bullet without the brackets is an entry with the lowest confidence; unknown fields
    and lines that are not entries (headings, prose, bullets under another ``##`` heading) are ignored; only the section
    ``## 假设`` (or text before any heading) holds entries."""
    entries: list[PlaybookEntry] = []
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
        body = _one_line(body)
        if not body:
            ignored += 1
            continue
        entries.append(
            normalize_entry(
                PlaybookEntry(
                    text=body,
                    confidence=fields.get("confidence", "低"),
                    support=fields.get("support", 0),
                    counter=fields.get("counter", 0),
                    source=fields.get("source", ""),
                    tags=tuple(fields["tags"]),
                )
            )
        )
    return entries, ignored


def parse_playbook(text: Any) -> list[PlaybookEntry]:
    return parse_playbook_ex(text)[0]


def render_entry(entry: PlaybookEntry) -> str:
    entry = normalize_entry(entry)
    head = [f"把握:{entry.confidence}", f"支持:{entry.support}", f"反例:{entry.counter}"]
    if entry.source:
        head.append(f"来源:{entry.source}")
    head.extend(f"#{tag}" for tag in entry.tags)
    return f"- [{'|'.join(head)}] {entry.text}"


def render_playbook(entries: Sequence[PlaybookEntry]) -> str:
    """The canonical ``playbook.md`` (``""`` for no entries): the heading ``## 假设`` and one line per entry, one final newline."""
    if not entries:
        return ""
    return f"## {PLAYBOOK_HEADING}\n" + "\n".join(render_entry(entry) for entry in entries) + "\n"


def check_playbook(entries: Sequence[PlaybookEntry]) -> list[str]:
    """Every fault of a playbook: more than ``PLAYBOOK_MAX`` entries, an entry that is empty or longer than ``PLAYBOOK_ENTRY_CHARS``."""
    problems: list[str] = []
    if len(entries) > PLAYBOOK_MAX:
        problems.append(f"打法最多 {PLAYBOOK_MAX} 条（现在 {len(entries)} 条）。")
    for index, entry in enumerate(entries, start=1):
        text = _one_line(entry.text)
        if not text:
            problems.append(f"打法第 {index} 条没有内容。")
        elif len(text) > PLAYBOOK_ENTRY_CHARS:
            problems.append(f"打法第 {index} 条最多 {PLAYBOOK_ENTRY_CHARS} 字（现在 {len(text)} 字）：{_shown(text, 16)}")
    return problems


# ======================================================================================================================
# people.md
# ======================================================================================================================
def parse_people_ex(text: Any) -> tuple[dict[str, str], int]:
    """``people.md`` -> ``({name: impression}, ignored)``.  Lines are ``- <名字>：<印象>`` (``:`` works too, the bullet is optional,
    the name ends at the first colon); a heading, a blank line and an empty impression (also ``（暂无）``) are no entry; a line
    that is not understood counts as ``ignored``.  A name written twice (any case) keeps the later impression at the place of
    the first."""
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
                continue  # "Bob：" / "Bob：（暂无）": nothing to remember
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
    """The canonical ``people.md`` (``""`` for nobody): ``- <名字>：<印象>`` per line, one final newline."""
    lines = []
    for name, impression in people.items():
        value = clean_impression(impression)
        if str(name).strip() and value:
            lines.append(f"- {_one_line(name)}：{value}")
    return "\n".join(lines) + "\n" if lines else ""


def check_people(people: dict[str, str]) -> list[str]:
    """Every fault of the impressions: more than ``PEOPLE_MAX`` people, a name that is not allowed, an impression over ``IMPRESSION_CHARS``."""
    problems: list[str] = []
    if len(people) > PEOPLE_MAX:
        problems.append(f"印象最多 {PEOPLE_MAX} 人（现在 {len(people)} 人）。")
    for name, impression in people.items():
        try:
            clean_name(name)
        except RosterError as exc:
            problems.append(f"印象里的名字「{_shown(str(name), 12)}」不合法：{exc}")
            continue
        n = len(clean_impression(impression))
        if n > IMPRESSION_CHARS:
            problems.append(f"对「{name}」的印象最多 {IMPRESSION_CHARS} 字（现在 {n} 字）。")
    return problems


# ======================================================================================================================
# reading and writing a part of the memory (the files of one bot)
# ======================================================================================================================
def check_scenario_id(scenario_id: str) -> str:
    """The id of a scenario as a path component: letters, digits, ``_`` and ``-``, at most 64 characters (never a separator or ``..``)."""
    value = str(scenario_id or "")
    if not SCENARIO_ID_RE.match(value):
        raise BotFileError("剧本 id 只能由字母、数字、下划线和连字符组成。")
    return value


def part_relpath(part: str, scenario_id: str = "") -> str:
    """Where a part of the memory lives inside the bot directory (a posix relative path)."""
    if part == "arch_notes":
        return "arch/notes.md"
    if part == "people":
        return "people.md"
    if part in ("digest", "playbook", "questions"):
        return f"rules/{check_scenario_id(scenario_id)}/{part}.md"
    raise BotFileError(f"不认识的记忆部分「{part}」，只能是 " + "、".join(MEMORY_PARTS) + "。")


def unknown_tags(entries: Sequence[PlaybookEntry], known: Iterable[str]) -> list[str]:
    """The situation tags the entries use that are not in ``known`` (the tags of the scenario's ``situations``), in order of
    appearance, each once.  An unknown tag is no fault - the scenario may have been changed, the entry is simply never retrieved
    by it - so this is only ever a hint."""
    wanted = {str(tag) for tag in known}
    found: list[str] = []
    for entry in entries:
        for tag in entry.tags:
            if tag not in wanted and tag not in found:
                found.append(tag)
    return found


def normalize_part(part: str, content: Any, *, known_tags: Iterable[str] | None = None) -> tuple[str, str]:
    """A text meant for ``part`` -> ``(canonical text, note)`` or :class:`BotFileError` (every fault in ``problems``).  ``note`` is
    a Chinese remark about what was dropped or what will not work (``""`` when there is nothing to say).  An empty text is fine: it
    clears the part.  A playbook / people text that is not empty but gives no entry at all is refused (it is not understood).
    ``known_tags`` (playbook only): the situation tags of the scenario; tags outside it are only mentioned in the note."""
    if part not in MEMORY_PARTS:
        raise BotFileError(f"不认识的记忆部分「{part}」，只能是 " + "、".join(MEMORY_PARTS) + "。")
    label = PART_LABELS[part]
    raw = checked_text(content, label)
    if len(raw) > MAX_PART_INPUT_CHARS:
        raise BotFileError(f"「{label}」太长了（最多 {MAX_PART_INPUT_CHARS} 字，现在 {len(raw)} 字）。")
    if part == "playbook":
        entries, ignored = parse_playbook_ex(raw)
        if raw.strip() and not entries:
            raise BotFileError(
                "没有读出任何一条打法：每条写成一行「- [把握:中|支持:0|反例:0|来源:复盘|#标签] 一句话」。"
            )
        problems = check_playbook(entries)
        if problems:
            raise BotFileError(problems)
        notes = [f"有 {ignored} 行没有读出，已忽略。"] if ignored else []
        if known_tags is not None:
            missing = unknown_tags(entries, known_tags)
            if missing:
                notes.append("有标签不在这个剧本的情境标签里，按它检索不到：" + "、".join(f"#{tag}" for tag in missing) + "。")
        return render_playbook(entries).strip(), "".join(notes)
    if part == "people":
        people, ignored = parse_people_ex(raw)
        if raw.strip() and not people and ignored:
            raise BotFileError("没有读出任何一条印象：每条写成一行「- 名字：一句话印象」。")
        problems = check_people(people)
        if problems:
            raise BotFileError(problems)
        return render_people(people).strip(), (f"有 {ignored} 行没有读出，已忽略。" if ignored else "")
    return clean_part_text(part, raw), ""


def read_part(bot_dir: Path, part: str, scenario_id: str = "") -> str:
    """The text of a part as it is on disk (``""`` when there is none)."""
    return read_text(bot_dir / part_relpath(part, scenario_id)).strip()


def write_part(bot_dir: Path, part: str, content: Any, scenario_id: str = "", *, known_tags: Iterable[str] | None = None) -> WriteResult:
    """Validate ``content`` for ``part`` and replace the file atomically (the canonical text is written: a playbook is re-rendered,
    impressions are re-rendered, free text is stripped); an empty text removes the file.  The result says what happened in
    Chinese; a refused text changes nothing (``ok`` is ``False``, the message names the fault and its limit).  ``known_tags``: see
    :func:`normalize_part` (a playbook tag the scenario does not know is written all the same, the message says so)."""
    label = PART_LABELS.get(part, part)
    try:
        path = bot_dir / part_relpath(part, scenario_id)
        text, note = normalize_part(part, content, known_tags=known_tags)
    except BotFileError as exc:
        return WriteResult(False, f"{exc} 没有保存，请改好后重新写入。", file=part)
    write_text(path, text)
    if not text:
        return WriteResult(True, f"已清空{label}。", file=part, chars=0)
    detail = ""
    if part == "playbook":
        detail = f"{len(parse_playbook(text))} 条，"
    elif part == "people":
        detail = f"{len(parse_people(text))} 人，"
    message = f"已写入{label}（{detail}{len(text)} 字）。" + (note if note else "")
    return WriteResult(True, message, file=part, chars=len(text))


def part_stats(bot_dir: Path, scenario_id: str = "") -> dict[str, int]:
    """``BotSummary.memory_stats`` without the record: characters of ``arch_notes`` / ``digest`` / ``questions``, number of ``playbook`` entries and of ``people``."""
    sid = check_scenario_id(scenario_id) if scenario_id else ""
    stats = {
        "arch_notes": len(read_part(bot_dir, "arch_notes")),
        "digest": len(read_part(bot_dir, "digest", sid)) if sid else 0,
        "playbook": len(parse_playbook(read_part(bot_dir, "playbook", sid))) if sid else 0,
        "questions": len(read_part(bot_dir, "questions", sid)) if sid else 0,
        "people": len(parse_people(read_part(bot_dir, "people"))),
    }
    return stats


# ======================================================================================================================
# record.json
# ======================================================================================================================
def _non_negative(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def read_record(bot_dir: Path) -> RecordTally:
    """``record.json`` -> the tally (a missing or broken file is an empty record; wins never exceed games)."""
    try:
        data = json.loads(read_text(bot_dir / RECORD_FILE) or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    games = _non_negative(data.get("games"))
    wins = min(_non_negative(data.get("wins")), games)
    by_role: dict[str, list[int]] = {}
    raw = data.get("by_role")
    if isinstance(raw, dict):
        for role, pair in raw.items():
            if isinstance(role, str) and role and isinstance(pair, (list, tuple)) and len(pair) >= 2:
                played = _non_negative(pair[0])
                by_role[role] = [played, min(_non_negative(pair[1]), played)]
    return RecordTally(games, wins, by_role)


def write_record(bot_dir: Path, tally: RecordTally) -> None:
    """``{"games": n, "wins": n, "by_role": {"<身份名>": [局数, 胜场]}}``, atomically; an empty record removes the file."""
    if not tally.games and not tally.by_role:
        remove_file(bot_dir / RECORD_FILE)
        return
    data = {"games": tally.games, "wins": tally.wins, "by_role": {role: list(pair) for role, pair in sorted(tally.by_role.items())}}
    write_bytes(bot_dir / RECORD_FILE, (json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))


def add_record(bot_dir: Path, role_label: str, won: bool | None) -> RecordTally:
    """Count one finished game for the bot (``won=None``: an outcome nobody knows leaves the record alone); returns the new tally."""
    with _IO_LOCK:  # (read, add, write: two games finishing at once must both count)
        tally = read_record(bot_dir)
        if won is None:
            return tally
        tally.add(_one_line(role_label) or "未知身份", bool(won))
        write_record(bot_dir, tally)
        return tally


def record_text(tally: RecordTally) -> str:
    """``BotMind.record``: the ``## 战绩`` text (``场次 7，胜 4，负 3，胜率 57%`` and a line per role), ``""`` before the first game."""
    return tally.render() if tally.games else ""


def record_summary(tally: RecordTally) -> str:
    """``BotSummary.memory_stats.record``: ``场次 7 · 胜 4 · 胜率 57%`` (``""`` before the first game)."""
    if not tally.games:
        return ""
    return f"场次 {tally.games} · 胜 {tally.wins} · 胜率 {tally.wins / tally.games:.0%}"


def tally_from_text(text: str) -> RecordTally:
    """A ``## 战绩`` text written by v0.13 -> the tally (for the migration)."""
    return RecordTally.parse(text or "")


__all__ = [
    "BotFileError",
    "CORE_FILE_NAMES",
    "CoreOnDisk",
    "EXTRA_SECTION_PREFIX",
    "EXTRA_TITLE_CHARS",
    "MAX_CORE_MARKDOWN_CHARS",
    "PART_LABELS",
    "ParsedCore",
    "RECORD_FILE",
    "SCENARIO_ID_RE",
    "add_record",
    "apply_section",
    "assign_slugs",
    "check_people",
    "check_playbook",
    "check_scenario_id",
    "checked_text",
    "clean_core",
    "clean_part_text",
    "clear_core",
    "core_char_counts",
    "core_section_text",
    "extra_files",
    "file_sha256",
    "name_hint",
    "normalize_entry",
    "normalize_newlines",
    "normalize_part",
    "parse_core_markdown",
    "parse_extra_file",
    "parse_people",
    "parse_people_ex",
    "parse_playbook",
    "parse_playbook_ex",
    "part_relpath",
    "part_stats",
    "read_core",
    "read_core_on_disk",
    "read_part",
    "read_record",
    "read_text",
    "record_summary",
    "record_text",
    "remove_file",
    "render_core_markdown",
    "render_entry",
    "render_extra_file",
    "render_people",
    "render_playbook",
    "slug_for_title",
    "split_section",
    "tally_from_text",
    "unknown_tags",
    "validate_core",
    "write_bytes",
    "write_core",
    "write_part",
    "write_record",
    "write_text",
]
