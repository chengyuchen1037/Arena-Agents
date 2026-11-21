"""The official contract (L1, H7, H38; docs/v0.12-contracts.md §13.2): the default text, how a contract file is read and checked,
and what every contract has to mention.

The contract says how the *platform* works for a player - how to speak and sleep, what a receipt code means, how notes work, what
a player may and may not rely on.  It never teaches how to play (that comes from the scenario and from the bot's own memory) and
it names no game: like the rest of the engine packages it is neutral.

A contract file is markdown with a small front matter, a ``## 必读`` section (mounted into every bot's prompt, at most
``CONTRACT_MUST_READ_CHARS`` characters) and a ``## 参考`` section (read on demand with the ``read_contract`` tool)::

    ---
    format: arena-contract
    version: v0.14.0
    ---
    # 平台契约
    ## 必读
    ...
    ## 参考
    ### 小标题
    ...

``version`` is the name of the contract (what ``contract-<version>.md`` is called when it is downloaded); a file without one is
"custom".  :func:`validate_contract` is the single check the contract store (``PUT /api/contract``) and the tests use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import app.config as app_config
from app.agents.schemas import bot_tool_names
from app.bots.types import (
    CONTRACT_MAX_CHARS,
    CONTRACT_MUST_READ_CHARS,
    CONTRACT_MUST_READ_HEADING,
    CONTRACT_REFERENCE_HEADING,
    ContractDoc,
    split_contract,
)
from app.world.types import Code

#: ``prompts/contract.md``: the contract that ships with the engine (copied to the data directory on the first run, restored by
#: "恢复默认").  The repository root is two levels above this file.
DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "contract.md"


def default_contract_path() -> Path:
    """The same file below the *program directory* (``app.config.BASE_DIR``): what :func:`load_default_contract` reads, so a test
    that points the program directory at a copy (``tests/conftest.py``) gets that copy.  In a real installation both are one."""
    return Path(app_config.BASE_DIR) / "prompts" / "contract.md"

CONTRACT_FORMAT = "arena-contract"
CUSTOM_VERSION = "custom"
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
#: ``read_contract`` returns at most this many characters in one answer (the default reference is far shorter).
READ_CONTRACT_MAX_CHARS = 12_000

#: The receipt codes a contract has to explain, the ones a player meets first.  ``NOT_FINISHED`` is not a rejection of the world
#: but the answer of ``sleep`` to a player who still owes something; it is a "code" in the tool result all the same.
NOT_FINISHED_CODE = "NOT_FINISHED"
_CODE_ORDER = (
    "NO_FLOOR", "NOT_MEMBER", "CHANNEL_CLOSED", "QUOTA_EXCEEDED", "TOO_LONG", "FORMAT", "STALE", "REQUEST_CLOSED",
    "ABILITY_DISABLED", "GAME_NOT_RUNNING", "INVALID_PARAMS", "INVALID_TARGET", "UNKNOWN_CHANNEL", "UNKNOWN_TEMPLATE",
    "UNKNOWN_ABILITY", "UNKNOWN_REQUEST", "UNKNOWN_PLAYER", "UNKNOWN_TICKET", "FORBIDDEN", "CONSTRAINT", "ALREADY_EXISTS",
    "INTERNAL_ERROR", NOT_FINISHED_CODE,
)


def receipt_codes() -> list[str]:
    """Every receipt code of the world (``Code.ALL``) plus ``NOT_FINISHED``: the player-facing ones first in a fixed order, a
    code the engine may add later after them (a contract then has to be extended: the check names it)."""
    rest = sorted(code for code in Code.ALL if code not in _CODE_ORDER)
    return [*_CODE_ORDER, *rest]


def required_terms(spec: Any | None = None) -> list[str]:
    """What a contract has to mention (H38): the name of every tool a bot has and every receipt code.

    ``spec`` (a ``ScenarioSpec``) narrows the tools to the scenario's: no ``speak_template`` without templates for players, no
    ``use_ability`` without abilities.  Without it - what the contract store has - every tool is required."""
    return [*bot_tool_names(spec), *receipt_codes()]


def mentions(markdown: str, term: str) -> bool:
    """Whether ``term`` is mentioned as a word: ``speak`` is not found in ``speak_template`` (an identifier does not stop at ``_``)."""
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", markdown or "") is not None


def missing_terms(markdown: str, spec: Any | None = None) -> list[str]:
    return [term for term in required_terms(spec) if not mentions(markdown, term)]


# ---------------------------------------------------------------------------------------------------- the file
def read_header(markdown: str) -> tuple[dict[str, str] | None, str]:
    """``(fields, body)``: the front matter of a contract file and the text after it.  ``fields`` is ``None`` when there is no
    front matter (the first line is not ``---``) or it is not closed; a ``#`` that starts a word is a comment."""
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
            return fields, "\n".join(lines[index + 1:])
        content = re.sub(r"(^|\s)#.*$", "", line).strip()
        if not content or ":" not in content:
            continue
        key, _, value = content.partition(":")
        fields[key.strip().lower()] = value.strip().strip("\"'")
    return None, text


def contract_version(markdown: str) -> str:
    """The ``version`` the front matter names, or ``custom`` when it names none (or none that is usable)."""
    fields, _ = read_header(markdown)
    version = (fields or {}).get("version", "")
    return version if VERSION_RE.match(version) else CUSTOM_VERSION


def parse_contract(markdown: str) -> ContractDoc:
    """A ``ContractDoc`` of a contract file (``version`` from its front matter); the text is not checked, see :func:`validate_contract`."""
    return split_contract(markdown, contract_version(markdown))


