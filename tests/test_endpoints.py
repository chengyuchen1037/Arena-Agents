"""v0.11 endpoint plans, and the *real* requests the three SDKs send (mock HTTP)."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app.engine.llm import LLMClient
from app.llm import endpoints
from app.llm.discovery import DiscoveryError, discover_models
from app.llm.endpoints import (
    EndpointError,
    FixedUrlTransport,
    build_endpoints,
    endpoint_preview,
)
from test_llm_support import (
    ANTHROPIC_SDK,
    CHAT_SDK,
    RESPONSES_SDK,
    FakeStore,
    base_cfg,
    make_binding,
    make_provider,
)

CHAT = "openai_chat"
RESP = "openai_responses"
ANTH = "anthropic_messages"


# ------------------------------------------------------------------ builder
@pytest.mark.parametrize(
    "protocol, base, request_url, models_url, sdk_base",
    [
        (CHAT, "https://api.example.com", "https://api.example.com/v1/chat/completions", "https://api.example.com/v1/models", "https://api.example.com/v1"),
        (CHAT, "https://api.example.com/", "https://api.example.com/v1/chat/completions", "https://api.example.com/v1/models", "https://api.example.com/v1"),
        (CHAT, "https://api.example.com/v1", "https://api.example.com/v1/chat/completions", "https://api.example.com/v1/models", "https://api.example.com/v1"),
        (CHAT, "https://api.example.com/v1/", "https://api.example.com/v1/chat/completions", "https://api.example.com/v1/models", "https://api.example.com/v1"),
        (CHAT, "  https://api.example.com/v1  ", "https://api.example.com/v1/chat/completions", "https://api.example.com/v1/models", "https://api.example.com/v1"),
        (CHAT, "https://gw.example.com/openai", "https://gw.example.com/openai/v1/chat/completions", "https://gw.example.com/openai/v1/models", "https://gw.example.com/openai/v1"),
        (CHAT, "https://gw.example.com/openai/v1", "https://gw.example.com/openai/v1/chat/completions", "https://gw.example.com/openai/v1/models", "https://gw.example.com/openai/v1"),
        (CHAT, "http://localhost:11434", "http://localhost:11434/v1/chat/completions", "http://localhost:11434/v1/models", "http://localhost:11434/v1"),
        (CHAT, "http://[::1]:8000/v1", "http://[::1]:8000/v1/chat/completions", "http://[::1]:8000/v1/models", "http://[::1]:8000/v1"),
        (RESP, "https://api.example.com", "https://api.example.com/v1/responses", "https://api.example.com/v1/models", "https://api.example.com/v1"),
        (RESP, "https://api.example.com/v1", "https://api.example.com/v1/responses", "https://api.example.com/v1/models", "https://api.example.com/v1"),
        (ANTH, "https://api.anthropic.com", "https://api.anthropic.com/v1/messages", "https://api.anthropic.com/v1/models", "https://api.anthropic.com"),
        (ANTH, "https://api.anthropic.com/v1", "https://api.anthropic.com/v1/messages", "https://api.anthropic.com/v1/models", "https://api.anthropic.com"),
        (ANTH, "https://api.anthropic.com/v1/", "https://api.anthropic.com/v1/messages", "https://api.anthropic.com/v1/models", "https://api.anthropic.com"),
        (ANTH, "https://gw.example.com/anthropic", "https://gw.example.com/anthropic/v1/messages", "https://gw.example.com/anthropic/v1/models", "https://gw.example.com/anthropic"),
    ],
)
def test_auto_mode_plans(protocol, base, request_url, models_url, sdk_base) -> None:
    plan = build_endpoints(protocol, base, True)
    assert plan.auto_append_path is True
    assert plan.protocol == protocol
    assert plan.request_url == request_url
    assert plan.models_url == models_url
    assert plan.sdk_base_url == sdk_base
    assert build_endpoints(protocol, base) == plan  # auto is the default


@pytest.mark.parametrize(
    "protocol, base",
    [
        (CHAT, "https://api.example.com/v1/chat/completions"),
        (CHAT, "https://api.example.com/v1/chat/completions/"),
        (CHAT, "https://api.example.com/chat/completions"),
        (RESP, "https://api.example.com/v1/responses"),
        (ANTH, "https://api.example.com/v1/messages"),
        (CHAT, "https://api.example.com/v1/messages"),  # any known endpoint suffix
    ],
)
def test_auto_mode_rejects_a_complete_endpoint(protocol, base) -> None:
    with pytest.raises(EndpointError) as raised:
        build_endpoints(protocol, base, True)
    assert "自动拼接" in str(raised.value)
    # the very same URL is the right input for manual mode
    plan = build_endpoints(protocol, base, False)
    assert plan.request_url == base


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "ftp://example.com",
        "example.com/v1",
        "https://user:pw@example.com",
        "https://exa mple.com",
        "https://example.com/\nx",
        "https://example.com#frag",
        "https://",
        "https://example.com:99999",
    ],
)
@pytest.mark.parametrize("auto", [True, False])
def test_invalid_urls_are_rejected_in_both_modes(bad, auto) -> None:
    with pytest.raises(EndpointError):
        build_endpoints(CHAT, bad, auto)


def test_unknown_protocol() -> None:
    with pytest.raises(EndpointError):
        build_endpoints("gopher", "https://a.example", True)


def test_query_string_only_allowed_when_manual() -> None:
    with pytest.raises(EndpointError):
        build_endpoints(CHAT, "https://a.example/v1?x=1", True)
    plan = build_endpoints(CHAT, "https://a.example/openai/deployments/d/chat/completions?api-version=2024-10-21", False)
    assert plan.request_url.endswith("?api-version=2024-10-21")


@pytest.mark.parametrize(
    "protocol, base",
    [
        (CHAT, "https://api.example.com/v1/chat/completions"),
        (CHAT, "https://api.example.com/custom/path/"),  # trailing slash kept: sent verbatim
        (RESP, "https://api.example.com/v1/responses"),
        (ANTH, "https://api.example.com/v1/messages"),
        (CHAT, "http://localhost:8080/x"),
    ],
)
def test_manual_mode_sends_the_url_verbatim(protocol, base) -> None:
    plan = build_endpoints(protocol, f"  {base}  ", False)
    assert plan.auto_append_path is False
    assert plan.request_url == base
    assert plan.models_url is None
    assert plan.sdk_base_url == base


def test_warnings() -> None:
    assert build_endpoints(CHAT, "https://api.example.com", True).warnings == ()
    http_plan = build_endpoints(CHAT, "http://api.example.com", True)
    assert any("HTTP" in w for w in http_plan.warnings)
    odd = build_endpoints(CHAT, "https://open.example.com/api/paas/v4", True)
    assert odd.request_url == "https://open.example.com/api/paas/v4/v1/chat/completions"
    assert any("v1" in w for w in odd.warnings)
    short = build_endpoints(CHAT, "https://api.example.com/custom", False)
    assert any("/chat/completions" in w for w in short.warnings)
    assert build_endpoints(CHAT, "https://api.example.com/v1/chat/completions", False).warnings == ()
    assert build_endpoints(ANTH, "https://api.example.com/v1/messages", False).warnings == ()


def test_endpoint_preview_is_json_safe() -> None:
    preview = endpoint_preview(RESP, "https://api.example.com", True)
    assert preview == {
        "auto_append_path": True,
        "request_url": "https://api.example.com/v1/responses",
        "models_url": "https://api.example.com/v1/models",
        "warnings": [],
    }
    json.dumps(preview)
    manual = endpoint_preview(CHAT, "https://x.example/chat", False)
    assert manual["models_url"] is None and manual["request_url"] == "https://x.example/chat"
    assert isinstance(manual["warnings"], list)
    with pytest.raises(EndpointError):
        endpoint_preview(CHAT, "nope", True)


# ------------------------------------------------- SSE bodies and mock server
def sse(*events: tuple[str | None, dict]) -> bytes:
    out = []
    for name, data in events:
        if name:
            out.append(f"event: {name}\n")
        out.append(f"data: {json.dumps(data)}\n\n")
    return "".join(out).encode()


CHAT_BODY = sse(
    (None, {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "你好"}, "finish_reason": None}]}),
    (None, {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
            "choices": [{"index": 0, "delta": {"content": "，世界"}, "finish_reason": "stop"}]}),
    (None, {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m", "choices": [],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}}),
) + b"data: [DONE]\n\n"

RESPONSES_BODY = sse(
    ("response.output_text.delta", {"type": "response.output_text.delta", "item_id": "i", "output_index": 0,
                                    "content_index": 0, "delta": "你好", "sequence_number": 1}),
    ("response.output_text.delta", {"type": "response.output_text.delta", "item_id": "i", "output_index": 0,
                                    "content_index": 0, "delta": "，世界", "sequence_number": 2}),
    ("response.completed", {"type": "response.completed", "sequence_number": 3, "response": {
        "id": "r", "object": "response", "created_at": 1, "model": "m", "status": "completed",
        "output": [], "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "usage": {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13,
                  "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}}),
)

ANTHROPIC_BODY = sse(
    ("message_start", {"type": "message_start", "message": {
        "id": "m", "type": "message", "role": "assistant", "model": "m", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 9, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "你好"}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "，世界"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                       "usage": {"output_tokens": 4}}),
    ("message_stop", {"type": "message_stop"}),
)

BODIES = {CHAT: CHAT_BODY, RESP: RESPONSES_BODY, ANTH: ANTHROPIC_BODY}


class MockNetwork:
    """Replaces the real inner transport; records the requests that would hit the wire."""

    def __init__(self, protocol: str, *, chunk_size: int = 23, handler=None) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self._protocol = protocol
        self._chunk = chunk_size
        self._handler = handler

    def __enter__(self) -> "MockNetwork":
        async def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            self.bodies.append(await request.aread())
            if self._handler:
                return self._handler(request)
            data = BODIES[self._protocol]
            pieces = [data[i : i + self._chunk] for i in range(0, len(data), self._chunk)]

            async def stream():
                for piece in pieces:  # tiny chunks: events straddle read boundaries
                    yield piece

            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())

        self._patch = patch.object(
            endpoints, "default_inner_transport", return_value=httpx.MockTransport(handle)
        )
        self._patch.start()
        return self

    def __exit__(self, *exc) -> None:
        self._patch.stop()


def provider_with(protocol: str, base_url: str, auto: bool, **kw):
    """A provider with ``auto_append_path`` regardless of whether ProviderConfig has the field yet."""
    base = make_provider(protocol, base_url=base_url, **kw)
    data = {name: getattr(base, name) for name in type(base).model_fields}
    data["auto_append_path"] = auto
    return SimpleNamespace(**data)


MESSAGES = [{"role": "system", "content": "规则"}, {"role": "user", "content": "hi"}]


class ManualEndpointRequestTests(unittest.IsolatedAsyncioTestCase):
    """Real SDKs + FixedUrlTransport: the URL that actually goes out."""

    async def _run(self, protocol: str, base_url: str, auto: bool, *, on_chunk=None, **kw):
        provider = provider_with(protocol, base_url, auto)
        llm = LLMClient(base_cfg(), provider_store=FakeStore(provider))
        try:
            with MockNetwork(protocol, **kw) as net:
                resp = await llm.chat_stream(MESSAGES, binding=make_binding(), on_chunk=on_chunk)
        finally:
            await llm.aclose()
        return resp, net

    async def test_manual_mode_posts_to_exactly_the_given_url_for_every_protocol(self) -> None:
        cases = (
            (CHAT, "https://gw.example.com/custom/path/chat"),
            (RESP, "https://gw.example.com/custom/path/respond"),
            (ANTH, "https://gw.example.com/custom/path/msg"),
        )
        for protocol, url in cases:
            with self.subTest(protocol=protocol):
                resp, net = await self._run(protocol, url, False)
                (request,) = net.requests
                self.assertEqual(request.method, "POST")
                self.assertEqual(str(request.url), url)  # no /chat/completions, /v1/messages, ... added
                self.assertEqual(request.headers["host"], "gw.example.com")
                self.assertEqual(resp.content, "你好，世界")
                self.assertEqual((resp.prompt_tokens, resp.completion_tokens), (9, 4))
                body = json.loads(net.bodies[0])
                self.assertEqual(body["model"], "model-a")
                self.assertTrue(body["stream"])

    async def test_manual_mode_keeps_query_string_port_and_authentication(self) -> None:
        resp, net = await self._run(CHAT, "http://10.0.0.3:8080/openai/deployments/d?api-version=2024-10-21", False)
        (request,) = net.requests
        self.assertEqual(str(request.url), "http://10.0.0.3:8080/openai/deployments/d?api-version=2024-10-21")
        self.assertEqual(request.headers["host"], "10.0.0.3:8080")
        self.assertEqual(request.headers["authorization"], "Bearer sk-test-secret")
        resp, net = await self._run(ANTH, "https://gw.example.com/anthropic-compat", False)
        self.assertEqual(net.requests[0].headers["x-api-key"], "sk-test-secret")
        self.assertIn("anthropic-version", net.requests[0].headers)

    async def test_manual_url_already_ending_like_the_sdk_path_is_not_doubled(self) -> None:
        for protocol, url in (
            (CHAT, "https://gw.example.com/v1/chat/completions"),
            (RESP, "https://gw.example.com/v1/responses"),
            (ANTH, "https://gw.example.com/v1/messages"),
        ):
            with self.subTest(protocol=protocol):
                _, net = await self._run(protocol, url, False)
                self.assertEqual(str(net.requests[0].url), url)

    async def test_streaming_is_incremental_in_manual_mode(self) -> None:
        for protocol in (CHAT, RESP, ANTH):
            with self.subTest(protocol=protocol):
                seen: list[tuple[str, str]] = []

                async def on_chunk(thought: str, content: str) -> None:
                    seen.append((thought, content))

                resp, _ = await self._run(
                    protocol, "https://gw.example.com/x", False, on_chunk=on_chunk, chunk_size=7
                )
                contents = [c for _, c in seen]
                self.assertEqual(contents[-1], "你好，世界")
                self.assertIn("你好", contents)  # an intermediate state was reported
                self.assertGreaterEqual(len(contents), 2)
                self.assertEqual(resp.content, "你好，世界")

    async def test_auto_mode_goes_through_the_sdk_path_not_the_fixed_transport(self) -> None:
        # No inner transport patch is needed for the SDK's own client; patch httpx at the SDK level.
        expected = {
            CHAT: ("https://api.example.com", "https://api.example.com/v1/chat/completions"),
            RESP: ("https://api.example.com/v1", "https://api.example.com/v1/responses"),
            ANTH: ("https://api.example.com/v1", "https://api.example.com/v1/messages"),
        }
        for protocol, (base, final) in expected.items():
            with self.subTest(protocol=protocol):
                seen: list[httpx.Request] = []
                body = BODIES[protocol]

                async def handle(request: httpx.Request, _body=body) -> httpx.Response:
                    seen.append(request)
                    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_body)

                provider = provider_with(protocol, base, True)
                llm = LLMClient(base_cfg(), provider_store=FakeStore(provider))
                with (
                    patch(CHAT_SDK, new=_sdk_over_mock(CHAT_SDK, handle)),
                    patch(RESPONSES_SDK, new=_sdk_over_mock(RESPONSES_SDK, handle)),
                    patch(ANTHROPIC_SDK, new=_sdk_over_mock(ANTHROPIC_SDK, handle)),
                ):
                    resp = await llm.chat_stream(MESSAGES, binding=make_binding())
                await llm.aclose()
                self.assertEqual(str(seen[0].url), final)
                self.assertEqual(resp.content, "你好，世界")

    async def test_manual_mode_does_not_follow_redirects(self) -> None:
        def redirect(request: httpx.Request) -> httpx.Response:
            return httpx.Response(307, headers={"location": "https://evil.example/steal"})

        provider = provider_with(CHAT, "https://gw.example.com/x", False)
        llm = LLMClient(base_cfg(retry={"max_attempts": 1}), provider_store=FakeStore(provider))
        with MockNetwork(CHAT, handler=redirect) as net:
            with self.assertRaises(Exception):
                await llm.chat_stream(MESSAGES, binding=make_binding())
        await llm.aclose()
        self.assertEqual([str(r.url) for r in net.requests], ["https://gw.example.com/x"])

    async def test_http_errors_still_classify_and_retry_in_manual_mode(self) -> None:
        attempts = {"n": 0}

        def flaky(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(503, json={"error": {"message": "busy"}})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=CHAT_BODY)

        provider = provider_with(CHAT, "https://gw.example.com/x", False)
        llm = LLMClient(base_cfg(), provider_store=FakeStore(provider))
        with MockNetwork(CHAT, handler=flaky) as net, patch("app.llm.common.asyncio.sleep") as sleep:
            sleep.return_value = None
            resp = await llm.chat_stream(MESSAGES, binding=make_binding())
        await llm.aclose()
        self.assertEqual(resp.content, "你好，世界")
        self.assertEqual([str(r.url) for r in net.requests], ["https://gw.example.com/x"] * 2)


class FixedUrlTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_rewrites_url_host_and_keeps_body_headers_and_method(self) -> None:
        seen: list[httpx.Request] = []

        async def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True}, headers={"x-reply": "1"})

        transport = FixedUrlTransport("https://real.example.com:8443/a/b?c=d", httpx.MockTransport(handle))
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.post(
                "https://sdk.invalid/v1/chat/completions?ignored=1",
                json={"k": "v"},
                headers={"Authorization": "Bearer t", "X-Custom": "1"},
            )
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(response.headers["x-reply"], "1")
        (request,) = seen
        self.assertEqual(str(request.url), "https://real.example.com:8443/a/b?c=d")
        self.assertEqual(request.headers["host"], "real.example.com:8443")
        self.assertEqual(request.headers["authorization"], "Bearer t")
        self.assertEqual(request.headers["x-custom"], "1")
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.content), {"k": "v"})
        self.assertNotIn("sdk.invalid", repr(request.headers))

    async def test_aclose_closes_inner_only_if_it_was_created(self) -> None:
        closed: list[int] = []

        class Inner(httpx.AsyncBaseTransport):
            async def aclose(self) -> None:
                closed.append(1)

        await FixedUrlTransport("https://a.example/x").aclose()  # nothing created yet
        await FixedUrlTransport("https://a.example/x", Inner()).aclose()
        self.assertEqual(closed, [1])


class ClientKeyAndTargetTests(unittest.IsolatedAsyncioTestCase):
    def test_target_carries_the_plan(self) -> None:
        llm = LLMClient(base_cfg(), provider_store=FakeStore(provider_with(CHAT, "https://api.example.com", True)))
        target = llm.build_target(make_binding())
        self.assertTrue(target.auto_append_path)
        self.assertEqual(target.request_url, "https://api.example.com/v1/chat/completions")
        self.assertEqual(target.models_url, "https://api.example.com/v1/models")
        self.assertEqual(target.sdk_base_url, "https://api.example.com/v1")

        llm = LLMClient(base_cfg(), provider_store=FakeStore(provider_with(CHAT, "https://gw.example.com/chat", False)))
        target = llm.build_target(make_binding())
        self.assertFalse(target.auto_append_path)
        self.assertEqual(target.request_url, "https://gw.example.com/chat")
        self.assertIsNone(target.models_url)

    def test_providers_without_the_field_default_to_auto(self) -> None:
        provider = make_provider(CHAT, base_url="https://api.example.com")  # field may not exist yet
        llm = LLMClient(base_cfg(), provider_store=FakeStore(provider))
        self.assertTrue(llm.build_target(make_binding()).auto_append_path)

    def test_client_key_changes_with_mode_and_final_address(self) -> None:
        def key(base_url: str, auto: bool):
            llm = LLMClient(base_cfg(), provider_store=FakeStore(provider_with(CHAT, base_url, auto)))
            return llm.build_target(make_binding()).client_key

        self.assertEqual(key("https://a.example/v1", True), key("https://a.example/v1", True))
        self.assertNotEqual(key("https://a.example/v1", True), key("https://a.example/v1/chat/completions", False))
        self.assertNotEqual(key("https://a.example/x", False), key("https://a.example/y", False))
        # same text, different mode -> different client
        self.assertNotEqual(key("https://a.example/v1", True), key("https://a.example/v1", False))

    def test_invalid_stored_url_gives_a_clear_error_naming_the_provider(self) -> None:
        provider = provider_with(CHAT, "https://a.example/v1/chat/completions", True)
        llm = LLMClient(base_cfg(), provider_store=FakeStore(provider))
        with self.assertRaises(EndpointError) as raised:
            llm.build_target(make_binding())
        self.assertIn(provider.name, str(raised.exception))
        self.assertNotIn("sk-test-secret", str(raised.exception))

    async def test_switching_mode_rebuilds_the_client_and_closes_the_old_one(self) -> None:
        provider = provider_with(CHAT, "https://a.example/v1/chat/completions", False)
        store = FakeStore(provider)
        llm = LLMClient(base_cfg(), provider_store=store)
        with MockNetwork(CHAT) as net:
            await llm.chat_stream(MESSAGES, binding=make_binding())
            first_handle = llm._cache.get("prov1")
            store.providers["prov1"] = provider_with(CHAT, "https://a.example", True)
            # auto mode uses the SDK's own HTTP client, so serve it from a mock as well
            def ok(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=CHAT_BODY)

            with patch(CHAT_SDK, new=_sdk_over_mock(CHAT_SDK, ok)):
                await llm.chat_stream(MESSAGES, binding=make_binding())
            self.assertIsNot(llm._cache.get("prov1"), first_handle)
            self.assertTrue(first_handle.closed)
        await llm.aclose()
        self.assertEqual(str(net.requests[0].url), "https://a.example/v1/chat/completions")

    async def test_legacy_cfg_fallback_keeps_the_v09_url_semantics(self) -> None:
        llm = LLMClient(base_cfg(base_url="https://gateway.example/api"))
        target = llm.build_target(None)
        self.assertEqual(target.sdk_base_url, "https://gateway.example/api")
        self.assertTrue(target.auto_append_path)


def _sdk_over_mock(sdk_path: str, handler):
    """The real SDK class, but with an ``http_client`` that talks to ``handler`` (auto mode)."""
    module_name, _, attr = sdk_path.rpartition(".")
    real = getattr(__import__(module_name, fromlist=[attr]), attr)

    def factory(**kwargs):
        assert "http_client" not in kwargs  # auto mode lets the SDK build its own client
        kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return real(**kwargs)

    return factory


class DiscoveryUnificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_provider_is_rejected_without_any_network_activity(self) -> None:
        called: list[int] = []

        async def resolver(host: str, port: int) -> list[str]:
            called.append(1)
            return ["93.184.216.34"]

        def boom(request: httpx.Request) -> httpx.Response:
            called.append(2)
            return httpx.Response(200, json={"data": []})

        provider = provider_with(CHAT, "https://api.example.com/v1/chat/completions", False)
        with self.assertRaises(DiscoveryError) as raised:
            await discover_models(provider, transport=httpx.MockTransport(boom), resolver=resolver)
        self.assertEqual(raised.exception.kind, "manual_endpoint")
        self.assertIn("手动", str(raised.exception))
        self.assertEqual(called, [])

    async def test_discovery_url_equals_the_plan_models_url(self) -> None:
        cases = (
            (CHAT, "https://api.example.com"),
            (CHAT, "https://api.example.com/v1/"),
            (RESP, "https://api.example.com/v1"),
            (ANTH, "https://api.example.com"),
            (ANTH, "https://gw.example.com/anthropic/v1"),
        )
        for protocol, base in cases:
            with self.subTest(protocol=protocol, base=base):
                seen: list[httpx.Request] = []

                def handle(request: httpx.Request) -> httpx.Response:
                    seen.append(request)
                    return httpx.Response(200, json={"data": [{"id": "m"}]})

                async def resolver(host: str, port: int) -> list[str]:
                    return ["93.184.216.34"]

                await discover_models(
                    provider_with(protocol, base, True),
                    transport=httpx.MockTransport(handle),
                    resolver=resolver,
                )
                planned = httpx.URL(build_endpoints(protocol, base, True).models_url)
                self.assertEqual(seen[0].url.path, planned.path)
                self.assertEqual(seen[0].headers["host"], planned.host)

    async def test_invalid_or_complete_endpoint_urls_are_invalid_url_errors(self) -> None:
        for base in ("https://api.example.com/v1/chat/completions", "ftp://x.example", "https://u:p@x.example"):
            with self.subTest(base=base):
                with self.assertRaises(DiscoveryError) as raised:
                    await discover_models(provider_with(CHAT, base, True))
                self.assertEqual(raised.exception.kind, "invalid_url")

    async def test_provider_objects_without_the_field_still_discover(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"id": "x"}]})

        async def resolver(host: str, port: int) -> list[str]:
            return ["93.184.216.34"]

        models = await discover_models(
            make_provider(CHAT, base_url="https://api.example.com"),
            transport=httpx.MockTransport(handle),
            resolver=resolver,
        )
        self.assertEqual(models, ["x"])


if __name__ == "__main__":
    unittest.main()
