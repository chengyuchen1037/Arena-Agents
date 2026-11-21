"use strict";
// v0.14 (web-play: W1-W4), the pure parts: the card before the deal (H52), the line-up and moving the cards (H29), the system status label (H26),
// the start check model (H25), the question at the end of a game (H31) - static/js/view.js and store.js, no DOM.  The wiring to the page:
// test_v014_play_ui.js.  (The viewpoint label and its menu: test_takeover_view.js.)
// Run: node tests/frontend/test_v014_play_view.js
const assert = require("node:assert/strict");
const path = require("node:path");
const Store = require(path.join(__dirname, "..", "..", "static", "js", "store.js"));
const View = require(path.join(__dirname, "..", "..", "static", "js", "view.js"));
const F = require("./fixtures.js");

const tests = [];
const test = (name, fn) => tests.push([name, fn]);
const GOD = View.GOD;

const plain = (over = {}) => Store.normalizeSession(F.makeSession(over));
/** A game that has not started: nobody is dealt (H52). */
const idle = (over = {}) => Store.normalizeSession(F.undealt(F.makeSession({ status: "idle", ...over })));

// ------------------------------------------------------------------------------------------- the card before the deal (H52)
test("before the deal (dealt === false) a card has NO identity: no main tag, no badges, not even the grey ? - whatever the viewpoint", () => {
  const s = idle();
  for (const vp of [GOD, "N1", "N3"]) {
    for (const t of View.boardTiles(s, vp)) {
      assert.equal(t.main, null, `${vp}/${t.id}: no identity tag`);
      assert.deepEqual(t.badges, [], `${vp}/${t.id}: no badge`);
      assert.ok(!/\?/.test(t.title), "the hover text says nothing about it either");
    }
  }
  assert.equal(View.boardSummary(s, GOD), null, "nothing to count: the summary strip is empty");
  assert.equal(View.isDealt(s), false);
  // the helpers themselves say it too (anything that draws a tag asks them): no main tag, no badges
  const carol = s.participants.find((p) => p.id === "N3");
  assert.equal(View.mainTagInfo(s, GOD, carol), null);
  assert.deepEqual(View.badgeTags(s, GOD, carol), []);
  const withTags = { ...s, dealt: true, participants: s.participants.map((p) => ({ ...p, tags: ["狼人", "存活", "警长"] })) };
  assert.equal(View.mainTagInfo(withTags, GOD, withTags.participants[2]).text, "狼人");
  assert.equal(View.badgeTags(withTags, GOD, withTags.participants[2]).length, 1);
});

test("a session that has tags although it says it was not dealt shows none of them (the page does not trust the participants for this)", () => {
  const s = Store.normalizeSession(F.makeSession({ status: "idle", dealt: false })); // (the fixture keeps the role tags)
  assert.ok(s.participants.every((p) => p.tags.length > 0));
  assert.ok(View.boardTiles(s, GOD).every((t) => t.main === null && t.badges.length === 0));
});

test("after the deal the cards are as before: the identity is the main tag, other viewpoints see the grey ?; a server that does not say has dealt", () => {
  const s = plain({ status: "running" });
  assert.equal(View.isDealt(s), true);
  const god = View.boardTiles(s, GOD);
  assert.deepEqual(god.find((t) => t.id === "N3").main, { text: "狼人", known: true, variant: "danger" });
  assert.deepEqual(View.boardTiles(s, "N1").find((t) => t.id === "N3").main, { text: "?", known: false, variant: "muted" });
  const old = Store.normalizeSession(F.makeSession({ status: "running" }));
  delete old.dealt;
  assert.equal(View.isDealt(old), true);
  assert.equal(Store.normalizeSession({ status: "idle" }).dealt, true, "an older server's game (it says nothing) has its identities");
  assert.equal(View.isDealt(null), true);
});

