"""Run the node-based frontend tests (tests/frontend/*.js). Skipped when node is missing."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_TESTS = ROOT / "tests" / "frontend"
STATIC_JS = ROOT / "static" / "js"

_WINDOWS_NODE = (
    Path.home()
    / ".dsh"
    / "dsh-runtimes"
    / "dsh-primary-runtime"
    / "dependencies"
    / "node"
    / "bin"
    / "node.exe"
)


def _find_node() -> str | None:
    env = os.environ.get("NODE_BIN")
    if env and Path(env).exists():
        return env
    found = shutil.which("node")
    if found:
        return found
    if _WINDOWS_NODE.exists():
        return str(_WINDOWS_NODE)
    return None


NODE = _find_node()
pytestmark = pytest.mark.skipif(NODE is None, reason="node executable not found")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [NODE, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


@pytest.mark.parametrize("script", sorted(p.name for p in STATIC_JS.glob("*.js")))
def test_static_js_syntax(script: str) -> None:
    result = _run("--check", str(STATIC_JS / script))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", sorted(p.name for p in FRONTEND_TESTS.glob("test_*.js")))
def test_frontend_script(script: str) -> None:
    result = _run(str(FRONTEND_TESTS / script))
    assert result.returncode == 0, f"{script} failed:\n{result.stdout}\n{result.stderr}"
