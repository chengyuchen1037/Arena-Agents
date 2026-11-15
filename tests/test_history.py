from __future__ import annotations

import unittest
from copy import deepcopy
from types import SimpleNamespace

from app.engine.history import (
    complete_interrupted_tool_calls,
    normalize_tool_history,
    trim_moderator_history,
)


EMPTY_REPORT = {
    "added_ids": [],
    "deduplicated_ids": [],
    "orphan_removed": [],
}


def assistant_with_calls(*call_ids: str) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": f"fn_{call_id}", "arguments": "{}"},
            }
            for call_id in call_ids
        ],
    }


def tool_response(call_id: str, content: str | None = None) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": content or f"result-{call_id}",
    }


class NormalizeToolHistoryTests(unittest.TestCase):
    def test_normal_history_is_unchanged(self) -> None:
        messages = [
            {"role": "system", "content": "system rules"},
            {"role": "user", "content": "user kickoff"},
            assistant_with_calls("call_1", "call_2"),
            tool_response("call_2", "second result first"),
            tool_response("call_1", "first result second"),
            {"role": "assistant", "content": "done"},
        ]
        original = deepcopy(messages)

        repaired, report = normalize_tool_history(messages)

        self.assertEqual(repaired, original)
        self.assertEqual(report, EMPTY_REPORT)
        self.assertEqual(messages, original)
        self.assertIsNot(repaired, messages)
        self.assertIsNot(repaired[0], messages[0])

    def test_missing_current_batch_is_completed_after_kickoff(self) -> None:
        kickoff = {"role": "user", "content": "continue from the saved state"}
        messages = [
            kickoff,
            assistant_with_calls("call_1", "call_2", "call_3"),
            {"role": "user", "content": "next instruction"},
        ]

        repaired, report = normalize_tool_history(messages)

        self.assertEqual(repaired[0], kickoff)
        self.assertEqual(
            [message.get("tool_call_id") for message in repaired[2:5]],
            ["call_1", "call_2", "call_3"],
        )
        self.assertTrue(
            all(
                message["content"].startswith("[此前工具执行中断]")
                for message in repaired[2:5]
            )
        )
        self.assertEqual(repaired[5]["content"], "next instruction")
        self.assertEqual(report["added_ids"], ["call_1", "call_2", "call_3"])
        self.assertEqual(report["deduplicated_ids"], [])
        self.assertEqual(report["orphan_removed"], [])

    def test_existing_partial_results_are_preserved(self) -> None:
        existing = tool_response("call_2", "real result")
        messages = [
            {"role": "user", "content": "kickoff"},
            assistant_with_calls("call_1", "call_2", "call_3"),
            existing,
            {"role": "assistant", "content": "later message"},
        ]

        repaired, report = normalize_tool_history(
            messages, placeholder_prefix="[cancelled]"
        )

        self.assertEqual(repaired[2], existing)
        self.assertEqual(repaired[2]["content"], "real result")
        self.assertEqual(
            [message["tool_call_id"] for message in repaired[2:5]],
            ["call_2", "call_1", "call_3"],
        )
        self.assertEqual(repaired[3]["content"], "[cancelled] tool_call_id=call_1")
        self.assertEqual(repaired[4]["content"], "[cancelled] tool_call_id=call_3")
        self.assertEqual(repaired[5]["content"], "later message")
        self.assertEqual(report["added_ids"], ["call_1", "call_3"])

    def test_duplicate_response_keeps_the_first_copy(self) -> None:
        messages = [
            assistant_with_calls("call_1", "call_2"),
            tool_response("call_1", "keep me"),
            tool_response("call_1", "remove duplicate"),
            tool_response("call_2"),
        ]

        repaired, report = normalize_tool_history(messages)

        self.assertEqual(len(repaired), 3)
        self.assertEqual(repaired[1]["content"], "keep me")
        self.assertEqual(repaired[2]["content"], "result-call_2")
        self.assertNotIn("remove duplicate", str(repaired))
        self.assertEqual(report["deduplicated_ids"], ["call_1"])
        self.assertEqual(report["added_ids"], [])
        self.assertEqual(report["orphan_removed"], [])

    def test_orphan_responses_are_removed_and_reported(self) -> None:
        messages = [
            tool_response("orphan_before"),
            {"role": "user", "content": "kickoff"},
            assistant_with_calls("call_1"),
            tool_response("wrong_parent"),
            tool_response("call_1", "valid response"),
            {"role": "assistant", "content": "no calls here"},
            tool_response("orphan_after"),
        ]

        repaired, report = normalize_tool_history(messages)

        self.assertEqual(
            repaired,
            [
                {"role": "user", "content": "kickoff"},
                assistant_with_calls("call_1"),
                tool_response("call_1", "valid response"),
                {"role": "assistant", "content": "no calls here"},
            ],
        )
        self.assertEqual(
            report["orphan_removed"],
            ["orphan_before", "wrong_parent", "orphan_after"],
        )
        self.assertEqual(report["added_ids"], [])
        self.assertEqual(report["deduplicated_ids"], [])

    def test_user_only_kickoff_is_untouched(self) -> None:
        messages = [{"role": "user", "content": "kickoff must survive"}]

        repaired, report = normalize_tool_history(messages)

        self.assertEqual(repaired, messages)
        self.assertEqual(report, EMPTY_REPORT)


