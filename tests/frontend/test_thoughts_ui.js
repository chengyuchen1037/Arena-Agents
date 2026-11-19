"use strict";
// v0.13.1 (G21-G26, contract §12.4): the thinking record window (static/js/thoughts.js) and its button in the detail panel
//   the button (players and the moderator), disabled with the reason while ANOTHER seat is played and the game is not over
//   the window: a chat log, one divider per wake-up (game time), bubbles for thinking / self-talk, newest at the bottom
//   paging: "加载更早的记录" goes back with `before` = next_before;  polling: every 3 s the newest page again, new ids only
//   errors: the server's detail as it is (409 / 404 / 400);  Esc / ✕ / the panel next to it
// Run: node tests/frontend/test_thoughts_ui.js
const assert = require("node:assert/strict");
const path = require("node:path");
const H = require("./harness.js");
const Thoughts = require(path.join(__dirname, "..", "..", "static", "js", "thoughts.js"));

const { F, target, settle, bootLive } = H;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

async function live(over = {}, overrides = {}, envOpts = {}) {
  const session = F.makeSession({ event_id: 500, ...over });
  const ctx = await bootLive({ session, ...overrides }, envOpts);
  ctx.env.sse("snapshot", { session }, 500);
  return ctx;
}

const flush = async (env, ms = 30) => {
  await settle(ms);
  env.flushRaf();
};
const html = (env, id) => env.el(id).innerHTML;
const unesc = (s) => s.replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
const textOf = (s) => unesc(s.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim());
const cardClick = (env, id) => env.el("participants-grid").dispatch("click", { target: target({ ".pcard": { "data-actor": id } }) });
const modClick = (env) => env.el("topbar-status").dispatch("click", { target: target({ "#mod-chip": { "data-actor": "moderator" } }) });
const panelClick = (env, action) => env.el("detail-panel").dispatch("click", { target: target({ "[data-detail-action]": { "data-detail-action": action } }) });
const listClick = (env, action, extra = {}) => env.el("thoughts-list").dispatch("click", { target: target({ "[data-thoughts-action]": { "data-thoughts-action": action, ...extra } }) });
const ui = (env) => env.app().thoughts();
const win = (env) => env.el("thoughts-window");
/** The newest-last record of an agent: `gen` changes every `per` items (one wake-up), `round` counts inside it. */
const record = (n, per = 3, over = {}) =>
  Array.from({ length: n }, (_, i) => F.makeThought(i + 1, { gen: Math.floor(i / per) + 1, round: (i % per) + 1, kind: i % 5 === 4 ? "content" : "thought", text: `第 ${i + 1} 条`, ...over }));
