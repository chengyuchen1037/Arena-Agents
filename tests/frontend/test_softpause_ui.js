"use strict";
// The soft pause (v0.13.1, G1-G9), the "held" card state and the public floor without a deadline (G10) in the page:
// the store (session.soft_pause / clock_running, agent.status held, FloorView.untimed), the pure view model (view.js:
// softPauseOf, clockRunning, takeoverBar().softPause, cardState, composerHint ...) and the page itself (takeover.js:
// the bar's state machine, app.js: the local clock estimate and the cards) against the fake DOM and the fake backend
// (harness.js).  Contract: docs/v0.12-contracts.md §12.2.
// Run: node tests/frontend/test_softpause_ui.js
const assert = require("node:assert/strict");
const vm = require("node:vm");
const H = require("./harness.js");

const { F, Core, Store, View, target, settle, bootLive } = H;

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// ---- helpers -------------------------------------------------------------------------------------------------
const draining = (n) => ({ state: "draining", in_flight: n });
const held = () => ({ state: "held", in_flight: 0 });
const flush = async (env, n = 25) => {
  await settle(n);
  env.flushRaf();
};
const html = (env, id) => env.el(id).innerHTML;
const attr = (env, id, name) => env.el(id).getAttribute(name);
const lastToast = (env) => (env.toasts.length ? env.toasts[env.toasts.length - 1].textContent : "");
const posts = (backend, suffix) => backend.calls.filter((c) => c.method === "POST" && c.url.endsWith(suffix));
const channelClick = (env, id) => env.el("channels-list").dispatch("click", { target: target({ ".channel-item": { "data-channel": id } }) });

/** A game in which `seat` is played (running unless `over` says otherwise). */
const playedOver = (seat, over = {}, takeoverOver = {}) => ({
  status: "running",
  participants: F.makeParticipants({ [seat]: { state: "human" } }),
  takeover: F.makeTakeover(seat, takeoverOver),
  ...over,
});

/** Boot, connect and deliver a snapshot of `over` (event id 500). */
async function live(over = {}, overrides = {}, envOpts = {}) {
  const session = F.makeSession({ event_id: 500, ...over });
  const ctx = await bootLive({ session, ...overrides }, envOpts);
  ctx.env.sse("snapshot", { session }, 500);
  return ctx;
}
const livePlayed = (seat = "N3", over = {}, takeoverOver = {}, overrides = {}) => live(playedOver(seat, over, takeoverOver), overrides);

/** The sandbox's clock: `Date.now()` of app.js is ours (so the local game-clock estimate can be stepped). */
function fakeClock(env, start = 1_000_000) {
  vm.runInContext(`globalThis.__t = ${start}; Date.now = () => globalThis.__t;`, env.sandbox);
  return {
    now: () => vm.runInContext("globalThis.__t", env.sandbox),
    advance: (ms) => vm.runInContext(`globalThis.__t += ${ms}`, env.sandbox),
  };
}
/** The page's estimate of the game clock right now (what `clockNow()` of app.js answers). */
const estimate = (env, clock) => Core.gameClockNow(env.app().state.clockSync, clock.now());

const bar = (env) => ({
  hidden: env.el("takeover-bar").hidden,
  text: env.el("softpause-status").textContent,
  textHidden: env.el("softpause-status").hidden,
  phase: attr(env, "softpause-status", "data-phase"),
  button: env.el("btn-softpause").textContent,
  action: attr(env, "btn-softpause", "data-action"),
  enabled: !env.el("btn-softpause").disabled,
  nowHidden: env.el("btn-softpause-now").hidden,
  soft: env.el("takeover-bar").classList.contains("is-soft-paused"),
});

// =====================================================================================================
// the store
// =====================================================================================================
test("soft_pause is null or {state, in_flight}: junk is no soft pause, `held` has nothing in flight", () => {
  assert.equal(Store.normalizeSoftPause(null), null);
  assert.equal(Store.normalizeSoftPause(undefined), null);
  assert.equal(Store.normalizeSoftPause("held"), null);
  assert.equal(Store.normalizeSoftPause({ state: "paused", in_flight: 1 }), null);
  assert.deepEqual(Store.normalizeSoftPause({ state: "draining", in_flight: "3" }), draining(3));
  assert.deepEqual(Store.normalizeSoftPause({ state: "draining", in_flight: -2 }), draining(0));
  assert.deepEqual(Store.normalizeSoftPause({ state: "draining" }), draining(0));
  assert.deepEqual(Store.normalizeSoftPause({ state: "held", in_flight: 4 }), held());
});

test("a session: soft_pause only exists while the game runs; clock_running is the server's word, else the status's", () => {
  const norm = (over) => Store.normalizeSession(F.makeSession(over));
  // the server says so
  let s = norm({ status: "running", soft_pause: held(), clock_running: false });
  assert.deepEqual([s.soft_pause, s.clock_running], [held(), false]);
  s = norm({ status: "running", soft_pause: null, clock_running: true });
  assert.deepEqual([s.soft_pause, s.clock_running], [null, true]);
  // an older server sends neither: the status decides
  s = norm({ status: "running" });
  assert.deepEqual([s.soft_pause, s.clock_running], [null, true]);
  for (const status of ["idle", "paused", "finished"]) {
    s = norm({ status });
    assert.deepEqual([s.soft_pause, s.clock_running], [null, false], status);
  }
  // a soft pause of a game that does not run (a stale field) is none (G6)
  s = norm({ status: "paused", soft_pause: draining(2) });
  assert.equal(s.soft_pause, null);
  // no flag, but a soft pause: the clock stands still
  s = norm({ status: "running", soft_pause: held() });
  assert.equal(s.clock_running, false);
  assert.equal(Store.emptySession().soft_pause, null);
});

