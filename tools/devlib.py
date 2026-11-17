"""Helpers shared by the development tools (``dev_server.py``, ``real_smoke.py``).

``apply_price_source`` downloads the LiteLLM price table (the default *price source* of the app) and writes
its prices into the model entries the given bindings use - in the data directory the process is configured
with (``ARENA_DATA_DIR``), i.e. in the *scratch copy* the dev tools work on, never in a user's real
workspace.  It is the same thing the Provider dialog's "刷新价格表" + "自动填入" do, so a development instance
shows the cost a provider really bills instead of whatever was typed into the registry by hand.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


async def apply_price_source(
    bindings: Iterable[Any],
    *,
    transport: Any = None,
    resolver: Any = None,
    allow_private: bool | None = None,
) -> list[str]:
    """Refresh the price table and price every model the ``bindings`` use with it.

    Returns printable report lines.  Never raises for a network / parsing problem (the existing prices simply
    stay): a development instance must still start offline.
    """
    from app.providers.pricing import get_price_store
    from app.providers.store import get_provider_store

    lines: list[str] = []
    prices = get_price_store()
    status = await prices.refresh(transport=transport, resolver=resolver, allow_private=allow_private)
    if status["error"]:
        return [f"price table: NOT updated ({status['error']}); the configured prices stay"]
    lines.append(f"price table: {status['entries']} models from {status['url']}")

    store = get_provider_store()
    done: set[tuple[str, str]] = set()
    for binding in bindings:
        provider_id, model_id = str(binding.provider_id), str(binding.model_id)
        if (provider_id, model_id) in done:
            continue
        done.add((provider_id, model_id))
        try:
            provider = store.get_provider(provider_id)
            match = prices.match(provider, model_id)
            if match is None:
                lines.append(f"  {model_id}: no price found in the table; the configured price stays")
                continue
            store.update_model(
                provider_id,
                model_id,
                input_price_per_m=match.input_price_per_m,
                output_price_per_m=match.output_price_per_m,
                cache_read_price_per_m=match.cache_read_price_per_m,
                cache_write_price_per_m=match.cache_write_price_per_m,
                price_source=f"litellm:{match.key}",
            )
        except Exception as exc:  # noqa: BLE001 - read-only providers, a missing model ...: report, go on
            lines.append(f"  {model_id}: not updated ({type(exc).__name__})")
            continue
        lines.append(
            f"  {model_id}: in ${match.input_price_per_m}/M, cache read ${match.cache_read_price_per_m}/M, "
            f"cache write ${match.cache_write_price_per_m}/M, out ${match.output_price_per_m}/M  (litellm:{match.key})"
        )
    return lines
