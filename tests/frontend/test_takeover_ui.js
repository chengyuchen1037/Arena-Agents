"use strict";
// Playing a seat (v0.13, F1-F19) in the page: the viewpoint bar [视角▾][切换][接管], the confirmations, the takeover
// bar, the reshuffle reminder, the input box of every channel, the floor / ability buttons, the answers of request
// cards and the "待你操作" marks - static/js/app.js + takeover.js + composer.js against the fake DOM and the fake
// backend (see harness.js).  The pure view model: test_takeover_view.js.
// Run: node tests/frontend/test_takeover_ui.js
const assert = require("node:assert/strict");
const H = require("./harness.js");

const { F, target, settle, bootLive } = H;

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

/** Boot, connect and deliver a snapshot of `over` (event id 500). */
async function live(over = {}, overrides = {}, envOpts = {}) {
  const session = F.makeSession({ event_id: 500, ...over });
  const ctx = await bootLive({ session, ...overrides }, envOpts);
  ctx.env.sse("snapshot", { session }, 500);
  return ctx;
}

/** A game in which `seat` is played (paused unless `over` says otherwise). */
function playedOver(seat, over = {}, takeoverOver = {}) {
  return { status: "paused", participants: F.makeParticipants({ [seat]: { state: "human" } }), takeover: F.makeTakeover(seat, takeoverOver), ...over };
}
const livePlayed = (seat = "N3", over = {}, takeoverOver = {}, overrides = {}, envOpts = {}) => live(playedOver(seat, over, takeoverOver), overrides, envOpts);

const flush = async (env, n = 25) => {
  await settle(n);
  env.flushRaf();
};
const text = (html) => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
const html = (env, id) => env.el(id).innerHTML;
const pick = (env, id) => {
  env.el("select-viewpoint").value = id;
  env.el("select-viewpoint").dispatch("change");
};
const dialogOpen = (env) => env.el("dialog-modal").style.display === "flex";
const dialogOk = async (env) => {
  env.el("btn-dialog-ok").dispatch("click");
  await flush(env);
};
const dialogCancel = async (env) => {
  env.el("btn-dialog-cancel").dispatch("click");
  await flush(env);
};
const lastToast = (env) => (env.toasts.length ? env.toasts[env.toasts.length - 1].textContent : "");
const channelClick = (env, id) => env.el("channels-list").dispatch("click", { target: target({ ".channel-item": { "data-channel": id } }) });
const cardClick = (env, id) => env.el("participants-grid").dispatch("click", { target: target({ ".pcard": { "data-actor": id } }) });
const feedHtml = (env) => {
  const el = env.el("feed-records-container");
  return `${el.innerHTML}\n${el.children.map((c) => c.outerHTML).join("\n")}`;
};
const attr = (env, id, name) => env.el(id).getAttribute(name);
const takeover = (env, seat, over) => env.sse("takeover.updated", { takeover: F.makeTakeover(seat, over) });
const writable = (extra = {}) => ({ channels: { public: { can_write: true, max_chars: 300, floor: F.seatFree(), ...extra } } });
const type = (env, value) => {
  env.el("composer-input").value = value;
  env.el("composer-input").dispatch("input");
};
const enter = (env, extra = {}) => {
  let prevented = false;
  env.el("composer-input").dispatch("keydown", { key: "Enter", shiftKey: false, preventDefault: () => (prevented = true), ...extra });
  return prevented;
};
const speakCalls = (backend) => (backend.seatCalls || []).filter((c) => c.kind === "speak").map((c) => c.body);

// =====================================================================================================
// the viewpoint bar
// =====================================================================================================
test("viewpoint bar: god view cannot take over; a chosen player can - only while the game has not started or is paused", async () => {
  for (const [status, enabled] of [["idle", true], ["paused", true], ["running", false], ["finished", false]]) {
    const { env } = await live({ status });
    assert.equal(env.el("btn-takeover").disabled, true, `${status}: nobody chosen`);
    assert.match(attr(env, "btn-takeover", "title"), /上帝视角不能接管/);
    pick(env, "N3");
    assert.equal(env.el("btn-takeover").disabled, !enabled, status);
    if (!enabled) assert.match(attr(env, "btn-takeover", "title"), status === "running" ? /对局进行中不能接管：请先点「暂停」/ : /对局已结束/);
    else assert.match(attr(env, "btn-takeover", "title"), /接管 3 · Carol/);
    assert.equal(env.el("viewpoint-bar").hidden, false);
    assert.equal(env.el("takeover-bar").hidden, true);
  }
});

test("the status of the game decides at once: a running game disables 接管 even with a choice pending", async () => {
  const { env } = await live({ status: "paused" });
  pick(env, "N3");
  assert.equal(env.el("btn-takeover").disabled, false);
  env.sse("game.status", { status: "running", reason: "continued", winner: null });
  assert.equal(env.el("btn-takeover").disabled, true);
  assert.equal(env.el("select-viewpoint").value, "N3", "the pending choice is kept");
  assert.equal(env.el("btn-viewpoint-switch").disabled, false, "切换 still works while the game runs");
});

test("接管 asks first (what happens), does nothing on cancel, and POSTs the chosen seat on confirm", async () => {
  const { env, backend } = await live({ status: "paused" });
  backend.takeoverResult = F.makePlayedSession("N3", { event_id: 501, status: "paused" });
  pick(env, "N3");
  env.el("btn-takeover").click();
  await flush(env);
  assert.equal(dialogOpen(env), true);
  assert.equal(env.el("dialog-title").textContent, "接管 3 · Carol？");
  const body = text(html(env, "dialog-body"));
  assert.match(body, /以 3 · Carol 的身份参与对局/);
  assert.match(body, /agent 会暂停/);
  assert.match(body, /视角被锁定/);
  assert.match(body, /不知道有人类参与/);
  assert.equal(env.el("btn-dialog-ok").textContent, "接管");

  await dialogCancel(env);
  assert.equal(dialogOpen(env), false);
  assert.equal(backend.takeoverCalls, undefined, "cancelled: nothing was sent");
  assert.equal(env.app().state.viewpoint, "god");

  env.el("btn-takeover").click();
  await flush(env);
  await dialogOk(env);
  assert.deepEqual(backend.takeoverCalls, [{ seat: "N3" }]);
  assert.equal(env.app().state.session.takeover.seat, "N3");
  assert.equal(env.app().state.viewpoint, "N3", "the viewpoint is locked to the seat");
  assert.match(lastToast(env), /已接管 3 · Carol/);
});

