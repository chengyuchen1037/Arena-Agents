"use strict";
// Shared test harness (not a test): runs static/js/app.js (+ layout / providers / archive) under node
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
  archive: read("archive.js"),
  roster: read("roster.js"),
  budget: read("budget.js"),
  marks: read("marks.js"), // v0.13.1: the colour marks on the players
  thoughts: read("thoughts.js"), // v0.13.1: the thinking record window
  calllog: read("calllog.js"), // v0.13.1: the error log window + the badge
  takeover: read("takeover.js"),
  composer: read("composer.js"),
  history: read("history.js"),
  replay: read("replay.js"),
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

/** A fake XMLHttpRequest: records the request; tests (or `hooks.auto`) drive progress / the answer. */
function makeXhrClass(list, hooks) {
  return class FakeXhr {
    constructor() {
      this.headers = {};
      this.status = 0;
      this.responseText = "";
      this.upload = { onprogress: null, onload: null };
      this.aborted = false;
      this.sent = false;
      list.push(this);
    }

    open(method, url) {
      this.method = method;
      this.url = url;
    }

    setRequestHeader(name, value) {
      this.headers[name] = value;
    }

    send(body) {
      this.body = body;
      this.sent = true;
      if (hooks.auto) Promise.resolve().then(() => hooks.auto(this));
    }

    abort() {
      this.aborted = true;
      if (this.onabort) this.onabort({});
    }

    /** the browser's upload progress event */
    progress(loaded, total) {
      if (this.upload.onprogress) this.upload.onprogress({ loaded, total, lengthComputable: true });
    }

    /** the request body has been sent completely (the server is now checking the file) */
    uploaded() {
      if (this.upload.onload) this.upload.onload({});
    }

    /** the server's answer */
    respond(status, body) {
      this.status = status;
      this.responseText = typeof body === "string" ? body : JSON.stringify(body);
      if (this.onload) this.onload({});
    }

    /** a network failure */
    fail() {
      if (this.onerror) this.onerror({});
    }
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
  const clipboard = []; // v0.13.1: what the page copied (navigator.clipboard.writeText)
  const esInstances = [];
  const reloads = [];
  const downloads = [];
  const xhrs = [];
  const xhrHooks = { auto: null };
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
      // (v0.13.1: stopImmediatePropagation keeps the listeners registered after the one that called it from running)
      let stopped = false;
      const own = evt.stopImmediatePropagation;
      const e = { type: t, target: this, preventDefault() {}, ...evt };
      e.stopImmediatePropagation = () => {
        stopped = true;
        if (typeof own === "function") own();
      };
      for (const fn of (docListeners[t] || []).slice()) {
        if (stopped) break;
        fn(e);
      }
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
    XMLHttpRequest: makeXhrClass(xhrs, xhrHooks),
    navigator: {
      userAgent,
      maxTouchPoints,
      platform: "",
      userAgentData: null,
      clipboard: { writeText: async (text) => void clipboard.push(String(text)) },
    },
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
    clipboard,
    reloads,
    downloads,
    xhrs,
    xhrHooks,
    document,
    el: (id) => document.getElementById(id),
    run: () => {
      vm.runInContext(SRC.layout, sandbox, { filename: "layout.js" });
      vm.runInContext(SRC.providers, sandbox, { filename: "providers.js" });
      vm.runInContext(SRC.roster, sandbox, { filename: "roster.js" });
      vm.runInContext(SRC.budget, sandbox, { filename: "budget.js" });
      vm.runInContext(SRC.archive, sandbox, { filename: "archive.js" });
      vm.runInContext(SRC.marks, sandbox, { filename: "marks.js" });
      vm.runInContext(SRC.thoughts, sandbox, { filename: "thoughts.js" });
      vm.runInContext(SRC.calllog, sandbox, { filename: "calllog.js" });
      vm.runInContext(SRC.takeover, sandbox, { filename: "takeover.js" });
      vm.runInContext(SRC.composer, sandbox, { filename: "composer.js" });
      vm.runInContext(SRC.history, sandbox, { filename: "history.js" });
      vm.runInContext(SRC.replay, sandbox, { filename: "replay.js" });
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

/** The `takeover` of a session in which `seat` is played and nothing else is going on yet (F.takeover with defaults). */
function takeoverOf(session, seat, over = {}) {
  return F.makeTakeover(seat, { reshuffle_available: session.status === "idle", ...over });
}

// ---- v0.13.1: the memory of a seat as the server keeps it (contract §12.5): the impressions of the OTHER seats + lessons ----
const LIMITS = { impression_chars: 80, experience_items: 3, experience_chars: 300, memory_chars: 1500, legacy_chars: 600, persona_chars: 500, persona_item_chars: 40 };
const emptyParts = (id) => ({
  record: "",
  impressions: Object.fromEntries(F.PLAYER_IDS.filter((p) => p !== id).map((p) => [p, ""])),
  experience: [],
  legacy: "",
});

/** The canonical memory text of a seat (the server renders it; GET /api/roster sends it as `memory`, ``""`` when there is nothing). */
function memoryTextOf(seat, seats) {
  const p = seat.memory_parts;
  if (!(p.record.trim() || Object.values(p.impressions).some(Boolean) || p.experience.length || p.legacy.trim())) return "";
  const name = (id) => (seats.find((s) => s.id === id) || {}).name || id;
  const blocks = [];
  if (p.record.trim()) blocks.push(`## 战绩\n${p.record.trim()}`);
  const lines = Object.keys(p.impressions).map((id) => `- ${id} ${name(id)}：${p.impressions[id] || "（暂无）"}`);
  blocks.push(`## 印象\n${lines.join("\n")}\n\n## 经验${p.experience.length ? `\n${p.experience.map((e) => `- ${e}`).join("\n")}` : ""}`);
  if (p.legacy.trim()) blocks.push(`## 旧记忆（待整理）\n${p.legacy.trim()}`);
  return blocks.join("\n\n");
}

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
    // v0.13 (history.js): the rows of GET /api/games (the live game is marked `current` by the route) and, per id, the
    // answer of GET /api/games/{id}/replay
    games: [
      { session_id: "s-test", title: "Arena-Agents", scenario_title: "狼人杀 12 人局", status: "idle", winner: null, created_at: "2026-11-18T08:00:00Z", updated_at: "2026-11-18T08:00:00Z", duration_gt: 0, players: 12, tokens: 0, cost_usd: 0, human_involved: false, schema_version: 5, size_bytes: 1200 },
      { session_id: "old-1", title: "Arena-Agents", scenario_title: "狼人杀 12 人局", status: "finished", winner: "好人阵营", created_at: "2026-11-17T09:00:00Z", updated_at: "2026-11-17T10:00:00Z", duration_gt: 1523, players: 12, tokens: 1250000, cost_usd: 0.5, human_involved: false, schema_version: 5, size_bytes: 91000 },
      { session_id: "old-2", title: "Arena-Agents", scenario_title: "狼人杀 12 人局", status: "paused", winner: null, created_at: "2026-11-16T09:00:00Z", updated_at: "2026-11-16T09:40:00Z", duration_gt: 600, players: 12, tokens: 3200, cost_usd: 0.01, human_involved: true, schema_version: 5, size_bytes: 40000 },
    ],
    replays: {},
    discovered: ["model-x", "m-new", "other/slash"],
    suggestions: {}, // model id -> a `suggested` object (null / missing = no match)
    pricing: { url: Core.DEFAULT_PRICING_SOURCE_URL, default_url: Core.DEFAULT_PRICING_SOURCE_URL, fetched_at: "2026-11-17T08:00:00Z", entries: 2345, error: null },
    budget: { max_tokens: null, max_cost_usd: null, force_stop: false, used_tokens: 0, used_cost_usd: 0, state: "ok" },
    roster: {
      memory_enabled: true,
      editable: true,
      limits: { ...LIMITS }, // v0.13.1: the limits of persona and memory (contract §12.5)
      seats: F.PLAYER_IDS.map((id, i) => ({
        seat: i + 1,
        id,
        name: F.NAMES[i],
        default_name: F.NAMES[i],
        avatar: F.avatarOf(i + 1),
        persona: "",
        memory: "",
        memory_parts: emptyParts(id),
      })),
      moderator: { id: "moderator", name: "主持人", avatar: F.avatarOf(13) },
    },
    refineDraft: "【草稿】这是主持人模型完善后的设定。",
    // v0.13.1: the memory draft `refine` answers with (`null`: the fake merges the notes into what the seat remembers as one lesson)
    refineMemory: null,
    // v0.13.1: the error log (GET /api/llm/calls, contract §12.3): CallEntry lines, oldest first (ids count up from 1);
    // `callsFailure` = [status, detail] makes the endpoint fail
    llmCalls: [],
    callsFailure: null,
    // v0.13.1: the thinking record (GET /api/agents/{id}/thoughts, §12.4): agent id -> ThoughtItem, oldest first;
    // `thoughtsFailure` = [status, detail] makes the endpoint fail
    thoughts: {},
    thoughtsFailure: null,
    reviewStarted: ["N1"],
    // v0.13 (archive.js, contract section 11.9): /api/state.legacy_data, GET /api/archive/status, the import summary
    legacyData: { found: false, dismissed: false },
    archiveStatus: { busy: false, job: null, import: null, game_active: false },
    importSummary: {
      archive_id: "0123456789abcdef0123456789abcdef",
      format_version: 1,
      app_version: "v0.13.1",
      created_at: "2026-11-18T00:42:07-08:00",
      size_bytes: 123456,
      uncompressed_bytes: 654321,
      file_count: 20,
      games: 3,
      providers: 2,
      roster_seats: 12,
      has_secrets: false,
      has_logs: true,
      provider_names: ["DeepSeek", "Claude"],
      game_list: [],
      games_listed: 0,
      warnings: [],
    },
    archiveJobs: {}, // job id -> what GET /api/archive/jobs/{id} answers
    parts: PARTS_MIN, // the sprite library served at static/avatar/parts.json (null = 404)
    requireToken: "",
    failures: {},
    controlSession: null, // what POST /api/control/* answers with (defaults to the current session)
    ...overrides,
    session,
  };
  /** The default answer to an archive upload (POST /api/archive/import by XHR): progress, "uploaded", then the reply. */
  b.uploadHandler = (xhr) => {
    const size = Number((xhr.body && xhr.body.size) || 0);
    const name = decodeURIComponent((String(xhr.url).match(/[?&]name=([^&]*)/) || [])[1] || "");
    b.uploads = (b.uploads || []).concat([{ url: xhr.url, name, size, headers: { ...xhr.headers } }]);
    xhr.progress(Math.floor(size / 2), size);
    xhr.progress(size, size);
    xhr.uploaded();
    const reply = typeof b.uploadReply === "function" ? b.uploadReply(xhr, size, name) : b.uploadReply;
    if (reply) return xhr.respond(reply[0], reply[1]);
    return xhr.respond(200, { import_id: "imp-1", name, size_bytes: size, summary: b.importSummary, expires_in: 7200 });
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
    if (key === "GET /api/state") return resp(200, { app_version: "v0.13.1-test", legacy_data: b.legacyData, session: b.session });
    // (v0.13.1: soft-pause / soft-resume answer like the other controls - a test sets `controlSession` to what the
    // server would answer with, e.g. the session with `soft_pause: {state: "draining", in_flight: 2}`)
    if (/^POST \/api\/control\/(new|start|pause|force-pause|soft-pause|soft-resume)$/.test(key)) {
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
    // ---- v0.13: archive export / import (archive.js; contract section 11.5 / 11.9)
    if (key === "GET /api/archive/status") return resp(200, b.archiveStatus);
    if (key === "POST /api/archive/export") {
      b.exportCalls = (b.exportCalls || []).concat([body]);
      if (b.exportReply) return resp(b.exportReply[0], b.exportReply[1]);
      return resp(202, { job_id: "job-export-1", kind: "export" });
    }
    if ((m = key.match(/^GET \/api\/archive\/jobs\/([^/]+)$/))) {
      const job = b.archiveJobs[decodeURIComponent(m[1])];
      return job ? resp(200, job) : resp(404, { detail: "找不到这个任务。", code: "not_found" });
    }
    if ((m = key.match(/^POST \/api\/archive\/import\/([^/]+)\/apply$/))) {
      b.applyCalls = (b.applyCalls || []).concat([{ import_id: decodeURIComponent(m[1]), body }]);
      const reply = typeof b.applyReply === "function" ? b.applyReply(body, b.applyCalls.length) : b.applyReply;
      if (reply) return resp(reply[0], reply[1]);
      return resp(202, { job_id: "job-import-1", kind: "import" });
    }
    if ((m = key.match(/^DELETE \/api\/archive\/import\/([^/]+)$/))) {
      b.discardCalls = (b.discardCalls || []).concat([decodeURIComponent(m[1])]);
      return resp(200, { ok: true, discarded: true });
    }
    if (key === "POST /api/notices/legacy-data:dismiss") {
      b.legacyDismissed = (b.legacyDismissed || 0) + 1;
      b.legacyData = { ...b.legacyData, dismissed: true };
      return resp(200, { ok: true });
    }
    if ((m = key.match(/^DELETE \/api\/sessions\/(.+)$/))) {
      b.sessions = b.sessions.filter((x) => x.session_id !== decodeURIComponent(m[1]));
      return resp(200, { ok: true });
    }
    if ((m = key.match(/^POST \/api\/sessions\/(.+)\/load$/))) {
      return resp(200, { ok: true, session: { ...b.session, session_id: decodeURIComponent(m[1]), status: "paused" } });
    }
    // ---- v0.13: the history of games (history.js; contract section 11.5 / 11.8)
    if (key === "GET /api/games") {
      const current = b.session.session_id;
      return resp(200, { games: b.games.map((g) => ({ ...g, current: g.session_id === current })), current });
    }
    if ((m = key.match(/^DELETE \/api\/games\/([^/]+)$/))) {
      const id = decodeURIComponent(m[1]);
      if (id === b.session.session_id) return resp(409, { detail: "当前对局不能删除：请先新建或载入另一局。" });
      b.games = b.games.filter((g) => g.session_id !== id);
      return resp(200, { ok: true, session_id: id });
    }
    if ((m = key.match(/^POST \/api\/games\/([^/]+)\/load$/))) {
      if (b.loadError) return resp(409, { detail: b.loadError });
      return resp(200, { ok: true, session: { ...b.session, session_id: decodeURIComponent(m[1]), status: "paused" } });
    }
    if ((m = key.match(/^GET \/api\/games\/([^/]+)\/replay$/))) {
      if (b.replayError) return resp(409, { detail: b.replayError });
      const replay = b.replays[decodeURIComponent(m[1])];
      return replay ? resp(200, replay) : resp(404, { detail: "对局不存在。" });
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
    if (key === "POST /api/roster/memory:clear") {
      b.lastClear = body;
      for (const seat of b.roster.seats) {
        seat.memory = "";
        seat.memory_parts = emptyParts(seat.id);
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
      b.refineCalls = (b.refineCalls || []).concat([{ id: m[1], ...body }]);
      if (b.refineError) return resp(502, { detail: b.refineError });
      if (body.kind === "memory") {
        // v0.13.1 (contract §12.5): {draft: the 印象 / 经验 text, memory_parts: {impressions (every OTHER seat), experience}} - never legacy / record
        const seat = b.roster.seats.find((x) => x.id === m[1]);
        if (b.refineMemory) return resp(200, b.refineMemory);
        const parts = { impressions: { ...seat.memory_parts.impressions }, experience: [String(body.text).split("\n")[0], ...seat.memory_parts.experience].slice(0, 3) };
        const draft = `## 印象\n${Object.entries(parts.impressions).map(([id, t]) => `- ${id} ${(b.roster.seats.find((x) => x.id === id) || {}).name}：${t || "（暂无）"}`).join("\n")}\n\n## 经验\n${parts.experience.map((e) => `- ${e}`).join("\n")}`;
        return resp(200, { draft, memory_parts: parts });
      }
      return resp(200, { draft: b.refineDraft });
    }
    if (key === "PUT /api/roster/moderator") {
      // the moderator only has an avatar to change (contract §10.9), and only before the game starts
      b.rosterPuts = (b.rosterPuts || []).concat([{ id: "moderator", ...body }]);
      if (b.session.status !== "idle") return resp(409, { detail: "对局已开始，不能修改头像" });
      b.roster.moderator.avatar = body.avatar;
      return resp(200, b.roster.moderator);
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
      for (const k of ["persona", "avatar"]) if (k in body) seat[k] = body[k];
      if ("memory" in body) seat.memory_parts = { ...emptyParts(seat.id), legacy: String(body.memory) }; // (a whole text: free text becomes the legacy text)
      if (body.memory_parts) {
        // only the parts that are given change; `record` is the engine's (accepted, ignored)
        const mp = body.memory_parts;
        if (mp.impressions) Object.assign(seat.memory_parts.impressions, mp.impressions);
        if ("experience" in mp) seat.memory_parts.experience = mp.experience.slice();
        if ("legacy" in mp) seat.memory_parts.legacy = mp.legacy;
      }
      seat.memory = memoryTextOf(seat, b.roster.seats);
      return resp(200, seat);
    }
    if (key === "POST /api/game/review") {
      b.lastReview = body;
      if (b.reviewError) return resp(409, { detail: b.reviewError });
      return resp(200, { started: b.reviewStarted });
    }
    // ---- v0.13.1: the error log (G32-G36, contract §12.3).  `b.llmCalls` is the log (oldest first); `b.llmCallRequests`
    // records the query of every GET
    if ((m = key.match(/^GET \/api\/llm\/calls(?:\?(.*))?$/))) {
      const q = new URLSearchParams(m[1] || "");
      b.llmCallRequests = (b.llmCallRequests || []).concat([Object.fromEntries(q)]);
      if (b.callsFailure) return resp(b.callsFailure[0], { detail: b.callsFailure[1] });
      const after = Number(q.get("after") || 0);
      const limit = Number(q.get("limit") || 500);
      const failed = new Set(["retry", "timeout", "error"]);
      let rows = b.llmCalls.filter((c) => c.id > after && (q.get("errors_only") !== "true" || failed.has(c.status)));
      if (rows.length > limit) rows = rows.slice(rows.length - limit);
      return resp(200, {
        calls: rows,
        last_id: b.llmCalls.reduce((n, c) => Math.max(n, c.id), 0),
        total: b.llmCalls.length,
        errors: b.llmCalls.filter((c) => c.status === "timeout" || c.status === "error").length,
        retries: b.llmCalls.filter((c) => c.status === "retry").length,
      });
    }
    // ---- v0.13.1: the thinking record (G21-G26, contract §12.4): the newest page first, `before` = the id of the oldest
    // item of an earlier page; 404 / 409 like the server (409: another seat is played and the game is not over)
    if ((m = key.match(/^GET \/api\/agents\/([^/?]+)\/thoughts(?:\?(.*))?$/))) {
      const id = decodeURIComponent(m[1]);
      const q = new URLSearchParams(m[2] || "");
      b.thoughtRequests = (b.thoughtRequests || []).concat([{ id, ...Object.fromEntries(q) }]);
      if (b.thoughtsFailure) return resp(b.thoughtsFailure[0], { detail: b.thoughtsFailure[1] });
      if (id !== "moderator" && !F.PLAYER_IDS.includes(id)) return resp(404, { detail: "没有这个玩家。" });
      const played = b.session.takeover && b.session.takeover.seat;
      if (played && b.session.status !== "finished" && id !== played) {
        return resp(409, { detail: "接管期间不能查看其他玩家的思考记录，对局结束后开放。" });
      }
      const all = b.thoughts[id] || [];
      let older = all;
      if (q.get("before")) {
        const at = all.findIndex((t) => t.id === q.get("before"));
        if (at < 0) return resp(400, { detail: "游标无效。" });
        older = all.slice(0, at);
      }
      const limit = Number(q.get("limit") || 50);
      const items = older.slice(Math.max(0, older.length - limit));
      const hasMore = older.length > items.length;
      const name = id === "moderator" ? "主持人" : F.NAMES[F.PLAYER_IDS.indexOf(id)];
      return resp(200, { agent_id: id, name, items, next_before: hasMore ? items[0].id : null, has_more: hasMore });
    }
    // ---- v0.13: playing a seat (docs/v0.12-contracts.md §11.7).  The fake keeps no game: a test sets
    // `b.takeoverResult` (what POST /api/takeover answers with), `b.seatReply.<kind>` (a receipt, or a function of
    // the body returning one) and reads `b.takeoverCalls` / `b.releaseCalls` / `b.shuffleCalls` / `b.seatCalls`.
    if (key === "POST /api/takeover") {
      b.takeoverCalls = (b.takeoverCalls || []).concat([body]);
      const taken = b.takeoverResult || { ...b.session, takeover: takeoverOf(b.session, body.seat) };
      return resp(200, { ok: true, session: taken });
    }
    if (key === "POST /api/takeover/release") {
      b.releaseCalls = (b.releaseCalls || 0) + 1;
      return resp(200, { ok: true, session: b.releaseResult || { ...b.session, takeover: { seat: null, reshuffle_available: false } } });
    }
    if (key === "POST /api/control/shuffle-identities") {
      b.shuffleCalls = (b.shuffleCalls || 0) + 1;
      return resp(200, { ok: true, session: b.controlSession || b.session });
    }
    if ((m = key.match(/^POST \/api\/seat\/(speak|pass|respond|ability)$/))) {
      b.seatCalls = (b.seatCalls || []).concat([{ kind: m[1], body }]);
      const reply = (b.seatReply || {})[m[1]];
      const out = typeof reply === "function" ? reply(body) : reply;
      return resp(200, out || { accepted: true, seq: 99, code: null, message: "已发送。" });
    }
    return resp(404, { detail: "nf" });
  };
  // v0.13.1: `b.hold(test)` parks the answers of the requests that `test(key, url)` accepts.  An answer is COMPUTED when the request is made
  // (what the server said at that moment) and DELIVERED when the gate lets it go - the way to make an older answer arrive after a newer one.
  // gate.parked = [{key, url}] in the order the requests were made; gate.release(i = 0) delivers one, gate.releaseAll() the rest,
  // gate.off() stops holding (and delivers what is parked).
  b.holds = [];
  b.hold = (test) => {
    const gate = {
      test,
      parked: [],
      release(i = 0) {
        const entry = gate.parked.splice(i, 1)[0];
        if (entry) entry.resolve();
        return Boolean(entry);
      },
      releaseAll() {
        while (gate.parked.length) gate.release(0);
      },
      off() {
        b.holds = b.holds.filter((h) => h !== gate);
        gate.releaseAll();
      },
    };
    b.holds.push(gate);
    return gate;
  };
  const served = b.fetch;
  b.fetch = (url, init0) => {
    const answer = served(url, init0);
    if (!b.holds.length) return answer; // (the very promise it always was: no extra tick for the many tests that never hold anything)
    const key = `${(init0 && init0.method) || "GET"} ${String(url).replace(/^\/arena/, "")}`;
    const gate = b.holds.find((h) => h.test(key, url));
    if (!gate) return answer;
    return answer.then((out) => new Promise((resolve) => gate.parked.push({ key, url, resolve: () => resolve(out) })));
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
  if (!backend.uploadManual) env.xhrHooks.auto = backend.uploadHandler;
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
  let finished = false;
  let running = "";
  // A test that waits for something that never happens (a dialog nobody answers, a promise nobody resolves) does
  // not hang node: the event loop simply runs dry and the process ends with exit code 0, which looks like a pass.
  process.on("exit", () => {
    if (finished) return;
    console.log(`FAIL - the process ended before the tests finished (stuck in: ${running || "?"})`);
    process.exitCode = 1;
  });
  (async () => {
    let failed = 0;
    for (const [name, fn] of tests) {
      running = name;
      try {
        await fn();
        console.log(`ok   - ${name}`);
      } catch (e) {
        failed++;
        console.log(`FAIL - ${name}\n${e && e.stack ? e.stack : e}`);
      }
    }
    finished = true;
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
