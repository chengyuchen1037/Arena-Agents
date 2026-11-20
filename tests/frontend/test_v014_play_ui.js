"use strict";
// v0.14 (web-play: W1-W4) in the page: the player cards (the seat is a bubble, ✔ ⚠ ✖ ⏳ next to the name, nothing about an identity before the
// deal - H28, H22, H52), moving the cards while the game has not started (H29), the system status label and the viewpoint label (H26, H27), the
// start check dialog (H25), the input box of a played seat (H16) and the question at the end of a game (H31) - static/js/app.js + takeover.js +
// composer.js + layout.js against the fake DOM and the fake backend (see harness.js).  The pure functions: test_v014_play_view.js.
// Run: node tests/frontend/test_v014_play_ui.js
const assert = require("node:assert/strict");
const H = require("./harness.js");

const { F, target, settle, bootLive } = H;

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

/** Boot, connect and deliver a snapshot of `session` (event id 500). */
async function liveWith(session, overrides = {}, envOpts = {}) {
  const ctx = await bootLive({ session, ...overrides }, envOpts);
  // (a render that throws - e.g. a card that reads the identity of a game that has not been dealt - stops the first state fetch: no stream, no page)
  assert.ok(ctx.env.lastEs(), "the page booted and opened its event stream: the first render did not throw");
  ctx.env.sse("snapshot", { session }, 500);
  return ctx;
}
const live = (over = {}, overrides = {}, envOpts = {}) => liveWith(F.makeSession({ event_id: 500, ...over }), overrides, envOpts);
/** A game that has not started: nobody is dealt (H52) - `dealt` is false and no seat has a tag. */
const liveIdle = (over = {}, overrides = {}, envOpts = {}) => liveWith(F.undealt(F.makeSession({ event_id: 500, status: "idle", ...over })), overrides, envOpts);

const flush = async (env, n = 25) => {
  await settle(n);
  env.flushRaf();
};
const html = (env, id) => env.el(id).innerHTML;
const count = (text, re) => (String(text).match(re) || []).length;
const cards = (env) => html(env, "participants-grid");

// =====================================================================================================
// W1: the cards
// =====================================================================================================
test("a game that has not started: the real tileHtml / renderBoard draw all 12 cards - the seat bubble, the ✔ next to the name, and NO identity tag, not even a grey ?", async () => {
  const { env } = await liveIdle();
  const board = cards(env);
  assert.equal(count(board, /class="pcard[ "]/g), 12, "twelve cards (the blocker: a null identity must not stop the board from being drawn)");
  assert.equal(count(board, /pcard-tag/g), 0, "no identity tag at all");
  assert.ok(!/>\s*\?\s*</.test(board), "no grey ? placeholder");
  assert.ok(!/当前视角看不到这名玩家的身份/.test(board));
  for (const word of ["狼人", "预言家", "女巫", "猎人", "守卫", "平民", "白狼王", "存活"]) assert.ok(!board.includes(word), `nothing about the identity: ${word}`);
  assert.equal(count(board, /class="pcard-num seat-bubble"/g), 12, "every card carries its seat bubble");
  for (const n of [1, 6, 12]) assert.match(board, new RegExp(`class="pcard-num seat-bubble"[^>]*>${n}</span>`));
  assert.equal(count(board, /class="pcard-init init-ready"/g), 12, "✔ next to every name");
  assert.match(board, /class="pcard-nameline"><span class="pcard-name">Alice<\/span><span class="pcard-init init-ready"[^>]*>✔<\/span>/);
  assert.equal(count(board, /class="pcard [^"]*no-tag/g), 12, "a card without a tag is drawn tighter");
});

