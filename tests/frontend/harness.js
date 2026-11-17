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
const Platform = require(jsPath("platform.js"));
const Store = require(jsPath("store.js"));
const View = require(jsPath("view.js"));
const F = require("./fixtures.js");

const read = (name) => fs.readFileSync(jsPath(name), "utf8");
const SRC = {
  app: read("app.js"),
  providers: read("providers.js"),
  layout: read("layout.js"),
  workspace: read("workspace.js"),
  roster: read("roster.js"),
  budget: read("budget.js"),
  platform: read("platform.js"),
  avatar: read("avatar.js"),
  core: read("core.js"),
  view: read("view.js"),
  store: read("store.js"),
};
// elements that carry the `hidden` attribute in index.html start hidden (the fake DOM creates elements on demand)
const HIDDEN_IDS = new Set();
(() => {
  const html = fs.readFileSync(path.join(__dirname, "..", "..", "static", "index.html"), "utf8");
  for (const m of html.matchAll(/<[a-zA-Z][^>]*>/g)) {
    const id = (m[0].match(/\bid="([^"]+)"/) || [])[1];
    if (id && /\shidden(?=[\s>\/=])/.test(m[0])) HIDDEN_IDS.add(id);
  }
})();
const PARTS_MIN = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "parts.min.json"), "utf8"));
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
  querySelectorAll() {
    return [];
  }
  closest() {
    return null;
  }
  scrollIntoView() {}
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

function resp(status, body, ct = "application/json", extraHeaders = {}) {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: {
      get: (k) => (k.toLowerCase() === "content-type" ? ct : extraHeaders[k.toLowerCase()] !== undefined ? extraHeaders[k.toLowerCase()] : null),
    },
    text: async () => text,
    json: async () => JSON.parse(text),
    blob: async () => new Blob([text], { type: ct }),
  };
}

