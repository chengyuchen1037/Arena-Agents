from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
import yaml

from app.agents.brain import ArenaBrain
from app.agents.schemas import (
    MODERATOR_TOOL_INTENTS,
    PLAYER_TOOL_INTENTS,
    SHARED_TOOL_NAMES,
    SLEEP_TOOL,
    shared_tool_schemas,
)
from app.world.core import WorldCore
from app.world.intents import FREE_LIMIT_KEYS, MODERATOR_INTENTS, PLAYER_INTENTS, TOKEN_LIMIT_KEYS
from app.world.loader import load_scenario_v2, parse_scenario_v2, split_body
from app.world.render import player_label, render_text, validate_params
from app.world.spec import ParamSpec, ScenarioError, ScenarioSpec
from app.world.state import PlayerState
from app.world.types import MODERATOR_ID, Receipt
from app.world.world import World

from .agents.fakes import FakeWorld

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
    assert render_text(template.text, params or {}, template.params, world) == "【出局公告】a1 甲、a2 乙 出局。"


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


# ======================================================================================================
# werewolf_12p (schema v2): structure, manual references, checks, templates, prompts
# ======================================================================================================


WEREWOLF = ROOT / "scenarios" / "werewolf_12p.md"
ALL_TOOLS = {*MODERATOR_TOOL_INTENTS, *PLAYER_TOOL_INTENTS, SLEEP_TOOL, *SHARED_TOOL_NAMES}
LEGACY_TOOLS = (
    "grant_speaking_turn", "ask_yes_or_no", "answer_yes_or_no", "request_players_vote", "request_player_speak",
    "request_player_interrupt", "trigger_interrupt", "cast_vote", "request_players_strategic_planning",
    "submit_strategic_plan", "set_interrupt_quota", "clear_interrupt_quota", "record_engine_note",
    "update_player_tags", "batch_update_player_tags",
)


@pytest.fixture(scope="module")
def ww() -> ScenarioSpec:
    return load_scenario_v2(WEREWOLF)


# ----------------------------------------------------------------------------- a 12-player world


def _pool(spec: ScenarioSpec) -> list[str]:
    roles: list[str] = []
    for item in spec.setup.identity_pool:
        roles += [item.role] * item.count
    return roles


NAMES = ["Alice", "Bob", "Carol", "David", "Eve", "Frank", "Grace", "Henry", "Ivy", "Jack", "Kate", "Leo"]


def _players(spec: ScenarioSpec, out: frozenset[str] = frozenset()) -> list[PlayerState]:
    """N1.. in identity-pool order: 3 werewolves, the white wolf, 4 villagers, seer, witch, hunter, guard."""
    players = []
    for index, role_id in enumerate(_pool(spec), start=1):
        role = spec.setup.roles[role_id]
        pid = f"N{index}"
        tags = [t for t in role.tags if not (pid in out and t == "存活")] + (["出局"] if pid in out else [])
        players.append(
            PlayerState(
                id=pid, seat=str(index), name=NAMES[index - 1], role=role_id, faction=role.faction,
                tags=tags, attributes=dict(role.attributes),
            )
        )
    return players


def _world(spec: ScenarioSpec, out: tuple[str, ...] = ()) -> WorldCore:
    world = WorldCore(spec, _Clock())
    world.state.players = _players(spec, frozenset(out))
    return world


def _check(world: WorldCore, spec: ScenarioSpec, name: str, **args: object) -> object:
    return world.to_plain(world.eval_expr(spec.checks[name], args))


ROLE_OF = {f"N{i}": role for i, role in enumerate(
    ["werewolf"] * 3 + ["white_wolf"] + ["villager"] * 4 + ["seer", "witch", "hunter", "guard"], start=1)}
WOLVES = ["N1", "N2", "N3", "N4"]
VILLAGERS = ["N5", "N6", "N7", "N8"]
GODS = ["N9", "N10", "N11", "N12"]


# ----------------------------------------------------------------------------- structure


def test_werewolf_scenario_loads_with_the_expected_setup(ww: ScenarioSpec) -> None:
    assert (ww.id, ww.schema_version, ww.version) == ("werewolf_12p", 2, "2.1.0")
    assert sum(item.count for item in ww.setup.identity_pool) == 12
    assert {i.role: i.count for i in ww.setup.identity_pool} == {
        "werewolf": 3, "white_wolf": 1, "villager": 4, "seer": 1, "witch": 1, "hunter": 1, "guard": 1,
    }
    assert set(ww.setup.roles) == {i.role for i in ww.setup.identity_pool}
    factions = [ww.setup.roles[r].faction for r in _pool(ww)]
    assert factions.count("wolves") == 4 and factions.count("good") == 8
    groups = [ww.setup.roles[r].attributes["group"] for r in _pool(ww)]
    assert (groups.count("wolves"), groups.count("villagers"), groups.count("gods")) == (4, 4, 4)
    assert ww.setup.shuffle and ww.constraints == []
    assert ww.invalidates_drafts == ["player.updated", "phase.changed"]
    witch, guard = ww.setup.roles["witch"], ww.setup.roles["guard"]
    assert witch.attributes["antidote"] == 1 and witch.attributes["poison"] == 1 and guard.attributes["last_guard"] == ""


def test_werewolf_defaults_and_ui(ww: ScenarioSpec) -> None:
    d = ww.defaults
    assert (d.floor.duration_s, d.floor.max_messages, d.floor.max_chars_per_message, d.floor.max_chars_total) == (120, 1, 300, 600)
    assert (d.free.duration_s, d.free.max_messages_per_member, d.free.max_chars_per_message) == (300, 6, 300)
    assert (d.free.max_chars_total, d.free.silence_s) == (2000, 30)
    assert not hasattr(d.free, "debounce_s")  # v0.12.1: every delivery wakes its recipient at once
    assert d.request.duration_s == 90 and d.llm.max_tokens == 8192
    assert d.dm.max_chars_per_message == 600 and d.moderator.max_chars_per_message == 2000
    assert ww.ui.out_tags == ["出局"] and ww.ui.highlight_tags == ["警长"]
    assert ww.ui.status_tags == ["存活", "出局", "警长"] == ww.setup.public_tags


def test_every_role_has_a_private_brief_with_its_win_condition(ww: ScenarioSpec) -> None:
    for role_id, role in ww.setup.roles.items():
        assert "胜利条件" in role.brief and len(role.brief) > 40, role_id
        assert "【" in role.brief  # names the identity
    assert "white_wolf_explode" in ww.setup.roles["white_wolf"].brief


def test_body_is_split_into_rules_and_manual(ww: ScenarioSpec) -> None:
    assert ww.manual_text.startswith("# 主持人手册") and ww.rules_text.startswith("# 12 人标准局狼人杀")
    assert "主持人手册" not in ww.rules_text
    for private in ("开局 B", "N5 夜间结算", "evaluate(", "set_floor_queue(", "《进度》", "hold_game"):
        assert private in ww.manual_text and private not in ww.rules_text
    for rule in ("同守同救", "屠边", "1.5 票", "警左", "遗言", "暗牌局", "平票"):
        assert rule in ww.rules_text, rule
    assert "{{" not in ww.rules_text and "{{" not in ww.manual_text


def test_tags_agree_with_the_ui_and_the_tag_styles_of_the_config(ww: ScenarioSpec) -> None:
    styles = set((yaml.safe_load((ROOT / "config.yml").read_text(encoding="utf-8")).get("tag_styles") or {}))
    identity_tags = set()
    for role_id, role in ww.setup.roles.items():
        assert role.tags[0] != "存活" and role.tags[-1] == "存活" and len(role.tags) == 2, role_id
        identity_tags.add(role.tags[0])
    assert len(identity_tags) == 7
    assert identity_tags.isdisjoint(ww.setup.public_tags)  # identities stay private
    shown = identity_tags | set(ww.setup.public_tags) | set(ww.ui.out_tags) | set(ww.ui.status_tags) | set(ww.ui.highlight_tags)
    assert shown <= styles, f"missing tag_styles: {sorted(shown - styles)}"
    assert set(ww.ui.out_tags) <= set(ww.setup.public_tags)  # everybody must see who is out


# ----------------------------------------------------------------------------- the checks


def test_checks_at_the_start_of_the_game(ww: ScenarioSpec) -> None:
    world = _world(ww)
    assert _check(world, ww, "good_wins") is False and _check(world, ww, "wolves_win") is False
    assert _check(world, ww, "outcome") == "未分胜负"
    assert _check(world, ww, "candidates") == [f"N{i}" for i in range(1, 13)]
    assert _check(world, ww, "alive_wolves") == WOLVES
    counts = _check(world, ww, "counts")
    assert counts["factions"] == [
        {"name": "狼人阵营", "alive": 4, "total": 4, "variant": "danger"},
        {"name": "神职", "alive": 4, "total": 4, "variant": "purple"},
        {"name": "平民阵营", "alive": 4, "total": 4, "variant": "neutral"},
    ]
    assert counts["tag_stats"] == [
        {"name": "存活", "count": 12, "variant": "success"},
        {"name": "出局", "count": 0, "variant": "muted"},
        {"name": "警长", "count": 0, "variant": "warning"},
    ]


def test_counts_can_be_passed_straight_to_update_dashboard(ww: ScenarioSpec) -> None:
    from app.world.intents import ModUpdateDashboard

    world = _world(ww, out=("N2", "N5", "N11"))
    world.state.players[6].tags.append("警长")
    counts = _check(world, ww, "counts")
    ModUpdateDashboard.model_validate(counts)  # exactly the payload of update_dashboard
    assert [f["alive"] for f in counts["factions"]] == [3, 3, 3]
    assert {t["name"]: t["count"] for t in counts["tag_stats"]} == {"存活": 9, "出局": 3, "警长": 1}