const bubbles = (env) => [...html(env, "thoughts-list").matchAll(/data-thought-id="([^"]+)"/g)].map((m) => m[1]).filter((id, i, all) => all.indexOf(id) === i && html(env, "thoughts-list").includes(`<article class="th-bubble`) );
const texts = (env) => [...html(env, "thoughts-list").matchAll(/<div class="th-text">([^<]*)<\/div>/g)].map((m) => unesc(m[1]));
const dividers = (env) => (html(env, "thoughts-list").match(/class="th-divider"/g) || []).length;
const requests = (backend) => backend.thoughtRequests || [];
/** Run the poll that is scheduled (the one the window armed), then let the answer arrive. */
const runPoll = async (env) => {
  const id = ui(env)._state.timer;
  const i = env.timers.findIndex((t) => t.id === id);
  assert.ok(i >= 0, "a poll is scheduled");
  const [t] = env.timers.splice(i, 1);
  assert.equal(t.ms, 3000, "every 3 seconds (G22)");
  await t.fn();
  await flush(env);
};
const open = async (env, id) => {
  if (id === "moderator") modClick(env);
  else cardClick(env, id);
  panelClick(env, "open-thoughts");
  await flush(env);
};
/** Give the list a scroll geometry (the fake DOM has none): the height follows the content, so that prepending moves things. */
const geometry = (env, { client = 300, perChar = 0.1 } = {}) => {
  const list = env.el("thoughts-list");
  Object.defineProperty(list, "scrollHeight", { get: () => Math.round(list.innerHTML.length * perChar), configurable: true });
  list.clientHeight = client;
  return list;
};

// =====================================================================================================
// pure helpers
// =====================================================================================================
test("blockedReason: another agent's thoughts are closed while a seat is played and the game is not over (G25); the played seat's own are open", () => {
  const played = (status, seat = "N3") => ({ status, takeover: { seat } });
  const reason = "接管期间不能查看其他玩家的思考记录，对局结束后开放。";
  assert.equal(Thoughts.blockedReason(played("paused"), "N4"), reason);
  assert.equal(Thoughts.blockedReason(played("running"), "moderator"), reason, "the moderator is another agent too");
  assert.equal(Thoughts.blockedReason(played("paused"), "N3"), "", "the played seat's own record (its agent thought before the takeover)");
  assert.equal(Thoughts.blockedReason(played("finished"), "N4"), "", "open after the end");
  assert.equal(Thoughts.blockedReason({ status: "paused", takeover: { seat: null } }, "N4"), "", "nobody is played: a spectator may always look");
  assert.equal(Thoughts.blockedReason({ status: "running" }, "N4"), "");
  assert.equal(Thoughts.blockedReason(null, "N4"), "");
  assert.equal(Thoughts.blockedReason({ status: "idle" }, "N4"), "");
  // a replay shows another game than the server's record
  assert.match(Thoughts.blockedReason({ status: "finished" }, "N4", { replaying: true }), /重放的是历史对局：思考记录只针对当前对局/);
  assert.equal(Thoughts.blockedReason({ status: "finished" }, "N4", { replaying: false }), "");
});

test("normalizeItem / normalizePage: every field present and typed, anything that is no item is dropped, a cursor needs has_more", () => {
  assert.deepEqual(Thoughts.normalizeItem({ id: 7, ts: "2026-11-18T10:00:00Z", gt: "12.5", gen: 2, round: "3", kind: "content", text: "hi", model: "m" }), {
    id: "7", ts: "2026-11-18T10:00:00Z", gt: 12.5, gen: 2, round: 3, kind: "content", text: "hi", model: "m",
  });
  assert.deepEqual(Thoughts.normalizeItem({ id: "a" }), { id: "a", ts: "", gt: null, gen: null, round: null, kind: "thought", text: "", model: null });
  assert.equal(Thoughts.normalizeItem({ id: "x", kind: "weird" }).kind, "thought", "only the two kinds of G23 exist");
  assert.equal(Thoughts.normalizeItem({ text: "no id" }), null);
  assert.equal(Thoughts.normalizeItem(null), null);
  assert.equal(Thoughts.normalizeItem([1]), null);
  const page = Thoughts.normalizePage({ agent_id: "N4", name: "David", items: [{ id: "a" }, null, { nope: 1 }, { id: "b" }], next_before: "a", has_more: true });
  assert.deepEqual(page.items.map((i) => i.id), ["a", "b"]);
  assert.deepEqual([page.agent_id, page.name, page.next_before, page.has_more], ["N4", "David", "a", true]);
  assert.equal(Thoughts.normalizePage({ items: [], has_more: true }).has_more, false, "no cursor: nothing to go back to");
  assert.deepEqual(Thoughts.normalizePage(null), { agent_id: "", name: "", items: [], next_before: null, has_more: false });
  assert.deepEqual(Thoughts.normalizePage({ items: "x" }).items, []);
});

test("buildRows puts a divider before the first item and wherever the wake-up (gen) changes; freshItems keeps the ids that are new", () => {
  const items = record(7, 3).map(Thoughts.normalizeItem);
  const rows = Thoughts.buildRows(items);
  assert.deepEqual(rows.map((r) => r.type), ["divider", "bubble", "bubble", "bubble", "divider", "bubble", "bubble", "bubble", "divider", "bubble"]);
  assert.deepEqual(rows.filter((r) => r.type === "divider").map((r) => r.gen), [1, 2, 3]);
  assert.deepEqual(rows.filter((r) => r.type === "divider").map((r) => r.gt), [21, 24, 27], "a divider carries the game time of the first bubble");
  assert.deepEqual(Thoughts.buildRows([]), []);
  const unknown = Thoughts.buildRows([{ ...items[0], gen: null }, { ...items[1], gen: null }]);
  assert.equal(unknown.filter((r) => r.type === "divider").length, 1, "items without a wake-up number share one divider");
  const seen = new Set([items[0].id, items[1].id]);
  assert.deepEqual(Thoughts.freshItems(items.slice(0, 4), seen).map((i) => i.id), [items[2].id, items[3].id]);
});

test("a divider says when the agent was woken up (game time, else the local time); a bubble says what it is and which round", () => {
  const Core = H.Core;
  const d = Thoughts.dividerHtml({ type: "divider", gen: 4, gt: 125, ts: "" }, Core);
  assert.match(d, /class="th-divider" data-gen="4"><span>⏰ 被唤醒 · 游戏时间 2:05<\/span>/);
  assert.match(Thoughts.dividerHtml({ type: "divider", gen: 1, gt: 3725, ts: "" }, Core), /游戏时间 1:02:05/);
  assert.match(Thoughts.dividerHtml({ type: "divider", gen: 1, gt: null, ts: "2026-11-18T10:03:12" }, Core), /被唤醒 · 时间 \d\d:\d\d:\d\d/);
  assert.match(Thoughts.dividerHtml({ type: "divider", gen: null, gt: null, ts: "" }, Core), /<span>⏰ 被唤醒<\/span>/);
  const item = Thoughts.normalizeItem(F.makeThought(5, { round: 2, kind: "content", text: "a <b> & \"c\"\n第二行", model: "deepseek-chat" }));
  const b = Thoughts.bubbleHtml(item, { Core, expanded: false });
  assert.match(b, /class="th-bubble th-content" data-thought-id="1:5000"/);
  assert.match(b, /<span class="th-kind">💬 自言自语<\/span>/);
  assert.match(b, /<span class="th-round">第 2 轮<\/span>/);
  assert.match(b, /<span class="th-model" title="模型">deepseek-chat<\/span>/);
  assert.match(b, /<div class="th-text">a &lt;b&gt; &amp; &quot;c&quot;\n第二行<\/div>/, "escaped; line breaks stay (the CSS keeps them)");
  assert.doesNotMatch(b, /th-fold/, "short: no fold button");
  assert.match(Thoughts.bubbleHtml(Thoughts.normalizeItem(F.makeThought(1)), { Core }), /🧠 思考/);
  // a long one is folded: the first 700 characters and a way to the rest
  const long = Thoughts.normalizeItem(F.makeThought(6, { text: "长".repeat(1000) }));
  const folded = Thoughts.bubbleHtml(long, { Core, expanded: false });
  assert.match(folded, /th-folded/);
  assert.ok(folded.includes("长".repeat(700) + "…") && !folded.includes("长".repeat(701)));
  assert.match(folded, /data-thoughts-action="fold" data-thought-id="1:6000">展开全文（1000 字）<\/button>/);
  const open_ = Thoughts.bubbleHtml(long, { Core, expanded: true });
  assert.ok(open_.includes("长".repeat(1000)));
  assert.match(open_, />收起<\/button>/);
});

// =====================================================================================================
// the button (G21, G25)
// =====================================================================================================
test("the detail panel of a player and of the moderator has '🧠 思考记录'; it opens the window and reads the newest page", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.thoughts.N4 = record(7);
  backend.thoughts.moderator = record(4, 2, { text: "主持人想：" });
  cardClick(env, "N4");
  const head = html(env, "detail-head");
  assert.match(head, /id="btn-detail-thoughts" data-detail-action="open-thoughts" title="像聊天记录一样看这名玩家的思考（不含工具调用）">🧠 思考记录<\/button>/);
  assert.doesNotMatch(head, /id="btn-detail-thoughts"[^>]* disabled/);
  assert.ok(!head.includes("detail-thoughts-note"), "no reason to give");
  assert.equal(win(env).hidden, true, "closed until asked for");
  panelClick(env, "open-thoughts");
  await flush(env);
  assert.equal(win(env).hidden, false);
  assert.deepEqual(requests(backend), [{ id: "N4", limit: "50" }], "one request: the newest page, no cursor");
  assert.equal(env.app().roster().openId(), "N4", "the detail panel stays open next to the window");
  assert.match(textOf(html(env, "thoughts-head")), /🧠 David 的思考记录/);
  assert.match(textOf(html(env, "thoughts-head")), /4 号 · 只显示思考与自言自语，不显示工具调用/);
  assert.match(html(env, "thoughts-head"), /<img class="avatar-img vw-avatar-img"/, "the avatar of the agent");
  // the moderator has one too (the panel of the moderator is the same)
  modClick(env);
  assert.match(html(env, "detail-head"), /id="btn-detail-thoughts" data-detail-action="open-thoughts" title="像聊天记录一样看主持人的思考/);
  panelClick(env, "open-thoughts");
  await flush(env);
  assert.deepEqual(requests(backend).pop(), { id: "moderator", limit: "50" });
  assert.match(textOf(html(env, "thoughts-head")), /🧠 主持人 的思考记录/);
  assert.match(textOf(html(env, "thoughts-head")), /主持人 · 只显示思考/);
  assert.ok(texts(env).every((t) => t.startsWith("主持人想：")), "the window switched to the moderator's record");
  assert.equal(ui(env).agentId(), "moderator");
});

test("the window is a chat log: bubbles in time order, newest at the bottom, a divider per wake-up with the game time, the view starts at the bottom", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.thoughts.N4 = record(7);
  const list = env.el("thoughts-list");
  list.scrollHeight = 900;
  list.clientHeight = 300;
  await open(env, "N4");
  assert.deepEqual(texts(env), ["第 1 条", "第 2 条", "第 3 条", "第 4 条", "第 5 条", "第 6 条", "第 7 条"], "oldest first");
  assert.equal(dividers(env), 3, "three wake-ups");
  const body = html(env, "thoughts-list");
  assert.ok(body.indexOf("游戏时间 0:21") < body.indexOf("第 1 条") && body.indexOf("第 3 条") < body.indexOf("游戏时间 0:24"), "dividers between the groups");
  assert.match(body, /<span class="th-round">第 3 轮<\/span>/);
  assert.match(body, /class="th-bubble th-content"/, "self-talk is told apart from thinking");
  assert.match(body, /— 这是最早的记录 —/, "nothing older: the log says it starts here");
  assert.doesNotMatch(body, /加载更早的记录/);
  assert.equal(list.scrollTop, 900, "opened at the bottom, where the newest is");
  assert.match(html(env, "thoughts-foot"), /已显示 7 条 · 窗口打开期间每 3 秒读取一次新内容/);
  assert.ok(!body.includes("tool") && !body.includes("工具调用"), "tool calls are never part of the record");
});

test("empty record: no activity yet / a model that returned no thinking are told apart; the first load says so while it waits", async () => {
  const { env, backend } = await live({ status: "running" });
  // N7 never called a model in the fixture; the moderator did (2 calls) but its model returned no thinking
  await open(env, "N7");
  assert.match(textOf(html(env, "thoughts-list")), /还没有思考记录：这名玩家还没有开始行动。对局开始后，每次被唤醒的思考都会按时间出现在这里。/);
  await open(env, "moderator");
  assert.match(textOf(html(env, "thoughts-list")), /该模型没有返回思考内容（或这局的日志不含思考记录：例如导入时选择了“不含日志”）。/);
  // while the first page is on its way
  let release;
  const gate = new Promise((r) => (release = r));
  const realFetch = env.sandbox.fetch;
  env.sandbox.fetch = async (url, init) => {
    if (/thoughts/.test(url)) await gate;
    return realFetch(url, init);
  };
  backend.thoughts.N5 = record(2);
  cardClick(env, "moderator"); // (closes the moderator's panel)
  cardClick(env, "N5");
  panelClick(env, "open-thoughts");
  await flush(env, 5);
  assert.match(html(env, "thoughts-list"), /<div class="th-state">读取中…<\/div>/);
  release();
  await flush(env);
  assert.deepEqual(texts(env), ["第 1 条", "第 2 条"]);
});

test("while ANOTHER seat is played the buttons of the others are disabled with the reason as tooltip and as a line of text; the played seat's own works", async () => {
  const played = F.makePlayedSession("N3", { event_id: 500 });
  const { env, backend } = await bootLive({ session: played });
  env.sse("snapshot", { session: played }, 500);
  backend.thoughts.N3 = record(3);
  backend.thoughts.N4 = record(3);
  const reason = "接管期间不能查看其他玩家的思考记录，对局结束后开放。";
  cardClick(env, "N4");
  let head = html(env, "detail-head");
  assert.match(head, new RegExp(`id="btn-detail-thoughts" data-detail-action="open-thoughts" disabled title="${reason}">🧠 思考记录</button>`));
  assert.match(head, new RegExp(`<div class="form-hint detail-thoughts-note">🔒 ${reason}</div>`), "a phone has no tooltips: the reason is written out too");
  panelClick(env, "open-thoughts"); // a stale click on the disabled button
  await flush(env);
  assert.equal(win(env).hidden, true);
  assert.equal(requests(backend).length, 0, "nothing is even asked");
  // the moderator is another agent as well
  modClick(env);
  assert.match(html(env, "detail-head"), /id="btn-detail-thoughts"[^>]* disabled/);
  assert.match(html(env, "detail-head"), /detail-thoughts-note/);
  // the played seat's own record is open (its agent thought before the takeover)
  cardClick(env, "moderator");
  cardClick(env, "N3");
  head = html(env, "detail-head");
  assert.doesNotMatch(head, /id="btn-detail-thoughts"[^>]* disabled/);
  assert.ok(!head.includes("detail-thoughts-note"));
  panelClick(env, "open-thoughts");
  await flush(env);
  assert.equal(win(env).hidden, false);
  assert.deepEqual(requests(backend), [{ id: "N3", limit: "50" }]);
  // after the game ended everything is open again - the panel follows the event
  cardClick(env, "N3");
  cardClick(env, "N4");
  assert.match(html(env, "detail-head"), /id="btn-detail-thoughts"[^>]* disabled/);
  backend.session.status = "finished"; // (the server knows the game is over - it would answer 409 otherwise)
  env.sse("game.status", { status: "finished", reason: "end_game", winner: "好人阵营", summary: "x" });
  head = html(env, "detail-head");
  assert.doesNotMatch(head, /id="btn-detail-thoughts"[^>]* disabled/);
  assert.ok(!head.includes("detail-thoughts-note"));
  panelClick(env, "open-thoughts");
  await flush(env);
  assert.deepEqual(requests(backend).pop(), { id: "N4", limit: "50" });
  assert.equal(texts(env).length, 3);
});

test("an open window closes its view when a seat is taken over (and opens again at the end); no request is made while it is closed", async () => {
  const { env, backend } = await live({ status: "paused" });
  backend.thoughts.N4 = record(3);
  await open(env, "N4");
  assert.equal(texts(env).length, 3);
  const before = requests(backend).length;
  // a seat is taken over: the other agents' thoughts are closed from now on
  const takeover = F.makeTakeover("N3");
  env.sse("takeover.updated", { takeover });
  assert.match(textOf(html(env, "thoughts-list")), /🔒 接管期间不能查看其他玩家的思考记录，对局结束后开放。/);
  assert.equal(html(env, "thoughts-foot"), "");
  assert.equal(texts(env).length, 0, "the bubbles are not shown any more");
  backend.thoughts.N4.push(F.makeThought(4, { text: "第 4 条" }));
  await runPoll(env);
  assert.equal(requests(backend).length, before, "the poll runs but asks for nothing");
  // the game ends: open again, with what has come in meanwhile
  env.sse("game.status", { status: "finished", reason: "end_game", winner: "好人阵营", summary: "x" });
  assert.equal(texts(env).length, 3, "what was loaded is shown again at once");
  await runPoll(env);
  assert.equal(requests(backend).length, before + 1);
  assert.equal(texts(env).length, 4);
});

test("a replay shows another game: the button is disabled with its own reason, the window follows the game that is on screen", async () => {
  const { env, backend } = await live({ status: "finished" });
  backend.thoughts.N4 = record(3);
  await open(env, "N4");
  assert.equal(texts(env).length, 3);
  const app = env.app();
  const liveSession = app.state.session;
  // (what enterReplay does: the replayed session becomes the one on screen)
  app.state.replay = { live: liveSession, installing: false };
  app.state.session = F.makeSession({ session_id: "old-1", status: "finished", event_id: 1 });
  ui(env).onSession(app.state.session);
  app.roster().render();
  assert.match(textOf(html(env, "thoughts-list")), /🔒 重放的是历史对局：思考记录只针对当前对局，退出重放后再看。/);
  assert.match(html(env, "detail-head"), /id="btn-detail-thoughts"[^>]* disabled title="重放的是历史对局/);
  const asked = requests(backend).length;
  // leaving the replay: the live game's record is read afresh (it is another game from the page's point of view)
  app.state.replay = null;
  app.state.session = liveSession;
  ui(env).onSession(liveSession);
  await flush(env);
  assert.equal(requests(backend).length, asked + 1);
  assert.equal(texts(env).length, 3);
});

// =====================================================================================================
// going back (G22)
// =====================================================================================================
test("'加载更早的记录' goes back with before = next_before; older bubbles go in FRONT, no id twice, the reader's place is kept; the start is announced", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.thoughts.N4 = record(120);
  const list = geometry(env);
  await open(env, "N4");
  assert.deepEqual(requests(backend), [{ id: "N4", limit: "50" }]);
  assert.equal(texts(env).length, 50);
  assert.equal(texts(env)[0], "第 71 条");
  assert.equal(texts(env)[49], "第 120 条", "the NEWEST page first");
  assert.match(html(env, "thoughts-list"), /data-thoughts-action="more"[^>]*>↑ 加载更早的记录<\/button>/);
  assert.doesNotMatch(html(env, "thoughts-list"), /这是最早的记录/);
  assert.match(html(env, "thoughts-foot"), /已显示 50 条（还有更早的）/);

  list.scrollTop = 5; // the reader is near the top: that is where "earlier" is wanted
  const heightBefore = list.scrollHeight;
  listClick(env, "more");
  await flush(env);
  assert.deepEqual(requests(backend).pop(), { id: "N4", before: "1:71000", limit: "50" }, "the cursor of the oldest item we hold");
  assert.equal(texts(env).length, 100);
  assert.equal(texts(env)[0], "第 21 条");
  assert.equal(texts(env)[99], "第 120 条");
  // (the page grew above the reader by the height of the 50 older bubbles; the "读取中…" label of the button differs by a pixel)
  assert.ok(Math.abs(list.scrollTop - (5 + (list.scrollHeight - heightBefore))) <= 2, `scrollTop ${list.scrollTop}: the bubble that was at the top is still where the reader's eyes are`);
  assert.ok(list.scrollTop > 1000, "(and it did move: the older bubbles are above)");
  listClick(env, "more");
  await flush(env);
  assert.deepEqual(requests(backend).pop(), { id: "N4", before: "1:21000", limit: "50" });
  assert.equal(texts(env).length, 120);
  assert.deepEqual(texts(env), Array.from({ length: 120 }, (_, i) => `第 ${i + 1} 条`), "every item once, in order");
  assert.match(html(env, "thoughts-list"), /— 这是最早的记录 —/);
  assert.doesNotMatch(html(env, "thoughts-list"), /加载更早的记录/);
  assert.match(html(env, "thoughts-foot"), /已显示 120 条 ·/);
  assert.equal(dividers(env), 40, "a divider for every wake-up");
});

