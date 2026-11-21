"use strict";
// v0.14 (task-25 / task-26): the bots in the page - the player detail panel (name, avatar, the lock, the initialization row), the marks on the
// cards, the initialization (one bot / all, progress, failure, cancel, the refusals), the 名册 menu, the review of a bot, Bots.focus.
// It replaces what test_v0121_ui.js (roster: names / avatars / menu / review) and test_memory_ui.js (persona + memory editors) tested of the
// roster of v0.12.1 / v0.13.1: the roster API is gone, a player IS a bot now (docs/v0.12-contracts.md §13.7).  The editors of the panel
// (底层设定, 记忆, 待确认, 导入 / 导出) are tested in test_botedit_ui.js.
// Run: node tests/frontend/test_bots_ui.js
const assert = require("node:assert/strict");
const H = require("./harness.js");
const M = require("./markup.js");

const { F, target, settle, bootLive, posts } = H;
const tests = [];
// Every test also checks the markup the page drew by the end of it (the fake DOM never parses markup: a stray </div>, a <div> in a <p> or an id that occurs
// twice would pass unseen and break the page in a real browser) - see markup.js.
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

async function live(over = {}, overrides = {}, envOpts = {}) {
  // the fake backend answers write requests from ITS session (e.g. "names can only change while idle"), so it gets the same game the stream's snapshot describes
  const session = F.makeSession({ event_id: 500, ...over });
  const ctx = await bootLive({ session, ...overrides }, envOpts);
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
const cardClick = (env, id) => env.el("participants-grid").dispatch("click", { target: target({ ".pcard": { "data-actor": id } }) });
const panelClick = (env, action, attrs = {}) => env.el("detail-panel").dispatch("click", { target: target({ "[data-detail-action]": { "data-detail-action": action, ...attrs } }) });
const menuClick = (env, action) => env.el("roster-menu").dispatch("click", { target: target({ "[data-roster-action]": { "data-roster-action": action } }) });
const html = (env, id) => env.el(id).innerHTML;
const textOf = (s) => s.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
const cardPart = (env, id) => env.el("participants-grid").innerHTML.split(/(?=<div class="pcard[ "])/).find((p) => p.includes(`data-actor="${id}"`)) || "";
const cardName = (env, id) => (cardPart(env, id).match(/<span class="pcard-name">([^<]*)<\/span>/) || [])[1];
const cardImg = (env, id) => (cardPart(env, id).match(/ src="([^"]+)"/) || [])[1];
const cardInit = (env, id) => (cardPart(env, id).match(/<span class="pcard-init init-(\w+)" data-init="\w+" title="([^"]*)">([^<]*)<\/span>/) || []).slice(1);
const lastToast = (env) => (env.toasts.length ? env.toasts[env.toasts.length - 1].textContent : "");
const toasts = (env) => env.toasts.map((t) => t.textContent);
const dialogOpen = (env) => env.el("dialog-modal").style.display === "flex";
const dialogText = (env) => textOf(html(env, "dialog-body"));
const dialogOk = (env) => env.el("btn-dialog-ok").dispatch("click");
const dialogCancel = (env) => env.el("btn-dialog-cancel").dispatch("click");
const session = (env) => env.app().state.session;
const part = (env, id) => session(env).participants.find((p) => p.id === id);
const modClick = (env) => env.el("topbar-status").dispatch("click", { target: target({ "#mod-chip": { "data-actor": "moderator" } }) });
const chipImg = (env) => (html(env, "mod-chip").match(/ src="([^"]+)"/) || [])[1];
const withHost = (status, avatar = F.avatarOf(13)) => ({ status, moderator: { id: "moderator", name: "主持人", avatar, state: "sleeping", sleep_until: null } });
const RESET = "修改名字或头像会清空这个玩家的初始化结果和全部记忆（操作笔记、规则要点、打法、待解问题、印象、战绩），底层设定保留。修改后需要重新初始化才能开局。是否确认？";
const INIT_TEXT = "bot 会根据自己的底层设定阅读官方契约和规则，写出操作笔记、规则要点和打法。每个最多 120 秒，期间不能开局。";
const ev = (botNumber, over = {}) => ({ job_id: "job-init-1", bot_id: F.botId(botNumber), name: F.NAMES[botNumber - 1], state: "running", seconds_left: 100, deadline_s: 120, files_written: [], error: null, ...over });

// =====================================================================================================
// names (before the game starts; H23: a bot that has learned something is frozen)
// =====================================================================================================
test("before the game starts the name is editable (1-20 characters, unique); saving PUTs /api/bots/{id} and every view follows", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "fresh" });
  assert.ok(backend.calls.some((c) => c.method === "GET" && c.url.endsWith("/api/bots")), "the bots were loaded at start-up (limits and the default library)");
  cardClick(env, "N3");
  const head = html(env, "detail-head");
  assert.match(head, /<input type="text" class="form-input detail-name-input" id="input-detail-name" value="Carol"/);
  assert.match(head, /data-detail-action="save-name"/);
  assert.match(head, /data-detail-action="toggle-avatar-pop"/, "(the re-roll lives in the small panel behind the 🎲)");
  assert.ok(!head.includes("regen-avatar") && !head.includes("lock-badge"), "not frozen: no lock, no question");

  env.el("input-detail-name").value = "Caroline";
  panelClick(env, "save-name");
  await flushAll(env);
  assert.deepEqual(backend.botPuts, [{ id: F.botId(3), name: "Caroline" }], "no confirm_reset: the bot has learned nothing");
  assert.equal(cardName(env, "N3"), "Caroline");
  env.el("btn-viewpoint").dispatch("click"); // (v0.14: the viewpoint menu lists the players by seat and the new name)
  assert.match(html(env, "viewpoint-menu"), /<span class="vp-label">3 · Caroline<\/span>/);
  assert.match(lastToast(env), /名字已保存：Caroline/);
  assert.equal(part(env, "N3").name, "Caroline");
  assert.equal(backend.botsApi.bot("N3").name, "Caroline", "the server's bot has it");
  assert.equal(part(env, "N3").bot_id, F.botId(3), "the same bot, the same id");
});