// --------------------------------------------------------------------------------------- moving the cards (H29)
test("seatDragState: the cards can be moved only while the game has not started, nobody is played and no replay is shown", () => {
  const ok = View.seatDragState(idle());
  assert.equal(ok.enabled, true);
  assert.match(ok.reason, /拖动卡片调整座位/);
  for (const status of ["running", "paused", "finished"]) {
    const s = View.seatDragState(plain({ status }));
    assert.deepEqual([s.enabled, s.reason], [false, "对局已经开始，不能再调整座位"], status);
  }
  const played = View.seatDragState(Store.normalizeSession(F.undealt(F.makePlayedSession("N3", { status: "idle" }))));
  assert.deepEqual([played.enabled, played.reason], [false, "接管期间不能调整座位：请先退出接管"]);
  const replay = View.seatDragState({ ...idle(), replay: { title: "x" } });
  assert.deepEqual([replay.enabled, replay.reason], [false, "重放中不能调整座位"]);
  const v5 = idle();
  v5.bots.lineup = [];
  assert.equal(View.seatDragState(v5).enabled, false, "a game of the old format has no line-up");
  assert.match(View.seatDragState(v5).reason, /没有 bot 阵容/);
});

test("moveInLineup: the bot at `from` takes the seat `to`, the bots in between move up (or down) one seat; nothing else changes", () => {
  const order = ["a", "b", "c", "d", "e"];
  assert.deepEqual(View.moveInLineup(order, 0, 2), ["b", "c", "a", "d", "e"]);
  assert.deepEqual(View.moveInLineup(order, 4, 1), ["a", "e", "b", "c", "d"]);
  assert.deepEqual(View.moveInLineup(order, 1, 0), ["b", "a", "c", "d", "e"]);
  assert.deepEqual(order, ["a", "b", "c", "d", "e"], "the original is not changed");
  for (const bad of [[2, 2], [-1, 1], [1, 5], [0.5, 1], [1, NaN], [undefined, 1]]) assert.equal(View.moveInLineup(order, ...bad), null, String(bad));
  assert.equal(View.moveInLineup(null, 0, 1), null);
});

test("seatIndexAt: the card under a point (the gap between two cards belongs to the nearer one within the slack); -1 over no card", () => {
  const rects = [{ left: 0, top: 0, right: 10, bottom: 10 }, { left: 14, top: 0, right: 24, bottom: 10 }, null, { left: 0, top: 14, right: 10, bottom: 24 }];
  assert.equal(View.seatIndexAt(rects, 5, 5), 0);
  assert.equal(View.seatIndexAt(rects, 20, 5), 1);
  assert.equal(View.seatIndexAt(rects, 12, 5), 0, "in the gap, nearer to the first");
  assert.equal(View.seatIndexAt(rects, 13, 5), 1, "in the gap, nearer to the second");
  assert.equal(View.seatIndexAt(rects, 5, 20), 3);
  assert.equal(View.seatIndexAt(rects, 100, 100), -1);
  assert.equal(View.seatIndexAt(rects, 12, 12, 1), -1, "in the corner between four cards, beyond a slack of 1");
  assert.equal(View.seatIndexAt([], 1, 1), -1);
});

test("seatKeyTarget: Alt+← / Alt+→ send the card one seat to the left / right, nothing at the ends or for other keys", () => {
  assert.equal(View.seatKeyTarget(3, "ArrowLeft", 12), 2);
  assert.equal(View.seatKeyTarget(3, "ArrowRight", 12), 4);
  assert.equal(View.seatKeyTarget(0, "ArrowLeft", 12), null);
  assert.equal(View.seatKeyTarget(11, "ArrowRight", 12), null);
  assert.equal(View.seatKeyTarget(3, "ArrowUp", 12), null);
  assert.equal(View.seatKeyTarget(3, "a", 12), null);
});

