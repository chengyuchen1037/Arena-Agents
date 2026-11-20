from __future__ import annotations

import os
import shutil
from pathlib import Path

import app.config
from app.storage.paths import get_data_dir

import conftest as project_conftest

REPOSITORY = Path(__file__).resolve().parents[1]
ASSETS = {"config.yml", "scenarios", "prompts", "knowledge", "static", "bots"}  # v0.14: bots/ = the default bot library
RETIRED_SWITCHES = ("ARENA_DISABLE_WORKSPACE", "ARENA_WORKSPACE_DIR", "ARENA_DISABLE_LEGACY_MIGRATION")


def test_conftest_isolates_the_program_directory_and_the_data_directory(tmp_path, isolated_external_storage) -> None:
    project = Path(app.config.BASE_DIR)
    assert project != REPOSITORY and project.is_dir()  # a temporary program directory, not the repository
    assert (project / "static").is_dir() and (project / "config.yml").is_file()
    assert {entry.name for entry in project.iterdir()} == ASSETS  # the code assets and nothing else
    assert not (project / ".env").exists() and not (project / "saves").exists()
    assert not (project / "data").exists()  # the program directory never receives data
    assert get_data_dir() == isolated_external_storage.resolve()
    assert get_data_dir().parent == tmp_path.resolve()
    assert not any(name in os.environ for name in RETIRED_SWITCHES)


def test_the_program_directory_holds_copies_never_links_into_the_repository() -> None:
    """The assets are copied, not linked: a symlink needs a privilege on Windows, and a directory junction is followed by
    some deleters (PowerShell 5.1's ``Remove-Item -Recurse``, older ``shutil.rmtree``), which would delete the
    repository's own ``static/`` etc. when ``pytest-of-*`` is cleaned."""
    project = Path(app.config.BASE_DIR)
    for entry in project.iterdir():
        assert not entry.is_symlink() and not os.path.isjunction(entry), entry.name
    assert not os.path.samefile(project / "config.yml", REPOSITORY / "config.yml")  # not even a hard link
    marker = project / "static" / "index.html"
    before = (REPOSITORY / "static" / "index.html").read_bytes()
    marker.write_text("changed by a test", encoding="utf-8")
    assert (REPOSITORY / "static" / "index.html").read_bytes() == before  # the repository is out of reach


def test_whatever_a_test_leaves_in_the_program_directory_is_gone_for_the_next_one(tmp_path) -> None:
    project = tmp_path / "program"
    project.mkdir()
    for name in project_conftest._CODE_ASSETS:
        project_conftest._copy_asset(name, project)
    pristine = project_conftest._snapshot(project)
    assert len(pristine) > 20 and "config.yml" in pristine and "static/index.html" in pristine

    (project / "data").mkdir()
    (project / "data" / "games.json").write_text("{}", encoding="utf-8")  # a test wrote data next to the program
    (project / ".env").write_text("DEEPSEEK_API_KEY=x", encoding="utf-8")
    (project / "static" / "index.html").write_text("edited", encoding="utf-8")  # ... edited an asset
    (project / "prompts" / "extra.md").write_text("new", encoding="utf-8")  # ... added one
    victim = next((project / "scenarios").glob("*.md"))
    victim.unlink()  # ... and removed one

    project_conftest._restore_program_dir(project, pristine)

    assert {entry.name for entry in project.iterdir()} == ASSETS
    assert project_conftest._snapshot(project) == pristine
    assert (project / "static" / "index.html").read_bytes() == (REPOSITORY / "static" / "index.html").read_bytes()
    assert victim.is_file() and not (project / "prompts" / "extra.md").exists()
    project_conftest._restore_program_dir(project, pristine)  # (nothing to do: stays as it is)
    assert project_conftest._snapshot(project) == pristine


def test_the_clean_up_only_ever_deletes_inside_the_program_directory(tmp_path) -> None:
    project = tmp_path / "program"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    for target in (outside, outside / "keep.txt", project, tmp_path):
        try:
            project_conftest._remove_inside(project, target)
        except AssertionError:
            continue
        raise AssertionError(f"{target} is not inside the program directory and must be refused")
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    shutil.rmtree(project)


def test_the_retired_switches_are_not_read_by_the_application() -> None:
    """The workspace and the v0.9 auto-migration are gone (F20, F25): no code may look at their switches."""
    root = Path(__file__).resolve().parents[1] / "app"
    offenders = sorted(
        f"{path.relative_to(root.parent).as_posix()}: {name}"
        for path in root.rglob("*.py")
        for name in RETIRED_SWITCHES
        if name in path.read_text(encoding="utf-8")
    )
    assert not offenders, offenders
