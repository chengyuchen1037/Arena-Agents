"use strict";
// Smoke test for static/js/app.js running against a tiny fake DOM (see harness.js):
// wiring, startup, auth, SSE reconnect/resume, and the Provider / binding / session dialogs.
// The v0.12 UI itself (tiles, popover, feed, channel bar, viewpoint) is in test_app_ui.js.
// Run: node tests/frontend/test_app_smoke.js
const assert = require("node:assert/strict");
const H = require("./harness.js");

const { Core, F, SRC, HTML_SRC, resp, target, settle, makeEnv, makeBackend, routeFetch, boot, posts } = H;

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

function ids(src) {
  return new Set([...src.matchAll(/getElementById\("([^"]+)"\)/g)].map((m) => m[1]));
}

// ---- wiring --------------------------------------------------------------------
test("every id used by the frontend modules exists in index.html", () => {
  const htmlIds = new Set([...HTML_SRC.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
  const combined = SRC.app + SRC.providers + SRC.layout + SRC.workspace;
  // app.js also reads ids through its `$ = (id) => getElementById(id)` helper
  const viaHelper = new Set([...SRC.app.matchAll(/\$\("([^"]+)"\)/g)].map((m) => m[1]));
  const missing = [...new Set([...ids(combined), ...viaHelper])].filter((i) => !htmlIds.has(i));
  assert.deepEqual(missing, []);
});

test("index.html uses relative asset paths and loads the scripts in dependency order", () => {
  assert.ok(!/(?:src|href)="\/(?:static|api)/.test(HTML_SRC));
  const order = ["core", "avatar", "store", "view", "layout", "providers", "workspace", "app"].map((n) =>
    HTML_SRC.indexOf(`static/js/${n}.js`)
  );
  assert.ok(order.every((i) => i >= 0), "every script is referenced");
  assert.deepEqual([...order].sort((a, b) => a - b), order, "scripts load in dependency order");
});

test("frontend modules have no absolute /api or /static URLs", () => {
  for (const src of [SRC.app, SRC.providers, SRC.layout, SRC.workspace]) {
    assert.ok(!/["'`]\/(api|static)\//.test(src));
  }
});

test("v0.11-only UI is gone: card sizes, force pause, tool toggle, fold buttons, streaming, engine stream", () => {
  for (const gone of [
    "select-card-size",
    "btn-force-pause",
    "chk-show-tools",
    "btn-reset-default",
    "btn-expand-all",
    "btn-collapse-all",
    "feed-streaming-container",
    "btn-shuffle-identities",
    "moderator-status-pill",
  ]) {
    assert.ok(!HTML_SRC.includes(gone), `index.html still has ${gone}`);
    assert.ok(!SRC.app.includes(gone), `app.js still refers to ${gone}`);
  }
  for (const gone of ["streaming_draft", "stream_chunk", "interrupt_quotas", "tool_call", "thought_text", "showToolCalls", "level1Expanded"]) {
    assert.ok(!SRC.app.includes(gone), `app.js still handles ${gone}`);
  }
  assert.ok(!SRC.layout.includes("cardSize") && !SRC.layout.includes("CardSize"));
  for (const gone of ["CARD_SIZES", "normalizeCardSize", "loadCardSize", "saveCardSize"]) {
    assert.equal(Core[gone], undefined, `core still exports ${gone}`);
  }
  // the control buttons that remain
  for (const id of ["btn-new", "btn-save", "btn-start", "btn-pause", "btn-resume", "btn-import-session"]) {
    assert.ok(HTML_SRC.includes(`id="${id}"`), `missing #${id}`);
  }
  // ... and the v0.12 widgets
  for (const id of ["select-viewpoint", "request-hints", "status-banner", "metrics-popover", "participants-grid", "channels-list", "feed-records-container"]) {
    assert.ok(HTML_SRC.includes(`id="${id}"`), `missing #${id}`);
  }
});

// ---- startup / auth ----------------------------------------------------------------
test("no token needed: no modal, no header, sub-path prefix, first SSE connect carries no resume id", async () => {
  const calls = [];
  const env = makeEnv({ fetchImpl: routeFetch(calls) });
  env.run();
  await settle();
  env.flushRaf();

  assert.deepEqual(
    calls.map((c) => c.url),
    ["/arena/api/providers", "/arena/api/settings/bindings", "/arena/api/state", "/arena/api/workspaces"]
  );
  assert.ok(calls.every((c) => !("X-Arena-Token" in c.init.headers)));
  assert.notEqual(env.el("token-modal").style.display, "flex");

  // moderator binding shown in the status pill; no legacy config endpoint used
  assert.match(env.el("api-status-label").textContent, /DeepSeek \/ model-x/);
  assert.ok(calls.every((c) => !c.url.endsWith("/api/config/llm")));
  assert.equal(env.el("app-version").textContent, "v0.12-test"); // the application version, not the session's

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
  assert.equal(calls.length, 1); // one request waiting on the modal, no stampede

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
  assert.ok(calls.every((c) => c.init.headers["X-Arena-Token"] === "secret"));
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

test("workspace.changed reloads the page (the whole server context changed)", async () => {
  const { env } = await H.bootLive();
  env.sse("snapshot", { session: F.makeSession() }, 10);
  assert.equal(env.reloads.length, 0);
  env.sse("workspace.changed", {}, 11);
  assert.equal(env.reloads.length, 1);
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

// ---- import / non-JSON ---------------------------------------------------------------------
test("import adopts the imported game (new session id) and toasts", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    if (url.endsWith("/api/session/import")) {
      return resp(200, { session: F.makeSession({ session_id: "s2", status: "paused" }) });
    }
    return routeFetch([])(url, init);
  };
  const env = makeEnv({ fetchImpl });
  env.run();
  await settle();
  env.flushRaf();
  const file = { name: "x.json", text: async () => JSON.stringify({ session: {} }) };
  env.el("file-import-session").dispatch("change", { target: { files: [file], value: "" } });
  await settle();
  env.flushRaf();
  const imp = calls.find((c) => c.url.endsWith("/api/session/import"));
  assert.ok(imp, "import request sent");
  assert.ok(env.toasts.some((t) => /已成功导入/.test(t.textContent)));
  assert.equal(env.app().state.session.session_id, "s2");
  assert.equal(env.app().state.selectedChannel, "all");
});

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

  // pick a new model: prices must be typed, none are prefilled
  env.el("discover-list").dispatch("change", { target: { value: "m-new" } });
  assert.equal(env.el("input-model-id").value, "m-new");
  assert.equal(env.el("input-model-price-in").value, "");
  assert.equal(env.el("input-model-price-out").value, "");
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
  assert.deepEqual(req[0].body, { model_id: "m-new", input_price_per_m: 1, output_price_per_m: 4.5 });
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
  env.el("input-model-price-in").value = "0.6";
  env.el("model-form").dispatch("submit");
  await settle();
  assert.equal(posts(backend, "POST", "/models").length, 0);
  const patch = posts(backend, "PATCH", "/models")[0];
  assert.deepEqual(patch.body, {
    model_id: "model-x",
    input_price_per_m: 0.6,
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
  assert.match(env.el("bindings-lock-hint").textContent, /执行深度/);

  const change = (actor, field, value) =>
    env.el("bindings-table").dispatch("change", {
      target: target({ ".binding-row": { "data-actor": actor } }, { getAttribute: (k) => (k === "data-field" ? field : null), value }),
    });
  change("p01", "provider_id", "p2");
  change("p01", "model_id", "claude-y");
  change("p01", "reasoning_depth", "xhigh");
  env.el("btn-save-bindings").dispatch("click");
  await settle();
  const put = backend.calls.find((c) => c.method === "PUT");
  assert.deepEqual(put.body.bindings.p01, {
    provider_id: "p2",
    model_id: "claude-y",
    reasoning_depth: "xhigh",
    context_window_tokens: 200000,
  });
  assert.ok(put.body.bindings.moderator); // moderator binding kept
  assert.equal(put.body.bindings.p02, undefined); // unbound players are not sent
  assert.equal(env.el("bindings-modal").style.display, "none");
});

test("bindings: partially filled row is rejected locally; apply-all copies the moderator", async () => {
  const session = F.makeSession({ status: "idle", participants: F.makeParticipants().slice(0, 1) });
  const { env, backend } = await boot({ session });
  env.el("btn-open-bindings-modal").dispatch("click");
  await settle();
  env.el("bindings-table").dispatch("change", {
    target: target({ ".binding-row": { "data-actor": "p01" } }, { getAttribute: (k) => (k === "data-field" ? "provider_id" : null), value: "p1" }),
  });
  env.el("btn-save-bindings").dispatch("click");
  await settle();
  assert.equal(backend.calls.filter((c) => c.method === "PUT").length, 0);
  assert.match(env.toasts[env.toasts.length - 1].textContent, /请同时选择 Provider 和模型/);

  env.el("btn-bindings-apply-all").dispatch("click");
  env.el("btn-save-bindings").dispatch("click");
  await settle();
  const put = backend.calls.find((c) => c.method === "PUT");
  assert.equal(put.body.bindings.p01.model_id, "model-x");
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
    binding_issues: [{ actor_id: "p01", message: "Provider 已被删除" }],
  });
  const { env, backend } = await H.bootLive({ session });
  assert.equal(env.el("restore-notice").hidden, false);
  assert.match(env.el("restore-notice-text").textContent, /已恢复上次对局（已暂停）/);
  assert.match(env.el("restore-notice-text").textContent, /p01（Provider 已被删除）/);
  assert.equal(backend.calls.filter((c) => c.url.includes("/control/")).length, 0);
  env.el("btn-dismiss-notice").dispatch("click");
  assert.equal(env.el("restore-notice").hidden, true);
  // a fresh, non-restored snapshot keeps it hidden
  env.sse("snapshot", { session: { ...session, restored_from_save: false, binding_issues: [] } }, 50);
  assert.equal(env.el("restore-notice").hidden, true);
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

test("sessions modal lists, loads and deletes external saves (current one is protected)", async () => {
  const { env, backend } = await boot();
  env.el("btn-open-sessions-modal").dispatch("click");
  await settle();
  const html = env.el("session-list").innerHTML;
  assert.match(html, /old-1/);
  assert.match(html, /当前/);
  assert.match(html, /data-action="delete-session" disabled/);
  env.el("session-list").dispatch("click", {
    target: target({ "[data-action]": { "data-action": "delete-session" }, ".session-item": { "data-session-id": "old-1" } }),
  });
  await settle();
  assert.ok(backend.calls.some((c) => c.method === "DELETE" && c.url.endsWith("/api/sessions/old-1")));
  assert.ok(!env.el("session-list").innerHTML.includes("old-1"));

  backend.sessions.push({ session_id: "s9", current: false, status: "paused" });
  env.el("btn-refresh-sessions").dispatch("click");
  await settle();
  env.el("session-list").dispatch("click", {
    target: target({ "[data-action]": { "data-action": "load-session" }, ".session-item": { "data-session-id": "s9" } }),
  });
  await settle();
  env.flushRaf();
  assert.ok(backend.calls.some((c) => c.method === "POST" && c.url.endsWith("/api/sessions/s9/load")));
  assert.equal(env.el("sessions-modal").style.display, "none");
  assert.ok(!backend.calls.some((c) => c.url.includes("/control/start")));
  assert.equal(env.app().state.session.session_id, "s9"); // the loaded game replaced the old one
});

test("UI keeps thinking depth and execution depth distinct", () => {
  assert.match(HTML_SRC, /执行深度:/);
  assert.match(HTML_SRC, /不是模型的“思考深度”/);
  assert.match(HTML_SRC, /思考深度 ≠ 执行深度/);
});

test("usage cost comes from the server total (price snapshots) and stays live with agent.metrics", async () => {
  const session = F.makeSession({ usage_stats: { total_tokens: 1500, prompt_tokens: 1000, completion_tokens: 500, total_cost_usd: 0.1234, by_actor: {}, by_model: {} } });
  const { env } = await H.bootLive({ session });
  assert.equal(env.el("usage-cost-text").textContent, "$0.1234");
  assert.match(env.el("usage-tokens-text").textContent, /1\.5K/);
  env.sse("agent.metrics", { id: "p01", calls: 1, prompt_tokens: 1000, completion_tokens: 1000, cost_usd: 0.0766, tool_calls: 2, thinking_rounds: 1, rejections: 0 }, 160);
  assert.equal(env.el("usage-cost-text").textContent, "$0.2000");
  assert.match(env.el("usage-tokens-text").textContent, /3\.5K/);
});

test("execution depth: the select posts the chosen depth; the session value is mirrored back", async () => {
  const { env, backend } = await H.bootLive({ session: F.makeSession({ execution_depth: "high" }) });
  assert.equal(env.el("select-exec-depth").value, "high");
  env.el("select-exec-depth").value = "low";
  env.el("select-exec-depth").dispatch("change");
  await settle();
  const post = backend.calls.find((c) => c.url.endsWith("/api/session/execution-depth"));
  assert.deepEqual(post.body, { execution_depth: "low" });
  env.sse("session.updated", { execution_depth: "xhigh" }, 170);
  assert.equal(env.el("select-exec-depth").value, "xhigh");
});

// ---- v0.11: app wiring for layout + workspace ------------------------------------------------------
test("v0.11 app: workspace label, board collapse and workspace modal are wired", async () => {
  const { env, backend } = await boot();
  assert.ok(backend.calls.some((c) => c.url.endsWith("/api/workspaces")));
  assert.equal(env.el("workspace-name-label").textContent, "Av11WS");

  assert.ok(!env.el("main-container").classList.contains("board-collapsed"));
  env.el("btn-toggle-board").dispatch("click");
  assert.ok(env.el("main-container").classList.contains("board-collapsed"));
  env.el("btn-toggle-board").dispatch("click");
  assert.ok(!env.el("main-container").classList.contains("board-collapsed"));

  env.el("btn-open-workspace-modal").dispatch("click");
  await settle(40);
  assert.equal(env.el("workspace-modal").style.display, "flex");
  assert.equal(env.el("workspace-current-name").textContent, "Av11WS");
  assert.match(env.el("workspace-session-list").innerHTML, /old-1/);
});

test("v0.11 app: a successful workspace switch reloads the page (window.location)", async () => {
  // switching is refused while a game runs, so start from a fresh (idle) one
  const { env, backend } = await boot({ session: F.makeSession({ status: "idle" }) });
  env.el("btn-open-workspace-modal").dispatch("click");
  await settle(40);
  env.el("input-workspace-path").value = "D:\\Arena-WS\\Next";
  env.el("btn-switch-workspace").dispatch("click");
  await settle(40);
  assert.equal(backend.lastWorkspaceSelect.path, "D:\\Arena-WS\\Next");
  assert.equal(env.reloads.length, 1);
});

H.runTests(tests);
