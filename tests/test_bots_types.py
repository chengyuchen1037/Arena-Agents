"""``app.bots.types``: the shared, dependency-free types of the v0.14 bot (contract §13.2)."""

from __future__ import annotations

import json

from app.bots.types import (
    CORE_LIMITS,
    CORE_SECTIONS,
    BotMind,
    ContractDoc,
    CoreFiles,
    PlaybookEntry,
    RulesMemory,
    core_hash,
    materials_hash,
    sha256_text,
    split_contract,
)


def test_core_files_report_the_missing_required_sections() -> None:
    assert CoreFiles().missing == CORE_SECTIONS
    assert not CoreFiles().complete
    core = CoreFiles(identity="一个老玩家", voice="  \n", mind="先看事实")
    assert core.missing == ("voice",)
    assert CoreFiles(identity="a", voice="b", mind="c").complete


def test_the_core_limits_are_the_agreed_ones() -> None:
    assert CORE_LIMITS == {"identity": 300, "voice": 600, "mind": 400}


def test_a_bot_mind_survives_a_json_round_trip() -> None:
    mind = BotMind(
        bot_id="b-1a2b3c4d",
        name="Alice",
        core=CoreFiles(identity="i", voice="v", mind="m", extras=(("口头禅", "「行」"),)),
        arch_notes="我的操作笔记",
        rules=RulesMemory(
            scenario_id="demo",
            digest="我的规则要点",
            playbook=(PlaybookEntry("先看票", "中", 3, 1, "复盘 2026-11-20", ("投票",)),),
            questions="还不确定…",
        ),
        people={"Bob": "话少"},
        record="场次 1 · 胜 0",
    )
    data = json.loads(json.dumps(mind.to_dict(), ensure_ascii=False))
    assert BotMind.from_dict(data) == mind


def test_a_snapshot_with_missing_or_odd_fields_still_loads() -> None:
    mind = BotMind.from_dict({"bot_id": "b-00000000", "name": "X", "rules": {"playbook": [{"text": "t", "confidence": "??"}]}})
    assert mind.core.missing == CORE_SECTIONS
    assert mind.rules.playbook[0].confidence == "低"  # an unknown confidence is the lowest one
    assert mind.people == {} and mind.arch_notes == ""


def test_split_contract_finds_the_two_sections() -> None:
    text = "# 契约\n前言\n\n## 必读\n第一段\n\n第二段\n\n## 参考\n详细说明\n"
    doc = split_contract(text, "v9")
    assert isinstance(doc, ContractDoc)
    assert doc.version == "v9"
    assert doc.must_read == "第一段\n\n第二段"
    assert doc.reference == "详细说明"
    assert doc.markdown == text
    assert doc.sha256 == sha256_text(text)


def test_core_hash_follows_the_text_not_the_file_layout() -> None:
    a = CoreFiles(identity="i\n", voice=" v", mind="m", extras=(("口头禅", "x "),))
    b = CoreFiles(identity="i", voice="v", mind="m\n", extras=(("口头禅", "x"),))
    assert core_hash(a) == core_hash(b)
    assert core_hash(a) != core_hash(CoreFiles(identity="i", voice="v", mind="m"))  # the extra section counts
    assert core_hash(CoreFiles(identity="i", voice="v", mind="m2")) != core_hash(CoreFiles(identity="i", voice="v", mind="m"))


def test_materials_hash_is_empty_without_materials_and_changes_with_any_edit() -> None:
    assert materials_hash({}) == ""
    one = materials_hash({"a": "text"})
    assert one and one == materials_hash({"a": "text"})
    assert one != materials_hash({"a": "text2"})
    assert one != materials_hash({"b": "text"})  # the slug counts


def test_split_contract_without_sections_is_empty_but_keeps_the_file() -> None:
    doc = split_contract("只有一段话")
    assert doc.must_read == "" and doc.reference == "" and doc.markdown == "只有一段话"
