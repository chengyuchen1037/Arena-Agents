"""v0.12.1 price source (E12): parse the LiteLLM table, match models, fetch it safely, serve it by API."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as ns

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.providers.pricing import (
    DEFAULT_PRICE_SOURCE_URL,
    PriceStore,
    PriceTable,
    parse_price_table,
    provider_names_for,
    validate_source_url,
)
from sec_api_fakes import install

# A trimmed copy of the real file's shape (USD per *token*).
SOURCE = {
    "sample_spec": {"input_cost_per_token": 0.0, "output_cost_per_token": 0.0, "litellm_provider": "docs", "mode": "chat"},
    "deepseek-chat": {
        "input_cost_per_token": 2.8e-07, "output_cost_per_token": 4.2e-07, "cache_read_input_token_cost": 2.8e-08,
        "litellm_provider": "deepseek", "max_input_tokens": 131072, "mode": "chat",
    },
    "deepseek/deepseek-chat": {
        "input_cost_per_token": 2.8e-07, "output_cost_per_token": 4.2e-07, "cache_read_input_token_cost": 2.8e-08,
        "cache_creation_input_token_cost": 0.0, "litellm_provider": "deepseek", "max_input_tokens": 131072, "mode": "chat",
    },
    "deepseek-flash": {
        "input_cost_per_token": 3e-07, "output_cost_per_token": 1.2e-06, "cache_read_input_token_cost": 6e-09,
        "cache_creation_input_token_cost": 0.0, "litellm_provider": "deepseek", "max_input_tokens": 1000000, "mode": "chat",
    },
    "claude-sonnet-4-5": {
        "input_cost_per_token": 3e-06, "output_cost_per_token": 1.5e-05, "cache_read_input_token_cost": 3e-07,
        "cache_creation_input_token_cost": 3.75e-06, "litellm_provider": "anthropic", "max_input_tokens": 200000, "mode": "chat",
    },
    "claude-sonnet-4-5-20250929": {
        "input_cost_per_token": 3e-06, "output_cost_per_token": 1.5e-05, "litellm_provider": "anthropic", "max_tokens": 64000, "mode": "chat",
    },
    "gpt-4o": {"input_cost_per_token": 2.5e-06, "output_cost_per_token": 1e-05, "litellm_provider": "openai", "max_input_tokens": 128000, "mode": "chat"},
    "openrouter/acme/special-1": {"input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06, "litellm_provider": "openrouter", "mode": "chat"},
    "other/acme/special-1": {"input_cost_per_token": 5e-06, "output_cost_per_token": 9e-06, "litellm_provider": "other", "mode": "chat"},
    "only/one/unique-model": {"input_cost_per_token": 7e-06, "output_cost_per_token": 8e-06, "litellm_provider": "only", "mode": "chat"},
    "text-embedding-3": {"input_cost_per_token": 1e-07, "output_cost_per_token": 0.0, "litellm_provider": "openai", "mode": "embedding"},
    "dall-e": {"input_cost_per_token": 1e-07, "output_cost_per_token": 0.0, "litellm_provider": "openai", "mode": "image_generation"},
    "broken-price": {"input_cost_per_token": "cheap", "output_cost_per_token": 1e-06, "mode": "chat"},
    "negative": {"input_cost_per_token": -1, "output_cost_per_token": 1e-06, "mode": "chat"},
    "no-mode-is-chat": {"input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06},
}


@pytest.fixture
def table() -> PriceTable:
    return PriceTable(parse_price_table(SOURCE))


# ------------------------------------------------------------------------------------ parsing
def test_prices_are_converted_from_per_token_to_per_million():
    entry = parse_price_table(SOURCE)["claude-sonnet-4-5"]
    assert (entry.input_price_per_m, entry.output_price_per_m) == (3.0, 15.0)
    assert (entry.cache_read_price_per_m, entry.cache_write_price_per_m) == (0.3, 3.75)
    assert entry.context_window_tokens == 200000 and entry.provider == "anthropic"


def test_only_usable_chat_entries_are_kept():
    entries = parse_price_table(SOURCE)
    for skipped in ("sample_spec", "text-embedding-3", "dall-e", "broken-price", "negative"):
        assert skipped not in entries
    assert "no-mode-is-chat" in entries  # an entry without a mode is treated as a chat model


def test_missing_cache_prices_stay_empty_but_zero_is_a_real_price():
    entries = parse_price_table(SOURCE)
    assert entries["deepseek-chat"].cache_write_price_per_m is None
    assert entries["deepseek/deepseek-chat"].cache_write_price_per_m == 0.0
    assert entries["claude-sonnet-4-5-20250929"].cache_read_price_per_m is None


def test_the_context_window_falls_back_to_max_tokens():
    assert parse_price_table(SOURCE)["claude-sonnet-4-5-20250929"].context_window_tokens == 64000
    assert parse_price_table(SOURCE)["no-mode-is-chat"].context_window_tokens is None


@pytest.mark.parametrize("payload", [[], "x", 5, None])
def test_a_source_that_is_not_an_object_is_rejected(payload):
    with pytest.raises(ValueError):
        parse_price_table(payload)


# ------------------------------------------------------------------------------------ matching
def test_exact_provider_prefixed_date_and_case_insensitive_matches(table):
    assert table.match("deepseek-flash", ["deepseek"]).key == "deepseek-flash"
    assert table.match("gpt-4o", ["openai"]).key == "gpt-4o"
    assert table.match("DeepSeek-Chat", []).key == "deepseek-chat"
    assert table.match("claude-sonnet-4-5-20250929", ["anthropic"]).key == "claude-sonnet-4-5-20250929"  # exact date key wins
    assert table.match("claude-sonnet-4-5-20251001", ["anthropic"]).key == "claude-sonnet-4-5"  # unknown date: strip it
    assert table.match("gpt-4o-2024-08-06", ["openai"]).key == "gpt-4o"


def test_the_provider_of_the_account_decides_between_same_named_models(table):
    assert table.match("acme/special-1", ["openrouter"]).key == "openrouter/acme/special-1"
    assert table.match("acme/special-1", ["other"]).key == "other/acme/special-1"


def test_a_suffix_match_needs_to_be_unambiguous_without_a_provider_hint(table):
    assert table.match("unique-model", []).key == "only/one/unique-model"  # exactly one "…/unique-model" key
    assert table.match("only/one/unique-model", []).key == "only/one/unique-model"
    assert table.match("special-1", []) is None  # two candidates and no hint: no guess


def test_nothing_matches_an_unknown_model_or_an_empty_table(table):
    assert table.match("totally-unknown", ["deepseek"]) is None
    assert table.match("", ["deepseek"]) is None
    assert PriceTable({}).match("gpt-4o", ["openai"]) is None


def test_provider_guessing_by_host_and_protocol():
    assert provider_names_for("https://api.deepseek.com", "openai_chat") == ["deepseek"]
    assert provider_names_for("https://api.openai.com/v1", "openai_responses") == ["openai"]
    assert provider_names_for("https://api.anthropic.com", "anthropic_messages") == ["anthropic"]
    assert provider_names_for("https://gateway.example.com/v1", "openai_chat") == []
    assert provider_names_for("https://relay.example.com", "anthropic_messages") == ["anthropic"]
    assert provider_names_for("not a url", "openai_chat") == []


# ------------------------------------------------------------------------------------ the store
def public_resolver():
    async def resolve(host, port):
        return ["93.184.216.34"]

    return resolve


def transport_for(handler):
    return httpx.MockTransport(handler)


def ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=SOURCE)


def test_a_fresh_store_has_the_default_source_and_no_table(tmp_path):
    status = PriceStore(tmp_path).status()
    assert status == {"url": DEFAULT_PRICE_SOURCE_URL, "default_url": DEFAULT_PRICE_SOURCE_URL, "fetched_at": None, "entries": 0, "error": None}


def test_refresh_downloads_parses_saves_and_survives_a_restart(tmp_path):
    store = PriceStore(tmp_path)
    status = asyncio.run(store.refresh(transport=transport_for(ok_handler), resolver=public_resolver()))
    assert status["error"] is None and status["entries"] == len(parse_price_table(SOURCE)) and status["fetched_at"]

    provider = ns(base_url="https://api.deepseek.com", protocol="openai_chat")
    entry = store.match(provider, "deepseek-flash")
    assert entry is not None and entry.suggestion()["price_source"] == "litellm:deepseek-flash"
    assert entry.suggestion()["cache_read_price_per_m"] == 0.006 and entry.suggestion()["key"] == "deepseek-flash"

    again = PriceStore(tmp_path)  # a "restart": the reduced table is on disk
    assert again.status()["entries"] == status["entries"] and again.match(provider, "deepseek-flash").key == "deepseek-flash"
    raw = json.loads((tmp_path / "pricing.json").read_text(encoding="utf-8"))
    assert raw["schema"] == 1 and len(raw["entries"]["deepseek-flash"]) == 6


def test_the_request_goes_to_the_vetted_address_without_following_redirects(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})

    store = PriceStore(tmp_path)
    status = asyncio.run(store.refresh(transport=transport_for(handler), resolver=public_resolver()))
    assert "重定向" in status["error"] and status["entries"] == 0
    assert len(seen) == 1 and seen[0].url.host == "93.184.216.34" and seen[0].headers["host"] == "raw.githubusercontent.com"


@pytest.mark.parametrize(
    "handler,fragment",
    [
        (lambda r: httpx.Response(500), "HTTP 500"),
        (lambda r: httpx.Response(200, content=b"not json at all"), "JSON"),
        (lambda r: httpx.Response(200, json=[1, 2, 3]), "JSON 对象"),
        (lambda r: httpx.Response(200, json={"sample_spec": {}}), "没有可识别"),
        (lambda r: httpx.Response(200, headers={"content-length": str(50 * 1024 * 1024)}, content=b"{}"), "过大"),
    ],
)
def test_a_bad_download_keeps_the_old_table_and_reports_why(tmp_path, handler, fragment):
    store = PriceStore(tmp_path)
    asyncio.run(store.refresh(transport=transport_for(ok_handler), resolver=public_resolver()))
    before = store.status()["entries"]
    status = asyncio.run(store.refresh(transport=transport_for(handler), resolver=public_resolver()))
    assert fragment in status["error"] and status["entries"] == before  # the good table is still there
    assert "不是有效" not in status["error"] or "JSON" in status["error"]


def test_a_streamed_body_over_the_limit_is_cut_off(tmp_path, monkeypatch):
    monkeypatch.setattr("app.providers.pricing.MAX_SOURCE_BYTES", 100)
    status = asyncio.run(
        PriceStore(tmp_path).refresh(transport=transport_for(lambda r: httpx.Response(200, content=b"x" * 500)), resolver=public_resolver())
    )
    assert "过大" in status["error"]


def test_a_private_or_metadata_address_is_never_contacted(tmp_path):
    store = PriceStore(tmp_path)
    store.set_url("https://prices.internal.example/table.json")
    hits: list[int] = []

    async def private(host, port):
        return ["10.0.0.5"]

    status = asyncio.run(store.refresh(transport=transport_for(lambda r: hits.append(1) or httpx.Response(200, json=SOURCE)), resolver=private))
    assert hits == [] and "内网" in status["error"]

    async def metadata(host, port):
        return ["169.254.169.254"]

    status = asyncio.run(store.refresh(transport=transport_for(lambda r: hits.append(1) or httpx.Response(200, json=SOURCE)), resolver=metadata))
    assert hits == [] and "禁止" in status["error"]


@pytest.mark.parametrize(
    "url",
    ["http://example.com/p.json", "ftp://example.com/p.json", "https://user:pw@example.com/p.json", "https://example.com/p.json?x=1", "https://", "javascript:alert(1)", "https://ex ample.com/x", "https://example.com/" + "a" * 600],
)
def test_only_plain_https_urls_are_accepted_as_a_source(url):
    with pytest.raises(ValueError):
        validate_source_url(url)


def test_changing_the_url_forgets_the_old_table_and_an_empty_url_means_the_default(tmp_path):
    store = PriceStore(tmp_path)
    asyncio.run(store.refresh(transport=transport_for(ok_handler), resolver=public_resolver()))
    status = store.set_url("https://prices.example.com/models.json")
    assert status["url"] == "https://prices.example.com/models.json" and status["entries"] == 0 and status["fetched_at"] is None
    assert PriceStore(tmp_path).status()["url"] == "https://prices.example.com/models.json"
    assert store.set_url("")["url"] == DEFAULT_PRICE_SOURCE_URL


def test_a_corrupt_price_file_is_ignored_not_fatal(tmp_path):
    (tmp_path / "pricing.json").write_text("{not json", encoding="utf-8")
    assert PriceStore(tmp_path).status()["entries"] == 0
    (tmp_path / "pricing.json").write_text(json.dumps({"schema": 9, "entries": {}}), encoding="utf-8")
    assert PriceStore(tmp_path).status()["entries"] == 0


# ------------------------------------------------------------------------------------ the API
@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ARENA_DATA_DIR", str(tmp_path / "data"))
    from app.storage import clear_store_caches

    clear_store_caches()
    fake = install(monkeypatch)
    yield fake
    clear_store_caches()


@pytest.fixture
def client(env):
    return TestClient(app)


def seed_table(tmp_path_factory=None):
    from app.providers.pricing import get_price_store

    store = get_price_store()
    asyncio.run(store.refresh(transport=transport_for(ok_handler), resolver=public_resolver()))
    return store


def test_the_source_endpoints_report_set_and_refresh(client, monkeypatch):
    assert client.get("/api/pricing/source").json()["url"] == DEFAULT_PRICE_SOURCE_URL
    changed = client.put("/api/pricing/source", json={"url": "https://prices.example.com/m.json"})
    assert changed.status_code == 200 and changed.json()["url"] == "https://prices.example.com/m.json"
    assert client.put("/api/pricing/source", json={"url": "http://insecure.example.com/m.json"}).status_code == 400
    assert client.put("/api/pricing/source", json={"url": ""}).json()["url"] == DEFAULT_PRICE_SOURCE_URL

    # a failed download is a normal 200 with the reason in "error" (the old table stays)
    from app.providers import pricing

    async def failing(*args, **kwargs):
        raise pricing.DiscoveryError("无法连接到价格源，请检查网络或 URL。", kind="network")

    monkeypatch.setattr(pricing, "fetch_source", failing)
    refreshed = client.post("/api/pricing/refresh")
    assert refreshed.status_code == 200 and "无法连接" in refreshed.json()["error"]


def test_match_and_discovery_suggest_prices_once_the_table_is_there(client, env):
    provider = env.providers.seed(name="DeepSeek", base_url="https://api.deepseek.com", models=())
    assert client.get("/api/pricing/match", params={"provider_id": provider.id, "model_id": "deepseek-flash"}).json() == {"matched": False, "key": None}

    seed_table()
    body = client.get("/api/pricing/match", params={"provider_id": provider.id, "model_id": "deepseek-flash"}).json()
    assert body["matched"] is True and body["key"] == "deepseek-flash" and body["price_source"] == "litellm:deepseek-flash"
    assert (body["input_price_per_m"], body["output_price_per_m"], body["cache_read_price_per_m"], body["cache_write_price_per_m"]) == (0.3, 1.2, 0.006, 0.0)
    assert body["context_window_tokens"] == 1000000
    assert client.get("/api/pricing/match", params={"provider_id": provider.id, "model_id": "mystery"}).json()["matched"] is False
    assert client.get("/api/pricing/match", params={"provider_id": "ghost", "model_id": "x"}).status_code == 404

    env.discover.return_value = ["deepseek-flash", "mystery"]
    found = client.post(f"/api/providers/{provider.id}/discover-models").json()
    assert found["suggested"]["deepseek-flash"]["cache_read_price_per_m"] == 0.006 and found["suggested"]["mystery"] is None


def test_a_model_saved_with_the_suggested_prices_remembers_the_source_and_an_edit_makes_it_manual(client, env):
    provider = env.providers.seed(name="DeepSeek", base_url="https://api.deepseek.com", models=())
    seed_table()
    body = {"model_id": "deepseek-flash", "input_price_per_m": 0.3, "output_price_per_m": 1.2,
            "cache_read_price_per_m": 0.006, "cache_write_price_per_m": 0.0, "context_window_tokens": 1000000}
    added = client.post(f"/api/providers/{provider.id}/models", json=body)
    assert added.status_code == 201 and added.json()["price_source"] == "litellm:deepseek-flash"
    assert added.json()["cache_read_price_per_m"] == 0.006

    edited = client.patch(f"/api/providers/{provider.id}/models", json={"model_id": "deepseek-flash", "output_price_per_m": 2.0})
    assert edited.json()["output_price_per_m"] == 2.0 and edited.json()["price_source"] == "manual"
    restored = client.patch(f"/api/providers/{provider.id}/models", json={"model_id": "deepseek-flash", "output_price_per_m": 1.2})
    assert restored.json()["price_source"] == "litellm:deepseek-flash"  # exactly the table's numbers again


def test_the_cache_prices_can_be_cleared_with_null_and_left_alone_by_omitting_them(client, env):
    provider = env.providers.seed(models=())
    client.post(f"/api/providers/{provider.id}/models", json={"model_id": "m", "input_price_per_m": 1, "output_price_per_m": 2, "cache_read_price_per_m": 0.1})
    only_output = client.patch(f"/api/providers/{provider.id}/models", json={"model_id": "m", "output_price_per_m": 3})
    assert only_output.json()["cache_read_price_per_m"] == 0.1  # not sent: untouched
    cleared = client.patch(f"/api/providers/{provider.id}/models", json={"model_id": "m", "cache_read_price_per_m": None})
    assert cleared.json()["cache_read_price_per_m"] is None  # sent as null: back to "billed like input"
    assert client.patch(f"/api/providers/{provider.id}/models", json={"model_id": "m", "cache_read_price_per_m": -1}).status_code == 422
    assert client.patch(f"/api/providers/{provider.id}/models", json={"model_id": "m"}).status_code == 400  # nothing to change


def test_the_price_endpoints_that_change_things_need_the_token(client, monkeypatch):
    monkeypatch.setenv("ARENA_API_TOKEN", "s3cret-token-value")
    assert client.put("/api/pricing/source", json={"url": ""}).status_code == 401
    assert client.post("/api/pricing/refresh").status_code == 401
