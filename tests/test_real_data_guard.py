"""The test process never resolves to the REAL data directory (``<repository>/data``).

v0.14 incident: a module-scoped fixture ran before the autouse per-test isolation and started a demo game against the real
data directory - the app then migrated the real roster to bots and rewrote the real ``settings.json``.  ``tests/conftest.py``
now points ``ARENA_DATA_DIR`` at a throw-away directory as soon as it is imported (before ``app`` is, and before any test
module is collected); these tests pin that.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import conftest as project_conftest
from app.storage.paths import get_data_dir

REPOSITORY = Path(__file__).resolve().parents[1]


def test_the_process_wide_data_directory_is_a_throwaway_one() -> None:
    process_dir = Path(project_conftest._PROCESS_DATA_ROOT).resolve()
    assert process_dir != REPOSITORY
    # outside the repository, or inside its git-ignored scratch space - never loose in the repository
    assert REPOSITORY not in process_dir.parents or ".pytest_tmp" in process_dir.parts
    assert (REPOSITORY / "data").resolve() != process_dir / "data"


def test_a_module_scoped_fixture_cannot_reach_the_real_data_directory(
    _module_data_dir: Path,
) -> None:
    """What a module-scoped fixture sees (it is set up BEFORE the autouse isolation of a test)."""
    assert _module_data_dir != (REPOSITORY / "data").resolve()
    assert REPOSITORY / "data" not in _module_data_dir.parents


@pytest.fixture(scope="module")
def _module_data_dir() -> Path:
    # (no monkeypatching here on purpose: this is exactly the situation of a module-scoped fixture)
    return get_data_dir()


def test_every_test_gets_its_own_data_directory_below_its_tmp_path(tmp_path: Path) -> None:
    assert get_data_dir() == (tmp_path / "arena-external-data").resolve()
    assert get_data_dir() != (REPOSITORY / "data").resolve()


def test_nothing_in_the_environment_points_at_the_repository_data_directory() -> None:
    value = os.environ.get("ARENA_DATA_DIR", "")
    assert value and Path(value).resolve() != (REPOSITORY / "data").resolve()