test("Store.applyLineup: the bots take their new seats (bot_id, name, avatar, init status of every seat, bots.lineup / bots.bots in seat order) and undo puts it all back", () => {
  const s = plain({ status: "idle", participants: F.makeParticipants({ N2: { init_status: "stale" } }) });
  s.bots.bots[1].init.status = "stale"; // (the bot of seat 2 needs a new initialization)
  const before = JSON.stringify([s.bots, s.participants]);
  const order = View.moveInLineup(s.bots.lineup, 1, 4); // Bob (seat 2) goes to seat 5
  const change = Store.applyLineup(s, order);
  assert.ok(change && typeof change.undo === "function");
  assert.deepEqual(s.bots.lineup, order);
  assert.deepEqual(s.bots.bots.map((b) => b.id), order, "bots.bots follows the line-up");
  assert.deepEqual(s.bots.bots.map((b) => b.seat), F.PLAYER_IDS, "each bot says the seat it sits in now");
  const seats = View.boardTiles(s, GOD).map((t) => [t.id, t.name, t.botId, t.initStatus]);
  assert.deepEqual(seats[1], ["N2", "Carol", F.botId(3), "ready"], "seat 2 is Carol's now");
  assert.deepEqual(seats[4], ["N5", "Bob", F.botId(2), "stale"], "seat 5 is Bob's, and his ⚠ moved with him");
  assert.deepEqual(seats.map((x) => x[0]), F.PLAYER_IDS, "the seats keep their ids");
  change.undo();
  assert.equal(JSON.stringify([s.bots, s.participants]), before, "undo: everything as it was");
});

test("Store.applyLineup refuses what is not a permutation of the bots at the table: nothing changes", () => {
  const s = plain({ status: "idle" });
  const before = JSON.stringify(s);
  const ids = s.bots.lineup.slice();
  assert.equal(Store.applyLineup(s, ids.slice(1)), null, "one missing");
  assert.equal(Store.applyLineup(s, [...ids.slice(0, 11), ids[0]]), null, "one twice");
  assert.equal(Store.applyLineup(s, [...ids.slice(0, 11), "b-ffffffff"]), null, "a stranger");
  assert.equal(Store.applyLineup(s, "nope"), null);
  assert.equal(Store.applyLineup({ participants: [] }, []), null, "no bots in the session (a game of the old format)");
  assert.equal(JSON.stringify(s), before);
});

// --------------------------------------------------------------------------------------- the system status label (H26)
const label = (s) => View.systemStatus(s);
test("systemStatus: the seven codes of the server and their texts (uninitialized · initializing · before_game · in_game · paused · finished · reviewing)", () => {
  const server = (code, text, reason = "") => ({ code, text, reason });
  const cases = [
    [plain({ status: "idle", system_status: server("uninitialized", "未初始化") }), "未初始化", "uninit"],
    [plain({ status: "idle", system_status: server("before_game", "对局前") }), "对局前", "idle"],
    [plain({ status: "running", system_status: server("in_game", "对局中") }), "对局中", "active"],
    [plain({ status: "paused", system_status: server("paused", "已暂停", "手动暂停") }), "已暂停 · 手动暂停", "paused"],
    [plain({ status: "finished", system_status: server("finished", "已结束") }), "已结束", "finished"],
  ];
  for (const [s, text, dot] of cases) {
    const got = label(s);
    assert.deepEqual([got.label, got.dot], [text, dot], text);
    assert.equal(got.code, s.system_status.code);
  }
  // initializing / reviewing: the server's label (H26: they win over everything else) - and, for an older server that sends none, what the page holds
  const init = plain({ status: "idle", system_status: server("initializing", "初始化中") });
  assert.deepEqual([label(init).code, label(init).label, label(init).dot], ["initializing", "初始化中", "busy"]);
  const review = plain({ status: "finished", system_status: server("reviewing", "复盘中") });
  assert.deepEqual([label(review).code, label(review).label, label(review).dot], ["reviewing", "复盘中", "busy"]);
  assert.match(label(init).title, /期间不能开局/);
  assert.match(label(review).title, /最多 60 秒/);
  const olderInit = plain({ status: "idle", system_status: undefined });
  olderInit.bots.init_job = { job_id: "j1", state: "running", ids: [F.botId(1)], started_at: 1, deadline_s: 120 };
  assert.equal(label(olderInit).code, "initializing", "no label from the server: a running initialization job says so");
  const olderReview = plain({ status: "finished", system_status: undefined });
  olderReview.review.running = true;
  assert.equal(label(olderReview).code, "reviewing");
  // the server's word is trusted while the status allows it: an initialization does not run in a game that is running, a review only in one that is over
  assert.equal(label(plain({ status: "running", system_status: server("initializing", "初始化中") })).code, "in_game");
  assert.equal(label(plain({ status: "idle", system_status: server("reviewing", "复盘中") })).code, "before_game");
});

