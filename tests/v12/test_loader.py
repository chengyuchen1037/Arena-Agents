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


# ======================================================================================================
# werewolf_12p (schema v2): structure, manual references, checks, templates, prompts
# ======================================================================================================

import ast  # noqa: E402
import re  # noqa: E402

import yaml  # noqa: E402

from app.agents.brain import ArenaBrain  # noqa: E402
from app.agents.schemas import (  # noqa: E402
    MODERATOR_TOOL_INTENTS,
    PLAYER_TOOL_INTENTS,
    SHARED_TOOL_NAMES,
    WAIT_TOOL,
    shared_tool_schemas,
)
from app.world.intents import MODERATOR_INTENTS, PLAYER_INTENTS  # noqa: E402
from app.world.spec import ParamSpec, ScenarioSpec  # noqa: E402
from app.world.types import MODERATOR_ID  # noqa: E402

from .agents.fakes import FakeWorld  # noqa: E402

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
    assert d.request.duration_s == 90 and d.llm.max_tokens == 2048
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