test("name validation happens locally first: empty, too long, a duplicate, a colon; the server's refusal is shown too", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "fresh" });
  cardClick(env, "N3");
  const attempt = async (value) => {
    env.el("input-detail-name").value = value;
    panelClick(env, "save-name");
    await flushAll(env, 20);
    return textOf(html(env, "detail-head"));
  };
  assert.match(await attempt("   "), /名字不能为空/);
  assert.match(await attempt("x".repeat(21)), /名字最多 20 个字符/);
  assert.match(await attempt("Bob"), /名字「Bob」已被其他玩家使用/);
  assert.match(await attempt("Carol:2"), /名字不能包含冒号（: 或 ：）。/);
  assert.match(await attempt("Carol：2"), /名字不能包含冒号（: 或 ：）。/);
  assert.equal((backend.botPuts || []).length, 0, "nothing was sent");
  assert.equal(cardName(env, "N3"), "Carol");
  assert.doesNotMatch(await attempt("Carol"), /已被/, "an unchanged name is no change");
  assert.equal((backend.botPuts || []).length, 0);
  // the server may still refuse: its message is shown, the card keeps its name
  backend.failures[`PUT /api/bots/${F.botId(3)}`] = [400, "名字「Caro l」已被 N7 使用，名字不能重复。", { code: "name_taken" }];
  const after = await attempt("Caro l");
  assert.match(after, /已被 N7 使用/);
  assert.equal(cardName(env, "N3"), "Carol");
  assert.match(lastToast(env), /已被 N7 使用/);
});

test("an OLD name with a colon (stored before the server refused them) still shows; Save on it changes nothing; only a NEW nickname with a colon is refused", async () => {
  const OLD = "Carol:2";
  const participants = F.makeSession().participants.map((p) => (p.id === "N3" ? { ...p, name: OLD } : p));
  const { env, backend } = await live({ status: "idle", participants }, { botsPreset: "fresh" });
  backend.botsApi.set("N3", { name: OLD });
  await env.app().bots().refreshBots();
  await flushAll(env);
  const attempt = async (value) => {
    env.el("input-detail-name").value = value;
    panelClick(env, "save-name");
    await flushAll(env, 20);
    return textOf(html(env, "detail-head"));
  };
  assert.equal(cardName(env, "N3"), OLD);
  cardClick(env, "N3");
  assert.match(html(env, "detail-head"), /id="input-detail-name" value="Carol:2"/);
  assert.doesNotMatch(textOf(html(env, "detail-head")), /冒号/);
  assert.doesNotMatch(await attempt("Carol:2"), /冒号/);
  assert.doesNotMatch(await attempt("  Carol:2  "), /冒号/);
  assert.equal((backend.botPuts || []).length, 0);
  for (const bad of ["Carol:3", "Carol：2", ":", "：", "Carol:2:"]) assert.match(await attempt(bad), /名字不能包含冒号（: 或 ：）。/, JSON.stringify(bad));
  assert.equal((backend.botPuts || []).length, 0, "nothing was sent");
  assert.doesNotMatch(await attempt("Caroline"), /冒号/);
  assert.deepEqual(backend.botPuts, [{ id: F.botId(3), name: "Caroline" }]);
});

test("once the game has started the name is read-only text and the 🎲 is gone; a new game makes it editable again", async () => {
  const { env } = await live({ status: "running" }, { botsPreset: "fresh" });
  cardClick(env, "N3");
  const head = html(env, "detail-head");
  assert.ok(!head.includes("input-detail-name") && !head.includes("toggle-avatar-pop"));
  assert.match(head, /<div class="detail-name" title="对局未开始时才能改名">Carol<\/div>/);
  env.sse("snapshot", { session: F.makeSession({ session_id: "next", status: "idle", event_id: 900 }) }, 900);
  await flushAll(env);
  assert.match(html(env, "detail-head"), /id="input-detail-name"/);
});

test("a bot that is initialized is frozen (🔒): a new name asks first - the fixed sentence - and only a yes sends confirm_reset; the bot gets a new id, the seat stays open", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "ready" });
  cardClick(env, "N3");
  assert.match(html(env, "detail-head"), /class="lock-badge"/);
  assert.match(textOf(html(env, "detail-head")), /已初始化：改名或换头像会清空初始化结果和记忆/);
  env.el("input-detail-name").value = "Caroline";
  panelClick(env, "save-name");
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.equal(dialogText(env).startsWith(RESET), true, "the sentence of §13.10");
  assert.match(env.el("btn-dialog-ok").className, /btn-danger-outline/);
  assert.equal((backend.botPuts || []).length, 0, "nothing is sent before the answer");
  dialogCancel(env);
  await flushAll(env);
  assert.equal((backend.botPuts || []).length, 0);
  assert.equal(cardName(env, "N3"), "Carol");
  panelClick(env, "save-name");
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.botPuts, [{ id: F.botId(3), name: "Caroline", confirm_reset: true }]);
  assert.equal(cardName(env, "N3"), "Caroline");
  assert.notEqual(part(env, "N3").bot_id, F.botId(3), "a reset bot has a new internal id");
  assert.equal(part(env, "N3").init_status, "uninitialized", "and has to be initialized again");
  assert.equal(env.app().bots().openId(), "N3", "the panel stays on the seat");
  assert.match(html(env, "detail-head"), /data-init="uninitialized"/);
  assert.ok(!html(env, "detail-head").includes("lock-badge"), "not frozen any more");
  assert.deepEqual(F.makeBotSummary(3).core.chars, backend.botsApi.summary("N3").core.chars, "the L0 stays");
});

test("a server that says 409 needs_confirm (the bot became frozen behind our back) gets the same question", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "fresh" });
  cardClick(env, "N4");
  backend.botsApi.initialized("N4"); // (the page was not told)
  env.el("input-detail-name").value = "David2";
  panelClick(env, "save-name");
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.equal(dialogText(env).startsWith("修改名字或头像会清空这个玩家"), true);
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.botPuts.map((p) => p.confirm_reset), [undefined, true]);
  assert.equal(cardName(env, "N4"), "David2");
});

// =====================================================================================================
// avatars
// =====================================================================================================
test("regenerate avatar (one player): POST /api/bots/{id}/avatar:regenerate, only that card changes", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "fresh" });
  const before = Object.fromEntries(F.PLAYER_IDS.map((id) => [id, cardImg(env, id)]));
  cardClick(env, "N5");
  panelClick(env, "toggle-avatar-pop");
  panelClick(env, "regen-avatar");
  await flushAll(env);
  assert.equal(posts(backend, "POST", `${F.botId(5)}/avatar:regenerate`).length, 1);
  assert.notEqual(cardImg(env, "N5"), before.N5);
  assert.equal(cardImg(env, "N4"), before.N4);
  assert.equal(cardImg(env, "N6"), before.N6);
  assert.match(lastToast(env), /头像已重新生成/);
  assert.match(html(env, "detail-head"), /class="avatar-img"[^>]* src="data:image\/png/);
});

