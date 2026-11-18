"use strict";
// Smoke test for static/js/app.js running against a tiny fake DOM (see harness.js):
// wiring, startup, auth, SSE reconnect/resume, and the Provider / binding / session dialogs.
// The v0.12.1 UI itself (cards, detail panel, feed, channel bar, viewpoint, budget, roster, price source,
// phone layout) is in test_app_ui.js / test_v0121_ui.js.
// Run: node tests/frontend/test_app_smoke.js
const assert = require("node:assert/strict");
const H = require("./harness.js");

const { Core, F, SRC, HTML_SRC, CSS_SRC, resp, target, settle, makeEnv, makeBackend, routeFetch, boot, posts } = H;

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

function ids(src) {
  return new Set([...src.matchAll(/getElementById\("([^"]+)"\)/g)].map((m) => m[1]));
}

// ---- wiring --------------------------------------------------------------------
test("every id used by the frontend modules exists in index.html", () => {
  const htmlIds = new Set([...HTML_SRC.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
  const combined = SRC.app + SRC.providers + SRC.layout + SRC.archive + SRC.roster + SRC.budget + SRC.takeover + SRC.composer;
  // the modules also read ids through their `$ = (id) => getElementById(id)` helper
  const viaHelper = new Set(
    [SRC.app, SRC.roster, SRC.budget, SRC.providers, SRC.archive, SRC.takeover, SRC.composer].flatMap((src) =>
      [...src.matchAll(/\$\(\s*[`"]([^`"$]+)[`"]\s*\)/g)].map((m) => m[1])
    )
  );
  // ids of elements the scripts render themselves (the detail panel's name field, the confirm dialog's
  // checkbox) are written as id="..." inside a template string
  const rendered = new Set([...combined.matchAll(/\bid="([^"$]+)"/g)].map((m) => m[1]));
  // ... and the buttons of the settings wizard (archive.js), which are made by a helper: button(label, action, { id: "..." })
  for (const m of SRC.archive.matchAll(/\bid: "([^"$]+)"/g)) rendered.add(m[1]);
  const missing = [...new Set([...ids(combined), ...viaHelper])].filter((i) => !htmlIds.has(i) && !rendered.has(i));
  assert.deepEqual(missing, []);
});

test("index.html uses relative asset paths and loads the scripts in dependency order", () => {
  assert.ok(!/(?:src|href)="\/(?:static|api)/.test(HTML_SRC));
  // (v0.13: takeover.js and composer.js come after the panels they share the dialog with, and before app.js)
  const order = ["platform", "core", "avatar", "store", "view", "layout", "providers", "roster", "budget", "archive", "takeover", "composer", "app"].map((n) =>
    HTML_SRC.indexOf(`static/js/${n}.js`)
  );
  assert.ok(order.every((i) => i >= 0), "every script is referenced");
  assert.deepEqual([...order].sort((a, b) => a - b), order, "scripts load in dependency order");
});

test("frontend modules have no absolute /api or /static URLs", () => {
  for (const src of [SRC.app, SRC.providers, SRC.layout, SRC.archive, SRC.roster, SRC.budget, SRC.platform, SRC.avatar, SRC.takeover, SRC.composer]) {
    assert.ok(!/["'`]\/(api|static)\//.test(src));
  }
});

test("v0.11 / v0.12.0-only UI is gone: card sizes, force pause, tool toggle, fold buttons, streaming, execution depth, legend, \"all\" channel, dashboard column", () => {
  for (const gone of [
    "select-exec-depth",
    "execution-depth",
    "board-legend",
    "dashboard-panel",
    "btn-toggle-dashboard",
    "metrics-popover",
    "dashboard-resizer",
    "select-card-size",
    "btn-force-pause",
    "chk-show-tools",
    "btn-reset-default",
    "btn-expand-all",
    "btn-collapse-all",
    "feed-streaming-container",
    "moderator-status-pill",
  ]) {
    assert.ok(!HTML_SRC.includes(gone), `index.html still has ${gone}`);
    assert.ok(!SRC.app.includes(gone), `app.js still refers to ${gone}`);
  }
  // the three removed agent states and their legend
  for (const gone of ["🎤", "✋", "▪", "已停止", "执行深度"]) {
    assert.ok(!HTML_SRC.includes(gone), `index.html still shows ${gone}`);
  }
  assert.ok(!SRC.app.includes("flow-floor") && !SRC.app.includes("badge-waiting"));
  assert.ok(!CSS_SRC.includes(".exec-depth") && !CSS_SRC.includes(".dashboard-panel"));
  for (const gone of ["streaming_draft", "stream_chunk", "interrupt_quotas", "tool_call", "thought_text", "showToolCalls", "level1Expanded"]) {
    assert.ok(!SRC.app.includes(gone), `app.js still handles ${gone}`);
  }
  assert.ok(!SRC.layout.includes("cardSize") && !SRC.layout.includes("CardSize"));
  for (const gone of ["CARD_SIZES", "normalizeCardSize", "loadCardSize", "saveCardSize"]) {
    assert.equal(Core[gone], undefined, `core still exports ${gone}`);
  }
  // the control buttons that remain
  // (v0.13: "保存" and the single-game JSON import are gone - the game saves itself - and "工作文件夹" became "⚙ 设置")
  for (const id of ["btn-new", "btn-start", "btn-pause", "btn-resume", "btn-open-settings-modal"]) {
    assert.ok(HTML_SRC.includes(`id="${id}"`), `missing #${id}`);
  }
  // ... and the v0.12 widgets
  for (const id of [
    "select-viewpoint",
    // v0.13: the viewpoint bar [视角▾][切换][接管], the takeover bar, the reshuffle reminder, the input box
    // ("btn-shuffle-identities" was a v0.11 button that v0.12 removed: it is the reminder's button now)
    "viewpoint-bar",
    "btn-viewpoint-switch",
    "btn-takeover",
    "takeover-bar",
    "btn-release-takeover",
    "takeover-reminder",
    "btn-shuffle-identities",
    "composer",
    "composer-input",
    "composer-count",
    "btn-composer-send",
    "btn-composer-pass",
    "btn-composer-ability",
    "request-hints",
    "status-banner",
    "detail-panel",
    "participants-grid",
    "board-summary",
    "channels-list",
    "feed-records-container",
    // v0.12.1
    "btn-open-budget-modal",
    "budget-modal",
    "btn-regen-all-avatars",
    "btn-roster-menu",
    "chk-memory-enabled",
    "btn-review-all",
    "input-pricing-url",
    "btn-refresh-pricing",
    "input-model-price-cache-read",
    "input-model-price-cache-write",
    "btn-more",
    "more-menu",
    "tabbar",
    "mod-chip",
  ]) {
    assert.ok(HTML_SRC.includes(`id="${id}"`), `missing #${id}`);
  }
  assert.match(HTML_SRC, /<html[^>]* data-platform="/, "the platform attribute exists before the script runs");
});

// ---- startup / auth ----------------------------------------------------------------
test("no token needed: no modal, no header, sub-path prefix, first SSE connect carries no resume id", async () => {
  const calls = [];
  const env = makeEnv({ fetchImpl: routeFetch(calls) });
  env.run();
  await settle();
  env.flushRaf();

  assert.deepEqual(
    calls.map((c) => c.url).sort(),
    [
      "/arena/api/providers",
      "/arena/api/roster",
      "/arena/api/settings/bindings",
      "/arena/api/state",
      "/arena/static/avatar/parts.json", // the sprite library: a public static file, fetched under the sub-path prefix
    ]
  );
  assert.ok(calls.every((c) => !("X-Arena-Token" in c.init.headers)));
  assert.notEqual(env.el("token-modal").style.display, "flex");

  // moderator binding shown in the status pill; no legacy config endpoint used
  assert.match(env.el("api-status-label").textContent, /DeepSeek \/ model-x/);
  assert.ok(calls.every((c) => !c.url.endsWith("/api/config/llm")));
  assert.equal(env.el("app-version").textContent, "v0.13.0-test"); // the application version, not the session's

  // SSE is delayed 300ms and uses the prefixed URL without a token; the first stream always starts
  // with a snapshot (no last_event_id even though /api/state reported event_id=100)
  env.runNextTimer();
  assert.equal(env.esInstances.length, 1);
  assert.equal(env.esInstances[0].url, "/arena/api/events");
});

test("401: modal appears, token is stored, requests retry with header, SSE gets ?token=", async () => {
  const calls = [];
  const env = makeEnv({ fetchImpl: routeFetch(calls, { requireToken: "secret" }) });
  env.run();
  await settle();
  assert.equal(env.el("token-modal").style.display, "flex");
  // one API request waiting on the modal, no stampede (the public sprite library is not an API call)
  assert.equal(calls.filter((c) => !c.url.includes("/static/")).length, 1);

  env.el("input-api-token").value = "secret";
  env.el("token-form").dispatch("submit");
  await settle(30);
  env.flushRaf();

  assert.equal(env.el("token-modal").style.display, "none");
  assert.equal(env.storage.get("arena_api_token"), "secret");
  const withHeader = calls.filter((c) => c.init.headers["X-Arena-Token"] === "secret");
  assert.ok(withHeader.some((c) => c.url.endsWith("/api/state")));
  assert.match(env.el("api-status-label").textContent, /model-x/);

  env.runNextTimer();
  assert.ok(env.esInstances.some((e) => e.url === "/arena/api/events?token=secret"));
});

test("stored token is sent from the first request (no modal)", async () => {
  const calls = [];
  const env = makeEnv({ fetchImpl: routeFetch(calls, { requireToken: "secret" }), storedToken: "secret" });
  env.run();
  await settle();
  assert.notEqual(env.el("token-modal").style.display, "flex");
  const api = calls.filter((c) => !c.url.includes("/static/"));
  assert.ok(api.length >= 4);
  assert.ok(api.every((c) => c.init.headers["X-Arena-Token"] === "secret"));
});

test("cancelling the token modal leaves a clickable banner and a clear toast", async () => {
  const calls = [];
  const env = makeEnv({ fetchImpl: routeFetch(calls, { requireToken: "secret" }) });
  env.run();
  await settle();
  env.el("btn-cancel-token").dispatch("click");
  await settle();
  assert.equal(env.el("token-modal").style.display, "none");
  assert.equal(env.el("conn-banner").hidden, false);
  assert.ok(env.el("conn-banner").classList.contains("clickable"));
});

// ---- SSE reconnect ---------------------------------------------------------------------
test("SSE reconnect: backoff grows, single timer, banner shown then cleared on recovery", async () => {
  const calls = [];
  const env = makeEnv({ fetchImpl: routeFetch(calls) });
  env.run();
  await settle();
  env.runNextTimer(); // initial connect
  const es1 = env.esInstances[0];
  es1.onopen();
  assert.equal(env.el("conn-banner").hidden, true);

  es1.onerror();
  await settle();
  assert.equal(env.el("conn-banner").hidden, false);
  assert.match(env.el("conn-banner").textContent, /连接断开，正在重连/);
  assert.equal(env.timers.length, 1);
  const d1 = env.timers[0].ms;

  // a stale second error event must not schedule another timer
  es1.onerror && es1.onerror();
  assert.equal(env.timers.length, 1);

  env.runNextTimer();
  const es2 = env.esInstances[1];
  es2.onerror();
  await settle();
  const d2 = env.timers[0].ms;
  env.runNextTimer();
  const es3 = env.esInstances[2];
  es3.onerror();
  await settle();
  const d3 = env.timers[0].ms;
  assert.ok(d1 <= 1000 && d2 <= 2000 && d3 <= 4000);
  assert.ok(d3 > d1, `backoff should grow (${d1} -> ${d3})`);
  assert.equal(env.timers.length, 1);

  env.runNextTimer();
  const es4 = env.esInstances[3];
  es4.onopen();
  assert.equal(env.el("conn-banner").hidden, true);
  // after recovery the attempt counter restarts from the base delay
  es4.onerror();
  await settle();
  assert.ok(env.timers[0].ms <= 1000);
});

test("SSE resume: a reconnect asks for events after the last received id; replays are skipped", async () => {
  const { env } = await H.bootLive();
  const es1 = env.lastEs();
  es1.onopen();
  // the snapshot carries its own id (event 500); two live events follow
  env.sse("snapshot", { session: F.makeSession({ event_id: 500 }) }, 500);
  env.sse("phase.changed", { text: "第 1 夜" }, 501);
  env.sse("phase.changed", { text: "第 2 天" }, 502);
  assert.equal(env.app().state.sseResumeId, 502);
  assert.equal(env.app().state.lastEventId, 502);

  es1.onerror(); // the stream breaks
  await settle();
  env.runNextTimer();
  const es2 = env.lastEs();
  assert.notEqual(es2, es1);
  // a re-created EventSource would not send Last-Event-ID by itself: the id travels in the URL
  assert.equal(es2.url, "/arena/api/events?last_event_id=502");

  // the server replays what the client already has (<=502) and then continues (503...)
  env.sse("phase.changed", { text: "旧事件（重复）" }, 502);
  env.sse("phase.changed", { text: "更旧的事件" }, 499);
  assert.equal(env.app().state.session.phase_text, "第 2 天");
  env.sse("phase.changed", { text: "第 2 天 · 放逐投票" }, 503);
  assert.equal(env.app().state.session.phase_text, "第 2 天 · 放逐投票");
  assert.equal(env.app().state.sseResumeId, 503);

  // token + resume id are both carried in the query string
  env.storage.set("arena_api_token", "tok");
  es2.onerror();
  await settle();
  env.runNextTimer();
  assert.equal(env.lastEs().url, "/arena/api/events?token=tok&last_event_id=503");
});

test("a snapshot after a gap replaces the session and resets the resume point (even to a lower id)", async () => {
  const { env } = await H.bootLive();
  env.sse("snapshot", { session: F.makeSession({ event_id: 900 }) }, 900);
  assert.equal(env.app().state.lastEventId, 900);
  // the server restarted: its ids start over and a snapshot comes first
  env.sse("snapshot", { session: F.makeSession({ session_id: "after-restart", event_id: 3, status: "paused" }) }, 3);
  assert.equal(env.app().state.lastEventId, 3);
  assert.equal(env.app().state.session.session_id, "after-restart");
  env.sse("phase.changed", { text: "继续" }, 4);
  assert.equal(env.app().state.session.phase_text, "继续");
});

test("archive.progress is not part of the game: no reload, no repaint of the session, and the stream id still advances", async () => {
  const { env } = await H.bootLive();
  env.sse("snapshot", { session: F.makeSession() }, 10);
  const before = JSON.stringify(env.app().state.session);
  env.sse("archive.progress", { job_id: "j1", kind: "export", phase: "pack", done: 5, total: 10, status: "running" }, 11);
  assert.equal(env.reloads.length, 0);
  assert.equal(env.app().state.lastEventId, 11, "a reconnect resumes after it");
  assert.equal(JSON.stringify(env.app().state.session), before);
  // (what the settings dialog does with it is tested in test_archive_ui.js)
});

test("SSE error events are toasted; broken payloads never crash the stream", async () => {
  const { env } = await H.bootLive();
  env.sse("snapshot", { session: F.makeSession() }, 10);
  env.sse("error", { message: "模拟后端错误" }, 11);
  assert.ok(env.toasts.some((t) => /模拟后端错误/.test(t.textContent)));
  const es = env.lastEs();
  assert.doesNotThrow(() => es.onmessage({ data: "{not json" }));
  assert.doesNotThrow(() => es.onmessage({ data: "null" }));
  assert.doesNotThrow(() => es.onmessage({ data: JSON.stringify({ id: 12, type: "no.such.event", data: { x: 1 } }) }));
  env.flushRaf();
  assert.equal(env.app().state.session.session_id, "s-test");
});

test("SSE events are coalesced into one render per frame", async () => {
  const { env } = await H.bootLive();
  env.sse("snapshot", { session: F.makeSession() }, 10);
  let renders = 0;
  Object.defineProperty(env.el("app-title"), "textContent", {
    set() {
      renders++;
    },
    get() {
      return "";
    },
  });
  for (let i = 0; i < 20; i++) {
    env.sse("agent.status", { id: "p01", state: i % 2 ? "thinking" : "acting" }, 11 + i, { flush: false });
  }
  env.sse("phase.changed", { text: "第 2 天" }, 40, { flush: false });
  assert.equal(renders, 0); // nothing rendered synchronously
  env.flushRaf();
  assert.equal(renders, 1); // one frame, one top-bar paint
});

test("an HTTP session that is older than the stream does not roll the store back", async () => {
  const { env, backend } = await H.bootLive();
  env.sse("snapshot", { session: F.makeSession({ event_id: 200, status: "running" }) }, 200);
  env.sse("phase.changed", { text: "第 3 天" }, 205);
  // the control response was produced at event 201: older than what the stream already delivered
  backend.controlSession = F.makeSession({ event_id: 201, status: "paused", phase_text: "第 1 天" });
  env.el("btn-pause").dispatch("click");
  await settle();
  assert.equal(env.app().state.session.status, "running"); // SSE is ahead: the stale body is ignored
  assert.equal(env.app().state.session.phase_text, "第 3 天");
  // a response at least as new is adopted
  backend.controlSession = F.makeSession({ event_id: 210, status: "paused", phase_text: "第 3 天 · 暂停" });
  env.el("btn-pause").dispatch("click");
  await settle();
  env.flushRaf();
  assert.equal(env.app().state.session.status, "paused");
  assert.equal(env.app().state.lastEventId, 210);
});

// ---- non-JSON ---------------------------------------------------------------------
test("non-JSON 500 on init does not crash and toasts a clear message", async () => {
  const fetchImpl = async () => resp(500, "<html>boom</html>", "text/html");
  const env = makeEnv({ fetchImpl });
  env.run();
  await settle();
  const msgs = env.toasts.map((t) => t.textContent).join("|");
  assert.match(msgs, /500/);
  assert.ok(!/SyntaxError|Unexpected token/.test(msgs));
});

// ---- v0.10 ------------------------------------------------------------------------------------
test("startup loads providers + bindings; the moderator binding is shown in the top bar", async () => {
  const { env, backend } = await boot();
  assert.ok(backend.calls.some((c) => c.url.endsWith("/api/providers")));
  assert.ok(backend.calls.some((c) => c.url.endsWith("/api/settings/bindings")));
  assert.ok(!backend.calls.some((c) => c.url.endsWith("/api/config/llm")));
  assert.match(env.el("api-status-label").textContent, /DeepSeek \/ model-x/);
  // the tiles themselves carry no model line any more (it lives in the metrics popover / bindings dialog)
  assert.ok(!env.el("participants-grid").innerHTML.includes("card-binding"));
});

test("provider modal: protocol options are the three localized labels; read-only provider is locked", async () => {
  const { env } = await boot();
  env.el("btn-open-provider-modal").dispatch("click");
  await settle();
  assert.equal(env.el("provider-modal").style.display, "flex");
  const opts = env.el("select-provider-protocol").innerHTML;
  assert.match(opts, /OpenAI 对话补全（Chat Completions）/);
  assert.match(opts, /OpenAI 响应接口（Responses）/);
  assert.match(opts, /Anthropic 消息接口（Messages）/);
  const list = env.el("provider-list").innerHTML;
  assert.match(list, /DeepSeek/);
  assert.match(list, /Claude/);
  assert.match(list, /只读/);
  assert.ok(!list.includes("sk-"));
  // select the read-only one
  env.el("provider-list").dispatch("click", { target: target({ ".provider-item": { "data-id": "env" } }) });
  assert.equal(env.el("input-provider-name").disabled, true);
  assert.equal(env.el("btn-save-provider").disabled, true);
  assert.equal(env.el("btn-delete-provider").disabled, true);
  assert.match(env.el("hint-provider-key").textContent, /只读/);
});

test("create provider: validates locally, sends the exact body, never echoes the key", async () => {
  const { env, backend } = await boot();
  env.el("btn-open-provider-modal").dispatch("click");
  await settle();
  env.el("btn-new-provider").dispatch("click");
  env.el("input-provider-name").value = "Mine";
  env.el("select-provider-protocol").value = "openai_responses";
  env.el("input-provider-base-url").value = "https://r.example.com";
  env.el("input-provider-key").value = "";
  env.el("provider-form").dispatch("submit");
  await settle();
  assert.equal(env.el("provider-form-error").hidden, false);
  assert.match(env.el("provider-form-error").textContent, /API Key 为必填项/);
  assert.equal(posts(backend, "POST", "/api/providers").length, 0);

  env.el("input-provider-key").value = "sk-secret-123";
  env.el("provider-form").dispatch("submit");
  await settle();
  const req = posts(backend, "POST", "/api/providers")[0];
  assert.deepEqual(req.body, {
    name: "Mine",
    protocol: "openai_responses",
    base_url: "https://r.example.com",
    api_key: "sk-secret-123",
    auto_append_path: true,
  });
  assert.equal(env.el("input-provider-key").value, ""); // key cleared after save
  assert.ok(!env.el("provider-list").innerHTML.includes("sk-secret"));
  assert.match(env.el("provider-list").innerHTML, /Mine/);
});

test("edit provider: base_url change without key is blocked; rename sends only the name", async () => {
  const { env, backend } = await boot();
  env.el("btn-open-provider-modal").dispatch("click");
  await settle();
  env.el("input-provider-base-url").value = "https://other.example.com";
  env.el("provider-form").dispatch("submit");
  await settle();
  assert.match(env.el("provider-form-error").textContent, /必须重新输入 API Key/);
  assert.equal(backend.calls.filter((c) => c.method === "PATCH").length, 0);

  env.el("input-provider-base-url").value = "https://api.example.com";
  env.el("input-provider-name").value = "DS renamed";
  env.el("provider-form").dispatch("submit");
  await settle();
  const patch = backend.calls.find((c) => c.method === "PATCH");
  assert.deepEqual(patch.body, { name: "DS renamed" });
});

test("models: discover -> radio single-select -> prices REQUIRED -> add exactly one", async () => {
  const { env, backend } = await boot();
  env.el("btn-open-provider-modal").dispatch("click");
  await settle();
  env.el("btn-discover-models").dispatch("click");
  await settle();
  const list = env.el("discover-list").innerHTML;
  assert.equal((list.match(/type="radio"/g) || []).length, 3);
  assert.ok(!/type="checkbox"/.test(list));
  // Model discovery stays single-select: no bulk "add all" control in its list.
  assert.ok(!/全选|批量/.test(list));
  assert.match(list, /已添加/); // model-x already exists

  // pick a new model without a price-source match: prices must be typed, none are prefilled
  env.el("discover-list").dispatch("change", { target: { value: "m-new" } });
  assert.equal(env.el("input-model-id").value, "m-new");
  assert.equal(env.el("input-model-price-in").value, "");
  assert.equal(env.el("input-model-price-cache-read").value, "");
  assert.equal(env.el("input-model-price-cache-write").value, "");
  assert.equal(env.el("input-model-price-out").value, "");
  assert.match(env.el("model-price-hint").textContent, /没有匹配到/);
  assert.equal(env.el("btn-submit-model").textContent, "添加模型");

  env.el("model-form").dispatch("submit");
  await settle();
  assert.match(env.el("model-form-error").textContent, /输入价格为必填项/);
  assert.match(env.el("model-form-error").textContent, /输出价格为必填项/);
  env.el("input-model-price-in").value = "1";
  env.el("model-form").dispatch("submit");
  await settle();
  assert.match(env.el("model-form-error").textContent, /输出价格为必填项/);
  assert.equal(posts(backend, "POST", "/models").length, 0);

  env.el("input-model-price-out").value = "abc";
  env.el("model-form").dispatch("submit");
  await settle();
  assert.match(env.el("model-form-error").textContent, /输出价格必须是非负数字/);
  assert.equal(posts(backend, "POST", "/models").length, 0);

  env.el("input-model-price-out").value = "4.5";
  env.el("model-form").dispatch("submit");
  await settle();
  const req = posts(backend, "POST", "/models");
  assert.equal(req.length, 1);
  // the two cache prices were left empty: null = bill at the input price
  assert.deepEqual(req[0].body, {
    model_id: "m-new",
    input_price_per_m: 1,
    cache_read_price_per_m: null,
    cache_write_price_per_m: null,
    output_price_per_m: 4.5,
  });
  // form is reset so the next model needs fresh prices
  assert.equal(env.el("input-model-id").value, "");
  assert.equal(env.el("input-model-price-in").value, "");
  assert.match(env.el("provider-model-list").innerHTML, /m-new/);
});

test("models: picking an existing model prefills prices and switches to edit (PATCH)", async () => {
  const { env, backend } = await boot();
  env.el("btn-open-provider-modal").dispatch("click");
  await settle();
  env.el("btn-discover-models").dispatch("click");
  await settle();
  env.el("discover-list").dispatch("change", { target: { value: "model-x" } });
  assert.equal(env.el("btn-submit-model").textContent, "保存价格");
  assert.equal(env.el("input-model-price-in").value, "0.5");
  assert.equal(env.el("input-model-price-cache-read").value, "0.1");
  assert.equal(env.el("input-model-price-cache-write").value, "");
  env.el("input-model-price-in").value = "0.6";
  env.el("model-form").dispatch("submit");
  await settle();
  assert.equal(posts(backend, "POST", "/models").length, 0);
  const patch = posts(backend, "PATCH", "/models")[0];
  assert.deepEqual(patch.body, {
    model_id: "model-x",
    input_price_per_m: 0.6,
    cache_read_price_per_m: 0.1,
    cache_write_price_per_m: null,
    output_price_per_m: 2,
    context_window_tokens: 128000,
  });
});

test("models: remove sends DELETE with model id in the body (slash-safe); 409 is toasted", async () => {
  const { env, backend } = await boot();
  backend.providers[0].models.push({ id: "a/b", input_price_per_m: 1, output_price_per_m: 1, context_window_tokens: 5000 });
  env.el("btn-open-provider-modal").dispatch("click");
  await settle();
  env.el("provider-model-list").dispatch("click", {
    target: target({ ".model-item": { "data-model-id": "a/b" }, "[data-action]": { "data-action": "remove-model" } }),
  });
  await settle();
  const del = backend.calls.find((c) => c.method === "DELETE" && c.url.endsWith("/models"));
  assert.deepEqual(del.body, { model_id: "a/b" });
  assert.ok(!del.url.includes("a/b"));

  backend.failures["DELETE /api/providers/p1/models"] = [409, "模型仍被主持人绑定，不能删除"];
  env.el("provider-model-list").dispatch("click", {
    target: target({ ".model-item": { "data-model-id": "model-x" }, "[data-action]": { "data-action": "remove-model" } }),
  });
  await settle();
  assert.match(env.toasts[env.toasts.length - 1].textContent, /仍被主持人绑定/);
});

test("bindings: moderator + player rows, provider change resets model, save PUTs payload", async () => {
  const session = F.makeSession({
    status: "paused",
    participants: F.makeParticipants().slice(0, 2),
  });
  const { env, backend } = await boot({ session });
  env.el("btn-open-bindings-modal").dispatch("click");
  await settle();
  assert.equal(env.el("bindings-modal").style.display, "flex");
  const html = env.el("bindings-table").innerHTML;
  assert.equal((html.match(/class="binding-row/g) || []).length, 3);
  assert.match(html, /xhigh（极深）/);
  assert.ok(!/ disabled/.test(html));
  // v0.12.1: no execution depth any more; the hint talks about the reasoning depth per role only
  assert.match(env.el("bindings-lock-hint").textContent, /思考深度/);
  assert.ok(!/执行深度/.test(env.el("bindings-lock-hint").textContent));

  const change = (actor, field, value) =>
    env.el("bindings-table").dispatch("change", {
      target: target({ ".binding-row": { "data-actor": actor } }, { getAttribute: (k) => (k === "data-field" ? field : null), value }),
    });
  change("N1", "provider_id", "p2");
  change("N1", "model_id", "claude-y");
  change("N1", "reasoning_depth", "xhigh");
  env.el("btn-save-bindings").dispatch("click");
  await settle();
  const put = backend.calls.find((c) => c.method === "PUT");
  assert.deepEqual(put.body.bindings.N1, {
    provider_id: "p2",
    model_id: "claude-y",
    reasoning_depth: "xhigh",
    context_window_tokens: 200000,
  });
  assert.ok(put.body.bindings.moderator); // moderator binding kept
  assert.equal(put.body.bindings.N2, undefined); // unbound players are not sent
  assert.equal(env.el("bindings-modal").style.display, "none");
});

test("bindings: partially filled row is rejected locally; apply-all copies the moderator", async () => {
  const session = F.makeSession({ status: "idle", participants: F.makeParticipants().slice(0, 1) });
  const { env, backend } = await boot({ session });
  env.el("btn-open-bindings-modal").dispatch("click");
  await settle();
  env.el("bindings-table").dispatch("change", {
    target: target({ ".binding-row": { "data-actor": "N1" } }, { getAttribute: (k) => (k === "data-field" ? "provider_id" : null), value: "p1" }),
  });
  env.el("btn-save-bindings").dispatch("click");
  await settle();
  assert.equal(backend.calls.filter((c) => c.method === "PUT").length, 0);
  assert.match(env.toasts[env.toasts.length - 1].textContent, /请同时选择 Provider 和模型/);

  env.el("btn-bindings-apply-all").dispatch("click");
  env.el("btn-save-bindings").dispatch("click");
  await settle();
  const put = backend.calls.find((c) => c.method === "PUT");
  assert.equal(put.body.bindings.N1.model_id, "model-x");
});

test("bindings are LOCKED while running: controls disabled, save does not send, and unlock live over SSE", async () => {
  const session = F.makeSession({ status: "running", participants: F.makeParticipants().slice(0, 1) });
  const { env, backend } = await H.bootLive({ session });
  env.el("btn-open-bindings-modal").dispatch("click");
  await settle();
  const html = env.el("bindings-table").innerHTML;
  assert.equal((html.match(/<select[^>]* disabled/g) || []).length, 6); // 2 rows x 3 selects
  assert.equal(env.el("btn-save-bindings").disabled, true);
  assert.equal(env.el("btn-bindings-apply-all").disabled, true);
  assert.match(env.el("bindings-lock-hint").textContent, /锁定/);
  // even if a click sneaks through, nothing is sent
  env.el("btn-save-bindings").dispatch("click");
  await settle();
  assert.equal(backend.calls.filter((c) => c.method === "PUT").length, 0);

  // provider form: address/protocol and delete are locked, key rotation still allowed
  env.el("btn-open-provider-modal").dispatch("click");
  await settle();
  assert.equal(env.el("select-provider-protocol").disabled, true);
  assert.equal(env.el("input-provider-base-url").disabled, true);
  assert.equal(env.el("btn-delete-provider").disabled, true);
  assert.equal(env.el("input-provider-key").disabled, false);

  // when the game pauses the lock is released live: session.updated is merged and providers.js is told
  env.sse("session.updated", { status: "paused", pause_reason: "user" }, 900);
  assert.equal(env.el("btn-delete-provider").disabled, false);
  assert.equal(env.el("select-provider-protocol").disabled, false);
});

test("backend 409 on saving bindings is shown as a toast", async () => {
  const session = F.makeSession({ status: "paused", participants: [] });
  const { env, backend } = await boot({ session });
  backend.failures["PUT /api/settings/bindings"] = [409, "对局运行中，不能修改模型绑定"];
  env.el("btn-open-bindings-modal").dispatch("click");
  await settle();
  env.el("btn-save-bindings").dispatch("click");
  await settle();
  assert.match(env.toasts[env.toasts.length - 1].textContent, /对局运行中，不能修改模型绑定/);
  assert.equal(env.el("bindings-modal").style.display, "flex"); // stays open for correction
});

test("restored session shows a notice, never auto-starts, and can be dismissed", async () => {
  const session = F.makeSession({
    status: "paused",
    restored_from_save: true,
    restore_notice: "已恢复上次对局（已暂停）",
    binding_issues: [{ actor_id: "N1", message: "Provider 已被删除" }],
  });
  const { env, backend } = await H.bootLive({ session });
  assert.equal(env.el("restore-notice").hidden, false);
  assert.match(env.el("restore-notice-text").textContent, /已恢复上次对局（已暂停）/);
  assert.match(env.el("restore-notice-text").textContent, /Alice（Provider 已被删除）/, "the notice names the nickname, not the id");
  assert.equal(backend.calls.filter((c) => c.url.includes("/control/")).length, 0);
  env.el("btn-dismiss-notice").dispatch("click");
  assert.equal(env.el("restore-notice").hidden, true);
  // a fresh, non-restored snapshot keeps it hidden (the server clears the notice together with the flag when the game starts)
  env.sse("snapshot", { session: { ...session, restored_from_save: false, restore_notice: "", binding_issues: [] } }, 50);
  assert.equal(env.el("restore-notice").hidden, true);
});

test("a notice the server sends WITHOUT a restore (old save, unreadable save, data directory failure) is shown too", async () => {
  // v0.12.1 starts a blank game and says why when the last game was saved by an older version (the save is kept)
  const notice = "上次的对局来自旧版本引擎（v0.12.0 及更早），v0.12.1 不兼容，已保留在存档列表中、未被改动；已为你新建了一局。";
  const session = F.makeSession({ status: "idle", restored_from_save: false, restore_notice: notice });
  const { env, backend } = await H.bootLive({ session });
  assert.equal(env.el("restore-notice").hidden, false, "the explanation is not hidden just because nothing was restored");
  assert.equal(env.el("restore-notice-text").textContent, notice);
  assert.equal(backend.calls.filter((c) => c.url.includes("/control/")).length, 0, "and nothing is started on the user's behalf");
  env.el("btn-dismiss-notice").dispatch("click");
  assert.equal(env.el("restore-notice").hidden, true);
  // starting / creating a game clears it on the server: the next snapshot has no notice and the bar stays hidden
  env.sse("snapshot", { session: { ...session, restore_notice: "" } }, 50);
  assert.equal(env.el("restore-notice").hidden, true);
  // an unreadable last save and a data directory that cannot be used say so in the same way
  const broken = F.makeSession({ status: "idle", restore_notice: "数据目录不可用：已有数据未被改动或删除；请检查 data 文件夹的权限后重启。" });
  const second = await H.bootLive({ session: broken });
  assert.match(second.env.el("restore-notice-text").textContent, /数据目录不可用/);
  assert.equal(second.env.el("restore-notice").hidden, false);
  // nothing to say, nothing shown
  const quiet = await H.bootLive({ session: F.makeSession({ status: "idle" }) });
  assert.equal(quiet.env.el("restore-notice").hidden, true);
});

test("/api/state top-level restored=true also raises the notice", async () => {
  const backend = makeBackend({ session: F.makeSession({ status: "paused" }) });
  const orig = backend.fetch;
  backend.fetch = async (url, init) => {
    if (url.endsWith("/api/state")) return resp(200, { restored: true, session: backend.session });
    return orig(url, init);
  };
  const env = makeEnv({ fetchImpl: backend.fetch });
  env.run();
  await settle();
  env.flushRaf();
  assert.equal(env.el("restore-notice").hidden, false);
});

test("the old sessions window is gone: the history of games (history.js, replay.js) replaced it", () => {
  for (const gone of ["btn-open-sessions-modal", "sessions-modal", "session-list", "btn-refresh-sessions", "btn-close-sessions-modal"]) {
    assert.ok(!HTML_SRC.includes(`id="${gone}"`), `index.html still has #${gone}`);
    assert.ok(!SRC.providers.includes(gone), `providers.js still refers to ${gone}`);
    assert.ok(!SRC.app.includes(gone), `app.js still refers to ${gone}`);
  }
  assert.ok(!SRC.providers.includes("api/sessions") && !SRC.app.includes("api/sessions"), "nothing asks for /api/sessions any more");
  for (const id of ["btn-open-history-modal", "history-modal", "history-list", "btn-refresh-history", "replay-bar", "btn-replay-play", "btn-replay-restart", "select-replay-speed", "chk-replay-skip", "replay-seek", "btn-exit-replay"]) {
    assert.ok(HTML_SRC.includes(`id="${id}"`), `missing #${id}`);
  }
  // (the behaviour is tested in test_history_ui.js)
});

test("the execution depth is gone (backend removed it): no select, no endpoint, no label; the reasoning depth stays per role", () => {
  assert.ok(!/执行深度/.test(HTML_SRC));
  assert.ok(!/exec-depth|execution_depth|execution-depth/.test(HTML_SRC + SRC.app + SRC.providers));
  assert.match(HTML_SRC, /思考深度/); // the binding table still has it
});

test("usage cost comes from the server total (price snapshots) and stays live with agent.metrics", async () => {
  const session = F.makeSession({ usage_stats: { total_tokens: 1500, prompt_tokens: 1000, completion_tokens: 500, total_cost_usd: 0.1234, by_actor: {}, by_model: {} } });
  const { env } = await H.bootLive({ session });
  assert.equal(env.el("usage-cost-text").textContent, "$0.1234");
  assert.match(env.el("usage-tokens-text").textContent, /1\.5K/);
  env.sse("agent.metrics", { id: "N1", calls: 1, prompt_tokens: 1000, cache_read_tokens: 0, cache_write_tokens: 0, completion_tokens: 1000, cost_usd: 0.0766, tool_calls: 2, thinking_rounds: 1, rejections: 0 }, 160);
  assert.equal(env.el("usage-cost-text").textContent, "$0.2000");
  assert.match(env.el("usage-tokens-text").textContent, /3\.5K/);
});

test("a session that still carries execution_depth (an older save) does not bring the select back", async () => {
  const { env, backend } = await H.bootLive({ session: F.makeSession({ execution_depth: "high" }) });
  env.sse("session.updated", { execution_depth: "xhigh" }, 170);
  assert.ok(!backend.calls.some((c) => c.url.includes("execution-depth")));
  assert.ok(!env.el("participants-grid").innerHTML.includes("执行深度"));
});

// ---- v0.11 layout wiring; v0.13 settings entry ------------------------------------------------------
test("app: the board collapses and expands from its toggle; the workspace folder button is gone", async () => {
  const { env, backend } = await boot();
  assert.ok(!backend.calls.some((c) => c.url.includes("/api/workspaces")), "nothing asks for workspaces any more");
  assert.ok(!env.el("main-container").classList.contains("board-collapsed"));
  env.el("btn-toggle-board").dispatch("click");
  assert.ok(env.el("main-container").classList.contains("board-collapsed"));
  env.el("btn-toggle-board").dispatch("click");
  assert.ok(!env.el("main-container").classList.contains("board-collapsed"));
});

test("v0.13: the folder / save / import-JSON / memory-pack entries are gone from the page and the scripts", () => {
  for (const gone of [
    "btn-open-workspace-modal",
    "workspace-modal",
    "workspace-name-label",
    "btn-save",
    "btn-import-session",
    "file-import-session",
    "btn-roster-export",
    "btn-roster-import",
    "file-import-memory",
  ]) {
    assert.ok(!HTML_SRC.includes(`id="${gone}"`), `index.html still has #${gone}`);
    for (const name of ["app", "roster", "providers", "layout", "budget"]) {
      assert.ok(!SRC[name].includes(`"${gone}"`), `${name}.js still refers to #${gone}`);
    }
  }
  assert.equal(SRC.workspace, undefined, "static/js/workspace.js is deleted");
  for (const gone of ["api/workspaces", "api/session/save", "api/session/import", "memory:export", "memory:import"]) {
    for (const name of ["app", "roster", "providers", "archive"]) assert.ok(!SRC[name].includes(gone), `${name}.js still calls ${gone}`);
  }
  for (const gone of ["ArenaWorkspaceUI", "workspace.changed", "normalizeWorkspaces", "validateWorkspacePath"]) {
    for (const name of ["app", "core", "store"]) assert.ok(!SRC[name].includes(gone), `${name}.js still has ${gone}`);
  }
  assert.ok(!CSS_SRC.includes(".btn-workspace") && !CSS_SRC.includes(".workspace-modal-body"), "the folder dialog's styles are gone");
});

test("v0.13: the settings button opens the dialog (it has only export and import)", async () => {
  const { env } = await boot();
  assert.ok(HTML_SRC.includes('id="btn-open-settings-modal"') && HTML_SRC.includes('id="settings-modal"'));
  env.el("btn-open-settings-modal").dispatch("click");
  await settle();
  assert.equal(env.el("settings-modal").style.display, "flex");
  assert.match(env.el("settings-body").innerHTML, /导出存档/);
  assert.match(env.el("settings-body").innerHTML, /导入存档/);
});

test("the shared dialog (takeover, history, import ... all ask through it): Esc answers it 'no'; a new dialog answers the old one 'no'", async () => {
  const { env } = await boot();
  const ask = (o) => env.app().roster().openDialog({ title: "问", bodyHtml: "<p>?</p>", ...o });

  // Esc = cancel, and the dialog closes
  const first = ask({ title: "一" });
  assert.equal(env.el("dialog-modal").style.display, "flex");
  assert.equal(env.el("dialog-title").textContent, "一");
  env.document.dispatchEvent("keydown", { key: "Escape" });
  assert.equal((await first).ok, false);
  assert.equal(env.el("dialog-modal").style.display, "none");

  // nothing keeps listening after it closed (an Esc with no dialog open is a plain Esc again)
  env.document.dispatchEvent("keydown", { key: "Escape" });
  assert.equal(env.el("dialog-modal").style.display, "none");

  // another key does nothing; the OK button answers "yes" and collects the values
  const second = ask({ collect: () => ({ picked: 3 }) });
  env.document.dispatchEvent("keydown", { key: "Enter" });
  assert.equal(env.el("dialog-modal").style.display, "flex", "only Esc closes it");
  env.el("btn-dialog-ok").dispatch("click");
  const answered = await second; // (objects made inside the page's realm: compare their fields, not their prototype)
  assert.equal(answered.ok, true);
  assert.equal(answered.values.picked, 3);
  env.document.dispatchEvent("keydown", { key: "Escape" }); // (the closed dialog no longer listens)

  // a dialog that opens while another one is open answers the old one "no" - its promise never hangs
  const older = ask({ title: "旧" });
  const newer = ask({ title: "新" });
  assert.equal((await older).ok, false);
  assert.equal(env.el("dialog-title").textContent, "新", "the new one is on screen");
  assert.equal(env.el("dialog-modal").style.display, "flex");
  env.el("btn-close-dialog").dispatch("click"); // the ✕ button = cancel
  assert.equal((await newer).ok, false);
  assert.equal(env.el("dialog-modal").style.display, "none");
});

test("Esc closes only the dialog when a dialog is open (the roster menu / detail panel behind it stay)", async () => {
  const { env } = await boot();
  env.el("btn-roster-menu").dispatch("click"); // the roster menu opens
  assert.equal(env.el("roster-menu").hidden, false);
  const pending = env.app().roster().openDialog({ title: "问", bodyHtml: "<p>?</p>" });
  env.document.dispatchEvent("keydown", { key: "Escape" });
  assert.equal((await pending).ok, false);
  assert.equal(env.el("roster-menu").hidden, false, "the first Esc went to the dialog");
  env.document.dispatchEvent("keydown", { key: "Escape" });
  assert.equal(env.el("roster-menu").hidden, true, "the next one closes the menu as before");
});

H.runTests(tests);
