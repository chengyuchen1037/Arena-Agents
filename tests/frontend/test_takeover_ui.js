"use strict";
// Playing a seat (v0.13, F1-F19; v0.14, H27) in the page: the viewpoint label and its menu (全局视角 / 以 X 的视角观看 / 接管 X / 退出接管),
// the confirmations, the takeover bar, the input box of every channel, the floor / ability buttons, the answers of request
// cards and the "待你操作" marks - static/js/app.js + takeover.js + composer.js against the fake DOM and the fake
// backend (see harness.js).  The pure view model: test_takeover_view.js.  (v0.14, H52: the reshuffle reminder is gone with the re-deal.)
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

/** A game that has not started: nobody is dealt yet (H52) - `dealt` is false and no seat has a tag. */
async function liveIdle(over = {}, overrides = {}, envOpts = {}) {
  const session = F.undealt(F.makeSession({ event_id: 500, status: "idle", ...over }));
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
/** An item of the viewpoint menu (opened first, like a user does): `attrName` = data-vp-view | data-vp-take | data-vp-release. */
const menuItem = (env, attrName, value = "") => {
  if (env.el("viewpoint-menu").hidden) env.el("btn-viewpoint").dispatch("click");
  env.el("viewpoint-menu").dispatch("click", { target: target({ [`[${attrName}]`]: { [attrName]: value } }) });
};
const takeSeat = (env, seat) => menuItem(env, "data-vp-take", seat);
const viewAs = (env, id) => menuItem(env, "data-vp-view", id);
/** The opening tag of the button `attrName="value"` of the (open) menu (`value` null: a button whose attribute has no value, like data-vp-release). */
const menuButton = (env, attrName, value) => {
  if (env.el("viewpoint-menu").hidden) env.el("btn-viewpoint").dispatch("click");
  const re = value === null ? new RegExp(`<button[^>]*${attrName}[^>]*>`) : new RegExp(`<button[^>]*${attrName}="${value}"[^>]*>`);
  return (html(env, "viewpoint-menu").match(re) || [""])[0];
};
const isDisabled = (tag) => / disabled(?:[ >=])/.test(tag);
const chipText = (env) => env.el("viewpoint-text").textContent;
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
// the viewpoint label and its menu (v0.14, H27)
// =====================================================================================================
test("the menu's 接管 buttons: only while the game has not started or is paused; each says why on hover (F2)", async () => {
  for (const [status, enabled] of [["idle", true], ["paused", true], ["running", false], ["finished", false]]) {
    const { env } = await live({ status });
    const take = menuButton(env, "data-vp-take", "N3");
    assert.equal(isDisabled(take), !enabled, status);
    if (!enabled) assert.match(take, status === "running" ? /对局进行中不能接管：请先点「暂停」/ : /对局已结束/);
    else assert.match(take, /接管 3 · Carol/);
    assert.equal(env.el("viewpoint-bar").hidden, false);
    assert.equal(env.el("takeover-bar").hidden, true);
    assert.equal(chipText(env), "全局视角");
  }
});

test("the status of the game decides at once: a running game disables every 接管 of an open menu - 观看 still works", async () => {
  const { env } = await live({ status: "paused" });
  assert.equal(isDisabled(menuButton(env, "data-vp-take", "N3")), false);
  env.sse("game.status", { status: "running", reason: "continued", winner: null });
  assert.equal(env.el("viewpoint-menu").hidden, false, "the menu stays open");
  assert.equal(isDisabled(menuButton(env, "data-vp-take", "N3")), true);
  assert.equal(isDisabled(menuButton(env, "data-vp-view", "N3")), false, "以 X 的视角观看 works while the game runs");
  assert.match(html(env, "viewpoint-menu"), /对局进行中不能接管：请先点「暂停」/, "the menu says why once, in a sentence (a phone has no hover)");
});

test("接管 asks first (what happens), does nothing on cancel, and POSTs the chosen seat on confirm", async () => {
  const { env, backend } = await live({ status: "paused" });
  backend.takeoverResult = F.makePlayedSession("N3", { event_id: 501, status: "paused" });
  takeSeat(env, "N3");
  await flush(env);
  assert.equal(env.el("viewpoint-menu").hidden, true, "choosing an item closes the menu");
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

  takeSeat(env, "N3");
  await flush(env);
  await dialogOk(env);
  assert.deepEqual(backend.takeoverCalls, [{ seat: "N3" }]);
  assert.equal(env.app().state.session.takeover.seat, "N3");
  assert.equal(env.app().state.viewpoint, "N3", "the viewpoint is locked to the seat");
  assert.equal(chipText(env), "Carol视角（接管）");
  assert.match(lastToast(env), /已接管 3 · Carol/);
});

test("a takeover before the start: the identity is dealt when the game starts (nothing to re-deal - H52); the seat cannot be moved meanwhile", async () => {
  const { env, backend } = await liveIdle();
  backend.takeoverResult = F.makePlayedSession("N3", { event_id: 501, status: "idle", dealt: false });
  takeSeat(env, "N3");
  await flush(env);
  const body = text(html(env, "dialog-body"));
  assert.match(body, /对局还没有开始：身份在点击「开始」时才发牌，开始后你会在自己的座位上看到它/);
  assert.match(body, /接管期间不能调整座位/);
  assert.doesNotMatch(body, /重新发牌/);
  await dialogOk(env);
  assert.equal(env.app().state.session.takeover.seat, "N3");
  assert.ok(!backend.calls.some((c) => /shuffle/.test(c.url)), "no re-deal request exists any more");
  assert.equal(html(env, "participants-grid").includes("pcard-tag"), false, "the played seat sees no identity before the deal either");
});

test("a refused takeover shows the server's reason and re-reads the state", async () => {
  const { env, backend } = await live({ status: "paused" });
  backend.failures["POST /api/takeover"] = [409, "对局进行中不能接管：请先暂停对局。"];
  takeSeat(env, "N3");
  await flush(env);
  await dialogOk(env);
  assert.match(lastToast(env), /对局进行中不能接管/);
  assert.equal(env.app().state.session.takeover.seat, null);
  assert.ok(backend.calls.filter((c) => c.method === "GET" && c.url.endsWith("/api/state")).length >= 2, "the state was fetched again");
  assert.equal(isDisabled(menuButton(env, "data-vp-take", "N3")), false, "the button is usable again");
});

test("the game may start while the confirmation is open: nothing is sent", async () => {
  const { env, backend } = await live({ status: "paused" });
  takeSeat(env, "N3");
  await flush(env);
  env.sse("game.status", { status: "running", reason: "continued", winner: null }); // somebody pressed 继续 meanwhile
  await dialogOk(env);
  assert.equal(backend.takeoverCalls, undefined);
  assert.match(lastToast(env), /对局进行中不能接管/);
});

// =====================================================================================================
// the takeover bar and the lock
// =====================================================================================================
test("a played seat: the bar says who, the label says 🎮 Carol视角（接管） and is locked, the viewpoint is the seat's, the card says 人类操作中", async () => {
  const { env } = await livePlayed("N3");
  assert.equal(env.el("takeover-bar").hidden, false);
  assert.equal(env.el("viewpoint-bar").hidden, false, "the label stays (v0.14): it carries the 🎮 and the lock");
  assert.deepEqual([chipText(env), env.el("viewpoint-icon").textContent], ["Carol视角（接管）", "🎮"]);
  assert.equal(attr(env, "btn-viewpoint", "data-kind"), "played");
  assert.equal(env.el("btn-viewpoint").classList.contains("is-played"), true);
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

test("the viewpoint stays locked: every viewpoint of the menu is off (only 退出接管 works) and the detail panel's 以 X 的视角观看 does nothing", async () => {
  const { env } = await livePlayed("N3");
  assert.match(attr(env, "btn-viewpoint", "title"), /视角已锁定为这个座位/);
  for (const id of ["god", "N1", "N5"]) {
    assert.equal(isDisabled(menuButton(env, "data-vp-view", id)), true, `view ${id}`);
    assert.match(menuButton(env, "data-vp-view", id), /接管期间视角已锁定/);
  }
  for (const id of ["N1", "N3", "N5"]) assert.equal(isDisabled(menuButton(env, "data-vp-take", id)), true, `take ${id}`);
  assert.match(html(env, "viewpoint-menu"), /data-vp-release[^>]*>退出接管（3 · Carol）</);
  viewAs(env, "N5"); // a click that gets through anyway
  assert.equal(env.app().state.viewpoint, "N3");
  assert.match(lastToast(env), /接管期间视角已锁定/);
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
  assert.equal(chipText(env), "Alice视角");
  takeover(env, "N3");
  assert.equal(env.app().state.viewpoint, "N3");
  assert.equal(env.el("takeover-bar").hidden, false);
  assert.equal(chipText(env), "Carol视角（接管）");
  assert.equal(env.storage.get("arena_viewpoint_v1"), "N1", "the user's own choice is not overwritten by the lock");
  env.sse("takeover.updated", { takeover: { seat: null } });
  assert.equal(env.app().state.viewpoint, "N1", "back to what the user had");
  assert.equal(env.el("takeover-bar").hidden, true);
  assert.equal(chipText(env), "Alice视角");
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
  assert.equal(chipText(env), "全局视角", "the label is the global view again");
  assert.equal(env.app().state.viewpoint, "god");
  assert.match(lastToast(env), /已退出接管：3 · Carol 交还给 agent/);
});

test("退出接管 is also in the menu of the label - the same question, the same request (H27)", async () => {
  const { env, backend } = await livePlayed("N3", { status: "paused" });
  menuItem(env, "data-vp-release");
  await flush(env);
  assert.equal(env.el("viewpoint-menu").hidden, true, "choosing an item closes the menu");
  assert.equal(env.el("dialog-title").textContent, "退出接管 3 · Carol？");
  await dialogOk(env);
  assert.equal(backend.releaseCalls, 1);
  assert.equal(chipText(env), "全局视角");
  // while the game runs the item is greyed and says why
  const running = await livePlayed("N3", { status: "running" });
  const tag = menuButton(running.env, "data-vp-release", null);
  assert.equal(isDisabled(tag), true);
  assert.match(tag, /对局进行中不能退出接管：请先点「暂停」/);
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
// there is no re-deal any more (v0.14, H52): the cards are dealt when the game starts
// =====================================================================================================
test("no re-deal anywhere: not in the page, not in the scripts, not in the styles, not in what the app asks the server", async () => {
  for (const gone of ["takeover-reminder", "btn-shuffle-identities", "shuffle-identities", "重新发牌", "reshuffle"]) {
    assert.ok(!H.HTML_SRC.includes(gone), `index.html still has ${gone}`);
    for (const [name, src] of Object.entries(H.SRC)) assert.ok(!src.includes(gone), `${name}.js still has ${gone}`);
  }
  assert.ok(!/\.takeover-reminder/.test(H.CSS_SRC), "no rule for the reminder");
  // a seat taken over before the start: the whole round trip asks for no re-deal
  const { env, backend } = await liveIdle();
  backend.takeoverResult = F.makePlayedSession("N3", { event_id: 501, status: "idle", dealt: false });
  takeSeat(env, "N3");
  await flush(env);
  await dialogOk(env);
  assert.ok(!backend.calls.some((c) => /shuffle/.test(c.url)));
  assert.equal(env.app().state.session.takeover.seat, "N3");
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
  // over the limit (v0.14, H16) the counter turns red and the box cannot be sent: a speech is refused whole, never cut (see test_v014_play_ui.js)
  type(env, "字".repeat(130));
  assert.equal(env.el("composer-count").textContent, "130/120");
  assert.equal(env.el("composer-count").classList.contains("over"), true);
  assert.match(attr(env, "composer-count", "title"), /这条发言 130 个字，超过了单条上限 120 字：请删减 10 字后再发送/);
  assert.equal(env.el("btn-composer-send").disabled, true);
});

test("the drafts belong to the seat: another game or another seat starts with empty boxes", async () => {
  const { env } = await livePlayed("N3", { status: "running" }, writable());
  type(env, "写到一半");
  env.sse("takeover.updated", { takeover: { seat: null } });
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

test("a refused send keeps the draft and says why; a message is never cut (v0.14, H16: TOO_LONG is a refusal, see test_v014_play_ui.js)", async () => {
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
  backend.seatReply = { speak: { accepted: true, seq: 9, code: null, message: "已发送。" } };
  enter(env);
  await flush(env);
  assert.equal(env.el("composer-input").value, "");
  assert.ok(!env.toasts.some((t) => /截掉/.test(t.textContent)), "nothing is cut, so nothing says it was");
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

// v0.13.1 (G45): a click on an option only PICKS it; the card's own button (「提交」 / 「修改回答」) sends the pick
const clickOption = (env, id, value) =>
  env.el("feed-records-container").dispatch("click", { target: target({ "[data-req-answer]": { "data-req-answer": id, "data-value": value } }) });
const clickConfirm = (env, id) =>
  env.el("feed-records-container").dispatch("click", { target: target({ "[data-req-confirm]": { "data-req-confirm": id } }) });

test("a request card of the played seat is clickable while the game runs: a click on an option only PICKS it, 「提交」 sends the pick (G45)", async () => {
  const { env, backend } = await livePlayed("N3", { status: "running", requests: [wolfRequest()] }, { requests: [F.seatRequest("r1", { channel_id: "wolves", deadline_clock: 72.5 })] });
  env.app().state.clockSync = { clock: 12.5, at: 0, running: false };
  channelClick(env, "wolves");
  env.flushRaf();
  let card = feedHtml(env);
  assert.match(card, /<div class="req-action" data-req-action="r1">/);
  assert.match(card, /轮到你作答了/);
  for (const id of ["N1", "N4", "N5"]) assert.match(card, new RegExp(`data-req-answer="r1" data-value="${id}" aria-pressed="false"`));
  assert.doesNotMatch(card, /data-req-answer="r1"[^>]* disabled/);
  assert.match(card, /class="req-card req-open is-share has-action"/);
  // nothing picked yet: 「提交」 is on the card, but it cannot be pressed
  assert.match(card, /data-req-confirm="r1" title="先选一个选项，再提交" disabled>提交</);
  assert.doesNotMatch(card, /req-pick/);

  // a click on an option only picks it: highlighted, "未提交", and NOTHING is sent
  clickOption(env, "r1", "N4");
  await flush(env);
  assert.equal(backend.seatCalls, undefined, "a pick is not a vote");
  card = feedHtml(env);
  assert.match(card, /class="req-opt picked" data-req-answer="r1" data-value="N4" aria-pressed="true"/);
  assert.match(card, /已选：<strong>4号 David<\/strong> · 未提交/);
  assert.match(card, /data-req-confirm="r1" title=""\s*>提交</, "pressable now");
  assert.doesNotMatch(card, /data-req-confirm="r1"[^>]* disabled/);

  // 「提交」 sends exactly the pick - once - and the pick is gone afterwards
  clickConfirm(env, "r1");
  await flush(env);
  assert.deepEqual(backend.seatCalls.map((c) => [c.kind, c.body]), [["respond", { request_id: "r1", value: "N4" }]]);
  assert.match(lastToast(env), /已提交：4号 David/);
  assert.equal(env.app().composer()._state.picked.size, 0);
});

test("an answered card marks the person's choice; changing it is a new pick + 「修改回答」, and the old choice stays marked until the change is sent (G45)", async () => {
  const mine = F.seatRequest("r1", { channel_id: "wolves", answered: true, my_answer: { value: "N4", label: "4号 David", reason: "" } });
  const { env, backend } = await livePlayed("N3", { status: "running", requests: [wolfRequest({ answers: [F.answer("N3", "N4", "4号 David")], answered_by: ["N3"] })] }, { requests: [mine] });
  channelClick(env, "wolves");
  env.flushRaf();
  let card = feedHtml(env);
  assert.match(card, /你的选择：<strong>4号 David<\/strong><span class="req-mine-hint">（请求关闭前可以修改）/);
  assert.match(card, /class="req-opt selected" data-req-answer="r1" data-value="N4" aria-pressed="true"[^>]*>✓ 4号 David/);
  assert.match(card, /aria-pressed="false"[^>]*>1号 Alice/);
  assert.match(card, /data-req-confirm="r1" title="先选一个选项，再提交" disabled>修改回答</, "nothing new to send yet");
  // a click on another option only picks it: the submitted choice keeps its ✓, the note says both, nothing is sent
  clickOption(env, "r1", "N5");
  await flush(env);
  assert.equal(backend.seatCalls, undefined, "a change is not sent by the click");
  card = feedHtml(env);
  assert.match(card, /class="req-opt selected" data-req-answer="r1" data-value="N4" aria-pressed="false"[^>]*>✓ 4号 David/);
  assert.match(card, /class="req-opt picked" data-req-answer="r1" data-value="N5" aria-pressed="true"/);
  assert.match(card, /已提交：4号 David · 改选：<strong>5号 Eve<\/strong>（未提交）/);
  assert.match(card, /data-req-confirm="r1" title=""\s*>修改回答</);
  // picking the submitted choice again is "no change": nothing to send
  clickOption(env, "r1", "N4");
  await flush(env);
  assert.doesNotMatch(feedHtml(env), /req-pick|req-opt picked/);
  assert.match(feedHtml(env), /data-req-confirm="r1"[^>]* disabled>修改回答</);
  clickConfirm(env, "r1");
  await flush(env);
  assert.equal(backend.seatCalls, undefined);
  assert.match(lastToast(env), /已经提交了这个选择/);
  // the change: pick another, 「修改回答」 sends it
  clickOption(env, "r1", "N5");
  await flush(env);
  clickConfirm(env, "r1");
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
  assert.match(card, /data-req-confirm="r1" title="对局已暂停[^"]*" disabled>提交</, "the button says why it is grey");
  clickOption(env, "r1", "N4");
  await flush(env);
  assert.equal(backend.seatCalls, undefined, "nothing is sent while the game is paused");
  assert.match(lastToast(env), /对局已暂停/);
  assert.equal(env.app().composer()._state.picked.size, 0, "a frozen card cannot even be picked on");
  clickConfirm(env, "r1");
  await flush(env);
  assert.equal(backend.seatCalls, undefined);
  // idle / finished say so as well
  for (const [status, re] of [["idle", /对局还没有开始/], ["finished", /对局已结束/]]) {
    const other = await livePlayed("N3", { status, requests: [wolfRequest({ status: status === "finished" ? "open" : "open" })] }, { requests: [F.seatRequest("r1", { channel_id: "wolves" })] });
    channelClick(other.env, "wolves");
    other.env.flushRaf();
    assert.match(feedHtml(other.env), re, status);
  }
});

test("a pick is only a pick: another option moves it, the same option takes it back, it survives repaints and a channel switch, and it goes with its request (G45)", async () => {
  const { env, backend } = await livePlayed("N3", { status: "running", requests: [wolfRequest()] }, { requests: [F.seatRequest("r1", { channel_id: "wolves" })] });
  channelClick(env, "wolves");
  env.flushRaf();
  clickOption(env, "r1", "N4");
  await flush(env);
  assert.match(feedHtml(env), /class="req-opt picked" data-req-answer="r1" data-value="N4"/);
  clickOption(env, "r1", "N5"); // another option moves the pick
  await flush(env);
  let card = feedHtml(env);
  assert.match(card, /class="req-opt picked" data-req-answer="r1" data-value="N5"/);
  assert.doesNotMatch(card, /req-opt picked" data-req-answer="r1" data-value="N4"/);
  // the world moves on (the card is repainted) and the person looks at another channel: the pick stays
  env.sse("request.progress", { request_id: "r1", answered_by: ["N6"], total: 4 });
  channelClick(env, "public");
  env.flushRaf();
  channelClick(env, "wolves");
  env.flushRaf();
  assert.match(feedHtml(env), /class="req-opt picked" data-req-answer="r1" data-value="N5"/);
  // the picked option again takes the pick back; the button is grey again
  clickOption(env, "r1", "N5");
  await flush(env);
  card = feedHtml(env);
  assert.doesNotMatch(card, /req-opt picked|req-pick/);
  assert.match(card, /data-req-confirm="r1"[^>]* disabled>提交</);
  // pressing the button without a pick sends nothing
  clickConfirm(env, "r1");
  await flush(env);
  assert.equal(backend.seatCalls, undefined);
  assert.match(lastToast(env), /先选一个选项/);
  // a pick is dropped when its request closes (the buttons go, the result stays)
  clickOption(env, "r1", "N4");
  await flush(env);
  assert.equal(env.app().composer()._state.picked.get("r1"), "N4");
  env.sse("request.closed", { request_id: "r1", reason: "all_answered", answered_by: ["N3", "N6", "N10", "N12"], defaulted: [], result: { reason: "all_answered", tally: { N5: 4 }, leaders: ["N5"], defaulted: [], missing: [] } });
  env.app().composer().render();
  assert.equal(env.app().composer()._state.picked.size, 0);
  assert.doesNotMatch(feedHtml(env), /data-req-answer|data-req-confirm/);
  assert.equal(backend.seatCalls, undefined, "a pick that was never submitted is never sent - the engine's default answer counts");
});

test("a refused answer keeps the pick (the person presses again); the same pick cannot be sent twice at once (G45)", async () => {
  const { env, backend } = await livePlayed("N3", { status: "running", requests: [wolfRequest()] }, { requests: [F.seatRequest("r1", { channel_id: "wolves" })] });
  channelClick(env, "wolves");
  env.flushRaf();
  backend.seatReply = { respond: { accepted: false, seq: null, code: "REQUEST_CLOSED", message: "这个请求已经关闭。" } };
  clickOption(env, "r1", "N4");
  await flush(env);
  clickConfirm(env, "r1");
  await flush(env);
  assert.match(lastToast(env), /这个请求已经关闭/);
  assert.equal(env.app().composer()._state.picked.get("r1"), "N4", "the pick is still there");
  assert.match(feedHtml(env), /已选：<strong>4号 David<\/strong> · 未提交/);
  // the server takes it this time
  backend.seatReply = {};
  clickConfirm(env, "r1");
  clickConfirm(env, "r1"); // a second press while the first is on its way
  await flush(env);
  assert.equal(backend.seatCalls.length, 2, "one refused + one accepted: the double press sent only one more");
  assert.equal(env.app().composer()._state.picked.size, 0);
});

test("the note of a pick that was not submitted is flagged 15 game seconds before the deadline (the 1 Hz tick sets it) (G45)", async () => {
  const { env } = await livePlayed("N3", { status: "running", requests: [wolfRequest({ seconds_left: 60 })] }, { requests: [F.seatRequest("r1", { channel_id: "wolves" })] });
  env.app().state.clockSync = { clock: 100, at: 0, running: false };
  env.app().renderAll(true);
  channelClick(env, "wolves");
  env.flushRaf();
  clickOption(env, "r1", "N4");
  await flush(env);
  const card = feedHtml(env);
  const deadline = Number(/data-deadline="([0-9.]+)"/.exec(card)[1]);
  assert.ok(Number.isFinite(deadline) && deadline > 15, "the card has a deadline");
  assert.match(card, new RegExp(`class="req-pick" data-req-pick="r1" data-warn-at="${deadline - 15}"`));
  assert.match(card, /<span class="req-pick-warn">⚠ 快到时间了，点「提交」才算数；到点按默认答案处理<\/span>/);
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
// request answers on their way (G45): what the page holds while an answer has not come back
// =====================================================================================================
// A press sends the answer; the reply comes later.  Meanwhile the person may pick again or type on, the request may
// close, the game on screen may change (a request id comes back in every game: "r1" again).  The reply releases its
// own slot (game + seat + request id) and drops only what it SENT - and only in the game it was sent for.

/**
 * Keep the REPLIES of POST api/seat/respond on the wire: the fake server has the answer (`backend.seatCalls`) but the
 * page does not hear back until the test lets it.  `held[i].release()` lets the server's own reply through,
 * `release(receipt)` a different one (the answer was refused after all), `fail(status, detail)` an HTTP error.  `held`
 * lists the answers on their way, in the order they were sent.
 */
function holdAnswers({ env, backend }) {
  const held = [];
  const original = backend.fetch;
  backend.fetch = async (url, init) => {
    const res = await original(url, init);
    if (!String(url).endsWith("/api/seat/respond")) return res;
    const late = await new Promise((release) =>
      held.push({ body: JSON.parse(init.body), release, fail: (status, detail) => release({ http: [status, detail] }) })
    );
    if (late === undefined) return res;
    return late.http ? H.resp(late.http[0], { detail: late.http[1] }) : H.resp(200, late);
  };
  env.sandbox.fetch = backend.fetch;
  return held;
}

/** The snapshot of ANOTHER game (a new session id) in which `seat` is played; its requests start again at "r1". */
const gameOf = (id, seat, over = {}, takeoverOver = {}) =>
  F.makeSession({ session_id: id, event_id: 700, ...playedOver(seat, { status: "running", ...over }, takeoverOver) });
/** The slots of the answers on their way ("<game>|<seat>|<request id>"). */
const slots = (env) => [...env.app().composer()._state.ops.keys()].sort();
const sentBodies = (backend) => (backend.seatCalls || []).map((c) => c.body);
const wolfSeat = (id = "r1", over = {}) => F.seatRequest(id, { channel_id: "wolves", ...over });
const mineAnswered = (id, value, label, over = {}) => wolfSeat(id, { answered: true, my_answer: { value, label, reason: "" }, ...over });
const closedPayload = (id) => ({
  request_id: id,
  reason: "all_answered",
  answered_by: ["N3", "N6", "N10", "N12"],
  defaulted: [],
  result: { reason: "all_answered", tally: { N5: 4 }, leaders: ["N5"], defaulted: [], missing: [] },
});
const typeIntoCard = (env, id, value) =>
  env.el("feed-records-container").dispatch("input", { target: target({ "[data-req-text]": { "data-req-text": id } }, { value }) });
const pressSubmit = (env, id) => env.el("feed-records-container").dispatch("click", { target: target({ "[data-req-submit]": { "data-req-submit": id } }) });
/** The wolves' request (a choice of N1 / N4 / N5 in the wolves' channel) of a played N3, the wolves' channel open. */
async function wolvesGame(over = {}, extraRequests = []) {
  const ctx = await livePlayed(
    "N3",
    { status: "running", requests: [wolfRequest(), ...extraRequests.map((r) => wolfRequest({ id: r }))], ...over },
    { requests: [wolfSeat(), ...extraRequests.map((r) => wolfSeat(r))] }
  );
  channelClick(ctx.env, "wolves");
  ctx.env.flushRaf();
  return { ...ctx, composer: ctx.env.app().composer(), held: holdAnswers(ctx) };
}

test("G45 race A: a pick made while the answer before it is on its way survives that answer; the button says 提交中…, a second press sends nothing", async () => {
  const { env, backend, composer, held } = await wolvesGame();

  // N4 is picked and sent: the answer is on its way
  clickOption(env, "r1", "N4");
  await flush(env);
  clickConfirm(env, "r1");
  await flush(env);
  assert.deepEqual(sentBodies(backend), [{ request_id: "r1", value: "N4" }]);
  assert.equal(held.length, 1);
  assert.deepEqual(slots(env), ["s-test|N3|r1"]);
  let card = feedHtml(env);
  assert.match(card, /data-req-confirm="r1" title="上一次提交还没有完成，完成后可以再提交新的选择" disabled>提交中…</);
  assert.doesNotMatch(card, /data-req-answer="r1"[^>]* disabled/, "the options stay usable");
  assert.doesNotMatch(card, /req-pick/, "the pick that is being sent is not an 'unsubmitted' one");

  // meanwhile the person picks N5: a newer wish, with a note of its own; the button is still grey (one answer at a time)
  clickOption(env, "r1", "N5");
  await flush(env);
  assert.equal(composer._state.picked.get("r1"), "N5");
  card = feedHtml(env);
  assert.match(card, /class="req-opt picked" data-req-answer="r1" data-value="N5" aria-pressed="true"/);
  assert.match(card, /已选：<strong>5号 Eve<\/strong> · 未提交/);
  assert.match(card, /data-req-confirm="r1"[^>]* disabled>提交中…</);
  // a second press - a keyboard, another tab, a script - sends nothing: neither N4 again nor N5
  assert.equal(await composer.confirmPick("r1"), false);
  assert.equal(await composer.respond("r1", "N5"), false);
  clickConfirm(env, "r1");
  await flush(env);
  assert.equal(held.length, 1);
  assert.equal(backend.seatCalls.length, 1);
  // a reconnect (the snapshot of the same game) changes neither the slot nor the pick
  env.sse("snapshot", { session: F.makeSession({ session_id: "s-test", event_id: 520, ...playedOver("N3", { status: "running", requests: [wolfRequest()] }, { requests: [wolfSeat()] }) }) }, 520);
  assert.deepEqual(slots(env), ["s-test|N3|r1"]);
  assert.equal(composer._state.picked.get("r1"), "N5");
  assert.match(feedHtml(env), /data-req-confirm="r1"[^>]* disabled>提交中…</);

  // N4 is accepted (the server tells the seat's state first, as it does)
  takeover(env, "N3", { requests: [mineAnswered("r1", "N4", "4号 David")] });
  held[0].release();
  await flush(env);
  assert.match(lastToast(env), /已提交：4号 David/);
  assert.equal(composer._state.picked.get("r1"), "N5", "the newer pick is not the answer's to take away");
  assert.deepEqual(slots(env), []);
  card = feedHtml(env);
  assert.match(card, /已提交：4号 David · 改选：<strong>5号 Eve<\/strong>（未提交）/);
  assert.match(card, /data-req-confirm="r1" title=""\s*>修改回答</);
  // the change goes out as the next answer; when it is accepted the pick is gone
  clickConfirm(env, "r1");
  await flush(env);
  assert.deepEqual(sentBodies(backend), [{ request_id: "r1", value: "N4" }, { request_id: "r1", value: "N5" }]);
  held[1].release();
  await flush(env);
  assert.equal(composer._state.picked.size, 0);
  assert.deepEqual(slots(env), []);
});

test("G45 race A (the other order): the answer comes back before the seat's state does - the newer pick is offered as a new answer until it says otherwise", async () => {
  const { env, backend, composer, held } = await wolvesGame();
  clickOption(env, "r1", "N4");
  await flush(env);
  clickConfirm(env, "r1");
  await flush(env);
  clickOption(env, "r1", "N5");
  await flush(env);
  held[0].release();
  await flush(env);
  assert.equal(composer._state.picked.get("r1"), "N5");
  let card = feedHtml(env);
  assert.match(card, /已选：<strong>5号 Eve<\/strong> · 未提交/);
  assert.match(card, /data-req-confirm="r1" title=""\s*>提交</, "free again; the seat's state does not know the first answer yet");
  // the seat's state arrives: now it is a change
  takeover(env, "N3", { requests: [mineAnswered("r1", "N4", "4号 David")] });
  card = feedHtml(env);
  assert.match(card, /已提交：4号 David · 改选：<strong>5号 Eve<\/strong>（未提交）/);
  assert.match(card, /data-req-confirm="r1" title=""\s*>修改回答</);
  assert.equal(composer._state.picked.get("r1"), "N5");
  assert.equal(backend.seatCalls.length, 1);
});

test("a refused answer, or one that failed on the way, keeps the picks and frees the slot - a newer pick included (G45)", async () => {
  const { env, backend, composer, held } = await wolvesGame();
  // N4 goes out, N5 is picked meanwhile; the server refuses N4
  clickOption(env, "r1", "N4");
  await flush(env);
  clickConfirm(env, "r1");
  await flush(env);
  clickOption(env, "r1", "N5");
  await flush(env);
  held[0].release({ accepted: false, seq: null, code: "REQUEST_CLOSED", message: "这个请求已经关闭。" });
  await flush(env);
  assert.match(lastToast(env), /这个请求已经关闭/);
  assert.equal(composer._state.picked.get("r1"), "N5", "the picks are the person's, not the answer's");
  assert.deepEqual(slots(env), []);
  assert.match(feedHtml(env), /data-req-confirm="r1" title=""\s*>提交</, "free: the person may press again");

  // N5 goes out and N1 is picked meanwhile; the reply is an HTTP error (the answer may have reached the server or not)
  clickConfirm(env, "r1");
  await flush(env);
  assert.deepEqual(sentBodies(backend), [{ request_id: "r1", value: "N4" }, { request_id: "r1", value: "N5" }]);
  clickOption(env, "r1", "N1");
  await flush(env);
  held[1].fail(503, "服务暂时不可用");
  await flush(env);
  assert.match(lastToast(env), /服务暂时不可用/);
  assert.equal(composer._state.picked.get("r1"), "N1");
  assert.deepEqual(slots(env), []);
  // N1 goes out and is accepted: only now is the pick gone
  clickConfirm(env, "r1");
  await flush(env);
  assert.deepEqual(sentBodies(backend).slice(2), [{ request_id: "r1", value: "N1" }]);
  held[2].release();
  await flush(env);
  assert.equal(composer._state.picked.size, 0);
  assert.deepEqual(slots(env), []);
});

test("G45 race B: the answer of the game before cannot touch the card of a new game whose request has the same id", async () => {
  const { env, backend, composer, held } = await wolvesGame({ session_id: "g1" });

  // game 1: N4 is on its way
  clickOption(env, "r1", "N4");
  await flush(env);
  clickConfirm(env, "r1");
  await flush(env);
  assert.deepEqual(slots(env), ["g1|N3|r1"]);

  // the game on screen changes: a new one, whose first request is "r1" again
  env.sse("snapshot", { session: gameOf("g2", "N3", { requests: [wolfRequest()] }, { requests: [wolfSeat()] }) }, 700);
  channelClick(env, "wolves");
  env.flushRaf();
  assert.equal(env.app().state.session.session_id, "g2");
  assert.equal(composer._state.picked.size, 0, "game 1's pick is gone");
  let card = feedHtml(env);
  assert.match(card, /data-req-confirm="r1" title="先选一个选项，再提交" disabled>提交</);
  assert.doesNotMatch(card, /提交中/, "game 2's card is not 'on its way' because of game 1's answer");

  // game 2: its first answer is not held up by game 1's (and goes out) ...
  clickOption(env, "r1", "N5");
  await flush(env);
  clickConfirm(env, "r1");
  await flush(env);
  assert.deepEqual(sentBodies(backend), [{ request_id: "r1", value: "N4" }, { request_id: "r1", value: "N5" }]);
  assert.deepEqual(slots(env), ["g1|N3|r1", "g2|N3|r1"]);
  // ... and the person picks again meanwhile
  clickOption(env, "r1", "N1");
  await flush(env);
  assert.equal(composer._state.picked.get("r1"), "N1");

  // game 1's answer comes back, accepted: it changes nothing of game 2
  const toasts = env.toasts.length;
  held[0].release();
  await flush(env);
  assert.deepEqual(slots(env), ["g2|N3|r1"], "it released its own slot only");
  assert.equal(composer._state.picked.get("r1"), "N1", "game 2's pick is still there");
  assert.equal(env.toasts.length, toasts, "no 'submitted' toast for a game that is not on screen");
  card = feedHtml(env);
  assert.match(card, /data-req-confirm="r1"[^>]* disabled>提交中…</, "game 2's answer is still on its way");
  assert.match(card, /已选：<strong>1号 Alice<\/strong> · 未提交/);
  // ... so a second press in game 2 is still a double press
  assert.equal(await composer.confirmPick("r1"), false);
  clickConfirm(env, "r1");
  await flush(env);
  assert.equal(backend.seatCalls.length, 2);

  // game 2's own answer: its slot is freed; N1 - picked after the press - stays
  held[1].release();
  await flush(env);
  assert.match(lastToast(env), /已提交：5号 Eve/);
  assert.deepEqual(slots(env), []);
  assert.equal(composer._state.picked.get("r1"), "N1");
});

test("G45 race B (seat): the answer of the seat before cannot touch the page of the next seat, though it is the same game and the same request", async () => {
  const { env, backend, composer, held } = await wolvesGame();
  clickOption(env, "r1", "N4"); // N3 answers N4
  await flush(env);
  clickConfirm(env, "r1");
  await flush(env);
  assert.deepEqual(slots(env), ["s-test|N3|r1"]);

  // N6 - another wolf, who has the same request - is played now (another tab gave the seat back and took this one)
  takeover(env, "N6", { requests: [wolfSeat()] });
  env.flushRaf();
  assert.equal(env.app().state.viewpoint, "N6");
  assert.equal(composer._state.picked.size, 0, "N3's pick is not N6's");
  assert.doesNotMatch(feedHtml(env), /提交中/, "N6's card is not 'on its way' because of N3's answer");
  clickOption(env, "r1", "N5");
  await flush(env);
  assert.equal(composer._state.picked.get("r1"), "N5");

  held[0].release();
  await flush(env);
  assert.deepEqual(slots(env), []);
  assert.equal(composer._state.picked.get("r1"), "N5", "N6's pick is still there");
  assert.doesNotMatch(lastToast(env), /已提交/, "no 'submitted' toast for a seat that is not played any more");
  // N6 answers on its own
  clickConfirm(env, "r1");
  await flush(env);
  assert.deepEqual(sentBodies(backend), [{ request_id: "r1", value: "N4" }, { request_id: "r1", value: "N5" }]);
  assert.deepEqual(slots(env), ["s-test|N6|r1"]);
  held[1].release();
  await flush(env);
  assert.equal(composer._state.picked.size, 0);
  assert.deepEqual(slots(env), []);
});

test("G45 race B (text): the answer of the game before leaves the text typed in the new game alone", async () => {
  const textReq = () => F.makeRequest({ id: "r2", kind: "text", options: [], question: "请留下遗言", to: ["N3"], share_answers: false, auto_close: "all_answered", seconds_left: 45 });
  const textSeat = () => F.seatRequest("r2", { kind: "text", max_chars: 300, channel_id: "dm-N3" });
  const ctx = await livePlayed("N3", { session_id: "g1", status: "running", requests: [textReq()] }, { requests: [textSeat()] });
  const { env, backend } = ctx;
  const composer = env.app().composer();
  channelClick(env, "dm-N3");
  env.flushRaf();
  const held = holdAnswers(ctx);

  typeIntoCard(env, "r2", "旧局的遗言");
  pressSubmit(env, "r2");
  await flush(env);
  assert.deepEqual(sentBodies(backend), [{ request_id: "r2", value: "旧局的遗言" }]);
  assert.deepEqual(slots(env), ["g1|N3|r2"]);

  env.sse("snapshot", { session: gameOf("g2", "N3", { requests: [textReq()] }, { requests: [textSeat()] }) }, 700);
  channelClick(env, "dm-N3");
  env.flushRaf();
  assert.equal(composer._state.reqDrafts.size, 0, "game 1's draft is gone");
  assert.match(feedHtml(env), /data-req-submit="r2">提交</, "game 2's card is not 'on its way'");

  typeIntoCard(env, "r2", "新局的遗言");
  held[0].release();
  await flush(env);
  assert.deepEqual(slots(env), []);
  assert.equal(composer._state.reqDrafts.get("r2"), "新局的遗言", "game 2's text is not the answer of game 1 to take away");
  assert.match(feedHtml(env), /data-req-submit="r2">提交</);

  // game 2's answer goes out on its own; the same text again while it is on its way is a double press
  pressSubmit(env, "r2");
  await flush(env);
  assert.deepEqual(sentBodies(backend), [{ request_id: "r2", value: "旧局的遗言" }, { request_id: "r2", value: "新局的遗言" }]);
  pressSubmit(env, "r2");
  await flush(env);
  assert.equal(backend.seatCalls.length, 2);
  held[1].release();
  await flush(env);
  assert.equal(composer._state.reqDrafts.size, 0);
  assert.deepEqual(slots(env), []);
});

test("an answer that comes back after its request closed resurrects nothing: no pick, no button, no slot (G45)", async () => {
  const { env, composer, held } = await wolvesGame({}, ["r3"]);
  // two answers on their way (N4 for r1, N5 for r3), a newer pick on each
  clickOption(env, "r1", "N4");
  clickConfirm(env, "r1");
  clickOption(env, "r3", "N5");
  clickConfirm(env, "r3");
  await flush(env);
  assert.equal(held.length, 2);
  clickOption(env, "r1", "N1");
  clickOption(env, "r3", "N1");
  await flush(env);
  assert.deepEqual([...composer._state.picked.keys()].sort(), ["r1", "r3"]);
  // both requests close: the picks go with them, the buttons too
  env.sse("request.closed", closedPayload("r1"));
  env.sse("request.closed", closedPayload("r3"));
  assert.equal(composer._state.picked.size, 0);
  assert.doesNotMatch(feedHtml(env), /data-req-answer|data-req-confirm/);
  assert.deepEqual(slots(env), ["s-test|N3|r1", "s-test|N3|r3"], "the answers are still on their way: the slots stay until they return");

  // r1's answer was accepted just before the close, r3's refused just after it
  held[0].release();
  await flush(env);
  assert.match(lastToast(env), /已提交：4号 David/);
  assert.equal(composer._state.picked.size, 0, "nothing came back to life");
  held[1].release({ accepted: false, seq: null, code: "REQUEST_CLOSED", message: "这个请求已经关闭。" });
  await flush(env);
  assert.match(lastToast(env), /这个请求已经关闭/);
  assert.equal(composer._state.picked.size, 0);
  assert.deepEqual(slots(env), []);
  assert.doesNotMatch(feedHtml(env), /data-req-answer|data-req-confirm|req-pick/);
});

test("the answers of two requests have a slot each: both go out, each clears only its own pick (G45)", async () => {
  const { env, backend, composer, held } = await wolvesGame({}, ["r3"]);
  clickOption(env, "r1", "N4");
  await flush(env);
  clickConfirm(env, "r1");
  await flush(env);
  // r1's answer is on its way: r3 is another request - its press is not held up
  clickOption(env, "r3", "N5");
  await flush(env);
  clickConfirm(env, "r3");
  await flush(env);
  assert.deepEqual(sentBodies(backend), [{ request_id: "r1", value: "N4" }, { request_id: "r3", value: "N5" }]);
  assert.deepEqual(slots(env), ["s-test|N3|r1", "s-test|N3|r3"]);
  held[1].release();
  await flush(env);
  assert.deepEqual(slots(env), ["s-test|N3|r1"]);
  assert.equal(composer._state.picked.get("r1"), "N4", "r1's own answer is still on its way: its pick stays");
  assert.equal(composer._state.picked.has("r3"), false, "r3's answer took r3's pick");
  held[0].release();
  await flush(env);
  assert.deepEqual(slots(env), []);
  assert.equal(composer._state.picked.size, 0);
});

test("G45: a text typed after the press stays, the text that was sent goes; the button says 提交中… meanwhile", async () => {
  const request = F.makeRequest({ id: "r2", kind: "text", options: [], question: "请留下遗言", to: ["N3"], share_answers: false, auto_close: "all_answered", seconds_left: 45 });
  const seatRow = (over = {}) => F.seatRequest("r2", { kind: "text", max_chars: 300, channel_id: "dm-N3", ...over });
  const ctx = await livePlayed("N3", { status: "running", requests: [request] }, { requests: [seatRow()] });
  const { env, backend } = ctx;
  const composer = env.app().composer();
  channelClick(env, "dm-N3");
  env.flushRaf();
  const held = holdAnswers(ctx);

  typeIntoCard(env, "r2", "我是好人");
  pressSubmit(env, "r2");
  await flush(env);
  assert.deepEqual(sentBodies(backend), [{ request_id: "r2", value: "我是好人" }]);
  let card = feedHtml(env);
  assert.match(card, /data-req-submit="r2" title="上一次提交还没有完成，完成后可以再提交新的选择" disabled>提交中…</);
  assert.doesNotMatch(card, /<textarea[^>]* disabled/, "the box stays usable");
  // the person goes on typing; a second press sends nothing
  typeIntoCard(env, "r2", "我是好人，真的");
  pressSubmit(env, "r2");
  await flush(env);
  assert.equal(backend.seatCalls.length, 1);

  takeover(env, "N3", { requests: [seatRow({ answered: true, my_answer: { value: "我是好人", label: "我是好人", reason: "" } })] });
  held[0].release();
  await flush(env);
  assert.equal(composer._state.reqDrafts.get("r2"), "我是好人，真的", "the newer text is not the answer's to take away");
  assert.deepEqual(slots(env), []);
  assert.match(feedHtml(env), /data-req-submit="r2">修改回答</);
  // the change goes out; the text that was sent goes with it - blanks around it do not count as newer
  pressSubmit(env, "r2");
  await flush(env);
  assert.deepEqual(sentBodies(backend).slice(1), [{ request_id: "r2", value: "我是好人，真的" }]);
  typeIntoCard(env, "r2", "我是好人，真的  ");
  held[1].release();
  await flush(env);
  assert.equal(composer._state.reqDrafts.has("r2"), false);
  assert.deepEqual(slots(env), []);
});

test("the card of a new game is not drawn with the pick of the game before, though its request has the same id (G45)", async () => {
  const vote = () => F.makeRequest({ id: "r1", to: F.PLAYER_IDS, share_answers: false, auto_close: "all_answered", seconds_left: 60 });
  const mine = () => ({ requests: [F.seatRequest("r1", { channel_id: "public" })] });
  const { env } = await livePlayed("N3", { session_id: "g1", status: "running", requests: [vote()] }, mine());
  const composer = env.app().composer();
  // game 1: a vote of everybody lives in the public channel, the one that is open: a pick
  clickOption(env, "r1", "N4");
  await flush(env);
  assert.match(feedHtml(env), /class="req-opt picked" data-req-answer="r1" data-value="N4"/);
  // game 2 arrives and the page is painted - the feed first, the input box after it
  env.sse("snapshot", { session: gameOf("g2", "N3", { requests: [vote()] }, mine()) }, 700);
  const card = feedHtml(env);
  assert.match(card, /data-req-answer="r1" data-value="N4" aria-pressed="false"/, "game 2's card is there");
  assert.doesNotMatch(card, /req-opt picked|req-pick|已选/);
  assert.match(card, /data-req-confirm="r1" title="先选一个选项，再提交" disabled>提交</);
  assert.equal(composer._state.picked.size, 0);
});

test("a press that lands between the arrival of a new game and the repaint does not cast the pick of the game before (G45)", async () => {
  const { env, backend, composer } = await wolvesGame({ session_id: "g1" });
  clickOption(env, "r1", "N4"); // game 1: picked, not submitted
  await flush(env);
  // game 2 arrives (its first request is "r1" as well) - the page has not repainted yet: game 1's card is still there
  env.sse("snapshot", { session: gameOf("g2", "N3", { requests: [wolfRequest()] }, { requests: [wolfSeat()] }) }, 700, { flush: false });
  clickConfirm(env, "r1"); // the press on game 1's button
  await flush(env);
  assert.equal(backend.seatCalls, undefined, "nothing was sent: N4 was picked in the game before");
  assert.match(lastToast(env), /先选一个选项/);
  assert.equal(composer._state.picked.size, 0);
  assert.deepEqual(slots(env), []);
});

test("a text typed in the game before is not put into the new game's input of the same request id, not even before the repaint (G45)", async () => {
  const textVote = () => F.makeRequest({ id: "r2", kind: "text", options: [], question: "请留下遗言", to: F.PLAYER_IDS, share_answers: false, auto_close: "all_answered", seconds_left: 45 });
  const mine = () => ({ requests: [F.seatRequest("r2", { kind: "text", max_chars: 300, channel_id: "public" })] });
  const { env } = await livePlayed("N3", { session_id: "g1", status: "running", requests: [textVote()] }, mine());
  const composer = env.app().composer();
  typeIntoCard(env, "r2", "旧局的草稿");
  assert.equal(composer._state.reqDrafts.get("r2"), "旧局的草稿");
  env.sse("snapshot", { session: gameOf("g2", "N3", { requests: [textVote()] }, mine()) }, 700, { flush: false });
  // the cards of game 2 are drawn now: their inputs are filled before the box has seen the new game
  const input = { value: "?", getAttribute: (k) => ({ "data-req-text": "r2", "data-initial": "" })[k] ?? null };
  composer.restoreRequestDrafts({ querySelectorAll: () => [input] });
  assert.equal(input.value, "", "an empty input: the text typed in the game before is not this game's draft");
  assert.equal(composer._state.reqDrafts.size, 0);
});

// =====================================================================================================
// the phone
// =====================================================================================================
test("phone: the viewpoint label and the takeover bar stay in the first row (v0.14: nothing of them goes into ⋯); the input box lives in the feed pane above the tab bar", async () => {
  const css = H.CSS_SRC.replace(/\r\n/g, "\n");
  // the label and the takeover bar take the place of the title: they stay in the first row (never moved into the ⋯ menu); the bar is a line of its own
  assert.match(css, /html\[data-layout="phone"\]\[data-takeover="1"\] \.brand-group \{\s*display: none/);
  assert.match(css, /html\[data-layout="phone"\] \.takeover-bar \{[^}]*flex: 1 1 100%/);
  assert.match(css, /html\[data-layout="phone"\] \.takeover-bar \.takeover-badge \{\s*display: none/, "the label already says whose seat it is");
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
  const live = env.app().state.session;
  env.app().state.replay = { live, installing: false };
  // what a replay shows is a session that says so (replay.js decorates it): nothing can be taken over meanwhile - the menu's 接管 buttons are off
  env.app().state.session = { ...live, takeover: { seat: null }, replay: { title: "旧对局", session_id: "old-1", duration_gt: 41 } };
  env.app().renderAll(true);
  env.el("btn-viewpoint").dispatch("click");
  assert.equal(isDisabled(menuButton(env, "data-vp-take", "N5")), true);
  assert.match(menuButton(env, "data-vp-take", "N5"), /重放中不能接管/);
  assert.equal(await env.app().takeover().takeover("N5"), false, "and the handler refuses as well");
  assert.match(lastToast(env), /重放中不能接管/);
  env.app().state.replay = null;
  env.app().state.session = live;
});

// -------------------------------------------------------------------------------------------------
H.runTests(tests);