test("the moderator's panel: its random avatar and, before the game, a button to roll a new one (POST /api/moderator/avatar:regenerate): only the moderator changes", async () => {
  const { env, backend } = await live(withHost("idle"));
  const players = Object.fromEntries(F.PLAYER_IDS.map((id) => [id, cardImg(env, id)]));
  const before = chipImg(env);
  assert.notEqual(before, H.Avatar.moderatorUrl(), "a random avatar, not the top hat");
  modClick(env);
  const head = html(env, "detail-head");
  assert.ok(head.includes(before), "the panel shows the same picture");
  assert.ok(!head.includes("input-detail-name") && !head.includes("save-name") && !head.includes("data-detail-action=\"init-one\""), "the moderator's name is fixed, and it is not a bot");
  panelClick(env, "toggle-avatar-pop");
  panelClick(env, "regen-avatar");
  await flushAll(env);
  assert.equal(posts(backend, "POST", "/api/moderator/avatar:regenerate").length, 1);
  assert.equal(backend.calls.filter((c) => /bots\/.*avatar:regenerate/.test(c.url)).length, 0);
  assert.notEqual(chipImg(env), before, "the chip shows the new avatar");
  assert.ok(html(env, "detail-head").includes(chipImg(env)), "and so does the open panel");
  for (const id of F.PLAYER_IDS) assert.equal(cardImg(env, id), players[id], `${id} is untouched`);
  assert.match(lastToast(env), /主持人头像已重新生成/);
  assert.equal(html(env, "detail-editors"), "", "the moderator has no editors");
});

test("the moderator's regenerate button is gone once the game has started, and a refusal is shown", async () => {
  const running = await live(withHost("running"));
  modClick(running.env);
  assert.ok(!html(running.env, "detail-head").includes("regen-avatar") && !html(running.env, "detail-head").includes("toggle-avatar-pop"));
  const { env, backend } = await live(withHost("idle"));
  backend.session.status = "paused"; // the server says the game moved on while the panel was open
  modClick(env);
  panelClick(env, "toggle-avatar-pop");
  panelClick(env, "regen-avatar");
  await flushAll(env);
  assert.match(lastToast(env), /头像只能在对局开始之前修改/);
  assert.equal(chipImg(env), H.Avatar.urlFor("moderator", undefined, F.avatarOf(13)), "nothing changed");
});

test("a game of the old format (no bot ids): the panel says so and offers neither initialization nor editors", async () => {
  const participants = F.makeSession().participants.map((p) => ({ ...p, bot_id: "", init_status: null }));
  const { env } = await live({ status: "running", schema_version: 5, participants, bots: undefined });
  cardClick(env, "N3");
  assert.match(textOf(html(env, "detail-head")), /这局是旧版存档：没有 bot 信息/);
  assert.ok(!html(env, "detail-head").includes("init-one"));
  assert.equal(html(env, "detail-editors"), "");
  assert.equal(cardInit(env, "N3").length, 0, "no mark on the card either");
});

// =====================================================================================================
// the marks of the initialization: on the cards, in the bar, in the panel (H22)
// =====================================================================================================
test("a card shows ✔ ⚠ ✖ ⏳ ○ next to the name, with the reason in its tooltip; the bar counts them", async () => {
  const { env } = await live({ status: "idle" }, { botsPreset: "mixed" });
  const marks = Object.fromEntries(F.PLAYER_IDS.map((id) => [id, cardInit(env, id)]));
  assert.deepEqual(marks.N1.slice(0, 1).concat(marks.N1[2]), ["ready", "✔"]);
  assert.deepEqual([marks.N2[0], marks.N2[2]], ["stale", "⚠"]);
  assert.match(marks.N2[1], /需重新初始化（底层设定变了）：记忆保留/);
  assert.deepEqual([marks.N3[0], marks.N3[2]], ["failed", "✖"]);
  assert.match(marks.N3[1], /初始化失败：原因：模型调用失败：HTTP 429（模拟）/);
  assert.deepEqual([marks.N4[0], marks.N4[2]], ["uninitialized", "○"]);
  assert.equal(env.el("init-summary").hidden, false);
  assert.equal(env.el("init-summary").textContent, "✔ 6 · ⚠ 1 · ✖ 1 · ○ 4");
  assert.equal(env.el("btn-init-all").textContent, "⚙ 全局初始化");
  assert.equal(env.el("btn-init-all").disabled, false);
  // the number follows the table: a bot becomes ✔
  env.sse("bots.updated", { bots: F.makeBots() });
  assert.equal(env.el("init-summary").textContent, "✔ 12");
  assert.equal(cardInit(env, "N3")[2], "✔");
});

test("the head of the panel: the state of the bot, why, how long it took; ⚠ and ✖ say what to do", async () => {
  const { env } = await live({ status: "idle" }, { botsPreset: "mixed" });
  cardClick(env, "N1");
  let head = textOf(html(env, "detail-head"));
  assert.match(head, /✔ 已初始化 重新初始化/);
  assert.match(head, /模型 deepseek-flash · 用时 \d+ 分 \d+ 秒/);
  cardClick(env, "N1");
  cardClick(env, "N2");
  head = textOf(html(env, "detail-head"));
  assert.match(head, /⚠ 需重新初始化（底层设定变了） 重新初始化/);
  cardClick(env, "N2");
  cardClick(env, "N3");
  head = textOf(html(env, "detail-head"));
  assert.match(head, /✖ 初始化失败 重试初始化/);
  assert.match(head, /原因：模型调用失败：HTTP 429（模拟）/);
  cardClick(env, "N3");
  cardClick(env, "N6"); // (no L0 at all)
  head = textOf(html(env, "detail-head"));
  assert.match(head, /底层设定还不完整：先补全三个必填分区。/);
  assert.match(html(env, "detail-head"), /id="btn-detail-init"[^>]* disabled/);
});

// =====================================================================================================
// initialization: one bot
// =====================================================================================================
test("初始化 of one bot: the fixed sentence (120 s, no game meanwhile), then POST /api/bots/init {ids}; the progress comes by events", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "fresh" });
  cardClick(env, "N4");
  panelClick(env, "init-one");
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.equal(env.el("dialog-title").textContent, "初始化 David");
  assert.equal((html(env, "dialog-body").match(/id="dialog-init-text">([^<]*)</) || [])[1], INIT_TEXT);
  assert.match(dialogText(env), /将初始化：4 号 David（未初始化）/);
  dialogCancel(env);
  await flushAll(env);
  assert.equal(posts(backend, "POST", "/api/bots/init").length, 0, "cancel: nothing is sent");
  panelClick(env, "init-one");
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.initCalls, [{ ids: [F.botId(4)] }]);
  assert.match(lastToast(env), /已开始初始化 1 个玩家，每个最多 120 秒/);
  // the table now has a job: the bar offers to cancel it; the card is ⏳ (the server says so)
  assert.equal(session(env).bots.init_job.ids[0], F.botId(4));
  assert.equal(env.el("btn-init-all").textContent, "■ 取消初始化 0/1");
  assert.equal(part(env, "N4").init_status, "initializing");
  assert.equal(cardInit(env, "N4")[2], "⏳");
  // the events: the countdown and the files written, on the card, in the panel and in the bar
  env.sse("bot.init.progress", ev(4, { seconds_left: 90, files_written: ["arch_notes"] }));
  assert.match(cardInit(env, "N4")[1], /初始化中：初始化中，还剩 90 秒；已写：操作笔记/);
  assert.match(textOf(html(env, "detail-head")), /⏳ 初始化中 取消初始化/);
  assert.match(textOf(html(env, "detail-head")), /初始化中，还剩 90 秒；已写：操作笔记/);
  assert.match(html(env, "detail-head"), /id="init-bar-fill" style="width:25%"/);
  assert.equal(env.el("init-summary").textContent, "初始化中 0/1 · 还剩 90 秒");
  assert.match(html(env, "detail-head"), /id="btn-detail-init-cancel"/);
  assert.ok(!html(env, "detail-head").includes('id="btn-detail-init"'), "the start button is replaced by cancel");
  env.sse("bot.init.progress", ev(4, { seconds_left: 40, files_written: ["arch_notes", "digest"] }));
  assert.match(html(env, "detail-head"), /id="init-bar-fill" style="width:67%"/);
  assert.equal(env.el("init-summary").textContent, "初始化中 0/1 · 还剩 40 秒");
});