@pytest.mark.parametrize(
    "out, good, wolves, outcome",
    [
        ((), False, False, "未分胜负"),
        (("N1", "N2", "N3"), False, False, "未分胜负"),  # the white wolf is still alive
        (tuple(WOLVES), True, False, "好人阵营"),
        (tuple(GODS), False, True, "狼人阵营"),  # all gods out: wolves win
        (tuple(VILLAGERS), False, True, "狼人阵营"),  # all villagers out: wolves win
        (("N9", "N10", "N11"), False, False, "未分胜负"),  # one god left
        ((*WOLVES, *GODS), True, True, "好人阵营"),  # both conditions at once: the good side wins
    ],
)
def test_victory_checks(ww: ScenarioSpec, out: tuple[str, ...], good: bool, wolves: bool, outcome: str) -> None:
    world = _world(ww, out=out)
    assert _check(world, ww, "good_wins") is good
    assert _check(world, ww, "wolves_win") is wolves
    assert _check(world, ww, "outcome") == outcome


def test_alive_wolves_follow_the_seat_order_and_the_dead(ww: ScenarioSpec) -> None:
    assert "first_wolf" not in ww.checks  # the wolves no longer send a representative (v0.12.1)
    world = _world(ww, out=("N1",))
    assert _check(world, ww, "alive_wolves") == ["N2", "N3", "N4"]
    world = _world(ww, out=tuple(WOLVES))
    assert _check(world, ww, "alive_wolves") == []


def test_candidates_without_removes_the_pending_dead(ww: ScenarioSpec) -> None:
    world = _world(ww, out=("N5",))
    assert _check(world, ww, "candidates") == [i for i in _check(world, ww, "candidates")] and "N5" not in _check(world, ww, "candidates")
    assert _check(world, ww, "candidates_without", excluded=["N3", "N11"]) == [
        "N1", "N2", "N4", "N6", "N7", "N8", "N9", "N10", "N12",
    ]
    assert len(_check(world, ww, "candidates_without", excluded=[])) == 11


SEATS = [f"N{i}" for i in range(1, 13)]


@pytest.mark.parametrize(
    "start, direction, out, expected",
    [
        ("", "left", (), SEATS),
        ("N7", "left", (), ["N8", "N9", "N10", "N11", "N12", "N1", "N2", "N3", "N4", "N5", "N6", "N7"]),
        ("N7", "right", (), ["N6", "N5", "N4", "N3", "N2", "N1", "N12", "N11", "N10", "N9", "N8", "N7"]),
        ("N1", "left", (), SEATS[1:] + ["N1"]),
        ("N1", "right", (), ["N12", "N11", "N10", "N9", "N8", "N7", "N6", "N5", "N4", "N3", "N2", "N1"]),
        ("N7", "left", ("N8", "N1"), ["N9", "N10", "N11", "N12", "N2", "N3", "N4", "N5", "N6", "N7"]),
        ("N7", "right", ("N6", "N12"), ["N5", "N4", "N3", "N2", "N1", "N11", "N10", "N9", "N8", "N7"]),
        ("", "left", ("N1", "N2"), SEATS[2:]),
        ("N2", "left", ("N2",), SEATS[0:1] + SEATS[2:]),  # a dead "sheriff" id: plain seat order, nobody last
    ],
)
def test_speech_order(ww: ScenarioSpec, start: str, direction: str, out: tuple[str, ...], expected: list[str]) -> None:
    world = _world(ww, out=out)
    assert _check(world, ww, "speech_order", start=start, direction=direction) == expected


def test_order_of_sorts_the_given_members_by_seat(ww: ScenarioSpec) -> None:
    world = _world(ww)
    assert _check(world, ww, "order_of", members=["N9", "N2", "N5"]) == ["N2", "N5", "N9"]
    assert _check(world, ww, "order_of", members=["N10", "ghost"]) == ["N10"]
    assert _check(world, ww, "order_of", members=[]) == []


def _answers(**votes: str) -> list[dict[str, str]]:
    return [{"player_id": voter, "value": target} for voter, target in votes.items()]


def test_weighted_vote_gives_the_sheriff_one_and_a_half_votes(ww: ScenarioSpec) -> None:
    world = _world(ww)
    answers = _answers(N1="N5", N2="N5", N3="abstain", N7="N6", N8="N6", N9="N5")
    result = _check(world, ww, "weighted_vote", answers=answers, privileged=["N7"], weight=1.5)
    assert result == {"tally": {"N5": 3.0, "N6": 2.5}, "leaders": ["N5"]}
    # the sheriff's 1.5 votes decide a close vote
    close = _answers(N1="N5", N2="N5", N7="N6", N8="N6", N9="N6")
    assert _check(world, ww, "weighted_vote", answers=close, privileged=["N7"], weight=1.5)["leaders"] == ["N6"]
    assert _check(world, ww, "weighted_vote", answers=close, privileged=[], weight=1)["leaders"] == ["N6"]


def test_weighted_vote_ties_abstentions_and_empty_votes(ww: ScenarioSpec) -> None:
    world = _world(ww)
    tie = _answers(N1="N5", N2="N5", N3="N6", N4="N6")
    assert _check(world, ww, "weighted_vote", answers=tie, privileged=[], weight=1) == {
        "tally": {"N5": 2.0, "N6": 2.0}, "leaders": ["N5", "N6"],
    }
    nobody = _answers(N1="abstain", N2="abstain")
    assert _check(world, ww, "weighted_vote", answers=nobody, privileged=[], weight=1) == {"tally": {}, "leaders": []}
    assert _check(world, ww, "weighted_vote", answers=[], privileged=[], weight=1) == {"tally": {}, "leaders": []}
    # the sheriff abstaining adds nothing; a sheriff vote for a rival breaks the tie
    mixed = _answers(N1="N5", N2="N6", N7="N6")
    assert _check(world, ww, "weighted_vote", answers=mixed, privileged=["N7"], weight=1.5)["leaders"] == ["N6"]


def test_wolf_choice_is_the_majority_and_ties_go_to_the_first_wolf_by_seat(ww: ScenarioSpec) -> None:
    world = _world(ww)

    def choose(**votes: str) -> object:
        return _check(world, ww, "wolf_choice", answers=_answers(**votes))

    assert choose(N1="N5", N2="N5", N3="N5", N4="N5") == "N5"  # unanimous
    assert choose(N1="N5", N2="N6", N3="N6", N4="N6") == "N6"  # a majority
    assert choose(N1="N5", N2="N6", N3="N6", N4="N5") == "N5"  # 2-2: the first wolf by seat decides ...
    assert choose(N1="N6", N2="N5", N3="N5", N4="N6") == "N6"  # ... whichever of the two options he chose
    assert choose(N4="N5", N3="N6", N2="N6", N1="N5") == "N5"  # the order of the answers does not matter
    assert choose(N2="N7", N3="N8", N4="N9") == "N7"  # 1-1-1: the first by seat
    assert choose(N10="N5", N2="N6") == "N6"  # seats are compared as numbers: N2 comes before N10
    # an empty kill ("none") is an option like any other
    assert choose(N1="none", N2="none", N3="N5") == "none"
    assert choose(N1="none", N2="N5") == "none"
    assert choose(N1="N5", N2="none", N3="none", N4="N5") == "N5"
    # not everybody answered: the answers there are decide; nobody answered: an empty kill
    assert choose(N3="N10") == "N10"
    assert choose(N1="N5", N3="N6", N4="N6") == "N6"
    assert choose() == "none"
    # the engine's own answers carry more keys (reason, defaulted): they are ignored
    full = [{"player_id": "N2", "value": "N8", "reason": "女巫", "defaulted": False}, {"player_id": "N1", "value": "N8", "reason": "", "defaulted": False}]
    assert _check(world, ww, "wolf_choice", answers=full) == "N8"


def test_checks_reject_missing_variables_clearly(ww: ScenarioSpec) -> None:
    from app.engine.expressions import ExpressionError

    world = _world(ww)
    with pytest.raises(ExpressionError, match="Unknown name: members"):
        world.eval_expr(ww.checks["order_of"], {})


def test_the_ability_holders_follow_the_white_wolf(ww: ScenarioSpec) -> None:
    holders = ww.abilities["white_wolf_explode"].holders
    assert _world(ww).to_plain(_world(ww).eval_expr(holders)) == ["N4"]
    dead = _world(ww, out=("N4",))
    assert dead.to_plain(dead.eval_expr(holders)) == []
    ability = ww.abilities["white_wolf_explode"]
    assert ability.quota == 1 and not ability.enabled and ability.freeze_channels == ["public"]
    assert ability.reveal is not None and ability.reveal.channel_id == "public"
    assert set(ability.params) == {"target", "declaration"}


# ----------------------------------------------------------------------------- templates


def _sample(spec: ParamSpec) -> object:
    return {
        "player_id": "N1",
        "player_ids": ["N1", "N2"],
        "enum": spec.values[0] if spec.values else "",
        "int": spec.min if spec.min is not None else 1,
        "text": "样例文字",
    }[spec.type]


def test_every_template_renders_a_sample(ww: ScenarioSpec) -> None:
    world = _world(ww)
    for name, template in ww.templates.items():
        params = {key: _sample(spec) for key, spec in template.params.items() if not spec.optional}
        clean, rejected = validate_params(template.params, params, world)
        assert rejected is None, name
        text = render_text(template.text, clean or {}, template.params, world)
        assert text and "{{" not in text, name
    template = ww.templates["death_notice"]
    clean, _ = validate_params(template.params, {"players": ["N1", "N2"]}, world)
    text = render_text(template.text, clean or {}, template.params, world)
    assert text == f"【出局公告】{player_label(world, 'N1')}、{player_label(world, 'N2')} 出局。"
    assert all(word in text for word in ("N1", "Alice", "N2", "Bob"))  # the id (what tools take) and the name
    seer = ww.templates["seer_result"]
    clean, _ = validate_params(seer.params, {"target": "N3", "side": "狼人阵营"}, world)
    assert render_text(seer.text, clean or {}, seer.params, world) == f"【查验结果】{player_label(world, 'N3')} 属于狼人阵营。"
    wolf = ww.templates["wolf_target"]
    clean, _ = validate_params(wolf.params, {"target": "N5"}, world)
    assert render_text(wolf.text, clean or {}, wolf.params, world) == f"【刀口】今晚刀口：{player_label(world, 'N5')}。"
    assert render_text(ww.templates["wolf_empty"].text, {}, {}, world) == "【刀口】今晚空刀。"
    # the side must be one of the two factions
    assert validate_params(seer.params, {"target": "N3", "side": "平民"}, world)[1] is not None


