"""The fake model of the benchmark's smoke tests (CI and unit tests: 0 calls to a real model).

A fake model is a ``LlmPort`` (``async complete(agent_id, messages, tools, *, max_tokens)``) whose answers come from a *policy*: a function that
looks at the world (like ``tests/v12/bots.py``: the scripted bots read the board, not the prompt) and says which tools to call.  The real ``Agent`` /
worker loop runs on top of it unchanged, so the smoke test goes through exactly what a real run goes through - the brain's prompts and tools, the
output gate, the commit gate, the receipts - and only the model's words are canned.  What these policies write is NOT a measurement of anything:
the numbers of a fake run only show that the pipeline works.

Stock policies (``--fake`` of ``python -m bench run``): ``prose`` (a short spoken text; a vote for the first other candidate), ``listy`` (first
a list-shaped text - a bot's output gate sends it back, the v0.13.1 player's does not - then prose), ``prefixed`` (first a text that starts with the
seat's own id), ``long`` (first a text over the limit of one message - the world sends it back - then a short one), ``sleepy`` (never acts) and
``mixed`` (one of the first four, by the sample number).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.llm.types import LLMResponse, ToolCallData
from app.world.world import World


@dataclass
class FakeContext:
    """What a policy may look at."""

    world: World
    seat: str
    bot: str
    situation: str
    sample: int
    call: int  # 0-based: how many answers this sample has had before
    messages: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] | None = None


@dataclass
class Plan:
    """What the fake model answers: tool calls (``(name, args)``), the thinking and the plain text that go with them."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    thought: str = ""
    content: str = ""
    finish_reason: str | None = None


Policy = Callable[[FakeContext], Plan]

PROSE_POOL = (
    "我先听听大家怎么说，现在还没有什么能核对的东西，不想乱站队。",
    "到现在为止我只看到发言，没有看到事实，所以我先不下结论。",
    "我想先把谁说了什么记下来，等有能对上的地方再说。",
    "这一轮我保持观望，谁的话和后面的结果对不上，我就记谁。",
)
LIST_TEXT = "我的看法如下：\n1. 目前没有可核对的事实\n2. 发言只有立场没有依据\n3. 所以我先观望"
LONG_PAD = "我想把现在的情况说得再仔细一点，" 


# ---------------------------------------------------------------------------------------------------------------------- reading the world
def open_request(world: World, seat: str) -> dict[str, Any] | None:
    """The first request the seat has not answered yet."""
    for request in world.board(seat).get("requests") or []:
        if not request.get("answered"):
            return request
    return None


def writable_channel(world: World, seat: str) -> dict[str, Any] | None:
    """The channel the seat may speak in right now: the one whose floor it holds, else a free one; a system DM is nobody's audience."""
    channels = [c for c in world.board(seat).get("channels") or [] if c.get("can_write") and not c.get("system")]
    for channel in channels:
        if (channel.get("floor") or {}).get("mine"):
            return channel
    return channels[0] if channels else None


def first_other(options: Sequence[Mapping[str, Any]], seat: str) -> str:
    """The first option that is not the seat itself."""
    ids = [str(o.get("id")) for o in options]
    return next((i for i in ids if i != seat), ids[0] if ids else "")


def _pick(pool: Sequence[str], ctx: FakeContext) -> str:
    key = f"{ctx.bot}|{ctx.situation}|{ctx.sample}".encode()
    return pool[int(hashlib.sha1(key).hexdigest()[:6], 16) % len(pool)]


def _sleep(ctx: FakeContext, why: str = "没有轮到我，也没有要作答的事") -> Plan:
    return Plan(calls=[("sleep", {})], thought=f"（假模型）{why}。")


# ---------------------------------------------------------------------------------------------------------------------- policies
def _act(ctx: FakeContext, texts: Callable[[FakeContext, int], str | None]) -> Plan:
    """The shared skeleton: answer a request if there is one, else speak where the seat may, else sleep.  ``texts(ctx, cap)`` is the speech."""
    request = open_request(ctx.world, ctx.seat)
    if request is not None:
        value = first_other(request.get("options") or [], ctx.seat) if request.get("kind") == "choice" else "我没有特别的看法。"
        return Plan(calls=[("respond", {"request_id": request["id"], "value": value})], thought=f"（假模型）作答 {request['id']}：{value}。")
    channel = writable_channel(ctx.world, ctx.seat)
    if channel is None:
        return _sleep(ctx)
    text = texts(ctx, int(channel.get("max_chars") or 300))
    if text is None:
        return _sleep(ctx, "决定不发言")
    return Plan(calls=[("speak", {"channel_id": channel["id"], "content": text})], thought=f"（假模型）在 {channel['id']} 发言。")


