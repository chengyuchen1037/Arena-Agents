"use strict";
// The phone walk-through of v0.14's screens, without a browser (the lead's list, item 4): the settings dialog with its five columns, the panel of a bot, the dialogs of the
// initialization, the dialog of the start check and the window at the end of a game - at 390 and 430 px, on a touch screen.
//
// A real browser is not at hand, so the page is built twice over: the HTML the scripts really draw (the fake DOM of harness.js keeps it) is put into index.html, and the
// real style sheet is asked, by tests/frontend/cssprobe.js, what its cascade makes of every element: is a control at least 40 px tall (the finger), does a row of controls
// wrap (instead of being cut off), is a width fixed to more than the screen has, does a long text refuse to break.  It cannot measure text - that is what a person
// with a phone is for - but these are the places where a phone layout goes wrong.
// Run: node tests/frontend/test_phone_layout.js            (--report: print what every scene found)
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const H = require("./harness.js");
const P = require("./cssprobe.js");

const { F, target, settle, bootLive } = H;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

const CSS = fs.readFileSync(path.join(H.ROOT, "static", "css", "style.css"), "utf8");
const PHONE_ATTRS = { "data-platform": "phone", "data-layout": "phone", "data-input": "touch", "data-orientation": "portrait", "data-os": "ios", "data-theme": "light" };
const WIDTHS = [360, 390, 430]; // a small Android phone, an iPhone, a big one
const TAP = 40; // the smallest tap target of the page (docs/ui-guide.md: "every tap target is at least 40 px")
const report = process.argv.includes("--report");

// =====================================================================================================
// the audit
// =====================================================================================================
const describe = (el) => {
  const cls = P.classesOf(el).slice(0, 3).join(".");
  const act = el.attrs["data-settings-action"] || el.attrs["data-detail-action"] || el.attrs["data-nr-fix"] || el.attrs["data-rv-open"] || el.attrs["data-roster-action"] || el.attrs["data-action"] || "";
  return `${el.tag}${cls ? `.${cls}` : ""}${el.attrs.id ? `#${el.attrs.id}` : ""}${act ? `[${act}]` : ""}`;
};
const closest = (el, tag) => {
  for (let n = el.parent; n; n = n.parent) if (n.tag === tag) return n;
  return null;
};
const isTapTarget = (el) => ["button", "select", "textarea", "summary"].includes(el.tag) || (el.tag === "input" && el.attrs.type !== "hidden") || (el.tag === "a" && "href" in el.attrs) || el.attrs.role === "button";

/** The height the style sheet gives an element at least (min-height or height, px), 0 when it gives none. */
function heightOf(engine, el) {
  const v = [engine.px(el, "min-height"), engine.px(el, "height")].filter((n) => Number.isFinite(n));
  return v.length ? Math.max(...v) : 0;
}

/** A run of characters that has no blank and no CJK in it: nothing in it lets a line break unless the style sheet says it may (overflow-wrap / word-break). */
const UNBREAKABLE = /[^\s\u2E80-\u9FFF\uF900-\uFAFF\uFF00-\uFFEF\u3000-\u303F]+/g;
const CHAR_PX = 8; // an ASCII character of 13 - 14 px text (bold, monospace: a little more)
const OUTSIDE = ["modal-body", "modal-card", "detail-panel"]; // the boxes of a screen: they clip or scroll the page, they do not keep a long word in

/** Is the element inside something that holds a long word in (its own scroll / clip), below the box of the screen? */
function holdsLongWord(engine, el, root) {
  for (let n = el; n && n.tag !== "#root"; n = n.parent) {
    if (P.classesOf(n).some((c) => OUTSIDE.includes(c))) return false;
    if (["hidden", "auto", "scroll", "clip"].includes(engine.computed(n, "overflow-x"))) return true;
    if (n === root) break;
  }
  return false;
}

/**
 * What is wrong in the subtree of `root` for a window of `width` px: {small, nowrap, wide, nowrapText, longText}, each a list of descriptions.
 *  small      a control the finger cannot hit: under 40 px tall (a check box / radio: its label)
 *  nowrap     a row (display: flex, direction row) of two or more things that does not wrap: it is cut off, or pushes the window wider, where it is too narrow
 *  wide       an element fixed to a width the window does not have
 *  nowrapText a text that refuses to break (white-space: nowrap) and is longer than a line of the window
 *  longText   a long word (a file name, an id) that is wider than the screen and that nothing lets break (overflow-wrap / word-break) or holds in (overflow)
 */
