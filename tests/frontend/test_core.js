"use strict";
// Pure-function tests for static/js/core.js. Run: node tests/frontend/test_core.js
const assert = require("node:assert/strict");
const path = require("node:path");
const Core = require(path.join(__dirname, "..", "..", "static", "js", "core.js"));

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// --- fake fetch helpers ---------------------------------------------------
function fakeResponse(status, body, contentType) {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  const ct =
    contentType !== undefined
      ? contentType
      : typeof body === "string"
        ? "text/plain"
        : "application/json";
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (k) => (k.toLowerCase() === "content-type" ? ct : null) },
    text: async () => text,
  };
}

function makeClient(handler, extra = {}) {
  const calls = [];
  let token = extra.token || "";
  const client = Core.createApiClient({
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      return handler(url, init, calls.length);
    },
    getToken: () => token,
    onAuthRequired: extra.onAuthRequired
      ? async (info) => {
          const t = await extra.onAuthRequired(info);
          if (t) token = t;
          return t;
        }
      : undefined,
    prefix: extra.prefix || "/",
  });
  return { client, calls, getToken: () => token };
}

// --- escaping / XSS --------------------------------------------------------
test("escapeHtml escapes all five special chars incl. single quote", () => {
  assert.equal(
    Core.escapeHtml(`<a href="x" onclick='y'>&</a>`),
    "&lt;a href=&quot;x&quot; onclick=&#39;y&#39;&gt;&amp;&lt;/a&gt;"
  );
  assert.equal(Core.escapeHtml(null), "");
  assert.equal(Core.escapeHtml(undefined), "");
  assert.equal(Core.escapeHtml(0), "0");
});

test("numText coerces dangerous values to plain numbers", () => {
  assert.equal(Core.numText(3), "3");
  assert.equal(Core.numText("7"), "7");
  assert.equal(Core.numText('<img src=x onerror="alert(1)">'), "0");
  assert.equal(Core.numText(null), "0");
  assert.equal(Core.numText(undefined), "0");
  assert.equal(Core.numText(NaN), "0");
  assert.equal(Core.numText(Infinity), "0");
  assert.equal(Core.numText({}), "0");
});