function makeEnv({
  fetchImpl,
  storedToken = "",
  storage: initialStorage = {},
  innerWidth = 1280,
  innerHeight = 800,
  userAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130 Safari/537.36",
  coarse = false,
  maxTouchPoints = 0,
  search = "",
} = {}) {
  const els = new Map();
  const timers = [];
  let timerId = 0;
  const rafQueue = [];
  const toasts = [];
  const esInstances = [];
  const reloads = [];
  const downloads = [];
  const storage = new Map(Object.entries(initialStorage));
  if (storedToken) storage.set("arena_api_token", storedToken);

  const docListeners = {};
  const winListeners = {};
  const document = {
    getElementById(id) {
      if (!els.has(id)) {
        const el = new FakeEl("div", id);
        if (HIDDEN_IDS.has(id)) el.hidden = true;
        els.set(id, el);
      }
      return els.get(id);
    },
    createElement(tag) {
      const el = new FakeEl(tag);
      if (tag === "a") el.click = () => downloads.push({ href: el.href, download: el.download });
      return el;
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
    navigator: { userAgent, maxTouchPoints, platform: "", userAgentData: null },
    matchMedia: (q) => ({ matches: /pointer:\s*coarse/.test(q) ? Boolean(coarse) : false, addEventListener() {} }),
    location: {
      search,
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
    ArenaPlatform: Platform,
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
    downloads,
    document,
    el: (id) => document.getElementById(id),
    run: () => {
      vm.runInContext(SRC.layout, sandbox, { filename: "layout.js" });
      vm.runInContext(SRC.providers, sandbox, { filename: "providers.js" });
      vm.runInContext(SRC.roster, sandbox, { filename: "roster.js" });
      vm.runInContext(SRC.budget, sandbox, { filename: "budget.js" });
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
        models: [
          {
            id: "model-x",
            input_price_per_m: 0.5,
            cache_read_price_per_m: 0.1,
            cache_write_price_per_m: null,
            output_price_per_m: 2,
            context_window_tokens: 128000,
            price_source: "litellm:deepseek/model-x",
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
        models: [
          {
            id: "claude-y",
            input_price_per_m: 3,
            cache_read_price_per_m: null,
            cache_write_price_per_m: null,
            output_price_per_m: 15,
            context_window_tokens: 200000,
            price_source: "manual",
          },
        ],
      },
      { id: "env", name: "Env", protocol: "openai_chat", base_url: "https://e.com", api_key_set: true, read_only: true, models: [] },
    ],
    bindings: session.model_bindings,
    sessions: [
      { session_id: "s-test", current: true, status: "idle", record_count: 0 },
      { session_id: "old-1", current: false, status: "paused", record_count: 7 },
    ],
    discovered: ["model-x", "m-new", "other/slash"],
    suggestions: {}, // model id -> a `suggested` object (null / missing = no match)
    pricing: { url: Core.DEFAULT_PRICING_SOURCE_URL, default_url: Core.DEFAULT_PRICING_SOURCE_URL, fetched_at: "2026-11-17T08:00:00Z", entries: 2345, error: null },
    budget: { max_tokens: null, max_cost_usd: null, force_stop: false, used_tokens: 0, used_cost_usd: 0, state: "ok" },
    roster: {
      memory_enabled: true,
      editable: true,
      seats: F.PLAYER_IDS.map((id, i) => ({
        seat: i + 1,
        id,
        name: F.NAMES[i],
        default_name: F.NAMES[i],
        avatar: F.avatarOf(i + 1),
        persona: "",
        memory: "",
      })),
      moderator: { id: "moderator", name: "主持人", avatar: F.avatarOf(13) },
    },
    importChanges: [{ id: "N3", persona: true, memory: true }, { id: "N5", persona: false, memory: true }],
    refineDraft: "【草稿】这是主持人模型完善后的设定。",
    reviewStarted: ["N1"],
    workspaces: {
      enabled: true,
      current: { id: "w1", name: "Av11WS", path: "D:\\Av11\\WS", status: "ok" },
      recent: [{ id: "w1", name: "Av11WS", path: "D:\\Av11\\WS" }],
      default_path: "D:\\Arena-Workspace\\Default",
      native_picker: true,
    },
    parts: PARTS_MIN, // the sprite library served at static/avatar/parts.json (null = 404)
    requireToken: "",
    failures: {},
    controlSession: null, // what POST /api/control/* answers with (defaults to the current session)
    ...overrides,
    session,
  };
  b.fetch = async (url, init0) => {
    const init = { method: "GET", ...(init0 || {}), headers: { ...((init0 && init0.headers) || {}) } };
    const isJson = !init.headers["Content-Type"] || /json/.test(init.headers["Content-Type"]);
    const body = init.body ? (isJson ? JSON.parse(init.body) : init.body) : undefined;
    const call = { url, method: init.method, body, headers: init.headers };
    b.calls.push(call);
    const key = init.method + " " + url.replace(/^\/arena/, "");
    // static files are public (no token); the sprite library is one of them (null = "not generated yet": 404)
    if (key === "GET /static/avatar/parts.json") {
      return b.parts ? resp(200, b.parts) : resp(404, "not found", "text/plain");
    }
    if (b.requireToken && init.headers["X-Arena-Token"] !== b.requireToken) {
      return resp(401, { detail: "Invalid or missing API token" });
    }
    if (b.failures[key]) return resp(b.failures[key][0], { detail: b.failures[key][1] });
    const find = (id) => b.providers.find((p) => p.id === id);
    let m;
    if (key === "GET /api/state") return resp(200, { app_version: "v0.12.1-test", session: b.session });
    if (/^POST \/api\/control\/(new|start|pause|force-pause)$/.test(key)) {
      return resp(200, { ok: true, session: b.controlSession || b.session });
    }
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
      const models = b.discoveredShape === "map"
        ? b.discovered
        : b.discovered.map((id) => ({ id, suggested: b.suggestions[id] || null }));
      const out = { models, existing_models: find(m[1]).models.map((x) => x.id) };
      if (b.discoveredShape === "map") out.suggested = b.suggestions;
      return resp(200, out);
    }
    if ((m = key.match(/^(POST|PATCH|DELETE) \/api\/providers\/([^/]+)\/models$/))) {
      const p = find(m[2]);
      if (m[1] === "POST") {
        const e = {
          id: body.model_id,
          input_price_per_m: body.input_price_per_m,
          cache_read_price_per_m: body.cache_read_price_per_m ?? null,
          cache_write_price_per_m: body.cache_write_price_per_m ?? null,
          output_price_per_m: body.output_price_per_m,
          context_window_tokens: body.context_window_tokens ?? 1000000,
          price_source: "manual",
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
    // ---- v0.12.1: price source, budget, roster, review
    if (key === "GET /api/pricing/source") return resp(200, b.pricing);
    if (key === "PUT /api/pricing/source") {
      b.pricing = { ...b.pricing, url: body.url, error: null };
      return resp(200, b.pricing);
    }
    if (key === "POST /api/pricing/refresh") {
      b.pricing = b.refreshError
        ? { ...b.pricing, error: b.refreshError }
        : { ...b.pricing, entries: 2400, fetched_at: "2026-11-17T09:30:00Z", error: null };
      return resp(200, b.pricing);
    }
    if ((m = key.match(/^GET \/api\/pricing\/match\?provider_id=([^&]*)&model_id=(.*)$/))) {
      const hit = b.suggestions[decodeURIComponent(m[2])];
      return resp(200, hit ? { matched: true, key: "litellm/x", ...hit } : { matched: false, key: null });
    }
    if (key === "GET /api/budget") return resp(200, b.budget);
    if (key === "PUT /api/budget") {
      b.budget = { ...b.budget, ...body };
      b.budget.state = Core.budgetStateOf(b.budget); // like the server: the state follows the new limits
      return resp(200, b.budget);
    }
    if (key === "GET /api/roster") return resp(200, b.roster);
    if (key === "PUT /api/roster/memory-enabled") {
      b.roster.memory_enabled = Boolean(body.enabled);
      return resp(200, { enabled: b.roster.memory_enabled });
    }
    if (key === "GET /api/roster/memory:export") {
      return resp(200, "ZIPDATA", "application/zip", { "content-disposition": 'attachment; filename="arena-memory-test.zip"' });
    }
    if ((m = key.match(/^POST \/api\/roster\/memory:import\?dry_run=(true|false)$/))) {
      b.importCalls = (b.importCalls || []).concat([{ dry_run: m[1] === "true", body, contentType: init.headers["Content-Type"] }]);
      if (b.importError) return resp(400, { detail: b.importError });
      return resp(200, { dry_run: m[1] === "true", changes: b.importChanges });
    }
    if (key === "POST /api/roster/memory:clear") {
      b.lastClear = body;
      for (const seat of b.roster.seats) {
        seat.memory = "";
        if (body && body.include_persona) seat.persona = "";
      }
      return resp(200, { ok: true });
    }
    if (key === "POST /api/roster/avatars:regenerate") {
      if (b.session.status !== "idle") return resp(409, { detail: "对局已开始，不能重新生成头像" });
      b.roster.seats.forEach((seat, i) => (seat.avatar = F.avatarOf(i + 20)));
      b.roster.moderator.avatar = F.avatarOf(40); // "all" includes the moderator
      return resp(200, { seats: b.roster.seats, moderator: b.roster.moderator });
    }
    if (key === "POST /api/roster/moderator/avatar:regenerate") {
      if (b.session.status !== "idle") return resp(409, { detail: "对局已开始，不能重新生成头像" });
      b.roster.moderator.avatar = F.avatarOf(41 + (b.moderatorRolls = (b.moderatorRolls || 0) + 1));
      return resp(200, b.roster.moderator);
    }
    if ((m = key.match(/^POST \/api\/roster\/([^/]+)\/avatar:regenerate$/))) {
      const seat = b.roster.seats.find((x) => x.id === m[1]);
      seat.avatar = F.avatarOf(30 + b.roster.seats.indexOf(seat));
      return resp(200, seat);
    }
    if ((m = key.match(/^POST \/api\/roster\/([^/]+)\/refine$/))) {
      b.lastRefine = { id: m[1], ...body };
      if (b.refineError) return resp(502, { detail: b.refineError });
      return resp(200, { draft: b.refineDraft });
    }
    if ((m = key.match(/^PUT \/api\/roster\/([^/]+)$/))) {
      const seat = b.roster.seats.find((x) => x.id === m[1]);
      if (!seat) return resp(404, { detail: "no such seat" });
      b.rosterPuts = (b.rosterPuts || []).concat([{ id: m[1], ...body }]);
      if (("name" in body || "avatar" in body) && b.session.status !== "idle") return resp(409, { detail: "对局已开始，不能修改名字或头像" });
      if ("name" in body) {
        if (b.roster.seats.some((x) => x.id !== m[1] && x.name === body.name)) return resp(422, { detail: `名字「${body.name}」已被使用` });
        seat.name = body.name;
      }
      for (const k of ["persona", "memory", "avatar"]) if (k in body) seat[k] = body[k];
      return resp(200, seat);
    }
    if (key === "POST /api/game/review") {
      b.lastReview = body;
      if (b.reviewError) return resp(409, { detail: b.reviewError });
      return resp(200, { started: b.reviewStarted });
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
    const norm = { method: "GET", ...(init || {}), headers: { ...((init && init.headers) || {}) } };
    const r = await b.fetch(url, norm);
    calls.push({ url, init: norm });
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
  Platform,
  PARTS_MIN,
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