test("a finished job: one summary toast for the whole table; the card is ✔ again and the panel says how it went", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "fresh" });
  cardClick(env, "N4");
  panelClick(env, "init-one");
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  env.sse("bot.init.progress", ev(4, { seconds_left: 50, files_written: ["arch_notes"] }));
  env.sse("bot.init.progress", ev(4, { state: "done", seconds_left: 33, files_written: ["arch_notes", "digest", "playbook"] }));
  assert.equal(env.el("init-summary").textContent, "初始化中 1/1");
  // the server finished the job: the table says so (init_job is gone, the bot is ready)
  backend.botsApi.initialized("N4");
  backend.botsFake.state.initJob = null;
  backend.botsApi.state.initJob = null;
  env.sse("bots.updated", { bots: backend.botsApi.live() });
  await flushAll(env);
  assert.equal(toasts(env).filter((t) => /初始化/.test(t)).pop(), "初始化完成：1 个玩家都写好了自己的笔记 ✔");
  assert.equal(env.el("btn-init-all").textContent, "⚙ 全局初始化");
  assert.equal(cardInit(env, "N4")[2], "✔");
  assert.match(textOf(html(env, "detail-head")), /✔ 已初始化 重新初始化/);
});

test("a failed or timed-out initialization shows the reason on the card and in the panel, and can be retried; the summary names the reasons", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "fresh" });
  cardClick(env, "N4");
  panelClick(env, "init-one");
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  env.sse("bot.init.progress", ev(4, { state: "timeout", seconds_left: 0, error: "超过 120 秒还没有写完" }));
  assert.match(textOf(html(env, "detail-head")), /超过 120 秒，已经停止：超过 120 秒还没有写完/);
  // the server: the bot is ✖ with its reason, the job is over
  Object.assign(backend.botsApi.bot("N4").init, { status: "failed", error: "超过 120 秒还没有写完" });
  backend.botsApi.state.initJob = null;
  env.sse("bots.updated", { bots: backend.botsApi.live() });
  await flushAll(env);
  assert.match(lastToast(env), /初始化结束：0 个成功，1 个没成功（David：超过 120 秒还没有写完）/);
  assert.deepEqual([cardInit(env, "N4")[0], cardInit(env, "N4")[2]], ["failed", "✖"]);
  assert.match(cardInit(env, "N4")[1], /初始化失败：原因：超过 120 秒还没有写完/);
  const head = textOf(html(env, "detail-head"));
  assert.match(head, /✖ 初始化失败 重试初始化/);
  assert.match(head, /原因：超过 120 秒还没有写完/);
  // the retry is a new question and a new job
  panelClick(env, "init-one");
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.equal(backend.initCalls.length, 2);
  assert.deepEqual(backend.initCalls[1], { ids: [F.botId(4)] });
});

test("the summary toast puts the reasons of the server in ONE bracket: a reason's own full stop is left out - no '。）', no '。；' between two reasons", async () => {
  // the way the server words them: a sentence with its own stop, with a bracket inside, the stop of the last one right before the closing bracket
  const REASONS = {
    1: "时间到了（120 秒），初始化没有完成，原来的记忆没有改动。",
    2: "调用模型失败（HTTP 429），原来的记忆没有改动，可以重试。",
    3: "没有写完必需的文件：打法，原来的记忆没有改动，可以重试。",
    5: "底层设定不完整：缺少「身份」，先写好再初始化。",
  };
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "fresh" });
  env.el("btn-init-all").dispatch("click");
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  for (const n of [1, 2, 3, 5]) env.sse("bot.init.progress", ev(n, { state: n === 1 ? "timeout" : "failed", seconds_left: 0, error: REASONS[n] }));
  for (const n of [4, 6]) env.sse("bot.init.progress", ev(n, { state: "done", seconds_left: 10, files_written: ["arch_notes", "digest", "playbook"] }));
  for (const n of [1, 2, 3, 5]) Object.assign(backend.botsApi.bot(`N${n}`).init, { status: "failed", error: REASONS[n] });
  backend.botsApi.initialized("N4");
  backend.botsApi.initialized("N6");
  backend.botsApi.state.initJob = null;
  backend.botsFake.state.initJob = null;
  env.sse("bots.updated", { bots: backend.botsApi.live() });
  await flushAll(env);
  const toast = toasts(env).filter((t) => /初始化结束/.test(t)).pop();
  assert.equal(
    toast,
    "初始化结束：2 个成功，4 个没成功（Alice：时间到了（120 秒），初始化没有完成，原来的记忆没有改动；Bob：调用模型失败（HTTP 429），原来的记忆没有改动，可以重试；" +
      "Carol：没有写完必需的文件：打法，原来的记忆没有改动，可以重试……）",
    "three reasons are named, the rest is an ellipsis; each one ends where its sentence did, without the stop"
  );
  assert.doesNotMatch(toast, /。/, "not one full stop in the whole toast");
  assert.doesNotMatch(toast, /[，；：、]）/, "and no comma or colon left before the closing bracket");
});

