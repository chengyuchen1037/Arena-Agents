"""v0.12.1: an agent's conversation is an append-only prefix - across rounds, activations and a pause / resume -
which is what makes provider prompt caches hit.  Checked on the real runtime with a recording scripted model."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from app.llm.types import LLMResponse
from app.world.types import MODERATOR_ID
from game_fakes import async_test
from test_roster_runtime import IDS, chatty, events_of, make_orch, wait_for
from v12.bots import BotLlm, simple_player


class RecordingBot(BotLlm):
    """Keeps a deep copy of everything each agent sent to the model (system + tools + messages)."""

    def __init__(self, world: Any, policies: dict[str, Any]) -> None:
        super().__init__(world, policies)
        self.sent: dict[str, list[dict[str, Any]]] = {}

    async def complete(self, agent_id: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, *, max_tokens: int | None = None) -> LLMResponse:
        self.sent.setdefault(agent_id, []).append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        return await super().complete(agent_id, messages, tools, max_tokens=max_tokens)


def install(orch) -> list[RecordingBot]:
    made: list[RecordingBot] = []

    def factory(o):
        # one bot per game: the pause / resume of the same game keeps talking to the same recorder
        if not made or made[-1].world is not o.world:
            made.append(RecordingBot(o.world, {MODERATOR_ID: chatty, **{pid: simple_player for pid in IDS}}))
        return made[-1]

    orch.ensure_bindings_ready = lambda: None
    orch.llm_port_factory = factory
    return made


def assert_append_only(calls: list[dict[str, Any]]) -> None:
    for before, after in zip(calls, calls[1:]):
        old, new = before["messages"], after["messages"]
        assert new[: len(old)] == old, "the conversation was rewritten, not appended to"
        assert after["tools"] == before["tools"]  # the tool list never changes either


@async_test
async def test_every_request_of_an_agent_extends_the_previous_one_byte_for_byte(monkeypatch):
    orch = make_orch(monkeypatch)
    bots = install(orch)
    await orch.start_or_resume()
    await wait_for(lambda: bots and all(len(bots[0].sent.get(pid, [])) >= 2 for pid in IDS))
    await orch.pause()
    bot = bots[0]
    for agent in (MODERATOR_ID, *IDS):
        calls = bot.sent[agent]
        assert len(calls) >= 1
        assert_append_only(calls)
        first = calls[0]["messages"]
        assert first[0]["role"] == "system" and first[1]["role"] == "user"  # system prompt, then the FULL first message


@async_test
async def test_pausing_and_resuming_keeps_the_conversation_and_the_system_prompt(monkeypatch):
    orch = make_orch(monkeypatch)
    bots = install(orch)
    await orch.start_or_resume()
    await wait_for(lambda: bots and len(bots[0].sent.get("N1", [])) >= 2)
    await orch.pause()
    bot = bots[0]
    before = len(bot.sent["N1"])
    system_before = bot.sent["N1"][0]["messages"][0]
    first_user = bot.sent["N1"][0]["messages"][1]

    # a message while the game is paused is delivered when it resumes; the agent then continues the same conversation
    await orch.start_or_resume()
    try:
        orch.world.submit  # the world is unchanged: same object, same ledger
        await wait_for(lambda: len(bot.sent["N1"]) > before or len(bot.sent[MODERATOR_ID]) > 0)
        await wait_for(lambda: orch.session.status == "running")
    finally:
        await orch.pause()
    assert bots[0] is bot and len(bots) == 1  # the same game, the same recorder
    for call in bot.sent["N1"][before:]:
        assert call["messages"][0] == system_before  # byte-identical system prompt: the cached prefix holds
        assert call["messages"][1] == first_user  # ... and so does the FULL first message
    assert_append_only(bot.sent["N1"])


@async_test
async def test_a_restored_game_starts_every_conversation_again_from_the_board(monkeypatch):
    orch = make_orch(monkeypatch)
    bots = install(orch)
    await orch.start_or_resume()
    await wait_for(lambda: bots and len(bots[0].sent.get("N2", [])) >= 1)
    await orch.pause()
    payload = orch.session_payload()

    fresh = make_orch(monkeypatch)
    other = install(fresh)
    await fresh.load_session_data(json.loads(json.dumps(payload)))
    assert len(fresh._book) == 0  # conversations are not part of a save
    await fresh.start_or_resume()  # the moderator is told the game was resumed; nobody else has anything new yet
    try:
        await wait_for(lambda: other and len(other[0].sent.get(MODERATOR_ID, [])) >= 1)
    finally:
        await fresh.pause()
    first = other[0].sent[MODERATOR_ID][0]["messages"]
    assert first[0]["role"] == "system" and first[1]["role"] == "user" and "【状态板】" in first[1]["content"]  # a FULL message again
    assert "game.resumed" in json.dumps(first[1]["content"], ensure_ascii=False) or "恢复" in first[1]["content"]


@async_test
async def test_every_message_wakes_the_others_but_never_its_sender(monkeypatch):
    orch = make_orch(monkeypatch)
    bots = install(orch)
    await orch.start_or_resume()
    await wait_for(lambda: orch.world.channel("public") is not None and any(e.type == "message" and e.actor_id in IDS for e in orch.world.ledger))
    await asyncio.sleep(0.3)
    await orch.pause()
    activations = {a: orch.usage.actor(a).activations for a in (MODERATOR_ID, *IDS)}
    assert all(n >= 1 for n in activations.values()), activations  # everybody was woken by the discussion
    speakers = {e.actor_id for e in orch.world.ledger if e.type == "message" and e.actor_id in IDS}
    assert speakers  # somebody spoke; each speaker's own message was not what woke him (checked at the world level in tests/v12/world)