function audit(engine, root, width) {
  const out = { small: [], nowrap: [], wide: [], nowrapText: [], longText: [] };
  for (const el of P.walk(root)) {
    if (P.isText(el) || el.tag === "#root") continue;
    if (!engine.rendered(el)) continue;
    for (const child of el.children) {
      if (!P.isText(child)) continue;
      const ow = engine.computed(el, "overflow-wrap");
      const wb = engine.computed(el, "word-break");
      if (ow === "anywhere" || ow === "break-word" || wb === "break-all" || wb === "break-word") continue;
      const longest = (child.text.match(UNBREAKABLE) || []).reduce((a, b) => (b.length > a.length ? b : a), "");
      if (longest.length * CHAR_PX > width - 64 && !holdsLongWord(engine, el, root)) out.longText.push(`${describe(el)} "${longest.slice(0, 24)}…" (${longest.length} chars)`);
    }
    if (isTapTarget(el)) {
      let box = el;
      if (el.tag === "input" && (el.attrs.type === "checkbox" || el.attrs.type === "radio")) box = closest(el, "label") || el;
      const h = heightOf(engine, box);
      if (h < TAP) out.small.push(`${describe(el)} ${h ? `${h}px` : "no height"}`);
    }
    const display = engine.display(el);
    if (display === "flex" || display === "inline-flex") {
      const dir = engine.computed(el, "flex-direction") || "row";
      const things = el.children.filter((c) => (P.isText(c) ? c.text.trim() : engine.rendered(c)));
      if (dir.startsWith("row") && things.length >= 2 && (engine.computed(el, "flex-wrap") || "nowrap") === "nowrap") out.nowrap.push(describe(el));
    }
    const w = engine.px(el, "width");
    if (Number.isFinite(w) && w > width) out.wide.push(`${describe(el)} width ${w}px`);
    const mw = engine.px(el, "min-width");
    if (Number.isFinite(mw) && mw > width - 24) out.wide.push(`${describe(el)} min-width ${mw}px`);
    if ((engine.computed(el, "white-space") || "normal") === "nowrap" && el.children.some(P.isText)) {
      const own = el.children.filter(P.isText).map((c) => c.text.trim()).join("");
      if (own.length * 13 > width - 60) out.nowrapText.push(`${describe(el)} ${own.length} chars`);
    }
  }
  for (const key of Object.keys(out)) out[key] = [...new Set(out[key])];
  return out;
}

/** Run a scene: `build(snap)` brings the page to the states worth looking at and calls `snap(env, label, rootId)` in each; every snapshot is audited at every width (`css`: another style sheet). */
async function runScene(build, css = CSS) {
  const snaps = [];
  const snap = (env, label, rootId, extra) => {
    for (const width of WIDTHS) {
      const page = P.buildPage(H.HTML_SRC, env, PHONE_ATTRS);
      const engine = new P.Engine(css, { width, height: 844, hover: false });
      const root = P.findById(page.html, rootId);
      assert.ok(root, `${rootId} is in the page`);
      assert.ok(engine.rendered(root), `${label}: ${rootId} is drawn (it is open)`);
      const text = P.textOf(root);
      assert.ok(text.length > 20, `${label}: there is something in it`);
      snaps.push({ label, width, controls: [...P.walk(root)].filter((n) => !P.isText(n) && isTapTarget(n) && engine.rendered(n)).length, ...audit(engine, root, width) });
      if (extra) extra({ page, engine, root, width, byId: (id) => P.findById(page.html, id) });
    }
  };
  await build(snap);
  if (report) {
    console.log(`\n== ${snaps.map((s) => s.label).filter((l, i, a) => a.indexOf(l) === i).length} snapshots: ${[...new Set(snaps.map((s) => s.label))].map((l) => `${l} (${snaps.find((s) => s.label === l).controls} controls)`).join(" | ")}`);
    for (const kind of ["small", "wide", "nowrapText", "longText", "nowrap"]) console.log(`   ${kind}: ${JSON.stringify(found(snaps, kind))}`);
  }
  return snaps;
}
const found = (snaps, kind) => [...new Set(snaps.flatMap((s) => s[kind]))].sort();

