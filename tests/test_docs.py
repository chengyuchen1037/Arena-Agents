"""Documentation checks (README, docs/, CHANGELOG): the documents must stay aligned with the code and with each other.

What is checked, and why:

* every relative link and image of README.md, README.en.md, CHANGELOG.md and docs/**/*.md resolves to a file;
* every repository path that a *current* document names in backticks (`app/runtime/agent.py`, `tools/make_diagrams.py`,
  `static/js/*.js` ...) exists - a renamed or deleted file must not live on in the documentation;
* the architecture pictures (docs/images/arch-*.{zh,en}.svg) are well-formed, self-contained SVG, are exactly what
  ``tools/make_diagrams.py`` generates (nobody edits an SVG by hand, the pictures cannot drift from the generator),
  and each README / architecture page embeds the four pictures of its own language;
* the numbers that the pictures and the architecture pages quote (retries, grace period, 60 % / 90 % ...) are the
  defaults of the code;
* every tool the agents have is named in both architecture pages;
* the version number is the same in the code, config.yml, docker-compose.yml, the READMEs, the CHANGELOG, index.html
  and the contract;
* the shipped documents and tools carry no local path or user name.

The plans (docs/v0.12*-plan.md) and the research note are history: they are only link-checked, because they
legitimately name files that no longer exist.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / "docs" / "images"
SVG_NS = "{http://www.w3.org/2000/svg}"

README_ZH = ROOT / "README.md"
README_EN = ROOT / "README.en.md"
ARCH_ZH = ROOT / "docs" / "architecture.md"
ARCH_EN = ROOT / "docs" / "architecture.en.md"

ALL_DOCS = sorted({README_ZH, README_EN, ROOT / "CHANGELOG.md", *(ROOT / "docs").rglob("*.md")})
#: documents that describe the CURRENT state of the code (everything else is history)
CURRENT_DOCS = [
    README_ZH,
    README_EN,
    ARCH_ZH,
    ARCH_EN,
    ROOT / "docs" / "ui-guide.md",
    ROOT / "docs" / "providers.md",
    ROOT / "docs" / "workspaces.md",
]

DIAGRAMS = ("overview", "actor", "worker", "timeline")
LANGS = ("zh", "en")


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _svg_path(name: str, lang: str) -> Path:
    return IMAGES / f"arch-{name}.{lang}.svg"


# ---------------------------------------------------------------------------------------------- links
_FENCE = re.compile(r"^(```|~~~).*?^\1[ \t]*$", re.S | re.M)
_INLINE = re.compile(r"`[^`\n]*`")
_MD_LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_HTML_REF = re.compile(r"""<(?:img|a|source)\b[^>]*?\b(?:src|href)=["']([^"']+)["']""", re.I)


def _prose(text: str) -> str:
    """The text without fenced blocks and inline code (examples there are not links of the document)."""
    return _INLINE.sub("", _FENCE.sub("", text))


def _local_targets(doc: Path):
    text = _prose(_read(doc))
    for match in [*_MD_LINK.finditer(text), *_HTML_REF.finditer(text)]:
        target = match.group(1).strip()
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
            continue  # http(s):, mailto:, an in-page anchor
        target = urllib.parse.unquote(target.split("#", 1)[0].split("?", 1)[0])
        if target:
            yield target


@pytest.mark.parametrize("doc", ALL_DOCS, ids=_rel)
def test_relative_links_and_images_resolve(doc: Path) -> None:
    missing = sorted({t for t in _local_targets(doc) if not (doc.parent / t).resolve().exists()})
    assert not missing, f"{_rel(doc)} links to files that do not exist: {missing}"


def test_every_image_in_docs_images_is_used() -> None:
    """No orphan pictures (they would bloat the repository and mislead): each is referenced by a document."""
    referenced = {Path(t).name for doc in ALL_DOCS for t in _local_targets(doc) if "images/" in t.replace("\\", "/")}
    orphans = sorted(p.name for p in IMAGES.iterdir() if p.is_file() and p.name not in referenced)
    assert not orphans, f"docs/images has files no document uses: {orphans}"


def test_both_readmes_show_the_same_screenshots() -> None:
    """The Chinese and the English README must not drift apart: same screenshots (the architecture SVGs differ by language)."""

    def shots(doc: Path) -> set[str]:
        return {Path(t).name for t in _local_targets(doc) if t.lower().endswith(".png")}

    assert shots(README_ZH) == shots(README_EN)


# ------------------------------------------------------------------------------------ repository paths
_REPO_PATH = re.compile(
    r"`((?:app|tools|static|tests|docs|scenarios|prompts|knowledge)/[^`\s]*"
    r"|config\.yml|docker-compose\.yml|requirements(?:-dev)?\.txt|Dockerfile|CHANGELOG\.md|README(?:\.en)?\.md)`"
)


def _expand_braces(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]*)\}", pattern)
    if not match:
        return [pattern]
    head, tail = pattern[: match.start()], pattern[match.end() :]
    return [p for alt in match.group(1).split(",") for p in _expand_braces(head + alt + tail)]


def _path_exists(rel: str) -> bool:
    rel = re.sub(r":\d+(?:-\d+)?$", "", rel).rstrip(".,;)")  # `file.py:12`, sentence punctuation
    if "..." in rel or any(ch in rel for ch in "<>…"):
        return True  # a placeholder, not a path
    for variant in _expand_braces(rel):
        if any(ch in variant for ch in "*?["):
            if not list(ROOT.glob(variant)):
                return False
        elif not (ROOT / variant).exists():
            return False
    return True


@pytest.mark.parametrize("doc", CURRENT_DOCS, ids=_rel)
def test_repository_paths_named_in_current_docs_exist(doc: Path) -> None:
    text = _FENCE.sub("", _read(doc))
    missing = sorted({m.group(1) for m in _REPO_PATH.finditer(text) if not _path_exists(m.group(1))})
    assert not missing, f"{_rel(doc)} names files that do not exist: {missing}"


# -------------------------------------------------------------------------------- architecture pictures
def _load_generator():
    spec = importlib.util.spec_from_file_location("make_diagrams", ROOT / "tools" / "make_diagrams.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _svg_text(path: Path) -> str:
    """All the text of a picture in document order, one space between the (wrapped) lines."""
    root = ET.fromstring(path.read_bytes())
    parts = ["".join(el.itertext()) for el in root.iter(f"{SVG_NS}text")]
    return re.sub(r"\s+", " ", " ".join(parts))


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("name", DIAGRAMS)
def test_diagram_is_a_wellformed_self_contained_svg(name: str, lang: str) -> None:
    path = _svg_path(name, lang)
    assert path.is_file(), f"{_rel(path)} is missing: run python tools/make_diagrams.py"
    raw = path.read_bytes()
    assert len(raw) < 120_000
    root = ET.fromstring(raw)  # raises on malformed XML
    assert root.tag == f"{SVG_NS}svg"
    assert root.get("viewBox") and root.get("width") and root.get("height")
    tags = {el.tag.split("}")[-1] for el in root.iter()}
    assert not tags & {"script", "foreignObject", "image", "iframe", "a", "use"}, tags
    text = raw.decode("utf-8")
    assert "http://" not in text.replace("http://www.w3.org/2000/svg", "") and "https://" not in text  # loads nothing
    title = root.find(f"{SVG_NS}title")
    desc = root.find(f"{SVG_NS}desc")
    assert title is not None and (title.text or "").strip() and desc is not None and (desc.text or "").strip()


def test_committed_diagrams_are_what_the_generator_writes() -> None:
    generated = _load_generator().render_all()
    assert len(generated) == len(DIAGRAMS) * len(LANGS)
    for path, svg in generated.items():
        assert path.is_file(), f"{_rel(path)} is missing: run python tools/make_diagrams.py"
        assert _read(path) == svg, f"{_rel(path)} is out of date (never edit an SVG by hand): run python tools/make_diagrams.py"


@pytest.mark.parametrize(
    "doc, lang",
    [(README_ZH, "zh"), (ARCH_ZH, "zh"), (README_EN, "en"), (ARCH_EN, "en")],
    ids=lambda v: _rel(v) if isinstance(v, Path) else v,
)
def test_readmes_and_architecture_pages_embed_the_pictures_of_their_language(doc: Path, lang: str) -> None:
    text = _read(doc)
    missing = [n for n in DIAGRAMS if f"arch-{n}.{lang}.svg" not in text]
    assert not missing, f"{_rel(doc)} does not embed the {lang} pictures {missing}"
    other = "en" if lang == "zh" else "zh"
    assert f".{other}.svg" not in text, f"{_rel(doc)} embeds a {other} picture"


# ------------------------------------------------------------------------- numbers quoted by the pages
def test_numbers_in_the_pictures_are_the_code_defaults() -> None:
    from app.runtime.agent import MIN_COMPACT_GAIN, AgentConfig

    cfg = AgentConfig()
    runtime = yaml.safe_load(_read(ROOT / "config.yml"))["runtime"]
    assert (cfg.cancel_grace_s, cfg.llm_retries, cfg.starvation_limit) == (5.0, 2, 2)
    assert (cfg.retry_backoff_s, cfg.compact_at, cfg.trim_at, MIN_COMPACT_GAIN) == (1.0, 0.6, 0.9, 0.1)
    assert (runtime["tick_interval"], runtime["compact_at"], runtime["trim_at"]) == (0.1, 0.6, 0.9)

    expected = {
        "en": {
            "actor": ["cancel_grace_s", "= 5 s", "llm_retries", "= 2 times", "2 cancellations", "starvation_limit", "tick_interval", "0.1 s", "1 s, 2 s"],
            "worker": ["60 %", "90 %", "10 %", "compact_at", "trim_at"],
        },
        "zh": {
            "actor": ["cancel_grace_s", "= 5 秒", "llm_retries", "= 2 次", "2 次后", "starvation_limit", "tick_interval", "0.1 秒", "1 秒、2 秒"],
            "worker": ["60 %", "90 %", "10 %", "compact_at", "trim_at"],
        },
    }
    for lang, pictures in expected.items():
        for name, phrases in pictures.items():
            text = _svg_text(_svg_path(name, lang))
            missing = [p for p in phrases if p not in text]
            assert not missing, f"arch-{name}.{lang}.svg no longer says {missing}: the code defaults changed, update tools/make_diagrams.py"


def test_architecture_pages_quote_the_runtime_defaults() -> None:
    from app.runtime.bus import EventBus

    runtime = yaml.safe_load(_read(ROOT / "config.yml"))["runtime"]
    assert runtime["watchdog"] == 20 and runtime["max_watchdog_wakes"] == 3
    assert inspect.signature(EventBus).parameters["ring"].default == 5000
    zh, en = _read(ARCH_ZH), _read(ARCH_EN)
    for text, phrases in ((zh, ["20 秒", "`max_watchdog_wakes`", "5000", "60%", "90%", "10%"]), (en, ["20 s", "`max_watchdog_wakes`", "5000", "60 %", "90 %", "10 %"])):
        missing = [p for p in phrases if p not in text]
        assert not missing, missing
    assert "连续 3 次" in zh and "After 3 fruitless wake-ups" in en


def test_architecture_pages_name_every_agent_tool() -> None:
    from app.agents.schemas import MODERATOR_TOOL_INTENTS, PLAYER_TOOL_INTENTS, SHARED_TOOL_NAMES, SLEEP_TOOL

    names = {*MODERATOR_TOOL_INTENTS, *PLAYER_TOOL_INTENTS, *SHARED_TOOL_NAMES, SLEEP_TOOL}
    for doc in (ARCH_ZH, ARCH_EN):
        text = _read(doc)
        missing = sorted(n for n in names if f"`{n}`" not in text)
        assert not missing, f"{_rel(doc)} does not mention the tools {missing}"


# ------------------------------------------------------------------------------------------- version
def test_the_version_is_the_same_everywhere() -> None:
    from app.config import APP_VERSION
    from app.game.session import APP_ENGINE_VERSION

    assert re.fullmatch(r"\d+\.\d+\.\d+", APP_VERSION)
    assert APP_ENGINE_VERSION == f"v{APP_VERSION}"
    assert yaml.safe_load(_read(ROOT / "config.yml"))["app"]["version"] == f"v{APP_VERSION}"
    assert re.search(rf"^\s*image:\s*arena-agents:{re.escape(APP_VERSION)}\s*$", _read(ROOT / "docker-compose.yml"), re.M)
    for readme in (README_ZH, README_EN):
        assert f"badge/version-{APP_VERSION}-" in _read(readme), _rel(readme)
        assert f"**{APP_VERSION}**" in _read(readme), f"{_rel(readme)} must name the current version in its release paragraph"
    changelog = [ln for ln in _read(ROOT / "CHANGELOG.md").splitlines() if ln.startswith("## ")]
    assert changelog and changelog[0].startswith(f"## {APP_VERSION}"), changelog[:1]
    index = _read(ROOT / "static" / "index.html")
    cache_busters = set(re.findall(r"\?v=([0-9.]+)", index))
    assert cache_busters == {APP_VERSION}, cache_busters
    assert f"`APP_VERSION = \"{APP_VERSION}\"`" in _read(ROOT / "docs" / "v0.12-contracts.md")
    for doc in (ARCH_ZH, ARCH_EN):
        assert f"v{APP_VERSION}" in _read(doc), _rel(doc)


# --------------------------------------------------------------------------------------------- privacy
#: ``C:\Users\<name>`` / ``/Users/<name>`` / ``/home/<name>`` with a real-looking name (placeholders are fine)
_WIN_USER = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+(?!(?:me|you|name|user|username|Public)\b|[<{$%]|\.\.\.)[^\\/\s\"'`]+", re.I)
_UNIX_USER = re.compile(r"(?:/Users/|/home/)(?!(?:me|you|name|user|username|runner|app)\b|[<{$])[A-Za-z0-9._-]+")
_SECRET = re.compile(r"\bsk-(?!test|secret|demo|xxx|placeholder|not-a-real)[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----")


def _shipped_text_files() -> list[Path]:
    roots = [ROOT / "scenarios", ROOT / "prompts", ROOT / "knowledge", ROOT / "tools"]
    files = [*ALL_DOCS, ROOT / "config.yml", ROOT / ".env.example", ROOT / "docker-compose.yml"]
    for folder in roots:
        files += [p for p in folder.rglob("*") if p.is_file() and p.suffix in {".md", ".py", ".yml", ".json"} and "__pycache__" not in p.parts]
    return sorted(set(files))


@pytest.mark.parametrize("path", _shipped_text_files(), ids=_rel)
def test_shipped_documents_and_tools_carry_no_local_path_or_secret(path: Path) -> None:
    text = _read(path)
    leaks = sorted({m.group(0) for pattern in (_WIN_USER, _UNIX_USER, _SECRET) for m in pattern.finditer(text)})
    assert not leaks, f"{_rel(path)} contains what looks like a local path or a secret: {leaks}"