def load_default_contract(path: Path | None = None) -> ContractDoc:
    """The contract that ships with the engine (``prompts/contract.md``, found below ``app.config.BASE_DIR``)."""
    target = Path(path) if path is not None else default_contract_path()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise FileNotFoundError(f"找不到默认契约文件：{target}") from exc
    return parse_contract(text)


def validate_contract(markdown: str, *, spec: Any | None = None) -> list[str]:
    """The problems of a contract file, in Chinese and naming what is wrong (an empty list = it is fine): the front matter
    (``format: arena-contract``), the two sections, the ``必读`` length, the tools and receipt codes it does not mention."""
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
    for heading in (CONTRACT_MUST_READ_HEADING, CONTRACT_REFERENCE_HEADING):
        count = headings.count(heading)
        if count == 0:
            problems.append(f"缺少“## {heading}”一节。")
        elif count > 1:
            problems.append(f"“## {heading}”出现了 {count} 次，只能有一节。")
    doc = split_contract(text)
    if CONTRACT_MUST_READ_HEADING in headings and not doc.must_read:
        problems.append(f"“{CONTRACT_MUST_READ_HEADING}”一节是空的。")
    if len(doc.must_read) > CONTRACT_MUST_READ_CHARS:
        problems.append(f"“{CONTRACT_MUST_READ_HEADING}”最多 {CONTRACT_MUST_READ_CHARS} 字（现在 {len(doc.must_read)} 字）。")
    if CONTRACT_REFERENCE_HEADING in headings and not doc.reference:
        problems.append(f"“{CONTRACT_REFERENCE_HEADING}”一节是空的。")
    tools = [t for t in bot_tool_names(spec) if not mentions(text, t)]
    codes = [c for c in receipt_codes() if not mentions(text, c)]
    if tools:
        problems.append("契约没有提到这些工具：" + "、".join(tools) + "。每个工具的名字都要出现在契约里。")
    if codes:
        problems.append("契约没有提到这些回执码：" + "、".join(codes) + "。每个回执码都要出现在契约里。")
    return problems


# ---------------------------------------------------------------------------------------------------- the reference
@dataclass(frozen=True)
class ReferenceSection:
    """One ``### 小标题`` of the "参考" part and its text (the text before the first heading has the title ``""``)."""

    title: str
    body: str


_H3_RE = re.compile(r"^###[ \t]+(.+?)\s*$")


def reference_sections(doc: ContractDoc) -> list[ReferenceSection]:
    """The "参考" part cut at its ``###`` headings (a heading inside a code fence does not count)."""
    sections: list[ReferenceSection] = []
    title = ""
    body: list[str] = []
    fenced = False

    def flush() -> None:
        text = "\n".join(body).strip()
        if title or text:
            sections.append(ReferenceSection(title, text))

    for line in (doc.reference or "").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        match = None if fenced else _H3_RE.match(line)
        if match:
            flush()
            title, body = match.group(1).strip(), []
            continue
        body.append(line)
    flush()
    return sections


def _plain(title: str) -> str:
    return re.sub(r"[\s`*_]+", "", title).casefold()


def read_reference(doc: ContractDoc, part: str | None = None) -> tuple[bool, str]:
    """The text ``read_contract(part)`` answers with: ``(ok, text)``.

    No ``part`` = the whole "参考" part.  A ``part`` is a heading of it (or a piece of one - the name of a tool is enough): the
    section whose heading is exactly that, else every section whose heading contains it.  An unknown ``part`` is not an error
    of the platform: the answer names the headings there are, so the bot can ask again."""
    sections = reference_sections(doc)
    if not (doc.reference or "").strip():
        return False, "这份契约没有“参考”部分。"
    wanted = _plain(part or "")
    if not wanted:
        text = (doc.reference or "").strip()
        if len(text) > READ_CONTRACT_MAX_CHARS:
            titles = "、".join(s.title for s in sections if s.title)
            return True, text[:READ_CONTRACT_MAX_CHARS].rstrip() + f"\n\n……（太长，后面的没有显示；用 part 读某一节：{titles}）"
        return True, text
    exact = [s for s in sections if s.title and _plain(s.title) == wanted]
    found = exact or [s for s in sections if s.title and wanted in _plain(s.title)]
    if not found:
        titles = "、".join(s.title for s in sections if s.title) or "（没有小标题，不带 part 读全部）"
        return False, f"没有叫“{part}”的小节。可用的小节：{titles}。"
    return True, "\n\n".join(f"### {s.title}\n\n{s.body}".rstrip() for s in found)


def reference_titles(doc: ContractDoc) -> list[str]:
    return [s.title for s in reference_sections(doc) if s.title]


def must_read_text(doc: ContractDoc, *, limit: int = CONTRACT_MUST_READ_CHARS) -> str:
    """What is mounted into a prompt: the "必读" part.  A contract that passed :func:`validate_contract` is within the limit; one
    that did not (an old snapshot) is cut at a paragraph so that the prompt cannot grow without bound."""
    text = (doc.must_read or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    paragraph = cut.rfind("\n\n")
    return (cut[:paragraph] if paragraph > limit // 2 else cut).rstrip()


__all__ = [
    "CONTRACT_FORMAT",
    "CUSTOM_VERSION",
    "DEFAULT_CONTRACT_PATH",
    "NOT_FINISHED_CODE",
    "READ_CONTRACT_MAX_CHARS",
    "ReferenceSection",
    "contract_version",
    "default_contract_path",
    "load_default_contract",
    "mentions",
    "missing_terms",
    "must_read_text",
    "parse_contract",
    "read_header",
    "read_reference",
    "receipt_codes",
    "reference_sections",
    "reference_titles",
    "required_terms",
    "validate_contract",
]
