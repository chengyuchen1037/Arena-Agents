"use strict";
// Smoke test for static/js/app.js running against a tiny fake DOM.
// Run: node tests/frontend/test_app_smoke.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.join(__dirname, "..", "..");
const Core = require(path.join(ROOT, "static", "js", "core.js"));
const APP_SRC = fs.readFileSync(path.join(ROOT, "static", "js", "app.js"), "utf8");
const PROVIDERS_SRC = fs.readFileSync(path.join(ROOT, "static", "js", "providers.js"), "utf8");
const LAYOUT_SRC = fs.readFileSync(path.join(ROOT, "static", "js", "layout.js"), "utf8");
const WORKSPACE_SRC = fs.readFileSync(path.join(ROOT, "static", "js", "workspace.js"), "utf8");
const HTML_SRC = fs.readFileSync(path.join(ROOT, "static", "index.html"), "utf8");

const tests = [];
const test = (name, fn) => tests.push([name, fn]);
const tick = () => new Promise((r) => setImmediate(r));
async function settle(n = 15) {
  for (let i = 0; i < n; i++) await tick();
}

// ---- fake DOM ---------------------------------------------------------------
class FakeEl {
  constructor(tag = "div", id = "") {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.children = [];
    this.parentNode = null;
    this.style = {};
    this.attrs = {};
    this.listeners = {};
    this.textContent = "";
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = true;
    this.className = "";
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this._html = "";
    this._classes = new Set();
    const self = this;
    this.classList = {
      add: (...c) => c.forEach((x) => self._classes.add(x)),
      remove: (...c) => c.forEach((x) => self._classes.delete(x)),
      contains: (c) => self._classes.has(c),
      toggle: (c, on) => {
        const want = on === undefined ? !self._classes.has(c) : Boolean(on);
        if (want) self._classes.add(c);
        else self._classes.delete(c);
        return want;
      },
    };
    this.content = { firstElementChild: null };
  }
  get innerHTML() {
    return this._html;
  }
  set innerHTML(v) {
    this._html = String(v);
    if (this.tagName === "TEMPLATE") {
      const t = this._html.trim();
      if (!t) this.content.firstElementChild = null;
      else {
        const el = new FakeEl("div");
        el.outerHTML = t;
        this.content.firstElementChild = el;
      }
    } else {
      this.children.forEach((c) => (c.parentNode = null));
      this.children = [];
    }
  }
  get firstChild() {
    return this.children[0] || null;
  }
  get lastElementChild() {
    return this.children[this.children.length - 1] || null;
  }
  get isConnected() {
    return this.parentNode !== null;
  }
  appendChild(n) {
    if (n.parentNode) n.remove();
    n.parentNode = this;
    this.children.push(n);
    return n;
  }
  replaceChild(n, old) {
    const i = this.children.indexOf(old);
    old.parentNode = null;
    n.parentNode = this;
    this.children[i] = n;
    return old;
  }
  insertBefore(n, ref) {
    if (n.parentNode) n.remove();
    n.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(n);
    else this.children.splice(i, 0, n);
    return n;
  }
  remove() {
    if (this.parentNode) {
      const i = this.parentNode.children.indexOf(this);
      if (i >= 0) this.parentNode.children.splice(i, 1);
      this.parentNode = null;
    }
  }
  addEventListener(t, fn) {
    (this.listeners[t] = this.listeners[t] || []).push(fn);
  }
  removeEventListener(t, fn) {
    this.listeners[t] = (this.listeners[t] || []).filter((f) => f !== fn);
  }
  dispatch(t, evt = {}) {
    const e = { type: t, target: this, preventDefault() {}, ...evt };
    (this.listeners[t] || []).slice().forEach((fn) => fn(e));
  }
  getAttribute(k) {
    return this.attrs[k] ?? null;
  }
  setAttribute(k, v) {
    this.attrs[k] = v;
  }
  querySelector() {
    return null;
  }
  closest() {
    return null;
  }
  focus() {}
  click() {}
}