test("the marks of the initialization next to the names: ✔ ready, ⚠ stale, ✖ failed, ⏳ initializing - and none for a game of the old format", async () => {
  const participants = F.makeParticipants({ N2: { init_status: "stale" }, N3: { init_status: "failed" }, N4: { init_status: "initializing" }, N5: { init_status: "uninitialized" }, N6: { init_status: null, bot_id: "" } });
  const { env } = await liveIdle({ participants: participants.map((p) => ({ ...p, tags: [] })) });
  const board = cards(env);
  assert.equal(count(board, /init-ready/g), 7);
  for (const [status, symbol] of [["stale", "⚠"], ["failed", "✖"], ["initializing", "⏳"], ["uninitialized", "○"]]) {
    assert.match(board, new RegExp(`class="pcard-init init-${status}"[^>]*>${symbol}</span>`), status);
  }
  assert.equal(count(board, /class="pcard-init/g), 11, "the seat of the old format (no bot) has no mark");
});

test("after the deal the identity tags are back (and the cards are not marked no-tag); a viewpoint that may not see them shows the grey ?", async () => {
  const { env } = await live({ status: "running" });
  let board = cards(env);
  assert.equal(count(board, /pcard-tag/g), 12);
  assert.match(board, /pcard-tag tag-danger"[^>]*>狼人</);
  assert.equal(count(board, /no-tag/g), 0);
  env.app().setViewpoint("N1");
  board = cards(env);
  assert.ok(count(board, /pcard-tag tag-[a-z]+ unknown/g) > 0, "another player's identity is a grey ? once the cards are dealt");
});

test("the deal changes the cards in place: an event that deals the game brings the identity tags, a new game takes them away again", async () => {
  const idle = F.undealt(F.makeSession({ event_id: 500, status: "idle" }));
  const { env } = await liveWith(idle);
  assert.equal(count(cards(env), /pcard-tag/g), 0);
  const dealt = F.makeSession({ event_id: 501, status: "running" });
  env.sse("snapshot", { session: dealt }, 501);
  assert.equal(count(cards(env), /pcard-tag/g), 12);
  env.sse("snapshot", { session: F.undealt(F.makeSession({ event_id: 502, status: "idle", session_id: "s-next" })) }, 502);
  assert.equal(count(cards(env), /pcard-tag/g), 0, "a new game: nothing is dealt");
});

test("before the deal even the global view shows no summary strip (nothing to count) and the participant bar has no faction numbers", async () => {
  const { env } = await liveIdle();
  assert.equal(env.el("board-summary").hidden, true);
  assert.equal(html(env, "board-summary"), "");
});

// =====================================================================================================
// W1: moving the cards while the game has not started (H29)
// =====================================================================================================
/** The fake DOM draws no layout: the twelve cards are fake elements the seat drag can ask for - card i sits at x = 100 * i (90 wide). */
function fakeCards(env) {
  const cards = F.PLAYER_IDS.map((id, i) => {
    const el = new H.FakeEl("div");
    el.setAttribute("data-actor", id);
    el.getBoundingClientRect = () => ({ left: i * 100, top: 0, right: i * 100 + 90, bottom: 120, width: 90, height: 120 });
    el.closest = (sel) => (String(sel).includes(".pcard") ? el : null);
    return el;
  });
  env.el("participants-grid").querySelectorAll = (sel) => (sel === ".pcard[data-actor]" ? cards : []);
  return cards;
}
const pointer = (extra = {}) => ({ pointerType: "mouse", pointerId: 1, isPrimary: true, ...extra });
const down = (env, card, x, y, extra = {}) => env.el("participants-grid").dispatch("pointerdown", { target: card, button: 0, clientX: x, clientY: y, ...pointer(extra) });
const move = (env, x, y, extra = {}) => env.document.dispatchEvent("pointermove", { clientX: x, clientY: y, buttons: 1, ...pointer(extra) });
const up = (env, x, y, extra = {}) => env.document.dispatchEvent("pointerup", { clientX: x, clientY: y, ...pointer(extra) });
const zeroTimer = (env) => {
  const i = env.timers.findIndex((t) => t.ms === 0);
  if (i < 0) return false;
  env.timers.splice(i, 1)[0].fn();
  return true;
};
const lastToast = (env) => (env.toasts.length ? env.toasts[env.toasts.length - 1].textContent : "");
/** The HTML of the card of `seat`. */
const cardOf = (env, seat) => cards(env).split(/(?=<div class="pcard[ "])/).find((c) => c.includes(`data-actor="${seat}"`)) || "";
const nameOfSeat = (env, seat) => (cardOf(env, seat).match(/class="pcard-name">([^<]*)</) || [])[1];

test("a card dragged onto another seat (mouse): the line-up changes at once - names, ✔ and the seat bubbles follow - and PUT /api/bots/lineup {order} makes it the server's", async () => {
  const { env, backend } = await liveIdle();
  const cardEls = fakeCards(env);
  assert.equal(env.el("participants-grid").classList.contains("seats-draggable"), true, "the cursor says the cards can be moved");
  assert.deepEqual(F.PLAYER_IDS.map((id) => nameOfSeat(env, id)).slice(0, 5), ["Alice", "Bob", "Carol", "David", "Eve"]);
  down(env, cardEls[1], 150, 60);
  assert.equal(env.app().seatDrag().busy(), true, "pressed: the grid is held");
  move(env, 153, 60); // (3 px: still a press)
  assert.equal(env.app().seatDrag().dragging(), false);
  move(env, 300, 60);
  assert.equal(env.app().seatDrag().dragging(), true);
  assert.equal(cardEls[1].classList.contains("seat-dragging"), true);
  assert.equal(cardEls[1].style.transform, "translate(150px, 0px)", "the card follows the hand");
  assert.equal(env.el("participants-grid").classList.contains("seats-dragging"), true);
  move(env, 460, 60); // over the card of seat 5
  assert.equal(cardEls[4].classList.contains("seat-target"), true, "the seat it would take is outlined");
  assert.equal(cardEls[2].classList.contains("seat-target"), false);
  up(env, 460, 60);
  assert.equal(cardEls[1].style.transform, "", "the card is put down");
  assert.equal(cardEls[4].classList.contains("seat-target"), false);
  assert.equal(env.app().seatDrag().busy(), false);
  // at once, before the answer of the server: Bob went to seat 5, the bots in between moved up
  assert.deepEqual(F.PLAYER_IDS.map((id) => nameOfSeat(env, id)).slice(0, 6), ["Alice", "Carol", "David", "Eve", "Bob", "Frank"]);
  assert.match(cardOf(env, "N5"), /class="pcard-num seat-bubble" aria-hidden="true">5<\/span>/, "the seats keep their numbers");
  await flush(env);
  const expected = [F.botId(1), ...[3, 4, 5].map(F.botId), F.botId(2), ...[6, 7, 8, 9, 10, 11, 12].map(F.botId)];
  assert.deepEqual(backend.lineupPuts, [expected], "one request, the whole line-up in seat order");
  assert.deepEqual(env.app().state.session.bots.lineup, expected);
  assert.equal(nameOfSeat(env, "N5"), "Bob", "the server's table says the same");
  assert.equal(env.app().state.lineupSaving, false);
});

test("a press that does not move is a click: the card opens on the click (never on the mousedown while it can be moved); after a drag the click that follows opens nothing", async () => {
  const { env, backend } = await liveIdle();
  const cardEls = fakeCards(env);
  const stub = target({ ".pcard": { "data-actor": "N3" } });
  // press + release without moving: nothing is moved
  down(env, cardEls[2], 250, 60);
  env.el("participants-grid").dispatch("mousedown", { target: stub, button: 0 });
  assert.equal(env.el("detail-panel").hidden, true, "a mousedown might be the start of a drag: it opens nothing");
  up(env, 250, 60);
  env.el("participants-grid").dispatch("click", { target: stub });
  assert.equal(env.el("detail-panel").hidden, false, "the click opens the card");
  assert.equal(backend.lineupPuts, undefined);
  env.app().closePopover();
  // a drag that ends on the card it began on is no move; the click that comes after it is not a press on the card
  down(env, cardEls[2], 250, 60);
  move(env, 300, 70);
  move(env, 262, 60);
  up(env, 262, 60);
  env.el("participants-grid").dispatch("click", { target: stub });
  assert.equal(env.el("detail-panel").hidden, true, "the click that ends a drag opens nothing");
  assert.equal(backend.lineupPuts, undefined, "dropped on its own seat: nothing to save");
  assert.equal(nameOfSeat(env, "N3"), "Carol");
});

test("while a card is in the hand the cards are not repainted (it would be replaced under the finger); they are drawn when it is let go", async () => {
  const { env } = await liveIdle();
  const cardEls = fakeCards(env);
  down(env, cardEls[0], 40, 60);
  env.sse("roster.updated", { participants: [{ id: "N1", name: "Alicia", avatar: F.avatarOf(1) }] });
  assert.equal(nameOfSeat(env, "N1"), "Alice", "held: still the old drawing");
  up(env, 40, 60);
  assert.equal(zeroTimer(env), true, "the repaint waits for the click that may follow the release");
  assert.equal(nameOfSeat(env, "N1"), "Alicia");
});

test("a press that is never let go does not freeze the cards: Esc, the window losing the focus and a cancelled pointer all put the card down; the browser's own image drag does not start", async () => {
  const { env } = await liveIdle();
  const cardEls = fakeCards(env);
  const grid = env.el("participants-grid");
  // the avatar is an <img>: the browser would start dragging it and take the pointer away - while the cards can be moved that is prevented (only then)
  let prevented = false;
  grid.dispatch("dragstart", { target: cardEls[0], preventDefault: () => (prevented = true) });
  assert.equal(prevented, true);
  // Esc
  down(env, cardEls[0], 40, 60);
  move(env, 200, 60);
  assert.equal(env.app().seatDrag().dragging(), true);
  env.document.dispatchEvent("keydown", { key: "Escape" });
  assert.deepEqual([env.app().seatDrag().busy(), cardEls[0].classList.contains("seat-dragging"), cardEls[0].style.transform], [false, false, ""]);
  // the window loses the focus (an alert, another window)
  down(env, cardEls[0], 40, 60);
  move(env, 200, 60);
  env.sandbox.dispatchEvent("blur");
  assert.equal(env.app().seatDrag().busy(), false);
  // the browser cancels the pointer (it took the gesture for a scroll)
  down(env, cardEls[0], 40, 60);
  env.document.dispatchEvent("pointercancel", pointer());
  assert.equal(env.app().seatDrag().busy(), false);
  // the cards are drawn again afterwards (what was held back is not lost)
  env.sse("roster.updated", { participants: [{ id: "N1", name: "Alicia", avatar: F.avatarOf(1) }] });
  assert.equal(nameOfSeat(env, "N1"), "Alicia");
  // after the start the browser's drag is left alone
  env.sse("game.status", { status: "running", reason: "started", winner: null });
  prevented = false;
  grid.dispatch("dragstart", { target: cardEls[0], preventDefault: () => (prevented = true) });
  assert.equal(prevented, false);
});

test("the keyboard focus stays on its card when the cards are drawn again (an event that changes a card must not throw a person off the card they tabbed to)", async () => {
  const { env } = await liveIdle();
  const cardEls = fakeCards(env);
  const grid = env.el("participants-grid");
  grid.contains = (node) => cardEls.includes(node);
  const focused = [];
  const fresh = new Map();
  cardEls.forEach((c, i) => (c.focus = (opts) => focused.push([i, opts])));
  grid.querySelector = (sel) => {
    const id = (sel.match(/data-actor="([^"]+)"/) || [])[1];
    const i = F.PLAYER_IDS.indexOf(id);
    if (i < 0) return null;
    if (!fresh.has(i)) fresh.set(i, { focus: (opts) => focused.push([i, opts]) }); // (the card as the new drawing made it)
    return fresh.get(i);
  };
  env.document.activeElement = cardEls[2];
  cardEls[2].getAttribute = (name) => (name === "data-actor" ? "N3" : null);
  env.sse("roster.updated", { participants: [{ id: "N5", name: "Eva", avatar: F.avatarOf(5) }] }); // another card changes: the grid is written again
  assert.equal(JSON.stringify(focused), JSON.stringify([[2, { preventScroll: true }]]), "the focus went back to Carol's card (without scrolling the page)");
  // nothing is written, nothing is refocused
  focused.length = 0;
  env.sse("session.updated", { summary: "x" });
  assert.deepEqual(focused, []);
  // the focus is somewhere else (the page): the cards do not take it
  env.document.activeElement = null;
  env.sse("roster.updated", { participants: [{ id: "N6", name: "Fran", avatar: F.avatarOf(6) }] });
  assert.deepEqual(focused, []);
});

test("touch: a card has to be held before it can be moved (a swipe scrolls instead); then the hand moves it like a mouse", async () => {
  const { env, backend } = await liveIdle();
  const cardEls = fakeCards(env);
  // a swipe: moved before the long press timer fired -> nothing is dragged, the press is over
  down(env, cardEls[1], 150, 60, { pointerType: "touch" });
  assert.equal(env.timers.filter((t) => t.ms === 380).length, 1, "the long press timer runs");
  move(env, 150, 90, { pointerType: "touch" });
  let scrolled = true;
  env.el("participants-grid").dispatch("touchmove", { target: cardEls[1], preventDefault: () => (scrolled = false) });
  assert.equal(scrolled, true, "a swipe is not cancelled: the page scrolls as usual");
  assert.equal(env.app().seatDrag().busy(), false, "a swipe ends the press");
  assert.equal(env.timers.filter((t) => t.ms === 380).length, 0, "and its timer");
  assert.equal(env.app().seatDrag().dragging(), false);
  // held: the timer fires, the card is lifted; moving the hand moves it; a touchmove is cancelled so the page does not scroll
  down(env, cardEls[1], 150, 60, { pointerType: "touch" });
  const timer = env.timers.find((t) => t.ms === 380);
  env.timers.splice(env.timers.indexOf(timer), 1);
  timer.fn();
  assert.equal(env.app().seatDrag().dragging(), true);
  assert.equal(cardEls[1].classList.contains("seat-dragging"), true);
  let prevented = false;
  env.el("participants-grid").dispatch("touchmove", { target: cardEls[1], preventDefault: () => (prevented = true) });
  assert.equal(prevented, true, "the page does not scroll while a card is carried");
  move(env, 350, 60, { pointerType: "touch" });
  assert.equal(cardEls[3].classList.contains("seat-target"), true);
  up(env, 350, 60, { pointerType: "touch" });
  await flush(env);
  assert.deepEqual(backend.lineupPuts.length, 1);
  assert.equal(nameOfSeat(env, "N4"), "Bob");
  // a long press that is let go without moving lifts and puts down: nothing is saved
  down(env, cardEls[0], 40, 60, { pointerType: "touch" });
  const hold = env.timers.find((t) => t.ms === 380);
  env.timers.splice(env.timers.indexOf(hold), 1);
  hold.fn();
  up(env, 40, 60, { pointerType: "touch" });
  await flush(env);
  assert.equal(backend.lineupPuts.length, 1);
});

test("keyboard: Alt+→ / Alt+← with the focus on a card move it one seat (and the focus goes with it); at the ends nothing moves but the browser does not go back", async () => {
  const { env, backend } = await liveIdle();
  const cardEls = fakeCards(env);
  let focused = null;
  cardEls.forEach((c, i) => (c.focus = () => (focused = i)));
  const key = (card, k, extra = {}) => {
    let prevented = false;
    env.el("participants-grid").dispatch("keydown", { target: card, key: k, altKey: true, preventDefault: () => (prevented = true), ...extra });
    return prevented;
  };
  assert.equal(key(cardEls[1], "ArrowRight"), true);
  await flush(env);
  assert.deepEqual(backend.lineupPuts[0].slice(0, 3), [F.botId(1), F.botId(3), F.botId(2)], "Bob went one seat to the right");
  assert.equal(focused, 2, "the focus follows the moved card: Alt+→ can be pressed again");
  assert.equal(nameOfSeat(env, "N3"), "Bob");
  assert.equal(key(cardEls[2], "ArrowLeft"), true);
  await flush(env);
  assert.equal(nameOfSeat(env, "N2"), "Bob");
  assert.equal(backend.lineupPuts.length, 2);
  // the ends: nothing to move, but Alt+← would take the browser back - it is taken
  assert.equal(key(cardEls[0], "ArrowLeft"), true);
  assert.equal(key(cardEls[11], "ArrowRight"), true);
  await flush(env);
  assert.equal(backend.lineupPuts.length, 2);
  // other keys / without Alt: not ours
  assert.equal(key(cardEls[1], "ArrowRight", { altKey: false }), false);
  assert.equal(key(cardEls[1], "ArrowUp"), false);
  assert.equal(backend.lineupPuts.length, 2);
});

test("a refused or failed move puts the cards back and says why: the server's sentence (409 the game has started / 422) or the failure of the request", async () => {
  const { env, backend } = await liveIdle();
  const cardEls = fakeCards(env);
  backend.failures["PUT /api/bots/lineup"] = [409, "阵容只能在对局开始之前修改；请先新建对局。", { code: "game_active" }];
  down(env, cardEls[1], 150, 60);
  move(env, 300, 60);
  move(env, 460, 60);
  up(env, 460, 60);
  assert.equal(nameOfSeat(env, "N5"), "Bob", "at once: the move is shown");
  await flush(env);
  assert.deepEqual(F.PLAYER_IDS.slice(0, 5).map((id) => nameOfSeat(env, id)), ["Alice", "Bob", "Carol", "David", "Eve"], "rolled back");
  assert.match(lastToast(env), /座位没有调整成功，已恢复原来的座位：.*阵容只能在对局开始之前修改/);
  assert.equal(JSON.stringify(env.app().state.session.bots.lineup), JSON.stringify(F.BOT_IDS), "the line-up of the session is the old one too");
  assert.equal(env.app().state.lineupSaving, false, "and the next move is possible");
  // a network failure rolls back as well
  delete backend.failures["PUT /api/bots/lineup"];
  const original = backend.fetch;
  backend.fetch = async (url, init) => {
    if (String(url).endsWith("/api/bots/lineup")) throw new TypeError("Failed to fetch");
    return original(url, init);
  };
  env.sandbox.fetch = backend.fetch;
  down(env, cardEls[0], 40, 60);
  move(env, 150, 60);
  move(env, 160, 60);
  up(env, 160, 60);
  await flush(env);
  assert.equal(nameOfSeat(env, "N1"), "Alice");
  assert.match(lastToast(env), /座位没有调整成功，已恢复原来的座位/);
});

test("one move at a time: a second move while the first is on its way waits (it says so) - and nothing is sent twice", async () => {
  const { env, backend } = await liveIdle();
  const cardEls = fakeCards(env);
  const gate = backend.hold((key) => key === "PUT /api/bots/lineup");
  down(env, cardEls[1], 150, 60);
  move(env, 300, 60);
  move(env, 460, 60);
  up(env, 460, 60);
  await flush(env, 5);
  assert.equal(env.app().state.lineupSaving, true);
  down(env, cardEls[0], 40, 60);
  move(env, 150, 60);
  move(env, 160, 60);
  up(env, 160, 60);
  assert.match(lastToast(env), /正在保存上一次的座位调整/);
  gate.releaseAll();
  await flush(env);
  assert.equal(backend.lineupPuts.length, 1, "the second move was not sent");
  gate.off();
});

test("no moving once the game has started, while a seat is played or while a replay is shown: a press is a plain press again (it opens the card on the mousedown, as before)", async () => {
  for (const [name, session] of [
    ["running", F.makeSession({ event_id: 500, status: "running" })],
    ["paused", F.makeSession({ event_id: 500, status: "paused" })],
    ["finished", F.makeSession({ event_id: 500, status: "finished" })],
    ["a seat is played", F.undealt(F.makePlayedSession("N3", { event_id: 500, status: "idle" }))],
  ]) {
    const { env, backend } = await liveWith(session);
    const cardEls = fakeCards(env);
    assert.equal(env.el("participants-grid").classList.contains("seats-draggable"), false, name);
    down(env, cardEls[1], 150, 60);
    assert.equal(env.app().seatDrag().busy(), false, `${name}: nothing is held`);
    move(env, 300, 60);
    move(env, 460, 60);
    up(env, 460, 60);
    assert.equal(backend.lineupPuts, undefined, name);
    assert.equal(env.app().seatDrag().armed(), false, name);
    env.el("participants-grid").dispatch("mousedown", { target: target({ ".pcard": { "data-actor": "N2" } }), button: 0 });
    assert.equal(env.el("detail-panel").hidden, false, `${name}: the mousedown opens the card`);
    // the drag handler of the app refuses too (a stale callback): it says why
    assert.equal(await env.app().moveSeat(1, 4), false);
    assert.match(lastToast(env), /对局已经开始，不能再调整座位|接管期间不能调整座位/, name);
  }
  // a game starting while the cards are in the hand: the drop is refused (the check is made again at the drop)
  const { env, backend } = await liveIdle();
  const cardEls = fakeCards(env);
  down(env, cardEls[1], 150, 60);
  move(env, 300, 60);
  move(env, 460, 60);
  env.sse("game.status", { status: "running", reason: "started", winner: null });
  up(env, 460, 60);
  await flush(env);
  assert.equal(backend.lineupPuts, undefined);
  assert.match(lastToast(env), /对局已经开始，不能再调整座位/);
});

test("the seat bubble: a translucent round bubble over the card's corner - its own colours in the light and in the dark theme; it never takes a click", () => {
  const css = H.CSS_SRC.replace(/\r\n/g, "\n");
  assert.match(css, /:root,\n\[data-theme="light"\] \{[^}]*--seat-bubble-bg: rgba\(30, 41, 59, 0\.6\);[^}]*--seat-bubble-fg: #ffffff;/);
  assert.match(css, /\[data-theme="dark"\] \{[^}]*--seat-bubble-bg: rgba\(226, 232, 240, 0\.66\);[^}]*--seat-bubble-fg: #0b1220;/);
  const bubble = (css.match(/\n\.pcard-num \{([^}]*)\}/) || [])[1] || "";
  for (const rule of [/top: -6px/, /left: -5px/, /border-radius: 999px/, /background: var\(--seat-bubble-bg\)/, /color: var\(--seat-bubble-fg\)/, /pointer-events: none/, /box-shadow: 0 0 0 2px var\(--seat-bubble-ring\)/]) {
    assert.match(bubble, rule);
  }
  assert.ok(!/--num-bg|--num-fg/.test(css), "the old in-card circle is gone");
  // the layouts: the bubble sits over the corner on a tablet and on a phone as well
  for (const sel of ['html[data-layout="compact"] .pcard-num', 'html[data-layout="phone"] .main-container[data-phone-tab="feed"] .pcard-num', 'html[data-layout="phone"] .main-container[data-phone-tab="players"] .pcard-num']) {
    const block = css.slice(css.indexOf(`${sel} {`), css.indexOf("}", css.indexOf(`${sel} {`)));
    assert.match(block, /top: -[3-6]px;/, sel);
    assert.match(block, /left: -[3-5]px;/, sel);
  }
  assert.match(css, /\.participants-grid\.seats-draggable \.pcard \{[^}]*cursor: grab/);
  assert.match(css, /\.pcard\.seat-dragging \{[^}]*pointer-events: none/);
  assert.match(css, /\.pcard\.seat-target \{[^}]*outline: 2px dashed/);
  assert.match(css, /\.pcard\.no-tag \.pcard-text \{[^}]*gap: 0/);
});

// =====================================================================================================
// W2: the system status label (H26) and the viewpoint label in the first row of a phone (H27)
// =====================================================================================================
const attr = (env, id, name) => env.el(id).getAttribute(name);
const labelOf = (env) => [env.el("active-status-text").textContent, attr(env, "active-status-pill", "data-code"), env.el("active-status-dot").className];
const cloneOf = (v) => JSON.parse(JSON.stringify(v));

test("the label at the front of the second row says what the system is doing: 对局前 · 未初始化 · 初始化中 · 对局中 · 已暂停 · 已结束 · 复盘中 (H26)", async () => {
  // the order in the page: the label is the first thing of the second row, before the phase
  const page = H.HTML_SRC;
  assert.ok(page.indexOf('id="topbar-status"') < page.indexOf('id="active-status-pill"') && page.indexOf('id="active-status-pill"') < page.indexOf('id="phase-pill"'));
  assert.ok(page.slice(page.indexOf('id="topbar-status"'), page.indexOf('id="active-status-pill"')).split("<div").length === 2, "nothing between the row and the label");

  const { env } = await liveIdle();
  assert.deepEqual(labelOf(env), ["对局前", "before_game", "status-dot idle"]);
  // a bot that is not initialized (a new bots payload and the server's label that goes with it)
  const bots = cloneOf(env.app().state.session.bots);
  bots.bots[2].init.status = "uninitialized";
  env.sse("bots.updated", { bots });
  env.sse("session.updated", { system_status: { code: "uninitialized", text: "未初始化", reason: "" } });
  assert.deepEqual(labelOf(env), ["未初始化", "uninitialized", "status-dot uninit"]);
  assert.match(attr(env, "active-status-pill", "title"), /先点「⚙ 全局初始化」/);
  // an initialization runs: 初始化中 (the server sends the bots, then the label that goes with them)
  bots.init_job = { job_id: "j1", state: "running", ids: [F.botId(3)], started_at: 1763800000, deadline_s: 120 };
  env.sse("bots.updated", { bots });
  env.sse("session.updated", { system_status: { code: "initializing", text: "初始化中", reason: "" } });
  assert.deepEqual(labelOf(env), ["初始化中", "initializing", "status-dot busy"]);
  assert.match(attr(env, "active-status-pill", "title"), /期间不能开局/);
  bots.init_job = null;
  bots.bots[2].init.status = "ready";
  env.sse("bots.updated", { bots });
  env.sse("session.updated", { system_status: { code: "before_game", text: "对局前", reason: "" } });
  assert.deepEqual(labelOf(env), ["对局前", "before_game", "status-dot idle"]);
  // the game runs, is paused by hand (with the reason), runs on with a soft pause, ends, and is reviewed
  env.sse("game.status", { status: "running", reason: "started", winner: null });
  assert.deepEqual(labelOf(env), ["对局中", "in_game", "status-dot active"], "(the label follows the status even before session.updated)");
  env.sse("session.updated", { status: "paused", pause_reason: "user", system_status: { code: "paused", text: "已暂停", reason: "手动暂停" } });
  assert.deepEqual(labelOf(env), ["已暂停 · 手动暂停", "paused", "status-dot paused"]);
  env.sse("session.updated", { status: "running", pause_reason: "", system_status: { code: "in_game", text: "对局中 · agent 已暂停", reason: "" }, soft_pause: { state: "held", in_flight: 0 }, clock_running: false });
  assert.deepEqual(labelOf(env), ["对局中 · agent 已暂停", "in_game", "status-dot paused"]);
  env.sse("session.updated", { status: "finished", winner: "好人阵营", soft_pause: null, system_status: { code: "finished", text: "已结束", reason: "" }, review: { available: true, done: [], running: false, supported: true, deadline_s: 60, seconds_left: null } });
  assert.deepEqual(labelOf(env), ["已结束", "finished", "status-dot finished"]);
  env.sse("session.updated", { system_status: { code: "reviewing", text: "复盘中", reason: "" }, review: { available: true, done: [], running: true, supported: true, deadline_s: 60, seconds_left: 50 } });
  env.sse("review.progress", { id: "N1", bot_id: F.botId(1), name: "Alice", state: "running", message: "", seconds_left: 50, deadline_s: 60, changes: null });
  assert.deepEqual(labelOf(env), ["复盘中", "reviewing", "status-dot busy"]);
  assert.match(attr(env, "active-status-pill", "title"), /最多 60 秒/);
  env.sse("review.progress", { id: "N1", bot_id: F.botId(1), name: "Alice", state: "done", message: "", seconds_left: null, deadline_s: 60, changes: { files: [], proposals: 0 } });
  env.sse("session.updated", { system_status: { code: "finished", text: "已结束", reason: "" }, review: { available: true, done: ["N1"], running: false, supported: true, deadline_s: 60, seconds_left: null } });
  assert.deepEqual(labelOf(env), ["已结束", "finished", "status-dot finished"]);
});

test("a label that arrives before the status it belongs to, or after it, never contradicts the game: the status of the page wins over a label that is late", async () => {
  const { env } = await liveIdle();
  env.sse("session.updated", { system_status: { code: "initializing", text: "初始化中", reason: "" } });
  assert.equal(env.el("active-status-text").textContent, "初始化中");
  // the game status moves first (game.status), the label of the next session.updated comes a moment later
  env.sse("game.status", { status: "running", reason: "started", winner: null });
  assert.equal(env.el("active-status-text").textContent, "对局中", "an initialization does not run in a game that runs");
  env.sse("game.status", { status: "finished", reason: "end_game", winner: "好人阵营", summary: "" });
  assert.equal(env.el("active-status-text").textContent, "已结束");
});

test("the label's own styles: one set of colours per state, a pulsing dot while the system works, no motion when the user asks for none", () => {
  const css = H.CSS_SRC.replace(/\r\n/g, "\n");
  assert.match(css, /\.status-pill\[data-code="uninitialized"\],\n\.status-pill\[data-code="paused"\] \{[^}]*background: var\(--amber-bg-tint\)/);
  assert.match(css, /\.status-pill\[data-code="initializing"\],\n\.status-pill\[data-code="reviewing"\] \{[^}]*background: var\(--blue-bg-tint\)/);
  assert.match(css, /\.status-dot\.uninit \{[^}]*background-color: var\(--amber-border\)/);
  assert.match(css, /\.status-dot\.busy \{[^}]*animation: statusBusy/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\) \{\s*\.status-dot\.busy \{\s*animation: none/);
  // every dot class the page can put on the label has a rule
  for (const dot of ["active", "paused", "idle", "finished", "uninit", "busy"]) assert.ok(css.includes(`.status-dot.${dot}`), dot);
});

test("the viewpoint label is in the first row on a phone as well: it is not moved into ⋯, it is smaller there, and while a seat is played it takes the title's place with the bar under it", async () => {
  const css = H.CSS_SRC.replace(/\r\n/g, "\n");
  assert.match(css, /html\[data-layout="phone"\] \.viewpoint-chip \{[^}]*min-height: 34px/);
  assert.match(css, /html\[data-layout="phone"\]\[data-takeover="1"\] \.viewpoint-bar \{[^}]*flex: 1 1 auto/);
  assert.match(css, /html\[data-layout="phone"\]\[data-takeover="1"\] \.viewpoint-chip \{[^}]*width: 100%/);
  const relocations = (H.SRC.app.match(/const RELOCATIONS = \[([\s\S]*?)\];/) || [, ""])[1];
  const moved = [...relocations.matchAll(/\bid:\s*"([^"]+)"/g)].map((m) => m[1]);
  assert.ok(moved.includes("btn-theme") && moved.length >= 5, "(the list of what moves into the ⋯ menu was found)");
  assert.deepEqual(moved.filter((id) => /viewpoint|takeover/.test(id)), [], "never moved into the ⋯ menu");
  // in the page the label comes before the takeover bar, both in the left part of the first row
  const left = H.HTML_SRC.slice(H.HTML_SRC.indexOf('class="topbar-left"'), H.HTML_SRC.indexOf('class="topbar-right"'));
  assert.ok(left.indexOf('id="viewpoint-bar"') > 0 && left.indexOf('id="viewpoint-bar"') < left.indexOf('id="takeover-bar"'));
  // the menu of a phone: as wide as the screen allows, scrolls when it is long; its buttons are tap targets
  assert.match(css, /\.viewpoint-menu \{[^}]*width: min\(340px, calc\(100vw - 16px\)\);[^}]*overflow-y: auto/);
  // the phone: while a seat is played the label says whose seat it is and the bar does not repeat it
  const { env } = await H.bootLive({ session: F.makePlayedSession("N3", { event_id: 500, status: "paused" }) }, { innerWidth: 390, innerHeight: 844, userAgent: "iPhone Mobile", coarse: true, maxTouchPoints: 5 });
  env.sse("snapshot", { session: F.makePlayedSession("N3", { event_id: 500, status: "paused" }) }, 500);
  assert.equal(env.document.documentElement.getAttribute("data-takeover"), "1");
  assert.equal(env.el("viewpoint-text").textContent, "Carol视角（接管）");
  assert.equal(env.el("takeover-bar").hidden, false);
});

// =====================================================================================================
// W3: the start check (H25) and the length of a speech (H16)
// =====================================================================================================
const dialogOpen = (env) => env.el("dialog-modal").style.display === "flex";
const dialogOk = async (env) => {
  env.el("btn-dialog-ok").dispatch("click");
  await flush(env);
};
const text = (htmlText) => String(htmlText).replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
const fixClick = (env, fix, botId = "") => env.el("dialog-body").dispatch("click", { target: target({ "[data-nr-fix]": { "data-nr-fix": fix, "data-nr-bot": botId } }) });

const NOT_READY = (extra = []) => [
  F.makeProblem("contract", [{ code: "contract_invalid", message: "官方契约有问题：契约没有提到这些工具：jot。", fix: "import_contract" }]),
  F.makeProblem("bot", [{ code: "not_initialized", message: "还没有初始化。", fix: "init" }, { code: "core_missing", message: "底层设定是空的。", fix: "edit_core" }], { bot_id: F.botId(3), seat: "N3", name: "Carol" }),
  F.makeProblem("bot", [{ code: "stale", message: "底层设定改过，需要重新初始化。", fix: "init" }], { bot_id: F.botId(6), seat: "N6", name: "Frank" }),
  F.makeProblem("binding", [{ code: "no_binding", message: "N7 还没有绑定可用的模型。", fix: "bind_model" }], { bot_id: F.botId(7), seat: "N7", name: "Grace" }),
  F.makeProblem("binding", [{ code: "no_binding", message: "主持人还没有绑定可用的模型。", fix: "bind_model" }], { name: "主持人" }),
  ...extra,
];
const refuseStart = (backend, problems = NOT_READY()) => {
  backend.failures["POST /api/control/start"] = [409, "还没有准备好，不能开局：官方契约：官方契约有问题。", { code: "NOT_READY", problems }];
};

test("start refused (409 NOT_READY): a dialog lists what is not ready by player - each item with the server's sentence and a button - and nothing is started or dealt (H25)", async () => {
  const { env, backend } = await liveIdle();
  refuseStart(backend);
  env.el("btn-start").dispatch("click");
  await flush(env);
  assert.equal(backend.calls.filter((c) => c.url.endsWith("/api/control/start")).length, 1, "the start was asked for once");
  assert.equal(dialogOpen(env), true);
  assert.equal(env.el("dialog-title").textContent, "还不能开局");
  const body = html(env, "dialog-body");
  assert.deepEqual([...body.matchAll(/<h4 class="nr-title">([^<]*)<\/h4>/g)].map((m) => m[1]), ["官方契约", "3 号 Carol", "6 号 Frank", "7 号 Grace", "主持人"], "the contract first, then the players in seat order, the moderator last");
  assert.match(text(body), /没有开局，也没有发牌/);
  assert.match(body, /<span class="nr-msg">还没有初始化。<\/span><button[^>]*data-nr-fix="init" data-nr-bot="b-00001003"[^>]*>去初始化<\/button>/);
  assert.match(body, /<span class="nr-msg">底层设定是空的。<\/span><button[^>]*data-nr-fix="edit_core"[^>]*>去编辑底层设定<\/button>/);
  assert.match(body, /data-nr-fix="import_contract" data-nr-bot=""[^>]*>去导入契约</);
  assert.match(body, /data-nr-fix="bind_model" data-nr-bot="b-00001007"[^>]*>去绑定模型</);
  assert.match(body, /data-nr-fix="bind_model" data-nr-bot="moderator"[^>]*>去绑定模型</, "the moderator's model: Bots.focus('moderator', 'bind_model')");
  assert.equal(env.el("btn-dialog-ok").textContent, "⚙ 全局初始化", "two players only need their initialization: one button for all");
  assert.equal(env.el("btn-dialog-cancel").hidden, false);
  assert.ok(!env.toasts.some((t) => /还没有准备好/.test(t.textContent)), "the dialog says it - no toast on top");
  // nothing was dealt, the game is as it was
  assert.equal(env.app().state.session.status, "idle");
  assert.equal(env.app().state.session.dealt, false);
  assert.equal(html(env, "participants-grid").includes("pcard-tag"), false);
  await dialogOk(env);
});

test("a fix button of the dialog closes it and asks window.Bots.focus(botId, fix) to take the user there; the global button starts the global initialization; without window.Bots nothing happens and nothing breaks", async () => {
  const { env, backend } = await liveIdle();
  const calls = [];
  env.sandbox.Bots.focus = (botId, fix) => {
    calls.push(["focus", botId, fix]);
    return true;
  };
  env.sandbox.Bots.initAll = () => calls.push(["initAll"]);
  refuseStart(backend);
  env.el("btn-start").dispatch("click");
  await flush(env);
  fixClick(env, "init", F.botId(3));
  await flush(env);
  assert.equal(dialogOpen(env), false, "the dialog is closed");
  assert.deepEqual(calls, [["focus", F.botId(3), "init"]]);
  // the contract / rules fixes carry no bot: the settings dialog opens at that column
  env.el("btn-start").dispatch("click");
  await flush(env);
  fixClick(env, "import_contract", "");
  await flush(env);
  assert.deepEqual(calls[1], ["focus", null, "import_contract"]);
  // the moderator's model: its own id, which Bots.focus knows
  env.el("btn-start").dispatch("click");
  await flush(env);
  fixClick(env, "bind_model", "moderator");
  await flush(env);
  assert.deepEqual(calls[2], ["focus", "moderator", "bind_model"]);
  calls.splice(2, 1); // (the rest of this test counts what follows)
  // the OK button of a dialog with several players that need their initialization: the global one
  env.el("btn-start").dispatch("click");
  await flush(env);
  await dialogOk(env);
  assert.deepEqual(calls[2], ["initAll"]);
  // a cancel does nothing
  env.el("btn-start").dispatch("click");
  await flush(env);
  env.el("btn-dialog-cancel").dispatch("click");
  await flush(env);
  assert.equal(calls.length, 3, JSON.stringify(calls));
  // no window.Bots at all: the fix is ignored in silence
  env.sandbox.Bots = undefined;
  env.el("btn-start").dispatch("click");
  await flush(env);
  fixClick(env, "init", F.botId(3));
  await flush(env);
  assert.equal(dialogOpen(env), false);
  assert.equal(calls.length, 3, JSON.stringify(calls));
  assert.ok(!env.toasts.some((t) => /失败|错误|undefined/.test(t.textContent)), "and no toast either");
});

test("one player that only needs the initialization: the dialog just says so (知道了, no global button); a refusal without a list is a toast with the server's sentence", async () => {
  const { env, backend } = await liveIdle();
  refuseStart(backend, [F.makeProblem("bot", [{ code: "stale", message: "需要重新初始化。", fix: "init" }], { bot_id: F.botId(2), seat: "N2", name: "Bob" })]);
  env.el("btn-start").dispatch("click");
  await flush(env);
  assert.equal(env.el("btn-dialog-ok").textContent, "知道了");
  assert.equal(env.el("btn-dialog-cancel").hidden, true);
  await dialogOk(env);
  refuseStart(backend, []);
  env.el("btn-start").dispatch("click");
  await flush(env);
  assert.equal(dialogOpen(env), false);
  assert.match(lastToast(env), /还没有准备好，不能开局/);
  // the real fake backend does the same on its own: nobody initialized -> every player is a group
  const fresh = await liveIdle({}, { botsPreset: "fresh" });
  fresh.env.el("btn-start").dispatch("click");
  await flush(fresh.env);
  assert.equal(dialogOpen(fresh.env), true);
  assert.ok((html(fresh.env, "dialog-body").match(/class="nr-group nr-player"/g) || []).length >= 12);
  assert.match(html(fresh.env, "dialog-body"), /data-nr-fix="init"/);
});

const playedOver = (seat, over = {}, takeoverOver = {}) => ({ status: "running", participants: F.makeParticipants({ [seat]: { state: "human" } }), takeover: F.makeTakeover(seat, takeoverOver), ...over });
const livePlayed = (seat = "N3", over = {}, takeoverOver = {}) => live(playedOver(seat, over, takeoverOver));
const type = (env, value) => {
  env.el("composer-input").value = value;
  env.el("composer-input").dispatch("input");
};
const enter = (env) => env.el("composer-input").dispatch("keydown", { key: "Enter", shiftKey: false, preventDefault() {} });
const speakCalls = (backend) => (backend.seatCalls || []).filter((c) => c.kind === "speak").map((c) => c.body);
const writable = (max = 300) => ({ channels: { public: { can_write: true, max_chars: max, floor: F.seatFree() } } });

test("the input box counts live against the limit of the channel: over it the counter and the box turn red, the sentence says how much to shorten, and the send button is OFF (H16)", async () => {
  const { env, backend } = await livePlayed("N3", {}, writable(300));
  type(env, "字".repeat(300));
  assert.equal(env.el("composer-count").textContent, "300/300");
  assert.equal(env.el("composer-count").classList.contains("over"), false);
  assert.equal(env.el("btn-composer-send").disabled, false, "exactly the limit is fine");
  type(env, "字".repeat(312));
  assert.equal(env.el("composer-count").textContent, "312/300");
  assert.equal(env.el("composer-count").classList.contains("over"), true);
  assert.equal(env.el("composer").classList.contains("is-over"), true);
  assert.equal(env.el("btn-composer-send").disabled, true);
  assert.equal(env.el("composer-hint").textContent, "这条发言 312 个字，超过了单条上限 300 字：请删减 12 字后再发送");
  assert.equal(attr(env, "btn-composer-send", "title"), env.el("composer-hint").textContent);
  // Enter does not send it either - and says why
  enter(env);
  await flush(env);
  assert.equal(speakCalls(backend).length, 0);
  assert.equal(env.el("composer-notice").hidden, false);
  assert.match(env.el("composer-notice").textContent, /超过了单条上限 300 字：请删减 12 字后再发送/);
  assert.equal(env.el("composer-input").value.length, 312, "the draft is untouched");
  // shortened: the red goes, the notice goes with the next keystroke, the button works
  type(env, "字".repeat(290));
  assert.equal(env.el("composer").classList.contains("is-over"), false);
  assert.equal(env.el("composer-notice").hidden, true);
  assert.equal(env.el("btn-composer-send").disabled, false);
  assert.match(H.CSS_SRC, /\.composer\.is-over \.composer-input \{\s*border-color: var\(--danger-text\)/);
});

test("a speech the server refuses as TOO_LONG (its count differs from the page's, or the limit changed): the draft stays in the box, the server's sentence is shown, the floor is still the person's; nothing is cut", async () => {
  const { env, backend } = await livePlayed("N3", {}, writable(300));
  const draft = "我想说的话".repeat(10);
  backend.seatReply = { speak: { accepted: false, seq: null, code: "TOO_LONG", message: "这条发言 50 个字，超过了单条上限 40 字，没有发出，发言权还在。请删减后重发。", data: { chars: 50, limit: 40 } } };
  type(env, draft);
  enter(env);
  await flush(env);
  assert.deepEqual(speakCalls(backend).map((c) => c.content), [draft], "it was sent whole: the page never cuts a speech");
  assert.equal(env.el("composer-input").value, draft, "the draft is kept");
  assert.equal(env.el("composer-notice").hidden, false);
  assert.equal(env.el("composer-notice").textContent, "这条发言 50 个字，超过了单条上限 40 字，没有发出，发言权还在。请删减后重发。");
  assert.equal(env.el("btn-composer-send").disabled, false, "the floor is still the person's: it can be sent again once it is shorter");
  assert.ok(!env.toasts.some((t) => /截/.test(t.textContent)));
  // typing clears the notice (the person is shortening it); a second refusal says it again
  type(env, draft.slice(0, 45));
  assert.equal(env.el("composer-notice").hidden, true);
  enter(env);
  await flush(env);
  assert.equal(env.el("composer-notice").hidden, false);
  assert.equal(speakCalls(backend).length, 2);
  // a refusal without a sentence of its own: the page words it from `data` (chars / limit)
  backend.seatReply = { speak: { accepted: false, seq: null, code: "TOO_LONG", data: { chars: 51, limit: 40 } } };
  enter(env);
  await flush(env);
  assert.equal(env.el("composer-notice").textContent, "这条发言 51 个字，超过了单条上限 40 字，没有发出，发言权还在。请删减后重发。");
  backend.seatReply = { speak: { accepted: false, seq: null, code: "TOO_LONG" } };
  enter(env);
  await flush(env);
  assert.equal(env.el("composer-notice").textContent, "这条发言太长，没有发出，发言权还在。请删减后重发。");
  // shortened enough: it is accepted, the box empties
  backend.seatReply = { speak: { accepted: true, seq: 70, code: null, message: "已发送。" } };
  type(env, "短一点");
  enter(env);
  await flush(env);
  assert.equal(env.el("composer-input").value, "");
});

// =====================================================================================================
// W4: the question at the end of a game - should the bots review it? (H31-H33)
// =====================================================================================================
const REVIEW_KEY = "arena_review_dismissed_v1";
const reviewOf = (over = {}) => ({ available: true, done: [], running: false, supported: true, deadline_s: 60, seconds_left: null, ...over });
const finished = (over = {}, review = {}) => F.makeSession({ event_id: 500, status: "finished", winner: "好人阵营", review: reviewOf(review), ...over });
const modalOpen = (env) => env.el("review-modal").style.display === "flex";
const rows = (env) => html(env, "review-body").split(/(?=<div class="rv-row)/).filter((r) => r.startsWith('<div class="rv-row'));
const rowOf = (env, seat) => rows(env).find((r) => r.includes(`data-seat="${seat}"`)) || "";
const check = (env, seat, on) => env.el("review-body").dispatch("change", { target: { checked: on, closest: (sel) => (sel.includes("data-rv-check") ? { getAttribute: () => seat, checked: on } : null), getAttribute: () => seat } });
const rvStyle = (env, seat, on) => env.el("review-body").dispatch("change", { target: { checked: on, closest: (sel) => (sel.includes("data-rv-style") ? { getAttribute: () => seat, checked: on } : null) } });
const progress = (id, over = {}) => ({ id, bot_id: F.botId(Number(id.slice(1))), name: F.NAMES[Number(id.slice(1)) - 1], state: "running", message: "", seconds_left: 58, deadline_s: 60, changes: null, ...over });
const withDetail = (env, backend, spans) => {
  const original = backend.fetch;
  backend.fetch = async (url, init) => (/\/api\/games\/[^/]+\/detail$/.test(String(url)) ? H.resp(200, { participants: [], files: { game_bytes: 1, prev_bytes: 0, log_bytes: 1 }, human_spans: spans }) : original(url, init));
  env.sandbox.fetch = backend.fetch;
};

test("a game that is over and was not reviewed asks once: all twelve bots listed in seat order and selected; the buttons are 复盘 and 暂不", async () => {
  const { env } = await liveWith(finished());
  assert.equal(modalOpen(env), true);
  assert.equal(env.el("review-title").textContent, "这局结束了，是否让 bot 复盘？");
  assert.equal(rows(env).length, 12);
  assert.deepEqual(rows(env).map((r) => r.match(/data-seat="([^"]+)"/)[1]), F.PLAYER_IDS);
  assert.equal(rows(env).filter((r) => /data-rv-check="N\d+" checked/.test(r)).length, 12, "all selected");
  assert.match(rows(env)[2], /<span class="rv-seat">3<\/span><span class="rv-name">Carol<\/span>/);
  assert.ok(!html(env, "review-body").includes("有人类代打") && !html(env, "review-body").includes("学习接管者的风格"), "nobody played: no label, no switch");
  assert.equal(env.el("review-count").textContent, "已选 12 / 12");
  assert.deepEqual([env.el("btn-review-start").textContent, env.el("btn-review-start").disabled, env.el("btn-review-later").textContent], ["复盘", false, "暂不"]);
  assert.match(text(html(env, "review-body")), /最多 60 秒.*每局每个 bot 只复盘一次.*参与者栏里的「复盘全部」「复盘此玩家」还可以补做/);
  // the same game again (a reconnect's snapshot, any event) does not ask a second time
  env.el("btn-review-later").dispatch("click");
  assert.equal(modalOpen(env), false);
  env.sse("snapshot", { session: finished({ event_id: 501 }) }, 501);
  env.sse("session.updated", { summary: "x" });
  assert.equal(modalOpen(env), false, "once per game");
});

test("暂不 (the button, ✕ and Escape) is remembered in localStorage[arena_review_dismissed_v1] for that game: not after a reload either, but another game asks again", async () => {
  for (const close of [(env) => env.el("btn-review-later").dispatch("click"), (env) => env.el("btn-close-review").dispatch("click"), (env) => env.document.dispatchEvent("keydown", { key: "Escape" })]) {
    const { env } = await liveWith(finished());
    assert.equal(modalOpen(env), true);
    close(env);
    assert.equal(modalOpen(env), false);
    assert.deepEqual(JSON.parse(env.storage.get(REVIEW_KEY)), { "s-test": true });
  }
  const again = await liveWith(finished(), {}, { storage: { [REVIEW_KEY]: JSON.stringify({ "s-test": true }) } });
  assert.equal(modalOpen(again.env), false, "a reload does not ask again");
  const other = await liveWith(finished({ session_id: "s-other" }), {}, { storage: { [REVIEW_KEY]: JSON.stringify({ "s-test": true }) } });
  assert.equal(modalOpen(other.env), true, "another game: the question is asked");
  other.env.el("btn-review-later").dispatch("click");
  assert.deepEqual(JSON.parse(other.env.storage.get(REVIEW_KEY)), { "s-test": true, "s-other": true });
  // junk in the storage is not an error
  const junk = await liveWith(finished(), {}, { storage: { [REVIEW_KEY]: "not json" } });
  assert.equal(modalOpen(junk.env), true);
  // the list is kept short: the oldest games are forgotten
  const many = Object.fromEntries(Array.from({ length: 100 }, (_, i) => [`g${i}`, true]));
  const full = await liveWith(finished(), {}, { storage: { [REVIEW_KEY]: JSON.stringify(many) } });
  full.env.el("btn-review-later").dispatch("click");
  const kept = JSON.parse(full.env.storage.get(REVIEW_KEY));
  assert.equal(Object.keys(kept).length, 100);
  assert.ok(kept["s-test"] && !kept.g0 && kept.g99);
});

test("it asks only when the game is over, can be reviewed, nothing runs and nobody was reviewed yet - and when the game BECOMES over", async () => {
  for (const [name, session] of [
    ["running", F.makeSession({ event_id: 500, status: "running", review: reviewOf({ available: false }) })],
    ["paused", F.makeSession({ event_id: 500, status: "paused" })],
    ["idle", F.undealt(F.makeSession({ event_id: 500, status: "idle" }))],
    ["a game of the old format (not supported)", finished({}, { supported: false })],
    ["not available", finished({}, { available: false })],
    ["a review is running", finished({}, { running: true })],
    ["somebody was reviewed already", finished({}, { done: ["N3"] })],
  ]) {
    const { env } = await liveWith(session);
    assert.equal(modalOpen(env), false, name);
  }
  // the game ends: game.status alone does not say that a review can be asked for - the next session.updated does
  const { env } = await liveWith(F.makeSession({ event_id: 500, status: "running", review: reviewOf({ available: false }) }));
  env.sse("game.status", { status: "finished", reason: "end_game", winner: "好人阵营", summary: "狼人全部出局" });
  assert.equal(modalOpen(env), false);
  env.sse("session.updated", { status: "finished", winner: "好人阵营", review: reviewOf() });
  assert.equal(modalOpen(env), true);
  // a replay on screen: the question waits (it is about the live game) and comes when the replay is left
  const live2 = await liveWith(F.makeSession({ event_id: 500, status: "running", review: reviewOf({ available: false }) }));
  live2.env.app().state.replay = { live: live2.env.app().state.session, installing: false };
  live2.env.app().state.replay.live.status = "finished";
  live2.env.app().state.replay.live.review = { ...live2.env.app().state.replay.live.review, ...reviewOf() };
  live2.env.app().handleEvent({ id: 700, type: "session.updated", data: { summary: "" } });
  assert.equal(modalOpen(live2.env), false, "not while a replay is shown");
});

test("the seats a person played are marked 有人类代打 and have the switch 学习接管者的风格 (on by default): from the seat played now and from the game's human_spans", async () => {
  const session = F.makePlayedSession("N3", { event_id: 500, status: "finished", review: reviewOf() });
  const { env, backend } = await bootLive({ session: F.makeSession({ event_id: 400, status: "running", review: reviewOf({ available: false }) }) }); // (the game is still running at the first look)
  withDetail(env, backend, [{ seat: "N6", from_seq: 40, to_seq: 218 }, { seat: "N3", from_seq: 220, to_seq: 400 }]);
  env.sse("snapshot", { session }, 500);
  assert.equal(modalOpen(env), true);
  assert.match(rowOf(env, "N3"), /有人类代打/);
  assert.match(rowOf(env, "N3"), /data-rv-style="N3" checked/);
  assert.ok(!rowOf(env, "N6").includes("有人类代打"), "until the game says who was played");
  await flush(env);
  assert.match(rowOf(env, "N6"), /有人类代打/);
  assert.match(rowOf(env, "N6"), /data-rv-style="N6" checked/);
  assert.ok(!rowOf(env, "N1").includes("学习接管者的风格"), "only a played seat has the switch");
  assert.match(text(html(env, "review-body")), /有人类代打」：这局里这个座位有人接管过/);
  // a detail that cannot be read leaves what the page knows
  const second = await bootLive({ session: F.makeSession({ event_id: 400, status: "running", review: reviewOf({ available: false }) }) });
  second.backend.failures["GET /api/games/s-test/detail"] = [404, "对局不存在。"];
  second.env.sse("snapshot", { session }, 500);
  await flush(second.env);
  assert.equal(modalOpen(second.env), true);
  assert.match(rowOf(second.env, "N3"), /有人类代打/);
  assert.ok(!rowOf(second.env, "N6").includes("有人类代打"));
});

test("复盘: POST /api/game/review {ids, learn_style: {seat: bool}} with what was ticked; the same window then shows every bot's countdown", async () => {
  const session = F.makePlayedSession("N3", { event_id: 500, status: "finished", review: reviewOf() });
  const { env, backend } = await bootLive({ session: F.makeSession({ event_id: 400, status: "running", review: reviewOf({ available: false }) }) });
  withDetail(env, backend, [{ seat: "N6", from_seq: 40, to_seq: 218 }]);
  env.sse("snapshot", { session }, 500);
  await flush(env);
  check(env, "N1", false);
  rvStyle(env, "N3", false);
  assert.equal(env.el("review-count").textContent, "已选 11 / 12");
  backend.reviewStarted = F.PLAYER_IDS.slice(1);
  env.el("btn-review-start").dispatch("click");
  await flush(env);
  assert.deepEqual(backend.lastReview, { ids: F.PLAYER_IDS.slice(1), learn_style: { N3: false, N6: true } }, "only the seats a person played carry a switch");
  assert.equal(env.el("review-title").textContent, "bot 正在复盘");
  assert.equal(env.el("btn-review-start").hidden, true);
  assert.equal(env.el("review-count").textContent, "还有 11 个在复盘");
  assert.equal(rows(env).length, 11);
  assert.ok(rows(env).every((r) => /排队中/.test(r)), "queued until the first progress event");
  assert.equal(env.el("btn-review-later").textContent, "关闭（后台继续）");
  assert.equal(JSON.parse(env.storage.get(REVIEW_KEY) || "{}")["s-test"], undefined, "it was answered with 复盘: nothing to remember");
  // the countdown: the seconds of the event, counted down between two events
  env.sse("review.progress", progress("N2", { message: "正在整理操作笔记" }));
  assert.match(rowOf(env, "N2"), /复盘中 · 还剩 <span class="rv-left" data-rv-left="N2">58<\/span> 秒/);
  assert.match(rowOf(env, "N2"), /rv-bar[^>]*><span data-rv-bar="N2" style="width:3%"/);
  assert.match(rowOf(env, "N2"), /正在整理操作笔记/);
  env.app().endReview()._state.stamps.N2 = Date.now() - 12000; // 12 seconds later, no new event
  env.app().endReview().tick();
  assert.match(rowOf(env, "N2"), /data-rv-left="N2">46<\/span>/, "58 - 12");
  assert.match(rowOf(env, "N2"), /style="width:23%"/);
  // finished: what it changed and the proposals that wait for the user
  env.sse("review.progress", progress("N2", { state: "done", seconds_left: null, changes: { files: [{ file: "playbook", before: "a", after: "b" }, { file: "people", before: "", after: "Bob 爱带节奏" }], proposals: 2 } }));
  assert.match(rowOf(env, "N2"), /✔ 已复盘/);
  assert.match(text(rowOf(env, "N2")), /改动了 2 个文件：打法、印象 · 有 2 条对底层设定的修改建议，等你确认/);
  env.sse("review.progress", progress("N4", { state: "done", seconds_left: null, changes: { files: [], proposals: 0 } }));
  assert.match(text(rowOf(env, "N4")), /这次没有改动记忆/);
  env.sse("review.progress", progress("N5", { state: "timeout", seconds_left: null, changes: { files: [{ file: "arch_notes", before: "", after: "x" }], proposals: 0 } }));
  assert.match(text(rowOf(env, "N5")), /时间到，已保存写好的部分/);
  assert.match(text(rowOf(env, "N5")), /改动了 1 个文件：操作笔记/);
  env.sse("review.progress", progress("N7", { state: "error", seconds_left: null, message: "模型调用失败" }));
  assert.match(rowOf(env, "N7"), /✖ 复盘失败：模型调用失败/);
  assert.equal(env.el("review-count").textContent, "还有 7 个在复盘");
  // the rest finishes: the window says it is done
  for (const id of F.PLAYER_IDS.slice(1).filter((x) => !["N2", "N4", "N5", "N7"].includes(x))) env.sse("review.progress", progress(id, { state: "done", seconds_left: null, changes: { files: [], proposals: 0 } }));
  assert.equal(env.el("review-title").textContent, "复盘完成");
  assert.equal(env.el("review-count").textContent, "全部完成：改动已经写进各自的记忆");
  assert.equal(env.el("btn-review-later").textContent, "关闭");
});

/** A body element that counts how often its HTML is written and finds the countdown elements (the fake DOM finds nothing by itself). */
function watchBody(env) {
  const body = env.el("review-body");
  let writes = 0;
  let html = body.innerHTML;
  Object.defineProperty(body, "innerHTML", { get: () => html, set: (v) => { html = String(v); writes++; }, configurable: true });
  const nodes = new Map();
  body.querySelector = (sel) => {
    if (!/data-rv-(left|bar)=/.test(sel)) return null;
    if (!nodes.has(sel)) nodes.set(sel, { textContent: "", style: {} });
    return nodes.get(sel);
  };
  return { writes: () => writes, nodes };
}

test("ticking a box or a switch does not rebuild the list (the keyboard focus stays on it); a repaint with nothing new writes nothing", async () => {
  const session = F.makePlayedSession("N3", { event_id: 500, status: "finished", review: reviewOf() });
  const { env } = await liveWith(session);
  const watch = watchBody(env);
  check(env, "N1", false);
  rvStyle(env, "N3", false);
  env.sse("session.updated", { summary: "x" }); // any event: the page asks the window to draw itself
  env.sse("agent.status", { id: "N2", state: "thinking", until: null });
  assert.equal(watch.writes(), 0, "the list was not written again");
  assert.equal(env.el("review-count").textContent, "已选 11 / 12");
  // ... but a change of what the list shows (a renamed player) is drawn, with the choices that were made kept
  env.sse("roster.updated", { participants: [{ id: "N2", name: "Bobby", avatar: F.avatarOf(2) }] });
  assert.equal(watch.writes(), 1);
  assert.match(rowOf(env, "N2"), /Bobby/);
  assert.ok(!/data-rv-check="N1" checked/.test(rowOf(env, "N1")), "N1 is still unticked");
  assert.ok(!/data-rv-style="N3" checked/.test(rowOf(env, "N3")), "and N3's switch is still off");
});

test("the countdowns move in place: the page writes the seconds and the bar into their elements once a second and does not rebuild the rows (a click on a row's button is never lost to a repaint)", async () => {
  const { env, backend } = await liveWith(finished());
  const watch = watchBody(env);
  backend.reviewStarted = F.PLAYER_IDS;
  env.el("btn-review-start").dispatch("click");
  await flush(env);
  env.sse("review.progress", progress("N2", { seconds_left: 58 }));
  const writesAfterEvent = watch.writes();
  assert.match(rowOf(env, "N2"), /data-rv-left="N2">58</);
  env.app().endReview()._state.stamps.N2 = Date.now() - 7000;
  env.app().endReview().tick();
  assert.equal(watch.writes(), writesAfterEvent, "only the numbers changed: nothing was rebuilt");
  assert.equal(watch.nodes.get('[data-rv-left="N2"]').textContent, "51");
  assert.equal(watch.nodes.get('[data-rv-bar="N2"]').style.width, "15%", "9 of the 60 seconds have gone");
  // a row that finishes changes the structure: that one is drawn again
  env.sse("review.progress", progress("N2", { state: "done", seconds_left: null, changes: { files: [], proposals: 0 } }));
  assert.ok(watch.writes() > writesAfterEvent);
  assert.match(rowOf(env, "N2"), /已复盘/);
});

test("去看看: the panel of that player opens (window.Bots.open) and the window goes; a review the server refuses stays a question with the reason", async () => {
  const { env, backend } = await liveWith(finished());
  const opened = [];
  env.sandbox.Bots.open = (seat) => opened.push(seat);
  backend.reviewStarted = ["N2"];
  check(env, "N1", false);
  for (const id of F.PLAYER_IDS.slice(2)) check(env, id, false);
  env.el("btn-review-start").dispatch("click");
  await flush(env);
  env.sse("review.progress", progress("N2", { state: "done", seconds_left: null, changes: { files: [{ file: "digest", before: "", after: "x" }], proposals: 1 } }));
  env.el("review-body").dispatch("click", { target: target({ "[data-rv-open]": { "data-rv-open": "N2" } }) });
  assert.deepEqual(opened, ["N2"]);
  assert.equal(modalOpen(env), false, "the window goes first: the panel opens behind a modal otherwise");
  assert.equal(env.el("review-modal").style.display, "none");
  // a refusal (409: nothing to review any more)
  const refused = await liveWith(finished());
  refused.backend.failures["POST /api/game/review"] = [409, "所选玩家本局都已复盘。"];
  refused.env.el("btn-review-start").dispatch("click");
  await flush(refused.env);
  assert.equal(modalOpen(refused.env), true, "still the question");
  assert.match(lastToast(refused.env), /复盘没有开始：.*所选玩家本局都已复盘/);
  assert.equal(refused.env.el("btn-review-start").disabled, false, "it can be tried again");
  // nobody selected: nothing to send
  const none = await liveWith(finished());
  none.env.el("review-body").dispatch("click", { target: target({ "[data-rv-all]": { "data-rv-all": "0" } }) });
  assert.equal(none.env.el("btn-review-start").disabled, true);
  assert.equal(none.env.el("review-count").textContent, "已选 0 / 12");
  none.env.el("review-body").dispatch("click", { target: target({ "[data-rv-all]": { "data-rv-all": "1" } }) });
  assert.equal(none.env.el("review-count").textContent, "已选 12 / 12");
});

test("the window of the end of a game is in the page: its ids, the script after composer.js and before app.js, its styles", () => {
  for (const id of ["review-modal", "review-title", "review-body", "review-count", "btn-review-start", "btn-review-later", "btn-close-review"]) assert.ok(H.HTML_SRC.includes(`id="${id}"`), id);
  const at = (name) => H.HTML_SRC.indexOf(`static/js/${name}.js`);
  assert.ok(at("composer") < at("endreview") && at("endreview") < at("app"));
  const css = H.CSS_SRC.replace(/\r\n/g, "\n");
  for (const sel of [".review-card", ".rv-row", ".rv-human", ".rv-style", ".rv-state.ok", ".rv-state.busy", ".rv-bar > span", ".rv-result", ".rv-proposals"]) assert.ok(css.includes(sel), sel);
  // its own width (the wide dialogs are 920 px), full screen on a phone like every dialog
  assert.ok(!/class="modal-card modal-wide review-card"/.test(H.HTML_SRC));
  assert.match(css, /\.modal-card\.review-card \{[^}]*width: 640px;[^}]*max-width: 96vw/);
  assert.match(css, /html\[data-layout="phone"\] \.review-body \{\s*max-height: none/);
});

// =====================================================================================================
H.runTests(tests);