def test_sensitive_templates_are_moderator_only_and_neutral(ww: ScenarioSpec) -> None:
    moderator = {tid for tid, t in ww.templates.items() if t.users == "moderator"}
    players = {tid for tid, t in ww.templates.items() if t.users in ("players", "any")}
    assert moderator == {
        "death_notice", "peaceful_night", "vote_result", "no_exile", "tie_pk", "seer_result", "wolf_target", "wolf_empty",
        "sheriff_candidates", "sheriff", "no_sheriff", "speech_direction", "hunter_shot", "game_over",
    }
    assert players == {"vote_declare", "check_report"}
    assert all(ww.templates[t].sensitive for t in moderator) and not any(ww.templates[t].sensitive for t in players)
    # a dark-card game: announcements about deaths and votes never name an identity or a cause of death
    identity_words = ("狼人", "白狼", "平民", "神职", "预言家", "女巫", "守卫", "猎人", "被刀", "被毒", "死因")
    for tid in ("death_notice", "peaceful_night", "vote_result", "no_exile", "tie_pk", "sheriff", "no_sheriff", "speech_direction"):
        assert not [w for w in identity_words if w in ww.templates[tid].text], tid


# ----------------------------------------------------------------------------- the manual vs. the real tools


_CALL = re.compile(r"`([a-z_]+)\((.*?)\)`", re.S)


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth, quote = 0, ""
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _kwargs(arg_text: str) -> tuple[dict[str, str], list[str]]:
    """``(name -> raw value text, bare pieces)`` of a call's top-level arguments."""
    named: dict[str, str] = {}
    bare: list[str] = []
    for piece in _split_top_level(arg_text):
        match = re.match(r"^([a-z_]+)\s*=\s*(.*)$", piece, re.S)
        if match:
            named[match.group(1)] = match.group(2).strip()
        else:
            bare.append(piece)
    return named, bare


def _dict_keys(raw: str) -> list[str]:
    raw = raw.strip()
    if not (raw.startswith("{") and raw.endswith("}")):
        return []
    keys = []
    for piece in _split_top_level(raw[1:-1]):
        match = re.match(r'^"([A-Za-z_]+)"\s*:', piece)
        if match:
            keys.append(match.group(1))
    return keys


def _tool_fields(name: str) -> tuple[set[str], set[str]]:
    """(allowed argument names, required argument names) of a tool."""
    if name in MODERATOR_TOOL_INTENTS:
        model = MODERATOR_INTENTS[MODERATOR_TOOL_INTENTS[name]]
    elif name in PLAYER_TOOL_INTENTS:
        model = PLAYER_INTENTS[PLAYER_TOOL_INTENTS[name]]
    elif name == SLEEP_TOOL:
        return {"seconds"}, set()
    else:
        params = next(t["function"]["parameters"] for t in shared_tool_schemas() if t["function"]["name"] == name)
        return set(params["properties"]), set(params.get("required", []))
    return set(model.model_fields), {f for f, info in model.model_fields.items() if info.is_required()}


def _calls(text: str) -> list[tuple[str, dict[str, str], list[str]]]:
    out = []
    for match in _CALL.finditer(text):
        named, bare = _kwargs(match.group(2))
        out.append((match.group(1), named, bare))
    return out


def test_every_tool_call_in_the_manual_is_a_real_tool_with_real_arguments(ww: ScenarioSpec) -> None:
    calls = _calls(ww.manual_text)
    assert len(calls) > 60  # the manual is full of concrete calls
    problems: list[str] = []
    for name, named, bare in calls:
        if name not in ALL_TOOLS:
            problems.append(f"unknown tool {name}")
            continue
        allowed, required = _tool_fields(name)
        problems += [f"{name}: unknown argument {k}" for k in sorted(set(named) - allowed)]
        if not bare:  # an argument list cut short with an ellipsis is exempt from the "required" check
            problems += [f"{name}: missing required {k}" for k in sorted(required - set(named))]
    assert not problems, "\n".join(sorted(set(problems)))


def test_tool_calls_in_the_prompts_name_real_tools_and_real_arguments() -> None:
    for prompt in ("participant.md", "moderator.md"):
        text = (ROOT / "prompts" / prompt).read_text(encoding="utf-8")
        for name, named, bare in _calls(text):
            assert name in ALL_TOOLS, f"{prompt}: unknown tool {name}"
            allowed, _ = _tool_fields(name)
            assert set(named) <= allowed, f"{prompt}: {name} {sorted(set(named) - allowed)}"
            assert {b for b in bare if re.fullmatch(r"[a-z_]+", b)} <= allowed, f"{prompt}: {name} {bare}"


def test_manual_ids_exist_in_the_spec(ww: ScenarioSpec) -> None:
    text = ww.manual_text
    for tid in re.findall(r'template_id="([^"]+)"', text):
        assert tid in ww.templates and ww.templates[tid].users in ("moderator", "any"), tid
    for cid in re.findall(r'check_id="([^"]+)"', text):
        assert cid in ww.checks, cid
    for aid in re.findall(r'ability_id="([^"]+)"', text):
        assert aid in ww.abilities, aid
    for channel in re.findall(r'channel_id="([^"]+)"', text):
        assert channel in {"public", "wolf-team"} or channel.startswith("dm-"), channel
    tags = set()
    for raw in re.findall(r"(?:add_tags|remove_tags)=\[(.*?)\]", text):
        tags |= set(re.findall(r'"([^"]+)"', raw))
    assert tags == {"出局", "存活", "警长"}
    attribute_names = {a for role in ww.setup.roles.values() for a in role.attributes}
    for raw in re.findall(r"set_attributes=(\{.*?\})", text):
        assert set(_dict_keys(raw)) <= attribute_names, raw


# What the manual may name besides tools, templates, checks, abilities and tool arguments.
MANUAL_EVENTS = frozenset(
    {
        "game.started", "game.resumed", "game.continued", "request.closed", "request.answer", "floor.completed",
        "free.advisory", "timer.elapsed", "ability.used", "challenge.opened", "message.new", "watchdog",
    }
)
MANUAL_WORDS = frozenset(
    {
        "silence", "time", "chars",  # reasons of ``free.advisory``
        "code", "message",  # fields of a receipt
        "last_guard",  # an attribute of the guard (see the scenario roles)
        "save",  # an option id of the witch's request
        "none",  # the "empty" option of the guard's / the wolves' request (and the result of ``wolf_choice``)
        "unanimous", "deadline", "all_answered",  # values of ``auto_close`` / reasons of ``request.closed``
        "public", "wolf-team",  # channel ids
    }
)


def test_every_backticked_word_of_the_manual_is_known(ww: ScenarioSpec) -> None:
    arguments = {f for model in [*MODERATOR_INTENTS.values(), *PLAYER_INTENTS.values()] for f in model.model_fields}
    arguments |= {*TOKEN_LIMIT_KEYS, *FREE_LIMIT_KEYS}  # the keys of ``limits``
    known = {*ALL_TOOLS, *ww.templates, *ww.checks, *ww.abilities, *arguments, *MANUAL_EVENTS, *MANUAL_WORDS}
    attributes = {a for role in ww.setup.roles.values() for a in role.attributes}
    unknown = sorted(
        word
        for word in set(re.findall(r"`([A-Za-z][A-Za-z0-9_.\-]*)`", ww.manual_text))
        if word not in known and word not in attributes
    )
    assert not unknown, f"the manual names things that do not exist: {unknown}"
    assert {"last_guard", "antidote", "poison"} <= attributes | MANUAL_WORDS


def test_every_event_the_manual_wakes_up_on_is_rendered_by_the_agents(ww: ScenarioSpec) -> None:
    from app.agents.context import Namer, render_wake
    from app.world.types import Envelope

    named = set(re.findall(r"`([a-z]+\.[a-z]+|watchdog)`", ww.manual_text))
    assert MANUAL_EVENTS <= named and named <= MANUAL_EVENTS
    namer = Namer(lambda agent_id: agent_id)
    for event in sorted(MANUAL_EVENTS):
        text = render_wake([Envelope(kind="notice", priority="urgent", payload={"event": event})], namer, agent_id=MODERATOR_ID)
        assert not text.splitlines()[1].startswith(f"- {event}："), f"{event} falls back to the generic rendering"


def test_manual_mentions_every_template_check_and_ability(ww: ScenarioSpec) -> None:
    moderator_templates = [t for t, spec in ww.templates.items() if spec.users != "players"]
    for kind, ids in (("template", moderator_templates), ("check", list(ww.checks)), ("ability", list(ww.abilities))):
        missing = [i for i in ids if f"`{i}`" not in ww.manual_text and f'"{i}"' not in ww.manual_text]
        assert not missing, f"the manual never mentions these {kind}s: {missing}"
    for tid in (t for t, spec in ww.templates.items() if spec.users == "players"):
        assert tid in ww.rules_text  # players are told about their templates in the rules


def _free_names(expression: str) -> set[str]:
    text = expression.strip()
    text = text[2:-1] if text.startswith("${") and text.endswith("}") else text
    tree = ast.parse(text.strip(), mode="eval")
    loaded = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    local = {n.id for c in ast.walk(tree) if isinstance(c, ast.comprehension) for n in ast.walk(c.target) if isinstance(n, ast.Name)}
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    return loaded - local - called - {"players", "true", "false", "null"}


def test_evaluate_examples_pass_exactly_the_variables_the_checks_use(ww: ScenarioSpec) -> None:
    expected = {name: _free_names(expr) for name, expr in ww.checks.items()}
    assert expected["speech_order"] == {"start", "direction"} and expected["weighted_vote"] == {"answers", "privileged", "weight"}
    assert expected["wolf_choice"] == {"answers"} and expected["alive_wolves"] == set()
    seen = set()
    for name, named, _bare in _calls(ww.manual_text):
        if name != "evaluate" or not re.fullmatch(r'"[a-z_]+"', named.get("check_id", "")):
            continue
        check = named["check_id"].strip('"')
        seen.add(check)
        given = set(_dict_keys(named["args"])) if "args" in named else set()
        assert given == expected[check], f"evaluate({check}) passes {sorted(given)}, the check uses {sorted(expected[check])}"
    assert seen == set(ww.checks)  # every check has a worked example in the manual