test("renderFormattedText never emits raw user HTML", () => {
  const evil = [
    '<script>alert(1)</script>',
    "<img src=x onerror='alert(1)'>",
    '**<b onclick="x">bold</b>**',
    "`<i>`",
    "```\n<svg onload=1>\n```",
    "> <u>q</u>",
    "- <a href='javascript:1'>l</a>",
    "# <h1>",
  ].join("\n");
  const html = Core.renderFormattedText(evil);
  // Only our own tags (with fmt-* classes) may appear; no user-controlled tag.
  const tags = html.match(/<\/?[a-zA-Z][^>]*>/g) || [];
  for (const t of tags) {
    assert.match(
      t,
      /^<\/?(p|br\/?|strong|em|code|pre|ul|ol|li|blockquote|hr|div)(\s+(class|value)="[^"]*")*\s*\/?>$/,
      `unexpected tag: ${t}`
    );
  }
  assert.ok(!html.includes("<script"));
  assert.ok(!html.includes("<img"));
  assert.ok(!/onerror=['"]?alert/.test(html.replace(/&#39;|&quot;/g, "")) || html.includes("&lt;img"));
  // single quotes in text must be entity-escaped
  assert.ok(html.includes("&#39;"));
});

test("renderFormattedText: basic markdown", () => {
  const html = Core.renderFormattedText("# T\n\n**b** and *i* and `c`\n\n- a\n- b\n\n1. x\n2. y");
  assert.match(html, /fmt-heading fmt-h1/);
  assert.match(html, /<strong class="fmt-strong">b<\/strong>/);
  assert.match(html, /<em class="fmt-em">i<\/em>/);
  assert.match(html, /<code class="fmt-code">c<\/code>/);
  assert.match(html, /<ul class="fmt-ul">/);
  assert.match(html, /<ol class="fmt-ol">/);
  assert.equal(Core.renderFormattedText(""), "");
  assert.equal(Core.renderFormattedText(null), "");
});

test("renderFormattedText ignores forged NUL placeholders", () => {
  const html = Core.renderFormattedText("\u0000BLOCKCODE_0\u0000 \u0000CODE_0\u0000 x");
  assert.ok(!html.includes("\u0000"));
});

test("renderFormattedBlocks: blocks join to renderFormattedText and are stable under append", () => {
  const a = Core.renderFormattedBlocks("para one\n\npara two");
  const b = Core.renderFormattedBlocks("para one\n\npara two more");
  assert.equal(a.length, 2);
  assert.equal(b.length, 2);
  assert.equal(a[0], b[0]); // earlier blocks unchanged => no DOM rewrite needed
  assert.notEqual(a[1], b[1]);
  assert.equal(a.join(""), Core.renderFormattedText("para one\n\npara two"));
});

// --- bounded map -------------------------------------------------------------
test("boundedSet caps size and evicts the oldest", () => {
  const m = new Map();
  for (let i = 0; i < 10; i++) Core.boundedSet(m, "k" + i, true, 4);
  assert.equal(m.size, 4);
  assert.deepEqual([...m.keys()], ["k6", "k7", "k8", "k9"]);
  Core.boundedSet(m, "k6", false, 4); // refresh recency
  Core.boundedSet(m, "k10", true, 4);
  assert.ok(m.has("k6"));
  assert.ok(!m.has("k7"));
});

// --- url helpers -------------------------------------------------------------
test("prefixFromScriptPath supports root and sub-path deployments", () => {
  assert.equal(Core.prefixFromScriptPath("/static/js/app.js"), "/");
  assert.equal(Core.prefixFromScriptPath("/arena/static/js/app.js"), "/arena/");
  assert.equal(Core.prefixFromScriptPath("/a/b/static/js/core.js"), "/a/b/");
  assert.equal(Core.prefixFromScriptPath(""), "/");
  assert.equal(Core.prefixFromScriptPath("/weird.js"), "/");
});

test("joinUrl / buildEventsUrl", () => {
  assert.equal(Core.joinUrl("/arena/", "/api/state"), "/arena/api/state");
  assert.equal(Core.joinUrl("/", "api/state"), "/api/state");
  assert.equal(Core.buildEventsUrl("/", ""), "/api/events");
  assert.equal(Core.buildEventsUrl("/x/", "a b&c"), "/x/api/events?token=a%20b%26c");
});

test("buildEventsUrl carries the SSE v2 resume point as ?last_event_id= (a re-created EventSource sends no Last-Event-ID)", () => {
  assert.equal(Core.buildEventsUrl("/", "", 42), "/api/events?last_event_id=42");
  assert.equal(Core.buildEventsUrl("/", "tok", 42), "/api/events?token=tok&last_event_id=42");
  assert.equal(Core.buildEventsUrl("/arena/", "", "7"), "/arena/api/events?last_event_id=7");
  assert.equal(Core.buildEventsUrl("/", "", 12.9), "/api/events?last_event_id=12");
  // nothing useful to resume from: a plain URL (the server answers with a snapshot)
  for (const none of [0, -3, NaN, null, undefined, "", "abc", Infinity]) {
    assert.equal(Core.buildEventsUrl("/", "", none), "/api/events", String(none));
  }
});

// --- v0.12 formatting --------------------------------------------------------
test("formatCount / formatTokensCount / formatCost", () => {
  assert.equal(Core.formatCount(0), "0");
  assert.equal(Core.formatCount(999), "999");
  assert.equal(Core.formatCount(6057), "6,057");
  assert.equal(Core.formatCount(12345), "12.3K");
  assert.equal(Core.formatCount(2_500_000), "2.50M");
  assert.equal(Core.formatCount(3_100_000_000), "3.10B");
  assert.equal(Core.formatCount("<b>"), "0");
  assert.equal(Core.formatCount(null), "0");
  assert.equal(Core.formatTokensCount(0), "0 Tokens");
  assert.equal(Core.formatTokensCount(1500), "1.5K Tokens");
  assert.equal(Core.formatTokensCount(2_930_000), "2.93M Tokens");
  assert.equal(Core.formatTokensCount(undefined), "0 Tokens");
  assert.equal(Core.formatCost(0), "$0.0000");
  assert.equal(Core.formatCost(0.0123456), "$0.0123");
  assert.equal(Core.formatCost(12.5, 2), "$12.50");
  assert.equal(Core.formatCost(-1), "$0.0000");
  assert.equal(Core.formatCost("x"), "$0.0000");
});

test("formatClockTime shows HH:MM:SS of a ledger timestamp and never throws", () => {
  const local = new Date(2026, 10, 17, 8, 5, 9);
  assert.equal(Core.formatClockTime(local.toISOString()), "08:05:09"); // local time of the viewer
  assert.equal(Core.formatClockTime("2026-11-17T08:05:09.123"), "08:05:09"); // naive timestamps are local
  assert.equal(Core.formatClockTime(""), "");
  assert.equal(Core.formatClockTime(null), "");
  assert.equal(Core.formatClockTime(undefined), "");
  assert.equal(Core.formatClockTime("12:34:56 whatever"), "12:34:56"); // not a date: pick the time out
  assert.equal(Core.formatClockTime("garbage"), "");
});

// --- backoff -----------------------------------------------------------------
test("computeBackoff: exponential, capped at 30s, jittered", () => {
  const noJitter = { rand: () => 0 };
  assert.equal(Core.computeBackoff(0, noJitter), 1000);
  assert.equal(Core.computeBackoff(1, noJitter), 2000);
  assert.equal(Core.computeBackoff(2, noJitter), 4000);
  assert.equal(Core.computeBackoff(4, noJitter), 16000);
  assert.equal(Core.computeBackoff(5, noJitter), 30000);
  assert.equal(Core.computeBackoff(50, noJitter), 30000);
  assert.equal(Core.computeBackoff(1e9, noJitter), 30000);
  for (let i = 0; i < 200; i++) {
    const d = Core.computeBackoff(8);
    assert.ok(d <= 30000 && d >= 0.7 * 30000 - 1, `delay out of range: ${d}`);
  }
  const lo = Core.computeBackoff(3, { rand: () => 0.999 });
  const hi = Core.computeBackoff(3, { rand: () => 0 });
  assert.ok(lo < hi);
});

// --- error detail formatting -------------------------------------------------
test("formatDetail never yields [object Object]", () => {
  const cases = [
    "plain",
    [{ loc: ["body", "port"], msg: "field required", type: "x" }],
    [{ msg: "a" }, { msg: "b" }],
    { detail: "nested" },
    { msg: "m" },
    { foo: { bar: 1 } },
    [["x"], { y: 1 }],
    42,
    true,
    null,
    undefined,
    {},
    [],
  ];
  for (const c of cases) {
    const out = Core.formatDetail(c);
    assert.equal(typeof out, "string");
    assert.ok(!out.includes("[object Object]"), JSON.stringify(c));
  }
  assert.equal(
    Core.formatDetail([{ loc: ["body", "port"], msg: "field required" }]),
    "port: field required"
  );
  assert.equal(Core.formatDetail([{ msg: "a" }, { msg: "b" }]), "a；b");
  assert.equal(Core.formatDetail({ foo: 1 }), '{"foo":1}');
});

test("describeHttpError gives a clear message per status", () => {
  for (const st of [400, 401, 403, 404, 409, 413, 422, 429, 500, 502, 503]) {
    const m = Core.describeHttpError(st, "详情");
    assert.ok(m.includes("详情"), `${st}: ${m}`);
    assert.ok(m.length > 4);
  }
  assert.match(Core.describeHttpError(401, ""), /令牌/);
  assert.match(Core.describeHttpError(403, ""), /拒绝/);
  assert.match(Core.describeHttpError(409, ""), /冲突/);
  assert.match(Core.describeHttpError(422, ""), /校验/);
  assert.match(Core.describeHttpError(500, ""), /500/);
  assert.match(Core.describeHttpError(418, ""), /418/);
});

// --- apiRequest --------------------------------------------------------------
test("api: success returns parsed JSON; adds JSON header only with a body", async () => {
  const { client, calls } = makeClient(() => fakeResponse(200, { ok: true, n: 1 }));
  assert.deepEqual(await client.get("api/state"), { ok: true, n: 1 });
  assert.equal(calls[0].url, "/api/state");
  assert.equal(calls[0].init.headers["Content-Type"], undefined);
  await client.post("api/control/new", { a: 1 });
  assert.equal(calls[1].init.method, "POST");
  assert.equal(calls[1].init.body, '{"a":1}');
  assert.equal(calls[1].init.headers["Content-Type"], "application/json");
  await client.post("api/control/new");
  assert.equal(calls[2].init.body, undefined);
});

test("api: sub-path prefix is applied", async () => {
  const { client, calls } = makeClient(() => fakeResponse(200, {}), { prefix: "/arena/" });
  await client.get("/api/state");
  assert.equal(calls[0].url, "/arena/api/state");
});

test("api: token header only when a token exists", async () => {
  const a = makeClient(() => fakeResponse(200, {}));
  await a.client.get("api/state");
  assert.equal("X-Arena-Token" in a.calls[0].init.headers, false);
  const b = makeClient(() => fakeResponse(200, {}), { token: "tok" });
  await b.client.get("api/state");
  assert.equal(b.calls[0].init.headers["X-Arena-Token"], "tok");
});

test("api: 500 HTML does not throw SyntaxError and is not dumped into the message", async () => {
  const { client } = makeClient(() =>
    fakeResponse(500, "<html><body><h1>Internal Server Error</h1></body></html>", "text/html")
  );
  await assert.rejects(client.get("api/state"), (e) => {
    assert.ok(e instanceof Core.ApiError);
    assert.ok(!(e instanceof SyntaxError));
    assert.equal(e.status, 500);
    assert.ok(!e.message.includes("<html"));
    assert.match(e.message, /500/);
    return true;
  });
});

test("api: 500 plain text shows the short text; huge text is dropped", async () => {
  const a = makeClient(() => fakeResponse(500, "Internal Server Error"));
  await assert.rejects(a.client.get("x"), (e) => /Internal Server Error/.test(e.message));
  const b = makeClient(() => fakeResponse(500, "x".repeat(5000)));
  await assert.rejects(b.client.get("x"), (e) => e.message.length < 100);
});

test("api: malformed JSON error body with json content-type is handled", async () => {
  const { client } = makeClient(() => fakeResponse(502, "{not json", "application/json"));
  await assert.rejects(client.get("x"), (e) => e instanceof Core.ApiError && e.status === 502);
});

test("api: 422 array detail becomes a readable string", async () => {
  const { client } = makeClient(() =>
    fakeResponse(422, {
      detail: [
        { loc: ["body", "context_window_tokens"], msg: "value is not a valid integer", type: "type_error" },
      ],
    })
  );
  await assert.rejects(client.post("api/config/llm", {}), (e) => {
    assert.ok(!e.message.includes("[object Object]"));
    assert.match(e.message, /校验失败/);
    assert.match(e.message, /context_window_tokens: value is not a valid integer/);
    return true;
  });
});

test("api: object/array detail on 400/403/409 is stringified", async () => {
  for (const [st, detail] of [
    [400, { error: "bad" }],
    [403, { reason: { x: 1 } }],
    [409, ["a", { msg: "b" }]],
    [400, "字符串"],
  ]) {
    const { client } = makeClient(() => fakeResponse(st, { detail }));
    await assert.rejects(client.get("x"), (e) => {
      assert.equal(e.status, st);
      assert.ok(!e.message.includes("[object Object]"), e.message);
      return true;
    });
  }
});

test("api: 200 with non-JSON body (e.g. proxy login page) is a clear error", async () => {
  const { client } = makeClient(() => fakeResponse(200, "<html>login</html>", "text/html"));
  await assert.rejects(client.get("x"), (e) => e.kind === "bad-response" && !(e instanceof SyntaxError));
});

test("api: 204 / empty body returns null", async () => {
  const { client } = makeClient(() => fakeResponse(204, "", "application/json"));
  assert.equal(await client.get("x"), null);
});

test("api: network failure becomes ApiError(kind=network)", async () => {
  const client = Core.createApiClient({
    fetchImpl: async () => {
      throw new TypeError("Failed to fetch");
    },
  });
  await assert.rejects(client.get("x"), (e) => e.kind === "network" && /无法连接/.test(e.message));
});

test("api: 401 without handler / cancelled prompt rejects with auth error", async () => {
  const a = makeClient(() => fakeResponse(401, { detail: "no token" }));
  await assert.rejects(a.client.get("x"), (e) => e.status === 401);
  const b = makeClient(() => fakeResponse(401, { detail: "no token" }), {
    onAuthRequired: async () => null,
  });
  await assert.rejects(b.client.get("x"), (e) => e.status === 401 && e.kind === "auth");
  assert.equal(b.calls.length, 1);
});

test("api: 401 prompts for a token, then retries with X-Arena-Token", async () => {
  const prompts = [];
  const { client, calls, getToken } = makeClient(
    (url, init) =>
      init.headers["X-Arena-Token"] === "good"
        ? fakeResponse(200, { session: { id: 1 } })
        : fakeResponse(401, { detail: "Invalid or missing API token" }),
    {
      onAuthRequired: async (info) => {
        prompts.push(info);
        return "good";
      },
    }
  );
  const res = await client.get("api/state");
  assert.deepEqual(res, { session: { id: 1 } });
  assert.equal(calls.length, 2);
  assert.equal(prompts.length, 1);
  assert.equal(prompts[0].hadToken, false);
  assert.equal(prompts[0].detail, "Invalid or missing API token");
  assert.equal(getToken(), "good");
});

test("api: wrong token prompts again (hadToken=true) until valid", async () => {
  const answers = ["bad2", "good"];
  const infos = [];
  const { client } = makeClient(
    (url, init) =>
      init.headers["X-Arena-Token"] === "good"
        ? fakeResponse(200, { ok: 1 })
        : fakeResponse(401, { detail: "bad" }),
    {
      token: "bad1",
      onAuthRequired: async (info) => {
        infos.push(info);
        return answers.shift();
      },
    }
  );
  assert.deepEqual(await client.get("x"), { ok: 1 });
  assert.equal(infos.length, 2);
  assert.ok(infos.every((i) => i.hadToken));
});

test("api: endless 401 stops after maxAuthAttempts", async () => {
  let n = 0;
  const client = Core.createApiClient({
    fetchImpl: async () => fakeResponse(401, { detail: "x" }),
    getToken: () => "t",
    onAuthRequired: async () => "t" + ++n,
    maxAuthAttempts: 3,
  });
  await assert.rejects(client.get("x"), (e) => e.status === 401);
  assert.equal(n, 3);
});

// --- v0.10: providers / models / bindings ------------------------------------
const PROVIDERS = [
  {
    id: "p1",
    name: "DeepSeek",
    protocol: "openai_chat",
    base_url: "https://api.deepseek.com",
    api_key_set: true,
    read_only: false,
    models: [
      { id: "ds-chat", input_price_per_m: 0.27, output_price_per_m: 1.1, context_window_tokens: 128000 },
    ],
  },
  {
    id: "p2",
    name: "Claude",
    protocol: "anthropic_messages",
    base_url: "https://api.anthropic.com",
    api_key_set: true,
    models: [{ id: "claude/x", input_price_per_m: 3, output_price_per_m: 15, context_window_tokens: 200000 }],
  },
];

test("protocol labels are localized for all three protocols", () => {
  assert.deepEqual(
    Core.PROTOCOLS.map((p) => p.id),
    ["openai_chat", "openai_responses", "anthropic_messages"]
  );
  assert.match(Core.protocolLabel("openai_chat"), /Chat Completions/);
  assert.match(Core.protocolLabel("openai_responses"), /Responses/);
  assert.match(Core.protocolLabel("anthropic_messages"), /Anthropic/);
  assert.match(Core.protocolLabel("openai_chat"), /[\u4e00-\u9fff]/);
  assert.equal(Core.protocolLabel("weird"), "weird");
  assert.equal(Core.protocolLabel(""), "未知协议");
});

test("reasoning depth set is exactly low/medium/high/xhigh", () => {
  assert.deepEqual(
    Core.REASONING_DEPTHS.map((d) => d.id),
    ["low", "medium", "high", "xhigh"]
  );
});

test("validatePrice: required, never defaults to 0, finite and non-negative", () => {
  for (const bad of ["", "  ", null, undefined, "abc", "-1", "NaN", "Infinity", "1e999", "1,5", "0x10", "--1"]) {
    assert.equal(Core.validatePrice(bad, "输入价格").ok, false, String(bad));
  }
  assert.match(Core.validatePrice("", "输入价格").error, /输入价格为必填项/);
  assert.deepEqual(Core.validatePrice("0"), { ok: true, value: 0 });
  assert.deepEqual(Core.validatePrice(" 0.27 "), { ok: true, value: 0.27 });
  assert.deepEqual(Core.validatePrice("1e2"), { ok: true, value: 100 });
  assert.deepEqual(Core.validatePrice(".5"), { ok: true, value: 0.5 });
  assert.deepEqual(Core.validatePrice(3), { ok: true, value: 3 });
});

test("buildModelPayload: input + output prices mandatory, cache prices optional (null = input price), single model only", () => {
  const ok = Core.buildModelPayload({ model_id: " m1 ", input_price_per_m: "1", output_price_per_m: "2" });
  assert.deepEqual(ok, {
    ok: true,
    payload: {
      model_id: "m1",
      input_price_per_m: 1,
      cache_read_price_per_m: null,
      cache_write_price_per_m: null,
      output_price_per_m: 2,
    },
  });
  const noIn = Core.buildModelPayload({ model_id: "m1", input_price_per_m: "", output_price_per_m: "2" });
  assert.equal(noIn.ok, false);
  assert.match(noIn.errors.join(), /输入价格为必填项/);
  const noOut = Core.buildModelPayload({ model_id: "m1", input_price_per_m: "1", output_price_per_m: "" });
  assert.match(noOut.errors.join(), /输出价格为必填项/);
  const both = Core.buildModelPayload({ model_id: "m1" });
  assert.equal(both.errors.length, 2);
  assert.equal(Core.buildModelPayload({ model_id: "", input_price_per_m: "1", output_price_per_m: "1" }).ok, false);
  // arrays / bulk are never produced: payload.model_id is a string
  assert.equal(typeof ok.payload.model_id, "string");
  const withCtx = Core.buildModelPayload({
    model_id: "m",
    input_price_per_m: "1",
    output_price_per_m: "1",
    context_window_tokens: "128000",
  });
  assert.equal(withCtx.payload.context_window_tokens, 128000);
  const badCtx = Core.buildModelPayload({
    model_id: "m",
    input_price_per_m: "1",
    output_price_per_m: "1",
    context_window_tokens: "12",
  });
  assert.equal(badCtx.ok, false);
});

test("modelFormMode: duplicate id switches to edit, new id is add", () => {
  assert.equal(Core.modelFormMode(PROVIDERS[0], "ds-chat"), "edit");
  assert.equal(Core.modelFormMode(PROVIDERS[0], "other"), "add");
  assert.equal(Core.modelFormMode(null, "x"), "add");
  assert.equal(Core.modelFormMode(PROVIDERS[1], "claude/x"), "edit"); // slash ids are fine
});

test("validateBaseUrl: http(s) only, no userinfo, no control chars", () => {
  assert.equal(Core.validateBaseUrl("https://api.example.com/v1").ok, true);
  assert.equal(Core.validateBaseUrl("http://localhost:11434").ok, true);
  for (const bad of ["", "ftp://x.com", "javascript:alert(1)", "not a url", "https://u:p@x.com", "https://x.com/\nfoo", "https://x.com/\u0000"]) {
    assert.equal(Core.validateBaseUrl(bad).ok, false, bad);
  }
});

test("buildProviderPayload: create requires every field incl. key", () => {
  const good = Core.buildProviderPayload(
    { name: " X ", protocol: "openai_responses", base_url: "https://a.com", api_key: " k " },
    null
  );
  assert.deepEqual(good, {
    ok: true,
    payload: {
      name: "X",
      protocol: "openai_responses",
      base_url: "https://a.com",
      api_key: "k",
      auto_append_path: true,
    },
  });
  // v0.11: auto_append_path is part of the create body (defaults on)
  const manual = Core.buildProviderPayload(
    {
      name: "X",
      protocol: "openai_chat",
      base_url: "https://a.com/v1/chat/completions",
      api_key: "k",
      auto_append_path: false,
    },
    null
  );
  assert.equal(manual.ok, true);
  assert.equal(manual.payload.auto_append_path, false);
  const noKey = Core.buildProviderPayload(
    { name: "X", protocol: "openai_chat", base_url: "https://a.com", api_key: "" },
    null
  );
  assert.equal(noKey.ok, false);
  assert.match(noKey.errors.join(), /API Key 为必填项/);
  const badProto = Core.buildProviderPayload(
    { name: "X", protocol: "grpc", base_url: "https://a.com", api_key: "k" },
    null
  );
  assert.match(badProto.errors.join(), /协议/);
});

test("buildProviderPayload: edit sends only changes; protocol/base_url change needs a key", () => {
  const existing = PROVIDERS[0];
  const same = Core.buildProviderPayload(
    { name: "DeepSeek", protocol: "openai_chat", base_url: "https://api.deepseek.com", api_key: "" },
    existing
  );
  assert.deepEqual(same, { ok: true, payload: {} });
  const rename = Core.buildProviderPayload(
    { name: "DS2", protocol: "openai_chat", base_url: "https://api.deepseek.com", api_key: "" },
    existing
  );
  assert.deepEqual(rename.payload, { name: "DS2" });
  const rotate = Core.buildProviderPayload(
    { name: "DeepSeek", protocol: "openai_chat", base_url: "https://api.deepseek.com", api_key: "new" },
    existing
  );
  assert.deepEqual(rotate.payload, { api_key: "new" });
  const urlNoKey = Core.buildProviderPayload(
    { name: "DeepSeek", protocol: "openai_chat", base_url: "https://evil.com", api_key: "" },
    existing
  );
  assert.equal(urlNoKey.ok, false);
  assert.match(urlNoKey.errors.join(), /必须重新输入 API Key/);
  const protoNoKey = Core.buildProviderPayload(
    { name: "DeepSeek", protocol: "anthropic_messages", base_url: "https://api.deepseek.com", api_key: "" },
    existing
  );
  assert.equal(protoNoKey.ok, false);
  const protoKey = Core.buildProviderPayload(
    { name: "DeepSeek", protocol: "anthropic_messages", base_url: "https://api.deepseek.com", api_key: "k" },
    existing
  );
  assert.deepEqual(protoKey.payload, { protocol: "anthropic_messages", api_key: "k" });
});

test("normalizeProviders / normalizeModelIds are tolerant", () => {
  assert.equal(Core.normalizeProviders({ providers: PROVIDERS }).length, 2);
  assert.equal(Core.normalizeProviders(PROVIDERS).length, 2);
  assert.deepEqual(Core.normalizeProviders(null), []);
  assert.deepEqual(Core.normalizeProviders([{ id: "x" }])[0].models, []);
  assert.deepEqual(Core.normalizeModelIds({ models: ["a", "a", { id: "b" }, "", 5, null] }), ["a", "b"]);
  assert.deepEqual(Core.normalizeModelIds(["z"]), ["z"]);
});

test("bindingsLocked only while running", () => {
  assert.equal(Core.bindingsLocked({ status: "running" }), true);
  for (const st of ["paused", "idle", "finished", "ended", undefined]) {
    assert.equal(Core.bindingsLocked({ status: st }), false, String(st));
  }
  assert.equal(Core.bindingsLocked(null), false);
});

test("buildBindingsPayload: skips unbound rows, validates, uses model context window", () => {
  const rows = [
    { actor_id: "moderator", provider_id: "p1", model_id: "ds-chat", reasoning_depth: "xhigh" },
    { actor_id: "p01", provider_id: "p2", model_id: "claude/x", reasoning_depth: "low" },
    { actor_id: "p02", provider_id: "", model_id: "", reasoning_depth: "high" },
  ];
  const r = Core.buildBindingsPayload(rows, PROVIDERS);
  assert.equal(r.ok, true);
  assert.deepEqual(r.payload, {
    bindings: {
      moderator: { provider_id: "p1", model_id: "ds-chat", reasoning_depth: "xhigh", context_window_tokens: 128000 },
      p01: { provider_id: "p2", model_id: "claude/x", reasoning_depth: "low", context_window_tokens: 200000 },
    },
  });
  const half = Core.buildBindingsPayload(
    [{ actor_id: "p01", label: "甲", provider_id: "p1", model_id: "" }],
    PROVIDERS
  );
  assert.equal(half.ok, false);
  assert.match(half.errors[0], /甲/);
  const ghost = Core.buildBindingsPayload(
    [{ actor_id: "p01", provider_id: "p1", model_id: "nope" }],
    PROVIDERS
  );
  assert.equal(ghost.ok, false);
  const badDepth = Core.buildBindingsPayload(
    [{ actor_id: "p01", provider_id: "p1", model_id: "ds-chat", reasoning_depth: "ultra" }],
    PROVIDERS
  );
  assert.equal(badDepth.payload.bindings.p01.reasoning_depth, "high");
});

test("applyModeratorToAll copies the moderator binding to every player", () => {
  const rows = [
    { actor_id: "moderator", provider_id: "p1", model_id: "ds-chat", reasoning_depth: "low" },
    { actor_id: "p01", provider_id: "", model_id: "", reasoning_depth: "high" },
    { actor_id: "p02", provider_id: "p2", model_id: "claude/x", reasoning_depth: "medium" },
  ];
  const out = Core.applyModeratorToAll(rows);
  assert.deepEqual(out.map((r) => [r.provider_id, r.model_id, r.reasoning_depth]), [
    ["p1", "ds-chat", "low"],
    ["p1", "ds-chat", "low"],
    ["p1", "ds-chat", "low"],
  ]);
});

test("bindingSummary: bound / unbound / stale", () => {
  assert.equal(
    Core.bindingSummary({ provider_id: "p1", model_id: "ds-chat", reasoning_depth: "high" }, PROVIDERS),
    "DeepSeek / ds-chat · high"
  );
  assert.equal(Core.bindingSummary(null, PROVIDERS), "未绑定");
  assert.match(Core.bindingSummary({ provider_id: "zz", model_id: "m" }, PROVIDERS), /失效/);
  assert.match(Core.bindingSummary({ provider_id: "p1", model_id: "gone" }, PROVIDERS), /失效/);
});

test("sessionCostUsd reads the server accumulated total", () => {
  assert.equal(Core.sessionCostUsd({ total_cost_usd: 1.25 }), 1.25);
  assert.equal(Core.sessionCostUsd({}), 0);
  assert.equal(Core.sessionCostUsd(null), 0);
  assert.equal(Core.sessionCostUsd({ total_cost_usd: "x" }), 0);
});

test("api: put / patch / del(with body) use the right methods", async () => {
  const { client, calls } = makeClient(() => fakeResponse(200, { ok: true }));
  await client.put("api/settings/bindings", { bindings: {} });
  await client.patch("api/providers/p1", { name: "n" });
  await client.del("api/providers/p1/models", { model_id: "a/b" });
  assert.deepEqual(calls.map((c) => c.init.method), ["PUT", "PATCH", "DELETE"]);
  assert.equal(calls[2].init.body, '{"model_id":"a/b"}');
  assert.equal(calls[2].init.headers["Content-Type"], "application/json");
});

// --- v0.11: layout / card size ------------------------------------------------
function memoryStorage(init) {
  const store = new Map(Object.entries(init || {}));
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    _map: store,
  };
}

test("normalizeLayoutState clamps the channel width, validates height, defaults everything (no right column any more)", () => {
  assert.deepEqual(Core.normalizeLayoutState(null), {
    boardCollapsed: false,
    leftCollapsed: false,
    boardHeight: null,
    leftWidth: Core.LAYOUT_DEFAULTS.leftWidth,
  });
  const s = Core.normalizeLayoutState({
    boardCollapsed: 1,
    leftCollapsed: "x",
    leftWidth: 5,
    rightWidth: 99999,
    rightCollapsed: true,
    boardHeight: 40,
  });
  assert.equal(s.boardCollapsed, true);
  assert.equal(s.leftCollapsed, true);
  assert.equal(s.leftWidth, Core.PANEL_LIMITS.left.min);
  assert.equal("rightWidth" in s || "rightCollapsed" in s, false);
  assert.equal(Core.PANEL_LIMITS.right, undefined);
  assert.equal(s.boardHeight, null); // below the minimum => natural height
  assert.equal(Core.normalizeLayoutState({ boardHeight: 320 }).boardHeight, 320);
  assert.equal(Core.normalizeLayoutState({ boardHeight: "abc" }).boardHeight, null);
});

test("layout state round-trips through storage; broken JSON falls back (card size tiers are gone)", () => {
  const storage = memoryStorage();
  assert.equal(Core.loadLayoutState(storage).boardCollapsed, false);
  const saved = Core.saveLayoutState(storage, {
    boardCollapsed: true,
    leftCollapsed: true,
    boardHeight: 240,
    leftWidth: 300,
  });
  assert.equal(saved.boardCollapsed, true);
  assert.deepEqual(Core.loadLayoutState(storage), saved);
  storage.setItem(Core.LAYOUT_STORAGE_KEY, "{not json");
  assert.deepEqual(Core.loadLayoutState(storage), Core.normalizeLayoutState(null));

  // v0.12 removed the four card-size tiers: players are fixed-size tiles
  for (const gone of ["CARD_SIZES", "CARD_SIZE_STORAGE_KEY", "normalizeCardSize", "loadCardSize", "saveCardSize"]) {
    assert.equal(Core[gone], undefined, gone);
  }
  assert.equal(Object.keys(Core.normalizeLayoutState({ cardSize: "large" })).includes("cardSize"), false);
});

// --- v0.11: provider auto_append_path + endpoint preview ----------------------
test("normalizeProviders defaults auto_append_path for legacy providers", () => {
  const list = Core.normalizeProviders([
    { id: "a", base_url: "https://a" },
    { id: "b", base_url: "https://b", auto_append_path: false },
    { id: "c", base_url: "https://c", auto_append_path: true },
  ]);
  assert.deepEqual(list.map((p) => p.auto_append_path), [true, false, true]);
  assert.equal(Core.providerAutoAppend(list[0]), true);
  assert.equal(Core.providerAutoAppend(list[1]), false);
  assert.equal(Core.providerAutoAppend(null), true);
  assert.equal(Core.canDiscoverModels(list[1]), false);
  assert.equal(Core.canDiscoverModels({ ...list[0], read_only: true }), false);
  assert.equal(Core.canDiscoverModels(list[0]), true);
});

test("buildProviderPayload: auto_append_path change is an endpoint change (needs key)", () => {
  const existing = {
    name: "P",
    protocol: "openai_chat",
    base_url: "https://a.com",
    api_key_set: true,
    auto_append_path: true,
  };
  const noKey = Core.buildProviderPayload(
    {
      name: "P",
      protocol: "openai_chat",
      base_url: "https://a.com",
      api_key: "",
      auto_append_path: false,
    },
    existing
  );
  assert.equal(noKey.ok, false);
  assert.match(noKey.errors.join(), /必须重新输入 API Key/);
  const withKey = Core.buildProviderPayload(
    {
      name: "P",
      protocol: "openai_chat",
      base_url: "https://a.com",
      api_key: "k",
      auto_append_path: false,
    },
    existing
  );
  assert.deepEqual(withKey.payload, { auto_append_path: false, api_key: "k" });
  const unchanged = Core.buildProviderPayload(
    {
      name: "P",
      protocol: "openai_chat",
      base_url: "https://a.com",
      api_key: "",
      auto_append_path: true,
    },
    existing
  );
  assert.deepEqual(unchanged.payload, {});
});

test("buildEndpointPreviewBody validates protocol/base URL, keeps auto flag", () => {
  assert.equal(
    Core.buildEndpointPreviewBody({ protocol: "grpc", base_url: "https://a.com" }).ok,
    false
  );
  assert.equal(
    Core.buildEndpointPreviewBody({ protocol: "openai_chat", base_url: "not a url" }).ok,
    false
  );
  const ok = Core.buildEndpointPreviewBody({
    protocol: "openai_chat",
    base_url: " https://a.com/v1 ",
    auto_append_path: false,
  });
  assert.deepEqual(ok, {
    ok: true,
    payload: {
      protocol: "openai_chat",
      base_url: "https://a.com/v1",
      auto_append_path: false,
    },
  });
  assert.equal(
    Core.buildEndpointPreviewBody({ protocol: "openai_chat", base_url: "https://a.com" })
      .payload.auto_append_path,
    true
  );
});

test("normalizeEndpointPreview tolerates missing/odd fields", () => {
  assert.deepEqual(Core.normalizeEndpointPreview(null), {
    auto_append_path: true,
    request_url: "",
    models_url: null,
    warnings: [],
  });
  const p = Core.normalizeEndpointPreview({
    auto_append_path: false,
    request_url: " https://a.com/v1/chat/completions ",
    models_url: "",
    warnings: ["注意一个坑", 5, ""],
  });
  assert.equal(p.request_url, "https://a.com/v1/chat/completions");
  assert.equal(p.models_url, null);
  assert.deepEqual(p.warnings, ["注意一个坑"]);
  assert.equal(p.auto_append_path, false);
});

// --- v0.11: model templates ---------------------------------------------------
test("normalizeTemplates/templateRows always expose the 3 editable slots", () => {
  const empty = Core.normalizeTemplates(null);
  assert.equal(empty.slots, 3);
  assert.deepEqual(empty.bySlot, {});
  const rows = Core.templateRows(empty);
  assert.deepEqual(rows.map((r) => r.slot), [1, 2, 3]);
  assert.ok(rows.every((r) => r.saved === false && !r.provider_id && r.reasoning_depth === "high"));

  const normalized = Core.normalizeTemplates({
    slots: 3,
    locked: true,
    templates: [
      {
        slot: 1,
        name: "旗舰",
        provider_id: "p1",
        model_id: "ds-chat",
        reasoning_depth: "xhigh",
        valid: true,
      },
      { slot: 3, provider_id: "p1", model_id: "ds-chat", reasoning_depth: "ultra" },
      { slot: 2, provider_id: "", model_id: "nope" }, // invalid: dropped
      { slot: 9, provider_id: "p1", model_id: "m" }, // out of range: dropped
    ],
  });
  assert.equal(normalized.locked, true);
  const merged = Core.templateRows(normalized);
  assert.equal(merged[0].name, "旗舰");
  assert.equal(merged[0].reasoning_depth, "xhigh");
  assert.equal(merged[0].saved, true);
  assert.equal(merged[2].name, "模板3"); // blank name falls back to 模板N
  assert.equal(merged[2].reasoning_depth, "high"); // unknown depth -> high
  assert.equal(merged[1].saved, false);
});

test("buildTemplatesPayload skips untouched slots, rejects partial/invalid refs", () => {
  const providers = [
    {
      id: "p1",
      name: "DeepSeek",
      protocol: "openai_chat",
      base_url: "https://a.com",
      models: [{ id: "ds-chat", input_price_per_m: 1, output_price_per_m: 2 }],
    },
  ];
  const rows = [
    { slot: 1, name: "", provider_id: "p1", model_id: "ds-chat", reasoning_depth: "xhigh" },
    { slot: 2, name: "", provider_id: "", model_id: "" },
    { slot: 3, name: "手动命名", provider_id: "p1", model_id: "ds-chat", reasoning_depth: "low" },
  ];
  const built = Core.buildTemplatesPayload(rows, providers);
  assert.equal(built.ok, true);
  assert.deepEqual(built.payload.templates, [
    { slot: 1, name: "模板1", provider_id: "p1", model_id: "ds-chat", reasoning_depth: "xhigh" },
    { slot: 3, name: "手动命名", provider_id: "p1", model_id: "ds-chat", reasoning_depth: "low" },
  ]);

  const partial = Core.buildTemplatesPayload(
    [{ slot: 1, name: "", provider_id: "p1", model_id: "", reasoning_depth: "high" }],
    providers
  );
  assert.equal(partial.ok, false);
  assert.match(partial.errors[0], /模板1：请同时选择 Provider 和模型/);

  const ghost = Core.buildTemplatesPayload(
    [{ slot: 2, name: "", provider_id: "p1", model_id: "missing", reasoning_depth: "high" }],
    providers
  );
  assert.equal(ghost.ok, false);
  assert.match(ghost.errors[0], /模板2：所选 Provider\/模型不存在/);

  assert.equal(Core.templateRowComplete({ provider_id: "p1", model_id: "m" }), true);
  assert.equal(Core.templateRowComplete({ provider_id: "p1", model_id: "" }), false);
  assert.deepEqual(
    Core.selectedActorIds([
      { actor_id: "moderator", selected: true },
      { actor_id: "p01", selected: false },
      { actor_id: "p02", selected: true },
    ]),
    ["moderator", "p02"]
  );
});

// --- v0.12.1: four prices, price source, suggestions ---------------------------------------
test("validateOptionalPrice: empty means null (input price); anything else must be a valid price", () => {
  for (const empty of ["", "  ", null, undefined]) assert.deepEqual(Core.validateOptionalPrice(empty), { ok: true, value: null });
  assert.deepEqual(Core.validateOptionalPrice("0.3"), { ok: true, value: 0.3 });
  assert.deepEqual(Core.validateOptionalPrice("0"), { ok: true, value: 0 });
  for (const bad of ["abc", "-1", "NaN", "1,5"]) assert.equal(Core.validateOptionalPrice(bad, "缓存读取价格").ok, false, bad);
  assert.match(Core.validateOptionalPrice("x", "缓存读取价格").error, /缓存读取价格/);
});

test("buildModelPayload with all four prices; invalid cache prices block the save", () => {
  const all = Core.buildModelPayload({
    model_id: "m",
    input_price_per_m: "3",
    cache_read_price_per_m: "0.3",
    cache_write_price_per_m: "3.75",
    output_price_per_m: "15",
    context_window_tokens: "200000",
  });
  assert.deepEqual(all.payload, {
    model_id: "m",
    input_price_per_m: 3,
    cache_read_price_per_m: 0.3,
    cache_write_price_per_m: 3.75,
    output_price_per_m: 15,
    context_window_tokens: 200000,
  });
  const bad = Core.buildModelPayload({ model_id: "m", input_price_per_m: "1", output_price_per_m: "1", cache_read_price_per_m: "-2", cache_write_price_per_m: "x" });
  assert.equal(bad.ok, false);
  assert.equal(bad.errors.length, 2);
  // the cache prices may be zero (free cache reads) and stay zero, not null
  assert.equal(Core.buildModelPayload({ model_id: "m", input_price_per_m: "1", output_price_per_m: "1", cache_read_price_per_m: "0" }).payload.cache_read_price_per_m, 0);
});

test("price source: the default URL, normalisation, https-only validation and the status line", () => {
  assert.equal(Core.DEFAULT_PRICING_SOURCE_URL, "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json");
  const n = Core.normalizePricingSource({ url: "https://x/y.json", fetched_at: "2026-11-17T08:00:00Z", entries: "2345", error: null });
  assert.deepEqual([n.url, n.entries, n.error, n.default_url], ["https://x/y.json", 2345, null, Core.DEFAULT_PRICING_SOURCE_URL]);
  const empty = Core.normalizePricingSource(null);
  assert.deepEqual([empty.url, empty.entries, empty.fetched_at, empty.error], ["", 0, null, null]);
  assert.equal(Core.normalizePricingSource({ error: { message: "x" } }).error.includes("[object"), false);
  for (const ok of ["https://raw.githubusercontent.com/a/b.json", " https://example.com/p.json "]) assert.equal(Core.validatePricingSourceUrl(ok).ok, true, ok);
  for (const bad of ["", "http://example.com/p.json", "ftp://x", "not a url", "https://u:p@x.com/p.json", "https://x.com/\nfoo", "javascript:alert(1)"]) {
    assert.equal(Core.validatePricingSourceUrl(bad).ok, false, JSON.stringify(bad));
  }
  assert.equal(Core.validatePricingSourceUrl(" https://example.com/p.json ").value, "https://example.com/p.json");
  const ok = Core.pricingSourceStatus({ entries: 2345, fetched_at: "2026-11-17T08:00:00Z" });
  assert.equal(ok.kind, "ok");
  assert.match(ok.text, /已加载 2,345 条 · 获取于 \d{4}-\d\d-\d\d \d\d:\d\d/);
  assert.equal(Core.pricingSourceStatus({ entries: 0 }).kind, "none");
  const err = Core.pricingSourceStatus({ entries: 10, error: "连接超时" });
  assert.equal(err.kind, "error");
  assert.match(err.text, /连接超时/);
  assert.match(err.text, /仍使用上次的 10 条/);
  assert.equal(Core.formatDateTime("garbage"), "");
  assert.equal(Core.formatDateTime(null), "");
  assert.match(Core.formatDateTime(1763366400), /^\d{4}-\d\d-\d\d \d\d:\d\d$/); // epoch seconds
  assert.match(Core.formatDateTime(1763366400000), /^\d{4}-\d\d-\d\d \d\d:\d\d$/); // epoch milliseconds
});

test("suggestions: normalised numbers (null for missing), both discover-models shapes, and the form values", () => {
  const raw = { key: "deepseek/deepseek-chat", input_price_per_m: 0.27, output_price_per_m: 1.1, cache_read_price_per_m: 0.07, cache_write_price_per_m: null, context_window_tokens: 128000 };
  assert.deepEqual(Core.normalizeSuggestion(raw), { ...raw });
  assert.equal(Core.normalizeSuggestion(null), null);
  assert.equal(Core.normalizeSuggestion([]), null);
  assert.equal(Core.normalizeSuggestion({ matched: false }), null);
  assert.equal(Core.normalizeSuggestion({ matched: true, input_price_per_m: null, output_price_per_m: null }), null, "no price at all = no match");
  assert.equal(Core.normalizeSuggestion({ input_price_per_m: "x", output_price_per_m: 2 }).input_price_per_m, null);
  assert.equal(Core.normalizeSuggestion({ input_price_per_m: -1, output_price_per_m: 2 }).input_price_per_m, null);
  assert.equal(Core.normalizeSuggestion({ input_price_per_m: 1, output_price_per_m: 2, price_source: "litellm:x" }).key, "litellm:x");

  // shape 1: per-model objects
  const a = Core.parseDiscoveredModels({ models: [{ id: "m1", suggested: raw }, { id: "m2", suggested: null }, "plain"], existing_models: [] });
  assert.deepEqual(a.ids, ["m1", "m2", "plain"]);
  assert.equal(a.suggestions.m1.input_price_per_m, 0.27);
  assert.equal(a.suggestions.m2, null);
  assert.equal(a.suggestions.plain, null);
  // shape 2: a map next to the ids
  const b = Core.parseDiscoveredModels({ models: ["m1", "m2"], suggested: { m1: raw, m2: null } });
  assert.deepEqual(b.ids, ["m1", "m2"]);
  assert.equal(b.suggestions.m1.context_window_tokens, 128000);
  assert.equal(b.suggestions.m2, null);
  // the old plain list still works
  assert.deepEqual(Core.parseDiscoveredModels(["x", "y"]).ids, ["x", "y"]);
  assert.deepEqual(Core.parseDiscoveredModels(null).ids, []);

  assert.deepEqual(Core.suggestionToForm(Core.normalizeSuggestion(raw)), { input: "0.27", cacheRead: "0.07", cacheWrite: "", output: "1.1", ctx: "128000" });
  assert.deepEqual(Core.suggestionToForm(null), { input: "", cacheRead: "", cacheWrite: "", output: "", ctx: "" });
  assert.equal(Core.formatPrice(null), "—");
  assert.equal(Core.formatPrice(undefined), "—");
  assert.equal(Core.formatPrice(3), "$3");
  assert.equal(Core.formatPrice(0.27), "$0.27");
  assert.equal(Core.formatPrice(0.0125), "$0.0125");
  assert.equal(Core.formatPrice(15), "$15");
  assert.equal(Core.formatPrice(1250), "$1250");
});

// --- v0.12.1: token / cost budget ------------------------------------------------------------
test("normalizeBudget repairs garbage; ratios and states follow the 80 % / 100 % rules", () => {
  assert.deepEqual(Core.normalizeBudget(null), { max_tokens: null, max_cost_usd: null, force_stop: false, used_tokens: 0, used_cost_usd: 0, state: "ok" });
  const b = Core.normalizeBudget({ max_tokens: "1000.7", max_cost_usd: "2.5", force_stop: 1, used_tokens: 400, used_cost_usd: 0.5, state: "warning" });
  assert.deepEqual(b, { max_tokens: 1000, max_cost_usd: 2.5, force_stop: true, used_tokens: 400, used_cost_usd: 0.5, state: "warning" });
  assert.equal(Core.normalizeBudget({ max_tokens: 0 }).max_tokens, null);
  assert.equal(Core.budgetRatio({ max_tokens: 1000, used_tokens: 250 }), 0.25);
  assert.equal(Core.budgetRatio({ max_tokens: 1000, used_tokens: 250, max_cost_usd: 1, used_cost_usd: 0.5 }), 0.5, "the larger ratio wins");
  assert.equal(Core.budgetRatio({}), null);
  assert.equal(Core.budgetStateOf({ max_tokens: 1000, used_tokens: 799 }), "ok");
  assert.equal(Core.budgetStateOf({ max_tokens: 1000, used_tokens: 800 }), "warning");
  assert.equal(Core.budgetStateOf({ max_tokens: 1000, used_tokens: 999 }), "warning");
  assert.equal(Core.budgetStateOf({ max_tokens: 1000, used_tokens: 1000 }), "reached");
  assert.equal(Core.budgetStateOf({ max_tokens: 1000, used_tokens: 5000 }), "reached");
  assert.equal(Core.budgetStateOf({ used_tokens: 5000 }), "ok", "no limit: never a warning");
  assert.equal(Core.BUDGET_WARN_RATIO, 0.8);
});

test("the budget form: empty = no limit; positive numbers only; tokens are whole numbers; 'force stop' is a flag", () => {
  const ok = Core.buildBudgetPayload({ max_tokens: " 2,000,000 ", max_cost_usd: "5.5", force_stop: true });
  assert.deepEqual(ok, { ok: true, payload: { max_tokens: 2000000, max_cost_usd: 5.5, force_stop: true } });
  assert.deepEqual(Core.buildBudgetPayload({ max_tokens: "", max_cost_usd: "   ", force_stop: false }).payload, { max_tokens: null, max_cost_usd: null, force_stop: false });
  assert.deepEqual(Core.buildBudgetPayload({}).payload, { max_tokens: null, max_cost_usd: null, force_stop: false });
  for (const bad of [{ max_tokens: "0" }, { max_tokens: "-5" }, { max_tokens: "1.5" }, { max_tokens: "abc" }, { max_tokens: "1e3" }, { max_cost_usd: "0" }, { max_cost_usd: "-1" }, { max_cost_usd: "x" }, { max_tokens: "99999999999999999999" }]) {
    const r = Core.buildBudgetPayload(bad);
    assert.equal(r.ok, false, JSON.stringify(bad));
    assert.ok(r.errors.length >= 1);
  }
  assert.equal(Core.buildBudgetPayload({ max_tokens: "x", max_cost_usd: "y" }).errors.length, 2);
  assert.equal(Core.parseBudgetField("3.", "cost").value, 3);
  assert.equal(Core.parseBudgetField(".5", "cost").value, 0.5);
  assert.equal(Core.formatPercent(0.8), "80%");
  assert.equal(Core.formatPercent(0.1234, 1), "12.3%");
  assert.equal(Core.formatPercent(-1), "0%");
  assert.equal(Core.formatPercent(500), "999%");
  assert.equal(Core.formatPercent("x"), "0%");
});

// --- v0.12.1: roster ------------------------------------------------------------------------------
test("nicknames: 1-20 characters (an emoji counts once), no control characters, unique among the others", () => {
  assert.deepEqual(Core.validateNickname("  Alice  ", []), { ok: true, value: "Alice" });
  assert.equal(Core.validateNickname("", []).ok, false);
  assert.equal(Core.validateNickname("   ", []).ok, false);
  assert.equal(Core.validateNickname("a".repeat(20), []).ok, true);
  assert.equal(Core.validateNickname("a".repeat(21), []).ok, false);
  assert.match(Core.validateNickname("a".repeat(21), []).error, /20/);
  assert.equal(Core.validateNickname("😀".repeat(20), []).ok, true, "20 emoji are 20 characters");
  assert.equal(Core.validateNickname("😀".repeat(21), []).ok, false);
  assert.equal(Core.validateNickname("小明小红小刚", []).ok, true);
  assert.equal(Core.validateNickname("a\u0000b", []).ok, false);
  assert.equal(Core.validateNickname("a\nb", []).ok, false);
  assert.equal(Core.validateNickname("Bob", ["Alice", "Bob"]).ok, false);
  assert.match(Core.validateNickname("Bob", ["Alice", "Bob"]).error, /已被其他玩家使用/);
  assert.equal(Core.validateNickname(" Bob ", ["Alice", "Bob"]).ok, false, "trimmed before the uniqueness check");
  assert.equal(Core.validateNickname("bob", ["Bob"]).ok, true, "names are case sensitive");
  assert.equal(Core.charLength("a😀b"), 3);
  assert.equal(Core.NICKNAME_MAX, 20);
});

test("nicknames: a colon (':' or '：') is refused like the server does - after the length, before uniqueness; look-alikes are no colons", () => {
  const COLON = "名字不能包含冒号（: 或 ：）。";
  assert.equal(Core.NICKNAME_COLON_ERROR, COLON, "the server's words, exactly (tests/frontend/test_roster_drift.py compares them with the backend)");
  for (const bad of ["A:B", "A：B", "A\uff1aB", ":", "：", "名字:", "：名字", "  Eve:2  ", "a".repeat(19) + ":", "😀:😀", "😀：".repeat(10)]) {
    assert.deepEqual(Core.validateNickname(bad, []), { ok: false, error: COLON }, JSON.stringify(bad));
  }
  // the order of the server's checks: empty, (control characters), too long - THEN the colon, then 'taken'
  assert.equal(Core.validateNickname("   ", []).error, "名字不能为空", "white space is empty, not a colon");
  assert.match(Core.validateNickname(":".repeat(21), []).error, /最多 20/, "too long is said before the colon");
  assert.equal(Core.validateNickname("a\u0000:", []).error, "名字含有非法控制字符", "a control character is said first");
  assert.equal(Core.validateNickname("A:B", ["A:B"]).error, COLON, "the colon comes before uniqueness");
  assert.equal(Core.validateNickname("Bob:", ["Alice", "Bob"]).error, COLON, "'Bob:' is not Bob: what is wrong with it is the colon");
  // exactly two characters are colons: no normalizing (the server takes a look-alike, so must the page)
  for (const fine of ["A\ua789B", "A\ufe55B", "A\u2236B", "A\u02d0B", "A\u0589B", "A;B", "A,B", "A.B", "A-B"]) {
    assert.deepEqual(Core.validateNickname(fine, []), { ok: true, value: fine }, JSON.stringify(fine));
  }
  assert.equal(Core.validateNickname("a".repeat(20), []).ok, true, "20 characters without a colon are fine");
  // the rule is about what is typed: an old name is never run through it (roster.js does not validate a name that did not change)
  assert.deepEqual(Core.validateNickname("Carol", ["Carol:2"]), { ok: true, value: "Carol" });
});

test("createWindowStack: the newest window is on top - its z-index and the Escape key; one Escape answers for one window only", () => {
  const stack = Core.createWindowStack();
  const z = { a: "-", b: "-", c: "-" };
  const setter = (name) => (v) => {
    z[name] = String(v);
  };
  const press = (key = "Escape") => ({ key, stopped: 0, stopImmediatePropagation() { this.stopped++; } });
  assert.equal(stack.top(), null);
  assert.equal(stack.opened("a", setter("a")), 8100);
  assert.equal(stack.opened("b", setter("b")), 8101);
  assert.deepEqual([stack.top(), stack.list(), z.a, z.b], ["b", ["a", "b"], "8100", "8101"]);
  // only the window on top answers, and it consumes the key press; other keys and other windows do not
  const first = press();
  assert.equal(stack.escape(first, "a"), false);
  assert.equal(first.stopped, 0, "(a window that is not on top leaves the key alone)");
  const second = press();
  assert.equal(stack.escape(second, "b"), true);
  assert.equal(second.stopped, 1, "(the listeners behind it do not see this key press)");
  const enter = press("Enter");
  assert.equal(stack.escape(enter, "b"), false);
  assert.equal(enter.stopped, 0);
  assert.equal(stack.escape(null, "b"), false);
  assert.equal(stack.escape({ key: "Escape" }, "b"), true, "(an event without stopImmediatePropagation is fine)");
  assert.equal(stack.escape(press(), "nobody"), false);
  // opened again = brought to the front: every window gets its z-index again
  stack.opened("a", setter("a"));
  assert.deepEqual([stack.top(), z.a, z.b], ["a", "8101", "8100"]);
  assert.equal(stack.opened("c", setter("c")), 8102);
  assert.deepEqual([stack.list(), z.a, z.b, z.c], [["b", "a", "c"], "8101", "8100", "8102"]);
  // closing gives the place back: no z-index of its own, the others move down to fill it
  stack.closed("a");
  assert.deepEqual([stack.list(), z.a, z.b, z.c], [["b", "c"], "", "8100", "8101"]);
  stack.closed("a"); // (twice, or a window that was never opened: nothing happens)
  stack.closed("never");
  assert.deepEqual(stack.list(), ["b", "c"]);
  assert.equal(stack.escape(press(), "b"), false);
  assert.equal(stack.escape(press(), "c"), true);
  stack.closed("c");
  stack.closed("b");
  assert.deepEqual([stack.top(), stack.list(), z.b, z.c], [null, [], "", ""]);
  assert.equal(stack.escape(press(), "b"), false, "nothing open: nobody answers");
  // a stack without z-index setters still orders the windows (a window created on its own)
  const plain = Core.createWindowStack();
  plain.opened("x");
  plain.opened("y");
  assert.deepEqual([plain.top(), plain.escape(press(), "y")], ["y", true]);
});

test("normalizeRoster tolerates partial payloads", () => {
  const r = Core.normalizeRoster({
    memory_enabled: false,
    editable: true,
    seats: [{ seat: 1, id: "N1", name: "Alice", default_name: "Alice", avatar: { v: 1 }, persona: "p", memory: "m" }, { id: "N2" }, null, { seat: 3 }],
  });
  assert.equal(r.memory_enabled, false);
  assert.equal(r.seats.length, 2);
  assert.deepEqual(r.seats[0], { seat: 1, id: "N1", name: "Alice", default_name: "Alice", avatar: { v: 1 }, persona: "p", memory: "m" });
  assert.deepEqual(r.seats[1], { seat: 0, id: "N2", name: "N2", default_name: "", avatar: null, persona: "", memory: "" });
  assert.deepEqual(Core.normalizeRoster(null), { memory_enabled: true, editable: true, seats: [] });
  assert.equal(Core.normalizeRoster({ editable: false }).editable, false);
});

// --- v0.12.1: game clock, sleeping ---------------------------------------------------------------------
test("the game clock estimate runs only while the game runs; seconds / countdown formatting", () => {
  assert.equal(Core.gameClockNow({ clock: 10, at: 1000, running: true }, 4500), 13.5);
  assert.equal(Core.gameClockNow({ clock: 10, at: 1000, running: false }, 99999), 10, "paused: the game clock stands still (D18)");
  assert.equal(Core.gameClockNow({ clock: 10, at: 5000, running: true }, 1000), 10, "never runs backwards");
  assert.equal(Core.gameClockNow(null, 1), 0);
  assert.equal(Core.formatSeconds(0), "0 秒");
  assert.equal(Core.formatSeconds(5.2), "6 秒");
  assert.equal(Core.formatSeconds(65), "1 分 05 秒");
  assert.equal(Core.formatSeconds(3700), "1 时 01 分");
  assert.equal(Core.formatSeconds(-3), "0 秒");
  assert.equal(Core.formatCountdown(0), "0:00");
  assert.equal(Core.formatCountdown(59.2), "1:00");
  assert.equal(Core.formatCountdown(90), "1:30");
  assert.equal(Core.formatCountdown(-5), "0:00");
  assert.equal(Core.sleepLabel(null, 10), "等待新消息");
  assert.equal(Core.sleepLabel(undefined, 10), "等待新消息");
  assert.equal(Core.sleepLabel(40, 10), "定时醒来：剩余 30 秒");
  assert.equal(Core.sleepLabel(10, 12), "即将醒来");
  assert.equal(Core.formatTokensShort(950), "950");
  assert.equal(Core.formatTokensShort(272900), "272.9K");
  assert.equal(Core.formatTokensShort(2930000), "2.93M");
  assert.equal(Core.formatTokensCount(272900), "272.9K Tokens");
});

// --- v0.12.1: downloads / raw uploads (the memory pack) -----------------------------------------------------
test("filenameFromDisposition prefers filename*=UTF-8'' and copes with quotes and nothing", () => {
  assert.equal(Core.filenameFromDisposition('attachment; filename="arena-memory.zip"'), "arena-memory.zip");
  assert.equal(Core.filenameFromDisposition("attachment; filename=plain.zip"), "plain.zip");
  assert.equal(Core.filenameFromDisposition("attachment; filename=\"fallback.zip\"; filename*=UTF-8''%E8%AE%B0%E5%BF%86.zip"), "记忆.zip");
  assert.equal(Core.filenameFromDisposition(""), "");
  assert.equal(Core.filenameFromDisposition(null), "");
  assert.equal(Core.filenameFromDisposition("inline"), "");
});

test("api: upload sends the raw bytes with their content type; download returns the blob + file name; errors stay readable", async () => {
  const seen = [];
  const { client } = makeClient((url, init) => {
    seen.push({ url, init });
    if (url.endsWith("memory:export")) {
      return {
        status: 200,
        ok: true,
        headers: { get: (k) => (k.toLowerCase() === "content-disposition" ? 'attachment; filename="m.zip"' : null) },
        blob: async () => ({ size: 3 }),
        text: async () => "",
      };
    }
    if (url.includes("bad")) return fakeResponse(400, { detail: "不是有效的 zip 记忆包" });
    return fakeResponse(200, { ok: true });
  });
  const bytes = new Uint8Array([80, 75, 3, 4]);
  await client.upload("api/roster/memory:import?dry_run=true", bytes, "application/zip");
  assert.equal(seen[0].init.method, "POST");
  assert.equal(seen[0].init.body, bytes, "the body is not JSON-encoded");
  assert.equal(seen[0].init.headers["Content-Type"], "application/zip");
  const dl = await client.download("api/roster/memory:export");
  assert.deepEqual(dl, { blob: { size: 3 }, filename: "m.zip" });
  assert.equal(seen[1].init.method, "GET");
  await assert.rejects(client.upload("api/bad", bytes, "application/zip"), (e) => /zip/.test(e.message) && e.status === 400);
  // JSON requests are unchanged
  await client.post("api/x", { a: 1 });
  assert.equal(seen[seen.length - 1].init.headers["Content-Type"], "application/json");
  assert.equal(seen[seen.length - 1].init.body, '{"a":1}');
});

// --- runner --------------------------------------------------------------------
(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log(`ok   - ${name}`);
    } catch (e) {
      failed++;
      console.log(`FAIL - ${name}\n${e && e.stack ? e.stack : e}`);
    }
  }
  console.log(`\n${tests.length - failed}/${tests.length} passed`);
  process.exit(failed ? 1 : 0);
})();
