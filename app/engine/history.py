from __future__ import annotations

from typing import Any, TypedDict


class ToolHistoryReport(TypedDict):
    """Repairs applied by :func:`normalize_tool_history`, in encounter order."""

    added_ids: list[str]
    deduplicated_ids: list[str]
    orphan_removed: list[str | None]


def _copy_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy each message dict one level deep.

    The repair helpers only add, drop and reorder messages; they never edit a
    message in place, so a per-message shallow copy is enough to keep the input
    untouched.  (A full ``deepcopy`` of an ever-growing history on every
    moderator step used to dominate the loop's CPU time.)
    """
    return [dict(message) if isinstance(message, dict) else message for message in messages]


def trim_moderator_history(
    messages: list[dict[str, Any]],
    *,
    max_messages: int = 240,
    keep_messages: int = 160,
) -> list[dict[str, Any]]:
    """Bound the stored moderator history without breaking tool-call blocks.

    Nothing happens until the list exceeds ``max_messages``; it is then cut down
    to roughly ``keep_messages`` (amortizing the cost over many steps).  The
    first user message (the kickoff) is preserved and the retained tail never
    starts with an orphaned ``tool`` response.  The model only ever sees the
    most recent 80 messages, so older entries are dead weight in saves.
    """
    if len(messages) <= max_messages:
        return messages
    start = max(1, len(messages) - keep_messages)
    while start < len(messages) and messages[start].get("role") == "tool":
        start += 1
    head = [messages[0]] if messages and messages[0].get("role") == "user" else []
    return head + list(messages[start:])


_PROVIDER_PRIVATE_KEY = "_provider_private"


def strip_provider_private(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make a history portable to another provider.

    Provider-private blocks (signed thinking, native reasoning payloads) are dropped and every
    tool-call id is re-issued consistently (``call_1``, ``call_2`` ...) because private ids must
    not cross providers.  Text and tool semantics are untouched; the input is not mutated.
    Run :func:`normalize_tool_history` afterwards to repair any interrupted call.
    """
    mapping: dict[str, str] = {}

    def new_id(old: Any) -> Any:
        if not isinstance(old, str) or not old:
            return old
        if old not in mapping:
            mapping[old] = f"call_{len(mapping) + 1}"
        return mapping[old]

    result: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            result.append(message)
            continue
        item = {k: v for k, v in message.items() if k != _PROVIDER_PRIVATE_KEY}
        calls = item.get("tool_calls")
        if isinstance(calls, list):
            item["tool_calls"] = [
                {**call, "id": new_id(call.get("id"))} if isinstance(call, dict) else call
                for call in calls
            ]
        if item.get("role") == "tool" and "tool_call_id" in item:
            item["tool_call_id"] = new_id(item["tool_call_id"])
        result.append(item)
    return result


def _tool_call_id(call: Any) -> str | None:
    value = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
    return value if isinstance(value, str) and value else None


def _declared_tool_call_ids(message: dict[str, Any]) -> list[str]:
    calls = message.get("tool_calls") or []
    if not isinstance(calls, (list, tuple)):
        return []
    return [call_id for call in calls if (call_id := _tool_call_id(call))]


def _response_id(message: dict[str, Any]) -> str | None:
    value = message.get("tool_call_id")
    return value if isinstance(value, str) and value else None


def _tool_response(call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _placeholder_content(placeholder_prefix: str, call_id: str) -> str:
    return f"{placeholder_prefix} tool_call_id={call_id}".strip()


def normalize_tool_history(
    messages: list[dict[str, Any]],
    placeholder_prefix: str = "[此前工具执行中断]",
) -> tuple[list[dict[str, Any]], ToolHistoryReport]:
    """Return a copy whose assistant/tool-call response blocks satisfy OpenAI.

    The first valid response for each declared tool call is retained in place.
    Missing responses are appended after the existing responses in that block,
    before the next non-tool message. Later duplicates and tool responses that
    do not belong to the immediately preceding assistant tool-call block are
    removed. Every other message retains its content and relative order.

    ``orphan_removed`` contains the removed responses' IDs, using ``None`` when
    a response had no valid string ``tool_call_id``. The input is never mutated.
    """

    source = _copy_messages(messages)
    repaired: list[dict[str, Any]] = []
    report: ToolHistoryReport = {
        "added_ids": [],
        "deduplicated_ids": [],
        "orphan_removed": [],
    }

    index = 0
    while index < len(source):
        message = source[index]

        if message.get("role") == "tool":
            report["orphan_removed"].append(_response_id(message))
            index += 1
            continue

        declared_ids = (
            _declared_tool_call_ids(message)
            if message.get("role") == "assistant"
            else []
        )
        repaired.append(message)

        if not declared_ids:
            index += 1
            continue

        expected = set(declared_ids)
        answered: set[str] = set()
        cursor = index + 1

        while cursor < len(source) and source[cursor].get("role") == "tool":
            response = source[cursor]
            response_id = _response_id(response)
            if response_id not in expected:
                report["orphan_removed"].append(response_id)
            elif response_id in answered:
                if response_id not in report["deduplicated_ids"]:
                    report["deduplicated_ids"].append(response_id)
            else:
                repaired.append(response)
                answered.add(response_id)
            cursor += 1

        for call_id in declared_ids:
            if call_id in answered:
                continue
            repaired.append(
                _tool_response(
                    call_id,
                    _placeholder_content(placeholder_prefix, call_id),
                )
            )
            report["added_ids"].append(call_id)
            answered.add(call_id)

        index = cursor

    return repaired, report


def complete_interrupted_tool_calls(
    messages: list[dict[str, Any]],
    all_tool_calls: list[Any],
    current_index: int,
    error_text: str,
) -> list[dict[str, Any]]:
    """Complete the current and remaining calls in an interrupted batch.

    ``current_index`` is 1-based, matching ``enumerate(..., start=1)`` in the
    orchestrator. ``all_tool_calls`` may contain OpenAI dictionaries or objects
    with an ``id`` attribute. ``error_text`` is used verbatim as the content of
    every response inserted by this helper.

    The target assistant must already be present in ``messages``. Existing
    responses in its contiguous tool-response block are preserved, and repeated
    calls to this helper do not create duplicate responses. The input is never
    mutated; assign the returned list back to the caller's message history.
    """

    calls = list(all_tool_calls)
    if current_index < 1 or current_index > len(calls):
        raise ValueError("current_index is 1-based and must identify a tool call")

    call_ids: list[str] = []
    for call in calls:
        call_id = _tool_call_id(call)
        if call_id is None:
            raise ValueError("every tool call must have a non-empty string id")
        call_ids.append(call_id)

    pending_ids = call_ids[current_index - 1 :]
    current_id = pending_ids[0]
    source = _copy_messages(messages)

    assistant_index: int | None = None
    assistant_ids: list[str] = []
    for index in range(len(source) - 1, -1, -1):
        message = source[index]
        if message.get("role") != "assistant":
            continue
        declared_ids = _declared_tool_call_ids(message)
        if current_id in declared_ids:
            assistant_index = index
            assistant_ids = declared_ids
            break

    if assistant_index is None:
        raise ValueError("the current tool call has no parent assistant message")

    if any(call_id not in assistant_ids for call_id in pending_ids):
        raise ValueError("all_tool_calls does not match the parent assistant")

    insert_at = assistant_index + 1
    answered: set[str] = set()
    while insert_at < len(source) and source[insert_at].get("role") == "tool":
        response_id = _response_id(source[insert_at])
        if response_id in assistant_ids:
            answered.add(response_id)
        insert_at += 1

    additions: list[dict[str, Any]] = []
    for call_id in pending_ids:
        if call_id in answered:
            continue
        additions.append(_tool_response(call_id, error_text))
        answered.add(call_id)

    return source[:insert_at] + additions + source[insert_at:]


__all__ = [
    "ToolHistoryReport",
    "complete_interrupted_tool_calls",
    "normalize_tool_history",
    "trim_moderator_history",
]