def test_announce_examples_use_the_declared_template_params(ww: ScenarioSpec) -> None:
    announced = set()
    for name, named, _bare in _calls(ww.manual_text):
        if name != "announce":
            continue
        tid = named["template_id"].strip('"')
        announced.add(tid)
        template = ww.templates[tid]
        keys = set(_dict_keys(named["params"]))
        required = {k for k, spec in template.params.items() if not spec.optional}
        assert required <= keys <= set(template.params), f"announce {tid}: {sorted(keys)} vs {sorted(template.params)}"
    assert announced == {t for t, spec in ww.templates.items() if spec.users == "moderator"}


def test_announcements_go_to_the_right_channels(ww: ScenarioSpec) -> None:
    """The knife only to the wolves, the check result only to the seer's own DM, everything else to the public."""
    routes = {"seer_result": r"dm-.+", "wolf_target": "wolf-team", "wolf_empty": "wolf-team"}
    seen: set[str] = set()
    for name, named, _bare in _calls(ww.manual_text):
        if name != "announce":
            continue
        tid, channel = named["template_id"].strip('"'), named["channel_id"].strip('"')
        assert re.fullmatch(routes.get(tid, "public"), channel), f"{tid} is announced in {channel}"
        seen.add(tid)
    assert {"seer_result", "wolf_target", "wolf_empty"} <= seen


def test_the_limits_in_the_manual_fit_their_mode(ww: ScenarioSpec) -> None:
    checked = []
    for name, named, _bare in _calls(ww.manual_text):
        if name not in ("open_floor", "update_channel", "set_floor_queue") or "limits" not in named:
            continue
        mode = named.get("mode", '"turn"').strip('"') if name == "open_floor" else ("free" if name == "update_channel" else "turn")
        keys, allowed = set(_dict_keys(named["limits"])), set(FREE_LIMIT_KEYS if mode == "free" else TOKEN_LIMIT_KEYS)
        assert keys and keys <= allowed, f"{name}(mode={mode}): {sorted(keys - allowed)} are no limits of that mode"
        checked.append((name, mode))
    assert ("open_floor", "free") in checked  # the wolves' discussion
    assert not any("free_limits" in text for text in (ww.manual_text, ww.rules_text))


def test_the_manual_and_the_moderator_prompt_list_the_keys_of_limits_exactly(ww: ScenarioSpec) -> None:
    prompt = (ROOT / "prompts" / "moderator.md").read_text(encoding="utf-8")
    for text, turn_pattern, free_pattern in (
        (ww.manual_text, r'`mode="turn"` 只接受([^；。]*)', r'`mode="free"` 只接受([^；。]*)'),
        (prompt, r"`turn` 只接受([^；。]*)", r"`free` 只接受([^；。]*)"),
    ):
        turn, free = re.search(turn_pattern, text), re.search(free_pattern, text)
        assert turn and free
        assert re.findall(r"`([a-z_]+)`", turn.group(1)) == list(TOKEN_LIMIT_KEYS)
        assert re.findall(r"`([a-z_]+)`", free.group(1)) == list(FREE_LIMIT_KEYS)


@pytest.mark.parametrize("name", ["werewolf_12p", "minimal_test"])
def test_faction_names_name_every_faction_of_the_roles(name: str) -> None:
    spec = load_scenario_v2(ROOT / "scenarios" / f"{name}.md")
    factions = {role.faction for role in spec.setup.roles.values()}
    assert spec.setup.faction_names and set(spec.setup.faction_names) == factions
    assert len(set(spec.setup.faction_names.values())) == len(factions)  # two factions never share a name


def test_the_winner_texts_of_the_werewolf_manual_are_the_faction_names(ww: ScenarioSpec) -> None:
    names = set(ww.setup.faction_names.values())
    assert ww.setup.faction_names == {"wolves": "狼人阵营", "good": "好人阵营"}
    # what the ``outcome`` check answers is exactly such a name (or "not decided")
    assert _check(_world(ww, out=tuple(WOLVES)), ww, "outcome") in names
    assert _check(_world(ww, out=tuple(GODS)), ww, "outcome") in names
    assert _check(_world(ww), ww, "outcome") not in names
    used = []
    for name, named, _bare in _calls(ww.manual_text):
        if name == "end_game" and re.fullmatch(r'"[^"]+"', named.get("winner", "")):
            used.append(named["winner"].strip('"'))
        if name == "announce" and named.get("template_id") == '"game_over"':
            used += re.findall(r'"winner"\s*:\s*"([^"]+)"', named["params"])
    assert len(used) == 2 and set(used) <= names
    assert "winner 填阵营的显示名" in ww.manual_text
    minimal = load_scenario_v2(ROOT / "scenarios" / "minimal_test.md")
    assert "winner 填阵营的显示名" in minimal.manual_text


# ----------------------------------------------------------------------------- legacy names and prompts


def test_no_legacy_tool_names_survive_in_prompts_knowledge_or_scenarios() -> None:
    files = [*(ROOT / "prompts").glob("*.md"), *(ROOT / "knowledge").glob("*.md"), *(ROOT / "scenarios").glob("*.md")]
    assert len(files) >= 8
    for path in files:
        text = path.read_text(encoding="utf-8")
        found = [name for name in LEGACY_TOOLS if name in text]
        assert not found, f"{path.relative_to(ROOT)} still mentions {found}"


def test_no_text_mentions_the_mechanics_that_v0_12_1_removed() -> None:
    files = [*(ROOT / "prompts").glob("*.md"), *(ROOT / "knowledge").glob("*.md"), *(ROOT / "scenarios").glob("*.md")]
    assert len(files) >= 8
    old_ids = re.compile(r"(?<![A-Za-z0-9_])p(?:0[1-9]|1[0-2])(?![A-Za-z0-9_])")
    for path in files:
        text = path.read_text(encoding="utf-8")
        where = path.relative_to(ROOT)
        assert "debounce" not in text, f"{where}: there is no debounce any more"
        assert "free_limits" not in text, f"{where}: open_floor / update_channel take one ``limits``"
        assert "ambient" not in text, where
        assert not re.search(r"`wait`|\bwait\(", text), f"{where}: the tool is called sleep"
        assert "你的思考不会被保留" not in text, f"{where}: the conversation is persistent now"
        assert not old_ids.search(text), f"{where}: the ids are N1..N12 now"
        assert "SEAT " not in text, where


def test_system_prompts_render_with_the_werewolf_scenario(ww: ScenarioSpec) -> None:
    world = FakeWorld(ww)
    brain = ArenaBrain(world, ww, docs_dir=None, personas={"p1": "冷静而谨慎"}, memories={"p1": "上一局我总被怀疑。"})
    player, moderator = brain.system_prompt("p1"), brain.system_prompt(MODERATOR_ID)
    for text in (player, moderator):
        assert "{{" not in text and "}}" not in text, "unrendered placeholder"
        assert ww.rules_text.strip() in text
        assert "\n\n\n" not in text  # an empty variable leaves no hole
    assert "Alex" in player and "p1" in player and "冷静而谨慎" in player
    assert player.startswith("# 你是谁\n\n你是 **p1 Alex**")  # the form models see: ``<id> <name>``
    # the memory block is for players only (and only when there is a memory)
    assert "# 你的记忆与经验" in player and "上一局我总被怀疑。" in player and "上一局" not in moderator
    assert "你的记忆与经验" not in ArenaBrain(world, ww, docs_dir=None).system_prompt("p1")
    # players never see the manual; the moderator does
    assert ww.manual_text.strip() in moderator
    assert ww.manual_text.strip() not in player and "主持人手册" not in player and "《进度》" not in player
    for must in ("write_note", "speak", "sleep", "respond", "NO_FLOOR", "STALE", "challenge_moderator", "truncated", "compact_context"):
        assert must in player, must
    for must in ("write_note", "sleep", "announce", "open_request", "set_floor_queue", "hold_game", "update_player", "STALE", "limits", "share_answers", "auto_close"):
        assert must in moderator, must
    # the activation model and the persistent conversation (v0.12.1)
    for text in (player, moderator):
        assert "每一条新消息唤醒" in text and "被唤醒不等于" in text
        assert "对话是连续的" in text and "压缩" in text
        assert "你的思考不会被保留" not in text
        assert not re.search(r"\bwait\b", text), "the tool is called sleep now"
    assert "出局后你只会在被直接点名时被唤醒" in player
    assert "不会发送到任何频道" in player.replace("**", "")
    assert "sleep(seconds=60)" in moderator and "催促" in moderator


def test_system_prompts_render_with_the_minimal_scenario() -> None:
    spec = load_scenario_v2(ROOT / "scenarios" / "minimal_test.md")
    brain = ArenaBrain(FakeWorld(spec), spec, docs_dir=None)
    for agent in ("p1", MODERATOR_ID):
        text = brain.system_prompt(agent)
        assert "{{" not in text and spec.rules_text.strip() in text


# ======================================================================================================
# The manual played on the real world: the tool calls of the manual, one by one, through the very path
# of an LLM tool call (``ArenaBrain.execute_tool`` -> commit gate -> ``World``)
# ======================================================================================================

