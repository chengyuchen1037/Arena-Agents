"""Fakes for the tests of the real-model tools (``tools/real_smoke.py``, ``tools/init_default_bots.py``): no model is ever called.

* :func:`make_source` - a data directory like an installation's (``--from``): a provider registry with its encrypted key and the default
  model bindings, saved by seat (v0.13) or by bot id (v0.14: with a table of bots and its line-up);
* :func:`tree` - every file below a directory, to prove that a tool left it alone;
* :func:`demo_factory` - the demo's scripted bots as the model port of the agents *and* of the learning runs (what a real model would be asked,
  answered with fixed tool calls), the port a tool test injects instead of the real models.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))  # the tools are plain scripts: ``import real_smoke`` works

SOURCE_KEY = "sk-source-key-123456"  # (a placeholder: never a real key)


def make_source(tmp_path: Path, *, by_bot_id: bool = False, seats: int = 12) -> Path:
    """A data directory like an installation's: one provider with the models ``big`` and ``small`` (its key stored encrypted), and default
    bindings for the moderator (``big``) and ``seats`` seats (seat 1 ``small``, the others ``big``).

    ``by_bot_id``: a v0.14 directory - it has a table of bots with a line-up and the bindings are saved by bot id (the settings store translates
    the seats into bots); else a v0.13 one (bindings saved by seat)."""
    from app.providers.models import ModelBinding
    from app.providers.store import ProviderStore
    from app.storage.bots import BotStore
    from app.storage.settings import SettingsStore

    source = tmp_path / "an-installation" / "data"
    store = ProviderStore(source)
    provider = store.create_provider("Fake Provider", "openai_chat", "https://example.invalid", SOURCE_KEY)
    store.add_model(provider.id, "big", 1.0, 2.0)
    store.add_model(provider.id, "small", 0.5, 1.0)
    if by_bot_id:
        BotStore(source, seats=seats).bootstrap()
    big = ModelBinding(provider_id=provider.id, model_id="big")
    small = ModelBinding(provider_id=provider.id, model_id="small")
    bindings = {"moderator": big, **{f"N{i}": (small if i == 1 else big) for i in range(1, seats + 1)}}
    SettingsStore(source).save_default_bindings(bindings)
    return source


def tree(root: Path) -> dict[str, bytes]:
    """``{relative path: content}`` of every file below ``root``."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file()}


def demo_factory(delay: float = 0.0, seed: int = 7) -> Callable[[Any], Any]:
    """``port_factory`` for a tool: the demo's scripted bots (they play the 12-player scenario, initialize and review with fixed answers, and
    complete an L0 on request).  The script state of one game survives a pause / resume of the same game."""
    from app.game.demo import DemoLlm

    cache: dict[int, DemoLlm] = {}

    def factory(orch: Any) -> DemoLlm:
        key = id(orch.world)
        if key not in cache:
            cache.clear()
            cache[key] = DemoLlm(orch.world, delay=delay, seed=seed)
        return cache[key]

    return factory


class Network(list):
    """The requests a faked network has seen (a list of ``{"moderator", "tools", "model", "provider"}``) and ``peak``: the most that were in flight at once."""

    inflight = 0
    peak = 0


def fake_network(monkeypatch: Any, *, ends_game_with: str | None = None, delay: float = 0.0) -> Network:
    """Fake only the NETWORK under the engine's own client (``LLMClient.chat_stream``): the production path of a tool - the budget around the client, the
    model port with its concurrency gate, the error log, the bindings of the bots and the seats - runs for real, and no request leaves the process.

    What comes back: the demo's fixed answers to a learning run (an initialization or a review: the tools have ``write_memory``) and to a one-shot call (no
    tools: "complete this L0"); the moderator ends the game at once (``ends_game_with`` is the winner) when it is asked to, every other agent goes to sleep.
    ``delay``: seconds every request takes (so that concurrent requests overlap and ``peak`` means something).  Returns the :class:`Network`."""
    import asyncio
    import json
    from types import SimpleNamespace as ns

    from app.engine.llm import LLMClient
    from app.game.demo import DemoLlm
    from app.llm.types import LLMResponse, ToolCallData

    demo = DemoLlm(ns(state=ns(players=[]), entries_visible=lambda who: []), seed=1, delay=0.0)
    seen = Network()
    ended = {"yes": False}  # (the game is ended once: the moderator's later calls go to sleep like everybody's)

    async def chat_stream(
        self: Any, messages: Any, *, is_moderator: bool = False, tools: Any = None, on_chunk: Any = None, max_tokens_override: Any = None,
        binding: Any = None, on_retry: Any = None,
    ) -> Any:
        names = [t.get("function", {}).get("name") for t in tools or []]
        seen.append({"moderator": is_moderator, "tools": names, "model": getattr(binding, "model_id", None), "provider": getattr(binding, "provider_id", None)})
        seen.inflight += 1
        seen.peak = max(seen.peak, seen.inflight)
        try:
            if delay:
                await asyncio.sleep(delay)
        finally:
            seen.inflight -= 1
        if tools is None or "write_memory" in names:  # a one-shot call, a learning run
            return await demo.complete("a-bot", messages, tools, max_tokens=max_tokens_override)
        name, args = ("sleep", {})
        if is_moderator and ends_game_with and not ended["yes"]:
            ended["yes"] = True
            name, args = "end_game", {"winner": ends_game_with, "summary": "测试"}
        return LLMResponse(
            thought="",
            content="",
            tool_calls=[ToolCallData(id=f"call_{len(seen)}", name=name, arguments_json=json.dumps(args, ensure_ascii=False))],
            finish_reason="tool_calls",
            prompt_tokens=100,
            cache_read_tokens=400,
            completion_tokens=20,
            provider_id=getattr(binding, "provider_id", "p"),
            model_id=getattr(binding, "model_id", "m"),
        )

    monkeypatch.setattr(LLMClient, "chat_stream", chat_stream)
    return seen
