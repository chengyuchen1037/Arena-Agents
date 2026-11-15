from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.engine.context import _build_isolated_history_text
from app.engine.history import normalize_tool_history
from app.engine.llm import LLMResponse, ToolCallData
from app.engine.orchestrator import GameOrchestrator
from app.engine.state import Channel, FeedRecord, GameSession, Participant


ROOT_DIR = Path(__file__).resolve().parents[1]
SAVES_DIR = ROOT_DIR / "saves"
TARGET_INTERRUPTED_CALL = "call_03_hI7wVpHyctqDJE8EqyWK1483"

# Avoid depending on production prompt/knowledge contents in these engine tests.
TEST_CFG = {
    "prompts": {
        "moderator_system": "__test_missing_moderator_prompt__.md",
        "participant_system": "__test_missing_participant_prompt__.md",
    },
    # Absolute path outside the repository: never created, never polluting the repo root.
    "knowledge_base": {
        "docs_dir": str(Path(tempfile.gettempdir()) / "arena_test_missing_knowledge")
    },
}


class SequenceLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def chat_stream(self, *args: Any, **kwargs: Any) -> LLMResponse:
        self.calls.append((args, kwargs))
        if not self._responses:
            raise AssertionError("fake LLM received more calls than expected")
        return self._responses.pop(0)


def make_orchestrator(session: GameSession, llm: SequenceLLM) -> GameOrchestrator:
    orchestrator = GameOrchestrator.__new__(GameOrchestrator)
    orchestrator.cfg = TEST_CFG
    orchestrator.session = session
    orchestrator.llm = llm
    orchestrator._subscribers = set()
    orchestrator._pause_requested = False
    orchestrator._last_stream_broadcast = 0.0
    return orchestrator


def make_tool_calls() -> list[ToolCallData]:
    return [
        ToolCallData(id=f"call_{index}", name=f"tool_{index}", arguments_json="{}")
        for index in range(1, 4)
    ]


def response_ids(messages: list[dict[str, Any]], call_id: str) -> list[dict[str, Any]]:
    return [
        message
        for message in messages
        if message.get("role") == "tool"
        and message.get("tool_call_id") == call_id
    ]


class RealSaveHistoryRegressionTests(unittest.TestCase):
    def test_real_failed_save_gets_exactly_one_missing_tool_response(self) -> None:
        paths = sorted(SAVES_DIR.glob("*0eff03efe1dc.json"))
        if not paths:
            self.skipTest("no saved snapshot for session 0eff03efe1dc")

        payload = json.loads(paths[-1].read_text(encoding="utf-8"))
        original_messages = payload["moderator_messages"]
        repaired, _ = normalize_tool_history(original_messages)

        matches = response_ids(repaired, TARGET_INTERRUPTED_CALL)
        self.assertEqual(
            len(matches),
            1,
            f"{TARGET_INTERRUPTED_CALL} must have exactly one tool response",
        )
        self.assertEqual(matches[0]["role"], "tool")


class ChannelHistoryIsolationTests(unittest.TestCase):
    def test_new_member_cannot_read_prejoin_id_or_legacy_snapshots(self) -> None:
        session = GameSession(
            participants=[
                Participant(id="p01", seat="S1", name="Alice"),
                Participant(id="p02", seat="S2", name="Bob"),
                Participant(id="p03", seat="S3", name="Newcomer"),
            ]
        )

        def record(
            record_id: str,
            content: str,
            *,
            visible_to_ids: list[str] | None = None,
            visible_to: list[str] | None = None,
        ) -> FeedRecord:
            return FeedRecord(
                id=record_id,
                timestamp="12:00:00",
                stream="channel",
                channel_id="private",
                kind="message",
                actor_id="p01",
                actor_name="Alice",
                content=content,
                visible_to_ids=visible_to_ids or [],
                visible_to=visible_to or [],
            )

        session.records = [
            record(
                "rec_0001",
                "IDS_BEFORE_JOIN_SECRET",
                visible_to_ids=["p01", "p02"],
                visible_to=["Alice", "Bob"],
            ),
            record(
                "rec_0002",
                "LEGACY_BEFORE_JOIN_SECRET",
                visible_to=["Alice", "Bob"],
            ),
            record(
                "rec_0003",
                "IDS_AFTER_JOIN_VISIBLE",
                visible_to_ids=["p01", "p02", "p03"],
                visible_to=["Alice", "Bob", "Newcomer"],
            ),
            record(
                "rec_0004",
                "LEGACY_AFTER_JOIN_VISIBLE",
                visible_to=["Alice", "Bob", "Newcomer"],
            ),
        ]
        # Current membership includes p03 only after the first two snapshots.
        session.channels = [
            Channel(
                id="private",
                name="Private",
                type="dm",
                members=["p01", "p02", "p03"],
            )
        ]

        channels, newcomer_history = _build_isolated_history_text(session, "p03")

        self.assertIn("private", channels)
        self.assertNotIn("IDS_BEFORE_JOIN_SECRET", newcomer_history)
        self.assertNotIn("LEGACY_BEFORE_JOIN_SECRET", newcomer_history)
        self.assertIn("IDS_AFTER_JOIN_VISIBLE", newcomer_history)
        self.assertIn("LEGACY_AFTER_JOIN_VISIBLE", newcomer_history)

        _, original_member_history = _build_isolated_history_text(session, "p01")
        for expected in (
            "IDS_BEFORE_JOIN_SECRET",
            "LEGACY_BEFORE_JOIN_SECRET",
            "IDS_AFTER_JOIN_VISIBLE",
            "LEGACY_AFTER_JOIN_VISIBLE",
        ):
            self.assertIn(expected, original_member_history)