test("a reason that comes without words (an empty error) is named by what happened; a reason that ends in a bracket keeps it", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "fresh" });
  cardClick(env, "N4");
  panelClick(env, "init-one");
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  env.sse("bot.init.progress", ev(4, { state: "failed", seconds_left: 0, error: "。" }));
  Object.assign(backend.botsApi.bot("N4").init, { status: "failed", error: "。" });
  backend.botsApi.state.initJob = null;
  env.sse("bots.updated", { bots: backend.botsApi.live() });
  await flushAll(env);
  assert.match(lastToast(env), /初始化结束：0 个成功，1 个没成功（David：失败）$/, "nothing but a stop is no reason");
  // a sentence that ends with a bracket of its own: the bracket is part of it
  panelClick(env, "init-one");
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  env.sse("bot.init.progress", ev(4, { job_id: "job-init-2", state: "failed", seconds_left: 0, error: "底层设定不完整，请先补全（原来的文件没有改动）。" }));
  Object.assign(backend.botsApi.bot("N4").init, { status: "failed", error: "底层设定不完整，请先补全（原来的文件没有改动）。" });
  backend.botsApi.state.initJob = null;
  env.sse("bots.updated", { bots: backend.botsApi.live() });
  await flushAll(env);
  assert.match(lastToast(env), /（David：底层设定不完整，请先补全（原来的文件没有改动））$/);
});

// =====================================================================================================
// initialization: everybody
// =====================================================================================================
test("全局初始化 lists who is taken (not ✔ yet); \"全部重新初始化\" takes everybody; the job is one POST with those ids", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "mixed" });
  env.el("btn-init-all").dispatch("click");
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.equal(env.el("dialog-title").textContent, "全局初始化");
  assert.equal((html(env, "dialog-body").match(/id="dialog-init-text">([^<]*)</) || [])[1], INIT_TEXT);
  let list = dialogText(env); // (the fake DOM does not parse the dialog's markup into elements: what the dialog was drawn with is its body)
  assert.match(list, /将初始化 6 个玩家：/);
  assert.match(list, /○ 4 号 David 未初始化/);
  assert.match(list, /⚠ 2 号 Bob 需重新初始化（底层设定变了）/);
  assert.match(list, /✖ 3 号 Carol 初始化失败/);
  assert.doesNotMatch(list, /1 号 Alice/, "a ✔ bot is left alone");
  assert.equal(env.el("chk-init-all").checked, false);
  assert.equal(env.el("btn-dialog-ok").disabled, false);
  // everybody
  env.el("chk-init-all").checked = true;
  env.el("chk-init-all").dispatch("change");
  list = textOf(html(env, "init-targets"));
  assert.match(list, /将初始化 12 个玩家：/);
  assert.match(list, /✔ 1 号 Alice 已初始化/);
  env.el("chk-init-all").checked = false;
  env.el("chk-init-all").dispatch("change");
  assert.match(textOf(html(env, "init-targets")), /将初始化 6 个玩家：/);
  dialogCancel(env);
  await flushAll(env);
  assert.equal(posts(backend, "POST", "/api/bots/init").length, 0);
  // confirm, default: the six
  env.el("btn-init-all").dispatch("click");
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.deepEqual(backend.initCalls, [{ ids: ["N2", "N3", "N4", "N5", "N6", "N7"].map((n) => F.botId(Number(n.slice(1)))) }]);
  assert.match(lastToast(env), /已开始初始化 6 个玩家/);
  assert.equal(env.el("btn-init-all").textContent, "■ 取消初始化 0/6");
});

test("全局初始化 with everybody ✔: nothing to do unless \"全部重新初始化\" is ticked", async () => {
  const { env, backend } = await live({ status: "idle" });
  env.el("btn-init-all").dispatch("click");
  await flushAll(env);
  assert.match(dialogText(env), /现在没有需要初始化的玩家：全部都是 ✔/);
  assert.equal(env.el("btn-dialog-ok").disabled, true, "nothing to start");
  env.el("chk-init-all").checked = true;
  env.el("chk-init-all").dispatch("change");
  assert.equal(env.el("btn-dialog-ok").disabled, false);
  assert.match(textOf(html(env, "init-targets")), /将初始化 12 个玩家/);
  dialogOk(env);
  await flushAll(env);
  assert.equal(backend.initCalls[0].ids.length, 12);
});

test("while a job runs the bar's button cancels it (POST /api/bots/init:cancel); the card marks and the counts follow the events", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "fresh" });
  env.el("btn-init-all").dispatch("click");
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.equal(env.el("btn-init-all").textContent, "■ 取消初始化 0/12");
  env.sse("bot.init.progress", ev(1, { seconds_left: 70 }));
  env.sse("bot.init.progress", ev(2, { seconds_left: 100 }));
  env.sse("bot.init.progress", ev(3, { state: "done", seconds_left: 20, files_written: ["arch_notes", "digest", "playbook"] }));
  env.sse("bot.init.progress", ev(4, { state: "queued", seconds_left: null }));
  assert.equal(env.el("btn-init-all").textContent, "■ 取消初始化 1/12");
  assert.equal(env.el("init-summary").textContent, "初始化中 1/12 · 还剩 100 秒");
  assert.match(cardInit(env, "N4")[1], /排队中…/);
  env.el("btn-init-all").dispatch("click");
  await flushAll(env);
  assert.equal(backend.initCancels, 1);
  assert.ok(toasts(env).some((x) => /已取消初始化：没写完的文件不会保留/.test(x)));
  assert.equal(env.el("btn-init-all").textContent, "⚙ 全局初始化");
  // the one that finished before the cancel is told, the others were cut - not failed
  assert.equal(lastToast(env), "初始化完成：1 个玩家都写好了自己的笔记 ✔（3 个被取消）");
});

test("the refusals of the server are shown where the user looks: no_binding lists the players and leads to the bindings; game_active / init_busy are toasts and a note", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "fresh" });
  backend.botsApi.set("N5", { binding: { ok: false, label: "" } });
  await env.app().bots().refreshBots();
  cardClick(env, "N5");
  assert.match(textOf(html(env, "detail-head")), /还没有给它绑定模型/);
  panelClick(env, "init-one");
  await flushAll(env);
  dialogOk(env); // the question
  await flushAll(env);
  assert.ok(dialogOpen(env), "the refusal is a dialog of its own");
  assert.equal(env.el("dialog-title").textContent, "还不能初始化");
  assert.match(dialogText(env), /有玩家还没有绑定模型，不能初始化：Eve/);
  assert.match(dialogText(env), /Eve：还没有给这个玩家绑定可用的模型。/);
  assert.equal(env.el("btn-dialog-ok").textContent, "去配置模型绑定");
  dialogOk(env);
  await flushAll(env);
  assert.equal(env.el("bindings-modal").style.display, "flex", "the bindings dialog opens");
  // game_active: the server knows better than the page (the game started in another tab)
  backend.botsApi.set("N5", { binding: { ok: true, label: "x" } });
  await env.app().bots().refreshBots();
  backend.session.status = "running";
  panelClick(env, "init-one");
  await flushAll(env);
  dialogOk(env);
  await flushAll(env);
  assert.match(lastToast(env), /对局正在进行（或已暂停），现在不能初始化/);
  assert.match(textOf(html(env, "detail-head")), /对局正在进行（或已暂停），现在不能初始化/, "and in the panel");
});

