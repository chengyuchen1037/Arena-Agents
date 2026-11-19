"use strict";
// Frontend tests for the v0.11 features that survive in v0.12.1: layout collapse/resize (the right-hand
// "对局状态" column is gone, the channel column collapses to an avatar rail),
// provider auto_append_path + endpoint preview, and the 3-slot model templates with actor checkboxes.
// Runs against a tiny fake DOM (no browser).
// Run: node tests/frontend/test_v011_ui.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.join(__dirname, "..", "..");
const Core = require(path.join(ROOT, "static", "js", "core.js"));
const APP_SRC = fs.readFileSync(path.join(ROOT, "static", "js", "app.js"), "utf8");
const PROVIDERS_SRC = fs.readFileSync(path.join(ROOT, "static", "js", "providers.js"), "utf8");
const LAYOUT_SRC = fs.readFileSync(path.join(ROOT, "static", "js", "layout.js"), "utf8");
const HTML_SRC = fs.readFileSync(path.join(ROOT, "static", "index.html"), "utf8");
const CSS_SRC = fs.readFileSync(path.join(ROOT, "static", "css", "style.css"), "utf8");

const tests = [];
const test = (name, fn) => tests.push([name, fn]);
const tick = () => new Promise((r) => setImmediate(r));
async function settle(n = 30) {
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
    this.checked = false;
    this.hidden = false;
    this.disabled = false;
    this.className = "";
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
  }
  get innerHTML() {
    return this._html;
  }
  set innerHTML(v) {
    this._html = String(v);
    if (this.tagName !== "TEMPLATE") this.children.forEach((c) => (c.parentNode = null));
    if (this.tagName !== "TEMPLATE") this.children = [];
  }
  get firstChild() {
    return this.children[0] || null;
  }
  get lastElementChild() {
    return this.children[this.children.length - 1] || null;
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
  focus() {}
  click() {
    this.dispatch("click");
  }
  getBoundingClientRect() {
    return { width: 0, height: 0 };
  }
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

const clone = (v) => JSON.parse(JSON.stringify(v));

const PROVIDERS = [
  {
    id: "p1",
    name: "DeepSeek",
    protocol: "openai_chat",
    base_url: "https://api.example.com",
    api_key_set: true,
    read_only: false,
    auto_append_path: true,
    models: [
      {
        id: "ds-chat",
        input_price_per_m: 1,
        output_price_per_m: 2,
        context_window_tokens: 128000,
      },
    ],
  },
  {
    id: "p2",
    name: "Claude",
    protocol: "anthropic_messages",
    base_url: "https://api.anthropic.com",
    api_key_set: true,
    read_only: false,
    auto_append_path: true,
    models: [
      { id: "claude-y", input_price_per_m: 3, output_price_per_m: 15 },
    ],
  },
];

function makeSession(overrides = {}) {
  return {
    session_id: "s1",
    title: "T",
    status: "idle",
    participants: [
      { id: "p01", seat: 1, name: "甲", tags: [] },
      { id: "p02", seat: 2, name: "乙", tags: [] },
    ],
    model_bindings: {
      moderator: { provider_id: "p1", model_id: "ds-chat", reasoning_depth: "high" },
    },
    binding_issues: [],
    records: [],
    channels: [],
    dashboard: {},
    usage_stats: {},
    ...overrides,
  };
}

function makeBackend(sessionRef) {
  const b = {
    calls: [],
    providers: clone(PROVIDERS),
    bindings: {
      moderator: { provider_id: "p1", model_id: "ds-chat", reasoning_depth: "high" },
    },
    templates: [
      {
        slot: 1,
        name: "旗舰",
        provider_id: "p1",
        model_id: "ds-chat",
        reasoning_depth: "xhigh",
        valid: true,
      },
    ],
    sessions: [
      { session_id: "s1", current: true, status: "idle" },
      { session_id: "old-1", current: false, status: "paused" },
    ],
    previews: [],
    failures: {},
    lastApply: null,
    lastSelect: null,
    patchBody: null,
    discoverCalls: 0,
  };
  b.fetch = async (url, init) => {
    const method = (init && init.method) || "GET";
    const body = init && init.body ? JSON.parse(init.body) : undefined;
    b.calls.push({ url, method, body, headers: (init && init.headers) || {} });
    const key = `${method} ${url.replace(/^\/arena/, "")}`;
    if (b.failures[key]) {
      const [status, detail] = b.failures[key];
      return resp(status, { detail });
    }
    if (key === "GET /api/providers") return resp(200, { providers: b.providers });
    if (key === "GET /api/settings/bindings")
      return resp(200, { bindings: b.bindings, providers: b.providers, locked: false, issues: [] });
    if (key === "POST /api/providers/endpoint-preview") {
      b.previews.push(body);
      const base = String(body.base_url || "").replace(/\/+$/, "");
      if (body.auto_append_path === false) {
        return resp(200, {
          auto_append_path: false,
          request_url: base,
          models_url: null,
          warnings: ["关闭自动拼接：Base URL 将原样作为消息 endpoint"],
        });
      }
      return resp(200, {
        auto_append_path: true,
        request_url: `${base}/v1/chat/completions`,
        models_url: `${base}/v1/models`,
        warnings: ["示例警告"],
      });
    }
    if (key === "GET /api/settings/templates") {
      return resp(200, {
        templates: b.templates,
        slots: 3,
        locked: sessionRef().status === "running",
      });
    }
    if (key === "PUT /api/settings/templates") {
      b.templates = (body.templates || []).map((t) => ({ ...t, valid: true }));
      return resp(200, { templates: b.templates, slots: 3, locked: false });
    }
    let m;
    if ((m = key.match(/^POST \/api\/settings\/templates\/(\d+)\/apply$/))) {
      b.lastApply = { slot: Number(m[1]), body };
      const bindings = {};
      (body.actor_ids || []).forEach((a) => {
        bindings[a] = { provider_id: "p1", model_id: "ds-chat", reasoning_depth: "xhigh" };
      });
      return resp(200, { bindings, providers: b.providers, locked: false, issues: [] });
    }
    if (key === "GET /api/sessions") return resp(200, { sessions: b.sessions });
    if ((m = key.match(/^POST \/api\/sessions\/(.+)\/load$/))) {
      return resp(200, {
        session: makeSession({ session_id: decodeURIComponent(m[1]), status: "paused" }),
      });
    }
    if ((m = key.match(/^PATCH \/api\/providers\/([^/]+)$/))) {
      const p = b.providers.find((x) => x.id === m[1]);
      if (!p) return resp(404, { detail: "nf" });
      Object.assign(p, clone(body));
      delete p.api_key;
      b.patchBody = body;
      return resp(200, p);
    }
    if ((m = key.match(/^POST \/api\/providers\/([^/]+)\/discover-models$/))) {
      b.discoverCalls += 1;
      return resp(200, { models: ["ds-chat", "m-new"] });
    }
    return resp(404, { detail: "nf" });
  };
  return b;
}

function makeEnv(opts = {}) {
  const els = new Map();
  const calls = [];
  const toasts = [];
  const reloads = [];
  const loadedSessions = [];
  const timers = [];
  let timerId = 0;
  const docListeners = {};
  const winListeners = {};
  const storageMap = new Map(Object.entries(opts.storage || {}));
  let session = makeSession(opts.session || {});
  const sessionRef = () => session;
  let confirmAnswer = opts.confirm === undefined ? true : opts.confirm;
  const backend = makeBackend(sessionRef);

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
    currentScript: { src: "http://localhost/static/js/app.js" },
  };

  const sandbox = {
    console,
    URL,
    document,
    localStorage: {
      getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
      setItem: (k, v) => storageMap.set(k, String(v)),
      removeItem: (k) => storageMap.delete(k),
    },
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
    innerWidth: opts.innerWidth || 1280,
    innerHeight: opts.innerHeight || 800,
    setTimeout: (fn, ms) => {
      timers.push({ id: ++timerId, fn, ms: ms || 0 });
      return timerId;
    },
    clearTimeout: (id) => {
      const i = timers.findIndex((t) => t.id === id);
      if (i >= 0) timers.splice(i, 1);
    },
    fetch: (url, init) => {
      calls.push({ url, init });
      return backend.fetch(url, init);
    },
    confirm: () => confirmAnswer,
    location: { search: "", href: "http://localhost/", pathname: "/" },
    requestAnimationFrame: (cb) => {
      cb();
      return 1;
    },
    getSelection: () => ({ toString: () => "" }),
    ArenaCore: Core,
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(LAYOUT_SRC, sandbox, { filename: "layout.js" });
  vm.runInContext(PROVIDERS_SRC, sandbox, { filename: "providers.js" });

  const api = Core.createApiClient({
    fetchImpl: sandbox.fetch,
    getToken: () => "",
    prefix: "/",
  });
  const showToast = (m) =>
    toasts.push(typeof m === "string" ? m : String((m && m.message) || m));

  const layout = sandbox.ArenaLayout.create({ Core });
  const providerUI = sandbox.ArenaProviderUI.create({
    api,
    Core,
    showToast,
    getSession: sessionRef,
    onSessionLoaded: (s) => {
      session = s;
      loadedSessions.push(s);
    },
    onChange: () => {},
  });

  return {
    sandbox,
    document,
    els,
    backend,
    calls,
    toasts,
    reloads,
    loadedSessions,
    timers,
    layout,
    providerUI,
    el: (id) => document.getElementById(id),
    getSession: sessionRef,
    setSession: (s) => {
      session = s;
    },
    setConfirm: (v) => {
      confirmAnswer = v;
    },
    flushTimers: (rounds = 20) => {
      let guard = 0;
      while (timers.length && guard++ < rounds) {
        const t = timers.shift();
        t.fn();
      }
    },
    storageMap,
    count: (method, suffix) =>
      calls.filter(
        (c) => c.init.method === method && String(c.url).endsWith(suffix)
      ).length,
  };
}

async function openProviderModal(env) {
  env.el("btn-open-provider-modal").dispatch("click");
  await settle();
  env.flushTimers();
  await settle();
}

async function openBindingsModal(env) {
  env.el("btn-open-bindings-modal").dispatch("click");
  await settle(40);
}

// event-target helpers --------------------------------------------------------
const target = (map, extra = {}) => ({
  closest: (sel) => {
    for (const k of Object.keys(map)) {
      if (sel.includes(k)) return { getAttribute: (a) => map[k][a] ?? null };
    }
    return null;
  },
  getAttribute: () => null,
  ...extra,
});

const templateTarget = (slot, field, value) => ({
  value,
  closest: (sel) =>
    sel.includes("template-row")
      ? { getAttribute: (a) => (a === "data-slot" ? String(slot) : null) }
      : null,
  getAttribute: (a) => (a === "data-field" ? field : null),
});

const bindingTarget = (actor, field, value) => ({
  value,
  closest: (sel) =>
    sel.includes("binding-row") ? { getAttribute: () => actor } : null,
  getAttribute: (a) => (a === "data-field" ? field : null),
});

const checkboxTarget = (actor, checked) => ({
  checked,
  value: checked ? "true" : "false",
  closest: () => null,
  getAttribute: (a) => (a === "data-select-actor" ? actor : null),
});

// ---- HTML / CSS contract -----------------------------------------------------
test("index.html ships every v0.11 control that survives (the right-hand column is gone)", () => {
  for (const id of [
    "btn-open-settings-modal",
    "btn-toggle-board",
    "btn-toggle-channels",
    "board-resizer",
    "channels-resizer",
    "settings-modal",
    "chk-auto-append",
    "endpoint-preview",
    "endpoint-preview-message-url",
    "endpoint-preview-models-url",
    "template-slots",
    "btn-save-templates",
    "btn-select-all-actors",
    "btn-select-none-actors",
    "btn-apply-template-1",
    "btn-apply-template-2",
    "btn-apply-template-3",
    "advanced-bindings",
  ]) {
    assert.ok(HTML_SRC.includes(`id="${id}"`), `missing #${id}`);
  }
  assert.ok(!HTML_SRC.includes("select-card-size"), "the card size picker was removed in v0.12");
  for (const gone of ["btn-toggle-dashboard", "dashboard-resizer", "dashboard-panel", "id=\"dashboard"]) {
    assert.ok(!HTML_SRC.includes(gone), `${gone} was folded into the participant bar in v0.12.1`);
  }
  // scripts load in dependency order (platform.js runs in <head>, before anything else)
  const pf = HTML_SRC.indexOf("static/js/platform.js");
  const c = HTML_SRC.indexOf("static/js/core.js");
  const l = HTML_SRC.indexOf("static/js/layout.js");
  const p = HTML_SRC.indexOf("static/js/providers.js");
  const r = HTML_SRC.indexOf("static/js/roster.js");
  const b = HTML_SRC.indexOf("static/js/budget.js");
  const w = HTML_SRC.indexOf("static/js/archive.js");
  const a = HTML_SRC.indexOf("static/js/app.js");
  assert.ok(pf >= 0 && pf < c && c < l && l < p && p < r && r < b && b < w && w < a);
});

test("style.css defines the collapse/resize/template hooks and the v0.12.1 card / responsive styles (no card tiers, no dashboard column)", () => {
  for (const frag of [
    ".row-resizer",
    ".col-resizer",
    ".board-collapsed",
    ".left-collapsed",
    ".narrow-layout",
    ".pcard",
    ".pcard-num",
    ".pcard-tag",
    ".pbadge",
    ".pcard.out",
    ".avatar-img",
    "image-rendering: pixelated",
    ".board-summary",
    ".channel-item.is-dm",
    ".req-card",
    ".detail-panel",
    ".detail-editor",
    ".budget-bar",
    ".pricing-bar",
    ".price-grid",
    ".tabbar",
    ".more-menu",
    'html[data-layout="phone"]',
    'html[data-layout="compact"]',
    'data-budget="warning"',
    'data-budget="reached"',
    ".endpoint-preview-url",
    ".template-row",
    ".advanced-bindings",
    ".settings-section",
    ".archive-bar-fill",
  ]) {
    assert.ok(CSS_SRC.includes(frag), `missing CSS ${frag}`);
  }
  for (const gone of [
    ".card-size-",
    ".player-card",
    ".action-card",
    ".trace-card",
    ".thought-card",
    ".subitem-",
    ".tile-tag",
    ".glyph-s-",
    ".metrics-popover",
    ".dashboard-panel",
    ".right-collapsed",
    ".exec-depth",
    ".board-legend",
    ".badge-floor",
    ".badge-waiting",
  ]) {
    assert.ok(!CSS_SRC.includes(gone), `obsolete CSS ${gone} is still there`);
  }
});

// ---- layout ------------------------------------------------------------------
test("layout: persisted collapse/size state is applied on create (an old save with a right column is tolerated)", () => {
  const env = makeEnv({
    storage: {
      [Core.LAYOUT_STORAGE_KEY]: JSON.stringify({
        boardCollapsed: true,
        leftCollapsed: true,
        rightCollapsed: true, // v0.12.0 leftovers are ignored
        boardHeight: 200,
        leftWidth: 300,
        rightWidth: 320,
      }),
    },
  });
  assert.ok(env.el("main-container").classList.contains("board-collapsed"));
  assert.equal(env.el("board-section").style.height, ""); // collapsed hides it
  assert.ok(env.el("board-resizer").hidden);
  // "collapsed" channel column = the avatar rail (names hidden), not a hidden column
  assert.ok(env.el("workspace-grid").classList.contains("left-collapsed"));
  assert.ok(!env.el("workspace-grid").classList.contains("right-collapsed"));
  assert.equal(env.el("channels-panel").style.width, "");
  assert.ok(env.el("channels-resizer").hidden);
  assert.equal(env.el("btn-toggle-board-label").textContent, "展开看板");
  assert.equal(env.el("btn-toggle-channels").textContent, "▶");
  assert.equal("rightWidth" in env.layout.state() || "rightCollapsed" in env.layout.state(), false);

  // expand the board: its persisted height comes back
  env.el("btn-toggle-board").dispatch("click");
  assert.ok(!env.el("main-container").classList.contains("board-collapsed"));
  assert.equal(env.el("board-section").style.height, "200px");
  assert.equal(env.el("btn-toggle-board-label").textContent, "折叠看板");
  assert.equal(
    JSON.parse(env.storageMap.get(Core.LAYOUT_STORAGE_KEY)).boardCollapsed,
    false
  );

  // expand the left panel: persisted width comes back
  env.el("btn-toggle-channels").dispatch("click");
  assert.equal(env.el("channels-panel").style.width, "300px");
  assert.ok(!env.el("workspace-grid").classList.contains("left-collapsed"));
});

test("layout: toggles persist to localStorage", () => {
  const env = makeEnv();
  assert.ok(!env.el("main-container").classList.contains("board-collapsed"));
  assert.equal(env.el("channels-panel").style.width, `${Core.LAYOUT_DEFAULTS.leftWidth}px`);

  env.el("btn-toggle-channels").dispatch("click");
  assert.ok(env.el("workspace-grid").classList.contains("left-collapsed"));
  assert.equal(env.el("channels-panel").style.width, "");
  const saved = JSON.parse(env.storageMap.get(Core.LAYOUT_STORAGE_KEY));
  assert.equal(saved.leftCollapsed, true);
  assert.equal("rightCollapsed" in saved, false);
  env.el("btn-toggle-channels").dispatch("click");
  assert.ok(!env.el("workspace-grid").classList.contains("left-collapsed"));
  assert.equal(JSON.parse(env.storageMap.get(Core.LAYOUT_STORAGE_KEY)).leftCollapsed, false);

  // the four card-size tiers are gone: no picker, no class on the grid, nothing persisted
  assert.equal(env.layout.setCardSize, undefined);
  assert.equal("cardSize" in env.layout.state(), false);
  assert.ok(![...env.el("participants-grid")._classes].some((c) => c.startsWith("card-size")));
  assert.equal([...env.storageMap.keys()].some((k) => /card_size/.test(k)), false);
});

test("layout: drag resizers resize within limits and persist; dblclick resets", () => {
  const env = makeEnv();
  // left column: drag 80px to the right
  const w0 = Core.LAYOUT_DEFAULTS.leftWidth;
  env.el("channels-resizer").dispatch("pointerdown", { clientX: 100, button: 0 });
  env.document.dispatchEvent("pointermove", { clientX: 180 });
  env.document.dispatchEvent("pointerup");
  assert.equal(env.el("channels-panel").style.width, `${w0 + 80}px`);
  assert.equal(
    JSON.parse(env.storageMap.get(Core.LAYOUT_STORAGE_KEY)).leftWidth,
    w0 + 80
  );

  // limits: clamping keeps a sane centre column / minimum width
  env.layout.beginResize("left", 0);
  env.layout.moveResize(-10000);
  env.layout.endResize();
  assert.equal(env.layout.state().leftWidth, Core.PANEL_LIMITS.left.min);

  // board: drag vertically (default start = 150px when nothing is measured)
  env.layout.beginResize("board", 300);
  env.layout.moveResize(420);
  env.layout.endResize();
  const boardHeight = env.layout.state().boardHeight;
  assert.ok(boardHeight >= Core.PANEL_LIMITS.board.min);
  assert.equal(env.el("board-section").style.height, `${boardHeight}px`);

  // double-click restores the defaults
  env.el("channels-resizer").dispatch("dblclick");
  assert.equal(env.layout.state().leftWidth, Core.LAYOUT_DEFAULTS.leftWidth);
  env.el("board-resizer").dispatch("dblclick");
  assert.equal(env.layout.state().boardHeight, null);
  assert.equal(env.el("board-section").style.height, "");
  assert.equal(JSON.parse(env.storageMap.get(Core.LAYOUT_STORAGE_KEY)).boardHeight, null);
});

test("layout: narrow viewports turn the channel column into the avatar rail automatically", () => {
  const env = makeEnv({ innerWidth: 900 });
  env.layout.reflowLimits();
  assert.ok(env.el("workspace-grid").classList.contains("narrow-layout"));
  env.sandbox.innerWidth = 1400;
  env.layout.reflowLimits();
  assert.ok(!env.el("workspace-grid").classList.contains("narrow-layout"));
  assert.equal(env.layout.isNarrow(), false);
});

test("layout: the platform's <html data-layout> decides (a touch tablet at 1024 px is compact, a wide window is not)", () => {
  const env = makeEnv({ innerWidth: 1024 });
  const html = env.document.documentElement;
  html.setAttribute("data-layout", "compact");
  env.layout.apply();
  assert.ok(env.el("workspace-grid").classList.contains("narrow-layout"), "compact -> rail even at 1024 px");
  assert.equal(env.layout.isNarrow(), true);
  html.setAttribute("data-layout", "wide");
  env.layout.apply();
  assert.ok(!env.el("workspace-grid").classList.contains("narrow-layout"));
  // the phone layout has its own panes (tab bar): the rail class is not used there
  html.setAttribute("data-layout", "phone");
  env.layout.apply();
  assert.ok(!env.el("workspace-grid").classList.contains("narrow-layout"));
});

// ---- provider: auto_append_path + endpoint preview ---------------------------
test("provider: endpoint preview is debounced and renders the final URLs", async () => {
  const env = makeEnv();
  await env.providerUI.load();
  await openProviderModal(env);
  assert.equal(env.backend.previews.length, 1);
  assert.deepEqual(env.backend.previews[0], {
    protocol: "openai_chat",
    base_url: "https://api.example.com",
    auto_append_path: true,
  });
  assert.equal(
    env.el("endpoint-preview-message-url").textContent,
    "https://api.example.com/v1/chat/completions"
  );
  assert.equal(
    env.el("endpoint-preview-models-url").textContent,
    "https://api.example.com/v1/models"
  );
  assert.equal(env.el("endpoint-preview-warnings").hidden, false);
  assert.match(env.el("endpoint-preview-warnings").innerHTML, /示例警告/);

  // two quick edits -> exactly one request (debounce cancels the first timer)
  const before = env.backend.previews.length;
  env.el("input-provider-base-url").value = "https://api.example.com/v1";
  env.el("input-provider-base-url").dispatch("input");
  env.el("input-provider-base-url").value = "https://api.example.com/v2";
  env.el("input-provider-base-url").dispatch("input");
  assert.equal(env.timers.length, 1);
  env.flushTimers();
  await settle();
  assert.equal(env.backend.previews.length, before + 1);
  assert.equal(env.backend.previews[before].base_url, "https://api.example.com/v2");

  // an invalid Base URL is reported inline without a request
  const callsBefore = env.backend.previews.length;
  env.el("input-provider-base-url").value = "not a url";
  env.el("input-provider-base-url").dispatch("input");
  env.flushTimers();
  await settle();
  assert.equal(env.backend.previews.length, callsBefore);
  assert.equal(env.el("endpoint-preview-status").className.includes("error"), true);
  assert.match(env.el("endpoint-preview-status").textContent, /URL/);
});

test("provider: auto_append_path off disables model discovery and explains manual IDs", async () => {
  const env = makeEnv();
  await env.providerUI.load();
  await openProviderModal(env);

  env.el("chk-auto-append").checked = false;
  env.el("chk-auto-append").dispatch("change");
  assert.equal(env.el("btn-discover-models").disabled, true);
  assert.match(env.el("discover-status").textContent, /手动填写模型 ID/);
  assert.match(env.el("auto-append-hint").textContent, /完整消息 endpoint/);
  assert.equal(env.el("input-model-id").disabled, false); // manual entry stays available
  assert.match(HTML_SRC, /模型 ID（从上方单选，或手动输入）/);

  const discoverBefore = env.backend.discoverCalls;
  env.el("btn-discover-models").dispatch("click");
  await settle();
  assert.equal(env.backend.discoverCalls, discoverBefore);
  assert.ok(env.toasts.some((t) => /手动填写模型 ID/.test(t)));

  env.flushTimers();
  await settle();
  const lastPreview = env.backend.previews[env.backend.previews.length - 1];
  assert.equal(lastPreview.auto_append_path, false);
  assert.match(env.el("endpoint-preview-models-url").textContent, /手动填写模型 ID/);

  // turning it back on re-enables discovery
  env.el("chk-auto-append").checked = true;
  env.el("chk-auto-append").dispatch("change");
  assert.equal(env.el("btn-discover-models").disabled, false);
});

test("provider: changing auto_append_path requires re-entering the API key", async () => {
  const env = makeEnv();
  await env.providerUI.load();
  await openProviderModal(env);

  env.el("chk-auto-append").checked = false;
  env.el("chk-auto-append").dispatch("change");
  env.el("input-provider-key").value = "";
  env.el("provider-form").dispatch("submit");
  await settle();
  assert.equal(env.el("provider-form-error").hidden, false);
  assert.match(env.el("provider-form-error").textContent, /必须重新输入 API Key/);
  assert.equal(env.count("PATCH", "/api/providers/p1"), 0);

  env.el("input-provider-key").value = "sk-new";
  env.el("provider-form").dispatch("submit");
  await settle(40);
  assert.equal(env.count("PATCH", "/api/providers/p1"), 1);
  assert.deepEqual(env.backend.patchBody, {
    auto_append_path: false,
    api_key: "sk-new",
  });
  // the saved provider re-renders with auto-append off
  assert.equal(env.el("chk-auto-append").checked, false);
  assert.ok(env.toasts.some((t) => /Provider 已保存/.test(t)));
});

// ---- templates ---------------------------------------------------------------
test("templates: GET fills 3 editable slots; edits save through PUT", async () => {
  const env = makeEnv();
  await env.providerUI.load();
  await openBindingsModal(env);
  const html = env.el("template-slots").innerHTML;
  assert.equal((html.match(/class="template-row"/g) || []).length, 3);
  assert.match(html, /模板1/);
  assert.match(html, /旗舰/);
  assert.match(html, /已保存/);
  assert.match(html, /未保存/);
  assert.match(env.el("templates-lock-hint").textContent, /保存模板/);

  // choose provider + model for slot 2 through the editable controls
  env.el("template-slots").dispatch("change", {
    target: templateTarget(2, "provider_id", "p2"),
  });
  env.el("template-slots").dispatch("change", {
    target: templateTarget(2, "model_id", "claude-y"),
  });
  env.el("template-slots").dispatch("change", {
    target: templateTarget(2, "reasoning_depth", "low"),
  });
  env.el("btn-save-templates").dispatch("click");
  await settle(40);
  assert.equal(env.count("PUT", "/api/settings/templates"), 1);
  const put = env.backend.calls.find(
    (c) => c.method === "PUT" && c.url.endsWith("/api/settings/templates")
  );
  assert.deepEqual(put.body.templates, [
    {
      slot: 1,
      name: "旗舰",
      provider_id: "p1",
      model_id: "ds-chat",
      reasoning_depth: "xhigh",
    },
    {
      slot: 2,
      name: "模板2",
      provider_id: "p2",
      model_id: "claude-y",
      reasoning_depth: "low",
    },
  ]);
  assert.equal(env.el("templates-error").hidden, true);
  assert.ok(env.toasts.some((t) => /模型模板已保存/.test(t)));
});

test("templates: partial slot is rejected locally (no PUT)", async () => {
  const env = makeEnv();
  await env.providerUI.load();
  await openBindingsModal(env);
  env.providerUI._state.templateRows[2].provider_id = "p1";
  env.providerUI._state.templateRows[2].model_id = "";
  env.el("btn-save-templates").dispatch("click");
  await settle();
  assert.equal(env.count("PUT", "/api/settings/templates"), 0);
  assert.equal(env.el("templates-error").hidden, false);
  assert.match(env.el("templates-error").textContent, /模板3：请同时选择 Provider 和模型/);
});

test("templates: checkboxes + select all/none + apply sends actor_ids", async () => {
  const env = makeEnv();
  await env.providerUI.load();
  await openBindingsModal(env);
  const rows = env.providerUI._state.rows;
  assert.equal(rows.length, 3); // moderator + 2 players
  assert.ok(rows.every((r) => r.selected === false));

  env.el("btn-select-all-actors").dispatch("click");
  assert.ok(env.providerUI._state.rows.every((r) => r.selected === true));
  assert.match(env.el("template-apply-hint").textContent, /已勾选 3 个角色/);

  env.el("btn-apply-template-1").dispatch("click");
  await settle(40);
  assert.deepEqual(env.backend.lastApply, {
    slot: 1,
    body: { actor_ids: ["moderator", "p01", "p02"] },
  });
  assert.equal(env.providerUI._state.rows[1].provider_id, "p1");
  assert.equal(env.providerUI._state.rows[1].model_id, "ds-chat");
  assert.ok(env.toasts.some((t) => /已把模板1应用到 3 个角色/.test(t)));

  // uncheck all -> apply is refused locally
  env.el("btn-select-none-actors").dispatch("click");
  assert.ok(env.providerUI._state.rows.every((r) => r.selected === false));
  env.el("btn-apply-template-1").dispatch("click");
  await settle();
  assert.ok(env.toasts.some((t) => /请先勾选/.test(t)));
  assert.equal(env.backend.lastApply.slot, 1);
  assert.equal(env.count("POST", "/api/settings/templates/1/apply"), 1);
});

test("templates: individual checkboxes are tracked without re-rendering selection", async () => {
  const env = makeEnv();
  await env.providerUI.load();
  await openBindingsModal(env);
  env.el("bindings-table").dispatch("change", { target: checkboxTarget("p01", true) });
  env.el("bindings-table").dispatch("change", { target: checkboxTarget("p02", true) });
  env.el("btn-apply-template-1").dispatch("click");
  await settle(30);
  assert.deepEqual(env.backend.lastApply.body, { actor_ids: ["p01", "p02"] });
});

test("templates: apply disabled while running; editing/saving stays allowed", async () => {
  const env = makeEnv({ session: { status: "running" } });
  await env.providerUI.load();
  await openBindingsModal(env);
  assert.equal(env.el("btn-apply-template-1").disabled, true);
  assert.equal(env.el("btn-apply-template-2").disabled, true);
  assert.equal(env.el("btn-save-templates").disabled, false);
  assert.match(env.el("templates-lock-hint").textContent, /不能应用/);
  assert.match(env.el("template-apply-hint").textContent, /暂停/);

  env.el("btn-select-all-actors").dispatch("click");
  env.el("btn-apply-template-1").dispatch("click");
  await settle();
  assert.equal(env.count("POST", "/api/settings/templates/1/apply"), 0);
  assert.ok(env.toasts.some((t) => /不能应用模板/.test(t)));

  // editing remains possible: save PUT is not blocked by the running game
  env.el("template-slots").dispatch("change", {
    target: templateTarget(1, "name", "运行中改名"),
  });
  env.el("btn-save-templates").dispatch("click");
  await settle(30);
  assert.equal(env.count("PUT", "/api/settings/templates"), 1);
});

test("templates: backend error (e.g. stale reference) is shown without breaking the modal", async () => {
  const env = makeEnv();
  await env.providerUI.load();
  await openBindingsModal(env);
  env.backend.failures["PUT /api/settings/templates"] = [
    400,
    "模板1引用的 Provider 或模型不存在",
  ];
  env.el("btn-save-templates").dispatch("click");
  await settle(30);
  assert.equal(env.el("templates-error").hidden, false);
  assert.match(env.el("templates-error").textContent, /引用的 Provider 或模型不存在/);
  assert.equal(env.el("bindings-modal").style.display, "flex"); // stays open for correction
});

test("templates: advanced manual bindings remain a folded section with the old controls", async () => {
  const env = makeEnv();
  await env.providerUI.load();
  await openBindingsModal(env);
  assert.ok(HTML_SRC.includes("<details class=\"advanced-bindings\""));
  assert.match(HTML_SRC, /高级手动绑定/);
  const html = env.el("bindings-table").innerHTML;
  assert.equal((html.match(/class="binding-row/g) || []).length, 3);
  assert.match(html, /data-field="provider_id"/);
  // the old save flow still works from inside the folded section
  env.el("bindings-table").dispatch("change", {
    target: bindingTarget("p01", "provider_id", "p2"),
  });
  env.el("bindings-table").dispatch("change", {
    target: bindingTarget("p01", "model_id", "claude-y"),
  });
  env.el("btn-save-bindings").dispatch("click");
  await settle(30);
  const put = env.backend.calls.find(
    (c) => c.method === "PUT" && c.url.endsWith("/api/settings/bindings")
  );
  assert.equal(put.body.bindings.p01.model_id, "claude-y");
});

// ---- runner ------------------------------------------------------------------
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