test("session.updated carries soft_pause and clock_running; every hard stop clears the soft pause", () => {
  const s = Store.normalizeSession(F.makeSession({ status: "running" }));
  let id = 100;
  const apply = (type, data) => Store.applyEvent(s, { id: ++id, type, data });

  let r = apply("session.updated", { status: "running", soft_pause: draining(2), clock_running: false, clock: 55.5 });
  assert.deepEqual([s.soft_pause, s.clock_running, s.clock], [draining(2), false, 55.5]);
  assert.deepEqual([r.dirty.topbar, r.dirty.board], [true, true]);
  apply("session.updated", { status: "running", soft_pause: draining(1), clock_running: false, clock: 55.5 });
  assert.deepEqual(s.soft_pause, draining(1));
  apply("session.updated", { status: "running", soft_pause: held(), clock_running: false, clock: 55.5 });
  assert.deepEqual(s.soft_pause, held());
  apply("session.updated", { status: "running", soft_pause: null, clock_running: true, clock: 55.5 });
  assert.deepEqual([s.soft_pause, s.clock_running], [null, true]);

  // a hard pause (or the end, the budget ...) arrives as game.status: the soft pause is gone, the clock stands still
  apply("session.updated", { status: "running", soft_pause: held(), clock_running: false, clock: 60 });
  apply("game.status", { status: "paused", reason: "user", winner: null });
  assert.deepEqual([s.status, s.soft_pause, s.clock_running], ["paused", null, false]);
  apply("game.status", { status: "running", reason: "resumed", winner: null });
  assert.deepEqual([s.status, s.soft_pause, s.clock_running], ["running", null, true]);
  apply("session.updated", { status: "running", soft_pause: draining(3), clock_running: false, clock: 70 });
  apply("game.status", { status: "finished", reason: "end_game", winner: "好人阵营", summary: "" });
  assert.deepEqual([s.status, s.soft_pause, s.clock_running], ["finished", null, false]);

  // a server that sends no clock_running (an older one) is judged by the status, also after a merge
  apply("game.status", { status: "running", reason: "continued", winner: null });
  apply("session.updated", { status: "paused", pause_reason: "user", clock: 80 });
  assert.deepEqual([s.status, s.soft_pause, s.clock_running], ["paused", null, false]);
  apply("session.updated", { status: "running", clock: 80 });
  assert.equal(s.clock_running, true);
});

test("agent.status: `held` is an agent state of its own (no wake-up time); the played seat stays human", () => {
  const s = Store.normalizeSession(F.makeSession(playedOver("N3")));
  let id = 200;
  const apply = (data) => Store.applyEvent(s, { id: ++id, type: "agent.status", data });
  apply({ id: "N4", state: "held", until: null });
  assert.deepEqual([View.participantOf(s, "N4").state, View.participantOf(s, "N4").sleep_until], ["held", null]);
  apply({ id: "moderator", state: "held", until: null });
  assert.equal(s.moderator.state, "held");
  apply({ id: "N4", state: "held", until: 99 }); // (only a sleeping agent has a wake-up time)
  assert.equal(View.participantOf(s, "N4").sleep_until, null);
  apply({ id: "N4", state: "thinking", until: null }); // released: back to work
  assert.equal(View.participantOf(s, "N4").state, "thinking");
  apply({ id: "N3", state: "held", until: null });
  assert.equal(View.participantOf(s, "N3").state, "human");
});

test("FloorView.untimed and the seat's floor.untimed are read as booleans (default false)", () => {
  assert.equal(Store.normalizeFloor({ kind: "turn", queue: [] }).untimed, false);
  assert.equal(Store.normalizeFloor({ kind: "turn", untimed: true, seconds_left: null, queue: [] }).untimed, true);
  assert.equal(Store.normalizeFloor({ kind: "turn", untimed: "yes" }).untimed, true);
  const t = Store.normalizeTakeover(F.makeTakeover("N3", { channels: { public: { can_write: true, max_chars: 300, floor: F.seatFloor({ untimed: true, seconds_left: null, deadline_clock: null }) } } }));
  assert.deepEqual([t.channels.public.floor.untimed, t.channels.public.floor.seconds_left, t.channels.public.floor.deadline_clock], [true, null, null]);
  assert.equal(Store.normalizeTakeover(F.makeTakeover("N3", { channels: { public: { can_write: true, max_chars: 300, floor: F.seatFloor() } } })).channels.public.floor.untimed, false);
});

// =====================================================================================================
// the view model
// =====================================================================================================
test("softPauseOf / clockRunning: the phases, only while running; the flag wins, else the status", () => {
  const of = (over) => View.softPauseOf(Store.normalizeSession(F.makeSession(over)));
  assert.deepEqual(of({ status: "running" }), { active: false, phase: null, inFlight: 0 });
  assert.deepEqual(of({ status: "running", soft_pause: draining(3) }), { active: true, phase: "draining", inFlight: 3 });
  assert.deepEqual(of({ status: "running", soft_pause: held() }), { active: true, phase: "held", inFlight: 0 });
  assert.deepEqual(of({ status: "paused", soft_pause: held() }), { active: false, phase: null, inFlight: 0 });
  assert.deepEqual(View.softPauseOf(null), { active: false, phase: null, inFlight: 0 });

  assert.equal(View.clockRunning({ status: "running", clock_running: false }), false, "the flag wins (a soft pause)");
  assert.equal(View.clockRunning({ status: "paused", clock_running: true }), true, "...whatever it says");
  assert.equal(View.clockRunning({ status: "running" }), true, "no flag: the status");
  assert.equal(View.clockRunning({ status: "running", soft_pause: held() }), false, "no flag, a soft pause");
  assert.equal(View.clockRunning({ status: "paused" }), false);
  assert.equal(View.clockRunning({ status: "finished" }), false);
  assert.equal(View.clockRunning(null), false);
});