def prose(ctx: FakeContext) -> Plan:
    return _act(ctx, lambda c, cap: _pick(PROSE_POOL, c))


def listy(ctx: FakeContext) -> Plan:
    return _act(ctx, lambda c, cap: LIST_TEXT if c.call == 0 else _pick(PROSE_POOL, c))


def prefixed(ctx: FakeContext) -> Plan:
    return _act(ctx, lambda c, cap: f"{c.seat}：{_pick(PROSE_POOL, c)}" if c.call == 0 else _pick(PROSE_POOL, c))


def long_text(ctx: FakeContext) -> Plan:
    def text(c: FakeContext, cap: int) -> str:
        if c.call != 0:
            return _pick(PROSE_POOL, c)
        return (LONG_PAD * (cap // len(LONG_PAD) + 3))[: cap + 40]

    return _act(ctx, text)


def sleepy(ctx: FakeContext) -> Plan:
    return _sleep(ctx, "决定先看看")


_MIXED = (prose, listy, long_text, prefixed)


def mixed(ctx: FakeContext) -> Plan:
    return _MIXED[ctx.sample % len(_MIXED)](ctx)


POLICIES: dict[str, Policy] = {
    "prose": prose, "listy": listy, "prefixed": prefixed, "long": long_text, "sleepy": sleepy, "mixed": mixed,
}


def policy_named(name: str) -> Policy:
    if name not in POLICIES:
        raise ValueError(f"不认识的假模型策略 {name!r}。可用的：{'、'.join(POLICIES)}。")
    return POLICIES[name]


def scripted(*rounds: Sequence[tuple[str, Mapping[str, Any]]], thoughts: Sequence[str] = ()) -> Policy:
    """A policy that answers call *i* with ``rounds[i]`` (a list of ``(tool, args)``), and with ``sleep`` once the script is over - the
    way a unit test says exactly what the "model" does."""

    def policy(ctx: FakeContext) -> Plan:
        if ctx.call >= len(rounds):
            return Plan(calls=[("sleep", {})])
        thought = thoughts[ctx.call] if ctx.call < len(thoughts) else ""
        return Plan(calls=[(name, dict(args)) for name, args in rounds[ctx.call]], thought=thought)

    return policy


# ---------------------------------------------------------------------------------------------------------------------- the port
def estimate_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    return max(1, len(json.dumps(list(messages), ensure_ascii=False, default=str)) // 3)


class FakeLlm:
    """A ``LlmPort`` that answers by ``policy``.  One per sample (it knows whose turn it is): ``calls`` keeps what every request showed."""

    def __init__(self, policy: Policy, *, world: World, seat: str, bot: str = "", situation: str = "", sample: int = 0) -> None:
        self.policy = policy
        self.world = world
        self.seat = seat
        self.bot = bot
        self.situation = situation
        self.sample = sample
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, agent_id: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, *, max_tokens: int | None = None
    ) -> LLMResponse:
        ctx = FakeContext(self.world, self.seat, self.bot, self.situation, self.sample, len(self.calls), list(messages), tools)
        self.calls.append({"agent_id": agent_id, "messages": list(messages), "tools": tools, "max_tokens": max_tokens})
        plan = self.policy(ctx)
        calls = [
            ToolCallData(id=f"fake_{uuid.uuid4().hex[:8]}", name=name, arguments_json=json.dumps(args, ensure_ascii=False))
            for name, args in plan.calls
        ]
        return LLMResponse(
            thought=plan.thought,
            content=plan.content,
            tool_calls=calls,
            prompt_tokens=estimate_tokens(messages),
            completion_tokens=max(1, sum(len(c.arguments_json) for c in calls) // 2 + len(plan.thought) // 2),
            finish_reason=plan.finish_reason or ("tool_calls" if calls else "stop"),
            provider_id="fake",
            model_id="fake",
        )


__all__ = [
    "FakeContext",
    "FakeLlm",
    "POLICIES",
    "Plan",
    "Policy",
    "estimate_tokens",
    "first_other",
    "open_request",
    "policy_named",
    "scripted",
    "writable_channel",
]
