"""``bench.fakes``: the scripted model of the smoke tests - what it reads, what it answers, and that it answers with tool calls only."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.llm.types import LLMResponse
from bench.fakes import (
    LIST_TEXT,
    POLICIES,
    FakeContext,
    FakeLlm,
    Plan,
    estimate_tokens,
    first_other,
    listy,
    long_text,
    mixed,
    open_request,
    policy_named,
    prefixed,
    prose,
    scripted,
    sleepy,
    writable_channel,
)
from bench.play import build_scene
from bench_helpers import example


def ctx_for(situation: str, seat: str = "N3", *, call: int = 0, sample: int = 0, bot: str = "bot:Alice") -> FakeContext:
    scene = build_scene(example(situation), seat=seat)
    return FakeContext(scene.world, seat, bot, f"example-{situation}", sample, call)


def calls_of(plan: Plan) -> list[tuple[str, dict[str, Any]]]:
    return plan.calls


# ------------------------------------------------------------------------------------------------ reading the world


def test_the_world_is_read_not_the_prompt() -> None:
    vote, speak = ctx_for("vote"), ctx_for("speak")
    request = open_request(vote.world, "N3")
    assert request is not None and request["id"] == "r1" and request["answered"] is False and open_request(speak.world, "N3") is None
    channel = writable_channel(speak.world, "N3")
    assert channel is not None and channel["id"] == "public" and channel["floor"]["mine"] and writable_channel(vote.world, "N3") is None  # (a system DM is nobody's audience)
    assert first_other([{"id": "N3"}, {"id": "N1"}], "N3") == "N1" and first_other([{"id": "N3"}], "N3") == "N3" and first_other([], "N3") == ""


# ------------------------------------------------------------------------------------------------ the policies


def test_a_request_is_answered_with_the_first_other_option_and_a_floor_is_used_with_prose() -> None:
    vote = prose(ctx_for("vote"))
    assert [(name, args) for name, args in vote.calls] == [("respond", {"request_id": "r1", "value": "N1"})] and "作答" in vote.thought
    speak = prose(ctx_for("speak"))
    assert len(speak.calls) == 1 and speak.calls[0][0] == "speak" and speak.calls[0][1]["channel_id"] == "public" and len(speak.calls[0][1]["content"]) < 120
    assert "\n" not in speak.calls[0][1]["content"]


def test_the_listy_policy_writes_a_list_first_and_prose_when_it_is_sent_back() -> None:
    first, again = listy(ctx_for("speak", call=0)), listy(ctx_for("speak", call=1))
    assert first.calls[0][1]["content"] == LIST_TEXT and again.calls[0][1]["content"] != LIST_TEXT and "\n" not in again.calls[0][1]["content"]
    assert prefixed(ctx_for("speak", call=0)).calls[0][1]["content"].startswith("N3：") and not prefixed(ctx_for("speak", call=1)).calls[0][1]["content"].startswith("N3：")


def test_the_long_policy_writes_just_over_the_limit_of_one_message_then_something_short() -> None:
    ctx = ctx_for("speak")
    limit = writable_channel(ctx.world, "N3")["max_chars"]
    first, again = long_text(ctx), long_text(ctx_for("speak", call=1))
    assert limit == 120 and limit < len(first.calls[0][1]["content"]) <= limit + 40 and len(again.calls[0][1]["content"]) < limit


def test_the_sleepy_policy_never_acts_and_mixed_cycles_through_the_others() -> None:
    assert sleepy(ctx_for("speak")).calls == [("sleep", {})] and sleepy(ctx_for("vote")).calls == [("sleep", {})]
    kinds = [mixed(ctx_for("speak", sample=i)).calls[0][1]["content"] for i in range(4)]
    assert kinds[1] == LIST_TEXT and kinds[0] != LIST_TEXT and len(kinds[2]) > 120 and kinds[3].startswith("N3：")
    assert [name for name in POLICIES] == ["prose", "listy", "prefixed", "long", "sleepy", "mixed"]
    assert policy_named("prose") is prose
    with pytest.raises(ValueError, match="不认识的假模型策略 'nope'"):
        policy_named("nope")


def test_the_prose_a_policy_picks_depends_on_the_bot_the_situation_and_the_sample_only() -> None:
    a = prose(ctx_for("speak", bot="bot:Alice")).calls[0][1]["content"]
    assert a == prose(ctx_for("speak", bot="bot:Alice")).calls[0][1]["content"]
    texts = {prose(ctx_for("speak", bot=f"bot:{n}", sample=s)).calls[0][1]["content"] for n in "ABCDEFGH" for s in range(3)}
    assert len(texts) >= 2  # (not one fixed sentence: the S3 numbers of a fake run are not a measurement, but they are not all the same either)


def test_a_scripted_policy_says_exactly_what_the_test_wants_then_sleeps() -> None:
    policy = scripted([("write_note", {"title": "t", "content": "c"}), ("speak", {"channel_id": "public", "content": "好"})], [("pass", {"channel_id": "public"})], thoughts=["想一想"])
    first, second, third = (policy(FakeContext(None, "N3", "b", "s", 0, i)) for i in range(3))  # type: ignore[arg-type]
    assert [c[0] for c in first.calls] == ["write_note", "speak"] and first.thought == "想一想" and second.calls == [("pass", {"channel_id": "public"})] and third.calls == [("sleep", {})]


# ------------------------------------------------------------------------------------------------ the port


def test_the_fake_port_answers_with_tool_calls_and_counts_tokens() -> None:
    scene = build_scene(example("speak"))
    llm = FakeLlm(prose, world=scene.world, seat="N3", bot="b", situation="s")
    messages = [{"role": "system", "content": "系统"}, {"role": "user", "content": "你好"}]
    resp = asyncio.run(llm.complete("N3", messages, [{"type": "function"}], max_tokens=100))
    assert isinstance(resp, LLMResponse) and resp.finish_reason == "tool_calls" and resp.provider_id == "fake" and resp.model_id == "fake"
    assert [c.name for c in resp.tool_calls] == ["speak"] and json.loads(resp.tool_calls[0].arguments_json)["channel_id"] == "public" and resp.tool_calls[0].id.startswith("fake_")
    assert resp.prompt_tokens == estimate_tokens(messages) > 0 and resp.completion_tokens >= 1 and resp.content == ""
    assert llm.calls[0]["agent_id"] == "N3" and llm.calls[0]["messages"] == messages and llm.calls[0]["max_tokens"] == 100
    again = asyncio.run(llm.complete("N3", messages, None))
    assert len(llm.calls) == 2 and again.tool_calls  # (the policy is told how many answers it has given: ``call``)


def test_a_policy_that_says_nothing_gives_a_plain_answer() -> None:
    scene = build_scene(example("speak"))
    llm = FakeLlm(lambda ctx: Plan(content="只是想想"), world=scene.world, seat="N3")
    resp = asyncio.run(llm.complete("N3", [{"role": "user", "content": "x"}], None))
    assert resp.tool_calls == [] and resp.content == "只是想想" and resp.finish_reason == "stop"
    cut = FakeLlm(lambda ctx: Plan(finish_reason="length"), world=scene.world, seat="N3")
    assert asyncio.run(cut.complete("N3", [{"role": "user", "content": "x"}], None)).finish_reason == "length"


def test_the_fake_imports_no_provider_and_no_network_library() -> None:
    import ast
    from pathlib import Path

    import bench.fakes as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    banned = ("httpx", "requests", "aiohttp", "openai", "anthropic", "socket", "urllib", "ssl", "app.providers", "app.storage", "app.engine", "app.llm.registry")
    assert not [name for name in imported if any(name == b or name.startswith(b + ".") for b in banned)], sorted(imported)
    assert "api_key" not in Path(module.__file__).read_text(encoding="utf-8")