class _GameClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _Table:
    """The real ``World`` + the real tool executor.  ``log`` keeps everything the world delivered (what woke
    whom, v0.12.1: every delivery wakes its recipient), ``inbox`` what the moderator has not looked at yet."""

    def __init__(self, spec: ScenarioSpec, seed: int = 3) -> None:
        self.spec = spec
        self.clock = _GameClock()
        players = [{"id": f"N{i}", "seat": str(i), "name": NAMES[i - 1]} for i in range(1, 13)]
        self.world = World.new_game(spec, players, self.clock, seed=seed)
        self.brain = ArenaBrain(self.world, spec, docs_dir=None)
        self.world.set_running(True, "started")
        self.log: list[tuple[str, dict]] = []
        self.inbox: list[tuple[str, dict]] = []
        self._pump()

    # -- plumbing
    def _pump(self) -> None:
        for d in self.world.drain_outbox():
            env = d.envelope
            item = (d.target, {**env.payload, "_priority": env.priority, "_kind": env.kind, "_interrupt": env.interrupt, "_sender": env.sender})
            self.log.append(item)
            self.inbox.append(item)
        self.world.drain_events()

    def call(self, agent: str, tool: str, **args: object) -> Receipt:
        outcome = self.brain.execute_tool(agent, tool, args, gen_id=1, seen_seq=self.world.seq)
        self._pump()
        assert outcome.receipt is not None, tool
        return outcome.receipt

    def ok(self, agent: str, tool: str, **args: object) -> Receipt:
        receipt = self.call(agent, tool, **args)
        assert receipt.ok, f"{agent}.{tool}({args}) -> {receipt.code}: {receipt.message}"
        return receipt

    def mod(self, tool: str, **args: object) -> Receipt:
        return self.ok(MODERATOR_ID, tool, **args)

    def note(self, agent: str, title: str, content: str, mode: str = "overwrite") -> None:
        """``write_note`` is a shared tool: no commit gate, no receipt, a JSON text result."""
        outcome = self.brain.execute_tool(agent, "write_note", {"title": title, "content": content, "mode": mode}, gen_id=1, seen_seq=self.world.seq)
        assert outcome.shared and json.loads(outcome.text)["ok"] is True

    def tick(self, seconds: float) -> None:
        self.clock.advance(seconds)
        self.world.tick()
        self._pump()

    # -- who was woken by what
    def mark(self) -> int:
        return len(self.log)

    def heard(self, target: str, event: str, *, since: int = 0) -> list[dict]:
        return [p for t, p in self.log[since:] if t == target and p.get("event") == event]

    def who_heard(self, event: str, *, since: int = 0) -> set[str]:
        return {t for t, p in self.log[since:] if p.get("event") == event}

    # -- what the manual keeps asking for
    def ev(self, check: str, **args: object) -> object:
        return self.mod("evaluate", check_id=check, args=args).data["value"]

    def label(self, pid: str) -> str:
        return f"{pid} {self.world.player(pid).name}"

    def alive(self) -> list[str]:
        return self.ev("candidates")  # type: ignore[return-value]

    def ask(self, to: list[str], question: str, options: list[tuple[str, str]], **kw: object) -> str:
        receipt = self.mod(
            "open_request", to=to, kind="choice", question=question,
            options=[{"id": i, "label": label} for i, label in options], **kw,
        )
        return receipt.data["request_id"]

    def answer(self, rid: str, who: str, value: str, reason: str = "") -> None:
        self.ok(who, "respond", request_id=rid, value=value, reason=reason)

    def closed(self, rid: str) -> dict:
        found = [p for t, p in self.inbox if t == MODERATOR_ID and p.get("event") == "request.closed" and p.get("request_id") == rid]
        assert len(found) == 1, f"{rid}: the moderator was woken {len(found)} times"
        return found[0]

    def has_closed(self, rid: str) -> bool:
        """Has the moderator been told that request ``rid`` closed?"""
        return any(t == MODERATOR_ID and p.get("event") == "request.closed" and p.get("request_id") == rid for t, p in self.inbox)

    def woken_by(self, event: str) -> list[dict]:
        found = [p for t, p in self.inbox if t == MODERATOR_ID and p.get("event") == event]
        self.inbox = [(t, p) for t, p in self.inbox if not (t == MODERATOR_ID and p.get("event") == event)]
        return found

    def speak_in_turn(self, channel: str, order: list[str], text: str = "我发言") -> None:
        for pid in order:
            self.ok(pid, "speak", channel_id=channel, content=f"{text} {pid}")

    # -- roles
    def with_role(self, role: str) -> list[str]:
        return [p.id for p in self.world.state.players if p.role == role]

    def one(self, role: str) -> str:
        (pid,) = self.with_role(role)
        return pid

    def out(self, *pids: str) -> None:
        for pid in pids:
            self.mod("update_player", player_id=pid, add_tags=["出局"], remove_tags=["存活"])
        counts = self.ev("counts")
        self.mod("update_dashboard", factions=counts["factions"], tag_stats=counts["tag_stats"])  # type: ignore[index]

    def open_channels(self) -> None:
        """B steps 3-4 of the manual."""
        ids = [p.id for p in self.world.state.players]
        wolves = [p.id for p in self.world.state.players if p.faction == "wolves"]
        self.mod("create_channel", channel_id="public", name="公共频道", type="public", mode="closed", members=ids)
        self.mod("create_channel", channel_id="wolf-team", name="狼人阵营", type="group", mode="closed", members=wolves)

    # -- the wolves' round (N2 of the manual)
    def open_wolf_round(self, night: int = 1) -> tuple[list[str], str]:
        """N2 steps 1-3: the free discussion of the wolves, the shared "knife" request, the opening message.
        With a single wolf left: just the request (step 2).  Returns the wolves asked and the request id."""
        wolves = self.ev("alive_wolves")
        assert isinstance(wolves, list) and wolves
        options = [("none", "空刀"), *[(i, self.label(i)) for i in self.alive()]]
        if len(wolves) == 1:
            return wolves, self.ask(wolves, f"第 {night} 夜：请选择今晚的刀口（可以自刀，也可以空刀）。", options, duration_s=60)
        before = self.mark()
        self.mod(
            "open_floor", channel_id="wolf-team", mode="free", holders=wolves,
            limits={"duration_s": 100, "max_messages_per_member": 6, "max_chars_per_message": 300, "max_chars_total": 3000, "silence_s": 40},
        )
        assert self.log[before:] == [], "opening a free discussion wakes nobody"
        rid = self.ask(
            wolves, f"第 {night} 夜：请和队友商量后选定今晚的刀口", options,
            share_answers=True, auto_close="unanimous", duration_s=150,
        )
        self.mod("send_message", channel_id="wolf-team", content=f"第 {night} 夜：请商议今晚的刀口，并用你的“刀口”请求拍刀。")
        return wolves, rid

    def close_wolf_round(self, rid: str) -> tuple[str, dict]:
        """N2 step 7: close the discussion, count (``wolf_choice``), announce in the wolves' channel only."""
        closed = self.closed(rid)
        if self.world.channel("wolf-team").mode == "free":
            self.mod("close_floor", channel_id="wolf-team")
        answers = [{"player_id": a["player_id"], "value": a["value"]} for a in closed["answers"]]
        target = self.ev("wolf_choice", answers=answers)
        assert isinstance(target, str)
        if target == "none":
            self.mod("announce", channel_id="wolf-team", template_id="wolf_empty", params={})
        else:
            self.mod("announce", channel_id="wolf-team", template_id="wolf_target", params={"target": target})
        return target, closed


