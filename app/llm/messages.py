"""Helpers over the canonical (OpenAI-chat-shaped) message list the engine builds.

Canonical messages::

    {"role": "system" | "user", "content": str}
    {"role": "assistant", "content": str | None, "tool_calls": [{"id", "type": "function",
        "function": {"name", "arguments": <json str>}}], "_provider_private": {...}?}
    {"role": "tool", "tool_call_id": str, "content": str}

Tools are OpenAI ``{"type": "function", "function": {name, description, parameters}}``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.llm.types import PROVIDER_PRIVATE_KEY

INTERRUPTED_TOOL_RESULT = "（该工具调用没有返回结果：调用被中断。）"
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def text_of(content: Any) -> str:
    """Flatten a message ``content`` (str / list of parts / None) into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


def strip_private(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shallow copies of ``messages`` without engine-private (``_``-prefixed) keys.

    ``_provider_private`` and friends must never reach an SDK request body.
    """
    cleaned: list[dict[str, Any]] = []
    for message in messages:
        if any(isinstance(k, str) and k.startswith("_") for k in message):
            cleaned.append({k: v for k, v in message.items() if not str(k).startswith("_")})
        else:
            cleaned.append(message)
    return cleaned


def private_of(message: dict[str, Any]) -> Any:
    return message.get(PROVIDER_PRIVATE_KEY)


def call_id_of(call: Any) -> str:
    if isinstance(call, dict):
        return str(call.get("id") or "")
    return str(getattr(call, "id", "") or "")


def call_parts(call: dict[str, Any]) -> tuple[str, str, str]:
    """``(id, name, arguments_json)`` of one canonical tool call."""
    fn = call.get("function") or {}
    arguments = fn.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
    return str(call.get("id") or ""), str(fn.get("name") or ""), arguments or "{}"


def repair_tool_pairs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make every tool call have exactly one result, directly after its assistant turn.

    * a call without a result gets a synthetic "interrupted" result;
    * a result without a preceding call (or a duplicate result) is dropped;
    * results are reordered right behind the assistant message that made the calls.
    """
    out: list[dict[str, Any]] = []
    i = 0
    n = len(messages)
    while i < n:
        message = messages[i]
        role = message.get("role")
        if role == "tool":
            # orphan result (its assistant turn was compacted away or never existed)
            i += 1
            continue
        out.append(message)
        i += 1
        calls = message.get("tool_calls") if role == "assistant" else None
        if not calls:
            continue
        wanted = [call_parts(c)[0] for c in calls]
        results: dict[str, dict[str, Any]] = {}
        # collect the tool results that follow (they may be interleaved with nothing else)
        while i < n and messages[i].get("role") == "tool":
            tool_msg = messages[i]
            tid = str(tool_msg.get("tool_call_id") or "")
            if tid in wanted and tid not in results:
                results[tid] = tool_msg
            i += 1
        for tid in wanted:
            out.append(
                results.get(tid)
                or {"role": "tool", "tool_call_id": tid, "content": INTERRUPTED_TOOL_RESULT}
            )
    return out


def safe_tool_id(tool_id: str) -> str:
    """Anthropic tool ids must match ``^[a-zA-Z0-9_-]{1,64}$``; map others deterministically."""
    cleaned = _SAFE_ID_RE.sub("_", tool_id or "")[:64]
    return cleaned or "call_0"


def arguments_to_dict(arguments_json: str) -> dict[str, Any]:
    """Tool-call arguments as an object (``{}`` when empty / not an object)."""
    if not arguments_json or not arguments_json.strip():
        return {}
    try:
        parsed = json.loads(arguments_json)
    except (ValueError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def tool_function_spec(tool: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """``(name, description, parameters_schema)`` from an OpenAI tool definition."""
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    schema = fn.get("parameters")
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    return str(fn.get("name") or ""), str(fn.get("description") or ""), schema