test("takeoverBar().softPause: pause only while the game runs; draining and held say it and offer 继续 (and 立即暂停 while draining)", () => {
  const ctl = (over) => View.takeoverBar(Store.normalizeSession(F.makeSession(playedOver("N3", over)))).softPause;

  const running = ctl({ status: "running" });
  assert.deepEqual([running.action, running.label, running.enabled, running.phase, running.text, running.showNow], ["pause", "⏸ 暂停 agent", true, null, "", false]);
  assert.match(running.title, /已发出的回答会照常完成.*计时冻结.*继续阅读、发言和作答/);

  for (const [status, why] of [["idle", /对局还没有开始/], ["paused", /对局已暂停/], ["finished", /对局已结束/]]) {
    const c = ctl({ status });
    assert.deepEqual([c.action, c.enabled, c.text, c.showNow], ["pause", false, "", false], status);
    assert.match(c.title, why, status);
    assert.match(c.title, /现在不能暂停 agent/, status);
  }

  const drain = ctl({ status: "running", soft_pause: draining(2) });
  assert.deepEqual([drain.action, drain.label, drain.enabled, drain.phase, drain.inFlight, drain.showNow], ["resume", "▶ 继续", true, "draining", 2, true]);
  assert.equal(drain.text, "⏳ 收尾中（还有 2 个回答）");
  assert.match(drain.statusTitle, /还有 2 个模型回答没有收完.*之后所有 agent 暂停.*计时已经冻结/);
  assert.match(drain.title, /取消这次暂停/);
  assert.match(drain.nowTitle, /取消仍在途的模型调用.*等同顶栏的「暂停」/);
  assert.equal(ctl({ status: "running", soft_pause: draining(0) }).text, "⏳ 收尾中", "nothing in flight: no number");

  const stopped = ctl({ status: "running", soft_pause: held() });
  assert.deepEqual([stopped.action, stopped.label, stopped.enabled, stopped.phase, stopped.showNow], ["resume", "▶ 继续", true, "held", false]);
  assert.equal(stopped.text, "⏸ agent 已暂停");
  assert.match(stopped.statusTitle, /计时已冻结.*继续阅读、发言和作答/);
  assert.match(stopped.title, /先合并你这段时间的发言和作答/);

  // a hard pause that clears the soft pause: the bar says pause (greyed) again
  assert.equal(ctl({ status: "paused", soft_pause: held() }).action, "pause");
  assert.equal(View.takeoverBar(Store.normalizeSession(F.makeSession())), null, "nobody is played: no bar");
  // the release rule is unchanged: not while the game runs, also not in a soft pause
  assert.equal(View.takeoverBar(Store.normalizeSession(F.makeSession(playedOver("N3", { soft_pause: held() })))).canRelease, false);
});

test("the status label (v0.14, H26) and its dot say what a soft pause is doing; the top bar's own controls are not changed", () => {
  const s = (over) => Store.normalizeSession(F.makeSession({ active_status_text: "对局进行中", ...over }));
  const label = (session) => View.systemStatus(session).label;
  assert.equal(label(s({ status: "running" })), "对局中");
  assert.equal(label(s({ status: "running", soft_pause: draining(2) })), "对局中 · 收尾中（还有 2 个回答）");
  assert.equal(label(s({ status: "running", soft_pause: held() })), "对局中 · agent 已暂停", "the soft pause is 对局中 · agent 已暂停 (the server says the same)");
  assert.equal(label(s({ status: "paused", active_status_text: "已暂停", soft_pause: held() })), "已暂停", "a hard pause ends the soft pause");
  assert.equal(label({ status: "running", soft_pause: held() }), "对局中 · agent 已暂停", "also without the server's label");
  assert.deepEqual([View.statusDotClass(s({ status: "running" })), View.statusDotClass(s({ status: "running", soft_pause: draining(1) })), View.statusDotClass(s({ status: "running", soft_pause: held() }))], ["active", "active", "paused"]);
  // 暂停 is the hard pause (it works in a soft pause), 继续 belongs to the takeover bar (the game still "runs")
  assert.deepEqual(View.controlState(s({ status: "running", soft_pause: held() })), { start: false, pause: true, resume: false });
});

test("cardState: the real state where the viewpoint may see it; a held soft pause puts ⏸ on every agent where it may not", () => {
  const session = (over) => {
    const s = Store.normalizeSession(F.makeSession(playedOver("N3", over)));
    View.participantOf(s, "N4").state = "thinking";
    View.participantOf(s, "N5").state = "held";
    s.moderator.state = "held";
    return s;
  };
  const tiles = (s, vp) => Object.fromEntries(View.boardTiles(s, vp, { clockNow: 0 }).map((t) => [t.id, t]));

  // god: the truth - activated agents are held, the others still sleep
  const god = tiles(session({ soft_pause: held() }), View.GOD);
  assert.deepEqual([god.N4.state, god.N5.state, god.N1.state, god.N3.state], ["thinking", "held", "sleeping", "human"]);
  assert.deepEqual([god.N5.stateIcon, god.N5.stateTitle], ["⏸", "暂停中：软暂停，等你点「继续」"]);
  assert.match(god.N5.title, /⏸ 暂停中：软暂停/);
  assert.equal(View.moderatorTile(session({ soft_pause: held() }), View.GOD).state, "held");

  // the played seat's locked viewpoint hides the others' states (who is awake is a secret) - except that when
  // the soft pause has stopped everybody, every card says so: that is the state of the game, it gives nothing away
  const mine = tiles(session({ soft_pause: held() }), "N3");
  assert.equal(mine.N3.state, "human", "its own card keeps 🎮");
  for (const id of ["N1", "N4", "N5", "N12"]) assert.deepEqual([mine[id].state, mine[id].stateIcon], ["held", "⏸"], id);
  assert.equal(View.moderatorTile(session({ soft_pause: held() }), "N3").state, "held");
  // draining: some are still working, nothing is shown; no soft pause: nothing (as before)
  for (const over of [{ soft_pause: draining(2) }, {}]) {
    const quiet = tiles(session(over), "N3");
    for (const id of ["N1", "N4", "N5"]) assert.equal(quiet[id].state, "", JSON.stringify(over) + id);
    assert.equal(View.moderatorTile(session(over), "N3").state, "");
  }
  assert.equal(View.stateTitle("held"), "暂停中：软暂停，等你点「继续」");
  assert.equal(View.STATE_ICONS.held, "⏸");
  assert.equal(View.STATE_LABELS.held, "暂停中");
});