test("initialization needs a game that is not running: the buttons are off with the reason; a finished game can initialize", async () => {
  const running = await live({ status: "running" });
  cardClick(running.env, "N2");
  assert.equal(running.env.el("btn-init-all").disabled, true);
  assert.match(running.env.el("btn-init-all").title, /对局进行中（或已暂停），不能初始化/);
  assert.match(html(running.env, "detail-head"), /id="btn-detail-init"[^>]* disabled/);
  assert.match(textOf(html(running.env, "detail-head")), /对局进行中（或已暂停），不能初始化：请先新建对局。/);
  const paused = await live({ status: "paused" });
  assert.equal(paused.env.el("btn-init-all").disabled, true);
  const finished = await live({ status: "finished", winner: "好人阵营" });
  assert.equal(finished.env.el("btn-init-all").disabled, false, "after the game the bots can learn");
  const idle = await live({ status: "idle" });
  assert.equal(idle.env.el("btn-init-all").disabled, false);
  // a game starts: the button goes off at once
  idle.env.sse("game.status", { status: "running", reason: "started", winner: null });
  assert.equal(idle.env.el("btn-init-all").disabled, true);
});

// =====================================================================================================
// the 名册 menu
// =====================================================================================================
test("名册 menu: opens and closes; it has the load-memory switch, the avatars, clear-all and the learning use (nothing of the old pack export / import)", async () => {
  const { env, backend } = await live({ status: "idle" });
  assert.equal(env.el("roster-menu").hidden, true);
  env.el("btn-roster-menu").dispatch("click");
  assert.equal(env.el("roster-menu").hidden, false);
  assert.equal(env.el("btn-roster-menu").getAttribute("aria-expanded"), "true");
  for (const id of ["chk-memory-enabled", "btn-roster-regen", "btn-roster-clear", "roster-usage"]) assert.ok(H.HTML_SRC.includes(`id="${id}"`), id);
  assert.ok(!H.HTML_SRC.includes("btn-regen-all-avatars"), "the standalone 🎲 button moved into the menu");
  assert.ok(!H.HTML_SRC.includes("btn-roster-export") && !H.HTML_SRC.includes("file-import-memory"));
  menuClick(env, "export"); // an old entry does nothing
  await flushAll(env, 10);
  assert.equal(backend.calls.filter((c) => /memory:(export|import)/.test(c.url)).length, 0);
  env.document.dispatchEvent("click", { target: target({}) });
  assert.equal(env.el("roster-menu").hidden, true);
  env.el("btn-roster-menu").dispatch("click");
  env.document.dispatchEvent("keydown", { key: "Escape" });
  assert.equal(env.el("roster-menu").hidden, true);
});

test("clear all learned memory: the question (danger), POST /api/bots/memory:clear; nothing happens on cancel; the bots become uninitialized", async () => {
  const { env, backend } = await live({ status: "idle" });
  menuClick(env, "clear");
  await flushAll(env);
  assert.ok(dialogOpen(env));
  assert.equal(env.el("dialog-title").textContent, "清除全部学到的记忆");
  assert.match(dialogText(env), /将清空全部 12 名玩家学到的记忆（操作笔记、规则要点、打法、待解问题、印象、战绩），初始化结果一并清空（变为未初始化），底层设定保留。/);
  assert.match(env.el("btn-dialog-ok").className, /btn-danger-outline/);
  dialogCancel(env);
  await flushAll(env);
  assert.equal(posts(backend, "POST", "/api/bots/memory:clear").length, 0);
  assert.equal(backend.botsApi.bot("N1").init.status, "ready");
  menuClick(env, "clear");
  await flushAll(env);
  dialogOk(env);
  await flushAll(env, 40);
  assert.deepEqual(backend.clearCalls, [{ key: "POST /api/bots/memory:clear", ids: null }]);
  assert.match(lastToast(env), /已清除全部学到的记忆（底层设定保留，需要重新初始化）/);
  assert.equal(backend.botsApi.bot("N1").init.status, "uninitialized");
  assert.equal(cardInit(env, "N1")[2], "○", "the cards follow");
  assert.equal(env.el("init-summary").textContent, "○ 12");
});

test("重新生成全部头像: POST /api/bots/avatars:regenerate; idle only; the initialized bots keep theirs (and the toast says so)", async () => {
  const { env, backend } = await live({ status: "idle" }, { botsPreset: "mixed" });
  const before = F.PLAYER_IDS.map((id) => cardImg(env, id));
  assert.equal(env.el("btn-roster-regen").disabled, false);
  menuClick(env, "regen");
  await flushAll(env, 40);
  assert.equal(posts(backend, "POST", "/api/bots/avatars:regenerate").length, 1);
  const after = F.PLAYER_IDS.map((id) => cardImg(env, id));
  const changed = after.filter((u, i) => u !== before[i]).length;
  assert.ok(changed >= 4 && changed < 12, `only the bots that are not frozen changed (${changed})`);
  assert.equal(after[0] === before[0], true, "N1 is ✔ (frozen): untouched");
  assert.match(lastToast(env), /已重新生成头像（\d+ 个已经初始化的玩家保持不变）/);
  env.sse("game.status", { status: "running", reason: "started", winner: null });
  assert.equal(env.el("btn-roster-regen").disabled, true);
  assert.match(env.el("btn-roster-regen").title, /对局已开始/);
  // everybody unfrozen: "全部"
  const fresh = await live({ status: "idle" }, { botsPreset: "fresh" });
  const was = F.PLAYER_IDS.map((id) => cardImg(fresh.env, id));
  menuClick(fresh.env, "regen");
  await flushAll(fresh.env, 40);
  assert.equal(F.PLAYER_IDS.map((id) => cardImg(fresh.env, id)).filter((u, i) => u !== was[i]).length, 12);
  assert.match(lastToast(fresh.env), /已重新生成全部头像/);
});

test("'本局加载记忆' switch: PUT /api/bots/memory-enabled; it follows the session; idle-only (and clear too)", async () => {
  const { env, backend } = await live({ status: "idle", memory_enabled: true });
  assert.equal(env.el("chk-memory-enabled").checked, true);
  env.el("chk-memory-enabled").checked = false;
  env.el("chk-memory-enabled").dispatch("change");
  await flushAll(env);
  assert.deepEqual(posts(backend, "PUT", "/api/bots/memory-enabled")[0].body, { enabled: false });
  assert.match(lastToast(env), /本局不加载记忆/);
  env.sse("session.updated", { memory_enabled: false });
  assert.equal(env.el("chk-memory-enabled").checked, false);
  env.sse("session.updated", { memory_enabled: true });
  assert.equal(env.el("chk-memory-enabled").checked, true);
  backend.failures["PUT /api/bots/memory-enabled"] = [409, "对局已开始"];
  env.el("chk-memory-enabled").checked = false;
  env.el("chk-memory-enabled").dispatch("change");
  await flushAll(env);
  assert.equal(env.el("chk-memory-enabled").checked, true, "a failure puts the checkbox back");
  assert.match(lastToast(env), /对局已开始/);
  env.sse("game.status", { status: "running", reason: "started", winner: null });
  assert.equal(env.el("chk-memory-enabled").disabled, true);
  assert.equal(env.el("btn-roster-clear").disabled, true);
});