test("scrolling to the top loads the earlier page by itself; a second request is not made while one is on its way", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.thoughts.N4 = record(80);
  const list = geometry(env);
  await open(env, "N4");
  list.scrollTop = 900;
  list.dispatch("scroll");
  assert.equal(requests(backend).length, 1, "far from the top: nothing to load");
  let release;
  const gate = new Promise((r) => (release = r));
  const realFetch = env.sandbox.fetch;
  env.sandbox.fetch = async (url, init) => {
    if (/before=/.test(url)) await gate;
    return realFetch(url, init);
  };
  list.scrollTop = 3;
  list.dispatch("scroll");
  list.dispatch("scroll");
  listClick(env, "more");
  await flush(env, 5);
  assert.match(html(env, "thoughts-list"), /data-thoughts-action="more"[^>]* disabled[^>]*>读取中…<\/button>/, "the button says it is working");
  release();
  await flush(env);
  assert.equal(requests(backend).filter((r) => r.before).length, 1, "one request for the earlier page, however often it was asked");
  assert.equal(texts(env).length, 80);
});

test("a huge log is searched one stretch per request: an EMPTY newest page that has_more offers '继续往前找' and goes on with its cursor (§12.4)", async () => {
  const { env } = await live({ status: "running" });
  const realFetch = env.sandbox.fetch;
  const asked = [];
  env.sandbox.fetch = async (url, init) => {
    if (/agents\/N4\/thoughts/.test(url)) {
      asked.push(url);
      if (!/before=/.test(url)) return H.resp(200, { agent_id: "N4", name: "David", items: [], next_before: "2:900", has_more: true });
      if (/before=2%3A900/.test(url)) return H.resp(200, { agent_id: "N4", name: "David", items: [F.makeThought(1), F.makeThought(2)], next_before: null, has_more: false });
    }
    return realFetch(url, init);
  };
  await open(env, "N4");
  const empty = html(env, "thoughts-list");
  assert.match(textOf(empty), /最近的日志里还没有找到思考记录（日志很大，每次只查一段）。/);
  assert.match(empty, /data-thoughts-action="more"[^>]*>继续往前找<\/button>/);
  assert.doesNotMatch(textOf(empty), /还没有开始行动|该模型没有返回思考内容/, "this is not the 'nothing there' message: there IS something further back");
  listClick(env, "more");
  await flush(env);
  assert.ok(asked.some((u) => /before=2%3A900&limit=50/.test(u)), "the cursor of the empty page is the one that is followed");
  assert.deepEqual(texts(env), ["第 1 条思考", "第 2 条思考"]);
  assert.match(html(env, "thoughts-list"), /— 这是最早的记录 —/);
  assert.doesNotMatch(html(env, "thoughts-list"), /继续往前找/);
});