// =====================================================================================================
// the scenes: a page in the state the user would see
// =====================================================================================================
const flushAll = async (env, ms = 30) => {
  await settle(ms);
  env.flushRaf();
  await settle(ms);
  env.flushRaf();
};
async function live(over = {}, overrides = {}, envOpts = {}) {
  const session = F.makeSession({ event_id: 500, ...over });
  const ctx = await bootLive({ session, ...overrides }, envOpts);
  ctx.env.sse("snapshot", { session }, 500);
  await flushAll(ctx.env, 20);
  return ctx;
}
const cardClick = (env, id) => env.el("participants-grid").dispatch("click", { target: target({ ".pcard": { "data-actor": id } }) });
const panelClick = (env, action, attrs = {}) => env.el("detail-panel").dispatch("click", { target: target({ "[data-detail-action]": { "data-detail-action": action, ...attrs } }) });
const act = (env, action, attrs = {}) => env.el("settings-body").dispatch("click", { target: target({ "[data-settings-action]": { "data-settings-action": action, ...attrs } }) });
const dialogOk = (env) => env.el("btn-dialog-ok").dispatch("click");
const dialogCancel = (env) => env.el("btn-dialog-cancel").dispatch("click");
const ev = (n, over = {}) => ({ job_id: "job-init-1", bot_id: F.botId(n), name: F.NAMES[n - 1], state: "running", seconds_left: 100, deadline_s: 120, files_written: [], error: null, ...over });
const REVIEW_KEY = "arena_review_dismissed_v1";
const reviewOf = (over = {}) => ({ available: true, done: [], running: false, supported: true, deadline_s: 60, seconds_left: null, ...over });

