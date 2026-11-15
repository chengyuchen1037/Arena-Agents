from __future__ import annotations

import re
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT_DIR / "app" / "engine"

# Keep the scan explicit; newly added optional modules are skipped when absent.
SCANNED_MODULES = (
    "tools.py",
    "context.py",
    "llm.py",
    "state.py",
    "expressions.py",
    "scenario.py",
    "director.py",
    "orchestrator.py",
    "choice.py",
    "participant_loop.py",
    "participant_tasks.py",
    "moderator_tools.py",
    "legacy_moderator.py",
    "toolargs.py",
    "sanitize.py",
    "texts.py",
    "events.py",
    "history.py",
    "bindings.py",
    "session_runtime.py",
    "session_migration.py",
    "workspace_runtime.py",
)

FORBIDDEN_TERMS = (
    "白狼王",
    "预言家",
    "女巫",
    "猎人",
    "守卫",
    "平民",
    "狼人阵营",
    "警长",
    "上警",
    "退水",
    "警左",
    "警右",
    "屠边",
    "wolf-team",
    "归票",
    "过麦",
    "战术指南",
    "Plan A",
    "出人",
)

FIXED_PARTICIPANT_ID_RE = re.compile(
    r"(?<![A-Za-z0-9])p(?:0[1-9]|1[0-2])(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def test_engine_modules_do_not_embed_game_specific_terms() -> None:
    violations: list[str] = []

    for module_name in SCANNED_MODULES:
        path = ENGINE_DIR / module_name
        if not path.is_file():
            continue
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            for term in FORBIDDEN_TERMS:
                if term in line:
                    violations.append(
                        f"{path.relative_to(ROOT_DIR)}:{line_number}: forbidden term {term!r}"
                    )
            if FIXED_PARTICIPANT_ID_RE.search(line):
                violations.append(
                    f"{path.relative_to(ROOT_DIR)}:{line_number}: fixed participant ID example"
                )

    assert not violations, "Game-specific coupling found in engine modules:\n" + "\n".join(
        violations
    )
