"use strict";
// The history of games and the replay player (static/js/history.js, static/js/replay.js; F34-F36).
// Run: node tests/frontend/test_replay_ui.js
const assert = require("node:assert/strict");
const path = require("node:path");
const Core = require(path.join(__dirname, "..", "..", "static", "js", "core.js"));
const Store = require(path.join(__dirname, "..", "..", "static", "js", "store.js"));
const View = require(path.join(__dirname, "..", "..", "static", "js", "view.js"));
const History = require(path.join(__dirname, "..", "..", "static", "js", "history.js"));
const Replay = require(path.join(__dirname, "..", "..", "static", "js", "replay.js"));
const F = require("./fixtures.js");
const H = require("./harness.js");
const M = require("./markup.js");

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
const { boot: bootRaw, settle, target, resp } = H;
const boot = async (...args) => {
  const ctx = await bootRaw(...args);
  envs.push(ctx.env);
  return ctx;
};

// ---- a small replay: three players speak in a channel, then the game ends ----------------------------------
function msg(seq, gt, text, from = "N1") {
  return {
    seq,
    gt,
    type: "message.committed",
    data: { seq, ts: "2026-11-18T08:00:00Z", channel_id: "public", from, from_name: from, content: text, kind: "speech", visible_to_ids: F.EVERYONE },
  };
}

function makeReplay(over = {}) {
  // (the replay of ANOTHER game: its own id, not the live game's)
  const initial = F.makeSession({ session_id: "old-1", status: "idle", messages: [], requests: [], channels: [], phase_text: "" });
  const events = [
    {
      seq: 1,
      gt: 0,
      type: "channel.created",
      data: { channel: { id: "public", name: "公共频道", type: "public", mode: "free", members: F.EVERYONE, readonly: [], frozen: false, system: false, owner: null, floor: { kind: "none", active: null, paused: false, seconds_left: null, queue: [], free: null } } },
    },
    { seq: 2, gt: 0, type: "game.status", data: { status: "running", reason: "start", winner: null } },
    msg(3, 1, "大家好"),
    msg(4, 2, "你好", "N2"),
    msg(5, 40, "很久以后…", "N3"), // a 38 s gap: "skip idle" compresses it
    { seq: 6, gt: 41, type: "game.status", data: { status: "finished", reason: "end_game", winner: "好人阵营", summary: "好人获胜" } },
  ];
  return { version: 1, game: { session_id: "old-1", title: "Arena-Agents", scenario_title: "狼人杀 12 人局", status: "finished", winner: "好人阵营", human_involved: false }, initial, events, duration_gt: 41, ...over };
}

// ---- pure helpers ------------------------------------------------------------------------------------------
test("buildSteps groups the events of one ledger entry", () => {
  const events = [{ seq: 1, gt: 0 }, { seq: 1, gt: 0 }, { seq: 2, gt: 3 }, { seq: 5, gt: 9 }, { seq: 5, gt: 9 }, { seq: 5, gt: 9 }];
  assert.deepEqual(Replay.buildSteps(events), [
    { seq: 1, gt: 0, from: 0, to: 2 },
    { seq: 2, gt: 3, from: 2, to: 3 },
    { seq: 5, gt: 9, from: 3, to: 6 },
  ]);
  assert.deepEqual(Replay.buildSteps([]), []);
  assert.deepEqual(Replay.buildSteps(null), []);
});