test("the floor of a person's turn: 不限时 instead of a countdown (hint, chip, floor summary); timed floors are unchanged", () => {
  const seat = (floor, over = {}) => ({ can_write: true, max_chars: 300, can_pass: true, floor, ...over });
  const session = (soft) => Store.normalizeSession(F.makeSession(playedOver("N3", { soft_pause: soft || null })));
  const timed = F.seatFloor({ seconds_left: 87.5, deadline_clock: 100 });
  const untimed = F.seatFloor({ untimed: true, seconds_left: null, deadline_clock: null });

  assert.equal(View.floorSecondsLeft(untimed, 50), null);
  assert.equal(View.floorSecondsLeft({ ...untimed, deadline_clock: 160 }, 100), null, "untimed wins, whatever else the floor says");
  assert.equal(View.floorSecondsLeft(timed, 40), 60);

  assert.equal(View.composerHint(session(), seat(untimed), 12), "轮到你发言 · 还可发 2 条 · 单条 ≤ 300 字 · 不限时");
  assert.equal(View.composerHint(session(), seat(timed), 40), "轮到你发言 · 还可发 2 条 · 单条 ≤ 300 字 · 剩余 1:00");
  // a soft pause stands the countdown still: say so
  assert.equal(View.composerHint(session(held()), seat(timed), 40), "轮到你发言 · 还可发 2 条 · 单条 ≤ 300 字 · 剩余 1:00（计时已冻结）");
  assert.equal(View.composerHint(session(held()), seat(untimed), 40), "轮到你发言 · 还可发 2 条 · 单条 ≤ 300 字 · 不限时");
  assert.equal(View.composerHint(session(draining(1)), seat(timed), 40), "轮到你发言 · 还可发 2 条 · 单条 ≤ 300 字 · 剩余 1:00（计时已冻结）");

  // the seat's own channel chip
  const mine = (floor) => View.personalFloorSummary({ kind: "queue", icon: "📋", text: "队列 1/12", detail: "" }, seat(floor));
  assert.deepEqual([mine(untimed).text, mine(untimed).detail], ["轮到你发言", "还可发 2 条 · 不限时"]);
  assert.equal(mine(timed).detail, "还可发 2 条");
  assert.equal(View.personalFloorSummary({ kind: "queue", icon: "📋", text: "x", detail: "" }, seat(F.seatFloor({ untimed: true, messages_left: null, seconds_left: null, deadline_clock: null }))).detail, "不限时");

  // everybody else's chip of the channel (the god view, a replay)
  const channel = (floor, mode) => F.channel("public", "公共频道", F.EVERYONE, { mode, floor });
  const queueFloor = (untimedFlag) => ({
    kind: "queue", active: "N1", paused: false, seconds_left: untimedFlag ? null : 80, untimed: untimedFlag,
    queue: [{ holder: "N1", state: "active", messages_used: 0, max_messages: 2 }, { holder: "N2", state: "pending", messages_used: 0, max_messages: 2 }], free: null,
  });
  const norm = (c) => Store.normalizeChannel(c);
  const plain = View.floorSummary(Store.emptySession(), norm(channel(queueFloor(false), "queue")));
  const free = View.floorSummary(Store.emptySession(), norm(channel(queueFloor(true), "queue")));
  assert.equal(plain.detail, "持麦：N1");
  assert.equal(free.detail, "持麦：N1 · 不限时");
  assert.equal(free.untimed, true);
  const turnFloor = { ...queueFloor(true), kind: "turn" };
  assert.match(View.floorSummary(Store.emptySession(), norm(channel(turnFloor, "turn"))).detail, /持麦：N1 · 不限时$/);
  assert.doesNotMatch(View.floorSummary(Store.emptySession(), norm(channel({ ...turnFloor, untimed: false, seconds_left: 30 }, "turn"))).detail, /不限时/);
});

// =====================================================================================================
// the takeover bar: the state machine
// =====================================================================================================
test("the soft-pause button is there while a seat is played: enabled while the game runs, grey (with the reason) otherwise", async () => {
  for (const [status, enabled] of [["running", true], ["paused", false], ["idle", false], ["finished", false]]) {
    const { env } = await livePlayed("N3", { status });
    const b = bar(env);
    assert.deepEqual([b.hidden, b.button, b.action, b.enabled, b.textHidden, b.nowHidden, b.soft], [false, "⏸ 暂停 agent", "pause", enabled, true, true, false], status);
    if (enabled) assert.match(attr(env, "btn-softpause", "title"), /已发出的回答会照常完成/);
    else assert.match(attr(env, "btn-softpause", "title"), /现在不能暂停 agent/);
  }
  // nobody plays: no bar at all
  const { env } = await live({ status: "running" });
  assert.equal(env.el("takeover-bar").hidden, true);
});

test("⏸ 暂停 agent → ⏳ 收尾中（还有 N 个回答）→ ⏸ agent 已暂停 → ▶ 继续: one request each, the state follows the stream", async () => {
  const { env, backend } = await livePlayed("N3", { status: "running", clock: 100 });
  const base = env.app().state.session;
  backend.controlSession = { ...JSON.parse(JSON.stringify(base)), event_id: 520, clock: 100, soft_pause: draining(2), clock_running: false };
  env.el("btn-softpause").click();
  await flush(env);
  assert.equal(posts(backend, "/api/control/soft-pause").length, 1);
  assert.equal(posts(backend, "/api/control/soft-resume").length, 0);
  assert.match(lastToast(env), /已发起软暂停/);
  let b = bar(env);
  assert.deepEqual([b.text, b.textHidden, b.phase, b.button, b.action, b.enabled, b.nowHidden, b.soft], ["⏳ 收尾中（还有 2 个回答）", false, "draining", "▶ 继续", "resume", true, false, true]);
  assert.match(attr(env, "softpause-status", "title"), /还有 2 个模型回答没有收完/);
  assert.match(attr(env, "btn-softpause-now", "title"), /取消仍在途的模型调用/);
  assert.equal(env.el("status-banner").hidden, true, "no banner: the bar and the status line say it");
  assert.match(env.el("active-status-text").textContent, /收尾中（还有 2 个回答）/);

  // the stream counts the answers down ...
  env.sse("session.updated", { status: "running", soft_pause: draining(1), clock_running: false, clock: 100 }, 521);
  assert.equal(bar(env).text, "⏳ 收尾中（还有 1 个回答）");
  // ... and everybody holds
  env.sse("session.updated", { status: "running", soft_pause: held(), clock_running: false, clock: 100 }, 522);
  b = bar(env);
  assert.deepEqual([b.text, b.phase, b.button, b.action, b.nowHidden, b.soft], ["⏸ agent 已暂停", "held", "▶ 继续", "resume", true, true]);
  assert.match(attr(env, "softpause-status", "title"), /所有 agent 已暂停，游戏计时已冻结/);
  assert.equal(env.el("active-status-text").textContent, "对局中 · agent 已暂停");
  assert.equal(env.el("active-status-dot").className, "status-dot paused");
  // (the top bar: 暂停 = the hard pause stays possible, 继续 is the takeover bar's)
  assert.deepEqual([env.el("btn-pause").disabled, env.el("btn-resume").disabled], [false, true]);
  assert.match(attr(env, "btn-pause", "title"), /硬暂停：立即停下所有 agent/);
  assert.match(attr(env, "btn-resume", "title"), /请用接管栏里的「▶ 继续」/);

  // ▶ 继续
  backend.controlSession = { ...JSON.parse(JSON.stringify(base)), event_id: 530, clock: 100, soft_pause: null, clock_running: true };
  env.el("btn-softpause").click();
  await flush(env);
  assert.equal(posts(backend, "/api/control/soft-resume").length, 1);
  assert.match(lastToast(env), /agent 已继续/);
  b = bar(env);
  assert.deepEqual([b.text, b.textHidden, b.phase, b.button, b.action, b.enabled, b.nowHidden, b.soft], ["", true, "", "⏸ 暂停 agent", "pause", true, true, false]);
  assert.equal(env.el("active-status-dot").className, "status-dot active");
  assert.match(attr(env, "btn-resume", "title"), /继续对局；对局结束后也可以继续/);
  assert.equal(posts(backend, "/api/control/soft-pause").length, 1, "one request for each click");
});