class CompleteInterruptedToolCallsTests(unittest.TestCase):
    def test_cancellation_completes_current_and_remaining_idempotently(self) -> None:
        calls = [SimpleNamespace(id=call_id) for call_id in ("c1", "c2", "c3")]
        messages = [
            {"role": "user", "content": "kickoff"},
            assistant_with_calls("c1", "c2", "c3"),
            tool_response("c1", "already completed"),
            {"role": "user", "content": "later user message"},
        ]
        original = deepcopy(messages)

        repaired = complete_interrupted_tool_calls(
            messages,
            calls,
            current_index=2,
            error_text="[已取消] 用户请求暂停",
        )

        self.assertEqual(messages, original)
        self.assertEqual(repaired[2]["content"], "already completed")
        self.assertEqual(repaired[3]["tool_call_id"], "c2")
        self.assertEqual(repaired[4]["tool_call_id"], "c3")
        self.assertEqual(repaired[3]["content"], "[已取消] 用户请求暂停")
        self.assertEqual(repaired[4]["content"], "[已取消] 用户请求暂停")
        self.assertEqual(repaired[5]["content"], "later user message")

        repaired_again = complete_interrupted_tool_calls(
            repaired,
            calls,
            current_index=2,
            error_text="[已取消] 用户请求暂停",
        )
        self.assertEqual(repaired_again, repaired)
        self.assertEqual(
            sum(
                message.get("role") == "tool"
                and message.get("tool_call_id") in {"c1", "c2", "c3"}
                for message in repaired_again
            ),
            3,
        )

    def test_current_existing_response_is_not_duplicated(self) -> None:
        calls = [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}]
        messages = [
            assistant_with_calls("c1", "c2", "c3"),
            tool_response("c1"),
            tool_response("c2", "current finished before cancellation"),
        ]

        repaired = complete_interrupted_tool_calls(
            messages,
            calls,
            current_index=2,
            error_text="[中断] remaining call skipped",
        )

        self.assertEqual(len(repaired), 4)
        self.assertEqual(repaired[2]["content"], "current finished before cancellation")
        self.assertEqual(repaired[3]["tool_call_id"], "c3")
        self.assertEqual(repaired[3]["content"], "[中断] remaining call skipped")

    def test_helper_requires_the_parent_assistant(self) -> None:
        with self.assertRaises(ValueError):
            complete_interrupted_tool_calls(
                [{"role": "user", "content": "kickoff"}],
                [{"id": "missing"}],
                current_index=1,
                error_text="cancelled",
            )


class HistoryGrowthTests(unittest.TestCase):
    def test_normalize_does_not_deepcopy_but_never_mutates_the_input(self) -> None:
        messages = [{"role": "user", "content": "k"}, assistant_with_calls("c1")]
        nested = messages[1]["tool_calls"]
        snapshot = deepcopy(messages)

        repaired, report = normalize_tool_history(messages)

        self.assertEqual(messages, snapshot)  # input untouched (a response was added to the copy)
        self.assertEqual(report["added_ids"], ["c1"])
        self.assertIsNot(repaired[1], messages[1])  # message dicts are copies
        self.assertIs(repaired[1]["tool_calls"], nested)  # but nested payloads are shared
        repaired[0]["content"] = "changed"
        self.assertEqual(messages[0]["content"], "k")

    def test_trim_is_a_noop_below_the_limit(self) -> None:
        messages = [{"role": "user", "content": "k"}] + [
            {"role": "assistant", "content": str(i)} for i in range(50)
        ]
        self.assertIs(trim_moderator_history(messages, max_messages=100), messages)

    def test_trim_bounds_history_keeps_kickoff_and_never_starts_with_a_tool_response(self) -> None:
        messages: list[dict] = [{"role": "user", "content": "kickoff"}]
        for index in range(200):
            messages.append(assistant_with_calls(f"c{index}a", f"c{index}b"))
            messages.append(tool_response(f"c{index}a"))
            messages.append(tool_response(f"c{index}b"))

        trimmed = trim_moderator_history(messages, max_messages=240, keep_messages=100)

        self.assertLessEqual(len(trimmed), 101)
        self.assertEqual(trimmed[0], {"role": "user", "content": "kickoff"})
        self.assertNotEqual(trimmed[1]["role"], "tool")
        self.assertEqual(trimmed[-1], messages[-1])
        _, report = normalize_tool_history(trimmed)
        self.assertFalse(any(report.values()), report)


if __name__ == "__main__":
    unittest.main()


class StripProviderPrivateTests(unittest.TestCase):
    def test_strips_private_blocks_and_reissues_ids_consistently(self) -> None:
        from app.engine.history import strip_provider_private

        messages = [
            {"role": "user", "content": "k"},
            {**assistant_with_calls("toolu_A", "toolu_B"), "_provider_private": {"sig": "x"}},
            tool_response("toolu_B"),
            tool_response("toolu_A"),
        ]
        original = deepcopy(messages)

        result = strip_provider_private(messages)

        self.assertEqual(messages, original)  # input untouched
        self.assertNotIn("_provider_private", result[1])
        self.assertEqual([c["id"] for c in result[1]["tool_calls"]], ["call_1", "call_2"])
        self.assertEqual([m["tool_call_id"] for m in result[2:]], ["call_2", "call_1"])
        _, report = normalize_tool_history(result)
        self.assertFalse(any(report.values()), report)
