"""Director regressions: failing tools, missing channels, event announcements, loops."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.engine.director import ScenarioDirector, ScenarioToolError
from app.engine.orchestrator import GameOrchestrator
from app.engine.scenario import ScenarioDocument, ScenarioError
from app.engine.state import Channel, GameSession, Participant


class Harness:
    """Minimal orchestrator stand-in; ``fail_tools`` makes named tools report an error."""

    def __init__(self, session: GameSession, document: ScenarioDocument, fail_tools: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.session = session
        self.scenario = document
        self._pause_requested = False
        self._last_tool_data: Any = None
        self._last_tool_error: str | None = None
        self.fail_tools = set(fail_tools)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def broadcast_state(self, event: str) -> None:
        return None

    def _log_tool(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def _execute_moderator_tool(self, call: Any) -> tuple[str, None]:
        args = call.parse_args()
        self.calls.append((call.name, args))
        if call.name in self.fail_tools:
            self._last_tool_error = f"错误：{call.name} 失败"
            return self._last_tool_error, None
        if call.name == "send_message":
            self.session.add_record(
                stream="channel",
                channel_id=str(args["channel_id"]),
                kind="message",
                actor_id="moderator",
                content=str(args["content"]),
            )
        return "ok", None


def document(definition: dict[str, Any]) -> ScenarioDocument:
    return ScenarioDocument(path="m.md", checksum="c", definition=definition, body="")


def session_for(doc: ScenarioDocument, **kw: Any) -> GameSession:
    return GameSession(status="running", scenario_snapshot=doc.snapshot(), **kw)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def one_step(actions: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": "t",
        "version": "1",
        "entry": "a",
        "workflow": [{"id": "a", "do": actions, "next": "a"}],
        **extra,
    }


def test_failed_vote_tool_pauses_instead_of_becoming_nobody_voted() -> None:
    doc = document(one_step([{"vote": {"channel_id": "public", "voter_ids": ["p1"], "save_as": "r"}}]))
    harness = Harness(session_for(doc), doc, fail_tools={"request_players_vote"})
    director = ScenarioDirector(harness)
    with pytest.raises(ScenarioToolError, match="request_players_vote"):
        run(director.run_step())
    assert "r" not in director.variables  # no fabricated {"ballots": [], ...}


def test_vote_without_structured_data_is_an_error() -> None:
    doc = document(one_step([{"vote": {"channel_id": "public", "voter_ids": ["p1"]}}]))
    harness = Harness(session_for(doc), doc)  # tool "succeeds" but yields no data
    with pytest.raises(ScenarioToolError, match="no structured result"):
        run(ScenarioDirector(harness).run_step())


def test_tool_action_can_explicitly_tolerate_failure() -> None:
    doc = document(
        one_step(
            [
                {"tool": {"name": "send_message", "args": {"channel_id": "x", "content": "c"}, "on_error": "continue"}},
                {"set": {"after": True}},
            ]
        )
    )
    harness = Harness(session_for(doc), doc, fail_tools={"send_message"})
    director = ScenarioDirector(harness)
    run(director.run_step())
    assert director.variables["after"] is True

    doc2 = document(one_step([{"tool": {"name": "send_message", "args": {}, "on_error": "sometimes"}}]))
    with pytest.raises(ScenarioError, match="on_error"):
        run(ScenarioDirector(Harness(session_for(doc2), doc2)).run_step())


@pytest.mark.parametrize(
    "action",
    [
        {"vote": {"voter_ids": ["p1"]}},
        {"speak": {"player_id": "p1"}},
        {"interrupt": {"player_id": "p1"}},
        {"dispatch_events": {"empty_message": "quiet"}},
    ],
)
def test_missing_channel_id_is_rejected_not_stringified(action: dict[str, Any]) -> None:
    doc = document(one_step([action]))
    harness = Harness(
        session_for(doc, participants=[Participant(id="p1", seat="S1", name="A")]), doc
    )
    with pytest.raises(ScenarioError, match="channel_id"):
        run(ScenarioDirector(harness).run_step())
    assert not any("None" in str(args.get("channel_id")) for _, args in harness.calls)


def test_dispatch_template_with_none_channel_is_rejected() -> None:
    doc = document(
        one_step(
            [
                {"eliminate": {"player_id": "p1", "cause": "x"}},
                {"dispatch_events": {"template": "{{player.name}} out"}},
            ]
        )
    )
    harness = Harness(
        session_for(
            doc,
            participants=[Participant(id="p1", seat="S1", name="A", data={"alive": True})],
        ),
        doc,
    )
    with pytest.raises(ScenarioError, match="channel_id"):
        run(ScenarioDirector(harness).run_step())
    assert harness.calls == []


def test_for_each_duplicate_items_are_each_executed() -> None:
    doc = document(
        one_step(
            [
                {
                    "for_each": {
                        "items": "${['a', 'a', 'b', 'a']}",
                        "as": "x",
                        "do": [{"set": {"n": "${n + 1}"}}],
                    }
                }
            ],
            variables={"n": 0},
        )
    )
    director = ScenarioDirector(Harness(session_for(doc), doc))
    run(director.run_step())
    assert director.variables["n"] == 4  # previously duplicates were skipped as "done"


def test_goto_from_a_hook_does_not_reannounce_the_event_in_the_next_step() -> None:
    announce = {"dispatch_events": {"channel_id": "public", "template": "{{player.name}} out"}}
    doc = document(
        {
            "schema_version": 1,
            "id": "t",
            "version": "1",
            "entry": "a",
            "workflow": [
                {"id": "a", "do": [{"eliminate": {"player_id": "p1", "cause": "boom"}}, announce], "next": "b"},
                {"id": "b", "do": [announce], "next": "b"},
            ],
            "hooks": {"eliminated": [{"do": [{"goto": "b"}]}]},
        }
    )
    session = session_for(
        doc,
        participants=[Participant(id="p1", seat="S1", name="Alice", data={"alive": True})],
        channels=[Channel(id="public", name="P", type="public", members=["moderator", "p1"])],
    )
    harness = Harness(session, doc)
    director = ScenarioDirector(harness)

    run(director.run_step())  # step a: announces, hook jumps to b
    assert director.runtime["step_id"] == "b"
    run(director.run_step())  # step b dispatches again: nothing left to announce

    sends = [args for name, args in harness.calls if name == "send_message"]
    assert [a["content"] for a in sends] == ["Alice out"]
    assert director.runtime["events"] == []
    assert [e["player_id"] for e in director.runtime["event_history"]] == ["p1"]


def test_real_orchestrator_reports_vote_on_missing_channel_as_scenario_error() -> None:
    orch = GameOrchestrator()
    doc = document(one_step([{"vote": {"channel_id": "nowhere", "voter_ids": ["p01"]}}]))
    orch.session = session_for(doc, participants=[Participant(id="p01", seat="S1", name="A")])
    orch._subscribers = set()
    director = ScenarioDirector(orch)
    with pytest.raises(ScenarioToolError, match="nowhere"):
        run(director.run_step())
    assert json.dumps(orch.session.model_dump())  # still serializable


def test_scenario_loop_turns_a_tool_failure_into_a_clear_pause() -> None:
    orch = GameOrchestrator()
    doc = document(one_step([{"vote": {"channel_id": "nowhere", "voter_ids": ["p01"]}}]))
    orch.session = session_for(doc, participants=[Participant(id="p01", seat="S1", name="A")])
    orch._subscribers = set()

    async def go() -> None:
        await orch._run_scenario_loop()

    run(go())
    assert orch.session.status == "paused"
    assert "nowhere" in orch.session.active_status_text