// =====================================================================================================
// polling (G22: every 3 seconds, no new SSE event)
// =====================================================================================================
test("while open the newest page is read again every 3 s; only the ids that are new are added; nothing new means no repaint", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.thoughts.N4 = record(7);
  const list = env.el("thoughts-list");
  list.scrollHeight = 700;
  list.clientHeight = 300;
  await open(env, "N4");
  assert.ok(ui(env)._state.timer !== null, "the poll is armed");
  const painted = html(env, "thoughts-list");
  await runPoll(env);
  assert.deepEqual(requests(backend).pop(), { id: "N4", limit: "50" }, "the newest page again - there is no `after`");
  assert.equal(html(env, "thoughts-list"), painted, "nothing new: the same HTML (and the reader's scroll position) stays");
  assert.ok(ui(env)._state.timer !== null, "and the next poll is armed");
  // two new bubbles in the middle of a wake-up and one in a new one
  backend.thoughts.N4.push(F.makeThought(8, { gen: 3, round: 2, text: "第 8 条" }), F.makeThought(9, { gen: 3, round: 3, text: "第 9 条" }), F.makeThought(10, { gen: 4, text: "第 10 条" }));
  list.scrollHeight = 1300;
  await runPoll(env);
  assert.deepEqual(texts(env), Array.from({ length: 10 }, (_, i) => `第 ${i + 1} 条`));
  assert.equal(dividers(env), 4);
  assert.equal(list.scrollTop, 1300, "at the bottom: the view follows the newest");
  assert.equal(ui(env)._state.unseen, 0);
  assert.equal(env.el("btn-thoughts-latest").hidden, true);
});