test("systemStatus: a pause says why; the reason's own '，已暂停' is not said twice; the full reason is the hover text", () => {
  const paused = (reason, over = {}) => plain({ status: "paused", system_status: { code: "paused", text: "已暂停", reason }, ...over });
  assert.equal(label(paused("对局长时间没有进展，已暂停")).label, "已暂停 · 对局长时间没有进展");
  assert.equal(label(paused("已达到预算上限，已暂停")).label, "已暂停 · 已达到预算上限");
  assert.equal(label(paused("主持人暂停了对局：请确认警长归属")).label, "已暂停 · 主持人暂停了对局：请确认警长归属");
  assert.equal(label(paused("已暂停")).label, "已暂停", "no reason: just 已暂停");
  assert.equal(label(paused("")).label, "已暂停");
  assert.match(label(paused("对局长时间没有进展，已暂停")).title, /对局长时间没有进展，已暂停/, "the whole sentence is on hover");
});

test("systemStatus: a soft pause is 对局中 · agent 已暂停 (the server says so; while it still drains the page says how many answers are on their way)", () => {
  const draining = (n) => ({ state: "draining", in_flight: n });
  const running = (over) => plain({ status: "running", system_status: { code: "in_game", text: "对局中 · agent 已暂停", reason: "" }, ...over });
  assert.equal(label(running({ soft_pause: { state: "held", in_flight: 0 } })).label, "对局中 · agent 已暂停");
  assert.equal(label(running({ soft_pause: draining(2) })).label, "对局中 · 收尾中（还有 2 个回答）");
  assert.equal(label(running({ soft_pause: draining(0) })).label, "对局中 · 收尾中");
  assert.equal(label(running({ soft_pause: null })).label, "对局中 · agent 已暂停", "(the server's own text stands until the next session.updated)");
  assert.equal(label(plain({ status: "running", system_status: { code: "in_game", text: "对局中", reason: "" }, soft_pause: { state: "held", in_flight: 0 } })).label, "对局中 · agent 已暂停");
  assert.equal(label(plain({ status: "running", system_status: { code: "in_game", text: "对局中", reason: "" }, soft_pause: { state: "held", in_flight: 0 } })).dot, "paused", "the amber dot: nothing moves");
});

test("systemStatus: a label that is a moment late does not stand against the status the page holds; an older server (no label) is derived", () => {
  // the game finished (game.status) but the label of the last session.updated still says 对局中
  const late = plain({ status: "finished", system_status: { code: "in_game", text: "对局中", reason: "" } });
  assert.equal(label(late).label, "已结束");
  const none = plain({ status: "paused", pause_reason: "hold", hold_reason: "请确认", system_status: undefined });
  assert.equal(label(none).label, "已暂停 · 主持人暂停了对局：请确认");
  assert.equal(label({ status: "idle", bots: { bots: [{ init: { status: "stale" } }] } }).label, "未初始化");
  assert.equal(label({ status: "idle" }).label, "对局前");
  assert.equal(label(undefined).label, "对局前");
});

test("game.status drops the label of the old status (the page works one out until the next session.updated); session.updated brings the server's", () => {
  const s = plain({ status: "running", system_status: { code: "in_game", text: "对局中", reason: "" } });
  assert.deepEqual(s.system_status, { code: "in_game", text: "对局中", reason: "" });
  Store.applyEvent(s, { id: 9, type: "game.status", data: { status: "paused", reason: "user", winner: null } });
  assert.deepEqual(s.system_status, { code: "", text: "", reason: "" }, "the label that was true for the running game is gone");
  assert.equal(label(s).label, "已暂停", "the page says what it knows: paused (the reason comes with the server's label)");
  Store.applyEvent(s, { id: 10, type: "session.updated", data: { status: "paused", pause_reason: "user", system_status: { code: "paused", text: "已暂停", reason: "手动暂停" } } });
  assert.equal(label(s).label, "已暂停 · 手动暂停");
  // a finished game that was being initialized: the old label does not come back to life when the status becomes one it would fit again
  const init = plain({ status: "idle", system_status: { code: "initializing", text: "初始化中", reason: "" } });
  Store.applyEvent(init, { id: 11, type: "game.status", data: { status: "running", reason: "started", winner: null } });
  Store.applyEvent(init, { id: 12, type: "game.status", data: { status: "finished", reason: "end_game", winner: "好人阵营", summary: "" } });
  assert.equal(label(init).code, "finished");
});