function ids(src) {
  return new Set([...src.matchAll(/getElementById\("([^"]+)"\)/g)].map((m) => m[1]));
}

function makeEnv({ fetchImpl, storedToken = "" }) {
  const els = new Map();
  const timers = [];
  let timerId = 0;
  const rafQueue = [];
  const toasts = [];
  const esInstances = [];
  const storage = new Map(storedToken ? [["arena_api_token", storedToken]] : []);

  const docListeners = {};
  const winListeners = {};
  const document = {
    getElementById(id) {
      if (!els.has(id)) els.set(id, new FakeEl("div", id));
      return els.get(id);
    },
    createElement(tag) {
      return new FakeEl(tag);
    },
    addEventListener(t, fn) {
      (docListeners[t] = docListeners[t] || []).push(fn);
    },
    removeEventListener(t, fn) {
      docListeners[t] = (docListeners[t] || []).filter((f) => f !== fn);
    },
    dispatchEvent(t, evt = {}) {
      const e = { type: t, target: this, preventDefault() {}, ...evt };
      (docListeners[t] || []).slice().forEach((fn) => fn(e));
      return e;
    },
    documentElement: new FakeEl("html"),
    body: new FakeEl("body"),
    activeElement: null,
    currentScript: { src: "http://localhost/arena/static/js/app.js" },
  };

  class FakeEventSource {
    constructor(url) {
      this.url = url;
      this.closed = false;
      esInstances.push(this);
    }
    close() {
      this.closed = true;
    }
  }

  const sandbox = {
    console,
    URL,
    Blob,
    CSS: { escape: (s) => s },
    document,
    localStorage: {
      getItem: (k) => (storage.has(k) ? storage.get(k) : null),
      setItem: (k, v) => storage.set(k, String(v)),
      removeItem: (k) => storage.delete(k),
    },
    EventSource: FakeEventSource,
    fetch: fetchImpl,
    location: { search: "", href: "http://localhost/arena/", pathname: "/arena/" },
    getSelection: () => ({ toString: () => "" }),
    innerWidth: 1280,
    innerHeight: 800,
    addEventListener(t, fn) {
      (winListeners[t] = winListeners[t] || []).push(fn);
    },
    removeEventListener(t, fn) {
      winListeners[t] = (winListeners[t] || []).filter((f) => f !== fn);
    },
    dispatchEvent(t, evt = {}) {
      const e = { type: t, target: this, ...evt };
      (winListeners[t] || []).slice().forEach((fn) => fn(e));
      return e;
    },
    requestAnimationFrame: (cb) => {
      rafQueue.push(cb);
      return rafQueue.length;
    },
    setTimeout: (fn, ms) => {
      timers.push({ id: ++timerId, fn, ms });
      return timerId;
    },
    clearTimeout: (id) => {
      const i = timers.findIndex((t) => t.id === id);
      if (i >= 0) timers.splice(i, 1);
    },
    ArenaCore: Core,
    confirm: () => true,
  };
  sandbox.window = sandbox;
  document.getElementById("toast-container");
  // capture toasts
  els.get("toast-container").appendChild = (n) => {
    toasts.push(n);
    return n;
  };

  vm.createContext(sandbox);
  return {
    sandbox,
    els,
    timers,
    esInstances,
    storage,
    toasts,
    el: (id) => document.getElementById(id),
    run: () => {
      vm.runInContext(LAYOUT_SRC, sandbox, { filename: "layout.js" });
      vm.runInContext(PROVIDERS_SRC, sandbox, { filename: "providers.js" });
      vm.runInContext(WORKSPACE_SRC, sandbox, { filename: "workspace.js" });
      vm.runInContext(APP_SRC, sandbox, { filename: "app.js" });
    },
    flushRaf: () => {
      const q = rafQueue.splice(0);
      q.forEach((cb) => cb());
    },
    runNextTimer: () => {
      const t = timers.shift();
      if (t) t.fn();
      return t;
    },
  };
}

function resp(status, body, ct = "application/json") {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (k) => (k.toLowerCase() === "content-type" ? ct : null) },
    text: async () => text,
  };
}