test("a reader who scrolled up is not pulled down: a '↓ 有 N 条新内容' button appears and takes them to the newest", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.thoughts.N4 = record(30);
  const list = geometry(env);
  await open(env, "N4");
  list.scrollTop = 40; // far above the bottom (the content is ~1000 tall, the window 300)
  list.dispatch("scroll");
  assert.equal(ui(env)._state.follow, false);
  backend.thoughts.N4.push(F.makeThought(31, { gen: 11, text: "第 31 条" }), F.makeThought(32, { gen: 11, round: 2, text: "第 32 条" }));
  await runPoll(env);
  assert.equal(texts(env).length, 32);
  assert.equal(ui(env)._state.unseen, 2);
  assert.equal(env.el("btn-thoughts-latest").hidden, false);
  assert.equal(env.el("btn-thoughts-latest").textContent, "↓ 有 2 条新内容");
  assert.equal(list.scrollTop, 40, "the view did not move");
  env.el("btn-thoughts-latest").dispatch("click");
  assert.equal(list.scrollTop, list.scrollHeight);
  assert.equal(env.el("btn-thoughts-latest").hidden, true);
  assert.equal(ui(env)._state.follow, true);
  // scrolling down by hand clears the notice as well
  list.scrollTop = 40;
  list.dispatch("scroll");
  backend.thoughts.N4.push(F.makeThought(33, { gen: 12, text: "第 33 条" }));
  await runPoll(env);
  assert.equal(env.el("btn-thoughts-latest").hidden, false);
  list.scrollTop = list.scrollHeight - list.clientHeight;
  list.dispatch("scroll");
  assert.equal(env.el("btn-thoughts-latest").hidden, true);
  assert.equal(ui(env)._state.unseen, 0);
});

test("more happened than a page since the last poll: the poll walks back until it meets something it knows, and nothing is lost or doubled", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.thoughts.N4 = record(30);
  await open(env, "N4");
  assert.equal(texts(env).length, 30);
  for (let n = 31; n <= 150; n++) backend.thoughts.N4.push(F.makeThought(n, { gen: Math.floor((n - 1) / 3) + 1, round: ((n - 1) % 3) + 1, text: `第 ${n} 条` }));
  const asked = requests(backend).length;
  await runPoll(env);
  const made = requests(backend).slice(asked);
  assert.equal(made[0].before, undefined, "first the newest page");
  assert.ok(made.length >= 3 && made.slice(1).every((r) => r.before), "then older pages by cursor");
  assert.deepEqual(texts(env), Array.from({ length: 150 }, (_, i) => `第 ${i + 1} 条`), "the whole stretch, in order, once");
});

const nth = (n) => F.makeThought(n, { gen: Math.floor((n - 1) / 3) + 1, round: ((n - 1) % 3) + 1, text: `第 ${n} 条` });
const upTo = (n) => Array.from({ length: n }, (_, i) => `第 ${i + 1} 条`);
const beforeOf = (reqs) => reqs.map((r) => r.before).filter(Boolean);

