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
    WAIT_TOOL,
    shared_tool_schemas,
)
from app.world.core import WorldCore
from app.world.intents import MODERATOR_INTENTS, PLAYER_INTENTS
from app.world.loader import load_scenario_v2, parse_scenario_v2, split_body
from app.world.render import render_text, validate_params
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


# ======================================================================================================
# werewolf_12p (schema v2): structure, manual references, checks, templates, prompts
# ======================================================================================================


WEREWOLF = ROOT / "scenarios" / "werewolf_12p.md"
ALL_TOOLS = {*MODERATOR_TOOL_INTENTS, *PLAYER_TOOL_INTENTS, WAIT_TOOL, *SHARED_TOOL_NAMES}
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


def _players(spec: ScenarioSpec, out: frozenset[str] = frozenset()) -> list[PlayerState]:
    """p01.. in identity-pool order: 3 werewolves, the white wolf, 4 villagers, seer, witch, hunter, guard."""
    players = []
    for index, role_id in enumerate(_pool(spec), start=1):
        role = spec.setup.roles[role_id]
        pid = f"p{index:02d}"
        tags = [t for t in role.tags if not (pid in out and t == "存活")] + (["出局"] if pid in out else [])
        players.append(
            PlayerState(
                id=pid, seat=f"SEAT {index:02d}", name=f"Name{index}", role=role_id, faction=role.faction,
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


ROLE_OF = {f"p{i:02d}": role for i, role in enumerate(
    ["werewolf"] * 3 + ["white_wolf"] + ["villager"] * 4 + ["seer", "witch", "hunter", "guard"], start=1)}
WOLVES = ["p01", "p02", "p03", "p04"]
VILLAGERS = ["p05", "p06", "p07", "p08"]
GODS = ["p09", "p10", "p11", "p12"]


# ----------------------------------------------------------------------------- structure


def test_werewolf_scenario_loads_with_the_expected_setup(ww: ScenarioSpec) -> None:
    assert (ww.id, ww.schema_version, ww.version) == ("werewolf_12p", 2, "2.0.0")
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
    assert (d.free.max_chars_total, d.free.debounce_s, d.free.silence_s) == (2000, 1.5, 30)
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
    assert _check(world, ww, "candidates") == [f"p{i:02d}" for i in range(1, 13)]
    assert _check(world, ww, "alive_wolves") == WOLVES and _check(world, ww, "first_wolf") == "p01"
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

    world = _world(ww, out=("p02", "p05", "p11"))
    world.state.players[6].tags.append("警长")
    counts = _check(world, ww, "counts")
    ModUpdateDashboard.model_validate(counts)  # exactly the payload of update_dashboard
    assert [f["alive"] for f in counts["factions"]] == [3, 3, 3]
    assert {t["name"]: t["count"] for t in counts["tag_stats"]} == {"存活": 9, "出局": 3, "警长": 1}


@pytest.mark.parametrize(
    "out, good, wolves, outcome",
    [
        ((), False, False, "未分胜负"),
        (("p01", "p02", "p03"), False, False, "未分胜负"),  # the white wolf is still alive
        (tuple(WOLVES), True, False, "好人阵营"),
        (tuple(GODS), False, True, "狼人阵营"),  # all gods out: wolves win
        (tuple(VILLAGERS), False, True, "狼人阵营"),  # all villagers out: wolves win
        (("p09", "p10", "p11"), False, False, "未分胜负"),  # one god left
        ((*WOLVES, *GODS), True, True, "好人阵营"),  # both conditions at once: the good side wins
    ],
)
def test_victory_checks(ww: ScenarioSpec, out: tuple[str, ...], good: bool, wolves: bool, outcome: str) -> None:
    world = _world(ww, out=out)
    assert _check(world, ww, "good_wins") is good
    assert _check(world, ww, "wolves_win") is wolves
    assert _check(world, ww, "outcome") == outcome


def test_wolf_helpers_follow_the_seat_order_and_the_dead(ww: ScenarioSpec) -> None:
    world = _world(ww, out=("p01",))
    assert _check(world, ww, "alive_wolves") == ["p02", "p03", "p04"] and _check(world, ww, "first_wolf") == "p02"
    world = _world(ww, out=tuple(WOLVES))
    assert _check(world, ww, "alive_wolves") == [] and _check(world, ww, "first_wolf") == ""


def test_candidates_without_removes_the_pending_dead(ww: ScenarioSpec) -> None:
    world = _world(ww, out=("p05",))
    assert _check(world, ww, "candidates") == [i for i in _check(world, ww, "candidates")] and "p05" not in _check(world, ww, "candidates")
    assert _check(world, ww, "candidates_without", excluded=["p03", "p11"]) == [
        "p01", "p02", "p04", "p06", "p07", "p08", "p09", "p10", "p12",
    ]
    assert len(_check(world, ww, "candidates_without", excluded=[])) == 11


SEATS = [f"p{i:02d}" for i in range(1, 13)]


@pytest.mark.parametrize(
    "start, direction, out, expected",
    [
        ("", "left", (), SEATS),
        ("p07", "left", (), ["p08", "p09", "p10", "p11", "p12", "p01", "p02", "p03", "p04", "p05", "p06", "p07"]),
        ("p07", "right", (), ["p06", "p05", "p04", "p03", "p02", "p01", "p12", "p11", "p10", "p09", "p08", "p07"]),
        ("p01", "left", (), SEATS[1:] + ["p01"]),
        ("p01", "right", (), ["p12", "p11", "p10", "p09", "p08", "p07", "p06", "p05", "p04", "p03", "p02", "p01"]),
        ("p07", "left", ("p08", "p01"), ["p09", "p10", "p11", "p12", "p02", "p03", "p04", "p05", "p06", "p07"]),
        ("p07", "right", ("p06", "p12"), ["p05", "p04", "p03", "p02", "p01", "p11", "p10", "p09", "p08", "p07"]),
        ("", "left", ("p01", "p02"), SEATS[2:]),
        ("p02", "left", ("p02",), SEATS[0:1] + SEATS[2:]),  # a dead "sheriff" id: plain seat order, nobody last
    ],
)
def test_speech_order(ww: ScenarioSpec, start: str, direction: str, out: tuple[str, ...], expected: list[str]) -> None:
    world = _world(ww, out=out)
    assert _check(world, ww, "speech_order", start=start, direction=direction) == expected


def test_order_of_sorts_the_given_members_by_seat(ww: ScenarioSpec) -> None:
    world = _world(ww)
    assert _check(world, ww, "order_of", members=["p09", "p02", "p05"]) == ["p02", "p05", "p09"]
    assert _check(world, ww, "order_of", members=["p10", "ghost"]) == ["p10"]
    assert _check(world, ww, "order_of", members=[]) == []


def _answers(**votes: str) -> list[dict[str, str]]:
    return [{"player_id": voter, "value": target} for voter, target in votes.items()]


def test_weighted_vote_gives_the_sheriff_one_and_a_half_votes(ww: ScenarioSpec) -> None:
    world = _world(ww)
    answers = _answers(p01="p05", p02="p05", p03="abstain", p07="p06", p08="p06", p09="p05")
    result = _check(world, ww, "weighted_vote", answers=answers, privileged=["p07"], weight=1.5)
    assert result == {"tally": {"p05": 3.0, "p06": 2.5}, "leaders": ["p05"]}
    # the sheriff's 1.5 votes decide a close vote
    close = _answers(p01="p05", p02="p05", p07="p06", p08="p06", p09="p06")
    assert _check(world, ww, "weighted_vote", answers=close, privileged=["p07"], weight=1.5)["leaders"] == ["p06"]
    assert _check(world, ww, "weighted_vote", answers=close, privileged=[], weight=1)["leaders"] == ["p06"]


def test_weighted_vote_ties_abstentions_and_empty_votes(ww: ScenarioSpec) -> None:
    world = _world(ww)
    tie = _answers(p01="p05", p02="p05", p03="p06", p04="p06")
    assert _check(world, ww, "weighted_vote", answers=tie, privileged=[], weight=1) == {
        "tally": {"p05": 2.0, "p06": 2.0}, "leaders": ["p05", "p06"],
    }
    nobody = _answers(p01="abstain", p02="abstain")
    assert _check(world, ww, "weighted_vote", answers=nobody, privileged=[], weight=1) == {"tally": {}, "leaders": []}
    assert _check(world, ww, "weighted_vote", answers=[], privileged=[], weight=1) == {"tally": {}, "leaders": []}
    # the sheriff abstaining adds nothing; a sheriff vote for a rival breaks the tie
    mixed = _answers(p01="p05", p02="p06", p07="p06")
    assert _check(world, ww, "weighted_vote", answers=mixed, privileged=["p07"], weight=1.5)["leaders"] == ["p06"]


def test_checks_reject_missing_variables_clearly(ww: ScenarioSpec) -> None:
    from app.engine.expressions import ExpressionError

    world = _world(ww)
    with pytest.raises(ExpressionError, match="Unknown name: members"):
        world.eval_expr(ww.checks["order_of"], {})


def test_the_ability_holders_follow_the_white_wolf(ww: ScenarioSpec) -> None:
    holders = ww.abilities["white_wolf_explode"].holders
    assert _world(ww).to_plain(_world(ww).eval_expr(holders)) == ["p04"]
    dead = _world(ww, out=("p04",))
    assert dead.to_plain(dead.eval_expr(holders)) == []
    ability = ww.abilities["white_wolf_explode"]
    assert ability.quota == 1 and not ability.enabled and ability.freeze_channels == ["public"]
    assert ability.reveal is not None and ability.reveal.channel_id == "public"
    assert set(ability.params) == {"target", "declaration"}


# ----------------------------------------------------------------------------- templates


def _sample(spec: ParamSpec) -> object:
    return {
        "player_id": "p01",
        "player_ids": ["p01", "p02"],
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
    clean, _ = validate_params(template.params, {"players": ["p01", "p02"]}, world)
    assert render_text(template.text, clean or {}, template.params, world) == "【出局公告】Name1（p01）、Name2（p02） 出局。"
    seer = ww.templates["seer_result"]
    clean, _ = validate_params(seer.params, {"target": "p03", "side": "狼人阵营"}, world)
    assert render_text(seer.text, clean or {}, seer.params, world) == "【查验结果】Name3（p03） 属于狼人阵营。"
    # the side must be one of the two factions
    assert validate_params(seer.params, {"target": "p03", "side": "平民"}, world)[1] is not None


def test_sensitive_templates_are_moderator_only_and_neutral(ww: ScenarioSpec) -> None:
    moderator = {tid for tid, t in ww.templates.items() if t.users == "moderator"}
    players = {tid for tid, t in ww.templates.items() if t.users in ("players", "any")}
    assert moderator == {
        "death_notice", "peaceful_night", "vote_result", "no_exile", "tie_pk", "seer_result", "sheriff_candidates",
        "sheriff", "no_sheriff", "speech_direction", "hunter_shot", "game_over",
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
    elif name == WAIT_TOOL:
        return set(), set()
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
        "game.started", "game.resumed", "game.continued", "request.closed", "floor.completed", "free.advisory",
        "ability.used", "challenge.opened", "message.new", "watchdog",
    }
)
MANUAL_WORDS = frozenset(
    {
        "silence", "time", "chars",  # reasons of ``free.advisory``
        "code", "message",  # fields of a receipt
        "last_guard",  # an attribute of the guard (see the scenario roles)
        "save",  # an option id of the witch's request
        "public", "wolf-team",  # channel ids
    }
)


def test_every_backticked_word_of_the_manual_is_known(ww: ScenarioSpec) -> None:
    arguments = {f for model in [*MODERATOR_INTENTS.values(), *PLAYER_INTENTS.values()] for f in model.model_fields}
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
    assert MANUAL_EVENTS <= named and named <= MANUAL_EVENTS | {"request.closed"}
    namer = Namer(lambda agent_id: agent_id)
    for event in sorted(MANUAL_EVENTS):
        text = render_wake([Envelope(kind="notice", priority="urgent", payload={"event": event})], [], namer, agent_id=MODERATOR_ID)
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


# ----------------------------------------------------------------------------- legacy names and prompts


def test_no_legacy_tool_names_survive_in_prompts_knowledge_or_scenarios() -> None:
    files = [*(ROOT / "prompts").glob("*.md"), *(ROOT / "knowledge").glob("*.md"), *(ROOT / "scenarios").glob("*.md")]
    assert len(files) >= 8
    for path in files:
        text = path.read_text(encoding="utf-8")
        found = [name for name in LEGACY_TOOLS if name in text]
        assert not found, f"{path.relative_to(ROOT)} still mentions {found}"


def test_system_prompts_render_with_the_werewolf_scenario(ww: ScenarioSpec) -> None:
    world = FakeWorld(ww)
    brain = ArenaBrain(world, ww, docs_dir=None, personas={"p1": "冷静而谨慎"})
    player, moderator = brain.system_prompt("p1"), brain.system_prompt(MODERATOR_ID)
    for text in (player, moderator):
        assert "{{" not in text and "}}" not in text, "unrendered placeholder"
        assert ww.rules_text.strip() in text
    assert "Alex" in player and "p1" in player and "冷静而谨慎" in player
    # players never see the manual; the moderator does
    assert ww.manual_text.strip() in moderator
    assert ww.manual_text.strip() not in player and "主持人手册" not in player and "《进度》" not in player
    for must in ("write_note", "speak", "wait", "respond", "NO_FLOOR", "STALE", "challenge_moderator", "truncated"):
        assert must in player, must
    for must in ("write_note", "wait", "announce", "open_request", "set_floor_queue", "hold_game", "update_player", "STALE"):
        assert must in moderator, must
    assert "你的思考不会被保留" in player and "你的思考不会被保留" in moderator
    assert "正文不会发送到任何频道" in player.replace("**", "") or "不会发送到任何频道" in player


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
    """The real ``World`` + the real tool executor; ``deliveries`` collects what the moderator is woken with."""

    def __init__(self, spec: ScenarioSpec, seed: int = 3) -> None:
        self.spec = spec
        self.clock = _GameClock()
        players = [{"id": f"p{i:02d}", "seat": f"SEAT {i:02d}", "name": f"N{i}", "persona": ""} for i in range(1, 13)]
        self.world = World.new_game(spec, players, self.clock, seed=seed)
        self.brain = ArenaBrain(self.world, spec, docs_dir=None)
        self.world.set_running(True, "started")
        self.inbox: list[tuple[str, dict]] = []  # (target, envelope payload) not yet looked at
        self._pump()

    # -- plumbing
    def _pump(self) -> None:
        self.inbox += [(d.target, {**d.envelope.payload, "_priority": d.envelope.priority}) for d in self.world.drain_outbox()]
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


def test_the_manual_plays_a_night_the_sheriff_campaign_and_a_white_wolf_explosion(ww: ScenarioSpec) -> None:
    t = _Table(ww)
    ids = [p.id for p in t.world.state.players]
    wolves = [p.id for p in t.world.state.players if p.faction == "wolves"]
    guard, seer, witch, hunter, white = (t.one(r) for r in ("guard", "seer", "witch", "hunter", "white_wolf"))
    villagers = t.with_role("villager")
    plain_wolf = t.with_role("werewolf")[0]
    assert sorted(wolves) == sorted([*t.with_role("werewolf"), white])

    # ---- B: opening
    t.mod("set_phase", text="准备阶段 · 建立频道")
    t.mod("create_channel", channel_id="public", name="公共频道", type="public", mode="closed", members=ids)
    t.mod("create_channel", channel_id="wolf-team", name="狼人阵营", type="group", mode="closed", members=wolves)
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
    rid = t.ask([guard], "第 1 夜：请选择今晚守护的玩家", [("none", "空守"), *[(i, t.label(i)) for i in t.alive() if i != ""]], default="none")
    t.answer(rid, guard, victim)
    assert t.closed(rid)["answers"][0]["value"] == victim
    t.mod("update_player", player_id=guard, set_attributes={"last_guard": victim})
    assert t.world.player(guard).attributes["last_guard"] == victim

    # ---- N2: the wolves talk, fall silent, the moderator closes the floor and asks the first wolf
    alive_wolves = t.ev("alive_wolves")
    assert alive_wolves == wolves
    t.mod("open_floor", channel_id="wolf-team", mode="free", holders=alive_wolves,
          free_limits={"duration_s": 180, "max_messages_per_member": 3, "max_chars_total": 1500})
    t.mod("send_message", channel_id="wolf-team", content="第 1 夜：请商议今晚的刀口。")
    woken = {target for target, p in t.inbox if p.get("event") == "message.new" and p["_priority"] == "normal"}
    assert woken == set(wolves)  # opening the discussion woke nobody, the opening message did
    t.ok(plain_wolf, "speak", channel_id="wolf-team", content="刀守卫保护的人吧。")
    t.tick(31)  # 30 s of silence
    (advisory,) = t.woken_by("free.advisory")
    assert advisory["reason"] == "silence" and advisory["channel_id"] == "wolf-team"
    t.mod("close_floor", channel_id="wolf-team")
    first_wolf = t.ev("first_wolf")
    assert first_wolf == min(wolves)
    rid = t.ask([first_wolf], "第 1 夜：请代表狼队提交今晚的刀口", [("none", "空刀"), *[(i, t.label(i)) for i in t.alive()]], default="none")
    t.answer(rid, first_wolf, victim)  # the same villager the guard protects

    # ---- N3: seer
    rid = t.ask([seer], "第 1 夜：请选择要查验的玩家", [("skip", "不查验"), *[(i, t.label(i)) for i in t.alive()]], default="skip")
    t.answer(rid, seer, white)
    checked = t.closed(rid)["answers"][0]["value"]
    side = "狼人阵营" if t.world.player(checked).faction == "wolves" else "好人阵营"
    t.mod("announce", channel_id=f"dm-{seer}", template_id="seer_result", params={"target": checked, "side": side})
    assert t.world.ledger[-1].visible_to_ids == [MODERATOR_ID, seer]
    assert "属于狼人阵营" in t.world.ledger[-1].data["content"]

    # ---- N4: witch (both potions available, the wolf target is known): poisons the hunter
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
    running = {plain_wolf, seer, villagers[1]}
    for pid in participants:
        t.answer(rid, pid, "yes" if pid in running else "no")
    answers = t.closed(rid)["answers"]
    candidates = [a["player_id"] for a in answers if a["value"] == "yes"]
    voters = [p for p in participants if p not in candidates]
    assert sorted(candidates) == sorted(running) and len(voters) == 8
    t.mod("announce", channel_id="public", template_id="sheriff_candidates", params={"players": candidates})
    t.mod("enable_ability", ability_id="white_wolf_explode")
    order = t.ev("order_of", members=candidates)
    assert order == sorted(candidates)
    t.mod("set_phase", text="第 1 天 · 警长竞选演说")
    t.mod("set_floor_queue", channel_id="public", order=order)
    t.mod("send_message", channel_id=f"dm-{white}", content="白天发言开始，你的“自爆”技能现已启用。")
    assert any(target == white and p.get("event") == "message.new" and p["_priority"] == "normal" for target, p in t.inbox)
    t.speak_in_turn("public", order, "竞选演说")
    (done,) = t.woken_by("floor.completed")
    assert [r["holder"] for r in done["results"]] == order
    t.mod("disable_ability", ability_id="white_wolf_explode")
    rid = t.ask(candidates, "是否退水？", [("stay", "不退水"), ("quit", "退水")], sealed=True, default="stay")
    for pid in candidates:
        t.answer(rid, pid, "quit" if pid == villagers[1] else "stay")
    remaining = [a["player_id"] for a in t.closed(rid)["answers"] if a["value"] == "stay"]
    assert sorted(remaining) == sorted([plain_wolf, seer])
    t.mod("send_message", channel_id="public", content=f"退水：{villagers[1]}；仍在竞选：{'、'.join(remaining)}")
    t.mod("announce", channel_id="public", template_id="sheriff_candidates", params={"players": remaining})
    rid = t.ask(voters, "警长投票：请在候选人中选一名。", [(i, t.label(i)) for i in remaining], sealed=True)
    for index, pid in enumerate(voters):
        t.answer(rid, pid, seer if index % 3 else plain_wolf)
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
    t.mod("set_phase", text="第 1 天 · 遗言")
    t.mod("set_floor_queue", channel_id="public", order=[hunter])
    t.speak_in_turn("public", [hunter], "遗言")
    assert len(t.woken_by("floor.completed")) == 1
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
    t.speak_in_turn("public", speech[:2], "发言")
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
    assert any("出局公告" in text and "（" in text for text in announcements)
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
    rid = t.ask(alive, "放逐投票", [("abstain", "弃票"), *[(i, t.label(i)) for i in alive]], sealed=True, default="abstain")
    for_hunter = [sheriff, others[1], others[2]]
    for_rival = [others[3], others[4], others[5]]
    for pid in alive:
        t.answer(rid, pid, hunter if pid in for_hunter else rival if pid in for_rival else "abstain", reason="我的理由，不公布")
    closed = t.closed(rid)
    assert closed["leaders"] == ["abstain"]  # 4 abstentions beat 3 + 3: the engine's count is not the verdict
    answers = [{"player_id": a["player_id"], "value": a["value"]} for a in closed["answers"]]
    result = t.ev("weighted_vote", answers=answers, privileged=[sheriff], weight=1.5)
    assert result == {"tally": {hunter: 3.5, rival: 3.0}, "leaders": [hunter]}  # type: ignore[comparison-overlap]
    assert not any("我的理由" in e.data.get("content", "") for e in t.world.ledger if e.type == "message")

    # ---- D6: a (forced) tie goes to PK speeches, then the others vote again
    tied = sorted([hunter, rival])
    t.mod("announce", channel_id="public", template_id="tie_pk", params={"players": tied})
    t.mod("enable_ability", ability_id="white_wolf_explode")
    order = t.ev("order_of", members=tied)
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
    rid = t.ask([hunter], "你出局了，可以亮出猎人身份开枪。", [("none", "不开枪"), *[(i, t.label(i)) for i in t.alive()]], default="none")
    t.answer(rid, hunter, sheriff)
    shot = t.closed(rid)["answers"][0]["value"]
    assert shot == sheriff
    t.mod("announce", channel_id="public", template_id="hunter_shot", params={"hunter": hunter, "target": shot})
    t.mod("announce", channel_id="public", template_id="death_notice", params={"players": [shot]})
    t.out(shot)
    heirs = t.alive()
    rid = t.ask([sheriff], "你是警长且已出局：请选择警徽的去向。", [("destroy", "撕毁警徽"), *[(i, t.label(i)) for i in heirs]], default="destroy")
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
    t.mod("announce", channel_id="public", template_id="game_over", params={"winner": "好人阵营", "summary": "所有狼人阵营成员出局。"})
    t.mod("end_game", winner="好人阵营", summary="所有狼人阵营成员出局。")
    assert t.world.state.status == "finished" and t.world.state.winner == "好人阵营" and not t.world.running


# ======================================================================================================
# prompts, config wiring and broken copies of the werewolf scenario
# ======================================================================================================

_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def test_prompt_templates_use_only_the_documented_variables() -> None:
    participant = (ROOT / "prompts" / "participant.md").read_text(encoding="utf-8")
    moderator = (ROOT / "prompts" / "moderator.md").read_text(encoding="utf-8")
    assert set(_VARIABLE.findall(participant)) == {"name", "seat", "id", "persona", "rules"}
    assert set(_VARIABLE.findall(moderator)) == {"rules", "manual"}
    assert participant.count("{{rules}}") == 1 and moderator.count("{{rules}}") == 1 and moderator.count("{{manual}}") == 1


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
    assert len(config["participants"]) == sum(item.count for item in spec.setup.identity_pool)
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
