"""Where the contract and the scenario of a game come from (v0.14, H7, H8, H38, H39): ``app.game.sources.Sources``.

The engine reads both from the data directory (the contract store and the rules store); ``config.yml`` and ``prompts/contract.md`` are the defaults
those stores copy.  Without the stores (before the data directory is locked) the defaults are read directly.  The hashes are what an initialization
depends on, and a verdict about a scenario text is remembered by its hash.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

import game_fakes
from app.agents.contract import load_default_contract
from app.game.sources import SourceError, Sources
from app.storage.contract import get_contract_store
from app.storage.rules import get_rules_store


def hub(monkeypatch: pytest.MonkeyPatch, *, io: bool = True, cfg: dict[str, Any] | None = None) -> Sources:
    config = copy.deepcopy(cfg or game_fakes.CFG)
    game_fakes.patch_config(monkeypatch, config)
    return Sources(lambda: config, lambda: io)


def test_the_stores_of_the_data_directory_are_the_source_and_the_config_only_names_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = hub(monkeypatch)
    contract = sources.contract()
    assert contract.sha256 == get_contract_store().current().sha256 and contract.must_read and contract.reference
    assert sources.scenario_ids() == ["minimal_test"] and sources.current_scenario_id() == "minimal_test" and sources.default_scenario_id() == "minimal_test"
    text = sources.scenario_text()
    assert text == get_rules_store().scenario_text("minimal_test") and sources.spec().id == "minimal_test"
    assert sources.scenario_problem() is None and sources.contract_problem(sources.spec()) is None and sources.materials() == {}


def test_a_contract_or_scenario_the_user_changed_is_what_is_in_force(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = hub(monkeypatch)
    before = sources.hashes()
    changed = get_contract_store()
    changed.set(sources.contract().markdown + "\n（补充的一句。）\n")
    after = sources.hashes()
    assert after.contract != before.contract and after.scenario == before.scenario and after.scenario_id == "minimal_test" and after.materials == ""
    store = get_rules_store()
    store.set_scenario("minimal_test", store.scenario_text("minimal_test").replace("title: 最小测试剧本", "title: 改过的剧本", 1))
    assert sources.spec().title == "改过的剧本" and sources.hashes().scenario != before.scenario
    store.set_material("minimal_test", "notes", "学习材料。", "笔记")
    assert sources.materials() == {"notes": "学习材料。\n"} or sources.materials() == {"notes": "学习材料。"}
    assert sources.hashes().materials != ""


def test_without_the_stores_the_defaults_are_read_directly(monkeypatch: pytest.MonkeyPatch, isolated_external_storage: Path) -> None:
    sources = hub(monkeypatch, io=False)  # (the data directory is not locked yet: nothing of it is read or created)
    assert sources.contract().sha256 == load_default_contract().sha256
    assert sources.scenario_ids() == ["minimal_test"] and sources.spec().id == "minimal_test"
    assert not (isolated_external_storage / "contract").exists() and not (isolated_external_storage / "rules").exists()


def test_what_cannot_be_used_is_a_source_error_with_the_code_the_start_check_reports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    broken = copy.deepcopy(game_fakes.CFG)
    broken["scenario"] = {"path": str(tmp_path / "nowhere.md")}
    sources = hub(monkeypatch, io=False, cfg=broken)
    with pytest.raises(SourceError) as missing:
        sources.default_scenario_id()
    assert (missing.value.kind, missing.value.code) == ("scenario", "scenario_missing")
    problem = sources.scenario_problem()
    assert problem is not None and problem.code == "scenario_missing"
    (tmp_path / "bad.md").write_text("没有 front matter 的文字", encoding="utf-8")
    broken["scenario"] = {"path": str(tmp_path / "bad.md")}
    sources = hub(monkeypatch, io=False, cfg=broken)
    with pytest.raises(SourceError) as invalid:
        sources.spec()
    assert (invalid.value.kind, invalid.value.code) == ("scenario", "scenario_invalid") and "front matter" in str(invalid.value)
    assert sources.scenario_problem().code == "scenario_invalid"  # (remembered by the hash of the text: the second answer is the first)
    # the hashes never raise: what cannot be read has the empty hash (nothing that was initialized matches it)
    hashes = sources.hashes()
    assert (hashes.scenario, hashes.materials, hashes.scenario_id) == ("", "", "") and hashes.contract == load_default_contract().sha256


def test_a_contract_that_does_not_mention_the_tools_of_the_scenario_is_a_problem_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = hub(monkeypatch)
    get_contract_store()  # (the store validates what it is given: write the file as a user who edited it by hand would)
    path = get_contract_store().path if hasattr(get_contract_store(), "path") else None
    if path is None:
        pytest.skip("the contract store has no path to edit by hand")
    path.write_text("# 契约\n\n## 必读\n\n只有一句话。\n\n## 参考\n\n没有工具名。\n", encoding="utf-8")
    problem = sources.contract_problem(sources.spec())
    assert problem is not None and problem.code == "contract_invalid" and "speak" in str(problem)


def test_the_snapshot_of_a_contract_rebuilds_the_same_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = hub(monkeypatch)
    contract = sources.contract()
    snapshot = Sources.snapshot_of(contract)
    assert set(snapshot) == {"version", "sha256", "markdown"}
    again = sources.contract_from_snapshot(snapshot)
    assert again.markdown == contract.markdown and again.must_read == contract.must_read and again.sha256 == contract.sha256
