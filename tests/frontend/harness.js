"use strict";
// Shared test harness (not a test): runs static/js/app.js (+ layout / providers / workspace) under node
// against a tiny fake DOM and a fake backend. No browser, no jsdom.
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.join(__dirname, "..", "..");
const jsPath = (name) => path.join(ROOT, "static", "js", name);

const Core = require(jsPath("core.js"));
const Avatar = require(jsPath("avatar.js"));
const Store = require(jsPath("store.js"));
const View = require(jsPath("view.js"));
const F = require("./fixtures.js");

const read = (name) => fs.readFileSync(jsPath(name), "utf8");
const SRC = {
  app: read("app.js"),
  providers: read("providers.js"),
  layout: read("layout.js"),
  workspace: read("workspace.js"),
};
const HTML_SRC = fs.readFileSync(path.join(ROOT, "static", "index.html"), "utf8");
const CSS_SRC = fs.readFileSync(path.join(ROOT, "static", "css", "style.css"), "utf8");

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
    this.offsetWidth = 316;
    this.offsetHeight = 200;
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
    return e;
  }
  getAttribute(k) {
    return k in this.attrs ? this.attrs[k] : null;
  }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
  }
  hasAttribute(k) {
    return k in this.attrs;
  }
  querySelector() {
    return null;
  }
  closest() {
    return null;
  }
  getBoundingClientRect() {
    return { left: 100, top: 60, right: 164, bottom: 104, width: 64, height: 44 };
  }
  focus() {}
  click() {
    this.dispatch("click");
  }
}

/** An event target stub: closest(selector) answers from `map` (selector fragment -> attributes). */
const target = (map, extra = {}) => ({
  closest: (sel) => {
    for (const k of Object.keys(map)) {
      if (sel.includes(k)) return { getAttribute: (a) => (map[k] && map[k][a] !== undefined ? map[k][a] : null) };
    }
    return null;
  },
  getAttribute: () => null,
  ...extra,
});

function resp(status, body, ct = "application/json") {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (k) => (k.toLowerCase() === "content-type" ? ct : null) },
    text: async () => text,
  };
}

function makeEnv({ fetchImpl, storedToken = "", storage: initialStorage = {}, innerWidth = 1280, innerHeight = 800 } = {}) {
  const els = new Map();
  const timers = [];
  let timerId = 0;
  const rafQueue = [];
  const toasts = [];
  const esInstances = [];
  const reloads = [];
  const storage = new Map(Object.entries(initialStorage));
  if (storedToken) storage.set("arena_api_token", storedToken);

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
    location: {
      search: "",
      href: "http://localhost/arena/",
      pathname: "/arena/",
      reload: () => reloads.push(1),
    },
    getSelection: () => ({ toString: () => "" }),
    innerWidth,
    innerHeight,
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
    ArenaAvatar: Avatar,
    ArenaStore: Store,
    ArenaView: View,
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
  const env = {
    sandbox,
    els,
    timers,
    esInstances,
    storage,
    toasts,
    reloads,
    document,
    el: (id) => document.getElementById(id),
    run: () => {
      vm.runInContext(SRC.layout, sandbox, { filename: "layout.js" });
      vm.runInContext(SRC.providers, sandbox, { filename: "providers.js" });
      vm.runInContext(SRC.workspace, sandbox, { filename: "workspace.js" });
      vm.runInContext(SRC.app, sandbox, { filename: "app.js" });
    },
    flushRaf: () => {
      // renders may schedule more renders: drain until quiet
      for (let guard = 0; guard < 10 && rafQueue.length; guard++) rafQueue.splice(0).forEach((cb) => cb());
    },
    runNextTimer: () => {
      const t = timers.shift();
      if (t) t.fn();
      return t;
    },
    lastEs: () => esInstances[esInstances.length - 1],
    /** Deliver one SSE v2 event to the newest EventSource (then paint the frame). */
    sse: (type, data = {}, id, { flush = true } = {}) => {
      const es = esInstances[esInstances.length - 1];
      const payload = { id: id === undefined ? env._nextId++ : id, type, data };
      es.onmessage({ data: JSON.stringify(payload) });
      if (flush) env.flushRaf();
      return payload;
    },
    _nextId: 1000,
    app: () => sandbox.ArenaApp,
  };
  return env;
}