test("a soft pause that is still draining can be continued (it is cancelled); the cancel also comes with the stream", async () => {
  const { env, backend } = await livePlayed("N3", { status: "running", soft_pause: draining(3), clock_running: false });
  assert.deepEqual([bar(env).button, bar(env).action, bar(env).enabled], ["▶ 继续", "resume", true]);
  backend.controlSession = { ...JSON.parse(JSON.stringify(env.app().state.session)), event_id: 540, soft_pause: null, clock_running: true };
  env.el("btn-softpause").click();
  await flush(env);
  assert.equal(posts(backend, "/api/control/soft-resume").length, 1);
  assert.equal(bar(env).action, "pause");
  // the same from another tab: the stream alone does it
  env.sse("session.updated", { status: "running", soft_pause: draining(1), clock_running: false, clock: 12.5 }, 541);
  assert.equal(bar(env).soft, true);
  env.sse("session.updated", { status: "running", soft_pause: null, clock_running: true, clock: 12.5 }, 542);
  assert.deepEqual([bar(env).soft, bar(env).textHidden, bar(env).action], [false, true, "pause"]);
});

test("立即暂停 (while draining) is the top bar's hard pause: POST api/control/pause, the soft pause is gone with it", async () => {
  const { env, backend } = await livePlayed("N3", { status: "running", soft_pause: draining(2), clock_running: false });
  assert.equal(bar(env).nowHidden, false);
  backend.controlSession = { ...JSON.parse(JSON.stringify(env.app().state.session)), event_id: 560, status: "paused", pause_reason: "user", soft_pause: null, clock_running: false };
  env.el("btn-softpause-now").click();
  await flush(env);
  assert.equal(posts(backend, "/api/control/pause").length, 1);
  assert.equal(posts(backend, "/api/control/soft-pause").length + posts(backend, "/api/control/soft-resume").length, 0);
  assert.match(lastToast(env), /已立即暂停：在途的模型调用已取消/);
  const b = bar(env);
  assert.deepEqual([b.text, b.textHidden, b.nowHidden, b.action, b.enabled, b.soft], ["", true, true, "pause", false, false], "paused: nothing left to soft-pause");
  assert.equal(env.el("btn-release-takeover").disabled, false, "and the seat can be given back now");
  assert.equal(env.el("btn-resume").disabled, false);
});

test("the top bar's 暂停 in a soft pause is a hard pause; the game.status that follows clears the soft pause everywhere", async () => {
  const { env, backend } = await livePlayed("N3", { status: "running", soft_pause: held(), clock_running: false });
  assert.equal(bar(env).text, "⏸ agent 已暂停");
  backend.controlSession = { ...JSON.parse(JSON.stringify(env.app().state.session)), event_id: 570, status: "paused", pause_reason: "user", soft_pause: null, clock_running: false };
  env.el("btn-pause").click();
  await flush(env);
  assert.equal(posts(backend, "/api/control/pause").length, 1);
  assert.deepEqual([bar(env).textHidden, bar(env).enabled, bar(env).soft], [true, false, false]);
  // the same through the stream alone (the budget limit, hold_game, the end ...)
  const other = await livePlayed("N3", { status: "running", soft_pause: draining(1), clock_running: false });
  other.env.sse("game.status", { status: "paused", reason: "budget", winner: null });
  assert.deepEqual([bar(other.env).textHidden, bar(other.env).nowHidden, bar(other.env).enabled, bar(other.env).soft], [true, true, false, false]);
  assert.equal(other.env.app().state.session.soft_pause, null);
});

test("the server's refusals are shown as they are (a sentence with its full stop), and the state is read again", async () => {
  for (const [path, detail, click] of [
    ["POST /api/control/soft-pause", "只有接管座位时才能软暂停。", "btn-softpause"],
    ["POST /api/control/soft-pause", "对局没有在运行。", "btn-softpause"],
    ["POST /api/control/soft-resume", "没有处于软暂停。", "btn-softpause"],
  ]) {
    const resume = path.endsWith("resume");
    const { env, backend } = await livePlayed("N3", { status: "running", ...(resume ? { soft_pause: held(), clock_running: false } : {}) });
    backend.failures[path] = [409, detail];
    const before = backend.calls.filter((c) => c.method === "GET" && c.url.endsWith("/api/state")).length;
    env.el(click).click();
    await flush(env);
    assert.ok(lastToast(env).includes(detail), `${detail} -> ${lastToast(env)}`);
    assert.ok(backend.calls.filter((c) => c.method === "GET" && c.url.endsWith("/api/state")).length > before, "the state was fetched again");
    assert.equal(env.el("btn-softpause").disabled, false, "and the button is usable again");
  }
});