test("stepDelayMs: game time over speed, idle gaps compressed, never closer than the minimum gap", () => {
  const ms = Replay.stepDelayMs;
  assert.equal(ms(0, 3, 1, true), 3000);
  assert.equal(ms(0, 3, 2, true), 1500);
  assert.equal(ms(0, 3, 0.5, true), 6000);
  assert.equal(ms(0, 8, 8, true), 63, "a long gap is compressed to half a second first: 0.5 s / 8 = 62.5 ms");
  assert.equal(ms(0, 38, 1, true), 500, "skip idle: 38 s -> 0.5 s");
  assert.equal(ms(0, 38, 1, false), 38000, "skip idle off: the whole gap");
  assert.equal(ms(10, 10, 1, true), 120, "two entries of the same game second are never closer than 0.12 s");
  assert.equal(ms(10, 10, 8, true), 15);
  assert.equal(ms(0, 5, 1, true), 5000, "exactly the idle gap is not compressed");
  assert.equal(ms(5, 3, 1, true), 120, "a clock that went backwards is no gap");
  assert.equal(ms(0, 2, "x", true), 2000, "a bad speed means 1x");
});

test("History.rowModel: what a row shows and which of its buttons work", () => {
  const game = { session_id: "g1", scenario_title: "狼人杀 12 人局", status: "finished", winner: "好人阵营", created_at: "2026-11-17T09:00:00Z", duration_gt: 1523, players: 12, tokens: 1250000, cost_usd: 0.5, human_involved: true, current: false };
  const row = History.rowModel(game, { status: "paused", takeoverSeat: null, replaying: false }, Core);
  assert.equal(row.title, "狼人杀 12 人局");
  assert.equal(row.human, true);
  assert.ok(row.meta.includes("已结束 · 好人阵营 获胜") && row.meta.includes("用时 25:23") && row.meta.includes("12 名玩家"));
  assert.ok(row.meta.some((m) => /tokens/.test(m)) && row.meta.includes("$0.50"));
  assert.equal(row.canReplay, true);
  assert.equal(row.showLoad, false, "a finished game is replayed, not continued");
  assert.equal(row.canDelete, true);

  const paused = History.rowModel({ ...game, status: "paused", winner: null }, { status: "paused", takeoverSeat: null, replaying: false }, Core);
  assert.equal(paused.showLoad, true);
  assert.equal(paused.canLoad, true);
  assert.equal(History.rowModel({ ...game, status: "paused" }, { status: "running", takeoverSeat: null, replaying: false }, Core).canLoad, false, "nothing is loaded while a game runs");
  assert.match(History.rowModel({ ...game, status: "paused" }, { status: "running", takeoverSeat: null, replaying: false }, Core).loadTitle, /先暂停/);
  assert.equal(History.rowModel({ ...game, status: "paused" }, { status: "paused", takeoverSeat: null, replaying: true }, Core).canLoad, false, "nor while a replay is shown");

  const live = History.rowModel({ ...game, status: "paused", current: true }, { status: "running", takeoverSeat: null, replaying: false }, Core);
  assert.equal(live.current, true);
  assert.equal(live.canDelete, false, "the live game cannot be deleted");
  assert.equal(live.showLoad, false, "... nor loaded again");
  assert.equal(live.canReplay, false, "... nor replayed while it runs");
  const playedSeat = History.rowModel({ ...game, status: "paused", current: true }, { status: "paused", takeoverSeat: "N3", replaying: false }, Core);
  assert.equal(playedSeat.canReplay, false, "the live game does not replay while a seat is played (the replay shows everything)");
  assert.equal(History.rowModel({ ...game, status: "paused", current: true }, { status: "paused", takeoverSeat: null, replaying: false }, Core).canReplay, true);

  // a game that was never started has no ledger: nothing to replay (the button says so)
  const fresh = History.rowModel({ ...game, status: "idle", winner: null, duration_gt: 0, current: true }, { status: "idle", takeoverSeat: null, replaying: false }, Core);
  assert.equal(fresh.canReplay, false);
  assert.match(fresh.replayTitle, /还没有开始/);
  assert.equal(fresh.canDelete, false);
  assert.equal(History.rowModel({ ...game, status: "idle", winner: null, duration_gt: 0, current: false }, { status: "paused", takeoverSeat: null, replaying: false }, Core).canReplay, false);

  const broken = History.rowModel({ ...game, status: "unreadable" }, null, Core);
  assert.equal(broken.canReplay, false);
  assert.equal(broken.showLoad, false);
  assert.equal(broken.canDelete, true, "an unreadable game can still be removed");
});