def test_the_manual_plays_a_night_the_sheriff_campaign_and_a_white_wolf_explosion(ww: ScenarioSpec) -> None:
    t = _Table(ww)
    ids = [p.id for p in t.world.state.players]
    wolves = [p.id for p in t.world.state.players if p.faction == "wolves"]
    guard, seer, witch, hunter, white = (t.one(r) for r in ("guard", "seer", "witch", "hunter", "white_wolf"))
    villagers = t.with_role("villager")
    plain_wolves = t.with_role("werewolf")
    assert sorted(wolves) == sorted([*plain_wolves, white])

    # ---- B: opening
    t.mod("set_phase", text="准备阶段 · 建立频道")
    t.open_channels()
    t.mod("send_message", channel_id="wolf-team", content=f"你们是狼人阵营的队友：{'、'.join(wolves)}。")
    counts = t.ev("counts")
    t.mod("update_dashboard", factions=counts["factions"], tag_stats=counts["tag_stats"])  # type: ignore[index]
    t.mod("send_message", channel_id="public", content="对局开始。")
    t.note(MODERATOR_ID, "进度", "步骤：N1 守卫（第 1 夜）")
    t.note(MODERATOR_ID, "台账", "开局：频道已建", "append")
    assert set(t.world.memory(MODERATOR_ID).notes) == {"进度", "台账"}
    assert t.world.channel("wolf-team").members == [MODERATOR_ID, *wolves]
    assert len(t.world.state.channels) == 12 + 2  # 12 system DMs + public + wolf-team

    # ---- N1: guard (protects a villager); the manual stores the choice in the guard's attributes
    victim = villagers[0]
    t.mod("set_phase", text="第 1 夜")
    rid = t.ask([guard], "第 1 夜：请选择今晚守护的玩家", [("none", "空守"), *[(i, t.label(i)) for i in t.alive()]], default="none")
    t.answer(rid, guard, victim)
    assert t.closed(rid)["answers"][0]["value"] == victim
    t.mod("update_player", player_id=guard, set_attributes={"last_guard": victim})
    assert t.world.player(guard).attributes["last_guard"] == victim

    # ---- N2: the wolves talk and put their targets down; they disagree, are urged once, argue on, and the
    #      majority decides at the deadline (the "opening -> knife -> urge -> disagreement -> again -> majority
    #      -> announcement" of the plan)
    start = t.mark()
    W, rid = t.open_wolf_round(night=1)
    assert W == wolves
    assert t.who_heard("message.new", since=start) == set(wolves)  # the opening message, and nobody else
    assert t.who_heard("request.opened", since=start) == set(wolves)
    t.note(MODERATOR_ID, "进度", "步骤：N2 狼人商议中｜等待：request.closed｜last_call=0")
    request = t.world.board(white)["requests"][0]
    assert request["share_answers"] is True and request["auto_close"] == "unanimous" and request["answers"] == []
    t.ok(plain_wolves[0], "speak", channel_id="wolf-team", content="刀守卫保护的人吧。")

    t.clock.advance(5)
    knife = t.mark()
    t.answer(rid, plain_wolves[0], victim)
    t.answer(rid, plain_wolves[1], victim)
    t.answer(rid, plain_wolves[2], seer)
    t.answer(rid, white, witch)
    # every answer wakes the other wolves and the moderator, at once, and nobody else (``request.answer``)
    shown = [(target, p) for target, p in t.log[knife:] if p.get("event") == "request.answer"]
    assert len(shown) == 4 * 4 and {target for target, _ in shown} == {*wolves, MODERATOR_ID}
    assert all(p["_priority"] == "normal" and p["value"] in (victim, seer, witch) for _, p in shown)
    assert not any(target == p["player_id"] for target, p in shown)  # nobody is woken by his own answer
    # the wolves see each other's answers on their boards, the others see nothing of it
    request = t.world.board(plain_wolves[1])["requests"][0]
    assert {a["player_id"]: a["value"] for a in request["answers"]} == {
        plain_wolves[0]: victim, plain_wolves[1]: victim, plain_wolves[2]: seer, white: witch,
    }
    assert t.world.board(villagers[1])["requests"] == [] and t.world.board(guard)["requests"] == []
    assert not t.has_closed(rid)  # not unanimous

    # silence -> the advisory -> the moderator urges once (``last_call=1``)
    t.tick(36)  # 41 s: 40 s without a word in the channel
    (advisory,) = t.woken_by("free.advisory")
    assert advisory["reason"] == "silence" and advisory["channel_id"] == "wolf-team"
    mark = t.mark()
    t.mod("send_message", channel_id="wolf-team", content="时间到：请尽快统一刀口。最后期限一到就按多数决定。")
    t.note(MODERATOR_ID, "进度", "步骤：N2 狼人商议中｜等待：request.closed｜last_call=1")
    assert t.who_heard("message.new", since=mark) == set(wolves)

    # the wolves argue on: one of them changes his mind (the last answer counts), the request stays open
    t.clock.advance(4)
    mark = t.mark()
    t.answer(rid, plain_wolves[2], victim)
    changed = [p for target, p in t.log[mark:] if target == white and p.get("event") == "request.answer"]
    assert [(p["player_id"], p["value"], p["replaced"]) for p in changed] == [(plain_wolves[2], victim, True)]
    assert not t.has_closed(rid)
    t.tick(55)  # 100 s: the discussion time is up (and the silence since the urge counts again): more advisories
    late = t.woken_by("free.advisory")
    assert "time" in {a["reason"] for a in late}  # ... which the manual tells the moderator to ignore (``last_call=1``)
    t.tick(51)  # 151 s: the last minute of the request is over, it closes by itself
    target, closed = t.close_wolf_round(rid)
    assert closed["reason"] == "deadline" and closed["missing"] == [] and len(closed["answers"]) == 4
    assert target == victim  # 3 of the 4 wolves: the majority
    note = t.world.ledger[-1]
    assert note.channel_id == "wolf-team" and note.visible_to_ids == [MODERATOR_ID, *wolves]
    assert note.data["content"] == f"【刀口】今晚刀口：{player_label(t.world, victim)}。"
    assert t.world.channel("wolf-team").mode == "closed"
    public_text = " ".join(e.data["content"] for e in t.world.ledger if e.type == "message" and e.channel_id == "public")
    assert "刀口" not in public_text  # the knife never reaches the public channel

    # ---- N3: seer
    rid = t.ask([seer], "第 1 夜：请选择要查验的玩家", [("skip", "不查验"), *[(i, t.label(i)) for i in t.alive()]], default="skip")
    t.answer(rid, seer, white)
    checked = t.closed(rid)["answers"][0]["value"]
    side = "狼人阵营" if t.world.player(checked).faction == "wolves" else "好人阵营"
    t.mod("announce", channel_id=f"dm-{seer}", template_id="seer_result", params={"target": checked, "side": side})
    assert t.world.ledger[-1].visible_to_ids == [MODERATOR_ID, seer]
    assert "属于狼人阵营" in t.world.ledger[-1].data["content"]

    # ---- N4: witch (both potions available, the target is known): poisons the hunter
    attrs = t.world.player(witch).attributes
    assert (attrs["antidote"], attrs["poison"]) == (1, 1)
    t.mod("send_message", channel_id=f"dm-{witch}", content=f"今晚被狼人袭击的是 {t.label(victim)}。")
    options = [("pass", "本晚不用药"), ("save", "使用解药"), *[(f"poison:{i}", f"毒杀 {t.label(i)}") for i in t.alive()]]
    rid = t.ask([witch], "第 1 夜：请选择本晚唯一的用药动作", options, default="pass")
    t.answer(rid, witch, f"poison:{hunter}")
    assert t.closed(rid)["answers"][0]["value"] == f"poison:{hunter}"
    t.mod("update_player", player_id=witch, set_attributes={"poison": 0})
    assert t.world.player(witch).attributes == {**ww.setup.roles["witch"].attributes, "poison": 0}

    # ---- N5: only the guard protected the victim -> survives; the hunter died of the poison (no shot)
    pending_dead = [hunter]

    # ---- D1: sheriff campaign before the death announcement; the pending dead do not take part
    t.mod("set_phase", text="第 1 天 · 警长竞选")
    participants = t.ev("candidates_without", excluded=pending_dead)
    assert hunter not in participants and len(participants) == 11
    rid = t.ask(participants, "是否上警竞选警长？", [("yes", "上警"), ("no", "不上警")], sealed=True, default="no")
    running = {plain_wolves[0], seer, villagers[1]}
    for pid in participants:
        t.answer(rid, pid, "yes" if pid in running else "no")
    answers = t.closed(rid)["answers"]
    candidates = [a["player_id"] for a in answers if a["value"] == "yes"]
    voters = [p for p in participants if p not in candidates]
    assert sorted(candidates) == sorted(running) and len(voters) == 8
    t.mod("announce", channel_id="public", template_id="sheriff_candidates", params={"players": candidates})
    t.mod("enable_ability", ability_id="white_wolf_explode")
    order = t.ev("order_of", members=candidates)
    assert order == sorted(candidates, key=lambda pid: int(pid[1:]))
    t.mod("set_phase", text="第 1 天 · 警长竞选演说")
    t.mod("set_floor_queue", channel_id="public", order=order)
    t.mod("send_message", channel_id=f"dm-{white}", content="白天发言开始，你的“自爆”技能现已启用。")
    assert t.heard(white, "message.new")[-1]["_priority"] == "normal"
    t.speak_in_turn("public", order, "竞选演说")
    (done,) = t.woken_by("floor.completed")
    assert [r["holder"] for r in done["results"]] == order
    t.mod("disable_ability", ability_id="white_wolf_explode")
    rid = t.ask(candidates, "是否退水？", [("stay", "不退水"), ("quit", "退水")], sealed=True, default="stay")
    for pid in candidates:
        t.answer(rid, pid, "quit" if pid == villagers[1] else "stay")
    remaining = [a["player_id"] for a in t.closed(rid)["answers"] if a["value"] == "stay"]
    assert sorted(remaining) == sorted([plain_wolves[0], seer])
    t.mod("send_message", channel_id="public", content=f"退水：{villagers[1]}；仍在竞选：{'、'.join(remaining)}")
    t.mod("announce", channel_id="public", template_id="sheriff_candidates", params={"players": remaining})
    rid = t.ask(voters, "警长投票：请在候选人中选一名。", [(i, t.label(i)) for i in remaining], sealed=True)
    for index, pid in enumerate(voters):
        t.answer(rid, pid, seer if index % 3 else plain_wolves[0])
    vote = t.closed(rid)
    assert len(vote["leaders"]) == 1 and sum(vote["tally"].values()) == len(voters)
    sheriff = vote["leaders"][0]
    summary = "票型：" + "，".join(f"{a['player_id']}→{a['value']}" for a in vote["answers"])
    t.mod("announce", channel_id="public", template_id="vote_result", params={"summary": summary})
    t.mod("update_player", player_id=sheriff, add_tags=["警长"])
    t.mod("announce", channel_id="public", template_id="sheriff", params={"player": sheriff})

    # ---- D2: the death announcement, the tags, the dashboard, the last words (night 1 poison)
    t.mod("set_phase", text="第 1 天 · 公布死讯")
    t.mod("announce", channel_id="public", template_id="death_notice", params={"players": pending_dead})
    t.out(hunter)
    # the hunter is out: broadcasts no longer wake him, what is addressed to him still does (E23)
    mark = t.mark()
    t.mod("set_phase", text="第 1 天 · 遗言")
    assert hunter not in t.who_heard("phase.changed", since=mark) and len(t.who_heard("phase.changed", since=mark)) == 11
    t.mod("set_floor_queue", channel_id="public", order=[hunter])
    assert t.heard(hunter, "floor.granted", since=mark)  # his last words
    t.speak_in_turn("public", [hunter], "遗言")
    assert len(t.woken_by("floor.completed")) == 1
    heard_it = t.who_heard("message.new", since=mark)
    assert hunter not in heard_it and heard_it >= {MODERATOR_ID, *[i for i in ids if i != hunter]}
    assert t.ev("outcome") == "未分胜负"

    # ---- D3: the sheriff chooses the direction, the sheriff speaks last
    rid = t.ask([sheriff], "请决定今日发言方向", [("left", "警左"), ("right", "警右")], default="right")
    t.answer(rid, sheriff, "left")
    direction = t.closed(rid)["answers"][0]["value"]
    t.mod("announce", channel_id="public", template_id="speech_direction", params={"direction": "警左"})
    speech = t.ev("speech_order", start=sheriff, direction=direction)
    assert speech[-1] == sheriff and len(speech) == 11 and hunter not in speech

    # ---- D4 + X: the white wolf explodes during the day speeches and takes the seer
    t.mod("set_phase", text="第 1 天 · 白天发言")
    t.mod("enable_ability", ability_id="white_wolf_explode")
    t.mod("set_floor_queue", channel_id="public", order=speech)
    mark = t.mark()
    t.speak_in_turn("public", speech[:2], "发言")
    # every speech wakes everybody who is alive (and the moderator), except its author
    first = [target for target, p in t.log[mark:] if p.get("event") == "message.new" and p["_sender"] == speech[0]]
    assert sorted(first) == sorted([MODERATOR_ID, *[i for i in ids if i not in (hunter, speech[0])]])
    explode = t.ok(white, "use_ability", ability_id="white_wolf_explode", params={"target": seer, "declaration": "我是白狼王。"})
    assert explode.data["uses_left"] == 0
    (used,) = t.woken_by("ability.used")
    assert used["actor_id"] == white and used["params"]["target"] == seer
    assert t.world.channel("public").frozen  # D25: the floor is frozen until the moderator rules
    assert t.call(speech[2], "speak", channel_id="public", content="还能说吗？").code == "NO_FLOOR"
    t.mod("disable_ability", ability_id="white_wolf_explode")
    t.mod("close_floor", channel_id="public")
    assert not t.world.channel("public").frozen and t.world.channel("public").mode == "closed"
    t.mod("announce", channel_id="public", template_id="death_notice", params={"players": [white, seer]})
    t.out(white, seer)
    assert t.call(white, "use_ability", ability_id="white_wolf_explode", params={"target": seer, "declaration": "x"}).code == "ABILITY_DISABLED"
    assert t.ev("outcome") == "未分胜负"
    announcements = [e.data["content"] for e in t.world.ledger if e.type == "message" and e.channel_id == "public" and e.data["kind"] == "announce"]
    assert any("出局公告" in text and f"{white} " in text for text in announcements)  # players are named "<id> <name>"
    assert not any(role in text for text in announcements for role in ("猎人", "白狼王", "预言家", "被毒"))  # a dark-card game
    assert t.world.state.status == "running"