test("MORE than 20 pages came in since the last poll (a throttled tab): the hole is remembered and filled a few pages per poll - never given up, never fetched twice", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.thoughts.N4 = record(30);
  await open(env, "N4");
  for (let n = 31; n <= 1250; n++) backend.thoughts.N4.push(nth(n)); // 1220 new items: 25 pages
  const asked = requests(backend).length;
  await runPoll(env);
  const first = requests(backend).slice(asked);
  assert.equal(first.length, 1 + Thoughts.MAX_GAP_PAGES, "the newest page and at most 20 older ones");
  const got = texts(env);
  assert.equal(got.length, 30 + 50 + 20 * 50, "what we had, the newest page, and the 20 pages next to it");
  assert.ok(got.includes("第 1250 条") && got.includes("第 201 条") && !got.includes("第 200 条") && !got.includes("第 31 条"), "the stretch in between is missing - for now");
  assert.equal(ui(env)._state.gaps.length, 1, "...and the page knows where");
  assert.match(html(env, "thoughts-foot"), /正在补读/);
  // the next poll: the newest page is known, the hole is filled from where it stopped
  const askedAgain = requests(backend).length;
  await runPoll(env);
  const second = requests(backend).slice(askedAgain);
  assert.deepEqual(texts(env), upTo(1250), "the whole log, in order, once");
  assert.equal(ui(env)._state.gaps.length, 0);
  assert.doesNotMatch(html(env, "thoughts-foot"), /正在补读/);
  assert.equal(second.length, 1 + 4, "the newest page, and the four pages that were left (the last one meets what we had)");
  // no page was asked for twice: the cursors of all the requests are different
  const cursors = beforeOf(requests(backend).slice(asked));
  assert.equal(new Set(cursors).size, cursors.length, "every older page once");
  // and it stays whole: a poll now is one request
  const calm = requests(backend).length;
  await runPoll(env);
  assert.equal(requests(backend).length, calm + 1);
  assert.deepEqual(texts(env), upTo(1250));
});

test("a second hole opens while the first one is still being filled: the newest hole is filled first, every poll stays bounded, the list ends up whole", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.thoughts.N4 = record(30);
  await open(env, "N4");
  for (let n = 31; n <= 1250; n++) backend.thoughts.N4.push(nth(n));
  await runPoll(env); // hole 1 is open (items 31 - 200)
  assert.equal(ui(env)._state.gaps.length, 1);
  for (let n = 1251; n <= 2350; n++) backend.thoughts.N4.push(nth(n)); // 1100 more: a new hole above the first poll's block
  const sizes = [];
  for (let polls = 0; polls < 6 && (polls === 0 || ui(env)._state.gaps.length); polls++) {
    const before = requests(backend).length;
    await runPoll(env);
    sizes.push(requests(backend).length - before);
  }
  assert.ok(sizes.every((n) => n <= 1 + Thoughts.MAX_GAP_PAGES), `no poll asked for more than 21 pages: ${sizes}`);
  assert.equal(ui(env)._state.gaps.length, 0, `closed after ${sizes.length} polls`);
  assert.deepEqual(texts(env), upTo(2350), "nothing lost, nothing doubled, in order");
  const cursors = beforeOf(requests(backend));
  assert.equal(cursors.length - new Set(cursors).size, 0, "(no older page was asked for twice)");
});

test("an empty record that suddenly has more than a page: the newest page is its end and '加载更早的记录' goes on from it", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.thoughts.N4 = [];
  await open(env, "N4");
  assert.equal(texts(env).length, 0);
  for (let n = 1; n <= 120; n++) backend.thoughts.N4.push(nth(n));
  await runPoll(env);
  assert.deepEqual(texts(env), Array.from({ length: 50 }, (_, i) => `第 ${i + 71} 条`), "the newest page");
  assert.equal(ui(env)._state.hasMore, true, "(the first 70 are behind it)");
  assert.match(html(env, "thoughts-list"), /加载更早的记录/);
  listClick(env, "more");
  await flush(env);
  listClick(env, "more");
  await flush(env);
  assert.deepEqual(texts(env), upTo(120));
  assert.doesNotMatch(html(env, "thoughts-list"), /加载更早的记录/);
  assert.match(html(env, "thoughts-list"), /这是最早的记录/);
});

test("closing the window stops the poll; opening it again reads afresh; a window never polls for an agent it is no longer showing", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.thoughts.N4 = record(3);
  backend.thoughts.N5 = record(2, 2, { text: "N5 想" });
  await open(env, "N4");
  const timerId = ui(env)._state.timer;
  assert.ok(env.timers.some((t) => t.id === timerId));
  env.el("btn-close-thoughts").dispatch("click");
  assert.equal(win(env).hidden, true);
  assert.equal(ui(env)._state.timer, null);
  assert.ok(!env.timers.some((t) => t.id === timerId), "the timer is gone");
  const asked = requests(backend).length;
  backend.thoughts.N4.push(F.makeThought(4, { text: "第 4 条" }));
  panelClick(env, "open-thoughts"); // (the panel is still open on N4: its button opens the window again)
  await flush(env);
  assert.equal(requests(backend).length, asked + 1);
  assert.equal(texts(env).length, 4, "what happened while it was closed is there");
  // another agent: the window is the other's, a late answer of the first is ignored
  let release;
  const gate = new Promise((r) => (release = r));
  const realFetch = env.sandbox.fetch;
  env.sandbox.fetch = async (url, init) => {
    if (/agents\/N4\//.test(url)) await gate;
    return realFetch(url, init);
  };
  panelClick(env, "open-thoughts"); // N4 again: a request that will be slow
  await flush(env, 5);
  cardClick(env, "N4");
  cardClick(env, "N5");
  panelClick(env, "open-thoughts");
  await flush(env);
  assert.deepEqual(texts(env), ["N5 想", "N5 想"]);
  release();
  await flush(env);
  assert.deepEqual(texts(env), ["N5 想", "N5 想"], "N4's late answer did not leak into N5's window");
  assert.equal(ui(env).agentId(), "N5");
});