const SCENES = {
  /** ⚙ 设置: the five columns, all open, with something in each - and the confirm dialogs that open from it. */
  async settings(snap) {
    const { env, backend } = await live({ status: "idle" }, { botsPreset: "mixed" });
    backend.botsApi.contractState.broken = "invalid"; // (the list of faults is the longest thing of column ③)
    backend.botsApi.rulesState.werewolf_12p.materials.openings = { title: "开局套路", markdown: "先听完警上发言。" };
    env.el("btn-open-settings-modal").dispatch("click");
    await flushAll(env);
    snap(env, "settings: the home view (only ① is open)", "settings-modal");
    for (const col of ["core", "contract", "rules", "games"]) act(env, "toggle-col", { "data-col": col });
    await flushAll(env);
    act(env, "core-toggle", { "data-bot": F.botId(1) });
    act(env, "games-toggle", { "data-id": "old-1" });
    act(env, "games-toggle", { "data-id": "old-2" });
    await flushAll(env);
    snap(env, "settings: all five columns open, a bot and two games expanded", "settings-modal");
    act(env, "core-clear", { "data-bot": F.botId(2) });
    await flushAll(env);
    snap(env, "settings: the question before an L0 is cleared", "dialog-modal");
    dialogCancel(env);
    await flushAll(env);
    act(env, "mat-add", { "data-id": "werewolf_12p" });
    env.el("file-settings-import").dispatch("change", { target: { files: [{ name: "Opening Moves.md", size: 20, text: async () => "# 开局套路\n先听完警上发言。\n" }], value: "x" } });
    await flushAll(env);
    snap(env, "settings: the question before a study material is added (a slug and a title to type)", "dialog-modal");
  },

  /** What a person brings from the downloads folder: a file with a long name and a game with a long id - they are PRINTED in the questions before an import. */
  async longNames(snap) {
    const LONG_NAME = "my-really-long-contract-file-name-that-stayed-in-the-downloads-folder-for-months-v2-final";
    const LONG_ID = "a1b2c3d4e5f6-a-game-id-of-the-longest-kind-the-server-allows-123"; // 64 characters: the most an id may have
    assert.equal(LONG_ID.length, 64);
    const { env, backend } = await live({ status: "idle" }, { botsPreset: "ready" });
    backend.games.push({ session_id: LONG_ID, title: "Arena-Agents", scenario_title: "狼人杀 12 人局", status: "finished", winner: "好人阵营", created_at: "2026-11-10T09:00:00Z", updated_at: "2026-11-10T10:00:00Z", duration_gt: 900, players: 12, tokens: 1000, cost_usd: 0.01, human_involved: false, schema_version: 6, size_bytes: 5000, log_bytes: 1000, readable: true, review: { done: 0, total: 12 }, participants_brief: F.NAMES.slice() });
    const file = (name, text) => ({ name, size: Buffer.byteLength(text), text: async () => text });
    env.el("btn-open-settings-modal").dispatch("click");
    await flushAll(env);
    for (const col of ["contract", "rules", "games"]) act(env, "toggle-col", { "data-col": col });
    await flushAll(env);
    act(env, "contract-import");
    env.el("file-settings-import").dispatch("change", { target: { files: [file(`${LONG_NAME}.md`, "---\nformat: arena-contract\nversion: 1\n---\n# x\n")], value: "x" } });
    await flushAll(env);
    snap(env, "long names: the question before a contract with a long file name is imported", "dialog-modal");
    dialogCancel(env);
    await flushAll(env);
    act(env, "rules-import", { "data-id": "werewolf_12p" });
    env.el("file-settings-import").dispatch("change", { target: { files: [file(`${LONG_NAME}.scenario.md`, "---\nschema_version: 2\n---\n")], value: "x" } });
    await flushAll(env);
    snap(env, "long names: the question before a scenario with a long file name is imported", "dialog-modal");
    dialogCancel(env);
    await flushAll(env);
    act(env, "games-import");
    env.el("file-settings-import").dispatch("change", { target: { files: [file(`game-${LONG_ID}-20251119.arenagame`, "zip")], value: "x" } });
    await flushAll(env);
    snap(env, "long names: the id of a game that exists (a copy / overwrite / cancel)", "dialog-modal");
    dialogCancel(env);
    await flushAll(env);
    // a game with another long id is simply added: the notice of the column prints the id
    act(env, "games-import");
    env.el("file-settings-import").dispatch("change", { target: { files: [file(`game-${LONG_ID.replace("a1b2", "1b8c")}-20251119.arenagame`, "zip")], value: "x" } });
    await flushAll(env);
    snap(env, "long names: the notice of a game that was imported (it prints its id)", "settings-modal");
    env.el("btn-close-settings-modal").dispatch("click");
    cardClick(env, "N2");
    await flushAll(env);
    panelClick(env, "toggle-editor", { "data-kind": "transfer" });
    await flushAll(env, 10);
    panelClick(env, "bot-import");
    env.el("file-bot-import").dispatch("change", { target: { files: [file(`${LONG_NAME}.arenabot`, "zip")], value: "x" } });
    await flushAll(env);
    snap(env, "long names: the question before a .arenabot with a long file name replaces a bot", "dialog-modal");
  },

  /** Column ① (the wizards of the archive, v0.13) in the five-column dialog: the question about the API key, the password and its rules, the choice of a file to import. */
  async settingsWizard(snap) {
    const { env } = await live({ status: "paused" }, { botsPreset: "ready" });
    env.el("btn-open-settings-modal").dispatch("click");
    await flushAll(env);
    const wizard = (action) => env.el("settings-footer").dispatch("click", { target: target({ "[data-action]": { "data-action": action } }) });
    wizard("export-start");
    snap(env, "settings ①: export, step 1 (the API key?)", "settings-modal");
    wizard("export-keys-yes");
    env.el("settings-body").dispatch("input", { target: { id: "input-archive-password", value: "abc" } });
    snap(env, "settings ①: export, step 2 (the password and its rules)", "settings-modal");
    wizard("export-to-keys");
    wizard("back-home");
    wizard("import-pick");
    snap(env, "settings ①: import, choose the file", "settings-modal");
  },

  /** The panel of a bot (a bottom drawer on a phone): the head with the name and the initialization row, the live part, the four editors - open, with proposals and a draft. */
  async panel(snap) {
    const { env, backend } = await live({ status: "idle" }, { botsPreset: "ready" });
    backend.botsApi.set("N2", {
      core: { extras: [{ title: "口头禅", slug: "x1", text: "「行。」" }] },
      staging: [
        F.makeProposal({ id: "p-0001", section: "voice", before: "你说话短。\n爱反问。", after: "你说话短。\n爱反问。\n先说结论，再补理由。", reason: "接管者说话短、先说结论" }),
        F.makeProposal({ id: "p-0002", section: "extra:x1", before: "「行。」", after: "「行，我听你的。」", reason: "口头禅" }),
      ],
    });
    backend.botsApi.initialized("N2");
    await env.app().bots().refreshBots();
    cardClick(env, "N2");
    await flushAll(env);
    snap(env, "panel: a ✔ bot (frozen: the lock), the editors closed", "detail-panel");
    for (const kind of ["core", "memory", "staging", "transfer"]) {
      panelClick(env, "toggle-editor", { "data-kind": kind });
      await flushAll(env, 10);
    }
    snap(env, "panel: all four editors open (the L0 with an extra, the memory, two proposals, the import / export)", "detail-panel");
    // a draft of 用主持人模型完善: the L0 as it is goes to the moderator's model, the draft is read first
    backend.refineDraft = "---\nformat: arena-core\nversion: 1\n---\n# 身份\n新的身份\n\n# 说话方式\n新的说话\n\n# 分析方式\n新的分析\n";
    panelClick(env, "core-refine");
    await flushAll(env);
    snap(env, "panel: the draft of 用主持人模型完善 is shown first", "detail-panel");
  },

  /** A bot that is being initialized (a bar, the seconds left, the button that cancels), one that failed (the reason, the retry), one that was never initialized. */
  async panelInit(snap) {
    const { env, backend } = await live({ status: "idle" }, { botsPreset: "mixed" });
    cardClick(env, "N3");
    await flushAll(env);
    snap(env, "panel: a ✖ bot (the reason and the retry button)", "detail-panel");
    cardClick(env, "N3");
    cardClick(env, "N4");
    await flushAll(env);
    snap(env, "panel: a ○ bot (never initialized; the button that starts it)", "detail-panel");
    panelClick(env, "toggle-avatar-pop");
    await flushAll(env, 10);
    snap(env, "panel: the 🎲 panel of a bot that is not frozen (the code and its buttons)", "detail-panel");
    panelClick(env, "toggle-avatar-pop");
    panelClick(env, "init-one");
    await flushAll(env);
    dialogOk(env);
    await flushAll(env);
    env.sse("bot.init.progress", ev(4, { seconds_left: 40, files_written: ["arch_notes", "digest"] }));
    await flushAll(env, 10);
    snap(env, "panel: a ⏳ bot (the bar, the seconds left, the files written, cancel)", "detail-panel");
    assert.ok(backend.initCalls.length);
  },

  /** After a game: the review of a bot in its panel (the countdown, what it changed, a diff). */
  async panelReview(snap) {
    const { env } = await live({ status: "finished", winner: "好人阵营", review: reviewOf() }, { botsPreset: "ready" }, { storage: { [REVIEW_KEY]: JSON.stringify({ "s-test": true }) } });
    env.sse("review.progress", { id: "N1", bot_id: F.botId(1), name: "Alice", state: "running", message: "正在整理印象", seconds_left: 41, deadline_s: 60, changes: null });
    cardClick(env, "N1");
    await flushAll(env);
    snap(env, "panel: a bot that is being reviewed (the countdown)", "detail-panel");
    env.sse("review.progress", { id: "N1", bot_id: F.botId(1), name: "Alice", state: "done", message: "", seconds_left: 5, deadline_s: 60, changes: { files: [{ file: "people", before: "- Bob：爱带节奏\n- Carol：话少", after: "- Bob：爱带节奏，容易被反驳带偏\n- Carol：话少" }], proposals: 1 } });
    await flushAll(env);
    panelClick(env, "toggle-changes");
    await flushAll(env, 10);
    snap(env, "panel: what the review changed (a line diff) and the proposals that wait", "detail-panel");
  },

  /** 全局初始化 and the refusals: the list of who is taken (with the check box), one bot's question, the dialog of a missing model, the question before the memory is cleared. */
  async initDialogs(snap) {
    const { env, backend } = await live({ status: "idle" }, { botsPreset: "mixed" });
    env.el("btn-init-all").dispatch("click");
    await flushAll(env);
    snap(env, "init: 全局初始化 - who is taken, and the check box 全部重新初始化", "dialog-modal");
    env.el("chk-init-all").checked = true;
    env.el("chk-init-all").dispatch("change");
    snap(env, "init: 全局初始化 with everybody", "dialog-modal");
    dialogCancel(env);
    await flushAll(env);
    backend.botsApi.set("N5", { binding: { ok: false, label: "" } });
    await env.app().bots().refreshBots();
    cardClick(env, "N5");
    await flushAll(env);
    panelClick(env, "init-one");
    await flushAll(env);
    snap(env, "init: the question before one bot is initialized", "dialog-modal");
    dialogOk(env);
    await flushAll(env);
    snap(env, "init: the refusal - a player has no model (a button to the bindings)", "dialog-modal");
    dialogCancel(env);
    await flushAll(env);
    env.el("btn-roster-menu").dispatch("click");
    env.el("roster-menu").dispatch("click", { target: target({ "[data-roster-action]": { "data-roster-action": "clear" } }) });
    await flushAll(env);
    snap(env, "init: the question before everything the bots learned is cleared", "dialog-modal");
  },

  /** The question before the name of a ✔ bot changes (it forgets what it learned). */
  async resetDialog(snap) {
    const { env } = await live({ status: "idle" }, { botsPreset: "ready" });
    cardClick(env, "N3");
    await flushAll(env);
    env.el("input-detail-name").value = "Caroline";
    panelClick(env, "save-name");
    await flushAll(env);
    snap(env, "reset: the question before a name or an avatar of a ✔ bot changes", "dialog-modal");
  },

  /** 开始 refused: what is not ready, by player, each item with its sentence and a button (the contract, five players, the moderator, one with several items). */
  async startCheck(snap) {
    const session = F.undealt(F.makeSession({ event_id: 500, status: "idle" }));
    const { env, backend } = await bootLive({ session, botsPreset: "mixed" });
    env.sse("snapshot", { session }, 500);
    await flushAll(env, 20);
    const problems = [
      F.makeProblem("contract", [{ code: "contract_invalid", message: "官方契约有问题：契约没有提到这些工具：jot、read_contract。每个工具的名字都要出现在契约里。", fix: "import_contract" }]),
      F.makeProblem("scenario", [{ code: "scenario_invalid", message: "剧本 werewolf_12p.md 无法加载：身份池共有 11 个身份，但对局有 12 名玩家，无法发牌。", fix: "import_rules" }]),
      F.makeProblem("bot", [{ code: "not_initialized", message: "还没有初始化。", fix: "init" }, { code: "core_missing", message: "底层设定是空的：缺少「身份」「说话方式」「分析方式」。", fix: "edit_core" }, { code: "core_import", message: "也可以直接导入一份底层设定文件。", fix: "import_core" }], { bot_id: F.botId(3), seat: "N3", name: "Carol" }),
      F.makeProblem("bot", [{ code: "stale", message: "底层设定改过，需要重新初始化。", fix: "init" }], { bot_id: F.botId(6), seat: "N6", name: "Frank" }),
      F.makeProblem("bot", [{ code: "failed", message: "上次初始化失败：时间到了（120 秒），初始化没有完成，原来的记忆没有改动。", fix: "init" }], { bot_id: F.botId(8), seat: "N8", name: "Henry" }),
      F.makeProblem("binding", [{ code: "no_binding", message: "N7 还没有绑定可用的模型。", fix: "bind_model" }], { bot_id: F.botId(7), seat: "N7", name: "Grace" }),
      F.makeProblem("binding", [{ code: "no_binding", message: "主持人还没有绑定可用的模型。", fix: "bind_model" }], { name: "主持人" }),
    ];
    backend.failures["POST /api/control/start"] = [409, "还没有准备好，不能开局：官方契约：官方契约有问题。", { code: "NOT_READY", problems }];
    env.el("btn-start").dispatch("click");
    await flushAll(env);
    snap(env, "start check: the dialog 还不能开局 (seven groups, the button of a global initialization)", "dialog-modal", ({ engine, byId, width }) => {
      // the dialog is the whole screen and its body scrolls: the list of the groups is no scroll box of its own inside it
      const groups = byId("not-ready-groups");
      assert.ok(groups, "the list of the groups");
      assert.ok(!Number.isFinite(engine.px(groups, "max-height")), `@${width}: no max-height on the list (${engine.computed(groups, "max-height")})`);
      assert.notEqual(engine.computed(groups, "overflow-y"), "auto", `@${width}: not a scroll box inside the scrolling body`);
      assert.equal(engine.computed(byId("dialog-body"), "overflow-y"), "auto", `@${width}: the body of the dialog is what scrolls`);
      // every message and its button wrap into lines of their own where they do not fit side by side
      const item = [...P.walk(groups)].find((n) => P.classesOf(n).includes("nr-item"));
      assert.ok(item, "an item of a group");
      assert.equal(engine.computed(item, "flex-wrap"), "wrap");
      assert.equal(engine.computed(item, "display"), "flex");
    });
  },

  /** The window at the end of a game: the question (twelve bots, a person's seat with its switch), then the countdowns and what every bot changed. */
  async endReview(snap) {
    const session = F.makePlayedSession("N3", { event_id: 500, status: "finished", winner: "好人阵营", review: reviewOf() });
    const { env, backend } = await bootLive({ session: F.makeSession({ event_id: 400, status: "running", review: reviewOf({ available: false }) }) });
    const original = backend.fetch;
    backend.fetch = async (url, init) => (/\/api\/games\/[^/]+\/detail$/.test(String(url)) ? H.resp(200, { participants: [], files: { game_bytes: 1, prev_bytes: 0, log_bytes: 1 }, human_spans: [{ seat: "N6", from_seq: 40, to_seq: 218 }] }) : original(url, init));
    env.sandbox.fetch = backend.fetch;
    env.sse("snapshot", { session }, 500);
    await flushAll(env);
    snap(env, "end of game: the question (twelve bots, the seats a person played with their switch)", "review-modal");
    backend.reviewStarted = F.PLAYER_IDS;
    env.el("btn-review-start").dispatch("click");
    await flushAll(env);
    const p = (id, over = {}) => ({ id, bot_id: F.botId(Number(id.slice(1))), name: F.NAMES[Number(id.slice(1)) - 1], state: "running", message: "", seconds_left: 58, deadline_s: 60, changes: null, ...over });
    env.sse("review.progress", p("N1", { message: "正在整理操作笔记" }));
    env.sse("review.progress", p("N2", { state: "done", seconds_left: null, changes: { files: [{ file: "playbook", before: "a", after: "b" }, { file: "people", before: "", after: "Bob 爱带节奏" }], proposals: 2 } }));
    env.sse("review.progress", p("N4", { state: "done", seconds_left: null, changes: { files: [], proposals: 0 } }));
    env.sse("review.progress", p("N5", { state: "timeout", seconds_left: null, changes: { files: [{ file: "arch_notes", before: "", after: "x" }], proposals: 0 } }));
    env.sse("review.progress", p("N7", { state: "error", seconds_left: null, message: "调用模型失败（HTTP 429 Too Many Requests），这个 bot 本局没有复盘。" }));
    await flushAll(env);
    snap(env, "end of game: the countdowns, a done review with changes and proposals, a timeout, a failure", "review-modal");
  },

  /** The bar of the players (the 玩家 tab of a phone): 全局初始化 with the counts, the roster menu - open - with its switch, 复盘全部. */
  async playersBar(snap) {
    const { env } = await live({ status: "finished", winner: "好人阵营", review: reviewOf() }, { botsPreset: "mixed" }, { storage: { [REVIEW_KEY]: JSON.stringify({ "s-test": true }) } });
    env.app().setPhoneTab("players"); // (a phone shows the bar in its own tab)
    env.el("btn-roster-menu").dispatch("click");
    await flushAll(env);
    snap(env, "players bar: 全局初始化, the counts, 名册 ▾ (open), 复盘全部", "board-header");
    snap(env, "players bar: the roster menu", "roster-menu");
  },
};

