"""A scripted stand-in for the language model: agents act by reading the *world*, not the prompt.

``BotLlm`` implements :class:`app.runtime.ports.LlmPort`.  It is used by the end-to-end tests to
play a whole (tiny) game through the real world / runtime / brain / orchestrator without a network.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from app.llm.types import LLMResponse, ToolCallData

Call = tuple[str, dict[str, Any]]


def tool_response(calls: list[Call], *, thought: str = "") -> LLMResponse:
    return LLMResponse(
        thought=thought,
        content="",
        tool_calls=[
            ToolCallData(id=f"call_{uuid.uuid4().hex[:10]}", name=name, arguments_json=json.dumps(args, ensure_ascii=False))
            for name, args in calls
        ],
        prompt_tokens=120,
        completion_tokens=30,
        finish_reason="tool_calls",
        provider_id="bot",
        model_id="scripted",
    )


class BotLlm:
    """``policies[agent_id](world, agent_id) -> list[Call]``; an empty list becomes ``sleep``."""

    def __init__(self, world: Any, policies: dict[str, Callable[[Any, str], list[Call]]]) -> None:
        self.world = world
        self.policies = policies
        self.calls = 0

    async def complete(
        self, agent_id: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, *, max_tokens: int | None = None
    ) -> LLMResponse:
        self.calls += 1
        policy = self.policies.get(agent_id)
        calls = policy(self.world, agent_id) if policy else []
        return tool_response(calls or [("sleep", {})], thought="（脚本）")


def simple_player(world: Any, me: str) -> list[Call]:
    """Answer requests with the first option that is not me; speak when holding a floor."""
    board = world.board(me)
    for request in board.get("requests") or []:
        if request.get("answered"):
            continue
        options = [o["id"] for o in (request.get("options") or []) if o["id"] != me]
        return [("respond", {"request_id": request["id"], "value": options[0] if options else request["options"][0]["id"]})]
    for channel in board.get("channels") or []:
        if channel.get("can_write") and not channel.get("system"):
            return [("speak", {"channel_id": channel["id"], "content": f"我是 {me}，大家好。"})]
    return []