test("a takeover before the start tells the person that the identity is known - and offers the re-deal", async () => {
  const { env, backend } = await live({ status: "idle" });
  backend.takeoverResult = F.makePlayedSession("N3", { event_id: 501, status: "idle" }, { reshuffle_available: true });
  pick(env, "N3");
  env.el("btn-takeover").click();
  await flush(env);
  assert.match(text(html(env, "dialog-body")), /对局还没有开始：接管后你会先看到自己的身份，可以重新发牌/);
  await dialogOk(env);
  assert.equal(env.el("takeover-reminder").hidden, false);
  assert.equal(env.el("takeover-reminder-text").textContent, "你已经知道自己的身份，建议点击「重新发牌」");
});

test("a refused takeover shows the server's reason and re-reads the state", async () => {
  const { env, backend } = await live({ status: "paused" });
  backend.failures["POST /api/takeover"] = [409, "对局进行中不能接管：请先暂停对局。"];
  pick(env, "N3");
  env.el("btn-takeover").click();
  await flush(env);
  await dialogOk(env);
  assert.match(lastToast(env), /对局进行中不能接管/);
  assert.equal(env.app().state.session.takeover.seat, null);
  assert.ok(backend.calls.filter((c) => c.method === "GET" && c.url.endsWith("/api/state")).length >= 2, "the state was fetched again");
  assert.equal(env.el("btn-takeover").disabled, false, "the button is usable again");
});

test("the game may start while the confirmation is open: nothing is sent", async () => {
  const { env, backend } = await live({ status: "paused" });
  pick(env, "N3");
  env.el("btn-takeover").click();
  await flush(env);
  env.sse("game.status", { status: "running", reason: "continued", winner: null }); // somebody pressed 继续 meanwhile
  await dialogOk(env);
  assert.equal(backend.takeoverCalls, undefined);
  assert.match(lastToast(env), /对局进行中不能接管/);
});

