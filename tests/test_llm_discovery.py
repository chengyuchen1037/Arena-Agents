"""Model discovery: URL normalisation, auth, limits, SSRF guards (all mocked)."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import httpx
import pytest

from app.llm import discovery
from app.llm.discovery import (
    MAX_MODELS,
    MAX_RESPONSE_BYTES,
    DiscoveryError,
    classify_address,
    discover_models,
    models_url,
    parse_base_url,
    parse_models_payload,
)
from test_llm_support import make_provider

PUBLIC_IP = "93.184.216.34"
SECRET = "sk-test-secret"


def resolver_for(*addresses: str):
    calls: list[tuple[str, int]] = []

    async def resolve(host: str, port: int) -> list[str]:
        calls.append((host, port))
        return list(addresses)

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


class Recorder:
    """httpx mock transport that records requests and answers with ``handler``."""

    def __init__(self, handler=None, *, payload=None, status=200, headers=None) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler
        self._payload = payload
        self._status = status
        self._headers = headers or {}

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self._handler is not None:
                return self._handler(request)
            return httpx.Response(self._status, json=self._payload, headers=self._headers)

        return httpx.MockTransport(handle)


class UrlAndHeaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_models_url_auth_and_pinned_address(self) -> None:
        rec = Recorder(payload={"data": [{"id": "gpt-a"}, {"id": "gpt-b"}]})
        resolve = resolver_for(PUBLIC_IP)
        models = await discover_models(
            make_provider("openai_chat", base_url="https://api.example.com/v1/"),
            transport=rec.transport(),
            resolver=resolve,
        )
        self.assertEqual(models, ["gpt-a", "gpt-b"])
        (request,) = rec.requests
        # connected to the vetted address, original name kept for Host/SNI
        self.assertEqual(request.url.host, PUBLIC_IP)
        self.assertEqual(request.url.path, "/v1/models")
        self.assertEqual(request.headers["host"], "api.example.com")
        self.assertEqual(request.headers["authorization"], f"Bearer {SECRET}")
        self.assertEqual(request.extensions.get("sni_hostname"), "api.example.com")
        self.assertEqual(resolve.calls, [("api.example.com", 443)])  # resolved exactly once

    async def test_responses_protocol_uses_the_openai_endpoint(self) -> None:
        rec = Recorder(payload={"data": [{"id": "m"}]})
        await discover_models(
            make_provider("openai_responses", base_url="https://api.example.com"),
            transport=rec.transport(),
            resolver=resolver_for(PUBLIC_IP),
        )
        self.assertEqual(rec.requests[0].url.path, "/v1/models")  # v0.11: root + /v1
        self.assertIn("authorization", rec.requests[0].headers)

    async def test_anthropic_endpoint_headers_and_v1_normalisation(self) -> None:
        for base in ("https://api.anthropic.example", "https://api.anthropic.example/v1", "https://api.anthropic.example/v1/"):
            with self.subTest(base=base):
                rec = Recorder(payload={"data": [{"id": "claude-x", "display_name": "X"}], "has_more": False})
                models = await discover_models(
                    make_provider("anthropic_messages", base_url=base),
                    transport=rec.transport(),
                    resolver=resolver_for(PUBLIC_IP),
                )
                self.assertEqual(models, ["claude-x"])
                request = rec.requests[0]
                self.assertEqual(request.url.path, "/v1/models")
                self.assertEqual(request.url.params["limit"], str(MAX_MODELS))
                self.assertEqual(request.headers["x-api-key"], SECRET)
                self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
                self.assertNotIn("authorization", request.headers)

    async def test_non_default_port_and_http_scheme(self) -> None:
        rec = Recorder(payload={"data": []})
        await discover_models(
            make_provider("openai_chat", base_url="http://llm.example.com:8080/api"),
            transport=rec.transport(),
            resolver=resolver_for(PUBLIC_IP),
        )
        request = rec.requests[0]
        self.assertEqual(request.url.port, 8080)
        self.assertEqual(request.url.path, "/api/v1/models")
        self.assertEqual(request.headers["host"], "llm.example.com:8080")
        self.assertNotIn("sni_hostname", request.extensions)

    async def test_ip_literal_host_is_used_as_is(self) -> None:
        rec = Recorder(payload={"data": []})
        await discover_models(
            make_provider("openai_chat", base_url=f"https://{PUBLIC_IP}/v1"),
            transport=rec.transport(),
        )
        self.assertEqual(rec.requests[0].url.host, PUBLIC_IP)

    def test_models_url_helper(self) -> None:
        self.assertEqual(models_url("openai_chat", "https://a.b/v1/"), ("https://a.b/v1/models", {}))
        url, params = models_url("anthropic_messages", "https://a.b/v1")
        self.assertEqual(url, "https://a.b/v1/models")
        self.assertEqual(params, {"limit": "1000"})


class ResponseHandlingTests(unittest.IsolatedAsyncioTestCase):
    async def _discover(self, rec: Recorder, **kw):
        return await discover_models(
            make_provider("openai_chat", base_url="https://api.example.com/v1"),
            transport=rec.transport(),
            resolver=resolver_for(PUBLIC_IP),
            **kw,
        )

    async def test_auth_errors_do_not_echo_the_key_or_upstream_body(self) -> None:
        rec = Recorder(payload={"error": {"message": f"Incorrect API key {SECRET} <script>"}}, status=401)
        with self.assertRaises(DiscoveryError) as raised:
            await self._discover(rec)
        text = str(raised.exception)
        self.assertEqual(raised.exception.kind, "auth")
        self.assertEqual(raised.exception.status, 401)
        self.assertIn("401", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("script", text)

    async def test_other_statuses_map_to_fixed_messages(self) -> None:
        for status, kind in ((403, "auth"), (404, "status"), (429, "status"), (500, "status"), (502, "status")):
            with self.subTest(status=status):
                with self.assertRaises(DiscoveryError) as raised:
                    await self._discover(Recorder(payload={"detail": "internal secret"}, status=status))
                self.assertEqual((raised.exception.kind, raised.exception.status), (kind, status))
                self.assertNotIn("internal secret", str(raised.exception))

    async def test_redirects_are_not_followed(self) -> None:
        rec = Recorder(
            status=302,
            headers={"location": "http://169.254.169.254/latest/meta-data/"},
        )
        with self.assertRaises(DiscoveryError) as raised:
            await self._discover(rec)
        self.assertEqual(raised.exception.kind, "redirect")
        self.assertEqual(len(rec.requests), 1)  # the Location was never requested

    async def test_oversized_response_is_rejected(self) -> None:
        big = b'{"data": [' + b'{"id": "x"},' * (MAX_RESPONSE_BYTES // 10) + b"{}]}"
        rec = Recorder(lambda request: httpx.Response(200, content=big))
        with self.assertRaises(DiscoveryError) as raised:
            await self._discover(rec)
        self.assertEqual(raised.exception.kind, "too_large")

    async def test_declared_content_length_over_limit_is_rejected_early(self) -> None:
        rec = Recorder(lambda request: httpx.Response(200, content=b"{}", headers={"content-length": str(MAX_RESPONSE_BYTES + 1)}))
        with self.assertRaises(DiscoveryError) as raised:
            await self._discover(rec)
        self.assertEqual(raised.exception.kind, "too_large")

    async def test_invalid_json_and_unknown_shape(self) -> None:
        with self.assertRaises(DiscoveryError) as raised:
            await self._discover(Recorder(lambda r: httpx.Response(200, content=b"<html>nope</html>")))
        self.assertEqual(raised.exception.kind, "invalid_response")
        with self.assertRaises(DiscoveryError) as raised:
            await self._discover(Recorder(payload={"object": "list"}))
        self.assertEqual(raised.exception.kind, "invalid_response")

    async def test_timeout_and_connection_errors_are_sanitised(self) -> None:
        def timeout(request: httpx.Request):
            raise httpx.ReadTimeout(f"timed out talking to {request.url} with {SECRET}")

        with self.assertRaises(DiscoveryError) as raised:
            await self._discover(Recorder(timeout))
        self.assertEqual(raised.exception.kind, "timeout")
        self.assertNotIn(SECRET, str(raised.exception))

        def refuse(request: httpx.Request):
            raise httpx.ConnectError(f"refused {SECRET}", request=request)

        with self.assertRaises(DiscoveryError) as raised:
            await self._discover(Recorder(refuse))
        self.assertEqual(raised.exception.kind, "network")
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertNotIn(PUBLIC_IP, str(raised.exception))

    async def test_overall_deadline_is_enforced(self) -> None:
        import asyncio

        async def never(*args, **kwargs):
            await asyncio.sleep(60)

        class Slow(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                await asyncio.sleep(60)

        with patch.object(discovery, "DISCOVERY_TIMEOUT_SECONDS", 0.05):
            with self.assertRaises(DiscoveryError) as raised:
                await discover_models(
                    make_provider("openai_chat", base_url="https://api.example.com"),
                    transport=Slow(),
                    resolver=resolver_for(PUBLIC_IP),
                )
        self.assertEqual(raised.exception.kind, "timeout")

    async def test_missing_key_or_bad_protocol(self) -> None:
        with self.assertRaises(DiscoveryError):
            await discover_models(make_provider("openai_chat", api_key="  "), resolver=resolver_for(PUBLIC_IP))
        with self.assertRaises(DiscoveryError):
            await discover_models(make_provider("openai_chat", api_key="bad\nkey"), resolver=resolver_for(PUBLIC_IP))

        class Weird:
            protocol, base_url, api_key = "gopher", "https://x.example", "k"

        with self.assertRaises(DiscoveryError):
            await discover_models(Weird(), resolver=resolver_for(PUBLIC_IP))


class PayloadParsingTests(unittest.TestCase):
    def test_common_shapes(self) -> None:
        self.assertEqual(parse_models_payload({"data": [{"id": "a"}, {"id": "b"}]}), ["a", "b"])
        self.assertEqual(parse_models_payload({"models": [{"name": "x"}, "y"]}), ["x", "y"])
        self.assertEqual(parse_models_payload(["p", {"id": "q"}]), ["p", "q"])
        self.assertEqual(parse_models_payload({"data": []}), [])

    def test_filters_dedupes_and_sanitises(self) -> None:
        payload = {
            "data": [
                {"id": "a"},
                {"id": "a"},  # duplicate
                {"id": "  b  "},
                {"id": ""},
                {"id": 5},
                {"id": "x" * 201},
                {"id": "bad\nid"},
                {"id": "ok/with-slash:tag"},
                None,
                42,
            ]
        }
        self.assertEqual(parse_models_payload(payload), ["a", "b", "ok/with-slash:tag"])

    def test_model_count_is_capped(self) -> None:
        payload = {"data": [{"id": f"m{i}"} for i in range(MAX_MODELS + 500)]}
        ids = parse_models_payload(payload)
        self.assertEqual(len(ids), MAX_MODELS)
        self.assertEqual(ids[0], "m0")

    def test_unrecognised_payload(self) -> None:
        for bad in ("text", 5, None, {"data": "x"}, {}):
            with self.assertRaises(DiscoveryError):
                parse_models_payload(bad)


@pytest.mark.parametrize(
    "address, expected",
    [
        ("93.184.216.34", "public"),
        ("8.8.8.8", "public"),
        ("2606:4700:4700::1111", "public"),
        ("127.0.0.1", "private"),
        ("::1", "private"),
        ("10.0.0.3", "private"),
        ("172.16.0.1", "private"),
        ("192.168.1.10", "private"),
        ("fd12:3456::1", "private"),
        ("100.64.0.1", "private"),  # carrier-grade NAT
        ("::ffff:127.0.0.1", "private"),  # IPv4-mapped loopback
        ("::ffff:93.184.216.34", "public"),
        ("169.254.169.254", "blocked"),  # cloud metadata
        ("169.254.1.1", "blocked"),  # link-local
        ("fe80::1", "blocked"),
        ("fd00:ec2::254", "blocked"),  # AWS IPv6 metadata
        ("100.100.100.200", "blocked"),  # Alibaba metadata
        ("168.63.129.16", "blocked"),  # Azure wire server
        ("::ffff:169.254.169.254", "blocked"),
        ("0.0.0.0", "blocked"),
        ("224.0.0.1", "blocked"),
        ("240.0.0.1", "blocked"),
    ],
)
def test_classify_address(address: str, expected: str) -> None:
    assert classify_address(address) == expected


class SsrfGuardTests(unittest.IsolatedAsyncioTestCase):
    async def _try(self, base_url: str, *addresses: str, allow_private=None, rec=None):
        rec = rec or Recorder(payload={"data": [{"id": "m"}]})
        models = await discover_models(
            make_provider("openai_chat", base_url=base_url),
            transport=rec.transport(),
            resolver=resolver_for(*addresses) if addresses else None,
            allow_private=allow_private,
        )
        return models, rec

    async def test_private_and_loopback_are_blocked_by_default(self) -> None:
        with patch.dict(os.environ, {discovery.ALLOW_PRIVATE_ENV: ""}):
            for url, addrs in (
                ("http://localhost:11434/v1", ("127.0.0.1",)),
                ("http://127.0.0.1:1234/v1", ()),
                ("http://[::1]:8000/v1", ()),
                ("http://intranet.example/v1", ("10.0.0.5",)),
                ("http://nas.example/v1", ("192.168.1.2",)),
            ):
                with self.subTest(url=url):
                    rec = Recorder(payload={"data": []})
                    with self.assertRaises(DiscoveryError) as raised:
                        await self._try(url, *addrs, rec=rec)
                    self.assertEqual(raised.exception.kind, "blocked")
                    self.assertEqual(rec.requests, [])  # nothing was sent

    async def test_private_allowed_with_the_explicit_switch(self) -> None:
        for value in ("true", "1", "YES", "on"):
            with self.subTest(value=value), patch.dict(os.environ, {discovery.ALLOW_PRIVATE_ENV: value}):
                models, rec = await self._try("http://localhost:11434/v1", "127.0.0.1")
                self.assertEqual(models, ["m"])
        with patch.dict(os.environ, {discovery.ALLOW_PRIVATE_ENV: "false"}):
            with self.assertRaises(DiscoveryError):
                await self._try("http://localhost:11434/v1", "127.0.0.1")
        models, _ = await self._try("http://127.0.0.1:1234/v1", allow_private=True)
        self.assertEqual(models, ["m"])

    async def test_metadata_and_link_local_are_always_blocked(self) -> None:
        for url, addrs in (
            ("http://169.254.169.254/latest", ()),
            ("http://metadata.example/v1", ("169.254.169.254",)),
            ("http://[fe80::1]/v1", ()),
            ("http://linklocal.example/v1", ("169.254.7.7",)),
            ("http://aws6.example/v1", ("fd00:ec2::254",)),
            ("http://0.0.0.0:8000/v1", ()),
        ):
            with self.subTest(url=url):
                rec = Recorder(payload={"data": []})
                with self.assertRaises(DiscoveryError) as raised:
                    await self._try(url, *addrs, allow_private=True, rec=rec)  # even with the switch on
                self.assertEqual(raised.exception.kind, "blocked")
                self.assertEqual(rec.requests, [])

    async def test_every_resolved_address_is_checked(self) -> None:
        # one public + one internal answer (typical rebinding trick): rejected
        with self.assertRaises(DiscoveryError) as raised:
            await self._try("https://rebind.example/v1", PUBLIC_IP, "10.0.0.9")
        self.assertEqual(raised.exception.kind, "blocked")
        with self.assertRaises(DiscoveryError):
            await self._try("https://rebind.example/v1", PUBLIC_IP, "169.254.169.254", allow_private=True)

    async def test_connection_goes_to_the_vetted_address_not_a_second_lookup(self) -> None:
        answers = iter([[PUBLIC_IP], ["127.0.0.1"]])  # a rebinding server would answer differently the 2nd time

        async def flipping(host: str, port: int) -> list[str]:
            return next(answers)

        rec = Recorder(payload={"data": [{"id": "m"}]})
        models = await discover_models(
            make_provider("openai_chat", base_url="https://flip.example/v1"),
            transport=rec.transport(),
            resolver=flipping,
            allow_private=False,
        )
        self.assertEqual(models, ["m"])
        self.assertEqual(rec.requests[0].url.host, PUBLIC_IP)

    async def test_ipv6_public_address_is_bracketed(self) -> None:
        rec = Recorder(payload={"data": []})
        await self._try("https://v6.example/v1", "2606:4700:4700::1111", rec=rec)
        self.assertEqual(rec.requests[0].url.host, "2606:4700:4700::1111")

    async def test_unresolvable_host(self) -> None:
        async def fail(host: str, port: int) -> list[str]:
            raise OSError("no such host")

        with self.assertRaises(DiscoveryError) as raised:
            await discover_models(
                make_provider("openai_chat", base_url="https://nx.example"),
                transport=Recorder(payload={}).transport(),
                resolver=fail,
            )
        self.assertEqual(raised.exception.kind, "network")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "ftp://example.com",
        "file:///etc/passwd",
        "example.com/v1",
        "https://user:pw@example.com/v1",
        "https://user@example.com",
        "https://example.com/v1?x=1",
        "https://example.com/v1#frag",
        "https://exa mple.com",
        "https://example.com/\nHost: evil",
        "https://example.com:99999",
        "https://",
        "javascript:alert(1)",
    ],
)
def test_invalid_base_urls_are_rejected(bad: str) -> None:
    with pytest.raises(DiscoveryError) as raised:
        parse_base_url(bad)
    assert raised.value.kind == "invalid_url"


def test_valid_base_urls() -> None:
    assert parse_base_url("https://api.example.com/v1/") == ("https", "api.example.com", 443, "/v1")
    assert parse_base_url("http://localhost:11434") == ("http", "localhost", 11434, "")
    assert parse_base_url("http://[::1]:8080/x") == ("http", "::1", 8080, "/x")


def test_error_never_contains_the_request_details() -> None:
    # DiscoveryError messages are fixed strings; make sure none can be built from a key.
    err = DiscoveryError("认证失败（HTTP 401），请检查 API Key。", kind="auth", status=401)
    assert SECRET not in json.dumps(str(err))
