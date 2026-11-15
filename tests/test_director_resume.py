from __future__ import annotations

import asyncio
import json
from typing import Any

from app.engine.director import ScenarioDirector
from app.engine.scenario import ScenarioDocument
from app.engine.state import Channel, GameSession, Participant


class _Harness:
    def __init__(
        self,
        session: GameSession,
        document: ScenarioDocument,
        *,
        pause_on_elimination: bool = False,
    ) -> None:
        self.session = session
        self.scenario = document
        self._pause_requested = False
        self._last_tool_data = None
        self.pause_on_elimination = pause_on_elimination
        self.tool_calls: list[tuple[str, dict[str, Any]]] = []

    async def broadcast_state(self, event: str) -> None:
        if self.pause_on_elimination and event == "scenario_event_queued":
            self._pause_requested = True
            self.session.status = "paused"

    def _log_tool(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def _execute_moderator_tool(self, call: Any) -> tuple[str, None]:
        args = call.parse_args()
        self.tool_calls.append((call.name, args))
        if call.name == "send_message":
            self.session.add_record(
                stream="channel",
                channel_id=str(args["channel_id"]),
                kind="message",
                actor_id="moderator",
                content=str(args["content"]),
            )
        return "sent", None


def _document(definition: dict[str, Any]) -> ScenarioDocument:
    return ScenarioDocument(
        path="memory.md",
        checksum="memory-checksum",
        definition=definition,
        body="",
    )


def _session(
    document: ScenarioDocument,
    *,
    participants: list[Participant] | None = None,
    channels: list[Channel] | None = None,
) -> GameSession:
    return GameSession(
        status="running",
        participants=participants or [],
        channels=channels or [],
        scenario_snapshot=document.snapshot(),
    )


def test_white_wolf_resume_dispatches_event_and_executes_goto() -> None:
    async def exercise() -> None:
        document = _document(
            {
                "schema_version": 1,
                "id": "white-wolf-resume",
                "version": "1",
                "entry": "speech_wave_loop",
                "workflow": [
                    {
                        "id": "speech_wave_loop",
                        "do": [
                            {
                                "for_each": {
                                    "items": "${alive(by_role('white_wolf'))}",
                                    "as": "actor",
                                    "max_iterations": 1,
                                    "do": [
                                        {
                                            "eliminate": {
                                                "player_id": "${actor.id}",
                                                "cause": "self_explode",
                                            }
                                        },
                                        {
                                            "dispatch_events": {
                                                "channel_id": "public",
                                                "template": "{{player.seat}} {{player.name}} 出局",
                                            }
                                        },
                                        {"goto": "night_start"},
                                    ],
                                }
                            }
                        ],
                        # A skipped loop must not accidentally satisfy the goto assertion.
                        "next": "unexpected_step",
                    },
                    {"id": "night_start", "do": [], "next": "night_start"},
                    {"id": "unexpected_step", "do": [], "next": "unexpected_step"},
                ],
            }
        )
        white_wolf = Participant(
            id="white",
            seat="SEAT 01",
            name="Alex",
            tags=["存活"],
            data={"alive": True, "role": "white_wolf", "faction": "wolves"},
        )
        session = _session(
            document,
            participants=[white_wolf],
            channels=[
                Channel(
                    id="public",
                    name="Public",
                    type="public",
                    members=["moderator", "white"],
                )
            ],
        )
        harness = _Harness(session, document, pause_on_elimination=True)
        director = ScenarioDirector(harness)

        await director.run_step()

        assert session.status == "paused"
        assert len(director.runtime["events"]) == 1
        assert director.runtime["completed_actions"] == ["do/0/item:white/0"]
        assert director.runtime["loop_snapshots"] == {
            "do/0": [{"__arena_participant__": "white"}]
        }

        # Verify the paused runtime, including loop snapshots, survives a real
        # model_dump -> JSON -> model_validate save/restore round trip.
        encoded = json.dumps(session.model_dump(), ensure_ascii=False)
        restored_session = GameSession.model_validate(json.loads(encoded))
        restored_session.status = "running"
        restored_harness = _Harness(restored_session, document)
        resumed = ScenarioDirector(restored_harness)

        await resumed.run_step()

        assert resumed.runtime["step_id"] == "night_start"
        assert resumed.runtime["events"] == []
        assert resumed.runtime["loop_snapshots"] == {}
        assert [
            event["cause"] for event in resumed.runtime["event_history"]
        ] == ["self_explode"]
        send_calls = [
            args
            for name, args in restored_harness.tool_calls
            if name == "send_message"
        ]
        assert len(send_calls) == 1
        assert send_calls[0] == {
            "channel_id": "public",
            "content": "SEAT 01 Alex 出局",
        }
        announcements = [
            record
            for record in restored_session.records
            if record.kind == "message" and record.channel_id == "public"
        ]
        assert len(announcements) == 1
        assert json.dumps(restored_session.model_dump(), ensure_ascii=False)

    asyncio.run(exercise())


def test_no_next_step_checks_termination_before_missing_successor_error() -> None:
    async def exercise() -> None:
        document = _document(
            {
                "schema_version": 1,
                "id": "terminal-step",
                "version": "1",
                "entry": "done",
                "workflow": [{"id": "done", "do": []}],
                "termination": [
                    {
                        "when": "${true}",
                        "winner": "finished",
                        "summary": "done",
                    }
                ],
            }
        )
        session = _session(document)
        harness = _Harness(session, document)
        director = ScenarioDirector(harness)

        await director.run_step()

        assert director.runtime["finished"] is True
        assert director.runtime["steps_executed"] == 1
        assert session.status == "finished"
        assert session.winner_faction == "finished"
        assert json.dumps(session.model_dump(), ensure_ascii=False)

    asyncio.run(exercise())


def test_while_resume_finishes_active_iteration_when_condition_becomes_false() -> None:
    async def exercise() -> None:
        document = _document(
            {
                "schema_version": 1,
                "id": "while-resume",
                "version": "1",
                "entry": "loop_step",
                "variables": {"keep_looping": True},
                "workflow": [
                    {
                        "id": "loop_step",
                        "do": [
                            {
                                "while": {
                                    "when": "${keep_looping}",
                                    "max_iterations": 3,
                                    "do": [
                                        {"set": {"keep_looping": False}},
                                        {
                                            "tool": {
                                                "name": "send_message",
                                                "args": {
                                                    "channel_id": "public",
                                                    "content": "after-resume",
                                                },
                                            }
                                        },
                                    ],
                                }
                            }
                        ],
                        "next": "after_loop",
                    },
                    {"id": "after_loop", "do": [], "next": "after_loop"},
                ],
            }
        )
        session = _session(
            document,
            channels=[
                Channel(
                    id="public",
                    name="Public",
                    type="public",
                    members=["moderator"],
                )
            ],
        )
        harness = _Harness(session, document)
        director = ScenarioDirector(harness)
        original_set = director._action_set

        def set_and_pause(specification: Any) -> None:
            original_set(specification)
            harness._pause_requested = True
            session.status = "paused"

        director._action_set = set_and_pause

        await director.run_step()

        assert director.variables["keep_looping"] is False
        assert director.runtime["active_loops"] == {"do/0": 1}
        assert director.runtime["completed_actions"] == [
            "do/0/iteration:1/0"
        ]
        assert harness.tool_calls == []

        encoded = json.dumps(session.model_dump(), ensure_ascii=False)
        restored_session = GameSession.model_validate(json.loads(encoded))
        restored_session.status = "running"
        restored_harness = _Harness(restored_session, document)
        resumed = ScenarioDirector(restored_harness)

        await resumed.run_step()

        assert resumed.runtime["step_id"] == "after_loop"
        assert resumed.runtime["active_loops"] == {}
        assert resumed.variables["keep_looping"] is False
        assert restored_harness.tool_calls == [
            (
                "send_message",
                {"channel_id": "public", "content": "after-resume"},
            )
        ]
        messages = [
            record
            for record in restored_session.records
            if record.kind == "message" and record.channel_id == "public"
        ]
        assert [record.content for record in messages] == ["after-resume"]

    asyncio.run(exercise())