test("the handlers check the state again: nothing is sent when the game is not running, nobody is played, or it is no soft pause", async () => {
  const paused = await livePlayed("N3", { status: "paused" });
  assert.equal(await paused.env.app().takeover().softPause(), false);
  assert.match(lastToast(paused.env), /对局已暂停，现在不能暂停 agent/);
  assert.equal(await paused.env.app().takeover().softResume(), false);
  assert.match(lastToast(paused.env), /现在没有处于软暂停/);
  assert.equal(await paused.env.app().takeover().hardPause(), false);
  assert.match(lastToast(paused.env), /对局已暂停，不需要再暂停/);
  assert.equal(paused.backend.calls.filter((c) => c.method === "POST").length, 0);
  // a click that gets through on a grey button does nothing either
  paused.env.el("btn-softpause").dispatch("click");
  await flush(paused.env);
  assert.equal(paused.backend.calls.filter((c) => c.method === "POST").length, 0);

  const free = await live({ status: "running" }); // nobody plays
  assert.equal(await free.env.app().takeover().softPause(), false);
  assert.equal(await free.env.app().takeover().softResume(), false);
  assert.equal(free.backend.calls.filter((c) => c.method === "POST").length, 0);

  // already soft-paused: pausing again sends nothing
  const soft = await livePlayed("N3", { status: "running", soft_pause: held(), clock_running: false });
  assert.equal(await soft.env.app().takeover().softPause(), false);
  assert.equal(posts(soft.backend, "/api/control/soft-pause").length, 0);

  // two quick clicks are one request
  const quick = await livePlayed("N3", { status: "running" });
  quick.env.el("btn-softpause").click();
  quick.env.el("btn-softpause").click();
  await flush(quick.env);
  assert.equal(posts(quick.backend, "/api/control/soft-pause").length, 1);
});

test("退出接管 stays a paused-game action: not while the game runs - in a soft pause neither; after a hard pause it works", async () => {
  const { env } = await livePlayed("N3", { status: "running", soft_pause: held(), clock_running: false });
  assert.equal(env.el("btn-release-takeover").disabled, true);
  assert.match(attr(env, "btn-release-takeover", "title"), /请先点「暂停」/);
  env.sse("game.status", { status: "paused", reason: "user", winner: null });
  assert.equal(env.el("btn-release-takeover").disabled, false);
});

// =====================================================================================================
// the local game clock
// =====================================================================================================
test("clock_running=false stops the local estimate (status stays running); the resume lets it go on from the server's value", async () => {
  const session = F.makeSession({ event_id: 500, ...playedOver("N3", { clock: 100 }) });
  const { env } = await bootLive({ session });
  const clock = fakeClock(env);
  env.sse("snapshot", { session }, 500);
  assert.equal(env.app().state.clockSync.running, true);
  clock.advance(10_000);
  assert.equal(Math.round(estimate(env, clock)), 110, "a running game counts on");

  // the soft pause: the stream tells the frozen clock - from then on the estimate stands still
  env.sse("session.updated", { status: "running", soft_pause: draining(1), clock_running: false, clock: 112 }, 501);
  assert.deepEqual([env.app().state.clockSync.running, env.app().state.clockSync.clock], [false, 112]);
  clock.advance(60_000);
  assert.equal(estimate(env, clock), 112, "a minute later: the same");
  env.sse("session.updated", { status: "running", soft_pause: held(), clock_running: false, clock: 112 }, 502);
  clock.advance(60_000);
  assert.equal(estimate(env, clock), 112);
  assert.equal(env.app().state.session.status, "running");

  // ▶ 继续
  env.sse("session.updated", { status: "running", soft_pause: null, clock_running: true, clock: 112 }, 503);
  assert.equal(env.app().state.clockSync.running, true);
  clock.advance(5_000);
  assert.equal(Math.round(estimate(env, clock)), 117);

  // a hard pause follows its game.status: frozen where the estimate was, then the session.updated's clock
  clock.advance(3_000);
  env.sse("game.status", { status: "paused", reason: "user", winner: null }, 504);
  assert.equal(env.app().state.clockSync.running, false);
  clock.advance(90_000);
  assert.equal(Math.round(estimate(env, clock)), 120);
  env.sse("game.status", { status: "running", reason: "resumed", winner: null }, 505);
  assert.equal(env.app().state.clockSync.running, true);
});

test("an HTTP answer (the control endpoints) syncs the clock the same way; a snapshot of a held game starts frozen", async () => {
  const held_ = F.makeSession({ event_id: 500, ...playedOver("N3", { clock: 100, soft_pause: held(), clock_running: false }) });
  const { env, backend } = await bootLive({ session: held_ });
  const clock = fakeClock(env);
  env.sse("snapshot", { session: held_ }, 500);
  assert.deepEqual([env.app().state.clockSync.running, env.app().state.clockSync.clock], [false, 100]);
  clock.advance(30_000);
  assert.equal(estimate(env, clock), 100);

  backend.controlSession = { ...JSON.parse(JSON.stringify(held_)), event_id: 510, clock: 100, soft_pause: null, clock_running: true };
  env.el("btn-softpause").click();
  await flush(env);
  assert.equal(env.app().state.clockSync.running, true, "the adopted session runs the clock");
  clock.advance(4_000);
  assert.equal(Math.round(estimate(env, clock)), 104);
});

test("the countdown of the person's own floor stands still in a soft pause and runs on after it", async () => {
  const floor = F.seatFloor({ seconds_left: 60, deadline_clock: 160 });
  const tk = { channels: { public: { can_write: true, max_chars: 300, can_pass: true, floor } }, seq: 41 };
  const session = F.makeSession({ event_id: 500, ...playedOver("N3", { clock: 100 }, tk) });
  const { env } = await bootLive({ session });
  const clock = fakeClock(env);
  env.sse("snapshot", { session }, 500);
  const hint = () => env.el("composer-hint").textContent;
  const render = () => env.app().composer().render();
  assert.match(hint(), /^轮到你发言 · 还可发 2 条 · 单条 ≤ 300 字 · 剩余 1:00$/);
  clock.advance(10_000);
  render();
  assert.match(hint(), /剩余 0:50$/);

  env.sse("session.updated", { status: "running", soft_pause: held(), clock_running: false, clock: 110 }, 501);
  clock.advance(120_000);
  render();
  assert.match(hint(), /剩余 0:50（计时已冻结）$/, "two minutes of waiting cost the person nothing");

  env.sse("session.updated", { status: "running", soft_pause: null, clock_running: true, clock: 110 }, 502);
  clock.advance(20_000);
  render();
  assert.match(hint(), /剩余 0:30$/);
  assert.doesNotMatch(hint(), /冻结/);
});