def test_the_manual_plays_the_vote_the_pk_the_hunter_the_badge_and_the_ending(ww: ScenarioSpec) -> None:
    t = _Table(ww)
    ids = [p.id for p in t.world.state.players]
    wolves = [p.id for p in t.world.state.players if p.faction == "wolves"]
    hunter = t.one("hunter")
    villagers = t.with_role("villager")
    t.mod("create_channel", channel_id="public", name="公共频道", type="public", mode="closed", members=ids)

    # a mid-game state: the first player is sheriff, two villagers are out
    sheriff = ids[0]
    t.mod("update_player", player_id=sheriff, add_tags=["警长"])
    t.out(*[v for v in villagers if v != sheriff][:2])
    alive = t.alive()
    assert hunter in alive and sheriff in alive and len(alive) == 10
    others = [i for i in alive if i not in (sheriff, hunter)]
    rival = others[0]

    # ---- D5: sealed vote with an abstain option; the sheriff's 1.5 votes decide, the engine's own count would not
    t.mod("set_phase", text="第 2 天 · 放逐投票")
    mark = t.mark()
    rid = t.ask(alive, "放逐投票", [("abstain", "弃票"), *[(i, t.label(i)) for i in alive]], sealed=True, default="abstain")
    for_hunter = [sheriff, others[1], others[2]]
    for_rival = [others[3], others[4], others[5]]
    for pid in alive:
        t.answer(rid, pid, hunter if pid in for_hunter else rival if pid in for_rival else "abstain", reason="我的理由，不公布")
    assert not t.who_heard("request.answer", since=mark)  # a sealed vote: nobody is told about the answers
    closed = t.closed(rid)
    assert closed["leaders"] == ["abstain"]  # 4 abstentions beat 3 + 3: the engine's count is not the verdict
    answers = [{"player_id": a["player_id"], "value": a["value"]} for a in closed["answers"]]
    result = t.ev("weighted_vote", answers=answers, privileged=[sheriff], weight=1.5)
    assert result == {"tally": {hunter: 3.5, rival: 3.0}, "leaders": [hunter]}  # type: ignore[comparison-overlap]
    assert not any("我的理由" in e.data.get("content", "") for e in t.world.ledger if e.type == "message")

    # ---- D6: a (forced) tie goes to PK speeches, then the others vote again
    tied = sorted([hunter, rival], key=lambda pid: int(pid[1:]))
    t.mod("announce", channel_id="public", template_id="tie_pk", params={"players": tied})
    t.mod("enable_ability", ability_id="white_wolf_explode")
    order = t.ev("order_of", members=tied)
    assert order == tied
    t.mod("set_floor_queue", channel_id="public", order=order)
    t.speak_in_turn("public", order, "PK")
    assert len(t.woken_by("floor.completed")) == 1
    t.mod("disable_ability", ability_id="white_wolf_explode")
    revoters = [i for i in alive if i not in tied]
    rid = t.ask(revoters, "平票重投", [("abstain", "弃票"), *[(i, t.label(i)) for i in tied]], sealed=True, default="abstain")
    for pid in revoters:
        t.answer(rid, pid, hunter)
    answers = [{"player_id": a["player_id"], "value": a["value"]} for a in t.closed(rid)["answers"]]
    result = t.ev("weighted_vote", answers=answers, privileged=[sheriff] if sheriff in revoters else [], weight=1.5)
    assert result["leaders"] == [hunter]  # type: ignore[index]

    # ---- D7 + E: the exiled hunter shoots the sheriff, the badge passes on, the hunter has last words
    t.mod("announce", channel_id="public", template_id="vote_result", params={"summary": "票型：……；得票：……"})
    t.mod("announce", channel_id="public", template_id="death_notice", params={"players": [hunter]})
    t.out(hunter)
    mark = t.mark()
    rid = t.ask([hunter], "你出局了，可以亮出猎人身份开枪。", [("none", "不开枪"), *[(i, t.label(i)) for i in t.alive()]], default="none")
    assert t.heard(hunter, "request.opened", since=mark)  # out, and still woken: the request is addressed to him
    t.answer(rid, hunter, sheriff)
    shot = t.closed(rid)["answers"][0]["value"]
    assert shot == sheriff
    t.mod("announce", channel_id="public", template_id="hunter_shot", params={"hunter": hunter, "target": shot})
    t.mod("announce", channel_id="public", template_id="death_notice", params={"players": [shot]})
    t.out(shot)
    heirs = t.alive()
    mark = t.mark()
    rid = t.ask([sheriff], "你是警长且已出局：请选择警徽的去向。", [("destroy", "撕毁警徽"), *[(i, t.label(i)) for i in heirs]], default="destroy")
    assert t.heard(sheriff, "request.opened", since=mark)
    t.answer(rid, sheriff, heirs[0])
    heir = t.closed(rid)["answers"][0]["value"]
    t.mod("update_player", player_id=sheriff, remove_tags=["警长"])
    t.mod("update_player", player_id=heir, add_tags=["警长"])
    t.mod("announce", channel_id="public", template_id="sheriff", params={"player": heir})
    t.mod("update_dashboard", **{k: v for k, v in t.ev("counts").items()})  # type: ignore[attr-defined]
    assert [p.id for p in t.world.state.players if "警长" in p.tags] == [heir]
    t.mod("set_phase", text="第 2 天 · 遗言")
    t.mod("set_floor_queue", channel_id="public", order=[hunter])
    t.speak_in_turn("public", [hunter], "遗言")
    assert len(t.woken_by("floor.completed")) == 1
    assert t.ev("outcome") == "未分胜负" and t.ev("good_wins") is False and t.ev("wolves_win") is False

    # ---- G: the wolf side is gone -> the good side wins, the moderator announces and ends the game
    t.out(*wolves)
    assert (t.ev("outcome"), t.ev("good_wins")) == ("好人阵营", True)
    winner = t.ev("outcome")
    assert winner in ww.setup.faction_names.values()  # ``end_game(winner=…)``: the display name of the faction
    t.mod("announce", channel_id="public", template_id="game_over", params={"winner": winner, "summary": "所有狼人阵营成员出局。"})
    t.mod("end_game", winner=winner, summary="所有狼人阵营成员出局。")
    assert t.world.state.status == "finished" and t.world.state.winner == "好人阵营" and not t.world.running


# ----------------------------------------------------------------------------- the wolves' round, variants


def _night_table(ww: ScenarioSpec, *, dead_wolves: tuple[int, ...] = ()) -> tuple[_Table, list[str]]:
    """A table at night with the channels open.  ``dead_wolves`` are positions (by seat) among the 4 wolves of
    the wolf side (the roles are dealt at random); returns the table and ALL the wolves in seat order."""
    t = _Table(ww)
    t.open_channels()
    wolves = [p.id for p in t.world.state.players if p.faction == "wolves"]
    if dead_wolves:
        t.out(*[wolves[i] for i in dead_wolves])
    t.mod("set_phase", text="第 2 夜")
    return t, wolves


def test_the_wolves_agree_and_the_request_closes_by_itself(ww: ScenarioSpec) -> None:
    t, wolves = _night_table(ww)
    W, rid = t.open_wolf_round(night=2)
    victim, other = t.with_role("villager")[:2]
    t.answer(rid, wolves[0], other)
    t.answer(rid, wolves[1], victim)
    t.answer(rid, wolves[2], victim)
    assert not t.has_closed(rid)
    t.answer(rid, wolves[0], victim)  # he gives in ...
    t.answer(rid, wolves[3], victim)  # ... and the last one agrees: everybody answered, all the same
    closed = t.closed(rid)  # no ``tick``: it closed the moment the last wolf agreed
    assert closed["reason"] == "unanimous" and closed["leaders"] == [victim] and t.world.clock.now() == 0
    assert t.world.state.requests[rid].status == "closed"
    target, _ = t.close_wolf_round(rid)
    assert target == victim and t.world.channel("wolf-team").mode == "closed"
    # nothing is left running: no deadline of the request, no advisory of the discussion
    t.tick(500)
    assert t.woken_by("free.advisory") == []
    assert len(t.woken_by("request.closed")) == 1  # the unanimous close; the deadline did not close it a second time


def test_a_tie_between_the_wolves_is_decided_by_the_first_wolf_by_seat(ww: ScenarioSpec) -> None:
    for first_pick, second_pick in (("A", "B"), ("B", "A")):
        t, wolves = _night_table(ww)
        W, rid = t.open_wolf_round(night=2)
        a, b = t.with_role("villager")[:2]
        picks = {"A": a, "B": b}
        # two against two; the wolf who is first by seat (``wolves[0]``) is on the side of ``first_pick``
        for pid, side in zip(wolves, (first_pick, second_pick, second_pick, first_pick)):
            t.answer(rid, pid, picks[side])
        assert not t.has_closed(rid)
        t.tick(151)
        target, closed = t.close_wolf_round(rid)
        assert closed["reason"] == "deadline" and sorted(closed["leaders"]) == sorted([a, b])
        assert target == picks[first_pick]  # not "a", not "b": the pick of the first wolf


def test_the_wolves_say_nothing_and_the_night_has_no_victim(ww: ScenarioSpec) -> None:
    t, wolves = _night_table(ww)
    W, rid = t.open_wolf_round(night=2)
    t.tick(41)
    assert [a["reason"] for a in t.woken_by("free.advisory")] == ["silence"]
    t.tick(60)
    assert [a["reason"] for a in t.woken_by("free.advisory")] == ["time"]
    t.tick(50)
    target, closed = t.close_wolf_round(rid)
    assert closed["reason"] == "deadline" and closed["answers"] == [] and closed["missing"] == wolves
    assert target == "none"
    assert t.world.ledger[-1].data["content"] == "【刀口】今晚空刀。"
    assert t.world.ledger[-1].visible_to_ids == [MODERATOR_ID, *wolves]


