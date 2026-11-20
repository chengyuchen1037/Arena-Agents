"use strict";
// v0.14 (task-27): the settings dialog in five columns (static/js/settings.js; archive.js draws column ① inside it) against the fake DOM and the fake backend:
//   ① 总存档   (the wizards: test_archive_ui.js)
//   ② 底层设定  the L0 of every bot: clear all / one, per row avatar · name · state · characters · time, expanded: preview, .md out / in, 生成
//   ③ 官方契约  source · time · characters · hash, the faults of a contract that cannot be used, .md out / in, back to the default
//   ④ 规则     one row per scenario (.md out / in, back to the default) and its study materials (add, export, delete)
//   ⑤ 对局管理  the games: state, winner, time, cost, review x/12, size; expanded: players, review, files; delete, .arenagame out / in (an id that exists:
//              copy / overwrite / cancel); a way to the history dialog and back
// Every file of these columns comes through the API client with the token as a HEADER (a link cannot send one): the tests run with an access token.
// Run: node tests/frontend/test_settings_ui.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const H = require("./harness.js");

const Settings = require(path.join(H.ROOT, "static", "js", "settings.js"));
const M = require("./markup.js");
const { F, target, settle, bootLive } = H;
const tests = [];
// Every test also checks the markup the page drew by the end of it: the fake DOM never parses it, so a stray </div> or an id that occurs twice would pass unseen.
const envs = [];
const test = (name, fn) =>
  tests.push([
    name,
    async () => {
      envs.length = 0;
      await fn();
      for (const env of envs) assert.deepEqual(M.envProblems(env, H.HTML_SRC), [], `the markup drawn in "${name}" is well formed`);
    },
  ]);

const TOKEN = "tok-settings";
async function live(over = {}, overrides = {}, envOpts = {}) {
  const session = F.makeSession({ event_id: 500, status: "paused", ...over });
  const ctx = await bootLive({ session, requireToken: TOKEN, ...overrides }, { storedToken: TOKEN, ...envOpts });
  envs.push(ctx.env);
  ctx.env.sse("snapshot", { session }, 500);
  await flushAll(ctx.env, 20);
  return ctx;
}
const flushAll = async (env, ms = 30) => {
  await settle(ms);
  env.flushRaf();
  await settle(ms);
  env.flushRaf();
};
const html = (env, id) => env.el(id).innerHTML;
const textOf = (s) => s.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
const colText = (env, col) => textOf(html(env, `settings-col-body-${col}`));
const colHtml = (env, col) => html(env, `settings-col-body-${col}`);
const lastToast = (env) => (env.toasts.length ? env.toasts[env.toasts.length - 1].textContent : "");
const toasts = (env) => env.toasts.map((t) => t.textContent);
const dialogOpen = (env) => env.el("dialog-modal").style.display === "flex";
const dialogText = (env) => textOf(html(env, "dialog-body"));
/** The text of the dialog with every blank taken out: a <strong> in the middle of a sentence adds blanks around itself (the markup is what the page has). */
const said = (env) => dialogText(env).replace(/\s+/g, "");
const ns = (s) => s.replace(/\s+/g, "");
/** What the page's code made (it runs in another realm: deepEqual wants the test's own arrays and objects). */
const plain = (x) => JSON.parse(JSON.stringify(x));
const dialogOk = (env) => env.el("btn-dialog-ok").dispatch("click");
const dialogCancel = (env) => env.el("btn-dialog-cancel").dispatch("click");
const modalOpen = (env) => env.el("settings-modal").style.display === "flex";
const act = (env, action, attrs = {}) => env.el("settings-body").dispatch("click", { target: target({ "[data-settings-action]": { "data-settings-action": action, ...attrs } }) });
const toggle = (env, col) => act(env, "toggle-col", { "data-col": col });
const file = (name, text, extra = {}) => ({ name, size: Buffer.byteLength(text || "x"), text: async () => text, ...extra });
const pick = (env, f) => env.el("file-settings-import").dispatch("change", { target: { files: [f], value: "C:\\fakepath\\" + f.name } });
const getsOf = (backend, suffix) => backend.calls.filter((c) => c.method === "GET" && c.url.endsWith(suffix));
const stripAscii = (s) => s.replace(/&amp;/g, "&");
async function openSettings(env, columns = []) {
  env.el("btn-open-settings-modal").dispatch("click");
  await flushAll(env);
  for (const c of columns) {
    if (!env.app().settings().openState()[c]) toggle(env, c);
  }
  await flushAll(env);
}
const CONTRACT_MD = (extra = "") =>
  `---\nformat: arena-contract\nversion: v9.9\n---\n# 平台契约\n\n## 必读\n\n你只能通过工具影响这个世界。\n\n## 参考\n\n工具：${["speak", "speak_template", "pass", "respond", "use_ability", "challenge_moderator", "sleep", "write_note", "read_notes", "compact_context", "jot", "read_contract"].map((x) => `\`${x}\``).join("、")}。回执码：${["NO_FLOOR", "NOT_MEMBER", "CHANNEL_CLOSED", "QUOTA_EXCEEDED", "TOO_LONG", "FORMAT", "STALE", "REQUEST_CLOSED", "ABILITY_DISABLED", "GAME_NOT_RUNNING", "INVALID_PARAMS", "INVALID_TARGET", "UNKNOWN_CHANNEL", "UNKNOWN_TEMPLATE", "UNKNOWN_ABILITY", "UNKNOWN_REQUEST", "UNKNOWN_PLAYER", "UNKNOWN_TICKET", "FORBIDDEN", "CONSTRAINT", "ALREADY_EXISTS", "INTERNAL_ERROR", "NOT_FINISHED"].map((x) => `\`${x}\``).join("、")}。\n${extra}`;
const SCENARIO_MD = (id = "werewolf_12p", version = "3.0.0") =>
  `---\nschema_version: 2\nid: ${id}\nversion: ${version}\ntitle: 导入的狼人杀\nmanual_heading: 主持人手册\n---\n# 剧本\n\n## 玩家规则\n\n规则。\n\n## 主持人手册\n\n按流程主持。\n`;
const CORE_MD = (identity, voice, mind) => `---\nformat: arena-core\nversion: 1\n---\n# 身份\n${identity}\n\n# 说话方式\n${voice}\n\n# 分析方式\n${mind}\n`;

// =====================================================================================================
// pure helpers
// =====================================================================================================
test("which columns are open is read from what localStorage holds: column ① the first time, a damaged value too", () => {
  assert.deepEqual(Settings.parseOpen(null), { archive: true });
  assert.deepEqual(Settings.parseOpen(""), { archive: true });
  assert.deepEqual(Settings.parseOpen("not json"), { archive: true });
  assert.deepEqual(Settings.parseOpen("[1]"), { archive: true });
  assert.deepEqual(Settings.parseOpen('{"archive":false,"games":true,"weird":true,"core":"yes"}'), { archive: false, games: true });
  assert.equal(Settings.OPEN_KEY, "arena_settings_open_v1");
  assert.deepEqual(Settings.COLUMNS.map((c) => c.id), ["archive", "core", "contract", "rules", "games"]);
  assert.deepEqual(Settings.COLUMNS.map((c) => c.title), ["总存档", "底层设定", "官方契约", "规则", "对局管理"]);
});

test("a column can be named by its id or by the name a caller knows it by (\"bots\" is the L0 column)", () => {
  assert.equal(Settings.columnOf("games"), "games");
  assert.equal(Settings.columnOf("bots"), "core");
  assert.equal(Settings.columnOf("l0"), "core");
  assert.equal(Settings.columnOf("export"), "archive");
  assert.equal(Settings.columnOf("history"), "games");
  assert.equal(Settings.columnOf("nonsense"), "");
  assert.equal(Settings.columnOf(undefined), "");
});

test("sizes, hashes and slugs the way the columns say them", () => {
  assert.equal(Settings.formatSize(512), "512 B");
  assert.equal(Settings.formatSize(1536), "1.5 KB");
  assert.equal(Settings.formatSize(91000), "89 KB");
  assert.equal(Settings.formatSize(2400000), "2.3 MB");
  assert.equal(Settings.formatSize(30 * 1048576), "30 MB");
  assert.equal(Settings.formatSize(3 * 1073741824), "3.0 GB");
  assert.equal(Settings.formatSize(-1), "—");
  assert.equal(Settings.formatSize("x"), "—");
  assert.equal(Settings.shortHash("0123456789abcdef0123"), "0123456789ab…");
  assert.equal(Settings.shortHash(""), "—");
  assert.equal(Settings.slugOfFileName("C:\\docs\\Opening Moves (v2).md"), "opening-moves-v2");
  assert.equal(Settings.slugOfFileName("开局.md"), "", "nothing usable: the user types one");
  assert.equal(Settings.slugOfFileName("a".repeat(40) + ".md").length, 32);
  assert.equal(Settings.slugOfFileName("--x--.txt"), "x");
});