test("a refresh button reads at once; opening the same agent twice does not start a second poll", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.thoughts.N4 = record(3);
  await open(env, "N4");
  const asked = requests(backend).length;
  backend.thoughts.N4.push(F.makeThought(4, { text: "第 4 条" }));
  env.el("btn-thoughts-refresh").dispatch("click");
  await flush(env);
  assert.equal(requests(backend).length, asked + 1);
  assert.equal(texts(env).length, 4);
  panelClick(env, "open-thoughts"); // the same agent again: brings it up to date, nothing more
  await flush(env);
  assert.equal(env.timers.filter((t) => t.id === ui(env)._state.timer).length, 1, "one poll timer");
  assert.equal(env.timers.filter((t) => t.ms === 3000 && t.id !== ui(env)._state.timer && t.fn.toString().includes("fetchLatest")).length, 0);
});

test("a new game while the window is open: its record is another one - the window starts over and reads it", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.thoughts.N4 = record(3);
  await open(env, "N4");
  assert.equal(texts(env).length, 3);
  backend.thoughts.N4 = [F.makeThought(1, { text: "新局第 1 条" })];
  env.sse("snapshot", { session: F.makeSession({ session_id: "next", status: "idle", event_id: 900 }) }, 900);
  await flush(env);
  assert.deepEqual(texts(env), ["新局第 1 条"]);
  assert.equal(ui(env).isOpen(), true, "the window stays; its content is the new game's");
  // the same game's snapshot (a reconnect) does not throw the log away
  const asked = requests(backend).length;
  env.sse("snapshot", { session: F.makeSession({ session_id: "next", status: "idle", event_id: 901 }) }, 901);
  await flush(env);
  assert.equal(requests(backend).length, asked);
  assert.deepEqual(texts(env), ["新局第 1 条"]);
});

// =====================================================================================================
// errors: the server's own words
// =====================================================================================================
test("the server's refusal is shown as it is (409 / 404 / 400); a network failure says so; a failing poll keeps what is there and recovers", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.thoughts.N4 = record(3);
  backend.thoughtsFailure = [409, "接管期间不能查看其他玩家的思考记录，对局结束后开放。"];
  await open(env, "N4");
  assert.match(html(env, "thoughts-list"), /<div class="th-state th-error" role="alert">接管期间不能查看其他玩家的思考记录，对局结束后开放。<\/div>/);
  assert.equal(win(env).hidden, false, "the window stays: the reason is the content");
  backend.thoughtsFailure = [404, "没有这个玩家。"];
  env.el("btn-thoughts-refresh").dispatch("click");
  await flush(env);
  assert.match(textOf(html(env, "thoughts-list")), /没有这个玩家。/);
  backend.thoughtsFailure = [400, "游标无效。"];
  env.el("btn-thoughts-refresh").dispatch("click");
  await flush(env);
  assert.match(textOf(html(env, "thoughts-list")), /游标无效。/);
  // it recovers by itself at the next poll (nothing was loaded: the poll is a first load)
  backend.thoughtsFailure = null;
  await runPoll(env);
  assert.deepEqual(texts(env), ["第 1 条", "第 2 条", "第 3 条"]);
  assert.doesNotMatch(html(env, "thoughts-list"), /th-error/);
  // a failing poll keeps the bubbles and shows the error under them
  const realFetch = env.sandbox.fetch;
  env.sandbox.fetch = async (url, init) => {
    if (/thoughts/.test(url)) throw new TypeError("Failed to fetch");
    return realFetch(url, init);
  };
  await runPoll(env);
  assert.deepEqual(texts(env), ["第 1 条", "第 2 条", "第 3 条"]);
  assert.match(html(env, "thoughts-list"), /th-state th-error" role="alert">无法连接到服务器/);
  env.sandbox.fetch = realFetch;
  backend.thoughts.N4.push(F.makeThought(4, { text: "第 4 条" }));
  await runPoll(env);
  assert.equal(texts(env).length, 4);
  assert.doesNotMatch(html(env, "thoughts-list"), /th-error/, "the error line goes with the next good answer");
});

test("a long thought is folded and opens with its button without throwing the view to the bottom; HTML in a thought is shown as text", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.thoughts.N4 = [F.makeThought(1, { text: "<img src=x onerror=alert(1)> 一" }), F.makeThought(2, { text: "长".repeat(1500) }), F.makeThought(3, { text: "尾" })];
  const list = env.el("thoughts-list");
  list.scrollHeight = 2000;
  await open(env, "N4");
  const painted = html(env, "thoughts-list");
  assert.ok(!painted.includes("<img src=x") && painted.includes("&lt;img src=x onerror=alert(1)&gt;"), "escaped");
  assert.ok(painted.includes("长".repeat(700) + "…") && !painted.includes("长".repeat(701)));
  list.scrollTop = 120;
  listClick(env, "fold", { "data-thought-id": "1:2000" });
  assert.ok(html(env, "thoughts-list").includes("长".repeat(1500)));
  assert.match(html(env, "thoughts-list"), />收起<\/button>/);
  assert.equal(list.scrollTop, 120, "the reader stays where they were");
  listClick(env, "fold", { "data-thought-id": "1:2000" });
  assert.ok(!html(env, "thoughts-list").includes("长".repeat(701)));
  // the open state survives a poll (the same text is not folded again under the reader's eyes)
  listClick(env, "fold", { "data-thought-id": "1:2000" });
  await runPoll(env);
  assert.ok(html(env, "thoughts-list").includes("长".repeat(1500)));
});

// =====================================================================================================
// the window next to the panel, Esc, the page around it
// =====================================================================================================
test("the window and the detail panel exist side by side: a click inside the window keeps the panel, Esc closes the window first and the panel second", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.thoughts.N4 = record(3);
  await open(env, "N4");
  const clickOn = (sel) => env.document.dispatchEvent("click", { target: target(sel ? { [sel]: {} } : {}) });
  clickOn(".vw-window");
  assert.equal(env.app().roster().openId(), "N4", "a click in the window is not a click 'outside' the panel");
  assert.equal(ui(env).isOpen(), true);
  env.document.dispatchEvent("keydown", { key: "Escape" });
  assert.equal(ui(env).isOpen(), false, "first the window");
  assert.equal(win(env).hidden, true);
  assert.equal(env.app().roster().openId(), "N4", "the panel is still there");
  env.document.dispatchEvent("keydown", { key: "Escape" });
  assert.equal(env.app().roster().openId(), null, "then the panel");
  // the ✕ button closes the window
  cardClick(env, "N4");
  panelClick(env, "open-thoughts");
  await flush(env);
  assert.equal(ui(env).isOpen(), true);
  env.el("btn-close-thoughts").dispatch("click");
  assert.equal(ui(env).isOpen(), false);
});

