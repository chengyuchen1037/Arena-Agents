from __future__ import annotations

import os

import app.config
from app.migration import migrate_legacy_data


def test_conftest_isolates_startup_base_dir_and_disables_migration(tmp_path) -> None:
    expected_base = tmp_path.parent / f"{tmp_path.name}-project"
    assert app.config.BASE_DIR == expected_base
    assert expected_base.is_dir()
    assert (expected_base / "static").is_dir()
    assert not (expected_base / ".env").exists()
    assert not (expected_base / "saves").exists()
    assert os.environ["ARENA_DISABLE_LEGACY_MIGRATION"] == "1"
    assert os.environ["ARENA_DISABLE_WORKSPACE"] == "1"

    # startup() imports app.config.BASE_DIR at call time. Even if it reaches the
    # migration call, the default switch prevents all legacy file access.
    report = migrate_legacy_data(app.config.BASE_DIR, {})
    assert report["status"] == "disabled"
    assert not (expected_base / ".env").exists()
    assert not (expected_base / "saves").exists()