const SESSION = {
  session_id: "s1",
  title: "T",
  status: "idle",
  records: [],
  participants: [{ id: "p1", seat: 1, name: "Ann'<b>", tags: [] }],
  channels: [
    {
      id: "c1",
      name: "C",
      type: "t",
      status: "open",
      members: ["p1"],
      interrupt_quotas: { p1: { player_id: "p1", player_name: "A", used: '<i>', max_count: "2" } },
    },
  ],
  dashboard: {
    factions: [{ name: "F'", alive: '<img src=x onerror=alert(1)>', total: 3, variant: "x" }],
    tag_stats: [{ name: "T", count: "<script>", variant: "neutral" }],
  },
  usage_stats: { total_tokens: 0, total_cost_usd: 0 },
  execution_depth: "medium",
  model_bindings: {
    moderator: { provider_id: "p1", model_id: "model-x", reasoning_depth: "high", context_window_tokens: 128000 },
  },
  binding_issues: [],
};

function makeBackend(overrides = {}) {
  const b = {
    calls: [],
    providers: [
      {
        id: "p1",
        name: "DeepSeek",
        protocol: "openai_chat",
        base_url: "https://api.example.com",
        api_key_set: true,
        read_only: false,
        models: [
          { id: "model-x", input_price_per_m: 0.5, output_price_per_m: 2, context_window_tokens: 128000 },
        ],
      },
      {
        id: "p2",
        name: "Claude",
        protocol: "anthropic_messages",
        base_url: "https://api.anthropic.com",
        api_key_set: true,
        read_only: false,
        models: [{ id: "claude-y", input_price_per_m: 3, output_price_per_m: 15, context_window_tokens: 200000 }],
      },
      { id: "env", name: "Env", protocol: "openai_chat", base_url: "https://e.com", api_key_set: true, read_only: true, models: [] },
    ],
    bindings: SESSION.model_bindings,
    sessions: [
      { session_id: "s1", current: true, status: "idle", record_count: 0 },
      { session_id: "old-1", current: false, status: "paused", record_count: 7 },
    ],
    session: SESSION,
    discovered: ["model-x", "m-new", "other/slash"],
    workspaces: {
      enabled: true,
      current: { id: "w1", name: "Av11WS", path: "D:\\Av11\\WS", status: "ok" },
      recent: [{ id: "w1", name: "Av11WS", path: "D:\\Av11\\WS" }],
      default_path: "D:\\Arena-Workspace\\Default",
      native_picker: true,
    },
    requireToken: "",
    failures: {},
    ...overrides,
  };
  b.fetch = async (url, init) => {
    const body = init.body ? JSON.parse(init.body) : undefined;
    const call = { url, method: init.method, body, headers: init.headers };
    b.calls.push(call);
    if (b.requireToken && init.headers["X-Arena-Token"] !== b.requireToken) {
      return resp(401, { detail: "Invalid or missing API token" });
    }
    const key = init.method + " " + url.replace(/^\/arena/, "");
    if (b.failures[key]) return resp(b.failures[key][0], { detail: b.failures[key][1] });
    const find = (id) => b.providers.find((p) => p.id === id);
    let m;
    if (key === "GET /api/state") return resp(200, { session: b.session });
    if (key === "GET /api/providers") return resp(200, { providers: b.providers });
    if (key === "POST /api/providers") {
      const p = { id: "new1", read_only: false, api_key_set: true, models: [], ...body };
      delete p.api_key;
      b.providers.push(p);
      return resp(201, p);
    }
    if ((m = key.match(/^PATCH \/api\/providers\/([^/]+)$/))) {
      Object.assign(find(m[1]), { ...body, api_key: undefined });
      return resp(200, find(m[1]));
    }
    if ((m = key.match(/^DELETE \/api\/providers\/([^/]+)$/))) {
      b.providers = b.providers.filter((p) => p.id !== m[1]);
      return resp(200, { ok: true });
    }
    if ((m = key.match(/^POST \/api\/providers\/([^/]+)\/discover-models$/))) {
      return resp(200, { models: b.discovered, existing_models: find(m[1]).models.map((x) => x.id) });
    }
    if ((m = key.match(/^(POST|PATCH|DELETE) \/api\/providers\/([^/]+)\/models$/))) {
      const p = find(m[2]);
      if (m[1] === "POST") {
        const e = { id: body.model_id, input_price_per_m: body.input_price_per_m, output_price_per_m: body.output_price_per_m, context_window_tokens: body.context_window_tokens ?? 1000000 };
        p.models.push(e);
        return resp(201, e);
      }
      if (m[1] === "PATCH") {
        Object.assign(p.models.find((x) => x.id === body.model_id), body, { id: body.model_id });
        return resp(200, {});
      }
      p.models = p.models.filter((x) => x.id !== body.model_id);
      return resp(200, { ok: true });
    }
    if (key === "GET /api/settings/bindings") {
      return resp(200, { bindings: b.bindings, providers: b.providers, locked: false, issues: [] });
    }
    if (key === "PUT /api/settings/bindings") {
      b.bindings = body.bindings;
      return resp(200, { bindings: b.bindings, providers: b.providers });
    }
    if (key === "GET /api/sessions") return resp(200, { sessions: b.sessions });
    if (key === "GET /api/workspaces") return resp(200, b.workspaces);
    if (key === "POST /api/workspaces/pick-native-folder") {
      return resp(200, { path: "D:\\Picked\\WS" });
    }
    if (key === "POST /api/workspaces/select") {
      b.lastWorkspaceSelect = body;
      return resp(200, {
        workspace: { id: "w9", name: "Next", path: body.path },
        session: b.session,
      });
    }
    if ((m = key.match(/^DELETE \/api\/sessions\/(.+)$/))) {
      b.sessions = b.sessions.filter((x) => x.session_id !== decodeURIComponent(m[1]));
      return resp(200, { ok: true });
    }
    if ((m = key.match(/^POST \/api\/sessions\/(.+)\/load$/))) {
      return resp(200, { ok: true, session: { ...SESSION, session_id: decodeURIComponent(m[1]), status: "paused" } });
    }
    return resp(404, { detail: "nf" });
  };
  return b;
}