// =====================================================================================================
// the cards
// =====================================================================================================
const card = (env, id) => html(env, "participants-grid").split(/(?=<div class="pcard[ "])/).find((p) => p.includes(`data-actor="${id}"`)) || "";

test("held: the card says ⏸ 暂停中 (a ring of its own) wherever states are visible", async () => {
  const { env } = await live({
    status: "running",
    soft_pause: held(),
    clock_running: false,
    participants: F.makeParticipants({ N4: { state: "held" }, N5: { state: "thinking" } }),
    moderator: { id: "moderator", name: "主持人", state: "held", sleep_until: null },
  });
  const c = card(env, "N4");
  assert.match(c, /class="pcard[^"]* st-held"/);
  assert.match(c, /data-state="held"/);
  assert.match(c, /class="pcard-state st-held" title="暂停中：软暂停，等你点「继续」">⏸</);
  assert.match(c, /title="4号 David｜预言家｜⏸ 暂停中：软暂停，等你点「继续」"/);
  assert.match(card(env, "N5"), /pcard-state st-thinking/);
  assert.match(html(env, "mod-chip"), /mod-chip-state st-held" title="暂停中：软暂停，等你点「继续」">⏸/);
  // (the page needs a CSS rule for it, too)
  const css = require("node:fs").readFileSync(require("node:path").join(H.ROOT, "static", "css", "style.css"), "utf8");
  assert.match(css, /\.pcard\.st-held \{/);
  assert.match(css, /\.pcard-state\.st-held,\s*\.mod-chip-state\.st-held \{/);
});

test("a played seat in a held soft pause: every other card says ⏸ 暂停中 (it gives nothing away), its own stays 🎮; draining shows nothing", async () => {
  const { env } = await livePlayed("N3", { status: "running", soft_pause: held(), clock_running: false, participants: F.makeParticipants({ N3: { state: "human" }, N4: { state: "thinking" }, N5: { state: "sleeping" } }) });
  assert.match(card(env, "N3"), /pcard-state st-human"[^>]*title="人类操作中">🎮</);
  for (const id of ["N1", "N4", "N5", "N12"]) assert.match(card(env, id), /pcard-state st-held"[^>]*>⏸</, id);
  assert.match(html(env, "mod-chip"), /mod-chip-state st-held/);
  // nothing leaks through the real states: the one that was thinking and the one that slept look the same
  const stateSpan = (id) => (card(env, id).match(/<span class="pcard-state[^>]*>[^<]*<\/span>/) || [""])[0];
  assert.equal(stateSpan("N4"), stateSpan("N5"));
  assert.notEqual(stateSpan("N4"), "");
  assert.doesNotMatch(card(env, "N4"), /st-thinking|💭/);

  // the calls in flight finish: nothing yet - everything the others do stays hidden, as ever
  env.sse("session.updated", { status: "running", soft_pause: draining(1), clock_running: false, clock: 12.5 }, 501);
  for (const id of ["N1", "N4", "N5"]) assert.doesNotMatch(card(env, id), /pcard-state/, id);
  assert.doesNotMatch(html(env, "mod-chip"), /mod-chip-state/);
  // everybody holds, then the seat lets them go: the ⏸ comes and goes with it
  env.sse("session.updated", { status: "running", soft_pause: held(), clock_running: false, clock: 12.5 }, 502);
  for (const id of ["N1", "N4"]) assert.match(card(env, id), /st-held/, id);
  env.sse("session.updated", { status: "running", soft_pause: null, clock_running: true, clock: 12.5 }, 503);
  for (const id of ["N1", "N4"]) assert.doesNotMatch(card(env, id), /pcard-state/, id);
});

test("agent.status held / back again repaints the card; the state a person plays never becomes held", async () => {
  const { env } = await live({ status: "running", soft_pause: held(), clock_running: false });
  env.sse("agent.status", { id: "N4", state: "held", until: null });
  assert.match(card(env, "N4"), /pcard-state st-held/);
  env.sse("agent.status", { id: "N4", state: "thinking", until: null });
  assert.match(card(env, "N4"), /pcard-state st-thinking/);
  const played = await livePlayed("N3", { status: "running", soft_pause: held(), clock_running: false });
  played.env.sse("agent.status", { id: "N3", state: "held", until: null });
  assert.match(card(played.env, "N3"), /pcard-state st-human"/);
});

// =====================================================================================================
// what stays possible in a soft pause (G4)
// =====================================================================================================
test("in a soft pause the person still writes, answers and uses abilities: the box, the request card and 技能 follow a running game", async () => {
  const request = F.makeRequest({ id: "r1", to: ["N12", "N3"], share_answers: false, auto_close: "all_answered", seconds_left: 60, opened_seq: 30 });
  const tk = {
    channels: { public: { can_write: true, max_chars: 300, can_pass: false, floor: F.seatFree() } },
    requests: [F.seatRequest("r1", { channel_id: "public", deadline_clock: 72.5 })],
    abilities: [F.explodeAbility()],
    seq: 77,
  };
  const { env, backend } = await livePlayed("N12", { status: "running", soft_pause: held(), clock_running: false, requests: [request] }, tk);
  // the input box
  env.el("composer-input").value = "我还在想，先说一句。";
  env.el("composer-input").dispatch("input");
  assert.equal(env.el("btn-composer-send").textContent, "发送");
  assert.equal(env.el("btn-composer-send").disabled, false);
  assert.doesNotMatch(env.el("composer-hint").textContent, /已暂停：/);
  env.el("btn-composer-send").click();
  await flush(env);
  assert.deepEqual((backend.seatCalls || []).filter((c) => c.kind === "speak").map((c) => c.body), [{ channel_id: "public", content: "我还在想，先说一句。", based_on_seq: 77 }]);
  // the request card: answerable (the same rule as in a running game), not greyed
  const action = View.requestAction(env.app().state.session, "N12", env.app().state.session.requests[0]);
  assert.equal(action.enabled, true);
  assert.equal(action.disabledReason, "");
  assert.doesNotMatch(html(env, "feed-records-container") + env.el("feed-records-container").children.map((c) => c.outerHTML).join(""), /继续后才能作答/);
  // abilities
  assert.equal(env.el("btn-composer-ability").disabled, false);
  assert.doesNotMatch(attr(env, "btn-composer-ability", "title"), /现在不能使用技能/);
});

// =====================================================================================================
// the public floor without a deadline (G10)
// =====================================================================================================
test("the person's turn in a public channel: the hint and the channel chip say 不限时, a timed turn says the time left", async () => {
  const untimedTk = (extra = {}) => ({
    channels: { public: { can_write: true, max_chars: 300, can_pass: true, floor: F.seatFloor({ messages_left: 1, untimed: true, seconds_left: null, deadline_clock: null }) }, ...extra },
    attention: ["public"],
  });
  const { env } = await livePlayed("N3", { status: "running", clock: 100 }, untimedTk());
  assert.equal(env.el("composer-hint").textContent, "轮到你发言 · 还可发 1 条 · 单条 ≤ 300 字 · 不限时");
  assert.doesNotMatch(env.el("composer-hint").textContent, /剩余/);
  const chips = html(env, "channels-list");
  assert.match(chips, /<span class="chip-text">轮到你发言<\/span>/);
  assert.match(chips, /还可发 1 条 · 不限时/);
  // the wolves' channel (a timed turn, say): the countdown
  env.sse("takeover.updated", { takeover: F.makeTakeover("N3", { channels: { public: { can_write: true, max_chars: 300, can_pass: true, floor: F.seatFloor({ messages_left: 1, seconds_left: 60, deadline_clock: 160 }) } }, attention: ["public"] }) }, 600);
  assert.match(env.el("composer-hint").textContent, /剩余 \d:\d\d$/);
  assert.doesNotMatch(env.el("composer-hint").textContent, /不限时/);
  assert.doesNotMatch(html(env, "channels-list"), /不限时/);
  // untimed again, this time pushed by the server (the seat was taken over while it held the floor)
  env.sse("takeover.updated", { takeover: F.makeTakeover("N3", untimedTk()) }, 601);
  assert.match(env.el("composer-hint").textContent, /不限时$/);
  // 结束发言 stays what ends it
  assert.equal(env.el("btn-composer-pass").hidden, false);
  assert.equal(env.el("btn-composer-pass").disabled, false);
});

test("a floor without deadline in the channel list of a spectator: 持麦：Alice · 不限时; the store keeps `untimed` through floor.changed", async () => {
  const floor = {
    kind: "queue", active: "N1", paused: false, seconds_left: null, untimed: true,
    queue: [{ holder: "N1", state: "active", messages_used: 0, max_messages: 2 }, { holder: "N2", state: "pending", messages_used: 0, max_messages: 2 }], free: null,
  };
  const channels = F.makeChannels();
  channels[0] = { ...channels[0], mode: "queue", floor };
  const { env } = await live({ status: "running", channels });
  assert.match(html(env, "channels-list"), /持麦：Alice · 不限时/);
  assert.equal(env.app().state.session.channels[0].floor.untimed, true);
  // the person gives the floor up: the next holder is timed
  env.sse("floor.changed", { channel_id: "public", floor: { ...floor, active: "N2", seconds_left: 90, untimed: false, queue: [{ ...floor.queue[0], state: "done" }, { ...floor.queue[1], state: "active" }] } }, 520);
  assert.doesNotMatch(html(env, "channels-list"), /不限时/);
  assert.match(html(env, "channels-list"), /持麦：Bob/);
  assert.equal(env.app().state.session.channels[0].floor.untimed, false);
});

// =====================================================================================================
// the page and its stylesheet
// =====================================================================================================
test("every class of the soft-pause markup has a rule in style.css (a mistyped class would draw nothing - the fake DOM cannot tell)", async () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const css = fs.readFileSync(path.join(H.ROOT, "static", "css", "style.css"), "utf8");
  const page = H.HTML_SRC;
  const block = page.slice(page.indexOf('<span class="softpause" id="softpause">'), page.indexOf('id="btn-release-takeover"'));
  assert.ok(block.length > 100, "the soft-pause block is in the takeover bar, before 退出接管");
  const classes = new Set();
  for (const m of block.matchAll(/class="([^"]+)"/g)) for (const c of m[1].split(/\s+/)) if (c) classes.add(c);
  for (const c of ["softpause", "softpause-status", "btn-softpause", "softpause-now"]) assert.ok(classes.has(c), `${c} is in index.html`);
  for (const c of [...classes].filter((x) => /softpause/.test(x))) assert.ok(css.includes(`.${c}`), `.${c} has a rule`);
  // what the scripts toggle at run time is what the stylesheet names
  for (const sel of [
    ".takeover-bar.is-soft-paused",
    '.softpause-status[data-phase="draining"]',
    ".pcard.st-held",
    ".pcard-state.st-held",
    ".mod-chip-state.st-held",
    ".detail-state.st-held",
  ]) {
    assert.ok(css.includes(sel), `${sel} has a rule`);
  }
  const { env } = await livePlayed("N3", { status: "running", soft_pause: held(), clock_running: false, participants: F.makeParticipants({ N3: { state: "human" }, N4: { state: "held" } }) });
  assert.ok(env.el("takeover-bar").classList.contains("is-soft-paused"));
  assert.equal(attr(env, "softpause-status", "data-phase"), "held");
  assert.match(html(env, "participants-grid"), /class="pcard[^"]* st-held"/);
  // the phone: the bar is a line of its own under the viewpoint label (the soft-pause part and 退出接管, wrapping when they do not fit),
  // in the first row of the page (not in the ⋯ menu)
  assert.match(css, /html\[data-layout="phone"\] \.takeover-bar \.softpause \{[^}]*flex: 1 1 auto;/);
  assert.match(css, /html\[data-layout="phone"\] \.takeover-bar \{[^}]*flex-wrap: wrap;/);
  const relocations = (fs.readFileSync(path.join(H.ROOT, "static", "js", "app.js"), "utf8").match(/const RELOCATIONS = \[([\s\S]*?)\];/) || [, ""])[1];
  const moved = [...relocations.matchAll(/\bid:\s*"([^"]+)"/g)].map((m) => m[1]);
  assert.ok(moved.includes("btn-theme") && moved.length >= 5, "(the list of what moves into the ⋯ menu was found)");
  assert.deepEqual(moved.filter((id) => /softpause|takeover-bar|release-takeover|viewpoint/.test(id)), [], "the viewpoint label, the takeover bar (and its soft-pause button) are never moved into the ⋯ menu (H27)");
});

// -------------------------------------------------------------------------------------------------
H.runTests(tests);