// ---- fake backend ------------------------------------------------------------
const clone = (v) => JSON.parse(JSON.stringify(v));

function makeBackend(overrides = {}) {
  const session = overrides.session || F.makeSession();
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
        models: [{ id: "model-x", input_price_per_m: 0.5, output_price_per_m: 2, context_window_tokens: 128000 }],
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
    bindings: session.model_bindings,
    sessions: [
      { session_id: "s-test", current: true, status: "idle", record_count: 0 },
      { session_id: "old-1", current: false, status: "paused", record_count: 7 },
    ],
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
    controlSession: null, // what POST /api/control/* answers with (defaults to the current session)
    ...overrides,
    session,
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
    if (key === "GET /api/state") return resp(200, { app_version: "v0.12-test", session: b.session });
    if (/^POST \/api\/control\/(new|start|pause|force-pause)$/.test(key)) {
      return resp(200, { ok: true, session: b.controlSession || b.session });
    }
    if (key === "POST /api/session/execution-depth") return resp(200, { ok: true, execution_depth: body.execution_depth });
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
        const e = {
          id: body.model_id,
          input_price_per_m: body.input_price_per_m,
          output_price_per_m: body.output_price_per_m,
          context_window_tokens: body.context_window_tokens ?? 1000000,
        };
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
    if (key === "GET /api/settings/templates") return resp(200, { templates: [], slots: 3, locked: false });
    if (key === "GET /api/sessions") return resp(200, { sessions: b.sessions });
    if (key === "GET /api/workspaces") return resp(200, b.workspaces);
    if (key === "POST /api/workspaces/pick-native-folder") return resp(200, { path: "D:\\Picked\\WS" });
    if (key === "POST /api/workspaces/select") {
      b.lastWorkspaceSelect = body;
      return resp(200, { workspace: { id: "w9", name: "Next", path: body.path }, session: b.session });
    }
    if ((m = key.match(/^DELETE \/api\/sessions\/(.+)$/))) {
      b.sessions = b.sessions.filter((x) => x.session_id !== decodeURIComponent(m[1]));
      return resp(200, { ok: true });
    }
    if ((m = key.match(/^POST \/api\/sessions\/(.+)\/load$/))) {
      return resp(200, { ok: true, session: { ...b.session, session_id: decodeURIComponent(m[1]), status: "paused" } });
    }
    if (key === "POST /api/session/save") return resp(200, { ok: true, filename: "x.json", session: b.session });
    if (key === "POST /api/session/import") return resp(200, { ok: true, session: { ...b.session, session_id: "imported" } });
    return resp(404, { detail: "nf" });
  };
  return b;
}

/** legacy helper: (calls array, options) -> fetch */
function routeFetch(calls, { requireToken = "", session } = {}) {
  const b = makeBackend({ requireToken, ...(session ? { session } : {}) });
  return async (url, init) => {
    const r = await b.fetch(url, init);
    calls.push({ url, init });
    return r;
  };
}

/** Boot app.js against a backend and paint the first frame. */
async function boot(overrides, envOpts) {
  const backend = makeBackend(overrides);
  const env = makeEnv({ fetchImpl: backend.fetch, ...(envOpts || {}) });
  env.run();
  await settle();
  env.flushRaf();
  return { env, backend };
}

/** boot() + connect the SSE stream and deliver the first snapshot. */
async function bootLive(overrides, envOpts) {
  const ctx = await boot(overrides, envOpts);
  ctx.env.runNextTimer(); // the delayed first connect
  return ctx;
}

const posts = (b, method, suffix) => b.calls.filter((c) => c.method === method && c.url.endsWith(suffix));

function runTests(tests) {
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
}

module.exports = {
  ROOT,
  Core,
  Avatar,
  Store,
  View,
  F,
  SRC,
  HTML_SRC,
  CSS_SRC,
  FakeEl,
  target,
  resp,
  clone,
  tick,
  settle,
  makeEnv,
  makeBackend,
  routeFetch,
  boot,
  bootLive,
  posts,
  runTests,
};