test("two windows open at once: the one opened last is on top and Escape closes only that one - then the other - then the panel behind them", async () => {
  const { env, backend } = await live({ status: "running" });
  backend.thoughts.N4 = record(3);
  const calllog = () => env.app().calllog();
  const z = (id) => Number(env.el(id).style.zIndex);
  const escape = () => env.document.dispatchEvent("keydown", { key: "Escape" });
  const behind = []; // a listener registered after the windows' own: it only hears a key press that nobody consumed
  env.document.addEventListener("keydown", (evt) => behind.push(evt.key));
  // the thinking record first, then the error log
  await open(env, "N4");
  env.el("btn-open-calllog").dispatch("click");
  await flush(env);
  assert.equal(ui(env).isOpen() && calllog().isOpen(), true);
  assert.ok(z("calllog-window") > z("thoughts-window"), "the newest window is on top on the screen too");
  assert.ok(z("calllog-window") < 9000, "(the dialogs stay above both)");
  escape();
  assert.equal(calllog().isOpen(), false, "one Escape: the log");
  assert.equal(ui(env).isOpen(), true, "the thinking record stays");
  assert.equal(env.app().roster().openId(), "N4");
  assert.deepEqual(behind, [], "the key press was consumed: nothing behind the window saw it");
  assert.equal(env.el("calllog-window").style.zIndex, "", "(a closed window gives its place back)");
  escape();
  assert.equal(ui(env).isOpen(), false, "the second: the thinking record");
  assert.equal(env.app().roster().openId(), "N4", "the panel is still there");
  escape();
  assert.equal(env.app().roster().openId(), null, "the third: the panel");
  assert.deepEqual(behind, ["Escape"], "(that one nothing consumed)");
  // the other order: the error log first, then the thinking record - which is then the one on top
  env.el("btn-open-calllog").dispatch("click");
  await flush(env);
  await open(env, "N4");
  assert.ok(z("thoughts-window") > z("calllog-window"));
  escape();
  assert.equal(ui(env).isOpen(), false, "the thinking record is the newest: it goes first");
  assert.equal(calllog().isOpen(), true);
  escape();
  assert.equal(calllog().isOpen(), false);
  // asking for the thinking record again brings it to the front: it is the newest again (the panel of N4 is still open)
  panelClick(env, "open-thoughts");
  await flush(env);
  env.el("btn-open-calllog").dispatch("click");
  await flush(env);
  assert.ok(z("calllog-window") > z("thoughts-window"), "the log was opened after it");
  panelClick(env, "open-thoughts"); // (the same agent: the window is already open - it just comes up front)
  await flush(env);
  assert.ok(z("thoughts-window") > z("calllog-window"));
  escape();
  assert.equal(ui(env).isOpen(), false);
  assert.equal(calllog().isOpen(), true);
  // a click on the button of the error log closes it (and gives its place back)
  env.el("btn-open-calllog").dispatch("click");
  assert.equal(calllog().isOpen(), false);
  escape();
  assert.equal(env.app().roster().openId(), null);
});

test("the markup and the style: a window of its own above the panel (not a browser pop-up), full screen on a phone, the scripts are loaded in order", () => {
  const css = require("node:fs").readFileSync(path.join(H.ROOT, "static", "css", "style.css"), "utf8");
  assert.match(H.HTML_SRC, /<div class="vw-window thoughts-window" id="thoughts-window" role="dialog" aria-label="思考记录" hidden>/);
  for (const id of ["thoughts-head", "thoughts-list", "thoughts-foot", "btn-close-thoughts", "btn-thoughts-latest", "btn-thoughts-refresh"]) assert.ok(H.HTML_SRC.includes(`id="${id}"`), id);
  assert.match(css, /\.vw-window \{[^}]*position: fixed;[^}]*z-index: 8100;/);
  assert.match(H.CSS_SRC, /\.detail-panel \{[^}]*z-index: 8000;/, "(the panel it was opened from)");
  assert.match(H.CSS_SRC, /\.modal-backdrop \{[^}]*z-index: 9000;/, "(dialogs stay above it)");
  assert.match(css, /html\[data-layout="phone"\] \.vw-window,\s*html\[data-layout="phone"\] \.calllog-window \{[^}]*top: 0;[^}]*right: 0;[^}]*bottom: 0;[^}]*left: 0;/);
  assert.match(css, /\.th-bubble \{/);
  assert.match(css, /\.th-divider::before,\s*\.th-divider::after \{/);
  // header, toolbar and foot keep their own height: squeezed by a long body, a subtitle that wrapped ran over the first bubble
  // (seen on a phone in a real browser)
  for (const part of ["vw-header", "vw-toolbar", "vw-foot"]) assert.match(css, new RegExp(`\\.${part} \\{[^}]*flex: none;`), `.${part} must not shrink`);
  assert.match(css, /\.vw-body \{[^}]*flex: 1 1 auto;[^}]*min-height: 0;[^}]*overflow-y: auto;/, "only the body scrolls");
  // (the cache-buster is the app version; that every one of them is the SAME version is tests/test_docs.py's job)
  const order = [...H.HTML_SRC.matchAll(/<script src="static\/js\/([a-z]+)\.js\?v=\d+\.\d+\.\d+"><\/script>/g)].map((m) => m[1]);
  assert.ok(order.indexOf("roster") < order.indexOf("thoughts") && order.indexOf("thoughts") < order.indexOf("app"), order.join());
  assert.match(H.HTML_SRC, /<link rel="stylesheet" href="static\/css\/style\.css\?v=\d+\.\d+\.\d+" \/>/);
  const play = css.indexOf("Arena-Agents v0.13.1 — playing a seat, part 2");
  const panels = css.indexOf("Arena-Agents v0.13.1 — panels:");
  assert.ok(play >= 0 && panels > play, "the panel rules retain their cascade order after the play rules");
  // the feed's placeholder no longer says the thoughts are only in the log (G26)
  assert.ok(!H.SRC.app.includes("思维链与工具调用只写入日志"));
  assert.match(H.SRC.app, /玩家的思考内容在详情面板的「🧠 思考记录」里查看，工具调用只写入日志/);
});

H.runTests(tests);