test("a row of the games list: status, winner, time, cost, review x/12, size; what can be done with it", () => {
  const Core = H.Core;
  const row = (g, live) => Settings.gameRow({ session_id: "g1", scenario_title: "狼人杀 12 人局", status: "finished", winner: "好人阵营", created_at: "2026-11-17T09:00:00Z", duration_gt: 1523, cost_usd: 0.5, size_bytes: 91000, log_bytes: 2400000, readable: true, review: { done: 5, total: 12 }, participants_brief: ["A"], ...g }, live || { status: "idle" }, Core);
  let r = row({});
  assert.equal(r.statusText, "已结束 · 好人阵营 获胜");
  assert.equal(r.duration, "用时 25:23");
  assert.equal(r.cost, "$0.50");
  assert.equal(r.reviewText, "复盘 5/12");
  assert.equal(r.size, "2.4 MB（日志 2.3 MB）", "the game's file and its logs together, then the logs");
  assert.deepEqual([r.canDelete, r.canExport], [true, true]);
  r = row({ review: { done: 0, total: null }, schema_version: 5 });
  assert.equal(r.reviewText, "复盘 —");
  assert.match(r.reviewTitle, /旧版（v5）存档/);
  r = row({ readable: false, status: "idle" });
  assert.deepEqual([r.unreadable, r.statusText, r.canDelete, r.canExport, r.size], [true, "无法读取", true, false, ""], "an unreadable save can still be deleted, not exported");
  assert.match(r.exportTitle, /无法读取/);
  r = row({ current: true, status: "running" }, { status: "running" });
  assert.deepEqual([r.canDelete, r.current], [false, true]);
  assert.match(r.deleteTitle, /进行中的对局不能删除，请先暂停。/);
  r = row({ current: true, status: "idle" }, { status: "paused" });
  assert.equal(r.statusText, "已暂停", "the current game's status is the live one");
  assert.equal(r.canDelete, true);
  assert.match(r.deleteTitle, /删除后会新建一局空白对局/);
});

// =====================================================================================================
// the frame: five columns, collapsible, remembered
// =====================================================================================================
test("the dialog has five columns in the fixed order; ① is open the first time, the others are closed; the meta line of a column says what is in it", async () => {
  const { env } = await live({}, { botsPreset: "mixed" });
  await openSettings(env);
  assert.ok(modalOpen(env));
  const body = html(env, "settings-body");
  assert.deepEqual([...body.matchAll(/id="settings-col-(\w+)" data-col/g)].map((m) => m[1]), ["archive", "core", "contract", "rules", "games"]);
  const heads = textOf(body);
  assert.match(heads, /① 总存档[^]*② 底层设定[^]*③ 官方契约[^]*④ 规则[^]*⑤ 对局管理/);
  assert.deepEqual(["archive", "core", "contract", "rules", "games"].map((c) => env.el(`settings-col-body-${c}`).hidden), [false, true, true, true, true]);
  assert.match(body, /id="btn-archive-export"/, "column ① holds the export and the import of the archive");
  assert.match(body, /id="btn-archive-import"/);
  assert.deepEqual(["core", "contract", "rules", "games"].map((c) => env.el(`settings-col-meta-${c}`).textContent), ["11/12 完整", "默认 · v0.14.0", "1 个剧本 · 0 份材料", "3 局"]);
  assert.equal(env.el("settings-col-head-archive").getAttribute("aria-expanded"), "true");
  assert.equal(env.el("settings-col-head-core").getAttribute("aria-expanded"), "false");
});

test("a column opens and closes by its head; which are open is stored in localStorage arena_settings_open_v1 and comes back after a reload", async () => {
  const { env } = await live();
  await openSettings(env);
  toggle(env, "core");
  toggle(env, "games");
  toggle(env, "archive");
  assert.deepEqual(JSON.parse(env.storage.get("arena_settings_open_v1")), { archive: false, core: true, games: true });
  assert.deepEqual(["archive", "core", "games"].map((c) => env.el(`settings-col-body-${c}`).hidden), [true, false, false]);
  assert.equal(env.el("settings-col-head-core").getAttribute("aria-expanded"), "true");
  assert.equal(env.el("settings-col-caret-core").textContent, "▼");
  assert.equal(env.el("settings-col-caret-archive").textContent, "▶");
  assert.deepEqual(plain(env.app().settings().openState()), { archive: false, core: true, games: true });
  // a page that comes up again with that stored
  const again = await live({}, {}, { storage: { arena_settings_open_v1: env.storage.get("arena_settings_open_v1") } });
  await openSettings(again.env);
  assert.deepEqual(["archive", "core", "contract", "rules", "games"].map((c) => again.env.el(`settings-col-body-${c}`).hidden), [true, false, true, true, false]);
  // a damaged value: ① open
  const broken = await live({}, {}, { storage: { arena_settings_open_v1: "{{" } });
  await openSettings(broken.env);
  assert.equal(broken.env.el("settings-col-body-archive").hidden, false);
});

test("opening the dialog reads what the columns show (also of the closed ones: for the line next to their title); a column needs no second read when it opens", async () => {
  const { env, backend } = await live();
  await openSettings(env);
  for (const suffix of ["/api/contract", "/api/rules", "/api/games"]) assert.equal(getsOf(backend, suffix).length, 1, suffix);
  toggle(env, "contract");
  toggle(env, "rules");
  toggle(env, "games");
  await flushAll(env);
  for (const suffix of ["/api/contract", "/api/rules", "/api/games"]) assert.equal(getsOf(backend, suffix).length, 1, `${suffix}: opening a column does not read again`);
  env.el("btn-close-settings-modal").dispatch("click");
  await openSettings(env);
  assert.equal(getsOf(backend, "/api/games").length, 2, "but opening the dialog again does");
});

test("a column that cannot be read says so and can be read again", async () => {
  const { env, backend } = await live({}, { failures: { "GET /api/contract": [500, "boom"] } });
  await openSettings(env, ["contract"]);
  assert.match(colText(env, "contract"), /读取官方契约失败/);
  delete backend.failures["GET /api/contract"];
  act(env, "contract-refresh");
  await flushAll(env);
  assert.match(colText(env, "contract"), /来源 默认（随程序发布）/);
});

test("the other columns have no say in the wizards of ①: an export result keeps the columns around it", async () => {
  const { env } = await live();
  await openSettings(env);
  env.el("settings-footer").dispatch("click", { target: target({ "[data-action]": { "data-action": "export-start" } }) });
  assert.match(env.el("settings-title").textContent, /导出存档/, "the wizard takes the dialog over");
  assert.ok(!html(env, "settings-body").includes("settings-col-"), "(its views are not columns)");
  env.el("settings-footer").dispatch("click", { target: target({ "[data-action]": { "data-action": "back-home" } }) });
  await flushAll(env);
  assert.match(html(env, "settings-body"), /id="settings-col-games"/, "back at the columns");
  assert.equal(env.el("settings-col-body-archive").hidden, false);
});

// =====================================================================================================
// ② 底层设定
// =====================================================================================================
test("② one row per bot: avatar, name, seat, initialization mark, whether the L0 is complete, the characters of each section, when it was changed", async () => {
  const { env } = await live({}, { botsPreset: "mixed" });
  await openSettings(env, ["core"]);
  const rows = colHtml(env, "core").split('<li class="settings-row').slice(1);
  assert.equal(rows.length, 12);
  const alice = textOf(rows[0]);
  assert.match(alice, /Alice N1 ✔ 完整 身份 26 · 说话方式 47 · 分析方式 25 · 附加 1 份 \d{4}-\d\d-\d\d \d\d:\d\d/);
  assert.match(rows[0], /<img class="avatar-img"/);
  assert.match(textOf(rows[1]), /Bob N2 ⚠ 完整/);
  assert.match(textOf(rows[2]), /Carol N3 ✖ 完整/);
  assert.match(textOf(rows[5]), /Frank N6 ○ 缺少：身份、说话方式、分析方式 身份 0 · 说话方式 0 · 分析方式 0/, "an empty L0: what is missing, and no time");
  assert.doesNotMatch(textOf(rows[5]), /\d{4}-\d\d-\d\d/);
  assert.equal(env.el("settings-col-meta-core").textContent, "11/12 完整");
  assert.match(colText(env, "core"), /12 个玩家的底层设定（身份 \/ 说话方式 \/ 分析方式 \/ 附加）。名字、头像和记忆不受影响。/);
});