/**
 * The rows that do not wrap and may not: each was looked at - what is in it, which part can give way (a text that breaks, a box with min-width: 0, an ellipsis).  A row of
 * two things or more that is NOT here and does not wrap is new: a person has to look at it on a phone before it is added.
 */
const ONE_LINE = {
  "div.modal-header": "the title and the ✕",
  "div.modal-footer": "the buttons of a dialog: on a phone they share the line (flex: 1), two or three short labels",
  "button.settings-col-head": "the caret, the number and the title of a column; the line about what is in it is not drawn on a phone",
  "label.checkbox-label.dialog-check": "a check box and its text, which breaks",
  "label.dropdown-item.check": "a check box and its text, which breaks",
  "div.detail-head-row": "the avatar (fixed) and the title block (flex: 1; min-width: 0, the name has an ellipsis)",
  "div.detail-name-row": "the box of the name (flex: 1; min-width: 0) and its buttons",
  "div.detail-init-line": "the mark, the state (flex: 1; min-width: 0, breaks) and one button",
  "div.detail-state": "an icon and a short word",
  "div.pop-model": "a label, the model (an ellipsis) and one button",
  "div.pop-stat": "the cost: a number and a label, both with an ellipsis",
  "button.detail-editor-toggle": "the caret, the title and a line about what is in it (min-width: 0, breaks)",
  "button.review-changes-toggle": "the caret and one line of text",
  "label.mem-label": "the name of a part (breaks) and its counter",
  "div.core-extra-head": "the title box (flex: 1; min-width: 0), its counter and the delete button",
  "div.diff-line": "the mark (10 px) and the text, which breaks anywhere",
  "div.rv-actions": "the two short buttons 全选 / 全不选",
  "label.rv-pick": "a check box, the seat and the name (an ellipsis)",
  "label.rv-style": "a check box and its text",
};
/** `button.settings-col-head#settings-col-head-core[toggle-col]` -> `button.settings-col-head` ; `div.diff-line.add` -> `div.diff-line` (the classes that decide what it is). */
const shapeOf = (description) => {
  const base = description.replace(/\[[^\]]*\]$/, "").replace(/#[\w-]+/, "");
  return Object.keys(ONE_LINE).find((k) => base === k || base.startsWith(`${k}.`)) || base;
};

