"""Provider / model / binding / session-list API tests (v0.10) with v0.9 guards kept."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api import providers as provider_api
from app.api import security
from app.main import app
from app.providers.models import ProviderError
from sec_api_fakes import SECRET_KEY, StorageError, bind, install

TOKEN = "s3cret-token-value"
NEW_KEY = "sk-new-rotated-key-98765"
PROVIDER_KEYS = {
    "id",
    "name",
    "protocol",
    "base_url",
    "models",
    "read_only",
    "api_key_set",
    "auto_append_path",
}
MODEL_KEYS = {
    "id",
    "input_price_per_m",
    "output_price_per_m",
    "cache_read_price_per_m",
    "cache_write_price_per_m",
    "context_window_tokens",
    "price_source",
}
ENV_NAMES = (
    "ARENA_API_TOKEN",
    "ARENA_ALLOWED_ORIGINS",
    "ARENA_ALLOWED_BASE_HOSTS",
    "ARENA_ALLOW_PRIVATE_PROVIDERS",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def env(monkeypatch):
    return install(monkeypatch)


@pytest.fixture
def client(env):
    return TestClient(app)


@pytest.fixture
def remote(env):
    return TestClient(app, client=("203.0.113.5", 50000))


def detail(response) -> str:
    body = response.json()
    assert set(body) == {"detail"}
    assert isinstance(body["detail"], str) and body["detail"]
    return body["detail"]


def create_body(**overrides):
    body = {
        "name": "My Provider",
        "protocol": "openai_chat",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-test-key-0001",
    }
    body.update(overrides)
    return body


def all_text(*responses) -> str:
    return "\n".join(r.text for r in responses)


# ------------------------------------------------------------ guards
@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/api/providers", None),
        ("post", "/api/providers", create_body()),
        ("patch", "/api/providers/x", {"name": "n"}),
        ("delete", "/api/providers/x", None),
        ("post", "/api/providers/x/discover-models", None),
        ("post", "/api/providers/endpoint-preview", {"protocol": "openai_chat", "base_url": "https://api.example.com"}),
        ("post", "/api/providers/x/models", {"model_id": "m", "input_price_per_m": 1, "output_price_per_m": 1}),
        ("get", "/api/settings/bindings", None),
        ("put", "/api/settings/bindings", {"bindings": {}}),
        ("get", "/api/settings/templates", None),
        ("put", "/api/settings/templates", {"templates": []}),
        ("post", "/api/settings/templates/1/apply", {"actor_ids": ["moderator"]}),
    ],
)
def test_every_new_endpoint_requires_the_token_when_configured(client, monkeypatch, method, path, body):
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    response = client.request(method.upper(), path, json=body)
    assert response.status_code == 401
    detail(response)


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/providers", create_body()),
        ("patch", "/api/providers/x", {"name": "n"}),
        ("delete", "/api/providers/x", None),
        ("post", "/api/providers/x/discover-models", None),
        ("post", "/api/providers/x/models", {"model_id": "m", "input_price_per_m": 1, "output_price_per_m": 1}),
        ("patch", "/api/providers/x/models", {"model_id": "m", "input_price_per_m": 1}),
        ("delete", "/api/providers/x/models", {"model_id": "m"}),
        ("put", "/api/settings/bindings", {"bindings": {}}),
        ("put", "/api/settings/templates", {"templates": []}),
        ("post", "/api/settings/templates/1/apply", {"actor_ids": ["moderator"]}),
    ],
)
def test_writes_are_loopback_only_without_token(remote, env, method, path, body):
    response = remote.request(method.upper(), path, json=body)
    assert response.status_code == 403
    detail(response)
    assert env.providers.calls == []
    env.discover.assert_not_awaited()


def test_reads_stay_open_for_remote_clients_without_token(remote):
    assert remote.get("/api/providers").status_code == 200
    assert remote.get("/api/settings/bindings").status_code == 200


def test_cross_origin_writes_are_rejected_even_with_token(client, env, monkeypatch):
    monkeypatch.setenv("ARENA_API_TOKEN", TOKEN)
    response = client.post(
        "/api/providers",
        json=create_body(),
        headers={"X-Arena-Token": TOKEN, "Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert env.providers.providers == {}


# --------------------------------------------------- provider creation
def test_create_returns_whitelisted_fields_and_never_the_key(client, env):
    response = client.post("/api/providers", json=create_body(api_key=NEW_KEY))
    assert response.status_code == 201
    body = response.json()
    assert set(body) == PROVIDER_KEYS
    assert body["api_key_set"] is True
    assert body["models"] == []
    assert body["read_only"] is False
    assert NEW_KEY not in response.text
    listing = client.get("/api/providers")
    assert NEW_KEY not in listing.text
    assert [set(p) for p in listing.json()["providers"]] == [PROVIDER_KEYS]
    assert env.providers.providers[body["id"]].api_key == NEW_KEY


def test_list_never_leaks_keys_of_existing_providers(client, env):
    env.providers.seed(models=("a", "b"))
    env.providers.seed(read_only=True, name="env", models=("c",))
    response = client.get("/api/providers")
    assert SECRET_KEY not in response.text
    for provider in response.json()["providers"]:
        assert set(provider) == PROVIDER_KEYS
        assert provider["api_key_set"] is True
        for model in provider["models"]:
            assert set(model) == MODEL_KEYS


@pytest.mark.parametrize(
    "overrides",
    [
        {"protocol": "grpc"},
        {"protocol": ""},
        {"extra_field": "x"},
        {"name": 5},
    ],
)
def test_create_rejects_bad_shape_with_422(client, env, overrides):
    response = client.post("/api/providers", json=create_body(**overrides))
    assert response.status_code == 422
    detail(response)
    assert env.providers.calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"name": "   "},
        {"name": "a\nb"},
        {"name": "x" * 101},
        {"api_key": ""},
        {"api_key": "   "},
        {"api_key": "sk-a\nINJECT=1"},
        {"api_key": "sk with space"},
        {"api_key": "密钥-key"},
        {"api_key": "k" * 3000},
    ],
)
def test_create_rejects_unsafe_name_or_key_with_400(client, env, overrides):
    response = client.post("/api/providers", json=create_body(**overrides))
    assert response.status_code == 400
    detail(response)
    assert env.providers.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "ftp://api.example.com",
        "file:///etc/passwd",
        "//api.example.com",
        "api.example.com",
        "https://user:pw@api.example.com/",
        "https://api.example.com@evil.example/",
        "https://api.example.com/#frag",
        "https://api.example.com/v1?api-key=leak",
        "https://api.example.com/a b",
        "https://api.example.com/\nHost: evil",
        "",
        "https://",
        "https://api.example.com:notaport/",
        "http://169.254.169.254/latest/meta-data",
        "http://[fd00:ec2::254]/",
        "http://[fe80::1]/",
        "http://metadata.google.internal/",
        "http://0.0.0.0:8000",
        "http://2130706433/",
        "http://0x7f.1/",
        "http://localhost:11434/v1",
        "http://127.0.0.1:11434/v1",
        "http://[::1]:11434/v1",
        "http://192.168.1.20:1234/v1",
        "http://10.0.0.5/v1",
        "http://ollama:11434/v1",
        "http://host.docker.internal:11434/v1",
        "http://printer.local/v1",
    ],
)
def test_create_rejects_dangerous_base_urls(client, env, url):
    response = client.post("/api/providers", json=create_body(base_url=url))
    assert response.status_code == 400, url
    detail(response)
    assert env.providers.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:1234/v1",
        "http://192.168.1.20:1234/v1",
        "http://host.docker.internal:11434/v1",
    ],
)
def test_private_hosts_need_the_explicit_admin_switch(client, env, monkeypatch, url):
    monkeypatch.setenv("ARENA_ALLOW_PRIVATE_PROVIDERS", "true")
    assert client.post("/api/providers", json=create_body(base_url=url)).status_code == 201


@pytest.mark.parametrize(
    "url", ["http://169.254.169.254/", "http://metadata.google.internal/", "http://2130706433/"]
)
def test_metadata_addresses_stay_blocked_even_with_private_switch(client, monkeypatch, url):
    monkeypatch.setenv("ARENA_ALLOW_PRIVATE_PROVIDERS", "true")
    assert client.post("/api/providers", json=create_body(base_url=url)).status_code == 400


@pytest.mark.parametrize(
    "url",
    ["https://api.deepseek.com", "https://api.openai.com/v1", "https://api.anthropic.com", "http://8.8.8.8/v1", "https://llm.corp.example:8443/v1"],
)
def test_public_hosts_are_allowed_by_default(client, url):
    assert client.post("/api/providers", json=create_body(base_url=url)).status_code == 201


def test_allowed_base_hosts_acts_as_allowlist_when_set(client, monkeypatch):
    monkeypatch.setenv("ARENA_ALLOWED_BASE_HOSTS", "api.deepseek.com, llm.corp.example")
    ok = client.post("/api/providers", json=create_body(base_url="https://llm.corp.example/v1"))
    assert ok.status_code == 201
    bad = client.post("/api/providers", json=create_body(base_url="https://api.openai.com/v1"))
    assert bad.status_code == 400
    assert "ARENA_ALLOWED_BASE_HOSTS" in detail(bad)
    assert client.post(
        "/api/providers", json=create_body(base_url="https://api.deepseek.com.evil.example/v1")
    ).status_code == 400


def test_store_errors_map_to_clean_http_errors(client, env, monkeypatch):
    def boom(*args, **kwargs):
        raise StorageError("主密钥不可用")

    monkeypatch.setattr(env.providers, "create_provider", boom)
    response = client.post("/api/providers", json=create_body())
    assert response.status_code == 503
    assert "主密钥" in detail(response)

    def refuse(*args, **kwargs):
        raise ProviderError("名称重复")

    monkeypatch.setattr(env.providers, "create_provider", refuse)
    response = client.post("/api/providers", json=create_body())
    assert response.status_code == 400
    assert detail(response) == "名称重复"


# --------------------------------------------------------- provider edit
def test_edit_name_only_needs_no_key(client, env):
    provider = env.providers.seed()
    response = client.patch(f"/api/providers/{provider.id}", json={"name": "Renamed"})
    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"
    assert SECRET_KEY not in response.text
    assert env.providers.providers[provider.id].api_key == SECRET_KEY  # untouched


@pytest.mark.parametrize(
    "change",
    [
        {"base_url": "https://evil.example.com/v1"},
        {"protocol": "anthropic_messages"},
        {"auto_append_path": False},
        {"base_url": "https://evil.example.com/v1", "api_key": ""},
        {"protocol": "openai_responses", "api_key": "   "},
        {"auto_append_path": False, "api_key": ""},
    ],
)
def test_changing_base_url_or_protocol_requires_a_fresh_key(client, env, change):
    provider = env.providers.seed()
    response = client.patch(f"/api/providers/{provider.id}", json=change)
    assert response.status_code == 400
    assert "API Key" in detail(response)
    stored = env.providers.providers[provider.id]
    assert stored.base_url == "https://api.example.com/v1"
    assert stored.protocol == "openai_chat"
    assert stored.api_key == SECRET_KEY
    assert all(call[0] != "update_provider" for call in env.providers.calls)


def test_redirecting_with_a_fresh_key_is_allowed_and_validated(client, env):
    provider = env.providers.seed()
    response = client.patch(
        f"/api/providers/{provider.id}",
        json={"base_url": "https://other.example.com/v1", "api_key": NEW_KEY, "protocol": "openai_responses"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["base_url"] == "https://other.example.com/v1"
    assert body["protocol"] == "openai_responses"
    assert NEW_KEY not in response.text
    bad = client.patch(
        f"/api/providers/{provider.id}",
        json={"base_url": "http://169.254.169.254/", "api_key": NEW_KEY},
    )
    assert bad.status_code == 400


def test_same_base_url_and_protocol_need_no_key(client, env):
    provider = env.providers.seed()
    response = client.patch(
        f"/api/providers/{provider.id}",
        json={"base_url": provider.base_url, "protocol": provider.protocol, "name": "n2"},
    )
    assert response.status_code == 200


def test_key_rotation_alone_is_allowed(client, env):
    provider = env.providers.seed()
    response = client.patch(f"/api/providers/{provider.id}", json={"api_key": NEW_KEY})
    assert response.status_code == 200
    assert env.providers.providers[provider.id].api_key == NEW_KEY
    assert NEW_KEY not in response.text


def test_edit_rejects_control_chars_in_key(client, env):
    provider = env.providers.seed()
    response = client.patch(f"/api/providers/{provider.id}", json={"api_key": "a\r\nb"})
    assert response.status_code == 400


def test_running_game_locks_protocol_and_url_of_used_providers_but_not_key_rotation(client, env):
    used = env.providers.seed(models=("m1",))
    unused = env.providers.seed(models=("m1",), name="Unused")
    env.session.model_bindings = {"moderator": bind(used.id)}
    env.session.status = "running"

    locked = client.patch(
        f"/api/providers/{used.id}", json={"base_url": "https://other.example.com", "api_key": NEW_KEY}
    )
    assert locked.status_code == 409
    detail(locked)
    locked = client.patch(
        f"/api/providers/{used.id}", json={"protocol": "anthropic_messages", "api_key": NEW_KEY}
    )
    assert locked.status_code == 409
    locked = client.patch(
        f"/api/providers/{used.id}", json={"auto_append_path": False, "api_key": NEW_KEY}
    )
    assert locked.status_code == 409
    assert "自动拼接" in detail(locked)
    assert env.providers.providers[used.id].auto_append_path is True

    assert client.patch(f"/api/providers/{used.id}", json={"api_key": NEW_KEY}).status_code == 200
    assert client.patch(f"/api/providers/{used.id}", json={"name": "ok"}).status_code == 200
    free = client.patch(
        f"/api/providers/{unused.id}", json={"base_url": "https://other.example.com", "api_key": NEW_KEY}
    )
    assert free.status_code == 200


def test_read_only_providers_cannot_be_modified(client, env):
    provider = env.providers.seed(read_only=True, models=("m1",))
    for response in (
        client.patch(f"/api/providers/{provider.id}", json={"name": "x"}),
        client.delete(f"/api/providers/{provider.id}"),
        client.post(
            f"/api/providers/{provider.id}/models",
            json={"model_id": "new", "input_price_per_m": 1, "output_price_per_m": 1},
        ),
        client.patch(f"/api/providers/{provider.id}/models", json={"model_id": "m1", "input_price_per_m": 5}),
        client.request("DELETE", f"/api/providers/{provider.id}/models", json={"model_id": "m1"}),
    ):
        assert response.status_code == 403
        detail(response)
    assert provider.id in env.providers.providers
    assert env.providers.calls == []


def test_unknown_provider_is_404(client):
    assert client.patch("/api/providers/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/providers/nope").status_code == 404
    assert client.post("/api/providers/nope/discover-models").status_code == 404


# ------------------------------------------------------ provider delete
def test_delete_unreferenced_provider(client, env):
    provider = env.providers.seed()
    assert client.delete(f"/api/providers/{provider.id}").json() == {"ok": True}
    assert provider.id not in env.providers.providers


def test_delete_provider_referenced_by_session_or_defaults_is_409(client, env):
    in_session = env.providers.seed(name="S")
    in_defaults = env.providers.seed(name="D")
    env.session.model_bindings = {"p01": bind(in_session.id)}
    env.defaults["moderator"] = bind(in_defaults.id)
    for provider in (in_session, in_defaults):
        response = client.delete(f"/api/providers/{provider.id}")
        assert response.status_code == 409
        assert "绑定" in detail(response)
        assert provider.id in env.providers.providers


# ------------------------------------------------------ model discovery
def test_discover_models_returns_names_and_marks_existing(client, env):
    provider = env.providers.seed(models=("m1",))
    response = client.post(f"/api/providers/{provider.id}/discover-models")
    assert response.status_code == 200
    assert response.json() == {
        "models": ["m1", "m2"],
        "existing_models": ["m1"],
        "suggested": {"m1": None, "m2": None},  # no price table has been downloaded in this data dir
    }
    env.discover.assert_awaited_once()
    assert env.discover.await_args.args[0].id == provider.id


def test_discover_works_for_read_only_provider(client, env):
    provider = env.providers.seed(read_only=True)
    assert client.post(f"/api/providers/{provider.id}/discover-models").status_code == 200


def test_discover_errors_are_sanitised(client, env):
    provider = env.providers.seed()
    env.discover.side_effect = RuntimeError(
        f"HTTP 401 from upstream for Bearer {SECRET_KEY} and key={SECRET_KEY}"
    )
    response = client.post(f"/api/providers/{provider.id}/discover-models")
    assert response.status_code == 502
    assert SECRET_KEY not in response.text
    detail(response)

    env.discover.side_effect = ProviderError(f"blocked address; token {SECRET_KEY}")
    response = client.post(f"/api/providers/{provider.id}/discover-models")
    assert response.status_code == 400
    assert SECRET_KEY not in response.text


@pytest.mark.parametrize(
    "kind,status",
    [
        ("invalid_url", 400),
        ("blocked", 400),
        ("manual_endpoint", 400),
        ("auth", 502),
        ("status", 502),
        ("timeout", 504),
        ("too_large", 502),
    ],
)
def test_discovery_error_kinds_map_to_statuses(client, env, kind, status):
    from app.llm.discovery import DiscoveryError

    provider = env.providers.seed()
    env.discover.side_effect = DiscoveryError(f"失败 {SECRET_KEY}", kind=kind)
    response = client.post(f"/api/providers/{provider.id}/discover-models")
    assert response.status_code == status
    assert SECRET_KEY not in response.text
    detail(response)


# --------------------------------------- auto_append_path / endpoint preview
def test_create_provider_defaults_to_auto_append_path_true(client, env):
    response = client.post("/api/providers", json=create_body())
    assert response.status_code == 201
    body = response.json()
    assert body["auto_append_path"] is True
    assert env.providers.providers[body["id"]].auto_append_path is True
    assert env.providers.calls[0][2]["auto_append_path"] is True


def test_auto_append_path_accepts_roots_and_rejects_complete_endpoints(client, env):
    # In auto mode a complete endpoint URL is a configuration error: the user must disable it.
    complete = client.post(
        "/api/providers",
        json=create_body(base_url="https://api.example.com/v1/chat/completions"),
    )
    assert complete.status_code == 400
    assert "自动拼接" in detail(complete)
    assert env.providers.providers == {}

    # ... in manual mode that same URL is exactly the message endpoint sent verbatim.
    manual = client.post(
        "/api/providers",
        json=create_body(
            base_url="https://api.example.com/custom/chat/completions?api-version=2024-10-21",
            auto_append_path=False,
        ),
    )
    assert manual.status_code == 201
    body = manual.json()
    assert body["auto_append_path"] is False
    stored = env.providers.providers[body["id"]]
    assert stored.base_url == "https://api.example.com/custom/chat/completions?api-version=2024-10-21"
    assert stored.auto_append_path is False


def test_endpoint_preview_shows_final_urls_for_both_modes(client, env):
    auto = client.post(
        "/api/providers/endpoint-preview",
        json={"protocol": "openai_chat", "base_url": "https://api.example.com", "auto_append_path": True},
    )
    assert auto.status_code == 200
    assert auto.json() == {
        "auto_append_path": True,
        "request_url": "https://api.example.com/v1/chat/completions",
        "models_url": "https://api.example.com/v1/models",
        "warnings": [],
    }
    # A base URL already ending in /v1 is not doubled.
    with_v1 = client.post(
        "/api/providers/endpoint-preview",
        json={"protocol": "anthropic_messages", "base_url": "https://api.anthropic.com/v1", "auto_append_path": True},
    )
    assert with_v1.json()["request_url"] == "https://api.anthropic.com/v1/messages"

    # Manual endpoint: the base URL is the request URL, and discovery is impossible.
    manual = client.post(
        "/api/providers/endpoint-preview",
        json={
            "protocol": "openai_chat",
            "base_url": "https://api.example.com/custom/chat/completions",
            "auto_append_path": False,
        },
    )
    assert manual.status_code == 200
    payload = manual.json()
    assert payload["auto_append_path"] is False
    assert payload["request_url"] == "https://api.example.com/custom/chat/completions"
    assert payload["models_url"] is None
    assert isinstance(payload["warnings"], list)


@pytest.mark.parametrize(
    "body",
    [
        # auto mode rejects an already-complete endpoint URL
        {"protocol": "openai_chat", "base_url": "https://api.example.com/v1/chat/completions", "auto_append_path": True},
        # unsafe / unsupported addresses surface as 400 while typing
        {"protocol": "openai_chat", "base_url": "ftp://api.example.com", "auto_append_path": True},
        {"protocol": "openai_chat", "base_url": "http://localhost:11434", "auto_append_path": True},
        {"protocol": "openai_chat", "base_url": "http://169.254.169.254", "auto_append_path": True},
        {"protocol": "openai_chat", "base_url": "https://api.example.com", "auto_append_path": "maybe"},
    ],
)
def test_endpoint_preview_rejects_invalid_input_without_echoing_it(client, env, body):
    response = client.post("/api/providers/endpoint-preview", json=body)
    assert response.status_code in (400, 422)
    assert "sk-" not in detail(response)


def test_preview_and_auto_append_patch_round_trip(client, env):
    provider = env.providers.seed(base_url="https://api.example.com/v1")
    response = client.patch(
        f"/api/providers/{provider.id}",
        json={"auto_append_path": False, "api_key": NEW_KEY},
    )
    assert response.status_code == 200
    assert response.json()["auto_append_path"] is False
    assert env.providers.providers[provider.id].api_key == NEW_KEY
    assert NEW_KEY not in response.text


def test_patch_back_to_auto_append_rejects_complete_endpoint(client, env):
    provider = env.providers.seed(
        auto_append_path=False, base_url="https://api.example.com/v1/chat/completions"
    )
    response = client.patch(
        f"/api/providers/{provider.id}", json={"auto_append_path": True, "api_key": NEW_KEY}
    )
    assert response.status_code == 400
    assert "自动拼接" in detail(response)
    assert env.providers.providers[provider.id].auto_append_path is False


def test_discover_is_refused_for_manual_endpoint_before_any_network_use(client, env):
    provider = env.providers.seed(
        auto_append_path=False, base_url="https://api.example.com/v1/chat/completions"
    )
    response = client.post(f"/api/providers/{provider.id}/discover-models")
    assert response.status_code == 400
    assert "自动拼接" in detail(response)
    env.discover.assert_not_awaited()


# ------------------------------------------------------------- models
def model_body(**overrides):
    body = {"model_id": "gpt-x", "input_price_per_m": 1.5, "output_price_per_m": 6.0}
    body.update(overrides)
    return body


def test_add_model_one_at_a_time_with_required_prices(client, env):
    provider = env.providers.seed(models=())
    response = client.post(f"/api/providers/{provider.id}/models", json=model_body(context_window_tokens=128000))
    assert response.status_code == 201
    assert response.json() == {
        "id": "gpt-x",
        "input_price_per_m": 1.5,
        "output_price_per_m": 6.0,
        "cache_read_price_per_m": None,
        "cache_write_price_per_m": None,
        "context_window_tokens": 128000,
        "price_source": "manual",
    }
    assert [m.id for m in env.providers.providers[provider.id].models] == ["gpt-x"]


def test_model_ids_with_slashes_and_colons_are_fine(client, env):
    provider = env.providers.seed(models=())
    for model_id in ("models/gemini-1.5-pro", "qwen:7b", "org/name:tag"):
        assert client.post(
            f"/api/providers/{provider.id}/models", json=model_body(model_id=model_id)
        ).status_code == 201


@pytest.mark.parametrize(
    "overrides,missing",
    [
        ({}, "input_price_per_m"),
        ({}, "output_price_per_m"),
    ],
)
def test_prices_are_mandatory_no_default(client, env, overrides, missing):
    provider = env.providers.seed(models=())
    body = model_body()
    del body[missing]
    response = client.post(f"/api/providers/{provider.id}/models", json=body)
    assert response.status_code == 422
    assert missing in detail(response)
    assert env.providers.calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"input_price_per_m": -1},
        {"output_price_per_m": -0.01},
        {"input_price_per_m": None},
        {"output_price_per_m": "free"},
        {"input_price_per_m": 1e12},
        {"context_window_tokens": 10},
        {"model_id": ["a", "b"]},  # no batch add
    ],
)
def test_invalid_prices_and_batches_are_rejected(client, env, overrides):
    provider = env.providers.seed(models=())
    response = client.post(f"/api/providers/{provider.id}/models", json=model_body(**overrides))
    assert response.status_code == 422
    assert env.providers.calls == []


@pytest.mark.parametrize("raw", [b'{"model_id":"m","input_price_per_m":NaN,"output_price_per_m":1}', b'{"model_id":"m","input_price_per_m":Infinity,"output_price_per_m":1}'])
def test_nan_and_infinite_prices_are_rejected(client, env, raw):
    provider = env.providers.seed(models=())
    response = client.post(
        f"/api/providers/{provider.id}/models", content=raw, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert env.providers.calls == []


@pytest.mark.parametrize("model_id", ["", "  ", "a\nb", "x" * 201])
def test_bad_model_ids_are_400(client, env, model_id):
    provider = env.providers.seed(models=())
    response = client.post(f"/api/providers/{provider.id}/models", json=model_body(model_id=model_id))
    assert response.status_code == 400
    assert env.providers.calls == []


def test_duplicate_model_is_409_telling_to_edit(client, env):
    provider = env.providers.seed(models=("gpt-x",))
    response = client.post(f"/api/providers/{provider.id}/models", json=model_body())
    assert response.status_code == 409
    assert "编辑" in detail(response)


def test_edit_model_price_even_while_running(client, env):
    provider = env.providers.seed(models=("m1",))
    env.session.model_bindings = {"moderator": bind(provider.id)}
    env.session.status = "running"
    response = client.patch(
        f"/api/providers/{provider.id}/models", json={"model_id": "m1", "input_price_per_m": 9.0}
    )
    assert response.status_code == 200
    assert response.json()["input_price_per_m"] == 9.0
    assert response.json()["output_price_per_m"] == 2.0


def test_edit_model_validation(client, env):
    provider = env.providers.seed(models=("m1",))
    url = f"/api/providers/{provider.id}/models"
    assert client.patch(url, json={"model_id": "m1"}).status_code == 400  # nothing to change
    assert client.patch(url, json={"model_id": "ghost", "input_price_per_m": 1}).status_code == 404
    assert client.patch(url, json={"model_id": "m1", "input_price_per_m": -1}).status_code == 422
    assert client.patch(url, json={"model_id": "m1", "output_price_per_m": 1e12}).status_code == 422


def test_remove_model_with_body_and_in_use_protection(client, env):
    provider = env.providers.seed(models=("m1", "models/a/b"))
    env.session.model_bindings = {"p01": bind(provider.id, "m1")}
    url = f"/api/providers/{provider.id}/models"

    in_use = client.request("DELETE", url, json={"model_id": "m1"})
    assert in_use.status_code == 409
    detail(in_use)

    assert client.request("DELETE", url, json={"model_id": "models/a/b"}).json() == {"ok": True}
    assert [m.id for m in env.providers.providers[provider.id].models] == ["m1"]
    assert client.request("DELETE", url, json={"model_id": "ghost"}).status_code == 404

    env.session.model_bindings = {}
    env.defaults["p02"] = bind(provider.id, "m1")
    assert client.request("DELETE", url, json={"model_id": "m1"}).status_code == 409


# ----------------------------------------------------------- bindings
def test_get_bindings_shape_and_issues(client, env):
    provider = env.providers.seed(models=("m1",))
    env.session.model_bindings = {"moderator": bind(provider.id, "m1", "xhigh")}
    body = client.get("/api/settings/bindings").json()
    assert body["bindings"] == {
        "moderator": {
            "provider_id": provider.id,
            "model_id": "m1",
            "reasoning_depth": "xhigh",
            "context_window_tokens": 1_000_000,
        }
    }
    assert body["actors"] == ["moderator", "p01", "p02"]
    assert body["reasoning_depths"] == ["low", "medium", "high", "xhigh"]
    assert body["locked"] is False
    assert {i["actor_id"] for i in body["issues"]} == {"p01", "p02"}
    assert [set(p) for p in body["providers"]] == [PROVIDER_KEYS]
    assert SECRET_KEY not in json.dumps(body)


def test_binding_to_a_deleted_provider_is_reported_as_needing_rebind(client, env):
    provider = env.providers.seed(models=("m1",))
    env.session.model_bindings = {a: bind(provider.id) for a in ("moderator", "p01", "p02")}
    assert client.get("/api/settings/bindings").json()["issues"] == []
    del env.providers.providers[provider.id]
    issues = client.get("/api/settings/bindings").json()["issues"]
    assert {i["actor_id"] for i in issues} == {"moderator", "p01", "p02"}
    assert "重新绑定" in issues[0]["message"]


def put_body(provider_id, actors=("moderator", "p01"), **overrides):
    binding = {"provider_id": provider_id, "model_id": "m1", "reasoning_depth": "high"}
    binding.update(overrides)
    return {"bindings": {actor: dict(binding) for actor in actors}}


def test_put_bindings_updates_engine_and_returns_the_new_state(client, env):
    provider = env.providers.seed(models=("m1",))
    response = client.put("/api/settings/bindings", json=put_body(provider.id))
    assert response.status_code == 200
    env.orch.update_model_bindings.assert_called_once()
    mapping = env.orch.update_model_bindings.call_args.args[0]
    assert set(mapping) == {"moderator", "p01"}
    assert mapping["p01"].provider_id == provider.id
    body = response.json()
    assert set(body["bindings"]) == {"moderator", "p01"}
    assert {i["actor_id"] for i in body["issues"]} == {"p02"}


def test_put_bindings_is_locked_while_running(client, env):
    provider = env.providers.seed(models=("m1",))
    env.session.status = "running"
    response = client.put("/api/settings/bindings", json=put_body(provider.id))
    assert response.status_code == 409
    assert "锁定" in detail(response)
    env.orch.update_model_bindings.assert_not_called()
    assert client.get("/api/settings/bindings").json()["locked"] is True


def test_put_bindings_maps_engine_runtime_error_to_409(client, env):
    provider = env.providers.seed(models=("m1",))
    env.orch.update_model_bindings.side_effect = RuntimeError("对局进行中")
    assert client.put("/api/settings/bindings", json=put_body(provider.id)).status_code == 409


@pytest.mark.parametrize(
    "mutation,status",
    [
        ({"reasoning_depth": "ultra"}, 422),
        ({"model_id": "ghost"}, 400),
        ({"provider_id": "ghost"}, 400),
        ({"context_window_tokens": 10}, 400),
        ({"api_key": "sk-smuggled"}, 422),
    ],
)
def test_put_bindings_validates_each_binding(client, env, mutation, status):
    provider = env.providers.seed(models=("m1",))
    body = put_body(provider.id)
    for binding in body["bindings"].values():
        binding.update(mutation)
    if mutation.get("provider_id") != "ghost" and "provider_id" in mutation:
        pass
    response = client.put("/api/settings/bindings", json=body)
    assert response.status_code == status
    detail(response)
    env.orch.update_model_bindings.assert_not_called()


def test_put_bindings_rejects_unknown_actor_and_empty_and_oversized(client, env):
    provider = env.providers.seed(models=("m1",))
    assert client.put("/api/settings/bindings", json=put_body(provider.id, actors=("p99",))).status_code == 400
    assert client.put("/api/settings/bindings", json={"bindings": {}}).status_code == 400
    assert client.put("/api/settings/bindings", json={}).status_code == 422
    env.orch.update_model_bindings.assert_not_called()


# ------------------------------------------------------ model templates
def template_item(provider_id: str, **overrides):
    item = {
        "slot": 1,
        "name": "快模型",
        "provider_id": provider_id,
        "model_id": "m1",
        "reasoning_depth": "high",
    }
    item.update(overrides)
    return item


def test_templates_empty_payload(client, env):
    response = client.get("/api/settings/templates")
    assert response.status_code == 200
    assert response.json() == {"templates": [], "slots": 3, "locked": False}


def test_put_templates_saves_partial_slots_and_orders_them(client, env):
    provider = env.providers.seed(models=("m1", "m2"))
    response = client.put(
        "/api/settings/templates",
        json={
            "templates": [
                template_item(provider.id, slot=2, name="深度", model_id="m2", reasoning_depth="low"),
                template_item(provider.id, slot=1, name="快"),
            ]
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert [item["slot"] for item in payload["templates"]] == [1, 2]
    assert all(item["valid"] is True for item in payload["templates"])
    assert payload["templates"][1]["reasoning_depth"] == "low"
    assert [(t["slot"], t["name"]) for t in env.settings.templates] == [(1, "快"), (2, "深度")]
    assert SECRET_KEY not in response.text

    # Replacing with one slot clears the omitted ones (partial update of the whole list).
    assert client.put(
        "/api/settings/templates", json={"templates": [template_item(provider.id, slot=3)]}
    ).status_code == 200
    assert [t["slot"] for t in env.settings.templates] == [3]
    assert client.put("/api/settings/templates", json={"templates": []}).status_code == 200
    assert env.settings.templates == []


def test_put_templates_rejects_duplicate_slots_and_out_of_range(client, env):
    provider = env.providers.seed()
    duplicate = client.put(
        "/api/settings/templates",
        json={"templates": [template_item(provider.id, slot=1), template_item(provider.id, slot=1)]},
    )
    assert duplicate.status_code == 400
    assert "槽位" in detail(duplicate)

    assert client.put(
        "/api/settings/templates", json={"templates": [template_item(provider.id, slot=4)]}
    ).status_code == 422
    assert client.put(
        "/api/settings/templates",
        json={"templates": [template_item(provider.id, reasoning_depth="extreme")]},
    ).status_code == 422
    assert env.settings.templates == []


def test_put_templates_rejects_missing_provider_or_model(client, env):
    provider = env.providers.seed(models=("m1",))
    for broken in (
        template_item("ghost-provider"),
        template_item(provider.id, model_id="ghost-model"),
    ):
        response = client.put("/api/settings/templates", json={"templates": [broken]})
        assert response.status_code == 400
        assert "模板1" in detail(response)
    assert env.settings.templates == []


def test_templates_can_be_edited_while_running_but_not_applied(client, env):
    provider = env.providers.seed(models=("m1",))
    env.settings.seed_template(slot=1, provider_id=provider.id)
    env.session.status = "running"
    assert client.put(
        "/api/settings/templates", json={"templates": [template_item(provider.id)]}
    ).status_code == 200
    applied = client.post("/api/settings/templates/1/apply", json={"actor_ids": ["moderator"]})
    assert applied.status_code == 409
    detail(applied)
    env.orch.update_model_bindings.assert_not_called()


def test_apply_template_binds_the_selected_actors_only(client, env):
    provider = env.providers.seed(models=("m1",))
    env.settings.seed_template(
        slot=2, provider_id=provider.id, model_id="m1", reasoning_depth="low"
    )
    response = client.post(
        "/api/settings/templates/2/apply",
        json={"actor_ids": ["moderator", "p01", "p01"]},
    )
    assert response.status_code == 200
    mapping = env.orch.update_model_bindings.call_args.args[0]
    assert set(mapping) == {"moderator", "p01"}  # duplicates removed
    binding = mapping["moderator"]
    assert binding.provider_id == provider.id
    assert binding.model_id == "m1"
    assert binding.reasoning_depth == "low"
    body = response.json()
    assert body["bindings"]["moderator"]["model_id"] == "m1"
    assert body["bindings"]["p01"]["reasoning_depth"] == "low"
    assert body["locked"] is False


def test_apply_template_rejects_unknown_slot_actor_and_stale_reference(client, env):
    provider = env.providers.seed(models=("m1",))
    missing = client.post("/api/settings/templates/1/apply", json={"actor_ids": ["moderator"]})
    assert missing.status_code == 404
    bad_slot = client.post("/api/settings/templates/9/apply", json={"actor_ids": ["moderator"]})
    assert bad_slot.status_code == 404

    env.settings.seed_template(slot=1, provider_id=provider.id)
    unknown_actor = client.post("/api/settings/templates/1/apply", json={"actor_ids": ["p99"]})
    assert unknown_actor.status_code == 400
    assert "p99" in detail(unknown_actor)
    assert client.post(
        "/api/settings/templates/1/apply", json={"actor_ids": []}
    ).status_code == 422

    env.providers.providers[provider.id].models.clear()  # the template now dangles
    stale = client.post("/api/settings/templates/1/apply", json={"actor_ids": ["moderator"]})
    assert stale.status_code == 400
    assert "已不存在" in detail(stale)
    env.orch.update_model_bindings.assert_not_called()


def test_get_templates_marks_stale_entries_invalid(client, env):
    provider = env.providers.seed()
    env.settings.seed_template(slot=1, provider_id=provider.id)
    env.settings.seed_template(slot=2, provider_id="ghost")
    payload = client.get("/api/settings/templates").json()
    assert [item["valid"] for item in payload["templates"]] == [True, False]


def _template_user(env, *, slot: int = 1):
    provider = env.providers.seed(models=("m1", "m2"))
    env.settings.seed_template(slot=slot, provider_id=provider.id, model_id="m1")
    return provider


def test_delete_provider_referenced_by_template_is_409(client, env):
    provider = _template_user(env, slot=3)
    response = client.delete(f"/api/providers/{provider.id}")
    assert response.status_code == 409
    assert "模板3" in detail(response)
    assert provider.id in env.providers.providers
    assert all(call[0] != "delete_provider" for call in env.providers.calls)


def test_remove_model_referenced_by_template_is_409(client, env):
    provider = _template_user(env)
    response = client.request(
        "DELETE", f"/api/providers/{provider.id}/models", json={"model_id": "m1"}
    )
    assert response.status_code == 409
    assert "模板1" in detail(response)
    assert {model.id for model in env.providers.providers[provider.id].models} == {"m1", "m2"}

    # A model that no template/binding references can still be removed.
    assert client.request(
        "DELETE", f"/api/providers/{provider.id}/models", json={"model_id": "m2"}
    ).status_code == 200


def test_template_storage_failure_is_503_and_never_treated_as_unreferenced(client, env):
    env.settings.fail_with = StorageError("settings unavailable")
    read = client.get("/api/settings/templates")
    assert read.status_code == 503
    detail(read)

    provider = env.providers.seed()
    deleted = client.delete(f"/api/providers/{provider.id}")
    assert deleted.status_code == 503
    assert provider.id in env.providers.providers
    assert all(call[0] != "delete_provider" for call in env.providers.calls)


# ------------------------------------------------------- start guard
def test_start_requires_complete_bindings_with_clear_chinese_error(client, env):
    provider = env.providers.seed(models=("m1",))
    env.session.model_bindings = {"moderator": bind(provider.id)}
    response = client.post("/api/control/start")
    assert response.status_code == 409
    text = detail(response)
    assert "p01" in text and "绑定" in text
    env.orch.start_or_resume.assert_not_awaited()

    env.session.model_bindings = {a: bind(provider.id) for a in ("moderator", "p01", "p02")}
    assert client.post("/api/control/start").status_code == 200
    env.orch.start_or_resume.assert_awaited_once()


# ------------------------------------------------------------ the v0.12 session endpoints are gone
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/sessions"),
        ("delete", "/api/sessions/abc"),
        ("post", "/api/sessions/abc/load"),
        ("post", "/api/session/save"),
        ("post", "/api/session/import"),
    ],
)
def test_the_v012_session_endpoints_no_longer_exist(client, env, method, path):
    """v0.13 (F26, F34): saving and loading single games went away with the "会话" dialog; the history is /api/games."""
    response = client.request(method.upper(), path, content=b"{}")
    assert response.status_code in (404, 405)
    env.orch.persist_session.assert_not_awaited()
    env.orch.load_session_data.assert_not_awaited()


# ------------------------------------------------- lifecycle / health
def test_lifespan_calls_orchestrator_shutdown(monkeypatch, env):
    import app.main as main_module

    monkeypatch.setattr(main_module, "get_orchestrator", lambda: env.orch)
    with TestClient(app) as lifecycle_client:
        assert lifecycle_client.get("/api/health").json()["status"] == "ok"
        env.orch.shutdown.assert_not_awaited()
    env.orch.shutdown.assert_awaited_once()


def test_lifespan_runs_startup_and_flags_recovery_needed(monkeypatch, env):
    import app.main as main_module

    monkeypatch.setattr(main_module, "get_orchestrator", lambda: env.orch)
    with TestClient(app) as lifecycle_client:
        env.orch.startup.assert_awaited_once()
        assert lifecycle_client.get("/api/health").json()["status"] == "ok"
    env.orch.startup.reset_mock()
    env.orch.startup.return_value = {"recovery_error": "存档损坏"}
    with TestClient(app) as lifecycle_client:
        health = lifecycle_client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "degraded"
        assert "损坏" not in health.text  # no details on the unauthenticated probe
    # a later healthy start clears the flag
    env.orch.startup.return_value = {"recovery_error": None}
    with TestClient(app) as lifecycle_client:
        assert lifecycle_client.get("/api/health").json()["status"] == "ok"


def test_storage_failure_at_startup_degrades_instead_of_crashing(monkeypatch):
    import app.main as main_module

    def broken():
        raise StorageError("master key unavailable")

    monkeypatch.setattr(main_module, "get_orchestrator", broken)
    with TestClient(app) as lifecycle_client:
        health = lifecycle_client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "degraded"
        assert "master key" not in health.text
    assert app.state.degraded is True


def test_shutdown_errors_never_block_exit(monkeypatch, env):
    import app.main as main_module

    env.orch.shutdown = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(main_module, "get_orchestrator", lambda: env.orch)
    with TestClient(app):
        pass
    env.orch.shutdown.assert_awaited_once()


def test_storage_error_handler_returns_503_string_detail(client, env, monkeypatch):
    def boom():
        raise StorageError("存储不可用：数据目录未挂载")

    monkeypatch.setattr(provider_api, "get_provider_store", boom)
    response = client.get("/api/providers")
    assert response.status_code == 503
    assert "数据目录" in detail(response)


# --------------------------------------------------- security helpers
@pytest.mark.parametrize(
    "host,expected",
    [
        ("api.openai.com", "public"),
        ("8.8.8.8", "public"),
        ("localhost", "private"),
        ("foo.localhost", "private"),
        ("127.0.0.1", "private"),
        ("::1", "private"),
        ("10.0.0.3", "private"),
        ("172.16.0.1", "private"),
        ("192.168.0.1", "private"),
        ("fd12::1", "private"),
        ("ollama", "private"),
        ("nas.local", "private"),
        ("169.254.169.254", "metadata"),
        ("fe80::1", "metadata"),
        ("metadata.google.internal", "metadata"),
        ("0.0.0.0", "metadata"),
        ("2130706433", "metadata"),
        ("0x7f.0.0.1", "metadata"),
        ("::ffff:169.254.169.254", "metadata"),
        ("100.100.100.200", "metadata"),
    ],
)
def test_classify_host(host, expected):
    assert security._classify_host(host) == expected


def test_redact_removes_secrets_and_bearer_tokens():
    text = security.redact(
        "error Authorization: Bearer abcdefgh12345678 key sk-abcdefghijkl and MYSECRETVALUE", ["MYSECRETVALUE"]
    )
    assert "MYSECRETVALUE" not in text
    assert "abcdefgh12345678" not in text
    assert "sk-abcdefghijkl" not in text
    assert len(security.redact("x" * 1000, [])) == 300
