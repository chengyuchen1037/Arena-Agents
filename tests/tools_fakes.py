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
