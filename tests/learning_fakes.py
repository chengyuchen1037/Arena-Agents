"""Fakes for the tests of the learning runs (v0.14): a scripted model that drives the REAL learning brain, and the memory texts it writes.

No test here calls a model: ``ScriptedLlm`` answers every request of a run from a script - a function of what the run is (``init`` /
``review``), how many answers it has given and what the bot said so far - with real tool calls (``read_source``, ``write_memory``, ``finish``
...), so the whole chain (loop, brain, port, store) is the production one.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from typing import Any

from app.llm.types import LLMResponse, ToolCallData

ARCH_NOTES = "我读完了平台契约：被唤醒不等于必须行动；要发言就调用 speak，没有事就 sleep；被退回时按回执改。"
DIGEST = "我理解的规则：每轮先发言再投票，一队全部出局则另一队获胜；公开的事实以主持人的公告为准。"
PLAYBOOK = (
    "- [把握:低|支持:0|反例:0|来源:初始化|#发言] 先说清楚自己依据的事实，再表态。\n"
    "- [把握:中|支持:0|反例:0|来源:初始化|#投票] 投票前先问自己：最硬的事实是什么。"
)
QUESTIONS = "不确定：别人的发言长短，和他说的是不是真话有没有关系。"
PEOPLE = "- Bob：说话短，爱抢先表态。"
#: What a review makes of the playbook: the same hypotheses and one more, written after the game (``来源:复盘``).
REVIEW_PLAYBOOK = PLAYBOOK + "\n- [把握:低|支持:0|反例:0|来源:复盘|#发言] 先把别人说过的话和事实对一遍，再决定信不信。"

Script = Callable[["Call"], list[tuple[str, dict[str, Any]]] | str]


class Call:
    """What a script is told about one request of a run."""

    def __init__(self, agent_id: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, index: int | None = None) -> None:
        self.agent_id = agent_id
        self.messages = messages
        self.tools = tools or []
        # how many answers THIS run has been given before this one (counted in its conversation: another job of the same bot starts at 0 again)
        self.index = sum(1 for m in messages if m.get("role") == "assistant") if index is None else index

    @property
    def kind(self) -> str:
        names = {t["function"]["name"] for t in self.tools}
        return "review" if "read_record" in names else "init"

    @property
    def system(self) -> str:
        return str(self.messages[0]["content"])

    @property
    def first(self) -> str:
        return str(self.messages[1]["content"])

    @property
    def last_tool_results(self) -> list[str]:
        out: list[str] = []
        for message in reversed(self.messages):
            if message.get("role") != "tool":
                break
            out.append(str(message.get("content")))
        return list(reversed(out))


def tool_calls(calls: list[tuple[str, dict[str, Any]]], *, finish_reason: str = "tool_calls", prompt_tokens: int = 100, completion_tokens: int = 40) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCallData(id=f"c_{uuid.uuid4().hex[:8]}", name=name, arguments_json=json.dumps(args, ensure_ascii=False)) for name, args in calls],
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason=finish_reason,
        provider_id="fake",
        model_id="scripted",
        input_price_per_m=1.0,
        output_price_per_m=2.0,
    )


def text_answer(text: str, *, finish_reason: str = "stop") -> LLMResponse:
    return LLMResponse(content=text, prompt_tokens=50, completion_tokens=20, finish_reason=finish_reason, provider_id="fake", model_id="scripted")


class ScriptedLlm:
    """An ``LlmPort`` over a script.  ``script(call)`` returns the tool calls of the answer (``[]`` or ``None``: ``sleep``-less silence: a plain
    text answer) or a ``str`` (a plain text answer).  Every request is kept in ``requests``; ``delay`` makes the answer wait (on the event loop)
    so that the wall clock can run out; ``gate`` blocks every answer until it is set."""

    def __init__(
        self, script: Script, *, delay: float = 0.0, gate: asyncio.Event | None = None, fail_for: tuple[str, ...] = (), fail_text: str = "upstream exploded sk-secret-1234567"
    ) -> None:
        self.script = script
        self.delay = delay
        self.gate = gate
        self.fail_for = fail_for  # the agents (a seat, a bot id) whose requests raise ``RuntimeError(fail_text)``
        self.fail_text = fail_text
        self.requests: list[tuple[str, list[dict[str, Any]]]] = []
        self.counts: dict[str, int] = {}
        self.entered: list[str] = []
        self.in_flight = 0
        self.peak = 0

    async def complete(
        self, agent_id: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, *, max_tokens: int | None = None
    ) -> LLMResponse:
        self.counts[agent_id] = self.counts.get(agent_id, 0) + 1
        self.requests.append((agent_id, [dict(m) for m in messages]))
        self.entered.append(agent_id)
        if agent_id in self.fail_for:
            raise RuntimeError(self.fail_text)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            answer = self.script(Call(agent_id, messages, tools))
        finally:
            self.in_flight -= 1
        if isinstance(answer, str):
            return text_answer(answer)
        if not answer:
            return text_answer("（我没有要做的事。）")
        return tool_calls(answer)


def good_init(call: Call) -> list[tuple[str, dict[str, Any]]] | str:
    """A model that reads the sources, writes the three required files and the optional one, and finishes."""
    steps: list[list[tuple[str, dict[str, Any]]]] = [
        [("read_source", {"source": "contract", "part": "must_read"}), ("read_source", {"source": "rules"})],
        [("write_memory", {"file": "arch_notes", "content": ARCH_NOTES}), ("write_memory", {"file": "digest", "content": DIGEST})],
        [("write_memory", {"file": "playbook", "content": PLAYBOOK}), ("write_memory", {"file": "questions", "content": QUESTIONS})],
        [("finish", {"summary": "读完了，写好了。"})],
    ]
    return steps[min(call.index, len(steps) - 1)]


def good_review(call: Call) -> list[tuple[str, dict[str, Any]]] | str:
    """A model that looks at the record, writes the impressions and a better playbook, and finishes."""
    steps: list[list[tuple[str, dict[str, Any]]]] = [
        [("read_record", {"limit": 20})],
        [("write_memory", {"file": "people", "content": PEOPLE}), ("write_memory", {"file": "playbook", "content": REVIEW_PLAYBOOK})],
        [("finish", {"summary": "复盘完了。"})],
    ]
    return steps[min(call.index, len(steps) - 1)]


def by_kind(init: Script = good_init, review: Script = good_review) -> Script:
    """A script that plays the init script for an initialization and the review script for a review."""

    def script(call: Call) -> list[tuple[str, dict[str, Any]]] | str:
        return (review if call.kind == "review" else init)(call)

    return script


# ------------------------------------------------------------------------------------------------ a real bot store and the sources it is initialized from
class Bench:
    """Four real bots (the first four of the default library, over a temporary data directory), the small scenario, the default contract and the
    hashes of both - what an initialization or a review needs besides a model."""

    def __init__(self, data_dir: Any) -> None:
        from pathlib import Path

        import app.config as app_config
        from app.agents.contract import load_default_contract
        from app.bots.types import SourceHashes, sha256_text
        from app.storage.bots import BotStore
        from app.world.loader import parse_scenario_v2

        self.store = BotStore(Path(data_dir), seats=4)
        self.store.bootstrap()
        self.scenario_text = (Path(app_config.BASE_DIR) / "scenarios" / "minimal_test.md").read_text(encoding="utf-8")
        self.spec = parse_scenario_v2(self.scenario_text, where="minimal_test.md")
        self.scenario_id = self.spec.id
        self.contract = load_default_contract()
        self.hashes = SourceHashes(
            contract=self.contract.sha256, scenario=sha256_text(self.scenario_text), materials="", scenario_id=self.scenario_id
        )
        self.ids = self.store.lineup()

    def status(self, bot_id: str, hashes: Any = None) -> str:
        return str(self.store.summary(bot_id, sources=hashes or self.hashes)["init"]["status"])

    def memory(self, bot_id: str) -> dict[str, str]:
        return {part: self.store.read_part(bot_id, part, self.scenario_id) for part in ("arch_notes", "digest", "playbook", "questions", "people")}

    async def init(self, bot_id: str, llm: Any, **kwargs: Any) -> Any:
        """``run_init`` for ``bot_id`` with ``llm`` (the bench's own contract, scenario and hashes; ``kwargs`` override)."""
        from app.learning.init_runner import run_init

        params: dict[str, Any] = dict(
            store=self.store, bot_id=bot_id, scenario_id=self.scenario_id, spec=self.spec, contract=self.contract, materials={},
            sources=self.hashes, llm=llm, agent_id=bot_id, model="fake / scripted", deadline_s=5,
        )
        params.update(kwargs)
        return await run_init(**params)

    def ready(self, bot_id: str) -> None:
        """Initialize ``bot_id`` as a finished run would (no model)."""
        self.store.commit_init(
            bot_id, self.scenario_id, {"arch_notes": ARCH_NOTES, "digest": DIGEST, "playbook": PLAYBOOK}, model="fake", duration_s=0.1, sources=self.hashes
        )
