from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.engine.scenario import ScenarioDocument, ScenarioError, load_scenario


MINIMAL_SCENARIO = """---
schema_version: 1
id: minimal
version: 1.0.0
entry: start
workflow:
  - id: start
    do: []
    next: finish
  - id: finish
    do: []
---
# Minimal scenario

Body text.
"""


def _write_scenario(
    tmp_path: Path,
    source: str = MINIMAL_SCENARIO,
    name: str = "scenario.md",
) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def test_scenario_snapshot_roundtrip(tmp_path: Path) -> None:
    path = _write_scenario(tmp_path)

    document = load_scenario(path)
    restored = ScenarioDocument.from_snapshot(document.snapshot())

    assert document.scenario_id == "minimal"
    assert document.version == "1.0.0"
    assert document.body == "# Minimal scenario\n\nBody text.\n"
    assert restored == document
    assert restored.snapshot() == document.snapshot()


def test_scenario_checksum_tracks_exact_markdown_source(tmp_path: Path) -> None:
    path = _write_scenario(tmp_path)
    document = load_scenario(path)

    assert document.checksum == hashlib.sha256(
        MINIMAL_SCENARIO.encode("utf-8")
    ).hexdigest()

    updated_source = MINIMAL_SCENARIO.replace("Body text.", "Changed body.")
    path.write_text(updated_source, encoding="utf-8")
    updated = load_scenario(path)

    assert updated.checksum == hashlib.sha256(
        updated_source.encode("utf-8")
    ).hexdigest()
    assert updated.checksum != document.checksum


def test_scenario_rejects_unknown_next_step(tmp_path: Path) -> None:
    source = MINIMAL_SCENARIO.replace("next: finish", "next: missing")
    path = _write_scenario(tmp_path, source, "unknown-next.md")

    with pytest.raises(ScenarioError, match="references unknown step"):
        load_scenario(path)


def test_scenario_rejects_duplicate_workflow_id(tmp_path: Path) -> None:
    source = MINIMAL_SCENARIO.replace("- id: finish", "- id: start")
    path = _write_scenario(tmp_path, source, "duplicate-id.md")

    with pytest.raises(ScenarioError, match="Duplicate workflow step id"):
        load_scenario(path)


def test_scenario_rejects_unknown_action(tmp_path: Path) -> None:
    source = MINIMAL_SCENARIO.replace(
        "    do: []\n    next: finish",
        "    do:\n      - unsupported_action: {}\n    next: finish",
    )
    path = _write_scenario(tmp_path, source, "unknown-action.md")

    with pytest.raises(ScenarioError, match="unsupported action"):
        load_scenario(path)


def test_scenario_rejects_invalid_yaml(tmp_path: Path) -> None:
    path = _write_scenario(
        tmp_path,
        "---\nschema_version: [\n---\n# Broken\n",
        "invalid-yaml.md",
    )

    with pytest.raises(ScenarioError, match="invalid YAML front matter"):
        load_scenario(path)


def test_scenario_rejects_non_markdown_file(tmp_path: Path) -> None:
    path = _write_scenario(tmp_path, name="scenario.yaml")

    with pytest.raises(ScenarioError, match="Markdown document"):
        load_scenario(path)