test("systemStatus: a replay on screen says so (its own status text); the hover text carries what the server's status text adds", () => {
  const rep = plain({ status: "finished", active_status_text: "重放中 · 0:41 / 0:41", replay: { title: "旧局", session_id: "old", duration_gt: 41 } });
  assert.deepEqual([label(rep).code, label(rep).label], ["replay", "重放中 · 0:41 / 0:41"]);
  const winner = plain({ status: "finished", system_status: { code: "finished", text: "已结束", reason: "" }, active_status_text: "对局已结束 · 好人阵营 获胜（可点击「继续」恢复）" });
  assert.match(label(winner).title, /好人阵营 获胜/);
  assert.ok(!/对局进行中/.test(label(plain({ status: "running", active_status_text: "对局进行中" })).title.split("\n").slice(1).join("\n")), "the generic sentence is not repeated");
});

// --------------------------------------------------------------------------------------------- the start check (H25)
test("startCheckModel: the problems are grouped by what is not ready - the contract, the scenario, the players in seat order (their own problems and the missing model of the seat in ONE group), the moderator", () => {
  const s = plain({ status: "idle" });
  const problems = [
    F.makeProblem("bot", [{ code: "not_initialized", message: "还没有初始化。", fix: "init" }, { code: "core_missing", message: "底层设定是空的。", fix: "edit_core" }], { bot_id: F.botId(6), seat: "N6", name: "Frank" }),
    F.makeProblem("contract", [{ code: "contract_invalid", message: "官方契约有问题。", fix: "import_contract" }]),
    F.makeProblem("binding", [{ code: "no_binding", message: "N3 还没有绑定可用的模型。", fix: "bind_model" }], { bot_id: F.botId(3), seat: "N3", name: "Carol" }),
    F.makeProblem("bot", [{ code: "stale", message: "需要重新初始化。", fix: "init" }], { bot_id: F.botId(3), seat: "N3", name: "Carol" }),
    F.makeProblem("binding", [{ code: "no_binding", message: "主持人还没有绑定可用的模型。", fix: "bind_model" }], { name: "主持人" }),
    F.makeProblem("scenario", [{ code: "scenario_missing", message: "读不到剧本。", fix: "import_rules" }]),
  ];
  const m = View.startCheckModel(s, problems);
  assert.deepEqual(m.groups.map((g) => g.key), ["contract", "scenario", "player:N3", "player:N6", "moderator"]);
  assert.deepEqual(m.groups.map((g) => g.title), ["官方契约", "剧本", "3 号 Carol", "6 号 Frank", "主持人"]);
  const carol = m.groups[2];
  assert.equal(carol.botId, F.botId(3));
  assert.deepEqual(m.groups.map((g) => g.focusId), ["", "", F.botId(3), F.botId(6), "moderator"], "what the buttons hand to Bots.focus: the bot, 'moderator' for its model, nothing for the contract");
  assert.deepEqual(carol.items.map((i) => [i.code, i.fix, i.fixLabel]), [["no_binding", "bind_model", "去绑定模型"], ["stale", "init", "去初始化"]]);
  assert.equal(m.total, 7);
  assert.equal(m.initCount, 2, "two items only need the initialization");
  assert.deepEqual(m.groups[0].items[0], { code: "contract_invalid", message: "官方契约有问题。", fix: "import_contract", fixLabel: "去导入契约" });
  assert.equal(m.groups[4].botId, null, "the moderator is not a bot");
});