test("the learning use (calls, tokens, cost of the initializations) is in the menu and follows bots.updated", async () => {
  const { env } = await live({ status: "idle" });
  assert.match(env.el("roster-usage").textContent, /学习用量：还没有/);
  env.sse("bots.updated", { bots: F.makeBots({ learning_usage: { calls: 24, tokens: 123456, cost_usd: 0.0371 } }) });
  assert.equal(env.el("roster-usage").textContent, "学习用量：24 次调用 · 123.5K tokens · $0.0371");
});

// =====================================================================================================
// the review of a bot (H31-H33; the end-of-game dialog is web-play's)
// =====================================================================================================
const finished = (over = {}) => ({ status: "finished", winner: "好人阵营", summary: "狼人全部出局", review: { available: true, done: [], running: false, supported: true, deadline_s: 60, seconds_left: null }, ...over });

test("复盘全部 only exists once the game is finished; it POSTs /api/game/review and shows progress (queued -> running with a countdown -> done), then 已复盘", async () => {
  const { env, backend } = await live({ status: "running" });
  assert.equal(env.el("btn-review-all").hidden, true);
  env.sse("game.status", { status: "finished", reason: "end_game", winner: "好人阵营", summary: "x" });
  env.sse("session.updated", { review: { available: true, done: [], running: false, supported: true, deadline_s: 60, seconds_left: null } });
  assert.equal(env.el("btn-review-all").hidden, false);
  assert.equal(env.el("btn-review-all").textContent, "复盘全部");
  env.el("btn-review-all").dispatch("click");
  await flushAll(env);
  assert.deepEqual(backend.lastReview, {}, "no ids: everybody");
  assert.match(lastToast(env), /已开始复盘全部玩家（最多 60 秒）/);
  env.sse("review.progress", { id: "N1", bot_id: F.botId(1), name: "Alice", state: "queued", message: "排队中…", seconds_left: null, deadline_s: 60, changes: null });
  assert.equal(env.el("btn-review-all").textContent, "复盘中 0/12…");
  assert.equal(env.el("btn-review-all").disabled, true);
  env.sse("review.progress", { id: "N1", bot_id: F.botId(1), name: "Alice", state: "running", message: "正在复盘…", seconds_left: 41, deadline_s: 60, changes: null });
  env.sse("review.progress", { id: "N1", bot_id: F.botId(1), name: "Alice", state: "done", message: "已写入", seconds_left: 12, deadline_s: 60, changes: { files: [], proposals: 0 } });
  env.sse("review.progress", { id: "N2", bot_id: F.botId(2), name: "Bob", state: "running", message: "正在复盘…", seconds_left: 50, deadline_s: 60, changes: null });
  assert.equal(env.el("btn-review-all").textContent, "复盘中 1/12…");
  for (const id of F.PLAYER_IDS.slice(1)) env.sse("review.progress", { id, bot_id: "", name: "", state: id === "N5" ? "timeout" : "done", message: "ok", seconds_left: 0, deadline_s: 60, changes: { files: [], proposals: 0 } });
  assert.equal(env.el("btn-review-all").textContent, "✓ 已复盘", "a review that ran into its 60 seconds counts as done");
  assert.equal(env.el("btn-review-all").disabled, true);
});

test("a player's panel: 复盘此玩家 -> 复盘中（还剩 N 秒） -> 已复盘; what the review changed is shown old -> new, with the proposals; a failed one can be retried", async () => {
  const { env, backend } = await live(finished());
  cardClick(env, "N4");
  assert.match(html(env, "detail-live"), /data-detail-action="review-one"[^>]*>复盘此玩家<\/button>/);
  panelClick(env, "review-one");
  await flushAll(env);
  assert.deepEqual(backend.lastReview, { ids: ["N4"] });
  assert.match(lastToast(env), /已开始复盘（最多 60 秒）/);
  env.sse("review.progress", { id: "N4", bot_id: F.botId(4), name: "David", state: "running", message: "正在复盘…", seconds_left: 44, deadline_s: 60, changes: null });
  assert.match(textOf(html(env, "detail-live")), /⏳ 复盘中…（还剩 44 秒） 正在复盘…/);
  assert.ok(!html(env, "detail-live").includes("review-one"), "no second review of the same game");
  const changes = {
    files: [
      { file: "people", before: "- Bob：话少", after: "- Bob：话少，但会抓票型" },
      { file: "playbook", before: "", after: "- [把握:低|支持:0|反例:0|来源:复盘] 先听完再站边" },
    ],
    proposals: 2,
  };
  env.sse("review.progress", { id: "N4", bot_id: F.botId(4), name: "David", state: "done", message: "已更新", seconds_left: 3, deadline_s: 60, changes });
  await flushAll(env);
  const live1 = html(env, "detail-live");
  assert.match(live1, /✓ 已复盘/);
  const summary = textOf(live1);
  assert.match(summary, /本次改动：印象、打法；2 条底层设定提案待确认（旧 → 新）/);
  assert.match(live1, /<div class="diff-line del">[^]*话少<\/span><\/div>/, "the old line");
  assert.match(live1, /<div class="diff-line add">[^]*话少，但会抓票型<\/span><\/div>/, "the new one");
  assert.match(live1, /data-detail-action="show-staging"/);
  // the proposals link opens the section of the proposals
  panelClick(env, "show-staging");
  await flushAll(env);
  assert.match(html(env, "detail-editors"), /<section class="detail-editor open" data-kind="staging">/);
  // folding the list
  panelClick(env, "toggle-changes");
  assert.ok(!html(env, "detail-live").includes("review-changes-list"));
  panelClick(env, "toggle-changes");
  assert.ok(html(env, "detail-live").includes("review-changes-list"));
  // other players can still be reviewed; a failed one shows why and can be retried
  cardClick(env, "N4");
  cardClick(env, "N5");
  env.sse("review.progress", { id: "N5", bot_id: F.botId(5), name: "Eve", state: "error", message: "模型不可用", seconds_left: null, deadline_s: 60, changes: null });
  assert.match(html(env, "detail-live"), /上次复盘失败：模型不可用/);
  assert.match(html(env, "detail-live"), /复盘此玩家/);
  assert.ok(env.toasts.some((t) => /复盘失败（Eve）：模型不可用/.test(t.textContent)));
});