def test_only_some_of_the_wolves_answer(ww: ScenarioSpec) -> None:
    t, wolves = _night_table(ww)
    W, rid = t.open_wolf_round(night=2)
    victim = t.with_role("seer")[0]
    t.answer(rid, wolves[1], victim)
    t.answer(rid, wolves[2], victim)
    t.tick(151)
    target, closed = t.close_wolf_round(rid)
    assert closed["reason"] == "deadline" and closed["missing"] == [wolves[0], wolves[3]]
    assert target == victim  # nobody defaulted for the silent ones: the answers that exist decide


def test_a_single_wolf_is_asked_directly(ww: ScenarioSpec) -> None:
    t, wolves = _night_table(ww, dead_wolves=(0, 1, 2))
    assert t.ev("alive_wolves") == [wolves[3]]
    mark = t.mark()
    (only,), rid = t.open_wolf_round(night=3)
    assert t.world.channel("wolf-team").mode == "closed"  # no discussion: there is nobody to talk to
    assert t.who_heard("request.opened", since=mark) == {only}
    victim = t.with_role("guard")[0]
    t.answer(rid, only, victim)
    target, closed = t.close_wolf_round(rid)
    assert closed["reason"] == "all_answered" and target == victim
    # a lone wolf who does not answer in time: an empty kill
    t2, _ = _night_table(ww, dead_wolves=(0, 1, 2))
    (only2,), rid2 = t2.open_wolf_round(night=3)
    t2.tick(61)
    assert t2.close_wolf_round(rid2)[0] == "none"


def test_wolves_who_are_out_sleep_through_the_round_but_can_read_it(ww: ScenarioSpec) -> None:
    t, wolves = _night_table(ww, dead_wolves=(1,))
    dead = wolves[1]
    mark = t.mark()
    W, rid = t.open_wolf_round(night=2)
    assert W == [wolves[0], wolves[2], wolves[3]]
    victim = t.with_role("villager")[0]
    for pid in W:
        t.ok(pid, "speak", channel_id="wolf-team", content=f"{pid} 同意刀 {victim}")
        t.answer(rid, pid, victim)
    target, closed = t.close_wolf_round(rid)
    assert closed["reason"] == "unanimous" and target == victim and len(closed["answers"]) == 3
    # nothing of it reached the dead wolf ...
    assert [p for target_, p in t.log[mark:] if target_ == dead] == []
    # ... and he does not need to be asked: he is not a recipient of the request
    assert dead not in t.world.state.requests[rid].to
    # but the history is his to read
    seen = [e.data["content"] for e in t.world.entries_visible(dead) if e.type == "message" and e.channel_id == "wolf-team"]
    assert any("今晚刀口" in text for text in seen) and any("同意刀" in text for text in seen)


# ======================================================================================================
# prompts, config wiring and broken copies of the werewolf scenario
# ======================================================================================================

_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def test_prompt_templates_use_only_the_documented_variables() -> None:
    participant = (ROOT / "prompts" / "participant.md").read_text(encoding="utf-8")
    moderator = (ROOT / "prompts" / "moderator.md").read_text(encoding="utf-8")
    assert set(_VARIABLE.findall(participant)) == {"id", "name", "seat", "persona", "memory_block", "rules"}
    assert set(_VARIABLE.findall(moderator)) == {"rules", "manual"}  # the moderator has no memory block
    assert participant.count("{{rules}}") == 1 and moderator.count("{{rules}}") == 1 and moderator.count("{{manual}}") == 1
    assert participant.count("{{memory_block}}") == 1
    # the memory block is a whole paragraph of its own (it disappears completely when it is empty)
    lines = participant.splitlines()
    at = lines.index("{{memory_block}}")
    assert lines[at - 1] == "" and lines[at + 1] == ""


def test_the_prompts_stay_game_agnostic() -> None:
    from .test_neutrality import FIXED_PARTICIPANT_ID_RE, FORBIDDEN_TERMS

    for name in ("participant.md", "moderator.md"):
        text = (ROOT / "prompts" / name).read_text(encoding="utf-8")
        assert not [term for term in FORBIDDEN_TERMS if term in text], name
        assert not FIXED_PARTICIPANT_ID_RE.search(text), name


def test_the_config_points_at_files_that_load() -> None:
    config = yaml.safe_load((ROOT / "config.yml").read_text(encoding="utf-8"))
    spec = load_scenario_v2(config["scenario"]["path"])  # relative: resolved against app.config.BASE_DIR
    assert spec.id == "werewolf_12p" and spec.manual_text and spec.rules_text
    for key in ("moderator_system", "participant_system"):
        assert (ROOT / config["prompts"][key]).is_file(), key
    assert (ROOT / config["knowledge_base"]["docs_dir"] / "rules_12p_standard.md").is_file()
    participants = config["participants"]
    assert len(participants) == sum(item.count for item in spec.setup.identity_pool)
    # v0.12.1 (E13): the ids are N1..N12 and are the seat numbers, the names have no number, no persona field
    assert [p["id"] for p in participants] == [f"N{i}" for i in range(1, 13)]
    assert [p["seat"] for p in participants] == [str(i) for i in range(1, 13)]
    assert [p["name"] for p in participants] == NAMES
    assert len({p["name"] for p in participants}) == 12 and not any(re.search(r"\d", p["name"]) for p in participants)
    assert all(set(p) == {"id", "seat", "name"} for p in participants)
    tag_styles = set(config["tag_styles"])
    assert {role.tags[0] for role in spec.setup.roles.values()} <= tag_styles


def _broken(old: str, new: str) -> str:
    source = WEREWOLF.read_text(encoding="utf-8")
    assert source.count(old) == 1, old
    return source.replace(old, new)


@pytest.mark.parametrize(
    "old, new, fragment",
    [
        ("  good_wins: \"${count(alive(by_faction('wolves'))) == 0}\"", "  good_wins: \"${count(alive(by_faction('wolves')) == 0}\"", "checks.good_wins"),
        ("text: \"【出局公告】{{players}} 出局。\"", "text: \"【出局公告】{{nobody}} 出局。\"", "templates.death_notice"),
        ("    holders: \"${alive(by_role('white_wolf'))}\"", "    holders: \"${alive(by_role('white_wolf')}\"", "abilities.white_wolf_explode.holders"),
        ("      text: \"【自爆】{{actor}} 亮明白狼王身份并自爆，带走 {{target}}。{{declaration}}\"", "      text: \"【自爆】{{actor}} {{boom}}\"", "abilities.white_wolf_explode.reveal"),
        ("    - {role: guard, count: 1}", "    - {role: ghost, count: 1}", "undeclared role"),
        ("schema_version: 2", "schema_version: 1", "schema_version"),
        ("    params: {target: player_id, side: \"enum(好人阵营,狼人阵营)\"}\n    users: moderator", "    params: {target: player_id, side: \"enum()\"}\n    users: moderator", "校验失败"),
    ],
)
def test_a_broken_werewolf_scenario_names_the_broken_part(old: str, new: str, fragment: str) -> None:
    with pytest.raises(ScenarioError) as info:
        parse_scenario_v2(_broken(old, new), where="werewolf_12p.md")
    assert fragment in str(info.value) and "werewolf_12p.md" in str(info.value)


def test_the_manual_heading_is_configurable() -> None:
    source = WEREWOLF.read_text(encoding="utf-8")
    spec = parse_scenario_v2(source.replace("# 主持人手册\n", "# 裁判须知\n").replace("manual_heading: 主持人手册", "manual_heading: 裁判须知"))
    assert spec.manual_text.startswith("# 裁判须知") and "开局 B" in spec.manual_text and "开局 B" not in spec.rules_text


def test_a_manual_that_cannot_be_split_off_is_refused_instead_of_leaking_to_the_players() -> None:
    source = WEREWOLF.read_text(encoding="utf-8")
    # a declared heading that the body does not contain
    with pytest.raises(ScenarioError, match="manual_heading") as info:
        parse_scenario_v2(source.replace("manual_heading: 主持人手册", "manual_heading: 不存在的标题"), where="x.md")
    assert "不存在的标题" in str(info.value) and "x.md" in str(info.value)
    # a typo in the body heading is the same mistake seen from the other side
    with pytest.raises(ScenarioError, match="manual_heading"):
        parse_scenario_v2(source.replace("# 主持人手册\n", "# 主持人手侧\n"))
    # two sections with the heading: the second would be shown to the players
    with pytest.raises(ScenarioError, match="出现了 2 次"):
        parse_scenario_v2(source + "\n# 主持人手册\n\n另一份手册。\n")
    # a scenario that declares nothing may have no manual at all (unit-test scenarios do)
    plain = parse_scenario_v2(GOOD_HEAD.format(extra="") + "\n# 规则\n只有规则。\n")
    assert plain.manual_text == "" and "只有规则" in plain.rules_text
    # headings inside code fences do not count
    fenced = parse_scenario_v2(GOOD_HEAD.format(extra="manual_heading: 手册\n") + "\n# 规则\n```\n# 手册\n```\n\n# 手册\n正文\n")
    assert fenced.manual_text == "# 手册\n正文" and "# 手册" in fenced.rules_text


_CJK = "\u3000-\u303f\uff00-\uffef\u4e00-\u9fff"


def test_front_matter_texts_have_no_yaml_folding_artifacts(ww: ScenarioSpec) -> None:
    """A folded YAML scalar (``>-``) puts a space at every line break, which looks wrong inside Chinese text."""
    texts = {
        **{f"roles.{rid}.brief": role.brief for rid, role in ww.setup.roles.items()},
        **{f"abilities.{aid}.description": ability.description for aid, ability in ww.abilities.items()},
        **{f"templates.{tid}.text": template.text for tid, template in ww.templates.items()},
        **{f"templates.{tid}.description": template.description for tid, template in ww.templates.items()},
    }
    assert len(texts) > 20
    for where, text in texts.items():
        assert not re.search(f"[{_CJK}] [{_CJK}]", text), f"{where}: {text!r}"
        assert text == text.strip() and "\n" not in text, where
