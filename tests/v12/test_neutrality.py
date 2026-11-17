"""The v0.12 engine packages must stay game-agnostic: rules live in the scenario, not in code."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCANNED_PACKAGES = ("app/world", "app/runtime", "app/agents", "app/orchestrator")  # ``app/game`` is exempt

FORBIDDEN_TERMS = (
    "白狼王", "预言家", "女巫", "猎人", "守卫", "平民", "狼人", "警长", "上警", "退水", "警左",
    "警右", "屠边", "归票", "过麦", "战术指南", "Plan A", "出人", "狼队", "werewolf", "villager",
    "white_wolf", "witch",
)
# A literal participant id: the old ``p01``-style ids (any two digits, any case) and the v0.12.1 ``N1``-``N12``
# ids (case-sensitive, so that a variable called ``n1`` is not mistaken for one).  ``\b`` makes ``N123`` /
# ``xN1`` / ``N1_x`` harmless.
FIXED_PARTICIPANT_ID_RE = re.compile(r"\b(?:(?i:p)\d{2}|N\d{1,2})\b")


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


def test_the_scan_covers_the_engine_packages() -> None:
    names = {path.relative_to(ROOT).as_posix() for path in _python_files()}
    assert {"app/world/chat.py", "app/world/core.py", "app/world/game.py", "app/runtime/agent.py", "app/agents/brain.py"} <= names
    assert not any(name.startswith("app/game/") for name in names)  # the orchestrator package (demo bots) is exempt


@pytest.mark.parametrize(
    "text",
    ["p01", "p12", "P07", "p99", "N1", "N12", "N7 Carol", "to=[N3]", '"N5"', "(p03)", "id: N10;"],
)
def test_the_id_pattern_catches_both_id_styles(text: str) -> None:
    assert FIXED_PARTICIPANT_ID_RE.search(text), text


@pytest.mark.parametrize(
    "text",
    ["N123", "xN1", "N1_x", "pN1", "p1", "p123", "n1", "Nx", "N", "ON1", "NN1", "np01", "dm-N1x", "pid", "UNKNOWN_PLAYER", "N1a"],
)
def test_the_id_pattern_leaves_ordinary_code_alone(text: str) -> None:
    assert not FIXED_PARTICIPANT_ID_RE.search(text), text