test("History.normalizeGames / formatGameTime / formatWhen are tolerant", () => {
  assert.deepEqual(History.normalizeGames({ games: [{ session_id: "a" }, { nope: 1 }, null, { session_id: 7 }] }).map((g) => g.session_id), ["a", "7"]);
  assert.deepEqual(History.normalizeGames([{ session_id: "b" }]).map((g) => g.session_id), ["b"]);
  assert.deepEqual(History.normalizeGames(undefined), []);
  assert.equal(History.formatGameTime(0), "");
  assert.equal(History.formatGameTime(65), "1:05");
  assert.equal(History.formatGameTime(3725), "1:02:05");
  assert.equal(History.formatWhen("garbage"), "");
  assert.equal(History.formatWhen(null), "");
  assert.match(History.formatWhen("2026-11-18T08:00:00Z"), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
});

// ---- the player on its own, against the real store -------------------------------------------------------------
function makePlayer(over = {}) {
  const els = new Map();
  const mk = (id, extra = {}) => {
    const e = new H.FakeEl("div", id);
    Object.assign(e, extra);
    els.set(id, e);
    return e;
  };
  mk("replay-bar", { hidden: true });
  mk("replay-title");
  mk("btn-replay-restart");
  mk("btn-replay-play");
  mk("select-replay-speed", { value: "1" });
  mk("chk-replay-skip", { checked: true });
  mk("replay-seek", { value: "0" });
  mk("replay-time");
  mk("btn-exit-replay");
  const doc = { getElementById: (id) => els.get(id) || null };
  const timers = [];
  const calls = { enter: [], leave: 0, render: 0, renderAll: 0, clock: [], messages: [] };
  let installed = null;
  const ui = Replay.create({
    Core,
    Store,
    document: doc,
    showToast: (t) => calls.toast = t,
    enter: (s) => {
      installed = s;
      calls.enter.push(s);
    },
    leave: () => (calls.leave += 1),
    render: () => (calls.render += 1),
    renderAll: () => (calls.renderAll += 1),
    setClock: (gt) => calls.clock.push(gt),
    onMessage: (m) => calls.messages.push(m.seq),
    setTimeout: (fn, ms) => {
      timers.push({ fn, ms });
      return timers.length;
    },
    clearTimeout: (id) => {
      if (timers[id - 1]) timers[id - 1] = null;
    },
    requestAnimationFrame: null,
    ...over,
  });
  const fire = () => {
    const t = timers.filter(Boolean).pop();
    assert.ok(t, "a timer is pending");
    timers[timers.indexOf(t)] = null;
    t.fn();
    return t.ms;
  };
  const pending = () => timers.filter(Boolean);
  return { ui, els, calls, fire, pending, session: () => installed };
}

test("start installs a fresh session, plays step by step on the game clock and stops at the end", () => {
  const p = makePlayer();
  assert.equal(p.ui.start(makeReplay(), { scenario_title: "狼人杀 12 人局" }), true);
  assert.equal(p.ui.isActive(), true);
  assert.equal(p.els.get("replay-bar").hidden, false);
  assert.equal(p.els.get("replay-title").textContent, "「狼人杀 12 人局」");
  let s = p.session();
  assert.equal(s.status, "idle");
  assert.equal(s.messages.length, 0);
  assert.ok(s.replay && s.replay.session_id === "old-1", "the session says it is a replay");
  assert.equal(s.active_status_text, "重放中 · 0:00 / 0:41");
  assert.equal(p.els.get("replay-seek").max, "6", "six ledger entries = six steps");
});

test("every step is one timer: the first at the minimum gap, the next after the game-time gap over the speed", () => {
  const p = makePlayer();
  p.ui.start(makeReplay());
  const delays = [];
  for (let i = 0; i < 6; i++) delays.push(p.fire());
  // seq 1 (gt 0) after 0.12 s, seq 2 (gt 0) 0.12 s, seq 3 (gt 1) 1 s, seq 4 (gt 2) 1 s, seq 5 (gt 40 = idle gap) 0.5 s, seq 6 (gt 41) 1 s
  assert.deepEqual(delays, [120, 120, 1000, 1000, 500, 1000]);
  assert.equal(p.ui.isPlaying(), false, "it stops by itself at the end");
  assert.equal(p.pending().length, 0);
  const s = p.session();
  assert.equal(s.messages.length, 3);
  assert.equal(s.status, "finished");
  assert.equal(s.winner, "好人阵营");
  assert.deepEqual(p.calls.messages, [3, 4, 5]);
  assert.equal(p.els.get("replay-time").textContent, "0:41 / 0:41");
  assert.equal(p.els.get("btn-replay-play").textContent, "▶");
  assert.equal(p.els.get("replay-seek").value, "6");
});

test("speed and 'skip idle' take effect on the wait that is running", () => {
  const p = makePlayer();
  p.ui.start(makeReplay());
  p.fire();
  p.fire();
  p.fire();
  p.fire(); // seq 4 applied; the next step is the 38 s gap
  assert.equal(p.pending()[0].ms, 500, "idle gap compressed");
  p.els.get("chk-replay-skip").checked = false;
  p.els.get("chk-replay-skip").dispatch("change");
  assert.equal(p.pending().length, 1, "the old timer was replaced, not stacked");
  assert.equal(p.pending()[0].ms, 38000);
  p.els.get("select-replay-speed").value = "8";
  p.els.get("select-replay-speed").dispatch("change");
  assert.equal(p.pending()[0].ms, 4750, "38 s at 8x");
});

test("pause stops the timer, play continues from the same step", () => {
  const p = makePlayer();
  p.ui.start(makeReplay());
  p.fire(); // channel
  p.fire(); // status
  p.fire(); // the first message
  const before = p.session().messages.length;
  assert.equal(before, 1);
  p.els.get("btn-replay-play").dispatch("click"); // pause
  assert.equal(p.ui.isPlaying(), false);
  assert.equal(p.pending().length, 0);
  assert.equal(p.els.get("btn-replay-play").textContent, "▶");
  p.els.get("btn-replay-play").dispatch("click"); // play
  assert.equal(p.ui.isPlaying(), true);
  assert.equal(p.els.get("btn-replay-play").textContent, "⏸");
  assert.equal(p.pending().length, 1, "exactly one timer after resuming");
  p.fire();
  assert.equal(p.session().messages.length, before + 1, "the next step is applied once, not twice");
});

test("seeking back rebuilds the session from the start; seeking forward applies the missing steps", () => {
  const p = makePlayer();
  p.ui.start(makeReplay());
  for (let i = 0; i < 6; i++) p.fire();
  assert.equal(p.session().messages.length, 3);
  const entered = p.calls.enter.length;

  p.ui.seekTo(3); // channel + status + the first message
  assert.equal(p.calls.enter.length, entered + 1, "going back installs a rebuilt session");
  assert.equal(p.session().messages.length, 1);
  assert.equal(p.session().status, "running");
  assert.equal(p.session().winner, null, "the verdict was taken back with the later steps");
  assert.equal(p.els.get("replay-time").textContent, "0:01 / 0:41");
  assert.equal(p.els.get("replay-seek").value, "3");

  const renderAll = p.calls.renderAll;
  p.ui.seekTo(5);
  assert.equal(p.session().messages.length, 3);
  assert.equal(p.calls.enter.length, entered + 1, "going forward only applies steps to the installed session");
  assert.equal(p.calls.renderAll, renderAll + 1);

  p.ui.seekTo(0);
  assert.equal(p.session().messages.length, 0);
  assert.equal(p.session().status, "idle");
  p.ui.seekTo(999);
  assert.equal(p.els.get("replay-seek").value, "6", "clamped to the end");
  p.ui.seekTo(-4);
  assert.equal(p.els.get("replay-seek").value, "0", "clamped to the start");
});

test("the seek bar seeks (a back jump is one synchronous rebuild) and keeps playing from there", () => {
  const p = makePlayer();
  p.ui.start(makeReplay());
  for (let i = 0; i < 6; i++) p.fire();
  p.els.get("replay-seek").value = "2";
  p.els.get("replay-seek").dispatch("input");
  assert.equal(p.session().messages.length, 0);
  assert.equal(p.els.get("replay-time").textContent, "0:00 / 0:41");
});

test("'play' at the end plays again from the start; the restart button rewinds and pauses", () => {
  const p = makePlayer();
  p.ui.start(makeReplay());
  for (let i = 0; i < 6; i++) p.fire();
  assert.equal(p.ui.isPlaying(), false);
  p.els.get("btn-replay-play").dispatch("click");
  assert.equal(p.ui.isPlaying(), true);
  assert.equal(p.session().messages.length, 0, "back at the start");
  assert.equal(p.pending().length, 1);
  p.els.get("btn-replay-restart").dispatch("click");
  assert.equal(p.ui.isPlaying(), false);
  assert.equal(p.pending().length, 0);
  assert.equal(p.els.get("replay-seek").value, "0");
});

test("exit leaves the replay, stops the clock and hides the bar; a second exit is a no-op", () => {
  const p = makePlayer();
  p.ui.start(makeReplay());
  p.fire();
  p.els.get("btn-exit-replay").dispatch("click");
  assert.equal(p.calls.leave, 1);
  assert.equal(p.ui.isActive(), false);
  assert.equal(p.els.get("replay-bar").hidden, true);
  assert.equal(p.pending().length, 0, "no timer survives");
  p.ui.exit();
  assert.equal(p.calls.leave, 1);
  p.ui.seekTo(2); // nothing to seek any more
  assert.equal(p.calls.enter.length, 1);
});

test("a replay without events is shown, cannot be played, and a malformed one is refused", () => {
  const p = makePlayer();
  assert.equal(p.ui.start(makeReplay({ events: [], duration_gt: 0 })), true);
  assert.equal(p.ui.isPlaying(), false);
  assert.equal(p.els.get("btn-replay-play").disabled, true);
  p.ui.exit();
  assert.equal(p.ui.start({ events: [] }), false, "no initial session");
  assert.equal(p.ui.start(null), false);
  assert.match(p.calls.toast, /无效/);
});

test("a replay replays the same session twice (the player never mutates the data it was given)", () => {
  const p = makePlayer();
  const data = makeReplay();
  const snapshot = JSON.stringify(data);
  p.ui.start(data);
  for (let i = 0; i < 6; i++) p.fire();
  assert.equal(JSON.stringify(data), snapshot);
  p.ui.exit();
  p.ui.start(data);
  for (let i = 0; i < 6; i++) p.fire();
  assert.equal(p.session().messages.length, 3);
});

// ---- the view model: a replayed session is not the live game ----------------------------------------------------
test("a replayed session disables every live control and the takeover button", () => {
  const s = Store.normalizeSession(F.makeSession({ status: "paused" }));
  assert.deepEqual(View.controlState(s), { start: false, pause: false, resume: true });
  s.replay = { title: "x" };
  assert.deepEqual(View.controlState(s), { start: false, pause: false, resume: false });
  const avail = View.takeoverAvailability(s, "N3");
  assert.equal(avail.ok, false);
  assert.match(avail.reason, /重放/);
});

// ---- the app: history list, delete, continue, replay, and the live game staying put --------------------------------
async function openHistory(env) {
  env.el("btn-open-history-modal").dispatch("click");
  await settle();
  env.flushRaf();
}
const click = (env, action, id) =>
  env.el("history-list").dispatch("click", { target: target({ "[data-action]": { "data-action": action }, ".history-item": { "data-session-id": id } }) });

/** Replace the app's confirmation dialog: every question is recorded, `answer(index, options)` says yes / no. */
function stubDialog(env, answer = () => true) {
  const roster = env.app().roster();
  const asked = [];
  const original = roster.openDialog;
  roster.openDialog = async (o) => {
    asked.push(o);
    return { ok: Boolean(answer(asked.length - 1, o)), values: {} };
  };
  return { asked, restore: () => (roster.openDialog = original) };
}

const pausedGame = () => ({ session: F.makeSession({ status: "paused" }) });

test("the history modal lists the saved games: current one marked, a human badge, newest data from the server", async () => {
  const { env, backend } = await boot();
  await openHistory(env);
  assert.equal(env.el("history-modal").style.display, "flex");
  assert.ok(backend.calls.some((c) => c.method === "GET" && c.url.endsWith("/api/games")));
  const html = env.el("history-list").innerHTML;
  for (const id of ["s-test", "old-1", "old-2"]) assert.ok(html.includes(`data-session-id="${id}"`), id);
  assert.match(html, /当前/);
  assert.match(html, /有人类参与/);
  assert.match(html, /已结束 · 好人阵营 获胜/);
  assert.match(html, /data-action="delete" disabled/, "the live game cannot be deleted");
  assert.match(html, /data-action="load" disabled title="对局进行中，不能载入其他对局：请先暂停"/, "the default fixture game is running: nothing can be loaded");
  assert.equal(env.el("history-hint").textContent, "共 3 局，最近保存的在前。");
  env.el("btn-close-history-modal").dispatch("click");
  assert.equal(env.el("history-modal").style.display, "none");
});

test("a failed list is said so, an empty one too", async () => {
  const { env, backend } = await boot();
  backend.failures["GET /api/games"] = [503, "存储不可用"];
  await openHistory(env);
  assert.match(env.el("history-list").innerHTML, /读取历史对局失败：.*存储不可用/);
  delete backend.failures["GET /api/games"];
  backend.games = [];
  env.el("btn-refresh-history").dispatch("click");
  await settle();
  assert.match(env.el("history-list").innerHTML, /还没有保存过的对局/);
});

test("deleting asks first and says the agents' memory is not affected; cancelling deletes nothing", async () => {
  const { env, backend } = await boot();
  await openHistory(env);
  const dialog = stubDialog(env, (i) => i > 0); // the first question is answered "no", the second "yes"
  click(env, "delete", "old-1");
  await settle();
  assert.equal(dialog.asked.length, 1);
  assert.ok(dialog.asked[0].bodyHtml.includes("删除这局的存档和日志。bot 已经通过复盘形成的记忆不会被删除，也无法通过删除对局撤销。"), "the fixed sentence of contract 13.10");
  assert.equal(dialog.asked[0].danger, true);
  assert.ok(!backend.calls.some((c) => c.method === "DELETE"), "cancelled: nothing was deleted");

  click(env, "delete", "old-1");
  await settle();
  assert.ok(backend.calls.some((c) => c.method === "DELETE" && c.url.endsWith("/api/games/old-1")));
  assert.ok(!env.el("history-list").innerHTML.includes('data-session-id="old-1"'), "the list was reloaded");
  assert.ok(env.toasts.some((t) => /bot 的记忆没有受到影响/.test(t.textContent)));
  dialog.restore();
});

test("the live game is never asked to be deleted, even if its (disabled) button is clicked anyway", async () => {
  const { env, backend } = await boot();
  await openHistory(env);
  const dialog = stubDialog(env);
  click(env, "delete", "s-test");
  await settle();
  assert.equal(dialog.asked.length, 0, "no question: the action itself refuses");
  assert.ok(!backend.calls.some((c) => c.method === "DELETE"));
  assert.ok(env.toasts.some((t) => /当前对局不能在这里删除：请在「设置 → 对局管理」里删除/.test(t.textContent)), "(v0.14: the current game is deleted in 对局管理)");
});

test("a running game cannot be replaced: the load button is refused with the reason", async () => {
  const { env, backend } = await boot(); // the fixture game is running
  await openHistory(env);
  const dialog = stubDialog(env);
  click(env, "load", "old-2");
  await settle();
  assert.equal(dialog.asked.length, 0);
  assert.ok(!backend.calls.some((c) => c.url.endsWith("/api/games/old-2/load")));
  assert.ok(env.toasts.some((t) => /请先暂停/.test(t.textContent)));
});

test("continuing loads the game through the server and the live game becomes it (paused)", async () => {
  const { env, backend } = await boot(pausedGame());
  await openHistory(env);
  const dialog = stubDialog(env);
  click(env, "load", "old-2");
  await settle();
  env.flushRaf();
  assert.equal(dialog.asked.length, 1);
  assert.match(dialog.asked[0].bodyHtml, /先自动保存/);
  assert.ok(backend.calls.some((c) => c.method === "POST" && c.url.endsWith("/api/games/old-2/load")));
  assert.ok(!backend.calls.some((c) => c.url.includes("/control/start")), "loading never starts the game");
  assert.equal(env.app().state.session.session_id, "old-2");
  assert.equal(env.el("history-modal").style.display, "none");
  dialog.restore();
});

test("declining the question loads nothing", async () => {
  const { env, backend } = await boot(pausedGame());
  await openHistory(env);
  stubDialog(env, () => false);
  click(env, "load", "old-2");
  await settle();
  assert.ok(!backend.calls.some((c) => c.url.endsWith("/api/games/old-2/load")));
  assert.equal(env.app().state.session.session_id, "s-test");
});

test("a refused load keeps the dialog and says why", async () => {
  const { env, backend } = await boot(pausedGame());
  backend.loadError = "对局进行中，不能载入其他对局";
  await openHistory(env);
  stubDialog(env);
  click(env, "load", "old-2");
  await settle();
  assert.ok(env.toasts.some((t) => /对局进行中/.test(t.textContent)));
  assert.equal(env.app().state.session.session_id, "s-test", "the live game stayed");
  assert.equal(env.el("history-modal").style.display, "flex", "the list is still open");
});

test("replay: the saved game takes the screen, the live one is kept aside and comes back untouched", async () => {
  const { env, backend } = await H.bootLive(); // the event stream is connected: the live game keeps going
  envs.push(env);
  env.sse("snapshot", { session: backend.session }, 500);
  backend.replays["old-1"] = makeReplay();
  const live = env.app().state.session;
  const liveId = live.session_id;
  await openHistory(env);
  click(env, "replay", "old-1");
  await settle();
  env.flushRaf();
  assert.ok(backend.calls.some((c) => c.method === "GET" && c.url.endsWith("/api/games/old-1/replay")));
  assert.equal(env.el("history-modal").style.display, "none");
  assert.equal(env.el("replay-bar").hidden, false);
  const app = env.app();
  assert.ok(app.state.session.replay, "the screen shows the replayed session");
  assert.equal(app.state.session.session_id, "old-1");
  assert.equal(app.liveSession().session_id, liveId, "the live game is kept aside");
  assert.equal(env.el("btn-start").disabled, true);
  assert.equal(env.el("btn-pause").disabled, true);
  assert.equal(env.el("btn-resume").disabled, true);
  assert.equal(env.el("btn-new").disabled, true, "nothing of the live game's controls applies");
  assert.match(env.el("active-status-text").textContent, /重放中/);

  // the live stream goes on in the background: its events change the live session, not the replay
  env.sse("phase.changed", { text: "live phase" });
  assert.equal(app.state.session.phase_text !== "live phase", true);
  assert.equal(app.liveSession().phase_text, "live phase");
  env.sse("message.committed", { seq: 900, channel_id: "public", from: "N1", from_name: "Alice", content: "live message", kind: "speech", visible_to_ids: F.EVERYONE });
  assert.ok(!app.state.session.messages.some((m) => m.content === "live message"), "the replay does not show live messages");
  assert.ok(app.liveSession().messages.some((m) => m.content === "live message"), "... they are in the live game");

  // a snapshot of the live stream (a reconnect) replaces the live game, never the replay
  env.sse("snapshot", { session: { ...clone(app.liveSession()), phase_text: "after snapshot" } });
  assert.ok(app.state.session.replay);
  assert.equal(app.liveSession().phase_text, "after snapshot");

  env.el("btn-exit-replay").dispatch("click");
  env.flushRaf();
  assert.equal(env.el("replay-bar").hidden, true);
  assert.equal(app.state.session.replay, undefined);
  assert.equal(app.state.session.session_id, liveId);
  assert.equal(app.state.session.phase_text, "after snapshot", "the game is exactly as it is now");
  assert.ok(app.state.session.messages.some((m) => m.content === "live message"), "nothing the live game did meanwhile was lost");
  assert.equal(env.el("btn-new").disabled, false);
});

const clone = (v) => JSON.parse(JSON.stringify(v));

test("replay: a refused replay says why and changes nothing", async () => {
  const { env, backend } = await boot();
  backend.replayError = "对局进行中或正在接管一个座位，不能重放当前对局：请先暂停并退出接管。";
  await openHistory(env);
  click(env, "replay", "old-1");
  await settle();
  assert.ok(env.toasts.some((t) => /不能重放当前对局/.test(t.textContent)));
  assert.equal(env.el("replay-bar").hidden, true);
  assert.equal(env.app().state.session.session_id, "s-test");
});

test("replay: the viewpoint can be switched while replaying; the takeover button stays off", async () => {
  const { env, backend } = await boot();
  backend.replays["old-1"] = makeReplay();
  await openHistory(env);
  click(env, "replay", "old-1");
  await settle();
  env.flushRaf();
  const app = env.app();
  app.setViewpoint("N2");
  env.flushRaf();
  assert.equal(app.state.viewpoint, "N2");
  assert.equal(env.el("viewpoint-text").textContent, "Bob视角", "the label follows the viewpoint (v0.14, H27)");
  // v0.14: the takeover buttons of the viewpoint menu stay off while a replay is shown - every row says why
  env.el("btn-viewpoint").dispatch("click");
  const menu = env.el("viewpoint-menu").innerHTML;
  assert.equal((menu.match(/data-vp-take="N\d+"[^>]*disabled/g) || []).length, 12, "no seat can be taken over during a replay");
  assert.match(menu, /data-vp-take="N2" title="重放中不能接管：请先退出重放"/);
  app.setViewpoint("god");
  assert.equal(app.state.viewpoint, "god");
});

test("replay: playing it through the app paints messages into the feed, speed changes apply", async () => {
  const { env, backend } = await boot();
  backend.replays["old-1"] = makeReplay();
  await openHistory(env);
  click(env, "replay", "old-1");
  await settle();
  env.flushRaf();
  const app = env.app();
  const player = app.replay();
  assert.equal(player.isPlaying(), true);
  // fast-forward by seeking to the end: the feed shows what a spectator saw
  player.seekTo(6);
  env.flushRaf();
  assert.equal(app.state.session.status, "finished");
  assert.equal(app.state.session.messages.length, 3);
  assert.match(env.el("feed-records-container").innerHTML, /很久以后/);
  assert.match(env.el("active-status-text").textContent, /重放中 · 0:41 \/ 0:41/);
});

test("the history button moves into the phone menu with the others and back", async () => {
  const { env } = await boot();
  assert.ok(env.el("btn-open-history-modal"));
  assert.ok(!H.HTML_SRC.includes("btn-open-sessions-modal"));
});

H.runTests(tests);