/** The declarations of the LAST rule of style.css with exactly this selector (a rule at the start of a line), as {property: value}; null when there is none. */
function cssRule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const found = [...H.CSS_SRC.replace(/\r\n/g, "\n").matchAll(new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`, "g"))];
  if (!found.length) return null;
  const out = {};
  for (const line of found[found.length - 1][1].split(";")) {
    const at = line.indexOf(":");
    if (at > 0) out[line.slice(0, at).trim()] = line.slice(at + 1).trim();
  }
  return out;
}

test("② the 清除 of a row is a quiet text button in the head of the row - one line per bot, not twelve red boxes; 全部清除… is a thin red outline", async () => {
  const { env } = await live({}, { botsPreset: "ready" });
  await openSettings(env, ["core"]);
  const rows = colHtml(env, "core").split('<li class="settings-row').slice(1);
  assert.equal(rows.length, 12);
  for (const row of rows) {
    // the head is ONE element that holds the toggle and the clear button (the clear button is a sibling of the toggle: a button in a button is no HTML)
    const head = row.split('<div class="settings-row-head">')[1].split("</div>")[0];
    assert.ok(head.includes('class="settings-row-main"') && head.includes('data-settings-action="core-clear"'), "toggle and clear are on the same line");
    assert.match(head, /<button type="button" class="btn-quiet danger" data-settings-action="core-clear" data-bot="b-[0-9a-f]{8}"/);
    assert.ok(head.indexOf('data-settings-action="core-toggle"') < head.indexOf('data-settings-action="core-clear"'), "the clear button is at the right end");
    assert.ok(!row.includes("srow-line") && !row.includes("btn-danger-outline"), "no line of its own, no red box");
  }
  assert.ok(!colHtml(env, "core").includes("btn-danger-outline"), "not a single red box among the rows");
  assert.match(colHtml(env, "core"), /<button type="button" class="btn btn-xs btn-danger-soft" id="btn-settings-core-clear-all"/);
  // the style: text only (no box, no fill), muted; the pointer makes it dark - a danger one red; a finger gets the room of a button
  const quiet = cssRule(".btn-quiet");
  assert.equal(quiet.background, "transparent");
  assert.equal(quiet.border, "1px solid transparent");
  assert.equal(quiet.color, "var(--text-muted)");
  assert.ok(parseFloat(quiet["font-size"]) <= 13, "small");
  assert.equal(cssRule(".btn-quiet.danger:hover:not(:disabled),\n.btn-quiet.danger:focus-visible").color, "var(--danger-text)", "red under the pointer");
  assert.ok(!("color" in (cssRule(".btn-quiet.danger") || {})), "and not before");
  assert.equal(cssRule('html[data-input="touch"] .btn-quiet')["min-height"], "var(--tap)");
  // the row: the toggle takes the line, the actions sit at its right end and wrap under it on a narrow one
  assert.equal(cssRule(".settings-row-head").display, "flex");
  assert.equal(cssRule(".settings-row-head")["flex-wrap"], "wrap");
  assert.match(cssRule(".settings-row-head > .settings-row-main").flex, /^1 1 \d+px$/);
  assert.equal(cssRule(".srow-actions")["margin-left"], "auto");
  // the thin outline of 全部清除…
  assert.equal(cssRule(".btn-danger-soft")["border-color"], "var(--danger-border)");
  assert.equal(cssRule(".btn-danger-soft").background, "transparent");
  assert.ok(!/border:\s*1\.5px/.test(JSON.stringify(cssRule(".btn-danger-soft"))), "no thick border");
});

test("② a row opens to a preview of the merged file (read from the server), with the .md buttons; it closes again", async () => {
  const { env, backend } = await live();
  await openSettings(env, ["core"]);
  act(env, "core-toggle", { "data-bot": F.botId(1) });
  await flushAll(env);
  assert.equal(getsOf(backend, `/api/bots/${F.botId(1)}/core`).length, 1);
  const pre = (html(env, "settings-col-body-core").match(/id="settings-core-preview-[^"]*">([^<]*)</) || [])[1];
  assert.match(pre.replace(/&#39;/g, "'"), /^---\nformat: arena-core\nversion: 1\nname: Alice\n---\n# 身份\n打了二十多年线下局的老玩家/);
  assert.match(pre, /# 附加：口头禅/);
  for (const label of ["导出 .md", "导入 .md…", "✨ 生成 / 完善…", "从默认库套用…"]) assert.ok(colText(env, "core").includes(label), label);
  assert.ok(!/data-bot="b-00001001"[^>]*>清除/.test(colHtml(env, "core").split("settings-row-body")[1] || ""), "(清除 is in the row itself, not in the opened part: H37)");
  assert.match(colHtml(env, "core"), /aria-expanded="true"/);
  act(env, "core-toggle", { "data-bot": F.botId(1) });
  assert.ok(!colHtml(env, "core").includes("settings-core-preview-"), "closed again");
});

test("② 导出 .md saves the merged file under the server's name - fetched with the access token (a link could not send it)", async () => {
  const { env, backend } = await live();
  await openSettings(env, ["core"]);
  act(env, "core-toggle", { "data-bot": F.botId(2) });
  await flushAll(env);
  act(env, "core-export", { "data-bot": F.botId(2) });
  await flushAll(env);
  const call = getsOf(backend, `/api/bots/${F.botId(2)}/core?download=1`)[0];
  assert.ok(call, "requested");
  assert.equal(call.headers["X-Arena-Token"], TOKEN, "the token travels as a header");
  assert.equal(env.downloads.length, 1);
  assert.equal(env.downloads[0].download, "Bob.core.md");
  assert.match(env.downloads[0].href, /^blob:/, "saved from memory, not through a link to the API");
});

test("② 导入 .md: a file is read, the question names it, then the L0 is replaced; a file that is none lists what is wrong in the row", async () => {
  const { env, backend } = await live({}, { botsPreset: "ready" });
  await openSettings(env, ["core"]);
  act(env, "core-toggle", { "data-bot": F.botId(3) });
  await flushAll(env);
  let clicked = 0;
  env.el("file-settings-import").click = () => clicked++;
  act(env, "core-import", { "data-bot": F.botId(3) });
  assert.equal(clicked, 1, "the hidden file input opens");
  assert.match(env.el("file-settings-import").accept, /\.md/);
  pick(env, file("carol.core.md", CORE_MD("新的身份", "新的说话", "新的分析")));
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.match(said(env), /将用文件carol\.core\.md里的内容替换Carol的底层设定/);
  assert.match(dialogText(env), /将变为需重新初始化，记忆保留。/, "the bot is initialized");
  dialogOk(env);
  await flushAll(env);
  assert.equal(backend.corePuts.length, 1);
  assert.equal(backend.botsApi.detail("N3").core.sections.identity, "新的身份");
  assert.match(colText(env, "core"), /已导入：carol\.core\.md/);
  assert.match(colText(env, "core"), /# 身份 新的身份/, "the preview shows the new text");
  assert.match(textOf(colHtml(env, "core").split('<li class="settings-row')[3]), /Carol N3 ⚠/, "and the bot is ⚠ now");
  // a file that is no L0
  act(env, "core-import", { "data-bot": F.botId(3) });
  pick(env, file("junk.md", "随便写点什么"));
  await flushAll(env);
  assert.ok(!dialogOpen(env), "nothing to confirm");
  assert.equal(backend.corePuts.length, 1);
  assert.match(colText(env, "core"), /缺少文件头：文件开头要有用 --- 包住的 format: arena-core 和 version: 1。/);
  assert.match(colText(env, "core"), /「junk\.md」不是合法的底层设定文件。/);
});

test("② 清除: one bot (the fixed sentence of §13.10) or all of them; nothing happens on cancel; the bots turn ⚠ and cannot start", async () => {
  const { env, backend } = await live({}, { botsPreset: "ready" });
  await openSettings(env, ["core"]);
  act(env, "core-clear", { "data-bot": F.botId(2) });
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.equal(env.el("dialog-title").textContent, "清除 Bob 的底层设定");
  assert.equal(said(env), ns("将清空Bob的底层设定。清除后需要重新生成，否则无法开始对局；已有的初始化会变为需重新初始化。"));
  assert.match(env.el("btn-dialog-ok").className, /btn-danger-outline/);
  dialogCancel(env);
  await flushAll(env);
  assert.equal(backend.clearCalls, undefined);
  act(env, "core-clear", { "data-bot": F.botId(2) });
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.clearCalls, [{ key: "POST /api/bots/core:clear", ids: [F.botId(2)] }]);
  assert.match(lastToast(env), /Bob 的底层设定已清除/);
  const rows = colHtml(env, "core").split('<li class="settings-row').slice(1);
  assert.match(textOf(rows[1]), /Bob N2 ⚠ 缺少：身份、说话方式、分析方式/);
  assert.match(textOf(rows[0]), /Alice N1 ✔ 完整/, "the others are untouched");
  assert.equal(env.app().state.session.bots.ready.ok, false);
  // all
  act(env, "core-clear-all");
  await flushAll(env);
  assert.equal(env.el("dialog-title").textContent, "清除全部底层设定");
  assert.equal(said(env), ns("将清空全部 12 个玩家的底层设定。清除后需要重新生成，否则无法开始对局；已有的初始化会变为需重新初始化。"));
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.clearCalls[1], { key: "POST /api/bots/core:clear", ids: null });
  assert.equal(colHtml(env, "core").split('<li class="settings-row').slice(1).every((r) => /缺少：身份、说话方式、分析方式/.test(textOf(r))), true);
  assert.match(html(env, "settings-col-body-core"), /id="btn-settings-core-clear-all"[^>]* disabled/, "nothing left to clear");
  assert.equal(env.el("settings-col-meta-core").textContent, "0/12 完整");
});

test("② while a bot is being initialized its L0 cannot be changed here (the server would answer 409 init_busy)", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "ready" });
  backend.botsApi.state.initJob = { job_id: "j", ids: [F.botId(2)], started_at: 1, states: {} };
  env.sse("bots.updated", { bots: backend.botsApi.live() });
  await openSettings(env, ["core"]);
  act(env, "core-toggle", { "data-bot": F.botId(2) });
  await flushAll(env);
  assert.match(colHtml(env, "core"), /data-settings-action="core-import"[^>]* disabled/);
  assert.match(colHtml(env, "core"), /data-settings-action="core-clear" data-bot="b-00001002" disabled/);
});

test("② 生成 / 完善 steps aside for the player's panel with the L0 editor open (the notes box and the draft that is read first are there); 从默认库套用 opens its library", async () => {
  const { env } = await live({ status: "idle" }, { botsPreset: "fresh" });
  await openSettings(env, ["core"]);
  act(env, "core-toggle", { "data-bot": F.botId(4) });
  await flushAll(env);
  act(env, "core-gen", { "data-bot": F.botId(4) });
  await flushAll(env);
  assert.ok(!modalOpen(env), "the settings dialog is closed");
  assert.equal(env.app().bots().openId(), "N4");
  assert.match(html(env, "detail-editors"), /<section class="detail-editor open" data-kind="core">/);
  assert.match(html(env, "detail-editors"), /id="btn-core-refine"/);
  // the default library: the panel of the bot at the section with the picker
  await openSettings(env, ["core"]);
  act(env, "core-toggle", { "data-bot": F.botId(5) });
  await flushAll(env);
  act(env, "core-default", { "data-bot": F.botId(5) });
  await flushAll(env);
  assert.ok(!modalOpen(env));
  assert.equal(env.app().bots().openId(), "N5");
  assert.match(html(env, "detail-editors"), /<section class="detail-editor open" data-kind="transfer">/);
  assert.match(html(env, "detail-editors"), /id="sel-default-bot"/);
  // once the game runs the library cannot be used: the button says so
  const running = await live({ status: "running" });
  await openSettings(running.env, ["core"]);
  act(running.env, "core-toggle", { "data-bot": F.botId(5) });
  await flushAll(running.env);
  assert.match(colHtml(running.env, "core"), /data-settings-action="core-default"[^>]* disabled[^>]*title="对局开始之后不能从默认库导入"/);
});

// =====================================================================================================
// ③ 官方契约
// =====================================================================================================
test("③ the contract: where it comes from, its version, characters (and the must-read part against its limit), its hash; a check that passes says so", async () => {
  const { env, backend } = await live();
  await openSettings(env, ["contract"]);
  const c = backend.botsApi.contractState;
  const text = colText(env, "contract");
  assert.match(text, /来源 默认（随程序发布）/);
  assert.match(text, /版本 v0\.14\.0/);
  assert.match(text, new RegExp(`字数 ${c.markdown.length} 字 · 必读 \\d+ / 1200`));
  assert.match(text, /SHA-256 [0-9a-f]{12}…/);
  assert.match(text, /契约检查通过：所有工具名和回执码都在里面，必读部分没有超长。/);
  assert.match(colHtml(env, "contract"), /data-settings-action="contract-reset"[^>]* disabled/, "the default cannot be reset to itself");
  assert.ok(!colHtml(env, "contract").includes("settings-contract-preview"), "no preview until asked");
  act(env, "contract-toggle");
  assert.match(colHtml(env, "contract"), /id="settings-contract-preview">---\nformat: arena-contract/);
  assert.match(colText(env, "contract"), /收起预览/);
  act(env, "contract-toggle");
  assert.ok(!colHtml(env, "contract").includes("settings-contract-preview"));
});

test("③ 导出 saves the contract (token header, the server's file name)", async () => {
  const { env, backend } = await live();
  await openSettings(env, ["contract"]);
  act(env, "contract-export");
  await flushAll(env);
  assert.equal(getsOf(backend, "/api/contract?download=1")[0].headers["X-Arena-Token"], TOKEN);
  assert.equal(env.downloads.at(-1).download, "contract-v0.14.0.md");
});

test("③ 导入: the question carries the fixed sentence (all bots become ⚠), the contract is replaced, the cards say ⚠ and the column says imported; 恢复默认 brings the old one back", async () => {
  const { env, backend } = await live({}, { botsPreset: "ready" });
  await openSettings(env, ["contract"]);
  act(env, "contract-import");
  pick(env, file("my-contract.md", CONTRACT_MD()));
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.equal(env.el("dialog-title").textContent, "导入官方契约");
  assert.match(said(env), /将用文件my-contract\.md（\d+字）替换官方契约。所有bot将变为需重新初始化。/);
  dialogCancel(env);
  await flushAll(env);
  assert.equal(backend.contractPuts, undefined, "no: nothing was sent");
  act(env, "contract-import");
  pick(env, file("my-contract.md", CONTRACT_MD()));
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.equal(backend.contractPuts.length, 1);
  assert.match(colText(env, "contract"), /来源 导入的文件/);
  assert.match(colText(env, "contract"), /版本 v9\.9/);
  assert.match(colText(env, "contract"), /已导入：my-contract\.md。所有 bot 将变为需重新初始化。/);
  assert.match(lastToast(env), /官方契约已导入/);
  assert.equal(env.el("settings-col-meta-contract").textContent, "已导入 · v9.9");
  assert.deepEqual(plain(env.app().state.session.participants.map((p) => p.init_status)), Array(12).fill("stale"), "every card is ⚠ (the bots were read again)");
  assert.match(env.app().state.session.bots.bots[0].init.stale.join(), /contract/);
  // back to the default
  assert.match(colHtml(env, "contract"), /id="btn-settings-contract-reset"(?![^>]*disabled)/);
  act(env, "contract-reset");
  await flushAll(env);
  assert.equal(env.el("dialog-title").textContent, "恢复默认契约");
  assert.equal(said(env), ns("将丢掉导入的契约，改用随程序发布的默认契约。所有 bot 将变为需重新初始化。"));
  dialogOk(env);
  await flushAll(env);
  assert.equal(backend.contractResets, 1);
  assert.match(colText(env, "contract"), /来源 默认（随程序发布）/);
  assert.match(colHtml(env, "contract"), /id="btn-settings-contract-reset"[^>]* disabled/);
});

test("③ a contract the server cannot use: its faults are listed (what is missing, how long the must-read part is); the old contract stays", async () => {
  const { env, backend } = await live();
  await openSettings(env, ["contract"]);
  act(env, "contract-import");
  pick(env, file("bad.md", "---\nformat: arena-contract\nversion: 1\n---\n## 必读\n短。\n## 参考\n没有任何工具名。\n"));
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  const box = colHtml(env, "contract");
  assert.match(textOf(box), /这份文件没有被采用：/);
  assert.match(textOf(box), /契约没有提到这些工具：speak、speak_template/);
  assert.match(textOf(box), /契约没有提到这些回执码：NO_FLOOR/);
  assert.match(colText(env, "contract"), /来源 默认/, "the contract in force is the old one");
  assert.equal(backend.botsApi.contractState.origin, "default");
  assert.match(lastToast(env), /契约没有保存/);
  // an unreadable file
  act(env, "contract-import");
  pick(env, { name: "x.md", size: 1, text: async () => { throw new Error("读取失败"); } });
  await flushAll(env);
  assert.match(colText(env, "contract"), /读取失败/);
});

test("③ a contract that the data folder holds in a broken state (edited by hand) lists its faults at once", async () => {
  const { env, backend } = await live();
  backend.botsApi.contractState.broken = "invalid";
  await openSettings(env, ["contract"]);
  assert.match(colText(env, "contract"), /这份契约现在不能用：/);
  assert.match(colText(env, "contract"), /契约没有提到这些工具：jot、read_contract。/);
  assert.equal(env.el("settings-col-meta-contract").textContent, "默认 · v0.14.0 · ⚠ 有问题");
});

// =====================================================================================================
// ④ 规则
// =====================================================================================================
test("④ one row per scenario: title, id, version, origin, characters and hash, and the study materials under it", async () => {
  const { env } = await live();
  await openSettings(env, ["rules"]);
  const text = colText(env, "rules");
  assert.match(text, /剧本文件同时决定主持人手册和游戏机制，所以导入的是整份剧本/);
  assert.match(text, /12 人标准局狼人杀（暗牌局，测试） werewolf_12p · v2\.2\.0 默认 \d+ 字 · [0-9a-f]{12}…/);
  assert.match(text, /学习材料（0 份）/);
  assert.match(text, /这个剧本没有学习材料。/);
  for (const label of ["导出", "导入…", "恢复默认", "＋ 添加学习材料…"]) assert.ok(text.includes(label), label);
  assert.match(colHtml(env, "rules"), /data-settings-action="rules-reset"[^>]* disabled/);
});

test("④ 导出 of a scenario saves its file (token header, the server's name)", async () => {
  const { env, backend } = await live();
  await openSettings(env, ["rules"]);
  act(env, "rules-export", { "data-id": "werewolf_12p" });
  await flushAll(env);
  assert.equal(getsOf(backend, "/api/rules/werewolf_12p?download=1")[0].headers["X-Arena-Token"], TOKEN);
  assert.equal(env.downloads.at(-1).download, "werewolf_12p-2.2.0.scenario.md");
});

test("④ 导入 a scenario: the fixed sentence in the question, the row follows the answer, all bots become ⚠; a file with faults is listed and nothing changes; 恢复默认", async () => {
  const { env, backend } = await live({}, { botsPreset: "ready" });
  await openSettings(env, ["rules"]);
  act(env, "rules-import", { "data-id": "werewolf_12p" });
  pick(env, file("w.scenario.md", SCENARIO_MD()));
  await flushAll(env);
  assert.equal(env.el("dialog-title").textContent, "导入剧本");
  assert.match(said(env), /将用文件w\.scenario\.md（\d+字）替换剧本werewolf_12p。所有bot将变为需重新初始化。/);
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.rulesPuts.map((x) => x.sid), ["werewolf_12p"]);
  assert.match(colText(env, "rules"), /导入的狼人杀 werewolf_12p · v3\.0\.0 导入/);
  assert.match(colText(env, "rules"), /剧本 werewolf_12p 已导入。所有 bot 将变为需重新初始化。/);
  assert.deepEqual(plain(env.app().state.session.participants.map((p) => p.init_status)), Array(12).fill("stale"));
  assert.match(env.app().state.session.bots.bots[0].init.stale.join(), /scenario/);
  // another scenario's file / a file that is none: listed, the scenario stays
  act(env, "rules-import", { "data-id": "werewolf_12p" });
  pick(env, file("other.md", SCENARIO_MD("another")));
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.match(colText(env, "rules"), /剧本文件里的 id 是「another」，与这个剧本的 id「werewolf_12p」不同。/);
  assert.match(colText(env, "rules"), /导入的狼人杀 werewolf_12p · v3\.0\.0/, "the row is unchanged");
  assert.match(lastToast(env), /导入剧本失败/);
  // back to the default
  act(env, "rules-reset", { "data-id": "werewolf_12p" });
  await flushAll(env);
  assert.equal(env.el("dialog-title").textContent, "恢复默认剧本");
  assert.equal(said(env), ns("将丢掉导入的版本，改用随程序发布的默认剧本werewolf_12p。所有 bot 将变为需重新初始化。"));
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.rulesResets, ["werewolf_12p"]);
  assert.match(colText(env, "rules"), /werewolf_12p · v2\.2\.0 默认/);
});

test("④ a scenario the loader cannot read says why (a file edited by hand)", async () => {
  const { env, backend } = await live();
  backend.botsApi.rulesState.werewolf_12p.markdown = "kaputt";
  await openSettings(env, ["rules"]);
  assert.match(colText(env, "rules"), /剧本文件 werewolf_12p\.md 缺少文件头/);
  assert.match(env.el("settings-col-meta-rules").textContent, /⚠ 有问题/);
});

test("④ study materials: 添加 (a file, a slug and a title asked in the dialog), listed, 导出, 删除 - each with the sentence that all bots become ⚠", async () => {
  const { env, backend } = await live({}, { botsPreset: "ready" });
  await openSettings(env, ["rules"]);
  act(env, "mat-add", { "data-id": "werewolf_12p" });
  pick(env, file("Opening Moves.md", "# 开局套路\n先听完警上发言。\n"));
  await flushAll(env);
  assert.equal(env.el("dialog-title").textContent, "添加学习材料");
  assert.match(said(env), /将把文件OpeningMoves\.md（\d+字）作为剧本werewolf_12p的学习材料。/);
  assert.match(said(env), /所有bot将变为需重新初始化。/);
  assert.match(html(env, "dialog-body"), /id="input-material-slug" value="opening-moves"/, "a slug out of the file name");
  env.el("input-material-slug").value = "openings";
  env.el("input-material-title").value = "开局套路";
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.materialPuts.map((x) => [x.sid, x.slug, x.title]), [["werewolf_12p", "openings", "开局套路"]]);
  assert.match(colText(env, "rules"), /学习材料（1 份）/);
  assert.match(colText(env, "rules"), /开局套路 openings · \d+ 字 · [0-9a-f]{12}…/);
  assert.match(colText(env, "rules"), /学习材料 openings 已添加。所有 bot 将变为需重新初始化。/);
  assert.equal(env.el("settings-col-meta-rules").textContent, "1 个剧本 · 1 份材料");
  assert.match(env.app().state.session.bots.bots[0].init.stale.join(), /materials/);
  // export / delete
  act(env, "mat-export", { "data-id": "werewolf_12p", "data-slug": "openings" });
  await flushAll(env);
  assert.equal(getsOf(backend, "/api/rules/werewolf_12p/materials/openings?download=1")[0].headers["X-Arena-Token"], TOKEN);
  assert.equal(env.downloads.at(-1).download, "werewolf_12p-openings.material.md");
  act(env, "mat-delete", { "data-id": "werewolf_12p", "data-slug": "openings" });
  await flushAll(env);
  assert.equal(env.el("dialog-title").textContent, "删除学习材料");
  assert.equal(said(env), ns("将删除剧本werewolf_12p的学习材料openings。所有 bot 将变为需重新初始化。"));
  dialogCancel(env);
  await flushAll(env);
  assert.equal(backend.materialDeletes, undefined);
  act(env, "mat-delete", { "data-id": "werewolf_12p", "data-slug": "openings" });
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.materialDeletes, [{ sid: "werewolf_12p", slug: "openings" }]);
  assert.match(colText(env, "rules"), /学习材料（0 份）/);
});

test("④ a material with a slug the server would refuse is stopped before it is sent; one the server refuses (too big) is said in the row", async () => {
  const { env, backend } = await live();
  await openSettings(env, ["rules"]);
  act(env, "mat-add", { "data-id": "werewolf_12p" });
  pick(env, file("开局.md", "内容"));
  await flushAll(env);
  assert.match(html(env, "dialog-body"), /id="input-material-slug" value=""/, "nothing usable in a Chinese file name: the user types one");
  env.el("input-material-slug").value = "Bad Slug!";
  dialogOk(env);
  await flushAll(env);
  assert.equal(backend.materialPuts, undefined, "not sent");
  assert.match(colText(env, "rules"), /材料的标识只能用小写字母、数字和连字符（最多 32 个字符，以字母或数字开头）。/);
  act(env, "mat-add", { "data-id": "werewolf_12p" });
  pick(env, file("big.md", "x".repeat(100 * 1024 + 1)));
  await flushAll(env);
  env.el("input-material-slug").value = "big";
  dialogOk(env);
  await flushAll(env);
  assert.match(colText(env, "rules"), /材料太大了：单份最多 100 KB。/);
  assert.match(lastToast(env), /添加材料失败/);
  // twenty are the most
  for (let i = 0; i < 20; i++) backend.botsApi.rulesState.werewolf_12p.materials[`m-${i}`] = { title: `材料 ${i}`, markdown: `内容 ${i}` };
  await env.app().settings().refresh("rules");
  assert.match(colHtml(env, "rules"), /data-settings-action="mat-add"[^>]* disabled/);
});

// =====================================================================================================
// ⑤ 对局管理
// =====================================================================================================
test("⑤ the list: title, 当前, 🎮, status, winner, time, cost, review x/12, size with the log; the current game is the live one", async () => {
  const { env } = await live({ status: "paused" });
  await openSettings(env, ["games"]);
  const rows = colHtml(env, "games").split('<li class="settings-row game-row').slice(1);
  assert.equal(rows.length, 3);
  assert.match(textOf(rows[0]), /狼人杀 12 人局 当前 \d{4}-\d\d-\d\d \d\d:\d\d 已暂停/, "the current game: the live status");
  assert.match(textOf(rows[1]), /狼人杀 12 人局 \d{4}-\d\d-\d\d \d\d:\d\d 已结束 · 好人阵营 获胜 用时 25:23 · \$0\.50 · 复盘 5\/12 · 2\.4 MB（日志 2\.3 MB）/);
  assert.match(textOf(rows[2]), /🎮/, "a person played a seat in this one");
  assert.match(rows[2], /复盘 —/);
  assert.match(rows[2], /title="旧版（v5）存档：没有 bot，不能复盘"/);
  assert.equal(env.el("settings-col-meta-games").textContent, "3 局");
  for (const b of ["导入对局…", "刷新", "历史对局（重放 / 继续）…"]) assert.ok(colText(env, "games").includes(b), b);
  // the actions of a row are quiet text buttons in the head of the row, like the 清除 of ② - not a second line of boxes under every game
  for (const row of rows) {
    const head = row.split('<div class="settings-row-head">')[1].split("</div>")[0];
    assert.deepEqual([...head.matchAll(/class="(btn-quiet(?: danger)?)" data-settings-action="(games-\w+)"/g)].map((m) => [m[2], m[1]]), [["games-export", "btn-quiet"], ["games-delete", "btn-quiet danger"], ["games-history", "btn-quiet"]]);
    assert.ok(!row.includes("srow-line") && !row.includes("btn-danger-outline") && !/class="btn btn-xs/.test(row), "no boxes");
  }
});

test("④ a study material's 导出 / 删除 are quiet text buttons at the end of its line", async () => {
  const { env, backend } = await live();
  backend.botsApi.rulesState.werewolf_12p.materials.openings = { title: "开局套路", markdown: "先听完警上发言。" };
  await openSettings(env, ["rules"]);
  const mat = colHtml(env, "rules").split('<li class="settings-row mat-row')[1].split("</li>")[0];
  assert.match(mat, /<span class="srow-actions"><button type="button" class="btn-quiet" data-settings-action="mat-export"[^>]*>导出<\/button><button type="button" class="btn-quiet danger" data-settings-action="mat-delete"[^>]*>删除<\/button><\/span>/);
  assert.ok(!mat.includes("btn-danger-outline") && !mat.includes("btn btn-xs"));
});

test("⑤ a row opens to the players (seat, name, identity and faction once the game is over, review), the files and who was played by a person", async () => {
  const { env, backend } = await live();
  await openSettings(env, ["games"]);
  act(env, "games-toggle", { "data-id": "old-1" });
  await flushAll(env);
  assert.equal(getsOf(backend, "/api/games/old-1/detail").length, 1);
  const detail = textOf(colHtml(env, "games").split('id="settings-game-detail-old-1"')[1].split("</li>")[0]);
  assert.match(detail, /座位 名字 身份 复盘/);
  assert.match(detail, /3 Carol 狼人 狼人阵营 已复盘/);
  assert.match(detail, /12 Leo 白狼王 狼人阵营 未复盘/);
  assert.match(detail, /文件：对局存档 89 KB，日志 2\.3 MB/);
  act(env, "games-toggle", { "data-id": "old-2" });
  await flushAll(env);
  const paused = textOf(colHtml(env, "games").split('id="settings-game-detail-old-2"')[1].split("</li>")[0]);
  assert.match(paused, /3 Carol — —/, "a game that is not over has no identities yet, and nothing to review yet (the server says pending)");
  assert.match(paused, /上一个存档点 20 KB/);
  assert.match(paused, /3 号由人类代打（第 40 – 218 条）/);
  act(env, "games-toggle", { "data-id": "old-1" });
  assert.ok(!colHtml(env, "games").includes("settings-game-detail-old-1"), "closed");
});

test("⑤ 删除: the fixed sentence and the game's name; the list is read again; the colour marks of the game go; the memory of the bots is not touched", async () => {
  const { env, backend } = await live();
  await openSettings(env, ["games"]);
  act(env, "games-delete", { "data-id": "old-1" });
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.equal(env.el("dialog-title").textContent, "删除对局");
  assert.match(said(env), /删除这局的存档和日志。bot已经通过复盘形成的记忆不会被删除，也无法通过删除对局撤销。/);
  assert.match(said(env), /要删除的是：狼人杀12人局（\d{4}-\d\d-\d\d\d\d:\d\d）/);
  assert.match(env.el("btn-dialog-ok").className, /btn-danger-outline/);
  dialogCancel(env);
  await flushAll(env);
  assert.equal(backend.gameDeletes, undefined);
  env.storage.set("arena_marks_v1", JSON.stringify({ "old-1": { N2: "red" }, "s-test": { N3: "blue" } }));
  act(env, "games-delete", { "data-id": "old-1" });
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.gameDeletes, ["old-1"]);
  assert.ok(!colHtml(env, "games").includes('data-game="old-1"'), "gone from the list");
  // (the note of the server is a sentence with its own full stop: inside the bracket it has none - "。）" would be two stops)
  assert.equal(lastToast(env), "对局已删除（bot 已经通过复盘形成的记忆不会被删除，也无法通过删除对局撤销）");
  assert.equal(backend.botsApi.bot("N1").mem.record.games, 7, "the record of the bots is untouched");
  assert.equal(env.el("settings-col-meta-games").textContent, "2 局");
});

test("⑤ the current game can be deleted unless it runs: the question says an empty game takes its place, the page follows the new game", async () => {
  const { env, backend } = await live({ status: "paused" });
  await openSettings(env, ["games"]);
  assert.match(colHtml(env, "games"), /data-id="s-test"[^>]*title="这是当前对局：删除后会新建一局空白对局。"/);
  act(env, "games-delete", { "data-id": "s-test" });
  await flushAll(env);
  assert.match(said(env), /这是当前对局：删除后会新建一局空白对局。/);
  backend.newSessionId = "s-fresh";
  const before = getsOf(backend, "/api/state").length;
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.gameDeletes, ["s-test"]);
  assert.equal(getsOf(backend, "/api/state").length, before + 1, "the page read the state again: the server made another game");
  // a running game cannot be deleted: the button says why and a stale click is refused before anything is asked
  const running = await live({ status: "running" });
  await openSettings(running.env, ["games"]);
  assert.match(colHtml(running.env, "games"), /data-id="s-test"[^>]* disabled[^>]*title="进行中的对局不能删除，请先暂停。"|disabled title="进行中的对局不能删除，请先暂停。"/);
  act(running.env, "games-delete", { "data-id": "s-test" });
  await flushAll(running.env);
  assert.ok(!dialogOpen(running.env));
  assert.equal(running.backend.gameDeletes, undefined);
});

test("⑤ a game whose files are in use cannot be deleted for the moment (409 in_use): the server's words are shown, the game stays; the same for the current game", async () => {
  const { env, backend } = await live({ status: "paused" });
  backend.gameLocks = ["old-2"];
  await openSettings(env, ["games"]);
  act(env, "games-delete", { "data-id": "old-2" });
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.match(colText(env, "games"), /这局的文件现在被占用，暂时删不掉：请稍后再试。/);
  assert.ok(colHtml(env, "games").includes('data-game="old-2"'), "still there");
  assert.match(lastToast(env), /被占用/);
  // the CURRENT game: the server made the empty new game all the same (new_game: true), so the page follows it although the answer is an error
  backend.gameLocks = ["s-test"];
  backend.newSessionId = "s-fresh";
  const reads = getsOf(backend, "/api/state").length;
  act(env, "games-delete", { "data-id": "s-test" });
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.match(colText(env, "games"), /这局的文件现在被占用，暂时删不掉；已经新建了一局空白对局。/);
  assert.equal(getsOf(backend, "/api/state").length, reads + 1, "the state was read again: there is another game now");
  assert.ok(colHtml(env, "games").includes('data-game="old-2"'));
});

test("⑤ an unreadable save (v4, damaged) stays in the list, is marked, can be deleted and cannot be exported or opened", async () => {
  const { env, backend } = await live();
  backend.games.push({ session_id: "old-v4", title: "", scenario_title: "", status: "idle", winner: null, created_at: null, updated_at: "2026-11-01T00:00:00Z", duration_gt: 0, players: 0, tokens: 0, cost_usd: 0, human_involved: false, schema_version: 4, size_bytes: 300, log_bytes: 0, readable: false, review: { done: 0, total: null }, participants_brief: [] });
  await openSettings(env, ["games"]);
  const row = colHtml(env, "games").split('<li class="settings-row game-row').find((r) => r.includes('data-game="old-v4"'));
  assert.match(row, /is-unreadable/);
  assert.match(textOf(row), /无法读取/);
  assert.match(row, /data-settings-action="games-toggle"[^>]* disabled/);
  assert.match(row, /data-settings-action="games-export"[^>]* disabled[^>]*title="这局的存档无法读取，不能导出。"/);
  assert.match(row, /data-settings-action="games-delete"(?![^>]* disabled)/);
});

test("⑤ 导出: the .arenagame is fetched with the token and saved under the server's name; a refusal (the current game while it runs, a save that cannot be opened) is said in the column", async () => {
  const { env, backend } = await live({ status: "paused" });
  await openSettings(env, ["games"]);
  act(env, "games-export", { "data-id": "old-1" });
  await flushAll(env);
  const call = getsOf(backend, "/api/games/old-1/export")[0];
  assert.equal(call.headers["X-Arena-Token"], TOKEN, "a GET of this kind needs the header: a link with ?token= would be refused");
  assert.equal(env.downloads.at(-1).download, "game-old-1-20251119.arenagame");
  assert.match(env.downloads.at(-1).href, /^blob:/);
  // the current game while a seat is played / while it runs
  backend.session.takeover = F.makeTakeover("N3");
  act(env, "games-export", { "data-id": "s-test" });
  await flushAll(env);
  assert.match(colText(env, "games"), /对局进行中或正在接管一个座位，不能导出当前对局：请先暂停并退出接管。/);
  assert.match(lastToast(env), /不能导出当前对局/);
  backend.session.takeover = F.makeTakeover(null);
  act(env, "games-export", { "data-id": "s-test" });
  await flushAll(env);
  assert.equal(env.downloads.length, 2, "paused and nobody takes a seat: it can be exported");
  // a save that cannot be opened
  backend.games.push({ session_id: "old-v4", status: "idle", readable: false, schema_version: 4, size_bytes: 3, log_bytes: 0, review: { done: 0, total: null }, participants_brief: [], title: "", scenario_title: "", players: 0 });
  await env.app().settings().refresh("games");
  await flushAll(env);
  act(env, "games-export", { "data-id": "old-v4" }); // (the button is off; a stale click is guarded by the server too)
  await flushAll(env);
  assert.equal(env.downloads.length, 2);
});

test("⑤ 导入 an .arenagame: a game that is new is added; an id that exists (the current game's too, saved or not) asks - copy (the default) / overwrite / cancel", async () => {
  const { env, backend } = await live({ status: "paused" });
  await openSettings(env, ["games"]);
  act(env, "games-import");
  assert.match(env.el("file-settings-import").accept, /\.arenagame/);
  pick(env, file("game-brand-new-20251119.arenagame", "zip"));
  await flushAll(env);
  assert.deepEqual(backend.gameImports, [{ name: "game-brand-new-20251119.arenagame", size: 3, on_conflict: "ask" }]);
  assert.ok(!dialogOpen(env), "no conflict: no question");
  assert.match(colText(env, "games"), /已导入对局：brand-new/);
  assert.ok(colHtml(env, "games").includes('data-game="brand-new"'));
  assert.equal(env.el("settings-col-meta-games").textContent, "4 局");
  // an id that exists: ask
  act(env, "games-import");
  pick(env, file("game-old-1-20251119.arenagame", "zip"));
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.equal(env.el("dialog-title").textContent, "对局 id 已存在");
  assert.match(said(env), /文件game-old-1-20251119\.arenagame里的对局和已有的一局id相同（old-1）。/);
  assert.match(dialogText(env), /作为副本导入（推荐：两局都保留）/);
  assert.match(dialogText(env), /覆盖已有的那一局（它的存档和日志会被替换）/);
  assert.match(html(env, "dialog-body"), /id="radio-game-copy" value="copy" checked/, "the copy is the default");
  assert.doesNotMatch(html(env, "dialog-body"), /id="radio-game-overwrite" value="overwrite" checked/);
  dialogCancel(env);
  await flushAll(env);
  assert.deepEqual(backend.gameImports.map((x) => x.on_conflict), ["ask", "ask"], "cancel: nothing more is sent");
  // copy (the default of the dialog: the user just presses 导入)
  act(env, "games-import");
  pick(env, file("game-old-1-20251119.arenagame", "zip"));
  await flushAll(env);
  env.el("radio-game-copy").checked = true;
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.gameImports.slice(-2).map((x) => x.on_conflict), ["ask", "copy"]);
  assert.match(colText(env, "games"), /已作为副本导入：old-1-copy1a2b/);
  assert.ok(colHtml(env, "games").includes('data-game="old-1-copy1a2b"') && colHtml(env, "games").includes('data-game="old-1"'), "both are there");
  // overwrite
  act(env, "games-import");
  pick(env, file("game-old-2-20251119.arenagame", "zip"));
  await flushAll(env);
  env.el("radio-game-copy").checked = false; // (the user took the other one)
  dialogOk(env);
  await flushAll(env);
  assert.equal(backend.gameImports.at(-1).on_conflict, "overwrite");
  assert.match(colText(env, "games"), /已覆盖导入：old-2/);
  // the current game's id counts as existing, even when it was never saved: the question says so and offers the copy only (overwrite is shown, off)
  act(env, "games-import");
  pick(env, file("game-s-test-20251119.arenagame", "zip"));
  await flushAll(env);
  assert.ok(dialogOpen(env), "asked");
  assert.match(said(env), /已有的这一局就是当前对局。/);
  assert.match(html(env, "dialog-body"), /id="radio-game-overwrite" value="overwrite" disabled/);
  assert.match(said(env), /覆盖已有的那一局（当前对局不能被覆盖）/);
  env.el("radio-game-copy").checked = false; // (not possible by hand: the other one is off)
  dialogOk(env);
  await flushAll(env);
  assert.equal(backend.gameImports.at(-1).on_conflict, "copy", "a copy, whatever the radio says");
  assert.match(colText(env, "games"), /已作为副本导入：s-test-copy1a2b/);
});

test("⑤ the server is the authority: an overwrite of a game that has become the current one in the meantime is refused in its words (409 current)", async () => {
  const { env, backend } = await live({ status: "paused" });
  await openSettings(env, ["games"]);
  act(env, "games-import");
  pick(env, file("game-old-2-20251119.arenagame", "zip"));
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.doesNotMatch(html(env, "dialog-body"), /id="radio-game-overwrite" value="overwrite" disabled/, "old-2 is not the current game as far as the page knows");
  backend.session.session_id = "old-2"; // (another tab continued that game while the question was open)
  env.el("radio-game-copy").checked = false;
  dialogOk(env);
  await flushAll(env);
  assert.equal(backend.gameImports.at(-1).on_conflict, "overwrite");
  assert.match(colText(env, "games"), /这是当前对局，不能被覆盖：请作为副本导入。/);
  assert.match(lastToast(env), /不能被覆盖/);
});

test("⑤ an .arenagame the server refuses: none (422 invalid_archive), another import or an archive job is running (409 busy), too big (413) - its words, nothing added", async () => {
  const { env, backend } = await live({ status: "paused" });
  await openSettings(env, ["games"]);
  const attempt = async (name) => {
    act(env, "games-import");
    pick(env, file(name, "zip"));
    await flushAll(env);
    return colText(env, "games");
  };
  assert.match(await attempt("bad.arenagame"), /这不是有效的 \.arenagame 文件（不是 zip，或者文件已损坏）。/);
  assert.match(await attempt("busy.arenagame"), /另一个导入 \/ 导出任务正在进行/);
  assert.match(await attempt("big.arenagame"), /\.arenagame 文件超过了 2 GiB 的上限。/);
  assert.equal(env.el("settings-col-meta-games").textContent, "3 局");
  assert.match(lastToast(env), /2 GiB/);
  // the button is off while an import runs
  const gate = backend.hold((key) => key.startsWith("POST /api/games/import"));
  act(env, "games-import");
  pick(env, file("game-slow-20251119.arenagame", "zip"));
  await flushAll(env, 10);
  assert.match(colHtml(env, "games"), /id="btn-settings-games-import"[^>]* disabled/, "the button is off while an import runs");
  gate.release(0);
  await flushAll(env);
  assert.match(colText(env, "games"), /已导入对局：slow/);
});

test("⑤ the way to and from the history dialog: 历史对局… closes this one and opens that one; the history dialog has a button back to 对局管理", async () => {
  const { env } = await live();
  await openSettings(env, ["games"]);
  act(env, "games-history", { "data-id": "old-1" });
  await flushAll(env);
  assert.ok(!modalOpen(env), "the settings dialog stepped aside");
  assert.equal(env.el("history-modal").style.display, "flex");
  assert.ok(H.HTML_SRC.includes('id="btn-history-manage"'));
  env.el("btn-history-manage").dispatch("click");
  await flushAll(env);
  assert.notEqual(env.el("history-modal").style.display, "flex");
  assert.ok(modalOpen(env), "the settings dialog is back ...");
  assert.equal(env.el("settings-col-body-games").hidden, false, "... at 对局管理");
  assert.ok(colHtml(env, "games").includes('data-game="old-1"'));
});

test("the games column follows the live game: when it starts, the current game's delete button goes off", async () => {
  const { env } = await live({ status: "paused" });
  await openSettings(env, ["games"]);
  assert.match(colHtml(env, "games"), /data-id="s-test"(?![^>]*disabled)[^>]*title="这是当前对局/);
  env.sse("game.status", { status: "running", reason: "resumed", winner: null });
  await flushAll(env);
  assert.match(colHtml(env, "games"), /data-settings-action="games-delete" data-id="s-test" disabled/);
  assert.match(colText(env, "games"), /进行中/);
});

// =====================================================================================================
// the start check's jump, the files, the style
// =====================================================================================================
test("Bots.focus(_, import_contract / import_rules) opens the dialog at that column (and keeps it open next time); a wizard result does not hide it", async () => {
  const { env } = await live();
  const Bots = env.sandbox.Bots;
  assert.equal(Bots.focus("", "import_contract"), true);
  await flushAll(env);
  assert.ok(modalOpen(env));
  assert.equal(env.el("settings-col-body-contract").hidden, false);
  assert.match(colText(env, "contract"), /来源 默认/);
  assert.equal(JSON.parse(env.storage.get("arena_settings_open_v1")).contract, true);
  env.el("btn-close-settings-modal").dispatch("click");
  assert.equal(Bots.focus("", "import_rules"), true);
  await flushAll(env);
  assert.equal(env.el("settings-col-body-rules").hidden, false);
  assert.match(colText(env, "rules"), /werewolf_12p/);
  // the dialog shows an export's result: the jump still lands on the columns
  env.el("settings-footer").dispatch("click", { target: target({ "[data-action]": { "data-action": "export-start" } }) });
  assert.match(env.el("settings-title").textContent, /导出存档/);
  assert.equal(Bots.focus("", "import_contract"), true);
  await flushAll(env);
  assert.match(html(env, "settings-body"), /id="settings-col-contract"/);
});

test("ArenaApp.openSettings(column): \"bots\" opens column ②, an unknown name just opens the dialog (at what was open last)", async () => {
  const { env } = await live();
  assert.equal(env.app().openSettings("bots"), true);
  await flushAll(env);
  assert.ok(modalOpen(env));
  assert.equal(env.el("settings-col-body-core").hidden, false);
  assert.match(colText(env, "core"), /Alice N1/);
  env.el("btn-close-settings-modal").dispatch("click");
  assert.equal(env.app().openSettings("nonsense"), true);
  await flushAll(env);
  assert.ok(modalOpen(env));
  assert.equal(env.el("settings-col-body-core").hidden, false, "what was open stays open");
});

test("every file of the columns is fetched through the API client: nothing in the page builds a ?token= link for them", () => {
  const src = fs.readFileSync(path.join(H.ROOT, "static", "js", "settings.js"), "utf8");
  assert.ok(!/\btoken=/.test(src.replace(/\{ token: true \}/g, "")), "settings.js never writes a token into a URL itself");
  const app = fs.readFileSync(path.join(H.ROOT, "static", "js", "app.js"), "utf8");
  assert.ok(/download: downloadFromApi/.test(app), "app.js hands the fetch-and-save function to the panels");
  assert.ok(/api\.download\(path\)/.test(app));
});

test("the confirm dialog is painted ABOVE the settings and the history dialogs (it comes first in the page: with the same z-index as theirs it would be hidden under them)", () => {
  const css = H.CSS_SRC;
  const zOf = (selector) => {
    const found = [...css.matchAll(new RegExp(`(?:^|\\n)${selector.replace(/[.#]/g, "\\$&")}\\s*\\{([^}]*)\\}`, "g"))].map((m) => (/z-index:\s*(\d+)/.exec(m[1]) || [])[1]).filter(Boolean);
    return found.length ? Number(found[found.length - 1]) : null;
  };
  const base = zOf(".modal-backdrop");
  assert.ok(base >= 9000, "the dialogs have a z-index of their own");
  const html = H.HTML_SRC;
  for (const id of ["settings-modal", "history-modal"]) {
    assert.ok(html.indexOf('id="dialog-modal"') < html.indexOf(`id="${id}"`), `(${id} comes after the confirm dialog in the page - which is why it needs the z-index)`);
  }
  assert.ok(zOf("#dialog-modal") > base, "#dialog-modal paints above every other dialog");
  assert.ok(zOf("#token-modal") > zOf("#dialog-modal"), "and the token prompt above that");
  assert.ok(zOf("#dialog-modal") < 9500, "but below the connection banner and the toasts");
});

test("every class the markup of the five columns puts on the page has a rule in style.css (a mistyped class would draw nothing - the fake DOM cannot tell)", () => {
  const src = fs.readFileSync(path.join(H.ROOT, "static", "js", "settings.js"), "utf8");
  const shared = new Set(["btn", "btn-xs", "btn-primary-outline", "btn-danger-outline", "form-hint", "form-error", "form-group", "form-label", "form-input", "tag-badge", "muted", "purple", "checkbox-label", "dialog-check", "caret", "open", "archive-facts", "editor-problems", "is-current", "is-unreadable", "game-row", "ok", "bad", "warn", "init-ready", "init-stale", "init-failed", "init-initializing", "init-uninitialized", "avatar-img"]);
  const classes = new Set();
  for (const m of src.matchAll(/class="([^"$]*)"/g)) for (const c of m[1].split(/\s+/)) if (c) classes.add(c);
  const missing = [...classes].filter((c) => !shared.has(c) && !H.CSS_SRC.includes(`.${c}`));
  assert.deepEqual(missing, []);
  assert.ok(H.HTML_SRC.includes('id="file-settings-import"'));
  assert.ok(H.HTML_SRC.indexOf("static/js/history.js") < H.HTML_SRC.indexOf("static/js/settings.js") && H.HTML_SRC.indexOf("static/js/settings.js") < H.HTML_SRC.indexOf("static/js/app.js"), "settings.js loads between history.js and app.js");
});

H.runTests(tests);
