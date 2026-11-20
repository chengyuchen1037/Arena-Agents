"""The two real-model tools as a user runs them: ``python tools/<script>.py ...`` in a NEW process, from any working directory.

The tests of the tools' main flows (``test_tools_real_smoke.py``, ``test_tools_init_default_bots.py``) call the functions in-process; what only a real command
line shows - that the script finds ``app`` and ``devlib`` whatever the working directory is, that the report can be printed to a pipe with Chinese in it, the exit
codes, that a refused run leaves no scratch directory behind - is checked here.  Nothing here reaches a model: only runs that stop before any model call (``--help``,
``--check``, bad arguments, a source without keys) are made.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from tools_fakes import make_source

ROOT = Path(__file__).resolve().parents[1]
SCRATCH_ROOT = ROOT / ".demo-data"  # where the tools make their scratch directories (git-ignored)


def cli(script: str, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONUTF8": "1"}
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / script), *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180
    )


def scratch_dirs() -> set[str]:
    return {p.name for p in SCRATCH_ROOT.glob("arena-*")} if SCRATCH_ROOT.is_dir() else set()


@pytest.mark.parametrize("script, options", [
    ("real_smoke.py", ["--from", "--init", "--core-from-sample", "--max-calls", "--oneshots", "--resume-calls"]),
    ("init_default_bots.py", ["--from", "--model", "--write-library", "--dry-run", "--check", "--max-calls"]),
])
def test_the_help_is_printed_from_any_working_directory(tmp_path: Path, script: str, options: list[str]) -> None:
    done = cli(script, "--help", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "docs/real-model-runs.md" in done.stdout and "Exit code:" in done.stdout and all(option in done.stdout for option in options)
    assert done.stderr == ""


def test_the_library_check_runs_without_a_model_a_source_or_a_trace_and_leaves_no_scratch_behind(tmp_path: Path) -> None:
    before = scratch_dirs()
    done = cli("init_default_bots.py", "--check", cwd=tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "the default bot library is up to date" in done.stdout and "contract " in done.stdout and "scenario werewolf_12p" in done.stdout
    assert scratch_dirs() == before  # (the scratch data directory of the check is deleted again)


def test_the_check_of_a_library_that_is_not_one_is_refused(tmp_path: Path) -> None:
    done = cli("init_default_bots.py", "--check", "--library", str(tmp_path), cwd=tmp_path)
    assert done.returncode == 1 and "no default bot library at" in done.stderr  # (``SystemExit`` with a message: it goes to stderr, exit code 1)


def test_arguments_that_make_no_sense_are_refused_with_exit_code_2(tmp_path: Path) -> None:
    for script, args, message in [
        ("init_default_bots.py", [], "--from is required (except with --check)"),
        ("init_default_bots.py", ["--from", "x", "--only", "Alice", "--all"], "--only and --all exclude each other"),
        ("init_default_bots.py", ["--from", "x", "--max-calls", "0"], "--max-calls must be at least 1"),
        ("real_smoke.py", ["--from", "x", "--players", "13"], "--players must be between 1 and 12"),
        ("real_smoke.py", ["--from", "x", "--oneshots", "5"], "--oneshots must be between 0 and --players"),
        ("real_smoke.py", ["--from", "x", "--init", "maybe"], "invalid choice"),
        ("real_smoke.py", [], "--from"),
    ]:
        done = cli(script, *args, cwd=tmp_path)
        assert done.returncode == 2 and message in done.stderr, (script, args, done.stderr)
        assert done.stdout == ""


@pytest.mark.parametrize("script, args", [("real_smoke.py", []), ("init_default_bots.py", ["--dry-run"])])
def test_a_source_without_a_registry_or_without_keys_is_refused_and_no_scratch_directory_is_left(tmp_path: Path, script: str, args: list[str]) -> None:
    from app.providers.store import ProviderStore

    before = scratch_dirs()
    empty = tmp_path / "empty"
    empty.mkdir()
    done = cli(script, "--from", str(empty), *args, cwd=tmp_path)
    assert done.returncode == 1 and "missing providers.json" in done.stderr, done.stderr
    keyless = tmp_path / "keyless"
    ProviderStore(keyless).create_provider("P", "openai_chat", "https://example.invalid", "")  # (a registry whose providers have no stored key)
    done = cli(script, "--from", str(keyless), *args, cwd=tmp_path)
    assert done.returncode == 1 and "missing secrets.v1.enc" in done.stderr, done.stderr
    assert scratch_dirs() == before


def test_a_dry_run_of_the_library_initialization_from_the_command_line_calls_no_model(tmp_path: Path) -> None:
    """The whole command line up to the plan: scratch directory, copy of the providers, the model, the table of the library - and then it stops."""
    source = make_source(tmp_path)
    before_scratch = scratch_dirs()
    done = cli("init_default_bots.py", "--from", str(source), "--model", "Fake Provider/big", "--dry-run", "--max-calls", "50", cwd=tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "initialize the default bot library" in done.stdout and "model: Fake Provider / big" in done.stdout
    assert "to initialize (not up to date): Alice, Bob, Carol" in done.stdout and "budget: at most 50 model calls" in done.stdout
    assert "dry run: no model was called, nothing was written" in done.stdout and done.stderr == ""
    assert scratch_dirs() == before_scratch
