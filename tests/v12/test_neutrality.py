"""The v0.12 engine packages must stay game-agnostic: rules live in the scenario, not in code."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED_PACKAGES = ("app/world", "app/runtime", "app/agents", "app/orchestrator")

FORBIDDEN_TERMS = (
    "白狼王", "预言家", "女巫", "猎人", "守卫", "平民", "狼人", "警长", "上警", "退水", "警左",
    "警右", "屠边", "归票", "过麦", "战术指南", "Plan A", "出人", "狼队", "werewolf", "villager",
    "white_wolf", "witch",
)
FIXED_PARTICIPANT_ID_RE = re.compile(r"(?<![A-Za-z0-9])p(?:0[1-9]|1[0-2])(?![A-Za-z0-9])", re.IGNORECASE)


def _python_files() -> list[Path]:
    files: list[Path] = []
    for package in SCANNED_PACKAGES:
        base = ROOT / package
        if base.is_dir():
            files.extend(sorted(base.rglob("*.py")))
        elif base.with_suffix(".py").is_file():
            files.append(base.with_suffix(".py"))
    return files


def test_v12_engine_packages_do_not_embed_game_specific_terms() -> None:
    violations: list[str] = []
    for path in _python_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for term in FORBIDDEN_TERMS:
                if term in line:
                    violations.append(f"{path.relative_to(ROOT)}:{number}: forbidden term {term!r}")
            if FIXED_PARTICIPANT_ID_RE.search(line):
                violations.append(f"{path.relative_to(ROOT)}:{number}: fixed participant id example")
    assert not violations, "Game-specific coupling found:\n" + "\n".join(violations)
