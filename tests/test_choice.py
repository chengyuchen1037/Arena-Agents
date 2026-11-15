from __future__ import annotations

import unittest
from typing import Any

from app.engine.choice import execute_choice_requests
from app.engine.llm import LLMResponse, ToolCallData
from app.engine.orchestrator import GameOrchestrator


class _FakeLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.calls = 0

    async def chat_stream(self, *_args: Any, **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


class StructuredChoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.orchestrator = GameOrchestrator()
        self.orchestrator.session.execution_depth = "low"
        self.orchestrator._subscribers = set()

    async def test_text_only_is_not_accepted_as_submission(self) -> None:
        fake = _FakeLLM([LLMResponse(content="yes")])
        self.orchestrator.llm = fake

        _, data = await execute_choice_requests(
            self.orchestrator,
            {
                "requests": [
                    {
                        "player_id": "p01",
                        "question": "choose",
                        "options": [{"id": "yes", "label": "是"}, {"id": "no", "label": "否"}],
                    }
                ]
            },
        )

        self.assertEqual(fake.calls, 2)
        self.assertIsNone(data["decisions"]["p01"])
        self.assertFalse(data["details"][0]["submitted"])

    async def test_real_tool_call_is_accepted(self) -> None:
        fake = _FakeLLM(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCallData(
                            id="call-choice",
                            name="submit_choice",
                            arguments_json='{"option_id":"yes"}',
                        )
                    ]
                )
            ]
        )
        self.orchestrator.llm = fake

        _, data = await execute_choice_requests(
            self.orchestrator,
            {
                "requests": [
                    {
                        "player_id": "p01",
                        "question": "choose",
                        "options": [{"id": "yes", "label": "是"}, {"id": "no", "label": "否"}],
                    }
                ]
            },
        )

        self.assertEqual(fake.calls, 1)
        self.assertEqual(data["decisions"]["p01"], "yes")
        self.assertTrue(data["details"][0]["submitted"])


if __name__ == "__main__":
    unittest.main()
