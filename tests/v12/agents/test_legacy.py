"""``LegacyBrain`` is the v0.13.1 brain, byte for byte (v0.14 H49, H58; contract §13.5).

The snapshot ``legacy_baseline.json`` was made by ``legacy_cases.scenes(ArenaBrain)`` on the unchanged v0.13.1 code (see
``legacy_cases.py``).  ``LegacyBrain`` (v5 games, the benchmark baseline) and ``ArenaBrain`` (still the moderator's brain) must
both reproduce it: the system prompts, the tool schemas, the opening message, every increment, the nudges and the tool results.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from app.agents.brain import ArenaBrain
from app.agents.legacy import LEGACY_LABEL, LegacyBrain
from app.runtime.clock import ManualClock
from app.runtime.ports import WakeContext
from app.world.loader import load_scenario_v2
from app.world.types import MODERATOR_ID
from app.world.world import World

from .fakes import FakeWorld
from .legacy_cases import BASELINE_PATH, scenes

ROOT = Path(__file__).resolve().parents[3]
#: sha256 of the prompt files the baseline was made with (read as text: the line endings do not matter).  The player prompt of
#: ``LegacyBrain`` is ``prompts/participant.md`` and "it is not changed" (H49); the moderator's is ``prompts/moderator.md``.
PROMPT_HASHES = {
    "participant.md": "15b7aa4a3ce92e6ef9fa33ddfa027e35ef1e6c36e96f077c0b2fecbecd012cc1",
    "moderator.md": "39b1cb0b02f19afce1bfb1eb00f9390bfd98a723d8a1bd446d2c480a6ef9bc6e",
}

BASELINE: dict[str, Any] = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
KEYS = [(group, key) for group, scene in BASELINE.items() for key in scene]
CLASSES = [pytest.param(LegacyBrain, id="LegacyBrain"), pytest.param(ArenaBrain, id="ArenaBrain")]


@pytest.fixture(scope="module", params=CLASSES)
def played(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return scenes(request.param, tmp_path_factory.mktemp("docs"))


def _ordered(value: Any) -> str:
    """A text that keeps the key order: what the provider sees is the serialization, and a changed order is a changed prefix."""
    return json.dumps(value, ensure_ascii=False)


# ------------------------------------------------------------------------------------------------ the snapshot


def test_the_prompt_files_are_the_ones_the_baseline_was_made_with() -> None:
    for name, expected in PROMPT_HASHES.items():
        text = (ROOT / "prompts" / name).read_text(encoding="utf-8")
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == expected, (
            f"prompts/{name} changed: the v0.13.1 baseline (LegacyBrain, the moderator) must not"
        )


def test_the_scenes_cover_what_the_baseline_has(played: dict[str, Any]) -> None:
    assert set(played) == set(BASELINE)
    for group in BASELINE:
        assert set(played[group]) == set(BASELINE[group]), group


@pytest.mark.parametrize(("group", "key"), KEYS, ids=[f"{g}.{k}" for g, k in KEYS])
def test_every_scene_is_byte_for_byte_the_v0_13_1_one(played: dict[str, Any], group: str, key: str) -> None:
    assert played[group][key] == BASELINE[group][key]
    assert _ordered(played[group][key]) == _ordered(BASELINE[group][key])  # (keys in the same order too)


def test_the_baseline_has_the_three_texts_a_player_sees_first() -> None:
    """The snapshot is not empty: a system prompt, an opening message, an increment (and what a rebuilt conversation opens with)."""
    player = BASELINE["player"]
    system = BASELINE["prompts_and_tools"]["system_rich"]
    assert "寡言、谨慎" in system and "# 你的记忆与经验" in system and "你怎么行动" in system and "list_documents" in system
    assert "【状态板】" in player["start"][0] and "【可见历史】" in player["start"][0] and "【本次唤醒】" in player["start"][0]
    assert "【新动态】" in player["inject_changes"][0] and "【状态变化】" in player["inject_changes"][0]
    assert "旧事摘要" in player["start_rebuilt"][0]
    assert "list_documents" in BASELINE["prompts_and_tools"]["tool_names_player"]
    assert "jot" not in BASELINE["prompts_and_tools"]["tool_names_player"]


# ------------------------------------------------------------------------------------------------ what LegacyBrain is


def test_legacy_brain_is_the_arena_brain_with_the_same_constructor() -> None:
    assert issubclass(LegacyBrain, ArenaBrain) and LegacyBrain.label == LEGACY_LABEL == "v0.13.1"
    world = FakeWorld()
    kwargs: dict[str, Any] = {"personas": {"p1": "冷静"}, "memories": {"p1": "上局输了"}, "history_chars": {"player": 900}, "docs_dir": None}
    legacy, original = LegacyBrain(world, world.spec, {}, **kwargs), ArenaBrain(world, world.spec, {}, **kwargs)
    for agent in ("p1", "p2", MODERATOR_ID):
        assert legacy.system_prompt(agent) == original.system_prompt(agent)
        assert _ordered(legacy.tool_schemas(agent)) == _ordered(original.tool_schemas(agent))
    assert legacy.history_chars == original.history_chars == {"player": 900, "moderator": 20_000}


def test_legacy_brain_keeps_the_v0_13_1_tool_list_and_adds_no_trace_hooks() -> None:
    world = FakeWorld()
    brain = LegacyBrain(world, world.spec)
    names = [t["function"]["name"] for t in brain.tool_schemas("p1")]
    assert names == ["speak", "speak_template", "pass", "respond", "use_ability", "challenge_moderator", "sleep",
                     "list_documents", "read_document", "write_note", "read_notes", "compact_context"]
    # (the engine calls these hooks only on a brain that has them: a v5 game traces exactly what it traced in v0.13.1)
    assert not hasattr(brain, "mount_manifest") and not hasattr(brain, "hint_manifest")
    speak = next(t["function"] for t in brain.tool_schemas("p1") if t["function"]["name"] == "speak")
    assert speak["parameters"]["properties"]["content"]["maxLength"] == 20000  # the v0.13.1 schema, as it was
    assert "超出部分会被截掉" in speak["description"]


def test_legacy_brain_reads_the_persona_and_memory_texts_it_is_given_not_a_bot() -> None:
    world = FakeWorld()
    prompt = LegacyBrain(world, world.spec, personas={"p1": "寡言"}, memories={"p1": "## 印象\n- Blair：爱说"}).system_prompt("p1")
    assert "寡言" in prompt and "- Blair：爱说" in prompt and "你是 **p1 Alex**" in prompt


# ------------------------------------------------------------------------------------------------ with the real world


def _real_world() -> World:
    spec = load_scenario_v2(ROOT / "scenarios" / "minimal_test.md")
    players = [{"id": f"u{i}", "seat": str(i), "name": f"玩家{i}"} for i in range(1, 5)]
    return World.new_game(spec, players, ManualClock(), seed=7)


def _wake(agent: str) -> WakeContext:
    return WakeContext(agent_id=agent, kind="start", gen_id=1, reasons=[], after_seq=0)


def test_legacy_brain_still_works_on_the_real_world_whatever_the_board_gains() -> None:
    """The board of the real world may get new keys (``facts``, ``max_chars_per_message`` ...): the v0.13.1 rendering ignores them."""
    world = _real_world()
    brain = LegacyBrain(world, world.spec, personas={"u1": "话多"}, memories={"u1": "上局赢了"})
    system = brain.system_prompt("u1")
    assert "你是 **u1 玩家1**" in system and "话多" in system and "上局赢了" in system and world.spec.rules_text.strip() in system
    text, seen = brain.start_message("u1", _wake("u1"))
    assert seen == world.seq and text.startswith("【状态板】") and "你是 u1 玩家1" in text
    assert "硬事实" not in text and "facts" not in text  # a v5 game never shows what v0.14 added
    moderator = brain.start_message(MODERATOR_ID, _wake(MODERATOR_ID))[0]
    assert "你是主持人" in moderator