// =====================================================================================================
// the takeover bar and the lock
// =====================================================================================================
test("a played seat: the bar says who, the viewpoint bar is gone, the viewpoint is the seat's, the card says 人类操作中", async () => {
  const { env } = await livePlayed("N3");
  assert.equal(env.el("takeover-bar").hidden, false);
  assert.equal(env.el("viewpoint-bar").hidden, true);
  assert.equal(env.el("takeover-seat").textContent, "3 · Carol");
  assert.equal(attr(env, "html", "x"), null);
  assert.equal(env.document.documentElement.getAttribute("data-takeover"), "1");
  assert.equal(env.app().state.viewpoint, "N3");
  assert.equal(env.el("feed-viewing").textContent, "视角：Carol");
  const card = html(env, "participants-grid").split(/(?=<div class="pcard[ "])/).find((p) => p.includes('data-actor="N3"'));
  assert.match(card, /pcard st-human|pcard[^"]* st-human/);
  assert.match(card, /pcard-state st-human"[^>]*title="人类操作中">🎮</);
  // others' states stay hidden in the locked view; identities too
  const other = html(env, "participants-grid").split(/(?=<div class="pcard[ "])/).find((p) => p.includes('data-actor="N4"'));
  assert.doesNotMatch(other, /pcard-state/);
});

test("the viewpoint stays locked: the drop-down hidden, 切换 and the detail panel's 以 X 的视角观看 do nothing", async () => {
  const { env } = await livePlayed("N3");
  assert.equal(env.el("btn-viewpoint-switch").disabled, true);
  assert.match(attr(env, "btn-viewpoint-switch", "title"), /已锁定/);
  env.app().setViewpoint("N5");
  assert.equal(env.app().state.viewpoint, "N3");
  cardClick(env, "N5");
  const panel = html(env, "detail-head");
  assert.match(panel, /data-detail-action="view-as" disabled/);
  assert.match(panel, /接管期间视角已锁定/);
  env.el("detail-panel").dispatch("click", { target: target({ "[data-detail-action]": { "data-detail-action": "view-as" } }) });
  assert.equal(env.app().state.viewpoint, "N3");
  // the seat itself has nothing to switch to
  cardClick(env, "N3");
  assert.doesNotMatch(html(env, "detail-head"), /view-as/);
  // while the game is not played the button works as before
  const free = await live({ status: "paused" });
  cardClick(free.env, "N5");
  assert.doesNotMatch(html(free.env, "detail-head"), /view-as" disabled/);
});

test("a takeover in another tab arrives as takeover.updated: this page locks, and unlocks to what the user had chosen", async () => {
  const { env } = await live({ status: "paused" });
  env.app().setViewpoint("N1");
  assert.equal(env.storage.get("arena_viewpoint_v1"), "N1");
  takeover(env, "N3");
  assert.equal(env.app().state.viewpoint, "N3");
  assert.equal(env.el("takeover-bar").hidden, false);
  assert.equal(env.el("viewpoint-bar").hidden, true);
  assert.equal(env.storage.get("arena_viewpoint_v1"), "N1", "the user's own choice is not overwritten by the lock");
  env.sse("takeover.updated", { takeover: { seat: null, reshuffle_available: false } });
  assert.equal(env.app().state.viewpoint, "N1", "back to what the user had");
  assert.equal(env.el("takeover-bar").hidden, true);
  assert.equal(env.el("viewpoint-bar").hidden, false);
  assert.equal(env.el("select-viewpoint").value, "N1");
  assert.equal(env.document.documentElement.getAttribute("data-takeover"), "0");
  assert.equal(env.el("composer").hidden, true);
});

test("a page loaded while a seat is played (a restart, a second tab) is locked from the first paint", async () => {
  const { env } = await livePlayed("N6");
  assert.equal(env.app().state.viewpoint, "N6");
  assert.equal(env.el("takeover-seat").textContent, "6 · Frank");
  // the saved viewpoint preference of an earlier visit does not matter
  const withPref = await livePlayed("N6", {}, {}, {}, { storage: { arena_viewpoint_v1: "N2" } });
  assert.equal(withPref.env.app().state.viewpoint, "N6");
});

// =====================================================================================================
// giving the seat back
// =====================================================================================================
test("退出接管: not while the game runs (tooltip says pause first); when paused: asks, then POSTs; the bar goes", async () => {
  const running = await livePlayed("N3", { status: "running" });
  assert.equal(running.env.el("btn-release-takeover").disabled, true);
  assert.match(attr(running.env, "btn-release-takeover", "title"), /请先点「暂停」/);

  const { env, backend } = await livePlayed("N3", { status: "paused" });
  assert.equal(env.el("btn-release-takeover").disabled, false);
  env.el("btn-release-takeover").click();
  await flush(env);
  assert.equal(env.el("dialog-title").textContent, "退出接管 3 · Carol？");
  assert.match(text(html(env, "dialog-body")), /完整的对局记录——包括你以它的名义说过的话/);
  assert.match(text(html(env, "dialog-body")), /不会被告知有人代打过/);
  assert.equal(env.el("btn-dialog-ok").textContent, "退出接管");
  await dialogCancel(env);
  assert.equal(backend.releaseCalls, undefined);
  assert.equal(env.app().state.session.takeover.seat, 3 && "N3");

  env.el("btn-release-takeover").click();
  await flush(env);
  await dialogOk(env);
  assert.equal(backend.releaseCalls, 1);
  assert.equal(env.app().state.session.takeover.seat, null);
  assert.equal(env.el("takeover-bar").hidden, true);
  assert.equal(env.el("viewpoint-bar").hidden, false);
  assert.equal(env.app().state.viewpoint, "god");
  assert.match(lastToast(env), /已退出接管：3 · Carol 交还给 agent/);
});

test("the handlers check the state again: a release or a takeover that gets through to a running game sends nothing", async () => {
  const running = await livePlayed("N3", { status: "running" });
  const released = await running.env.app().takeover().release();
  assert.equal(released, false);
  assert.equal(running.backend.releaseCalls, undefined);
  assert.match(lastToast(running.env), /对局进行中不能退出接管：请先点「暂停」/);
  assert.equal(dialogOpen(running.env), false, "not even a question is asked");

  const game = await live({ status: "running" });
  assert.equal(await game.env.app().takeover().takeover("N3"), false);
  assert.equal(game.backend.takeoverCalls, undefined);
  assert.match(lastToast(game.env), /对局进行中不能接管/);
  assert.equal(await game.env.app().takeover().takeover("moderator"), false, "never the moderator");
  assert.equal(await game.env.app().takeover().takeover("god"), false);
  assert.equal(game.backend.takeoverCalls, undefined);
  // and nothing is sent twice: a second request while the first is in flight is ignored
  const idle = await live({ status: "paused" });
  let release;
  const gate = new Promise((resolve) => (release = resolve));
  const original = idle.backend.fetch;
  idle.backend.fetch = async (url, init) => {
    if (String(url).endsWith("/api/takeover")) await gate;
    return original(url, init);
  };
  idle.env.sandbox.fetch = idle.backend.fetch;
  idle.backend.takeoverResult = F.makePlayedSession("N3", { event_id: 501, status: "paused" });
  const first = idle.env.app().takeover().takeover("N3");
  await flush(idle.env);
  assert.equal(dialogOpen(idle.env), true);
  const second = idle.env.app().takeover().takeover("N3");
  assert.equal(await second, false, "busy: ignored");
  await dialogOk(idle.env);
  release();
  assert.equal(await first, true);
  assert.equal(idle.backend.takeoverCalls.length, 1);
});

test("退出接管 is allowed before the start, when paused and after the end; the game starting meanwhile stops it", async () => {
  for (const status of ["idle", "paused", "finished"]) {
    const { env } = await livePlayed("N3", { status });
    assert.equal(env.el("btn-release-takeover").disabled, false, status);
  }
  const { env, backend } = await livePlayed("N3", { status: "paused" });
  env.el("btn-release-takeover").click();
  await flush(env);
  env.sse("game.status", { status: "running", reason: "continued", winner: null });
  await dialogOk(env);
  assert.equal(backend.releaseCalls, undefined);
  assert.match(lastToast(env), /对局进行中不能退出接管/);
});

test("a refused release shows the reason; the seat stays played", async () => {
  const { env, backend } = await livePlayed("N3", { status: "paused" });
  backend.failures["POST /api/takeover/release"] = [409, "对局进行中不能退出接管：请先暂停对局。"];
  env.el("btn-release-takeover").click();
  await flush(env);
  await dialogOk(env);
  assert.match(lastToast(env), /对局进行中不能退出接管/);
  assert.equal(env.app().state.session.takeover.seat, "N3");
});

// =====================================================================================================
// the re-deal reminder
// =====================================================================================================
test("the reminder: only for a played seat before the start; the button re-deals once, however often it is clicked", async () => {
  const { env, backend } = await livePlayed("N3", { status: "idle" }, { reshuffle_available: true });
  assert.equal(env.el("takeover-reminder").hidden, false);
  assert.equal(env.el("btn-shuffle-identities").disabled, false);
  env.el("btn-shuffle-identities").click();
  env.el("btn-shuffle-identities").click(); // a double click
  env.el("btn-shuffle-identities").click();
  await flush(env);
  assert.equal(backend.shuffleCalls, 1, "one request, however often it was clicked");
  assert.match(lastToast(env), /已重新发牌/);
  await flush(env);
  env.el("btn-shuffle-identities").click(); // again, later: allowed (still not started)
  await flush(env);
  assert.equal(backend.shuffleCalls, 2);
});

test("the reminder is hidden - and its button cannot re-deal - once the game has started, however it got there", async () => {
  for (const [name, over, tk] of [
    ["running", { status: "running" }, { reshuffle_available: false }],
    ["paused", { status: "paused" }, { reshuffle_available: false }],
    ["finished", { status: "finished" }, { reshuffle_available: false }],
    ["a start record while idle", { status: "idle" }, { reshuffle_available: false }],
    ["the flag is stale", { status: "running" }, { reshuffle_available: true }],
  ]) {
    const { env, backend } = await livePlayed("N3", over, tk);
    assert.equal(env.el("takeover-reminder").hidden, true, name);
    assert.equal(env.el("btn-shuffle-identities").disabled, true, name);
    env.el("btn-shuffle-identities").dispatch("click"); // a click that gets through anyway
    await flush(env);
    assert.equal(backend.shuffleCalls, undefined, name);
  }
  // not played: nothing to remind of
  const { env } = await live({ status: "idle" });
  assert.equal(env.el("takeover-reminder").hidden, true);
});

test("the start overtakes the click: a reminder that is still on screen cannot re-deal a game that has started", async () => {
  const { env, backend } = await livePlayed("N3", { status: "idle" }, { reshuffle_available: true });
  assert.equal(env.el("takeover-reminder").hidden, false);
  env.app().state.session.status = "running"; // the start arrived; the page has not been repainted yet
  env.el("btn-shuffle-identities").dispatch("click");
  await flush(env);
  assert.equal(backend.shuffleCalls, undefined);
  assert.match(lastToast(env), /对局已经开始，不能再重新发牌/);
  // the real thing: the start arrives as events
  const second = await livePlayed("N3", { status: "idle" }, { reshuffle_available: true });
  second.env.sse("game.status", { status: "running", reason: "started", winner: null });
  assert.equal(second.env.el("takeover-reminder").hidden, true);
  second.env.sse("takeover.updated", { takeover: F.makeTakeover("N3", { reshuffle_available: false }) });
  assert.equal(second.env.el("takeover-reminder").hidden, true);
});

test("a re-deal the server refuses (it has started) shows why and removes the reminder", async () => {
  const { env, backend } = await livePlayed("N3", { status: "idle" }, { reshuffle_available: true });
  backend.failures["POST /api/control/shuffle-identities"] = [409, "对局已经开始，不能再重新发牌。"];
  env.el("btn-shuffle-identities").click();
  await flush(env);
  assert.match(lastToast(env), /对局已经开始，不能再重新发牌/);
  assert.ok(backend.calls.filter((c) => c.url.endsWith("/api/state")).length >= 2, "the state was read again");
});

// =====================================================================================================
// channels: 禁言中 / 轮到你发言 / 待你操作
// =====================================================================================================
test("the channel bar marks the channels that wait for the person (the floor, an open request) and the muted ones", async () => {
  const { env } = await livePlayed("N3", { status: "running" }, {
    channels: {
      public: { can_write: true, max_chars: 300, floor: F.seatFloor({ messages_left: 1 }), can_pass: true },
      wolves: { can_write: false, code: "NO_FLOOR", why_not: "还没轮到你发言。", max_chars: null },
    },
    attention: ["public"],
  });
  const items = {};
  for (const part of html(env, "channels-list").split(/(?=<div class="channel-item )/).filter((p) => p.startsWith('<div class="channel-item '))) {
    items[(part.match(/data-channel="([^"]+)"/) || [])[1]] = part.split(/<div class="(?:ticket-item|channel-group-title)/)[0];
  }
  assert.match(items.public, /^<div class="channel-item[^"]* needs-action/);
  assert.match(items.public, /class="attn-chip"[^>]*>待你操作</);
  assert.match(items.public, /轮到你发言/);
  assert.match(items.public, /title="[^"]*待你操作/);
  assert.doesNotMatch(items.wolves, /needs-action|attn-chip/);
  assert.match(items.wolves, /🔇/);
  assert.match(items.wolves, /禁言中/);
  // when the push says nothing waits any more the marks go
  takeover(env, "N3", { attention: [], channels: { public: { can_write: true, max_chars: 300, floor: F.seatFree() } } });
  assert.doesNotMatch(html(env, "channels-list"), /needs-action|attn-chip/);
});

// =====================================================================================================
// the input box
// =====================================================================================================
test("the input box follows the selected channel; every channel keeps its own draft; the counter is n/max", async () => {
  const { env } = await livePlayed("N3", { status: "running" }, { channels: { public: { can_write: true, max_chars: 300, floor: F.seatFree() }, wolves: { can_write: true, code: null, why_not: null, max_chars: 120, floor: F.seatFree() } } });
  assert.equal(env.el("composer").hidden, false);
  assert.equal(env.app().state.selectedChannel, "public");
  type(env, "公共频道的草稿");
  assert.equal(env.el("composer-count").textContent, "7/300");
  assert.match(attr(env, "composer-input", "placeholder"), /在「公共频道」发言/);

  channelClick(env, "wolves");
  env.flushRaf();
  assert.equal(env.el("composer-input").value, "", "another channel, another box");
  assert.equal(env.el("composer-count").textContent, "0/120");
  assert.match(attr(env, "composer-input", "placeholder"), /在「狼人阵营」发言/);
  type(env, "狼人的悄悄话");
  assert.equal(env.el("composer-count").textContent, "6/120");

  channelClick(env, "public");
  env.flushRaf();
  assert.equal(env.el("composer-input").value, "公共频道的草稿");
  channelClick(env, "wolves");
  env.flushRaf();
  assert.equal(env.el("composer-input").value, "狼人的悄悄话");
  // a message arriving does not touch what is being typed
  env.sse("message.committed", { ...F.msg(900, "wolves", "N6", "我也在", "speech", ["moderator", ...F.WOLVES]) });
  assert.equal(env.el("composer-input").value, "狼人的悄悄话");
  // over the limit the counter turns red - the tail is cut by the server when it is sent
  type(env, "字".repeat(130));
  assert.equal(env.el("composer-count").textContent, "130/120");
  assert.equal(env.el("composer-count").classList.contains("over"), true);
  assert.match(attr(env, "composer-count", "title"), /超出 10 字/);
});

test("the drafts belong to the seat: another game or another seat starts with empty boxes", async () => {
  const { env } = await livePlayed("N3", { status: "running" }, writable());
  type(env, "写到一半");
  env.sse("takeover.updated", { takeover: { seat: null, reshuffle_available: false } });
  takeover(env, "N5", writable());
  assert.equal(env.el("composer-input").value, "", "N5's box is empty");
  assert.equal(env.el("composer").hidden, false);
});

test("the send button: 发送 / 禁言中 (hover: why) / 已暂停 / 未开始 / 已结束 - and the box can always be typed in", async () => {
  const muted = { channels: { public: { can_write: false, code: "NO_FLOOR", why_not: "还没轮到你发言，当前可以发言的是：Alice。", max_chars: null, floor: null } } };
  const states = [
    ["running", writable(), "发送", false],
    ["running", muted, "禁言中", true],
    ["paused", writable(), "已暂停", true],
    ["idle", writable(), "未开始", true],
    ["finished", writable(), "已结束", true],
  ];
  for (const [status, tk, label, disabled] of states) {
    const { env } = await livePlayed("N3", { status }, tk);
    type(env, "我想说一句话");
    assert.equal(env.el("btn-composer-send").textContent, label, `${status}/${label}`);
    assert.equal(env.el("btn-composer-send").disabled, disabled, `${status}/${label}`);
    assert.equal(env.el("composer-input").disabled, false, `${status}: typing is always possible`);
    assert.equal(env.el("composer-input").value, "我想说一句话");
    if (label === "禁言中") {
      assert.equal(attr(env, "btn-composer-send", "title"), "还没轮到你发言，当前可以发言的是：Alice。");
      assert.equal(env.el("composer-hint").textContent, "还没轮到你发言，当前可以发言的是：Alice。");
    }
  }
  // nothing typed: nothing to send
  const { env } = await livePlayed("N3", { status: "running" }, writable());
  assert.equal(env.el("btn-composer-send").disabled, true);
  type(env, "x");
  assert.equal(env.el("btn-composer-send").disabled, false);
});

test("Enter sends, Shift+Enter breaks the line, an IME confirmation is not a send; the box empties on success", async () => {
  const { env, backend } = await livePlayed("N3", { status: "running" }, { seq: 77, ...writable() });
  type(env, "大家好，我先说几句。");
  assert.equal(enter(env, { shiftKey: true }), false, "Shift+Enter: the browser breaks the line");
  assert.equal(enter(env, { isComposing: true }), false);
  assert.equal(enter(env, { keyCode: 229 }), false);
  await flush(env);
  assert.equal(speakCalls(backend).length, 0);

  assert.equal(enter(env), true, "Enter is taken (no line break)");
  await flush(env);
  assert.deepEqual(speakCalls(backend), [{ channel_id: "public", content: "大家好，我先说几句。", based_on_seq: 77 }]);
  assert.equal(env.el("composer-input").value, "", "sent: the draft is gone");
  assert.equal(env.el("composer-count").textContent, "0/300");
  // an empty box sends nothing
  enter(env);
  await flush(env);
  assert.equal(speakCalls(backend).length, 1);
  // the button does the same
  type(env, "再说一句");
  env.el("btn-composer-send").click();
  await flush(env);
  assert.equal(speakCalls(backend).length, 2);
});

test("the draft remembers what the person had seen when it was begun (based_on_seq); STALE keeps it and asks to send again", async () => {
  const { env, backend } = await livePlayed("N3", { status: "running" }, { seq: 50, ...writable() });
  type(env, "写了很久的草稿"); // begun at seq 50
  takeover(env, "N3", { seq: 61, ...writable() }); // the world moves on meanwhile (a phase change, say)
  backend.seatReply = { speak: { accepted: false, seq: null, code: "STALE", message: "你思考期间世界发生了重要变化，请先阅读最新内容后再决定是否发言。", data: { latest_seq: 61 } } };
  enter(env);
  await flush(env);
  assert.deepEqual(speakCalls(backend), [{ channel_id: "public", content: "写了很久的草稿", based_on_seq: 50 }]);
  assert.equal(env.el("composer-input").value, "写了很久的草稿", "the draft is kept");
  assert.equal(env.el("composer-notice").hidden, false);
  assert.match(env.el("composer-notice").textContent, /局面刚刚发生了重要变化.*草稿已保留.*再点一次「发送」/);

  backend.seatReply = { speak: { accepted: true, seq: 62, code: null, message: "已发送。" } };
  enter(env);
  await flush(env);
  assert.equal(speakCalls(backend)[1].based_on_seq, 61, "after the confirmation the draft is based on what is current");
  assert.equal(env.el("composer-input").value, "");
  assert.equal(env.el("composer-notice").hidden, true);
});

test("a refused send keeps the draft and says why; a cut message says how much was cut", async () => {
  const { env, backend } = await livePlayed("N3", { status: "running" }, writable());
  backend.seatReply = { speak: { accepted: false, seq: null, code: "NO_FLOOR", message: "该频道当前没有进行中的发言权。" } };
  type(env, "我想说");
  enter(env);
  await flush(env);
  assert.equal(env.el("composer-input").value, "我想说");
  assert.equal(env.el("composer-notice").textContent, "该频道当前没有进行中的发言权。");
  // typing again clears the old notice
  type(env, "我想说什么");
  assert.equal(env.el("composer-notice").hidden, true);
  backend.seatReply = { speak: { accepted: true, seq: 9, code: null, message: "已发送，但超出字数上限", truncated: { dropped_chars: 12, kept_chars: 300 } } };
  enter(env);
  await flush(env);
  assert.equal(env.el("composer-input").value, "");
  assert.match(lastToast(env), /发言超出字数上限，末尾 12 个字被截掉了/);
  // a network error: nothing is lost
  backend.failures["POST /api/seat/speak"] = [409, "对局已暂停，不能操作：请先点「继续」。"];
  type(env, "又一句");
  enter(env);
  await flush(env);
  assert.equal(env.el("composer-input").value, "又一句");
  assert.match(lastToast(env), /对局已暂停/);
});

test("a message cannot be sent twice by pressing Enter again while the first is on its way", async () => {
  let release;
  const gate = new Promise((resolve) => (release = resolve));
  const { env, backend } = await livePlayed("N3", { status: "running" }, writable());
  const original = backend.fetch;
  backend.fetch = async (url, init) => {
    if (String(url).endsWith("/api/seat/speak")) await gate;
    return original(url, init);
  };
  env.sandbox.fetch = backend.fetch;
  type(env, "只发一次");
  enter(env);
  enter(env);
  enter(env);
  release();
  await flush(env, 40);
  assert.equal(speakCalls(backend).length, 1);
});

// =====================================================================================================
// the floor: 结束发言
// =====================================================================================================
test("结束发言 appears only while the seat holds the floor of that channel; it POSTs a pass", async () => {
  const mine = { channels: { public: { can_write: true, max_chars: 300, can_pass: true, floor: F.seatFloor() } } };
  const { env, backend } = await livePlayed("N3", { status: "running" }, mine);
  assert.equal(env.el("btn-composer-pass").hidden, false);
  assert.equal(env.el("btn-composer-pass").disabled, false);
  assert.match(env.el("composer-hint").textContent, /^轮到你发言 · 还可发 2 条 · 单条 ≤ 300 字 · 剩余/);
  env.el("btn-composer-pass").click();
  await flush(env);
  assert.deepEqual((backend.seatCalls || []).filter((c) => c.kind === "pass").map((c) => c.body), [{ channel_id: "public" }]);
  assert.match(lastToast(env), /已结束发言/);
  // in the free discussion / a channel whose floor is not mine there is no such button
  takeover(env, "N3", writable());
  assert.equal(env.el("btn-composer-pass").hidden, true);
  takeover(env, "N3", { channels: { public: { can_write: true, max_chars: 300, floor: F.seatFloor({ mine: false }) } } });
  assert.equal(env.el("btn-composer-pass").hidden, true);
  // my floor, paused: the button is there but cannot be pressed
  takeover(env, "N3", { channels: { public: { can_write: false, max_chars: null, can_pass: false, code: "NO_FLOOR", why_not: "发言权已暂停。", floor: F.seatFloor({ paused: true }) } } });
  assert.equal(env.el("btn-composer-pass").hidden, false);
  assert.equal(env.el("btn-composer-pass").disabled, true);
  // a refused pass says why
  takeover(env, "N3", mine);
  backend.seatReply = { pass: { accepted: false, seq: null, code: "NO_FLOOR", message: "你当前没有可以放弃的发言权。" } };
  env.el("btn-composer-pass").click();
  await flush(env);
  assert.equal(env.el("composer-notice").textContent, "你当前没有可以放弃的发言权。");
});

// =====================================================================================================
// abilities
// =====================================================================================================
test("技能: grey without an enabled ability; with one: list -> form from the scenario's parameters -> a second confirmation -> POST", async () => {
  const { env, backend } = await livePlayed("N12", { status: "running" }, writable());
  assert.equal(env.el("btn-composer-ability").disabled, true);
  assert.match(attr(env, "btn-composer-ability", "title"), /现在没有可用的技能/);

  takeover(env, "N12", { ...writable(), abilities: [F.explodeAbility()] });
  assert.equal(env.el("btn-composer-ability").disabled, false);
  env.el("btn-composer-ability").click();
  await flush(env);
  assert.equal(env.el("dialog-title").textContent, "使用技能");
  const body = html(env, "dialog-body");
  assert.match(body, /white_wolf_explode/);
  assert.match(text(body), /白狼王自爆：只在主持人启用期间可用/);
  // the form is made from the parameters: a player choice (alive players) and a text of at most 120 characters
  assert.match(body, /<select class="form-input" id="ability-param-white_wolf_explode-target"><option value="">请选择一名玩家<\/option>/);
  assert.match(body, /<option value="N7">7 号 Grace<\/option>|<option value="N7">7 号 /);
  assert.match(body, /<option value="N12">12 号 Leo（我）<\/option>/);
  assert.match(body, /<textarea class="form-input" id="ability-param-white_wolf_explode-declaration" rows="2" maxlength="120"/);

  // an empty form: the dialog asks again, naming what is missing
  await dialogOk(env);
  assert.match(lastToast(env), /请填写「目标」/);
  assert.equal(dialogOpen(env), true);
  env.el("ability-param-white_wolf_explode-target").value = "N7";
  await dialogOk(env);
  assert.match(lastToast(env), /请填写「宣言」/);
  env.el("ability-param-white_wolf_explode-target").value = "N7";
  env.el("ability-param-white_wolf_explode-declaration").value = "我是白狼王，自爆！";
  await dialogOk(env);
  // the second confirmation shows what is about to happen
  assert.equal(env.el("dialog-title").textContent, "确认使用技能「white_wolf_explode」？");
  const sure = text(html(env, "dialog-body"));
  assert.match(sure, /目标 7 号 Grace/);
  assert.match(sure, /宣言 我是白狼王，自爆！/);
  assert.match(sure, /技能释放后不能撤销/);
  assert.equal(backend.seatCalls, undefined, "nothing is sent before the second confirmation");
  assert.match(env.el("btn-dialog-ok").className, /btn-danger-outline/);
  await dialogOk(env);
  assert.deepEqual(backend.seatCalls.map((c) => [c.kind, c.body]), [
    ["ability", { ability_id: "white_wolf_explode", params: { target: "N7", declaration: "我是白狼王，自爆！" } }],
  ]);
  assert.match(lastToast(env), /已使用技能「white_wolf_explode」/);
});

test("技能: cancelling either dialog sends nothing; a refusal is shown; a paused game cannot use it", async () => {
  const { env, backend } = await livePlayed("N12", { status: "running" }, { ...writable(), abilities: [F.explodeAbility()] });
  env.el("btn-composer-ability").click();
  await flush(env);
  await dialogCancel(env);
  assert.equal(backend.seatCalls, undefined);
  env.el("btn-composer-ability").click();
  await flush(env);
  env.el("ability-param-white_wolf_explode-target").value = "N4";
  env.el("ability-param-white_wolf_explode-declaration").value = "走你";
  await dialogOk(env);
  await dialogCancel(env); // "返回" at the confirmation
  assert.equal(backend.seatCalls, undefined);
  env.el("btn-composer-ability").click();
  await flush(env);
  env.el("ability-param-white_wolf_explode-target").value = "N4";
  env.el("ability-param-white_wolf_explode-declaration").value = "走你";
  await dialogOk(env);
  backend.seatReply = { ability: { accepted: false, seq: null, code: "ABILITY_DISABLED", message: "能力 white_wolf_explode 当前不可用（未启用）。" } };
  await dialogOk(env);
  assert.match(lastToast(env), /能力 white_wolf_explode 当前不可用/);

  const paused = await livePlayed("N12", { status: "paused" }, { ...writable(), abilities: [F.explodeAbility()] });
  assert.equal(paused.env.el("btn-composer-ability").disabled, true);
  assert.match(attr(paused.env, "btn-composer-ability", "title"), /对局已暂停，现在不能使用技能/);
});

test("技能 forms follow the parameter types: enums, integers, several players, optional texts", async () => {
  const ability = {
    id: "odd_one",
    description: "各种参数。",
    quota_left: 2,
    params: [
      { name: "side", type: "enum", optional: false, values: ["左", "右"] },
      { name: "count", type: "int", optional: false, min: 1, max: 5 },
      { name: "players", type: "player_ids", optional: false, candidates: ["N1", "N2", "N4"] },
      { name: "reason", type: "text", optional: true, max_chars: 20 },
    ],
  };
  const { env, backend } = await livePlayed("N3", { status: "running" }, { ...writable(), abilities: [ability] });
  env.el("btn-composer-ability").click();
  await flush(env);
  const body = html(env, "dialog-body");
  assert.match(text(body), /还可使用 2 次/);
  assert.match(body, /<option value="左">左<\/option><option value="右">右<\/option>/);
  assert.match(body, /<input type="number" class="form-input" id="ability-param-odd_one-count" step="1" min="1" max="5"/);
  assert.match(body, /ability-checks/);
  assert.match(body, /（可选）/);
  env.el("ability-param-odd_one-side").value = "右";
  env.el("ability-param-odd_one-count").value = "3";
  for (const id of ["N1", "N2", "N4"]) env.el(`ability-param-odd_one-players-${id}`).checked = id !== "N2";
  env.el("ability-param-odd_one-reason").value = "";
  await dialogOk(env);
  await dialogOk(env);
  assert.deepEqual(backend.seatCalls[0].body, { ability_id: "odd_one", params: { side: "右", count: 3, players: ["N1", "N4"] } });
});

// =====================================================================================================
// request cards
// =====================================================================================================
const wolfRequest = (over = {}) => F.makeRequest({ id: "r1", to: ["N3", "N6", "N10", "N12"], share_answers: true, seconds_left: 60, ...over });

test("a request card of the played seat is clickable while the game runs: the options are buttons, a click answers", async () => {
  const { env, backend } = await livePlayed("N3", { status: "running", requests: [wolfRequest()] }, { requests: [F.seatRequest("r1", { channel_id: "wolves", deadline_clock: 72.5 })] });
  env.app().state.clockSync = { clock: 12.5, at: 0, running: false };
  channelClick(env, "wolves");
  env.flushRaf();
  const card = feedHtml(env);
  assert.match(card, /<div class="req-action" data-req-action="r1">/);
  assert.match(card, /轮到你作答了/);
  for (const id of ["N1", "N4", "N5"]) assert.match(card, new RegExp(`data-req-answer="r1" data-value="${id}" aria-pressed="false"`));
  assert.doesNotMatch(card, /data-req-answer="r1"[^>]* disabled/);
  assert.match(card, /class="req-card req-open is-share has-action"/);

  env.el("feed-records-container").dispatch("click", { target: target({ "[data-req-answer]": { "data-req-answer": "r1", "data-value": "N4" } }) });
  await flush(env);
  assert.deepEqual(backend.seatCalls.map((c) => [c.kind, c.body]), [["respond", { request_id: "r1", value: "N4" }]]);
  assert.match(lastToast(env), /已提交：4号 David/);
});

test("an answered card marks the person's choice and may be changed until the request closes", async () => {
  const mine = F.seatRequest("r1", { channel_id: "wolves", answered: true, my_answer: { value: "N4", label: "4号 David", reason: "" } });
  const { env, backend } = await livePlayed("N3", { status: "running", requests: [wolfRequest({ answers: [F.answer("N3", "N4", "4号 David")], answered_by: ["N3"] })] }, { requests: [mine] });
  channelClick(env, "wolves");
  env.flushRaf();
  const card = feedHtml(env);
  assert.match(card, /你的选择：<strong>4号 David<\/strong><span class="req-mine-hint">（请求关闭前可以修改）/);
  assert.match(card, /class="req-opt selected" data-req-answer="r1" data-value="N4" aria-pressed="true"[^>]*>✓ 4号 David/);
  assert.match(card, /aria-pressed="false"[^>]*>1号 Alice/);
  env.el("feed-records-container").dispatch("click", { target: target({ "[data-req-answer]": { "data-req-answer": "r1", "data-value": "N5" } }) });
  await flush(env);
  assert.deepEqual(backend.seatCalls.map((c) => c.body), [{ request_id: "r1", value: "N5" }], "a change is just another answer");
  // the request closes: the buttons are gone, the result stays
  env.sse("request.closed", { request_id: "r1", reason: "unanimous", answered_by: ["N3", "N6", "N10", "N12"], defaulted: [], result: { reason: "unanimous", tally: { N5: 4 }, leaders: ["N5"], defaulted: [], missing: [] } });
  assert.doesNotMatch(feedHtml(env), /data-req-answer/);
});

test("a paused game freezes the card: the options are disabled and the card says why", async () => {
  const { env, backend } = await livePlayed("N3", { status: "paused", requests: [wolfRequest()] }, { requests: [F.seatRequest("r1", { channel_id: "wolves" })] });
  channelClick(env, "wolves");
  env.flushRaf();
  const card = feedHtml(env);
  assert.match(card, /data-req-answer="r1" data-value="N4" aria-pressed="false" disabled/);
  assert.match(card, /对局已暂停，继续后才能作答（请求的计时也已暂停）/);
  env.el("feed-records-container").dispatch("click", { target: target({ "[data-req-answer]": { "data-req-answer": "r1", "data-value": "N4" } }) });
  await flush(env);
  assert.equal(backend.seatCalls, undefined, "nothing is sent while the game is paused");
  assert.match(lastToast(env), /对局已暂停/);
  // idle / finished say so as well
  for (const [status, re] of [["idle", /对局还没有开始/], ["finished", /对局已结束/]]) {
    const other = await livePlayed("N3", { status, requests: [wolfRequest({ status: status === "finished" ? "open" : "open" })] }, { requests: [F.seatRequest("r1", { channel_id: "wolves" })] });
    channelClick(other.env, "wolves");
    other.env.flushRaf();
    assert.match(feedHtml(other.env), re, status);
  }
});

test("a request that is not for the seat, or is closed, has no buttons; the god view and a spectator never do", async () => {
  const forOthers = F.makeRequest({ id: "r2", to: ["N6", "N10"], share_answers: true });
  const { env } = await livePlayed("N3", { status: "running", requests: [forOthers] }, {});
  channelClick(env, "wolves");
  env.flushRaf();
  assert.doesNotMatch(feedHtml(env), /data-req-answer|req-action/);
  const spectator = await live({ status: "running", requests: [wolfRequest()] });
  channelClick(spectator.env, "wolves");
  spectator.env.flushRaf();
  assert.doesNotMatch(feedHtml(spectator.env), /data-req-answer|req-action/);
});

test("a text request: an input and a submit button; the typed text survives repaints; an answer can be changed", async () => {
  const request = F.makeRequest({ id: "r2", kind: "text", options: [], question: "请留下遗言", to: ["N3"], share_answers: false, auto_close: "all_answered", seconds_left: 45 });
  const { env, backend } = await livePlayed("N3", { status: "running", requests: [request] }, { requests: [F.seatRequest("r2", { kind: "text", max_chars: 300, channel_id: "dm-N3" })] });
  channelClick(env, "dm-N3");
  env.flushRaf();
  const card = feedHtml(env);
  assert.match(card, /<textarea class="req-text" rows="2" data-req-text="r2" data-draft-key="req:r2" data-initial="" aria-label="你的回答" placeholder="写下你的回答（最多 300 字）">/);
  assert.match(card, /data-req-submit="r2"[^>]*>提交</);
  // the person types (several input events) - repaints triggered by the world do not lose it
  const typeInto = (value) => env.el("feed-records-container").dispatch("input", { target: target({ "[data-req-text]": { "data-req-text": "r2" } }, { value }) });
  typeInto("我");
  typeInto("我是好人，请相信我。");
  env.sse("request.progress", { request_id: "r2", answered_by: [], total: 1 });
  assert.equal(env.app().composer()._state.reqDrafts.get("r2"), "我是好人，请相信我。");
  env.el("feed-records-container").dispatch("click", { target: target({ "[data-req-submit]": { "data-req-submit": "r2" } }) });
  await flush(env);
  assert.deepEqual(backend.seatCalls.map((c) => [c.kind, c.body]), [["respond", { request_id: "r2", value: "我是好人，请相信我。" }]]);
  // an empty answer is not sent
  env.app().composer()._state.reqDrafts.set("r2", "   ");
  env.el("feed-records-container").dispatch("click", { target: target({ "[data-req-submit]": { "data-req-submit": "r2" } }) });
  await flush(env);
  assert.equal(backend.seatCalls.length, 1);
  assert.match(lastToast(env), /先写下你的回答/);
  // an answered text request offers the change
  takeover(env, "N3", { requests: [F.seatRequest("r2", { kind: "text", max_chars: 300, channel_id: "dm-N3", answered: true, my_answer: { value: "我是好人，请相信我。", label: "我是好人，请相信我。", reason: "" } })] });
  assert.match(feedHtml(env), /你的回答：<strong>我是好人，请相信我。<\/strong>/);
  assert.match(feedHtml(env), /data-initial="我是好人，请相信我。"/);
  assert.match(feedHtml(env), />修改回答</);
});

test("the card's countdown follows the game clock; the answer part has no countdown of its own", async () => {
  const { env } = await livePlayed("N3", { status: "running", requests: [wolfRequest({ seconds_left: 60 })] }, { requests: [F.seatRequest("r1", { channel_id: "wolves" })] });
  env.app().state.clockSync = { clock: 100, at: 0, running: false };
  env.app().renderAll(true);
  channelClick(env, "wolves");
  env.flushRaf();
  assert.match(feedHtml(env), /data-deadline="/);
});

// =====================================================================================================
// the phone
// =====================================================================================================
test("phone: the takeover bar stays in the first row, the viewpoint bar goes into ⋯; the input box lives in the feed pane above the tab bar", async () => {
  const css = H.CSS_SRC.replace(/\r\n/g, "\n");
  // the takeover bar takes the place of the title: it stays in the first row (never moved into the ⋯ menu)
  assert.match(css, /html\[data-layout="phone"\]\[data-takeover="1"\] \.brand-group \{\s*display: none/);
  assert.match(css, /html\[data-layout="phone"\] \.takeover-bar \{[^}]*flex: 1 1 auto/);
  assert.match(css, /html\[data-layout="phone"\] \.composer \{[^}]*padding: 6px 10px 8px/);
  assert.match(css, /html\[data-layout="phone"\] \.composer-input \{[^}]*font-size: 16px/, "iOS does not zoom into 16 px fields");
  // the soft keyboard: the page is as tall as what is left, and only what is needed to write stays
  assert.match(css, /html\[data-layout="phone"\]\.kb-open body \{\s*height: var\(--vvh/);
  assert.match(css, /html\[data-layout="phone"\]\.kb-open \.tabbar,\s*html\[data-layout="phone"\]\.kb-open \.topbar-status,\s*html\[data-layout="phone"\]\.kb-open \.main-container\[data-phone-tab="feed"\] > \.board-section \{\s*display: none/);
  assert.match(css, /\.tabbar \{[^}]*padding-bottom: env\(safe-area-inset-bottom\)/, "the tab bar keeps the safe area");
  // the stylesheet shows the feed pane (and so its input box) on the feed tab only
  assert.match(css, /data-phone-tab="channels"\] > \.workspace-grid > \.feed-panel,/);
  // in the page the box is the last thing of the feed panel, after the list
  const panel = H.HTML_SRC.slice(H.HTML_SRC.indexOf('id="feed-panel"'));
  assert.ok(panel.indexOf('id="feed-list"') < panel.indexOf('id="composer"'));
  assert.ok(panel.indexOf('id="composer"') < panel.indexOf("</section>"));
});

test("phone: the soft keyboard is noticed from the visual viewport (the tab bar makes room, the page follows)", async () => {
  const listeners = {};
  const vv = {
    height: 800,
    addEventListener: (type, fn) => (listeners[type] = (listeners[type] || []).concat(fn)),
  };
  const backend = H.makeBackend({ session: F.makeSession(playedOver("N3", { status: "running" })) });
  const env = H.makeEnv({ fetchImpl: backend.fetch, innerWidth: 390, innerHeight: 800, userAgent: "iPhone Mobile" });
  env.sandbox.visualViewport = vv;
  env.sandbox.scrollTo = () => {};
  env.run();
  await settle();
  env.flushRaf();
  const html_ = env.document.documentElement;
  assert.equal(html_.classList.contains("kb-open"), false);
  vv.height = 440; // the keyboard takes 360 px
  listeners.resize.forEach((fn) => fn());
  assert.equal(html_.classList.contains("kb-open"), true);
  assert.equal(html_.style["--vvh"], undefined, "(style is a plain object in the fake DOM)");
  vv.height = 800;
  listeners.resize.forEach((fn) => fn());
  assert.equal(html_.classList.contains("kb-open"), false);
  const composer = env.app().composer();
  assert.equal(composer.keyboardOpen(800, 440), true);
  assert.equal(composer.keyboardOpen(800, 760), false, "a browser bar is not a keyboard");
});

test("a touch screen has no Shift+Enter: the placeholder is short there, the full hint stays on a desktop", async () => {
  const session = F.makeSession({ event_id: 500, ...playedOver("N3", { status: "running" }, writable()) });
  const desk = await bootLive({ session });
  desk.env.sse("snapshot", { session }, 500);
  assert.match(attr(desk.env, "composer-input", "placeholder"), /在「公共频道」发言（Enter 发送，Shift\+Enter 换行）/);
  const phone = await bootLive({ session }, { innerWidth: 390, innerHeight: 800, userAgent: "iPhone Mobile", coarse: true, maxTouchPoints: 5 });
  phone.env.sse("snapshot", { session }, 500);
  assert.equal(phone.env.document.documentElement.getAttribute("data-input"), "touch");
  assert.equal(attr(phone.env, "composer-input", "placeholder"), "在「公共频道」发言");
});

// =====================================================================================================
// a replay is not a takeover
// =====================================================================================================
test("a saved game on screen (a replay) cannot be taken over, and the person's input box is not part of it", async () => {
  const { env } = await livePlayed("N3", { status: "paused" }, writable());
  assert.equal(env.el("composer").hidden, false);
  env.app().state.replay = { live: env.app().state.session, installing: false };
  env.app().renderAll(true);
  assert.equal(env.el("btn-takeover").disabled, true);
  assert.match(attr(env, "btn-takeover", "title"), /重放中不能接管/);
  env.app().state.replay = null;
});

// -------------------------------------------------------------------------------------------------
H.runTests(tests);
