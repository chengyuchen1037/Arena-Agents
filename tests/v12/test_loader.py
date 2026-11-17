from __future__ import annotations

from pathlib import Path

import pytest

from app.world.core import WorldCore
from app.world.loader import load_scenario_v2, parse_scenario_v2, split_body
from app.world.render import render_text, validate_params
from app.world.spec import ScenarioError
from app.world.state import PlayerState

ROOT = Path(__file__).resolve().parents[2]
GOOD_HEAD = "---\nschema_version: 2\nid: t\nversion: '1'\n{extra}---\n"


class _Clock:
    def now(self) -> float:
        return 0.0


def test_split_body_extracts_the_manual_section_only() -> None:
    body = "# 规则\n正文\n\n# 主持人手册\n手册一\n## 子节\n手册二\n\n# 附录\n附录文字\n"
    rules, manual = split_body(body)
    assert "手册" not in rules and "规则" in rules and "附录文字" in rules
    assert manual.startswith("# 主持人手册") and "手册二" in manual and "附录" not in manual


def test_split_body_ignores_headings_in_code_fences_and_handles_absence() -> None:
    body = "# 规则\n```\n# 主持人手册\n假的\n```\n结尾\n"
    rules, manual = split_body(body)
    assert manual == "" and "假的" in rules
    assert split_body("只有正文") == ("只有正文", "")


def test_custom_manual_heading() -> None:
    rules, manual = split_body("# A\n甲\n## 裁判指南\n乙\n", "裁判指南")
    assert rules == "# A\n甲" and manual == "## 裁判指南\n乙"


def test_minimal_scenario_loads_and_splits() -> None:
    spec = load_scenario_v2(ROOT / "scenarios" / "minimal_test.md")
    assert spec.id == "minimal_test" and spec.schema_version == 2
    assert "开局" in spec.manual_text and "开局" not in spec.rules_text
    assert set(spec.templates) == {"out_notice", "say_vote"} and "shout" in spec.abilities


def test_minimal_scenario_expressions_work_on_a_world() -> None:
    spec = load_scenario_v2(ROOT / "scenarios" / "minimal_test.md")
    world = WorldCore(spec, _Clock())
    world.state.players = [
        PlayerState(id="a1", seat="S1", name="甲", role="red", faction="red", tags=["红队", "存活"]),
        PlayerState(id="a2", seat="S2", name="乙", role="red", faction="red", tags=["红队", "出局"]),
        PlayerState(id="b1", seat="S3", name="丙", role="blue", faction="blue", tags=["蓝队", "存活"]),
        PlayerState(id="b2", seat="S4", name="丁", role="blue", faction="blue", tags=["蓝队", "存活"]),
    ]
    assert world.to_plain(world.eval_expr(spec.abilities["shout"].holders)) == ["a1"]
    assert world.eval_expr(spec.checks["blue_wins"]) is False and world.eval_expr(spec.checks["red_wins"]) is False
    world.state.players[0].tags = ["红队", "出局"]
    assert world.eval_expr(spec.checks["blue_wins"]) is True


def test_templates_render_with_the_shared_helpers() -> None:
    spec = load_scenario_v2(ROOT / "scenarios" / "minimal_test.md")
    world = WorldCore(spec, _Clock())
    world.state.players = [PlayerState(id="a1", seat="S1", name="甲"), PlayerState(id="a2", seat="S2", name="乙")]
    template = spec.templates["out_notice"]
    params, rejected = validate_params(template.params, {"players": ["a1", "a2"]}, world)
    assert rejected is None
    assert render_text(template.text, params or {}, template.params, world) == "【出局公告】甲（a1）、乙（a2） 出局。"


@pytest.mark.parametrize(
    "source, fragment",
    [
        ("没有 front matter", "front matter"),
        ("---\nid: x\n", "没有以 ---"),
        ("---\n: : :\n---\n", "YAML"),
        ("---\nschema_version: 1\nid: x\nversion: '1'\n---\n", "schema_version"),
        (GOOD_HEAD.format(extra="") .replace("id: t\n", ""), "校验失败"),
        (GOOD_HEAD.format(extra="setup:\n  identity_pool: [{role: ghost, count: 1}]\n"), "undeclared role"),
        (GOOD_HEAD.format(extra="checks: {c: '${1 +}'}\n"), "checks.c"),
        (GOOD_HEAD.format(extra="templates: {x: {text: '{{missing}}', users: moderator}}\n"), "占位符"),
        (GOOD_HEAD.format(extra="abilities: {a: {holders: '${[}'}}\n"), "abilities.a.holders"),
        (GOOD_HEAD.format(extra="abilities: {a: {holders: '${[]}', reveal: {channel_id: c, text: '{{x}}'}}}\n"), "占位符"),
    ],
)
def test_bad_scenarios_raise_scenario_error_with_context(source: str, fragment: str) -> None:
    with pytest.raises(ScenarioError) as info:
        parse_scenario_v2(source, where="bad.md")
    assert fragment in str(info.value) and "bad.md" in str(info.value)


def test_missing_and_non_markdown_files(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError, match="找不到"):
        load_scenario_v2(tmp_path / "nope.md")
    other = tmp_path / "x.txt"
    other.write_text("---\n---\n", encoding="utf-8")
    with pytest.raises(ScenarioError, match="Markdown"):
        load_scenario_v2(other)