test("startCheckModel: every fix the contract names has its button text; an unknown fix has none (the sentence is still shown); junk is skipped", () => {
  assert.deepEqual(
    Object.entries(View.FIX_LABELS),
    [["edit_core", "去编辑底层设定"], ["import_core", "去导入底层设定"], ["init", "去初始化"], ["bind_model", "去绑定模型"], ["import_contract", "去导入契约"], ["import_rules", "去导入规则"]]
  );
  const m = View.startCheckModel(plain({ status: "idle" }), [
    F.makeProblem("bot", [{ code: "x", message: "别的问题。", fix: "something_new" }], { bot_id: F.botId(1), seat: "N1", name: "Alice" }),
    null,
    "junk",
    { scope: "bot", items: [] },
    F.makeProblem("game", [{ code: "g", message: "对局有问题。", fix: "" }]),
  ]);
  assert.deepEqual(m.groups.map((g) => g.title), ["1 号 Alice", "对局"]);
  assert.deepEqual(m.groups[0].items[0], { code: "x", message: "别的问题。", fix: "something_new", fixLabel: "" });
  assert.deepEqual(View.startCheckModel(plain(), undefined), { groups: [], total: 0, initCount: 0 });
});

// ---------------------------------------------------------------------------- the question at the end of a game (H31)
const over = (extra = {}, review = {}) => plain({ status: "finished", review: { available: true, done: [], running: false, supported: true, deadline_s: 60, seconds_left: null, ...review }, ...extra });
test("reviewPrompt: the question comes when the game is over, can be reviewed, nothing runs, nobody was reviewed and the user did not say 暂不", () => {
  assert.deepEqual(View.reviewPrompt(over(), {}), { show: true, why: "" });
  const no = (s, dismissed, why) => assert.deepEqual(View.reviewPrompt(s, dismissed), { show: false, why });
  no(plain({ status: "running" }), {}, "not finished");
  no(plain({ status: "paused" }), {}, "not finished");
  no(plain({ status: "idle" }), {}, "not finished");
  no(over({}, { supported: false }), {}, "unsupported");
  no(over({}, { available: false }), {}, "unavailable");
  no(over({}, { running: true }), {}, "running");
  no(over({}, { done: ["N3"] }), {}, "reviewed");
  no(over(), { "s-test": true }, "dismissed");
  assert.equal(View.reviewPrompt(over(), { "another-game": true }).show, true, "暂不 is remembered per game");
  no({ ...over(), replay: { title: "x" } }, {}, "not finished");
  assert.equal(View.reviewPrompt(over(), undefined).show, true);
  assert.equal(View.reviewPrompt(null, {}).show, false);
});

test("reviewRows: the twelve bots in seat order, all selected; a seat a person played says so and has the switch (on by default); reviewed ones are off", () => {
  const s = over({}, { done: ["N5"] });
  const rows = View.reviewRows(s, ["N3"], {});
  assert.equal(rows.length, 12);
  assert.deepEqual(rows.map((r) => r.id), F.PLAYER_IDS);
  assert.deepEqual(rows.filter((r) => r.checked).length, 11, "everybody but the one reviewed already");
  const carol = rows[2];
  assert.deepEqual([carol.seat, carol.name, carol.human, carol.checked, carol.learnStyle, carol.done], [3, "Carol", true, true, true, false]);
  assert.deepEqual([rows[0].human, rows[0].learnStyle], [false, false], "only a seat a person played has the switch");
  assert.deepEqual([rows[4].done, rows[4].checked], [true, false]);
  // what the user ticked
  const chosen = View.reviewRows(s, ["N3"], { N1: { checked: false }, N3: { learnStyle: false }, N5: { checked: true } });
  assert.equal(chosen[0].checked, false);
  assert.deepEqual([chosen[2].checked, chosen[2].learnStyle], [true, false]);
  assert.equal(chosen[4].checked, false, "a bot that was reviewed cannot be selected again");
  assert.deepEqual(View.reviewRows(s, undefined, undefined).map((r) => r.human), Array(12).fill(false));
});

// -------------------------------------------------------------------------------------------------
require("./harness.js").runTests(tests);