class OrchestratorRegressionTests(unittest.IsolatedAsyncioTestCase):
    def make_failed_moderator_step(self, failure: BaseException) -> GameOrchestrator:
        session = GameSession(status="running")
        session.moderator_messages = [{"role": "user", "content": "kickoff"}]
        llm = SequenceLLM([LLMResponse(tool_calls=make_tool_calls())])
        orchestrator = make_orchestrator(session, llm)

        async def execute_tool(tc: ToolCallData) -> tuple[str, None]:
            if tc.id == "call_2":
                raise failure
            return f"completed:{tc.id}", None

        orchestrator._execute_moderator_tool = execute_tool  # type: ignore[method-assign]
        return orchestrator

    def assert_complete_moderator_history(self, orchestrator: GameOrchestrator) -> None:
        messages = orchestrator.session.moderator_messages
        for call_id in ("call_1", "call_2", "call_3"):
            self.assertEqual(len(response_ids(messages, call_id)), 1)
        self.assertEqual(
            response_ids(messages, "call_1")[0]["content"],
            "completed:call_1",
        )
        normalized, report = normalize_tool_history(messages)
        self.assertEqual(normalized, messages)
        self.assertFalse(any(report.values()), report)

    async def test_moderator_tool_exception_completes_current_and_remaining(self) -> None:
        orchestrator = self.make_failed_moderator_step(RuntimeError("tool boom"))

        with self.assertRaisesRegex(RuntimeError, "tool boom"):
            await orchestrator._moderator_step()

        self.assert_complete_moderator_history(orchestrator)
        messages = orchestrator.session.moderator_messages
        self.assertIn("RuntimeError: tool boom", response_ids(messages, "call_2")[0]["content"])
        self.assertIn("工具执行异常", response_ids(messages, "call_3")[0]["content"])

    async def test_moderator_tool_cancellation_completes_current_and_remaining(self) -> None:
        orchestrator = self.make_failed_moderator_step(asyncio.CancelledError())

        with self.assertRaises(asyncio.CancelledError):
            await orchestrator._moderator_step()

        self.assert_complete_moderator_history(orchestrator)
        messages = orchestrator.session.moderator_messages
        for call_id in ("call_2", "call_3"):
            self.assertIn("工具执行已取消", response_ids(messages, call_id)[0]["content"])

    async def test_vote_rejects_noncandidate_and_no_tool_is_not_abstain(self) -> None:
        session = GameSession(
            status="running",
            participants=[
                Participant(id="p01", seat="S1", name="Voter"),
                Participant(id="p02", seat="S2", name="Candidate Two"),
                Participant(id="p03", seat="S3", name="Candidate Three"),
            ],
            channels=[
                Channel(
                    id="public",
                    name="Public",
                    type="public",
                    members=["p01", "p02", "p03"],
                )
            ],
        )
        invalid_vote = LLMResponse(
            tool_calls=[
                ToolCallData(
                    id="invalid_vote",
                    name="cast_vote",
                    arguments_json=json.dumps(
                        {"target_id": "p01", "reason": "not a candidate"}
                    ),
                )
            ]
        )
        llm = SequenceLLM(
            [
                invalid_vote,
                LLMResponse(content="text instead of a tool"),
                LLMResponse(content="still no tool"),
            ]
        )
        orchestrator = make_orchestrator(session, llm)

        result, interrupt = await orchestrator._handle_request_players_vote(
            {
                "channel_id": "public",
                "voter_ids": ["p01"],
                "candidate_ids": ["p02", "p03"],
                "allow_abstain": False,
                "vote_topic": "focused regression vote",
            }
        )

        self.assertIsNone(interrupt)
        self.assertEqual(len(llm.calls), 3)
        invalid_results = [
            record
            for record in session.records
            if record.kind == "tool_call" and record.tool_name == "cast_vote"
        ]
        self.assertEqual(len(invalid_results), 1)
        tool_result = json.loads(invalid_results[0].tool_result or "{}")
        self.assertFalse(tool_result["ok"])
        self.assertEqual(tool_result["error"], "目标不在本轮候选人列表中")

        self.assertEqual(orchestrator._last_tool_data, {"ballots": [], "tally": {}})
        self.assertIn("未提交有效选票", result)
        self.assertNotIn("弃票 (abstain)", result)
        vote_messages = [
            record
            for record in session.records
            if record.kind == "message" and record.action_type == "vote"
        ]
        self.assertEqual(len(vote_messages), 1)
        self.assertEqual(vote_messages[0].content, "⚠️ 未提交有效选票")
        self.assertNotIn("选择弃票", vote_messages[0].content)


if __name__ == "__main__":
    unittest.main()
