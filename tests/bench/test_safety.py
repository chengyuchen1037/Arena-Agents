"""``bench.safety``: a benchmark run touches nothing of the user's - not the data directory, not the system's temporary directory - and a key never shows."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from bench import safety
from bench.safety import (
    DATA_DIR_ENV,
    DEFAULT_RUNS_DIR,
    REPO_ROOT,
    UnsafeDataDir,
    default_out_dir,
    isolated_data_dir,
    real_data_dirs,
    refuse_real_data_dir,
    scrub,
)


def test_the_real_data_directories_are_refused_and_so_is_whatever_contains_or_lies_inside_them(tmp_path: Path) -> None:
    for path in (REPO_ROOT / "data", REPO_ROOT / "data" / "games", REPO_ROOT / "data" / "bench-x", REPO_ROOT / ".demo-data", REPO_ROOT / ".demo-data" / "real", REPO_ROOT):
        with pytest.raises(UnsafeDataDir, match="基准不能写进真实的数据目录"):
            refuse_real_data_dir(path)
    refuse_real_data_dir(tmp_path / "out")  # (a directory of the test's own)
    refuse_real_data_dir(DEFAULT_RUNS_DIR / "20260101-000000-abcd")  # (the default place of a run is none of them)
    assert REPO_ROOT / "data" in [Path(p) for p in map(str, real_data_dirs())] or any(str(p).endswith("data") for p in real_data_dirs())


def test_the_directory_the_app_was_started_with_counts_as_real_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(safety, "_STARTUP_DATA_DIR", str(tmp_path / "users-data"))
    with pytest.raises(UnsafeDataDir):
        refuse_real_data_dir(tmp_path / "users-data" / "games")
    refuse_real_data_dir(tmp_path / "elsewhere")


def test_the_default_output_directory_is_under_the_scratch_directory_of_the_project() -> None:
    out = default_out_dir("20260101-000000-abcd")
    assert out == REPO_ROOT / ".pytest_tmp" / "bench-runs" / "20260101-000000-abcd" == DEFAULT_RUNS_DIR / "20260101-000000-abcd"
    assert ".pytest_tmp/" in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").replace("\\", "/")  # (ignored by git, like every scratch directory)
    assert Path(tempfile.gettempdir()) not in out.parents or Path(tempfile.gettempdir()) == REPO_ROOT


def test_the_isolated_data_directory_lives_inside_the_given_directory_and_is_gone_afterwards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> str:
        raise AssertionError("tempfile must not be used")

    monkeypatch.setattr(tempfile, "mkdtemp", boom)
    monkeypatch.setattr(tempfile, "mkstemp", boom)
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "users"))
    base = tmp_path / "run"
    with isolated_data_dir(base) as data:
        assert data.parent == base and data.name.startswith(".arena-data-") and data.is_dir()
        assert os.environ[DATA_DIR_ENV] == str(data) != str(tmp_path / "users")
        (data / "providers.json").write_text("{}", encoding="utf-8")  # (whatever the app code would write there)
    assert os.environ[DATA_DIR_ENV] == str(tmp_path / "users")  # (the old value is back)
    assert list(base.iterdir()) == [] and not (tmp_path / "users").exists()


def test_the_data_directory_is_restored_and_removed_when_the_run_fails_and_when_there_was_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    with pytest.raises(RuntimeError, match="bang"):
        with isolated_data_dir(tmp_path / "a") as data:
            assert DATA_DIR_ENV in os.environ
            raise RuntimeError("bang")
    assert DATA_DIR_ENV not in os.environ and not data.exists()
    with isolated_data_dir(tmp_path / "b") as first, isolated_data_dir(tmp_path / "b") as second:
        assert first != second and os.environ[DATA_DIR_ENV] == str(second)  # (nested runs do not share a directory)
    assert DATA_DIR_ENV not in os.environ


def test_scrub_removes_every_secret_from_a_text_but_leaves_short_strings_alone() -> None:
    key = "sk-test-0123456789"
    assert scrub(f"401 for {key} and again {key}", [key]) == "401 for *** and again ***"
    assert scrub("a b c", ["a", "b"]) == "a b c"  # (a secret of fewer than six characters is not a secret to hide: it would eat ordinary words)
    assert scrub(RuntimeError(f"bad {key}"), [key]) == "bad ***" and scrub("plain", []) == "plain" and scrub(None, [key]) == "None"
    assert scrub(f"{key}-x", ["", key, "other-secret"]) == "***-x"
