"""H50: the benchmark's questions never reach a learning tool - nothing under ``app/`` (nor the prompts) reads ``bench/``."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from app.agents.learning_brain import build_learning_brain
from app.bots.types import BotMind, ContractDoc, LearningJob, WriteResult

ROOT = Path(__file__).resolve().parents[2]
_PATH_INTO_BENCH = re.compile(r"(?:^|[\\/\s\"'])bench(?:[\\/]|$)")


def _string_constants(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string literal of a module except docstrings (a docstring may *say* that bench/ is out of reach)."""
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def test_no_code_of_the_application_imports_or_names_the_benchmark() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "bench" for a in node.names):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: import bench")
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "bench":
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: from bench")
        for lineno, value in _string_constants(tree):
            if value == "bench" or _PATH_INTO_BENCH.search(value):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {value[:60]!r}")
    assert not offenders, "the application must not read bench/:\n" + "\n".join(offenders)


def test_no_prompt_points_at_the_benchmark() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "prompts").rglob("*.md"))
        if _PATH_INTO_BENCH.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, offenders


class _Port:
    def __init__(self) -> None:
        self.reads: list[tuple[str, str | None]] = []

    def read_source(self, source: str, part: str | None = None) -> str:
        self.reads.append((source, part))
        return "text"

    def write_memory(self, file: str, content: str) -> WriteResult:
        return WriteResult(ok=True, file=file, chars=len(content))

    def propose_core(self, section: str, content: str, reason: str) -> WriteResult:
        return WriteResult(ok=True, file=section)

    def read_record(self, query: str | None = None, start: int | None = None, limit: int = 40) -> str:
        return ""


def _brain(kind: str = "init") -> tuple[Any, _Port]:
    port = _Port()
    job = LearningJob(kind=kind, mind=BotMind(bot_id="b-00000001", name="A"), scenario_id="s", scenario_title="S", contract=ContractDoc(), deadline_s=120)
    return build_learning_brain(job, port), port


def test_the_learning_tools_read_only_the_five_known_sources_never_a_path() -> None:
    brain, port = _brain()
    for source in ("bench/fixtures/situations", "../bench", "fixtures", "bench", "C:/x/y", "/etc/passwd", ""):
        outcome = brain.execute_tool("read_source", {"source": source})
        assert "source 必须是" in outcome.text, (source, outcome.text)  # a refusal that names the five sources
    assert port.reads == [], "an unknown source must never reach the port"
    for source in ("contract", "rules", "materials", "my_core", "my_memory"):
        brain.execute_tool("read_source", {"source": source})
    assert [source for source, _part in port.reads] == ["contract", "rules", "materials", "my_core", "my_memory"]
