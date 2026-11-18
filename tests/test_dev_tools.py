"""The development tools' shared helper (tools/devlib.py): price a scratch copy of the providers with the price table."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace as ns

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import devlib  # noqa: E402
from app.providers.store import get_provider_store  # noqa: E402

TABLE = {
    "deepseek-flash": {
        "input_cost_per_token": 3e-07, "output_cost_per_token": 1.2e-06, "cache_read_input_token_cost": 6e-09,
        "cache_creation_input_token_cost": 0.0, "litellm_provider": "deepseek", "max_input_tokens": 1000000, "mode": "chat",
    },
}


async def public(host, port):
    return ["93.184.216.34"]


def make_provider():
    store = get_provider_store()
    provider = store.create_provider("DeepSeek", "openai_chat", "https://api.deepseek.com", "k")
    store.add_model(provider.id, "deepseek-flash", 5.0, 20.0)  # typed in by hand: far above the real price
    store.add_model(provider.id, "mystery", 1.0, 2.0)
    return provider


def binding(provider, model):
    return ns(provider_id=provider.id, model_id=model)


def run(bindings, handler=lambda r: httpx.Response(200, json=TABLE)):
    return asyncio.run(devlib.apply_price_source(bindings, transport=httpx.MockTransport(handler), resolver=public))


def test_the_bound_models_are_priced_with_the_table_and_remember_where_it_came_from():
    provider = make_provider()
    lines = run([binding(provider, "deepseek-flash"), binding(provider, "deepseek-flash"), binding(provider, "mystery")])
    model = {m.id: m for m in get_provider_store().get_provider(provider.id).models}
    flash = model["deepseek-flash"]
    assert (flash.input_price_per_m, flash.output_price_per_m, flash.cache_read_price_per_m, flash.cache_write_price_per_m) == (0.3, 1.2, 0.006, 0.0)
    assert flash.price_source == "litellm:deepseek-flash"
    assert (model["mystery"].input_price_per_m, model["mystery"].price_source) == (1.0, "manual")  # no match: left alone
    assert lines[0].startswith("price table: 1 models") and sum("deepseek-flash:" in line for line in lines) == 1  # once, not per binding
    assert any("no price found" in line and "mystery" in line for line in lines)


def test_a_failed_download_changes_nothing_and_does_not_raise():
    provider = make_provider()
    lines = run([binding(provider, "deepseek-flash")], handler=lambda r: httpx.Response(500))
    assert len(lines) == 1 and "NOT updated" in lines[0] and "HTTP 500" in lines[0]
    assert get_provider_store().get_provider(provider.id).models[0].input_price_per_m == 5.0


def test_a_binding_to_a_missing_provider_or_model_is_reported_not_fatal():
    provider = make_provider()
    lines = run([ns(provider_id="ghost", model_id="x"), binding(provider, "gone"), binding(provider, "deepseek-flash")])
    assert any("not updated" in line and "x:" in line for line in lines) and any("gone" in line for line in lines)
    assert get_provider_store().get_provider(provider.id).models[0].price_source == "litellm:deepseek-flash"