// legacy helper used by older tests: (calls array, options) -> fetch
function routeFetch(calls, { requireToken = "" } = {}) {
  const b = makeBackend({ requireToken });
  return async (url, init) => {
    const r = await b.fetch(url, init);
    calls.push({ url, init });
    return r;
  };
}

// ---- tests ------------------------------------------------------------------
test("every id used by the frontend modules exists in index.html", () => {
  const htmlIds = new Set([...HTML_SRC.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
  // These two are optional widgets: app.js null-guards them and index.html
  // does not currently ship them (pre-existing since v0.8).
  const optional = new Set(["scenario-name"]);
  const combined = APP_SRC + PROVIDERS_SRC + LAYOUT_SRC + WORKSPACE_SRC;
  const missing = [...ids(combined)].filter((i) => !htmlIds.has(i) && !optional.has(i));
  assert.deepEqual(missing, []);
});

test("index.html uses relative asset paths and loads core.js, providers.js, app.js in order", () => {
  assert.ok(!/(?:src|href)="\/(?:static|api)/.test(HTML_SRC));
  const c = HTML_SRC.indexOf("static/js/core.js");
  const p = HTML_SRC.indexOf("static/js/providers.js");
  const a = HTML_SRC.indexOf("static/js/app.js");
  const l = HTML_SRC.indexOf("static/js/layout.js");
  const w = HTML_SRC.indexOf("static/js/workspace.js");
  assert.ok(c >= 0 && l > c && l < p && p < w && w < a);
});

test("frontend modules have no absolute /api or /static URLs", () => {
  for (const src of [APP_SRC, PROVIDERS_SRC, LAYOUT_SRC, WORKSPACE_SRC]) {
    assert.ok(!/["'`]\/(api|static)\//.test(src));
  }
});

test("no token needed: no modal, no header, sub-path prefix, safe numeric rendering", async () => {
  const calls = [];
  const env = makeEnv({ fetchImpl: routeFetch(calls) });
  env.run();
  await settle();
  env.flushRaf();

  assert.deepEqual(
    calls.map((c) => c.url),
    [
      "/arena/api/providers",
      "/arena/api/settings/bindings",
      "/arena/api/state",
      "/arena/api/workspaces",
    ]
  );
  assert.ok(calls.every((c) => !("X-Arena-Token" in c.init.headers)));
  assert.notEqual(env.el("token-modal").style.display, "flex");

  // moderator binding shown in the status pill; no legacy config endpoint used
  assert.match(env.el("api-status-label").textContent, /DeepSeek \/ model-x/);
  assert.ok(calls.every((c) => !c.url.endsWith("/api/config/llm")));

  // numeric fields are coerced, never injected
  const fHtml = env.el("factions-list").innerHTML;
  assert.ok(!fHtml.includes("<img"));
  assert.match(fHtml, /0 \/ 3/);
  assert.ok(fHtml.includes("F&#39;"));
  assert.ok(!env.el("tag-stats-list").innerHTML.includes("<script"));
  const side = env.el("channels-list").innerHTML;
  assert.ok(!side.includes("<i>"));
  assert.match(side, /\(0\/2\)/);
  const board = env.el("participants-grid").innerHTML;
  assert.ok(!board.includes("<b>"));
  assert.ok(board.includes("&#39;"));

  // SSE is delayed 300ms and uses the prefixed URL without a token
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

test("SSE messages are coalesced into one render per frame", async () => {
  const calls = [];
  const env = makeEnv({ fetchImpl: routeFetch(calls) });
  env.run();
  await settle();
  env.flushRaf();
  env.runNextTimer();
  const es = env.esInstances[0];
  const before = env.el("participants-grid").innerHTML;
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
    es.onmessage({ data: JSON.stringify({ event: "stream_chunk", session: SESSION }) });
  }
  es.onmessage({ data: JSON.stringify({ event: "state_update", session: SESSION }) });
  assert.equal(renders, 0); // nothing rendered synchronously
  env.flushRaf();
  assert.equal(renders, 1); // one frame, one render (full wins over chunk-only)
  assert.equal(env.el("participants-grid").innerHTML, before);
});

test("streaming draft is patched in place, not recreated, on each chunk", async () => {
  const calls = [];
  const env = makeEnv({ fetchImpl: routeFetch(calls) });
  env.run();
  await settle();
  env.flushRaf();
  env.runNextTimer();
  const es = env.esInstances[0];
  const draft = (text) => ({
    ...SESSION,
    streaming_draft: {
      actor_id: "p1",
      actor_name: "A",
      action_id: "a1",
      stream: "channel",
      channel_id: "c1",
      thought_text: "",
      content_text: text,
    },
  });
  es.onmessage({ data: JSON.stringify({ event: "stream_chunk", session: draft("he") }) });
  env.flushRaf();
  const container = env.el("feed-streaming-container");
  assert.equal(container.children.length, 1);
  const card1 = container.children[0];
  es.onmessage({ data: JSON.stringify({ event: "stream_chunk", session: draft("hello wor") }) });
  env.flushRaf();
  assert.equal(container.children.length, 1);
  assert.equal(container.children[0], card1, "card element must be reused");
  // draft finished -> removed
  es.onmessage({ data: JSON.stringify({ event: "state_update", session: SESSION }) });
  env.flushRaf();
  assert.equal(container.children.length, 0);
});

test("import clears level1Expanded/level2Expanded and resets on a new session id", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    if (url.endsWith("/api/session/import")) {
      return resp(200, { session: { ...SESSION, session_id: "s2" } });
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
  const imp = calls.find((c) => c.url.endsWith("/api/session/import"));
  assert.ok(imp, "import request sent");
  assert.ok(env.toasts.some((t) => /已成功导入/.test(t.textContent)));
});

test("non-JSON 500 on init does not crash and toasts a clear message", async () => {
  const fetchImpl = async (url) => {
    if (url.endsWith("/api/config/llm")) return resp(500, "<html>boom</html>", "text/html");
    return resp(500, "<html>boom</html>", "text/html");
  };
  const env = makeEnv({ fetchImpl });
  env.run();
  await settle();
  const msgs = env.toasts.map((t) => t.textContent).join("|");
  assert.match(msgs, /500/);
  assert.ok(!/SyntaxError|Unexpected token/.test(msgs));
});

// ---- v0.10 ------------------------------------------------------------------
const target = (map, extra = {}) => ({
  closest: (sel) => {
    for (const k of Object.keys(map)) if (sel.includes(k)) return { getAttribute: (a) => map[k][a] ?? null };
    return null;
  },
  getAttribute: () => null,
  ...extra,
});

async function boot(overrides) {
  const backend = makeBackend(overrides);
  const env = makeEnv({ fetchImpl: backend.fetch });
  env.run();
  await settle();
  env.flushRaf();
  return { env, backend };
}
const posts = (b, method, suffix) =>
  b.calls.filter((c) => c.method === method && c.url.endsWith(suffix));

test("startup loads providers + bindings; seat cards show model binding; no /config/llm", async () => {
  const { env, backend } = await boot();
  assert.ok(backend.calls.some((c) => c.url.endsWith("/api/providers")));
  assert.ok(backend.calls.some((c) => c.url.endsWith("/api/settings/bindings")));
  assert.ok(!backend.calls.some((c) => c.url.endsWith("/api/config/llm")));
  const board = env.el("participants-grid").innerHTML;
  assert.match(board, /card-binding/);
  assert.match(board, /未绑定/); // p1 has no binding yet
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

test("bindings: moderator + 12-style player rows, provider change resets model, save PUTs payload", async () => {
  const session = {
    ...SESSION,
    status: "paused",
    participants: [
      { id: "p01", seat: 1, name: "甲", tags: [] },
      { id: "p02", seat: 2, name: "乙", tags: [] },
    ],
  };
  const { env, backend } = await boot({ session });
  env.el("btn-open-bindings-modal").dispatch("click");
  await settle();
  assert.equal(env.el("bindings-modal").style.display, "flex");
  const html = env.el("bindings-table").innerHTML;
  assert.equal((html.match(/class="binding-row"/g) || []).length, 3);
  assert.match(html, /低|low（浅）/);
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
  const session = { ...SESSION, status: "idle", participants: [{ id: "p01", seat: 1, name: "甲", tags: [] }] };
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

test("bindings are LOCKED while running: controls disabled, save does not send, backend 409 shows", async () => {
  const session = { ...SESSION, status: "running", participants: [{ id: "p01", seat: 1, name: "甲", tags: [] }] };
  const { env, backend } = await boot({ session });
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

  // when the game pauses the lock is released live
  const paused = { ...session, status: "paused" };
  backend.session = paused;
  const es = (() => { env.runNextTimer(); return env.esInstances[0]; })();
  es.onmessage({ data: JSON.stringify({ event: "status_change", session: paused }) });
  env.flushRaf();
  assert.equal(env.el("btn-delete-provider").disabled, false);
  assert.equal(env.el("select-provider-protocol").disabled, false);
});

test("backend 409 on saving bindings is shown as a toast", async () => {
  const session = { ...SESSION, status: "paused", participants: [] };
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
  const session = {
    ...SESSION,
    status: "paused",
    restored_from_save: true,
    restore_notice: "已恢复上次对局（已暂停）",
    binding_issues: [{ actor_id: "p01", message: "Provider 已被删除" }],
  };
  const { env, backend } = await boot({ session });
  assert.equal(env.el("restore-notice").hidden, false);
  assert.match(env.el("restore-notice-text").textContent, /已恢复上次对局（已暂停）/);
  assert.match(env.el("restore-notice-text").textContent, /p01（Provider 已被删除）/);
  assert.equal(backend.calls.filter((c) => c.url.includes("/control/")).length, 0);
  env.el("btn-dismiss-notice").dispatch("click");
  assert.equal(env.el("restore-notice").hidden, true);
  // a fresh, non-restored snapshot keeps it hidden
  env.runNextTimer();
  env.esInstances[0].onmessage({ data: JSON.stringify({ event: "state_update", session: { ...session, restored_from_save: false, binding_issues: [] } }) });
  env.flushRaf();
  assert.equal(env.el("restore-notice").hidden, true);
});

test("/api/state top-level restored=true also raises the notice", async () => {
  const backend = makeBackend({ session: { ...SESSION, status: "paused" } });
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
});

test("UI keeps thinking depth and execution depth distinct", () => {
  assert.match(HTML_SRC, /执行深度:/);
  assert.match(HTML_SRC, /不是模型的“思考深度”/);
  assert.match(HTML_SRC, /思考深度 ≠ 执行深度/);
});

test("usage cost comes from the server total (price snapshots)", async () => {
  const session = { ...SESSION, usage_stats: { total_tokens: 1500, total_cost_usd: 0.1234 } };
  const { env } = await boot({ session });
  assert.equal(env.el("usage-cost-text").textContent, "$0.1234");
  assert.match(env.el("usage-tokens-text").textContent, /1\.5K/);
});

// ---- v0.11: app wiring for layout + workspace --------------------------------
test("v0.11 app: workspace label, board collapse, card size and workspace modal are wired", async () => {
  const { env, backend } = await boot();
  assert.ok(backend.calls.some((c) => c.url.endsWith("/api/workspaces")));
  assert.equal(env.el("workspace-name-label").textContent, "Av11WS");

  assert.ok(!env.el("main-container").classList.contains("board-collapsed"));
  env.el("btn-toggle-board").dispatch("click");
  assert.ok(env.el("main-container").classList.contains("board-collapsed"));
  env.el("btn-toggle-board").dispatch("click");
  assert.ok(!env.el("main-container").classList.contains("board-collapsed"));

  env.el("select-card-size").value = "xlarge";
  env.el("select-card-size").dispatch("change");
  assert.ok(env.el("participants-grid").classList.contains("card-size-xlarge"));
  assert.equal(env.storage.get("arena_card_size_v1"), "xlarge");

  env.el("btn-open-workspace-modal").dispatch("click");
  await settle(40);
  assert.equal(env.el("workspace-modal").style.display, "flex");
  assert.equal(env.el("workspace-current-name").textContent, "Av11WS");
  assert.match(env.el("workspace-session-list").innerHTML, /old-1/);
});

test("v0.11 app: a successful workspace switch reloads the page (window.location)", async () => {
  const { env, backend } = await boot();
  let reloads = 0;
  env.sandbox.location.reload = () => {
    reloads += 1;
  };
  env.el("btn-open-workspace-modal").dispatch("click");
  await settle(40);
  env.el("input-workspace-path").value = "D:\\Arena-WS\\Next";
  env.el("btn-switch-workspace").dispatch("click");
  await settle(40);
  assert.equal(backend.lastWorkspaceSelect.path, "D:\\Arena-WS\\Next");
  assert.equal(reloads, 1);
});

// ---- runner -----------------------------------------------------------------
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