test("a finished review that changed nothing says so; a game of the old format cannot be reviewed (the button says why)", async () => {
  const { env } = await live(finished());
  cardClick(env, "N6");
  env.sse("review.progress", { id: "N6", bot_id: F.botId(6), name: "Frank", state: "done", message: "", seconds_left: 5, deadline_s: 60, changes: { files: [], proposals: 0 } });
  assert.match(textOf(html(env, "detail-live")), /本次复盘没有改动记忆（战绩已计入）。/);
  const old = await live(finished({ review: { available: true, done: [], running: false, supported: false, deadline_s: 60, seconds_left: null } }));
  assert.equal(old.env.el("btn-review-all").disabled, true);
  assert.match(old.env.el("btn-review-all").getAttribute("title"), /旧版存档，不能复盘/);
  cardClick(old.env, "N2");
  assert.match(html(old.env, "detail-live"), /data-detail-action="review-one"[^>]* disabled/);
  assert.match(textOf(html(old.env, "detail-live")), /这局是旧版存档，不能复盘。/);
});

test("review errors from the server are toasted (e.g. not finished / already reviewed)", async () => {
  const { env, backend } = await live(finished());
  backend.reviewError = "所选玩家本局都已复盘";
  env.el("btn-review-all").dispatch("click");
  await flushAll(env);
  assert.match(lastToast(env), /复盘失败：.*所选玩家本局都已复盘/);
  assert.equal(env.el("btn-review-all").disabled, false, "usable again");
});

test("when a review finishes the memory of the bot is read again: what was typed over the old text is dropped", async () => {
  const { env, backend } = await live(finished());
  cardClick(env, "N4");
  panelClick(env, "toggle-editor", { "data-kind": "memory" });
  await flushAll(env);
  env.el("detail-panel").dispatch("input", { target: { id: "ta-part-people", value: "- Bob：我自己写的", getAttribute: (k) => (k === "data-part-text" ? "people" : null) } });
  assert.match(html(env, "detail-editors"), /id="btn-part-save-people"[^>]*>保存/);
  const gets = () => backend.calls.filter((c) => c.method === "GET" && c.url.endsWith(`/api/bots/${F.botId(4)}`)).length;
  const before = gets();
  backend.botsApi.bot("N4").mem.people = "- Eve：复盘写的";
  env.sse("review.progress", { id: "N4", bot_id: F.botId(4), name: "David", state: "done", message: "", seconds_left: 5, deadline_s: 60, changes: { files: [{ file: "people", before: "", after: "- Eve：复盘写的" }], proposals: 0 } });
  await flushAll(env);
  assert.ok(gets() >= before + 1, "the detail was read again (the summary may not tell that a text of the same length changed)");
  assert.match(html(env, "detail-editors"), />- Eve：复盘写的\n<\/textarea>|>- Eve：复盘写的<\/textarea>/, "the box shows what the review wrote");
});

// =====================================================================================================
// Bots.focus (the start check of web-play jumps here)
// =====================================================================================================
test("Bots.focus: edit_core / import_core open the panel of the bot at the L0 editor; init at the initialization row; the others go where they belong", async () => {
  const { env } = await live({ status: "idle" }, { botsPreset: "mixed" });
  const Bots = env.sandbox.Bots;
  assert.equal(typeof Bots.focus, "function", "window.Bots.focus exists");
  assert.equal(Bots.focus(F.botId(6), "edit_core"), true);
  await flushAll(env);
  assert.equal(env.app().bots().openId(), "N6");
  assert.match(html(env, "detail-editors"), /<section class="detail-editor open" data-kind="core">/);
  assert.match(html(env, "detail-editors"), /id="ta-core-identity"/);
  Bots.focus(F.botId(2), "import_core");
  await flushAll(env);
  assert.equal(env.app().bots().openId(), "N2", "the panel moved to the other bot");
  assert.match(html(env, "detail-editors"), /id="btn-core-import"/);
  Bots.focus(F.botId(4), "init");
  await flushAll(env);
  assert.equal(env.app().bots().openId(), "N4");
  assert.match(html(env, "detail-head"), /id="detail-init"/);
  // a bot that is not at the table: nothing happens
  assert.equal(Bots.focus("b-ffffffff", "init"), false);
  assert.equal(env.app().bots().openId(), "N4");
  // the model: the bindings dialog; the contract / the rules: the settings dialog
  env.el("bindings-modal").style.display = "none";
  assert.equal(Bots.focus(F.botId(5), "bind_model"), true);
  await flushAll(env);
  assert.equal(env.el("bindings-modal").style.display, "flex");
  assert.equal(Bots.focus("", "import_contract"), true);
  await flushAll(env);
  assert.equal(env.el("settings-modal").style.display, "flex");
  assert.equal(Bots.focus("", "weird_fix"), false);
});

// =====================================================================================================
// an older backend, a new game
// =====================================================================================================
test("an older backend without the bots API: the editors are off with an explanation, the rest of the UI works", async () => {
  const { env } = await live({ status: "idle" }, { failures: { "GET /api/bots": [404, "Not Found"] } });
  cardClick(env, "N3");
  assert.match(html(env, "detail-editors"), /bots 接口不可用/);
  assert.equal(env.el("btn-init-all").disabled, true);
  assert.equal(env.el("btn-roster-regen").disabled, true);
  assert.equal(session(env).participants.length, 12);
});

test("a new game reloads the bots (the names, avatars and memory may have changed in between); ordinary events do not", async () => {
  const { env, backend } = await live({ status: "idle" });
  const n = () => backend.calls.filter((c) => c.method === "GET" && c.url.endsWith("/api/bots")).length;
  const before = n();
  env.sse("snapshot", { session: F.makeSession({ session_id: "another-game", status: "idle", event_id: 900 }) }, 900);
  await flushAll(env);
  assert.equal(n(), before + 1);
  env.sse("phase.changed", { text: "x" }, 901);
  await flushAll(env, 10);
  assert.equal(n(), before + 1, "ordinary events do not");
});

test("before the game starts no card has an identity (not even a '?') and the panel shows none; after the start they appear (H52)", async () => {
  const session0 = F.undealt(F.makeSession({ status: "idle", event_id: 500 }));
  const { env } = await live({ ...session0, event_id: 500 });
  assert.equal(session(env).dealt, false);
  cardClick(env, "N3");
  assert.doesNotMatch(html(env, "detail-live"), /身份：\?|tag-badge/, "(the cards are web-play's: they hide the tag before the deal)");
  env.sse("session.updated", { dealt: true, participants: undefined }, 501);
});

H.runTests(tests);
