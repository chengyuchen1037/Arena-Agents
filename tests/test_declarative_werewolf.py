from __future__ import annotations

import asyncio
import copy
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.engine.context import _build_public_seat_board_text
from app.engine.director import ScenarioDirector
from app.engine.orchestrator import GameOrchestrator
from app.engine.scenario import load_scenario


ROOT_DIR = Path(__file__).resolve().parents[1]
SCENARIO_PATH = ROOT_DIR / "scenarios" / "werewolf_12p.md"
ROLE_SEQUENCE = [
    "werewolf",
    "werewolf",
    "werewolf",
    "white_wolf",
    "villager",
    "villager",
    "villager",
    "villager",
    "seer",
    "witch",
    "hunter",
    "guard",
]
EXPECTED_ROLE_COUNTS = {
    "werewolf": 3,
    "white_wolf": 1,
    "villager": 4,
    "seer": 1,
    "witch": 1,
    "hunter": 1,
    "guard": 1,
}


def make_harness(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Build real scenario/orchestrator state with deterministic offline interactions."""
    document = load_scenario(SCENARIO_PATH)
    orchestrator = GameOrchestrator()
    orchestrator.scenario = document
    orchestrator.cfg["_scenario_snapshot"] = document.snapshot()
    orchestrator.session.scenario_snapshot = document.snapshot()
    orchestrator.session.scenario_name = document.scenario_id
    orchestrator.session.status = "running"
    orchestrator._pause_requested = False
    orchestrator._subscribers = set()

    session = orchestrator.session
    assert len(session.participants) == 12
    for participant, role_name in zip(session.participants, ROLE_SEQUENCE, strict=True):
        session.initial_assignments[participant.id] = {
            "role": role_name,
            "faction": document.definition["roles"][role_name]["faction"],
        }

    calls: dict[str, list[Any]] = {
        "choice": [],
        "speak": [],
        "vote": [],
        "plan": [],
        "interrupt": [],
    }
    control: dict[str, Any] = {
        "choice_handler": lambda request: request["options"][0]["id"]
    }

    async def fake_choices(orch: GameOrchestrator, args: dict[str, Any]):
        decisions: dict[str, Any] = {}
        details: list[dict[str, Any]] = []
        for original in args.get("requests") or []:
            request = copy.deepcopy(original)
            calls["choice"].append(request)
            selected = control["choice_handler"](copy.deepcopy(request))
            valid_ids = {option["id"] for option in request["options"]}
            if selected is not None and selected not in valid_ids:
                raise AssertionError(
                    f"fake choice {selected!r} is outside {sorted(valid_ids)!r}"
                )
            decisions[request["player_id"]] = selected
            details.append(
                {
                    "player_id": request["player_id"],
                    "option_id": selected,
                    "submitted": selected is not None,
                }
            )
        data = {"decisions": decisions, "details": details}
        orch._last_tool_data = data
        return json.dumps(data, ensure_ascii=False), data

    async def fake_speak(
        player_id: str, channel_id: str, instruction: str
    ) -> tuple[str, None]:
        calls["speak"].append(
            {
                "player_id": player_id,
                "channel_id": channel_id,
                "instruction": instruction,
            }
        )
        return f"fake speech by {player_id}", None

    async def fake_vote(args: dict[str, Any]) -> tuple[str, None]:
        calls["vote"].append(copy.deepcopy(args))
        candidates = list(args.get("candidate_ids") or [])
        fallback = candidates[0] if candidates else (
            "abstain" if args.get("allow_abstain", True) else ""
        )
        ballots = [
            {"voter_id": voter, "target_id": fallback, "reason": "fake"}
            for voter in args.get("voter_ids") or []
            if fallback
        ]
        tally: dict[str, int] = {}
        for ballot in ballots:
            target = ballot["target_id"]
            tally[target] = tally.get(target, 0) + 1
        orchestrator._last_tool_data = {"ballots": ballots, "tally": tally}
        return "fake structured vote", None

    async def fake_plan(args: dict[str, Any]) -> tuple[str, None]:
        calls["plan"].append(copy.deepcopy(args))
        orchestrator._last_tool_data = {
            "plans": [
                {"player_id": player_id, "plan_summary": "fake", "current_focus": "fake"}
                for player_id in args.get("player_ids") or []
            ]
        }
        return "fake strategic planning", None

    async def fake_interrupt(args: dict[str, Any]) -> tuple[str, None]:
        calls["interrupt"].append(copy.deepcopy(args))
        candidates = list(args.get("candidate_ids") or [])
        orchestrator._last_tool_data = {
            "triggered": True,
            "player_id": args.get("player_id", ""),
            "target_id": candidates[0] if candidates else "",
            "action_text": "fake interrupt",
        }
        return "fake structured interrupt", None

    monkeypatch.setattr(
        "app.engine.orchestrator.execute_choice_requests", fake_choices
    )
    monkeypatch.setattr(orchestrator, "_run_participant_speak", fake_speak)
    monkeypatch.setattr(orchestrator, "_handle_request_players_vote", fake_vote)
    monkeypatch.setattr(
        orchestrator, "_handle_request_players_strategic_planning", fake_plan
    )
    monkeypatch.setattr(orchestrator, "_handle_request_player_interrupt", fake_interrupt)

    return SimpleNamespace(
        document=document,
        orchestrator=orchestrator,
        session=session,
        director=ScenarioDirector(orchestrator),
        calls=calls,
        control=control,
    )


async def advance(harness: SimpleNamespace, steps: int = 1) -> None:
    for _ in range(steps):
        await harness.director.run_step()


def jump_to(harness: SimpleNamespace, step_id: str) -> None:
    """Jump without discarding pending events, as a focused test setup."""
    harness.director.runtime["step_id"] = step_id
    harness.director.runtime["completed_actions"] = []
    harness.director.runtime["control_decisions"] = {}
    harness.director.runtime["loop_snapshots"] = {}
    harness.director.runtime["active_loops"] = {}
    harness.orchestrator._pause_requested = False
    harness.session.status = "running"


def player_with_role(harness: SimpleNamespace, role: str):
    return next(
        participant
        for participant in harness.session.participants
        if participant.data.get("role") == role
    )


def force_out_for_fixture(participant: Any) -> None:
    participant.data["alive"] = False
    participant.tags = [tag for tag in participant.tags if tag != "存活"]
    if "出局" not in participant.tags:
        participant.tags.append("出局")


def test_setup_channels_dms_and_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise() -> None:
        harness = make_harness(monkeypatch)

        await advance(harness, 3)

        assert harness.director.runtime["step_id"] == "opening_plan"
        assert len(harness.session.participants) == 12
        assert Counter(
            participant.data["role"]
            for participant in harness.session.participants
        ) == Counter(EXPECTED_ROLE_COUNTS)
        assert all(
            participant.data["alive"] is True
            for participant in harness.session.participants
        )

        expected_player_ids = {
            participant.id for participant in harness.session.participants
        }
        public = harness.session.get_channel("public")
        wolves = harness.session.get_channel("wolf-team")
        assert public is not None and set(public.members) == {
            "moderator",
            *expected_player_ids,
        }
        expected_wolves = {
            participant.id
            for participant in harness.session.participants
            if participant.data["faction"] == "wolves"
        }
        assert wolves is not None and set(wolves.members) == {
            "moderator",
            *expected_wolves,
        }
        assert len(harness.session.channels) == 14
        for participant in harness.session.participants:
            dm = harness.session.get_channel(f"dm-{participant.id}")
            assert dm is not None
            assert dm.type == "dm"
            assert set(dm.members) == {"moderator", participant.id}

        factions = {
            item.name: (item.alive, item.total)
            for item in harness.session.dashboard.factions
        }
        assert factions == {
            "狼人阵营": (4, 4),
            "神职": (4, 4),
            "平民阵营": (4, 4),
        }
        tag_stats = {
            item.name: item.count
            for item in harness.session.dashboard.tag_stats
        }
        assert tag_stats == {"存活": 12, "出局": 0, "警长": 0}
        assert harness.calls["plan"] == []

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("guard_protected", "witch_saved", "expected_alive"),
    [
        (False, False, False),
        (True, False, True),
        (False, True, True),
        (True, True, False),
    ],
)
def test_night_resolve_protection_matrix(
    monkeypatch: pytest.MonkeyPatch,
    guard_protected: bool,
    witch_saved: bool,
    expected_alive: bool,
) -> None:
    async def exercise() -> None:
        harness = make_harness(monkeypatch)
        await advance(harness)
        target = player_with_role(harness, "villager")
        wolf = player_with_role(harness, "werewolf")
        jump_to(harness, "night_resolve")
        harness.director.variables.update(
            {
                "day": 2,
                "wolf_target": target.id,
                "wolf_actor_id": wolf.id,
                "guard_target": target.id if guard_protected else "none",
                "witch_save_target": target.id if witch_saved else "none",
                "witch_poison_target": "none",
            }
        )

        await harness.director.run_step()

        assert target.data["alive"] is expected_alive
        events = harness.director.runtime["events"]
        assert len(events) == (0 if expected_alive else 1)
        if expected_alive:
            assert "存活" in target.tags and "出局" not in target.tags
        else:
            assert "出局" in target.tags and "存活" not in target.tags
            assert events[0]["player_id"] == target.id
            assert events[0]["cause"] == "wolf_attack"
            assert events[0]["day"] == 2
        assert harness.director.runtime["step_id"] == "after_night"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("cause", "event_day", "expected_last_words"),
    [
        ("wolf_attack", 1, 1),
        ("wolf_attack", 2, 0),
        ("witch_poison", 1, 1),
        ("witch_poison", 2, 0),
    ],
)
def test_night_death_last_words_matrix(
    monkeypatch: pytest.MonkeyPatch,
    cause: str,
    event_day: int,
    expected_last_words: int,
) -> None:
    async def exercise() -> None:
        harness = make_harness(monkeypatch)
        await advance(harness, 2)
        target = player_with_role(harness, "villager")
        harness.director.variables["day"] = event_day
        await harness.director._action_eliminate(
            {"player_id": target.id, "cause": cause}
        )
        jump_to(harness, "post_exile")

        await harness.director.run_step()

        assert len(harness.calls["speak"]) == expected_last_words
        if expected_last_words:
            assert harness.calls["speak"][0]["player_id"] == target.id
        assert harness.director.runtime["events"] == []
        assert len(harness.director.runtime["event_history"]) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("event_day", [1, 2])
def test_poisoned_hunter_never_gets_shot_choice(
    monkeypatch: pytest.MonkeyPatch, event_day: int
) -> None:
    async def exercise() -> None:
        harness = make_harness(monkeypatch)
        await advance(harness, 2)
        hunter = player_with_role(harness, "hunter")
        harness.director.variables["day"] = event_day
        await harness.director._action_eliminate(
            {"player_id": hunter.id, "cause": "witch_poison"}
        )
        jump_to(harness, "post_exile")

        await harness.director.run_step()

        assert harness.calls["choice"] == []
        assert len(harness.calls["speak"]) == (1 if event_day == 1 else 0)

    asyncio.run(exercise())


def test_termination_waits_for_event_queue_and_skips_dusk_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        harness = make_harness(monkeypatch)
        await advance(harness, 2)
        wolves = [
            participant
            for participant in harness.session.participants
            if participant.data["faction"] == "wolves"
        ]
        final_wolf = wolves[0]
        for wolf in wolves[1:]:
            force_out_for_fixture(wolf)
        jump_to(harness, "night_resolve")
        harness.director.variables.update(
            {
                "day": 2,
                "wolf_target": final_wolf.id,
                "wolf_actor_id": final_wolf.id,
                "guard_target": "none",
                "witch_save_target": "none",
                "witch_poison_target": "none",
            }
        )

        await harness.director.run_step()

        assert final_wolf.data["alive"] is False
        assert len(harness.director.runtime["events"]) == 1
        assert harness.director.runtime["finished"] is False
        assert harness.session.status == "running"
        assert harness.session.winner_faction is None

        jump_to(harness, "post_exile")
        await harness.director.run()

        assert harness.director.runtime["events"] == []
        assert len(harness.director.runtime["event_history"]) == 1
        assert harness.director.runtime["finished"] is True
        assert harness.session.status == "finished"
        assert harness.session.winner_faction == "好人阵营"
        assert harness.director.runtime["step_id"] == "dusk_plan"
        assert harness.calls["plan"] == []

    asyncio.run(exercise())


def test_resume_does_not_repeat_completed_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        harness = make_harness(monkeypatch)
        real_execute = harness.orchestrator._execute_moderator_tool
        phase_calls: list[str] = []

        async def pause_after_phase(tool_call: Any):
            result = await real_execute(tool_call)
            if tool_call.name == "set_phase":
                phase_calls.append(tool_call.id)
                harness.orchestrator._pause_requested = True
                harness.session.status = "paused"
            return result

        monkeypatch.setattr(
            harness.orchestrator, "_execute_moderator_tool", pause_after_phase
        )

        await harness.director.run_step()

        assert len(phase_calls) == 1
        assert harness.director.runtime["step_id"] == "setup"
        assert harness.director.runtime["completed_actions"] == ["do/0"]
        assert all(
            not participant.data for participant in harness.session.participants
        )

        harness.orchestrator._pause_requested = False
        harness.session.status = "running"
        harness.director = ScenarioDirector(harness.orchestrator)
        await harness.director.run_step()

        assert len(phase_calls) == 1
        assert harness.director.runtime["step_id"] == "setup_channels"
        assert Counter(
            participant.data["role"]
            for participant in harness.session.participants
        ) == Counter(EXPECTED_ROLE_COUNTS)

    asyncio.run(exercise())


def test_resume_dispatch_preserves_event_without_duplicate_announcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        harness = make_harness(monkeypatch)
        await advance(harness, 2)
        target = player_with_role(harness, "villager")
        harness.director.variables["day"] = 2
        await harness.director._action_eliminate(
            {"player_id": target.id, "cause": "white_wolf"}
        )
        event_id = harness.director.runtime["events"][0]["id"]
        jump_to(harness, "post_exile")

        real_execute = harness.orchestrator._execute_moderator_tool
        announcements: list[str] = []

        async def pause_after_announcement(tool_call: Any):
            result = await real_execute(tool_call)
            args = tool_call.parse_args()
            content = str(args.get("content", ""))
            if tool_call.name == "send_message" and "出局公告" in content:
                announcements.append(content)
                harness.orchestrator._pause_requested = True
                harness.session.status = "paused"
            return result

        monkeypatch.setattr(
            harness.orchestrator,
            "_execute_moderator_tool",
            pause_after_announcement,
        )

        await harness.director.run_step()

        assert len(announcements) == 1
        assert len(harness.director.runtime["events"]) == 1
        assert harness.director.runtime["events"][0]["id"] == event_id
        assert harness.director.runtime["event_history"] == []
        assert any(
            key.endswith("/announcement")
            for key in harness.director.runtime["completed_actions"]
        )

        harness.orchestrator._pause_requested = False
        harness.session.status = "running"
        harness.director = ScenarioDirector(harness.orchestrator)
        await harness.director.run_step()

        assert len(announcements) == 1
        assert harness.director.runtime["events"] == []
        assert [
            event["id"] for event in harness.director.runtime["event_history"]
        ] == [event_id]
        assert harness.director.runtime["step_id"] == "dusk_plan"
        assert harness.session.status == "running"

    asyncio.run(exercise())


def test_resume_empty_announcement_is_not_repeated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        harness = make_harness(monkeypatch)
        await advance(harness, 2)
        harness.director.variables["day"] = 1
        jump_to(harness, "dawn_announce")
        assert harness.director.runtime["events"] == []

        real_execute = harness.orchestrator._execute_moderator_tool
        announcements: list[str] = []

        async def pause_after_first_empty_announcement(tool_call: Any):
            result = await real_execute(tool_call)
            args = tool_call.parse_args()
            content = str(args.get("content", ""))
            if tool_call.name == "send_message" and "平安夜" in content:
                announcements.append(content)
                if len(announcements) == 1:
                    harness.orchestrator._pause_requested = True
                    harness.session.status = "paused"
            return result

        monkeypatch.setattr(
            harness.orchestrator,
            "_execute_moderator_tool",
            pause_after_first_empty_announcement,
        )

        await harness.director.run_step()

        assert len(announcements) == 1
        assert harness.director.runtime["step_id"] == "dawn_announce"
        assert any(
            key.endswith("/empty_announcement")
            for key in harness.director.runtime["completed_actions"]
        )

        harness.orchestrator._pause_requested = False
        harness.session.status = "running"
        harness.director = ScenarioDirector(harness.orchestrator)
        await harness.director.run_step()

        assert len(announcements) == 1
        assert harness.director.runtime["events"] == []
        assert harness.director.runtime["step_id"] == "day_direction"
        assert harness.session.status == "running"

    asyncio.run(exercise())


def test_pending_elimination_uses_previous_tags_until_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        harness = make_harness(monkeypatch)
        await advance(harness, 2)
        target = player_with_role(harness, "villager")
        viewer = player_with_role(harness, "seer")
        harness.director.variables["day"] = 2

        await harness.director._action_eliminate(
            {"player_id": target.id, "cause": "white_wolf"}
        )

        assert target.data["alive"] is False
        assert "出局" in target.tags
        event = harness.director.runtime["events"][0]
        assert event["previous_tags"] == ["平民", "存活"]
        before_dispatch = _build_public_seat_board_text(
            harness.session, viewer.id
        )
        target_line_before = next(
            line
            for line in before_dispatch.splitlines()
            if f"({target.id})" in line
        )
        assert "公开状态: [存活]" in target_line_before
        assert "出局" not in target_line_before

        jump_to(harness, "post_exile")
        await harness.director.run_step()

        after_dispatch = _build_public_seat_board_text(
            harness.session, viewer.id
        )
        target_line_after = next(
            line
            for line in after_dispatch.splitlines()
            if f"({target.id})" in line
        )
        assert "公开状态: [出局]" in target_line_after
        assert "存活" not in target_line_after
        assert harness.director.runtime["events"] == []
        assert len(harness.director.runtime["event_history"]) == 1

    asyncio.run(exercise())
