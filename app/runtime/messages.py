"""Conversation-message helpers for the worker.

Messages use the canonical, OpenAI-chat-shaped form that every protocol adapter understands (see
:mod:`app.llm.messages`).  The worker builds a *fresh* conversation for every run, so none of this
has to survive a cancellation.
"""

from __future__ import annotations

import json
from typing import Any

from app.llm.types import PROVIDER_PRIVATE_KEY, LLMResponse, ToolCallData


def system_message(text: str) -> dict[str, Any]:
    return {"role": "system", "content": text}


def user_message(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def tool_message(call_id: str, text: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def assistant_text_message(resp: LLMResponse) -> dict[str, Any] | None:
    """The assistant turn of an answer *without* tool calls, or ``None`` when it said nothing."""
    content = (resp.content or "").strip()
    if not content:
        return None
    return {"role": "assistant", "content": content}


def assistant_tool_message(resp: LLMResponse) -> dict[str, Any]:
    """The assistant turn that carries ``resp``'s tool calls (same shape as the v0.11 engine).

    Provider-private blocks (e.g. signed thinking) are kept, so the *same* provider can resume the
    turn after the tool results; adapters ignore them for any other provider.
    """
    message: dict[str, Any] = {
        "role": "assistant",
        "content": resp.content or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_json or "{}"},
            }
            for call in resp.tool_calls
        ],
    }
    private = getattr(resp, "provider_private", None)
    if private:
        message[PROVIDER_PRIVATE_KEY] = private
    return message


def ensure_call_ids(resp: LLMResponse, prefix: str = "call") -> None:
    """Give every tool call a unique, non-empty id (some providers omit or repeat them).

    Every tool call needs exactly one tool result with the same id, so the ids are fixed *before*
    the assistant message is built.  Mutates ``resp.tool_calls`` in place.
    """
    seen: set[str] = set()
    for index, call in enumerate(resp.tool_calls):
        call_id = str(call.id or "")
        if not call_id or call_id in seen:
            call_id = f"{prefix}_{index}"
            while call_id in seen:
                call_id += "_"
            call.id = call_id
        seen.add(call_id)


def args_error_text(name: str, error: str) -> str:
    """Tool result for a call whose arguments could not be parsed (never reaches the world)."""
    return json.dumps(
        {"ok": False, "error": f"工具 {name} 的参数无效：{error}"}, ensure_ascii=False
    )


def tool_failure_text(name: str, exc: BaseException) -> str:
    """Tool result for a tool that raised (a bug must not kill the run or break call pairing)."""
    return json.dumps(
        {"ok": False, "error": f"工具 {name} 执行失败：{type(exc).__name__}: {exc}"},
        ensure_ascii=False,
    )


def parse_call(call: ToolCallData) -> tuple[dict[str, Any], str | None]:
    """``(args, error)``: ``args`` is always a dict, ``error`` explains unusable arguments."""
    try:
        args, error = call.parse_args_checked()
    except Exception as exc:  # noqa: BLE001 - parsing model output must never take a run down
        return {}, f"参数解析失败：{exc}"
    if error:
        return {}, error
    return args, None