for (const [name, build] of Object.entries(SCENES)) {
  test(`${name}: at ${WIDTHS.join(", ")} px every control is at least ${TAP} px tall, no width is more than the screen, no row of controls is left unreviewed`, async () => {
    const snaps = await runScene(build);
    assert.ok(snaps.length >= WIDTHS.length);
    if (report) return;
    assert.deepEqual(found(snaps, "small"), [], "a finger cannot hit these (min-height / height under 40 px)");
    assert.deepEqual(found(snaps, "wide"), [], "these are wider than the screen");
    assert.deepEqual(found(snaps, "nowrapText"), [], "a text that cannot break and is longer than a line");
    assert.deepEqual(found(snaps, "longText"), [], "a long word (a file name, an id) that nothing lets break: the card clips, the end of it is gone");
    const unreviewed = [...new Set(found(snaps, "nowrap").map(shapeOf))].filter((s) => !(s in ONE_LINE));
    assert.deepEqual(unreviewed, [], "a row of two things or more without flex-wrap: wrap it, or look at it on a phone and put it in ONE_LINE with the reason");
  });
}

test("the audit sees what it is for: take a rule of the style sheet away and the page it guards is reported", async () => {
  const without = (css, pattern) => {
    const cut = css.replace(pattern, "");
    assert.notEqual(cut, css, `the style sheet has ${pattern}`);
    return cut;
  };
  const scene = async (css, which = "settings") => runScene(SCENES[which], css);
  // (a) the hit area of the quiet buttons on a touch screen
  const quiet = await scene(without(CSS, /html\[data-input="touch"\] \.btn-quiet \{[^}]*\}/));
  assert.ok(found(quiet, "small").some((s) => s.startsWith("button.btn-quiet")), "a .btn-quiet button without the touch rule is under 40 px");
  // (b) the row head that wraps its actions under the toggle
  const unwrapped = await scene(CSS.replace(/(\.settings-row-head \{[^}]*?)flex-wrap: wrap;/, "$1"));
  assert.ok(found(unwrapped, "nowrap").some((s) => s.startsWith("div.settings-row-head")), "a row head without flex-wrap is reported");
  assert.ok(!found(await scene(CSS), "nowrap").some((s) => s.startsWith("div.settings-row-head")), "...and the real one is not");
  // (c) a fixed width
  const fixed = await scene(`${CSS}\n.settings-pre { width: 600px; }\n`);
  assert.ok(found(fixed, "wide").some((s) => /^pre\.settings-pre\S* width 600px$/.test(s)), "a width of 600px on a 390px screen is reported");
  // (d) the review window: the check boxes of a bot and of its switch
  const rv = await scene(`${CSS}\nhtml[data-layout="phone"] .rv-pick { min-height: 34px; }\n`, "endReview");
  assert.ok(found(rv, "small").some((s) => s.startsWith("input")), "a label of 34 px is reported through its check box");
  const rvStyle = await scene(without(CSS, /html\[data-layout="phone"\] \.rv-pick,\s*html\[data-layout="phone"\] \.rv-style \{[^}]*\}/), "endReview");
  assert.ok(found(rvStyle, "small").some((s) => s.startsWith("input")), "without the rule of this walk-through the labels of the review window are too small");
  // (e) the start check on a phone: the dialog is the whole screen and scrolls - a list with a scroll box of its own inside it traps the finger
  await assert.rejects(scene(without(CSS, /html\[data-layout="phone"\] \.nr-groups \{[^}]*\}/), "startCheck"), /no max-height on the list/);
  // (f) the touch hit area of a line that is only text (the toggle of what a review changed)
  const toggle = await scene(without(CSS, /html\[data-input="touch"\] \.review-changes-toggle \{[^}]*\}/), "panelReview");
  assert.ok(found(toggle, "small").some((s) => s.startsWith("button.review-changes-toggle")), "a 30 px line of text is reported");
  // (g) a long file name / id in a question or a notice: without the rule that lets it break, the audit names every place that prints one
  const clipped = await scene(without(CSS, /#dialog-body,\s*\.settings-col-body,\s*\.detail-editors \{[^}]*\}/), "longNames");
  const names = found(clipped, "longText");
  assert.equal(names.filter((s) => s.startsWith("strong ") && /my-really-long-contract-/.test(s)).length, 3, "the file name in the questions of a contract, a scenario and a .arenabot");
  assert.ok(names.some((s) => s.startsWith("strong ") && /game-a1b2c3d4e5f6-a-game/.test(s)), "the name of the .arenagame file in the conflict question");
  assert.ok(names.some((s) => s.startsWith("code ") && /a1b2c3d4e5f6-a-game-id/.test(s)), "the id of the game in the conflict question");
  assert.ok(names.some((s) => s.startsWith("div.form-hint#settings-games-notice ") && /1b8cc3d4e5f6-a-game-id/.test(s)), "and the notice of the column that prints the id of the game that came in");
  // ... and a short name is never a problem
  assert.deepEqual(found(await scene(CSS, "initDialogs"), "longText"), []);
});

H.runTests(tests);
