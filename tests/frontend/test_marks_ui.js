"use strict";
// The colour marks on the players (v0.13.1, G16-G20; contract §12.7): the stored format and the markup (static/js/marks.js,
// pure), and the page - the dot on every card while a seat is played, the palette, the marks in the feed / the
// request cards / the private-chat list, localStorage (per game, 50 games, other tabs), read-only after the seat is
// given back, nothing in a replay (static/js/app.js + marks.js against the fake DOM and the fake backend).
// Run: node tests/frontend/test_marks_ui.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const H = require("./harness.js");
const Marks = require(path.join(H.ROOT, "static", "js", "marks.js"));

const { F, target, settle, bootLive } = H;
const KEY = "arena_seat_marks_v1";

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// ---- helpers -------------------------------------------------------------------------------------------------
const flush = async (env, n = 25) => {
  await settle(n);
  env.flushRaf();
};
const text = (s) => s.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
const html = (env, id) => env.el(id).innerHTML;
const feedHtml = (env) => {
  const el = env.el("feed-records-container");
  return `${el.innerHTML}\n${el.children.map((c) => c.outerHTML).join("\n")}`;
};
const card = (env, id) => html(env, "participants-grid").split(/(?=<div class="pcard[ "])/).find((p) => p.includes(`data-actor="${id}"`)) || "";
const stored = (env) => JSON.parse(env.storage.get(KEY) || "{}");
const marksOfGame = (env, id = "s-test") => (stored(env)[id] || { marks: {} }).marks;
const channelClick = (env, id) => env.el("channels-list").dispatch("click", { target: target({ ".channel-item": { "data-channel": id } }) });

/** A game in which `seat` is played (running unless `over` says otherwise). */
const playedOver = (seat, over = {}, takeoverOver = {}) => ({
  status: "running",
  participants: F.makeParticipants({ [seat]: { state: "human" } }),
  takeover: F.makeTakeover(seat, takeoverOver),
  ...over,
});

async function live(over = {}, overrides = {}, envOpts = {}) {
  const session = F.makeSession({ event_id: 500, ...over });
  const ctx = await bootLive({ session, ...overrides }, envOpts);
  ctx.env.sse("snapshot", { session }, 500);
  return ctx;
}
const livePlayed = (seat = "N3", over = {}, takeoverOver = {}, overrides = {}, envOpts = {}) => live(playedOver(seat, over, takeoverOver), overrides, envOpts);

/** The corner dot of card `id` is pressed, the palette opens. */
const openPalette = (env, id) => {
  env.el("participants-grid").dispatch("click", { target: target({ "[data-mark-toggle]": { "data-mark-toggle": id } }) });
  env.flushRaf();
};
/** A colour of the open palette is pressed (the page repaints at the next frame). */
const pick = (env, id, color) => {
  env.el("mark-pop").dispatch("click", { target: target({ "[data-mark-set]": { "data-mark-set": String(color), "data-mark-actor": id } }) });
  env.flushRaf();
};
const wipe = (env, id) => {
  env.el("mark-pop").dispatch("click", { target: target({ "[data-mark-clear]": { "data-mark-actor": id } }) });
  env.flushRaf();
};
/** Mark `id` through the page, like a person does. */
const mark = (env, id, color) => {
  openPalette(env, id);
  pick(env, id, color);
};

// =====================================================================================================
// the pure part: the palette, the stored format
// =====================================================================================================
test("eight colours, each with a dot of its own form; the stylesheet draws every one of them in both themes", () => {
  assert.equal(Marks.COLORS.length, 8);
  assert.deepEqual(Marks.COLORS.map((c) => c.id), [0, 1, 2, 3, 4, 5, 6, 7]);
  assert.equal(new Set(Marks.COLORS.map((c) => c.name)).size, 8, "eight different names (the tooltip)");
  assert.equal(new Set(Marks.COLORS.map((c) => c.shape)).size, 8, "eight different forms (for people who cannot tell the colours apart)");
  assert.deepEqual(Marks.COLORS.slice(0, 3).map((c) => c.shape), ["circle", "square", "triangle"]);
  assert.equal(Marks.STORAGE_KEY, "arena_seat_marks_v1");
  assert.equal(Marks.MAX_GAMES, 50);
  assert.ok(Object.isFrozen(Marks.COLORS));
  const css = fs.readFileSync(path.join(H.ROOT, "static", "css", "style.css"), "utf8").replace(/\r\n/g, "\n");
  const start = css.indexOf("Arena-Agents v0.13.1 — playing a seat, part 2");
  const playCss = css.slice(start, css.indexOf("Arena-Agents v0.13.1 — panels:", start));
  const light = playCss.slice(playCss.indexOf(':root,\n[data-theme="light"] {'), playCss.indexOf('[data-theme="dark"] {'));
  const dark = playCss.slice(playCss.indexOf('[data-theme="dark"] {'));
  for (let i = 0; i < 8; i++) {
    assert.match(light, new RegExp(`--mark-${i}: #[0-9a-f]{6};`), `light colour ${i}`);
    assert.match(dark.slice(0, dark.indexOf("}")), new RegExp(`--mark-${i}: #[0-9a-f]{6};`), `dark colour ${i}`);
    assert.match(css, new RegExp(`\\.mark-c${i} \\{\\s*--mark-color: var\\(--mark-${i}\\);`), `.mark-c${i}`);
    assert.match(css, new RegExp(`\\.mark-dot\\.mark-c${i}::before \\{`), `the form of colour ${i}`);
  }
  assert.match(css, /\.pcard\.has-mark::before \{/, "the stripe on a marked card");
  assert.match(css, /\.mark-pop \{[^}]*position: fixed/);
});

test("the dot and the palette carry the colour's name and nothing else (G17: no text, no meaning prescribed)", () => {
  assert.equal(Marks.dotHtml(2), '<span class="mark-dot mark-c2" role="img" aria-label="黄色标记" title="黄"></span>');
  for (const bad of [null, undefined, -1, 8, 1.5, "2", NaN]) assert.equal(Marks.dotHtml(bad), "", String(bad));
  const palette = Marks.paletteHtml("N4", 3);
  assert.equal((palette.match(/data-mark-set="\d"/g) || []).length, 8);
  assert.equal((palette.match(/data-mark-clear/g) || []).length, 1);
  assert.match(palette, /class="mark-swatch selected" data-mark-set="3" data-mark-actor="N4" aria-pressed="true"/);
  assert.equal((palette.match(/aria-pressed="false"/g) || []).length, 7);
  assert.equal(text(palette), "✕", "no text but the clear button's cross");
  assert.doesNotMatch(palette, /disabled/, "something is marked: 清除 works");
  assert.match(Marks.paletteHtml("N4", null), /data-mark-clear[^>]*disabled/, "nothing to clear");
  assert.match(Marks.paletteHtml('N"<4', null), /data-actor="N4"/, "an id is kept out of the markup");
  assert.equal(Marks.safeId("N12"), "N12");
});

test("the stored form: {game: {updated_at, marks}} - what is not that is dropped, a game keeps nothing without marks", () => {
  assert.deepEqual(Marks.parseStore(null), {});
  assert.deepEqual(Marks.parseStore("not json"), {});
  assert.deepEqual(Marks.parseStore("[1,2]"), {});
  assert.deepEqual(Marks.parseStore('{"g1":{"updated_at":100,"marks":{"N4":2,"N7":5}}}'), { g1: { updated_at: 100, marks: { N4: 2, N7: 5 } } });
  const messy = Marks.parseStore(
    JSON.stringify({
      g1: { updated_at: "123", marks: { N1: 9, N2: -1, N3: 1.5, N4: "2", N5: 4, moderator: 3, engine: 1, "": 1, "a b": 2 } },
      g2: { marks: { N1: 0 } },
      g3: "x",
      g4: { marks: [1] },
      "bad id": { updated_at: 1, marks: {} },
      __proto__: { updated_at: 1, marks: { N1: 1 } },
    })
  );
  assert.deepEqual(Object.keys(messy).sort(), ["g1", "g2"]);
  assert.deepEqual(messy.g1, { updated_at: 123, marks: { N5: 4 } });
  assert.deepEqual(messy.g2, { updated_at: 0, marks: { N1: 0 } });
  assert.equal(Object.getPrototypeOf(messy), Object.prototype);

  let s = {};
  s = Marks.withMark(s, "g1", "N4", 2, 100.9);
  s = Marks.withMark(s, "g1", "N7", 5, 101);
  assert.deepEqual(s, { g1: { updated_at: 101, marks: { N4: 2, N7: 5 } } });
  const before = JSON.stringify(s);
  const same = Marks.withMark(s, "g1", "N4", 9, 102);
  assert.equal(same, s, "an unknown colour changes nothing");
  assert.equal(Marks.withMark(s, "g1", "moderator", 1, 102), s, "the moderator cannot be marked");
  assert.equal(Marks.withMark(s, "bad id", "N4", 1, 102), s);
  s = Marks.withMark(s, "g1", "N4", null, 103);
  assert.deepEqual(s, { g1: { updated_at: 103, marks: { N7: 5 } } });
  assert.equal(JSON.stringify(Marks.withMark(Marks.withMark({}, "g1", "N4", 2, 100), "g1", "N7", 5, 101)), before, "(nothing is changed in place)");
  s = Marks.withMark(s, "g1", "N7", undefined, 104);
  assert.deepEqual(s, {}, "the last mark of a game goes, the game's entry with it");
  assert.deepEqual(Marks.marksOf({ g1: { updated_at: 1, marks: { N1: 3 } } }, "g1"), { N1: 3 });
  assert.deepEqual(Marks.marksOf({}, "g1"), {});
  assert.deepEqual(Marks.marksOf({}, "__proto__"), {});
  const copy = Marks.marksOf({ g1: { updated_at: 1, marks: { N1: 3 } } }, "g1");
  copy.N1 = 5;
  assert.equal(Marks.withoutGame({ g1: { updated_at: 1, marks: { N1: 3 } }, g2: { updated_at: 2, marks: { N2: 1 } } }, "g1").g2.marks.N2, 1);
  assert.deepEqual(Object.keys(Marks.withoutGame({ g1: { updated_at: 1, marks: { N1: 3 } }, g2: { updated_at: 2, marks: { N2: 1 } } }, "g1")), ["g2"]);
  const keep = { g1: { updated_at: 1, marks: { N1: 3 } } };
  assert.equal(Marks.withoutGame(keep, "nope"), keep);
});

test("at most the 50 most recently marked games are kept (a tie goes to the one stored later)", () => {
  let s = {};
  for (let i = 0; i < 60; i++) s = Marks.withMark(s, `game-${i}`, "N1", i % 8, 1000 + i);
  assert.equal(Object.keys(s).length, 50);
  assert.ok(!("game-0" in s) && !("game-9" in s), "the oldest ten went");
  assert.ok("game-10" in s && "game-59" in s);
  // marking an old game again makes it young
  const old = Marks.withMark(s, "game-10", "N2", 1, 5000);
  const more = Marks.withMark(old, "game-60", "N1", 1, 5001);
  assert.ok("game-10" in more && !("game-11" in more));
  // a tie in time: the one stored later stays
  const tie = {};
  for (let i = 0; i < 52; i++) tie[`t-${i}`] = { updated_at: 7, marks: { N1: 0 } };
  const pruned = Marks.pruneStore(tie);
  assert.equal(Object.keys(pruned).length, 50);
  assert.ok(!("t-0" in pruned) && !("t-1" in pruned) && "t-2" in pruned && "t-51" in pruned);
  assert.equal(Marks.pruneStore({ a: { updated_at: 1, marks: {} } }).a.updated_at, 1);
});

// =====================================================================================================
// the page: marking
// =====================================================================================================
test("while a seat is played every other card has the mark button (an empty ring); the played seat's own card and the moderator have none", async () => {
  const { env } = await livePlayed("N3");
  for (const id of ["N1", "N2", "N4", "N12"]) {
    assert.match(card(env, id), new RegExp(`<span class="pcard-mark is-empty" data-mark-toggle="${id}" role="button" tabindex="0" aria-haspopup="true" aria-label="颜色标记" title="颜色标记"><span class="mark-ring"`), id);
    assert.match(card(env, id), /class="pcard[^"]* mark-slot/, id);
  }
  assert.doesNotMatch(card(env, "N3"), /pcard-mark|mark-slot/, "you cannot mark yourself");
  assert.doesNotMatch(html(env, "mod-chip"), /mark/, "the moderator cannot be marked");
  // a spectator (nobody is played) has no marks to make
  const spectator = await live({ status: "running" });
  assert.doesNotMatch(html(spectator.env, "participants-grid"), /pcard-mark|data-mark-toggle/);
  assert.equal(spectator.env.app().marks().editable(), false);
});

test("the dot opens the palette (eight colours + 清除, no text); a colour marks the player, in this browser, and closes it", async () => {
  const { env, backend } = await livePlayed("N3");
  assert.equal(env.el("mark-pop").hidden, true);
  openPalette(env, "N4");
  assert.equal(env.el("mark-pop").hidden, false);
  assert.equal(env.el("mark-pop").getAttribute("data-actor"), "N4", "(roster.js: a click in here is not outside the detail panel)");
  const palette = html(env, "mark-pop");
  assert.equal((palette.match(/data-mark-set="\d"/g) || []).length, 8);
  assert.equal(text(palette), "✕");
  assert.match(palette, /data-mark-clear[^>]*disabled/, "nothing to clear yet");

  pick(env, "N4", 2);
  assert.equal(env.el("mark-pop").hidden, true, "a colour closes the palette");
  const entry = stored(env)["s-test"];
  assert.deepEqual(entry.marks, { N4: 2 });
  assert.equal(Number.isInteger(entry.updated_at), true);
  assert.ok(Math.abs(entry.updated_at - Date.now() / 1000) < 60, "its time is in seconds");
  assert.deepEqual(Object.keys(stored(env)), ["s-test"]);
  // the card: a stripe (class), the corner dot, the sheriff's badge makes room
  const c = card(env, "N4");
  assert.match(c, /class="pcard[^"]* has-mark mark-c2 mark-slot/);
  assert.match(c, /<span class="pcard-mark" data-mark-toggle="N4"[^>]*><span class="mark-dot mark-c2" role="img" aria-label="黄色标记" title="黄"><\/span><\/span>/);
  assert.doesNotMatch(card(env, "N5"), /has-mark/);
  // a second player, another colour; the palette shows the current one pressed
  mark(env, "N7", 5);
  assert.deepEqual(marksOfGame(env), { N4: 2, N7: 5 });
  openPalette(env, "N4");
  assert.match(html(env, "mark-pop"), /class="mark-swatch selected" data-mark-set="2" data-mark-actor="N4" aria-pressed="true"/);
  assert.doesNotMatch(html(env, "mark-pop"), /data-mark-clear[^>]*disabled/);
  // 清除
  wipe(env, "N4");
  assert.deepEqual(marksOfGame(env), { N7: 5 });
  assert.doesNotMatch(card(env, "N4"), /has-mark|mark-c2/);
  assert.match(card(env, "N4"), /pcard-mark is-empty/, "the ring is back");
  wipe(env, "N7");
  assert.deepEqual(stored(env), {}, "no marks: no entry");
  // it never goes anywhere
  assert.equal(backend.calls.filter((x) => x.method !== "GET").length, 0, "no request: the marks are the browser's");
  assert.ok(!JSON.stringify(env.app().state.session).includes('"marks"'), "and no part of the game");
});

test("the dot is not the card: pressing it does not open the detail panel, pressing the card does; Enter and Space on the dot open the palette", async () => {
  const { env } = await livePlayed("N3");
  const bothTargets = (id) => target({ "[data-mark-toggle]": { "data-mark-toggle": id }, ".pcard": { "data-actor": id } });
  env.el("participants-grid").dispatch("mousedown", { button: 0, target: bothTargets("N4") });
  env.el("participants-grid").dispatch("click", { target: bothTargets("N4") });
  env.flushRaf();
  assert.equal(env.app().roster().openId(), null, "the detail panel stays shut");
  assert.equal(env.el("mark-pop").hidden, false, "the palette is open - and the press that opened it is not undone by its own click");
  // (pressing it again closes it)
  env.el("participants-grid").dispatch("mousedown", { button: 0, target: bothTargets("N4") });
  env.el("participants-grid").dispatch("click", { target: bothTargets("N4") });
  assert.equal(env.el("mark-pop").hidden, true);
  // the card itself
  env.el("participants-grid").dispatch("click", { target: target({ ".pcard": { "data-actor": "N5" } }) });
  assert.equal(env.app().roster().openId(), "N5");
  env.app().closePopover();
  // the keyboard: the dot has the focus (it is a role=button with tabindex 0)
  let prevented = 0;
  for (const key of ["Enter", " "]) {
    env.el("participants-grid").dispatch("keydown", { key, target: bothTargets("N6"), preventDefault: () => (prevented += 1) });
    assert.equal(env.el("mark-pop").hidden, false, key);
    assert.equal(env.el("mark-pop").getAttribute("data-actor"), "N6");
    env.el("participants-grid").dispatch("keydown", { key, target: bothTargets("N6"), preventDefault: () => {} });
    assert.equal(env.el("mark-pop").hidden, true, `${key} again closes it`);
  }
  assert.equal(prevented, 2);
  assert.equal(env.app().roster().openId(), null, "(and the card's own Enter handler stayed out of it)");
  env.el("participants-grid").dispatch("keydown", { key: "Enter", target: target({ ".pcard": { "data-actor": "N5" } }), preventDefault: () => {} });
  assert.equal(env.app().roster().openId(), "N5");
});

test("the palette closes with Escape or a click outside - and answers Escape before the detail panel behind it", async () => {
  const { env } = await livePlayed("N3");
  openPalette(env, "N4");
  env.document.dispatchEvent("click", { target: target({ "#mark-pop": {} }) });
  assert.equal(env.el("mark-pop").hidden, false, "a click inside stays inside");
  env.document.dispatchEvent("click", { target: target({ "[data-mark-toggle]": { "data-mark-toggle": "N4" } }) });
  assert.equal(env.el("mark-pop").hidden, false, "so does the press on the dot that opened it");
  env.document.dispatchEvent("click", { target: target({}) });
  assert.equal(env.el("mark-pop").hidden, true, "outside: closed");

  openPalette(env, "N4");
  env.app().openPopover("N5"); // the detail panel is open as well
  let stopped = 0;
  env.document.dispatchEvent("keydown", { key: "Escape", stopImmediatePropagation: () => (stopped += 1) });
  assert.equal(env.el("mark-pop").hidden, true);
  assert.equal(stopped, 1, "the palette takes the Escape");
  assert.equal(env.app().roster().openId(), "N5", "the panel behind it is still there");
  env.document.dispatchEvent("keydown", { key: "Escape" });
  assert.equal(env.app().roster().openId(), null, "the next Escape closes the panel");
});

test("an open palette goes when the seat is given back or the page shows a replay; it cannot be opened then", async () => {
  const { env } = await livePlayed("N3", { status: "paused" });
  openPalette(env, "N4");
  assert.equal(env.el("mark-pop").hidden, false);
  env.sse("takeover.updated", { takeover: { seat: null, reshuffle_available: false } });
  assert.equal(env.el("mark-pop").hidden, true, "closed by the next paint");
  assert.equal(env.app().marks().openPalette("N4", null), false);
  assert.equal(env.app().marks().set("N4", 1), false, "and nothing can be marked");
  assert.deepEqual(stored(env), {});
});

test("a colour is for a player: not the moderator, not the seat itself, not an unknown id, not a colour that does not exist", async () => {
  const { env } = await livePlayed("N3");
  const marks = env.app().marks();
  for (const [id, color] of [["moderator", 1], ["N3", 1], ["N99", 1], ["", 1], ["N4", 8], ["N4", -1], ["N4", "red"], ["N4", 1.5]]) {
    assert.equal(marks.set(id, color), false, `${id}/${color}`);
  }
  assert.deepEqual(stored(env), {});
  assert.equal(marks.set("N4", 7), true);
  assert.equal(marks.set("N4", null), true);
  assert.equal(marks.clear("N5"), true, "clearing nothing is harmless");
  assert.deepEqual(stored(env), {});
});

// =====================================================================================================
// the page: where a mark shows
// =====================================================================================================
const msgOf = (env, from) => (feedHtml(env).match(new RegExp(`<article class="msg[^"]*" data-seq="\\d+" data-from="${from}">[\\s\\S]*?</article>`)) || [""])[0];

test("in the feed the speaker's name has the colour dot before it; a mark that changes repaints the messages drawn before it", async () => {
  const { env } = await livePlayed("N3");
  assert.doesNotMatch(msgOf(env, "N1"), /mark-dot/, "no mark yet");
  mark(env, "N1", 2);
  assert.match(msgOf(env, "N1"), /<div class="msg-head"><span class="mark-dot mark-c2" role="img" aria-label="黄色标记" title="黄"><\/span><span class="msg-name">Alice<\/span>/);
  assert.doesNotMatch(msgOf(env, "N2"), /mark-dot/, "the others stay as they were");
  assert.doesNotMatch(feedHtml(env).match(/<article class="msg msg-kind-announce[\s\S]*?<\/article>/)[0], /mark-dot/, "the moderator's announcement has no dot");
  mark(env, "N1", 6);
  assert.match(msgOf(env, "N1"), /mark-dot mark-c6/);
  assert.doesNotMatch(msgOf(env, "N1"), /mark-c2/);
  // a new message of the player comes with the dot
  env.sse("message.committed", F.msg(900, "public", "N1", "我再说一句。"));
  assert.equal((feedHtml(env).match(/mark-dot mark-c6/g) || []).length, 2);
  openPalette(env, "N1");
  wipe(env, "N1");
  assert.doesNotMatch(feedHtml(env), /mark-dot/, "cleared: gone from every message");
});

test("a request card's rows show the recipients' marks; a private chat of the god view, once the seat is given back, shows its player's", async () => {
  const request = F.makeRequest({ id: "r1", to: ["N3", "N6", "N10", "N12"], share_answers: true, seconds_left: 60, opened_seq: 6 });
  const { env } = await livePlayed("N3", { requests: [request] }, { requests: [F.seatRequest("r1", { channel_id: "wolves" })] });
  mark(env, "N6", 4);
  channelClick(env, "wolves");
  env.flushRaf();
  const rows = feedHtml(env).match(/<li class="req-row[\s\S]*?<\/li>/g);
  assert.equal(rows.length, 4);
  const row6 = rows.find((r) => r.includes("Frank"));
  assert.match(row6, /<img[^>]*class="avatar-img req-avatar"[^>]*><span class="mark-dot mark-c4"[^>]*><\/span><span class="req-name">Frank<\/span>/);
  assert.equal(rows.filter((r) => r.includes("mark-dot")).length, 1);

  // after the game: the seat is given back and the same game shows the mark in the private chat with that player
  env.sse("game.status", { status: "paused", reason: "user", winner: null });
  env.sse("takeover.updated", { takeover: { seat: null, reshuffle_available: false } });
  env.flushRaf();
  assert.equal(env.app().state.viewpoint, "god");
  env.el("channels-list").dispatch("click", { target: target({ "[data-dm-toggle]": {} }) }); // unfold the private chats
  env.flushRaf();
  const open = html(env, "channels-list");
  const dm6 = (open.match(/<div class="channel-item is-dm[^"]*"[^>]*data-channel="dm-N6"[\s\S]*?<\/span><\/div>/) || [""])[0];
  assert.match(dm6, /<span class="channel-name"><span class="mark-dot mark-c4"[^>]*><\/span>Frank<\/span>/);
  assert.match(dm6, /<span class="chan-ov ov-mark"><span class="mark-dot mark-c4"/, "(and on the avatar of the narrow rail)");
  const dm5 = (open.match(/<div class="channel-item is-dm[^"]*"[^>]*data-channel="dm-N5"[\s\S]*?<\/span><\/div>/) || [""])[0];
  assert.doesNotMatch(dm5, /mark/);
});

test("the marks of the detail panel's head: the palette while a seat is played, the dot afterwards, nothing for the seat itself and the moderator", async () => {
  const { env } = await livePlayed("N3");
  const marks = env.app().marks();
  assert.match(marks.headHtml("N4"), /^<div class="detail-marks" data-actor="N4"><div class="mark-palette"[^>]*>/);
  assert.equal((marks.headHtml("N4").match(/data-mark-set/g) || []).length, 8);
  assert.equal(marks.headHtml("N3"), "", "not on yourself");
  assert.equal(marks.headHtml("moderator"), "");
  assert.equal(marks.headHtml("N99"), "");
  mark(env, "N4", 3);
  assert.match(marks.headHtml("N4"), /class="mark-swatch selected" data-mark-set="3"/);
  // a click in the panel's palette marks, too (the palette is rendered by roster.js's head through ctx.markHeadHtml)
  env.el("detail-panel").dispatch("click", { target: target({ "[data-mark-set]": { "data-mark-set": "0", "data-mark-actor": "N5" } }) });
  env.flushRaf();
  assert.deepEqual(marksOfGame(env), { N4: 3, N5: 0 });
  env.el("detail-panel").dispatch("click", { target: target({ "[data-mark-clear]": { "data-mark-actor": "N5" } }) });
  assert.deepEqual(marksOfGame(env), { N4: 3 });
  // after the seat is given back: only the dot
  env.sse("game.status", { status: "paused", reason: "user", winner: null });
  env.sse("takeover.updated", { takeover: { seat: null, reshuffle_available: false } });
  assert.equal(marks.headHtml("N4"), '<div class="detail-marks readonly" data-actor="N4"><span class="mark-dot mark-c3" role="img" aria-label="绿色标记" title="绿"></span></div>');
  assert.equal(marks.headHtml("N5"), "", "unmarked and not editable: nothing");
});

test("the open detail panel carries the palette in its head and follows a mark made there (roster.js asks for it through ctx.markHeadHtml)", async () => {
  const { env } = await livePlayed("N3");
  env.app().openPopover("N4");
  env.flushRaf();
  const head = () => html(env, "detail-head");
  assert.match(head(), /<div class="detail-marks" data-actor="N4"><div class="mark-palette"/, "the palette is in the head");
  assert.equal((head().match(/data-mark-set="\d"/g) || []).length, 8);
  assert.doesNotMatch(head(), /mark-swatch selected/);
  // pressing a colour in the panel marks the player (the click is the panel's own, not the floating palette's)
  env.el("detail-panel").dispatch("click", { target: target({ "[data-mark-set]": { "data-mark-set": "4", "data-mark-actor": "N4" } }) });
  env.flushRaf();
  assert.deepEqual(marksOfGame(env), { N4: 4 });
  assert.match(head(), /class="mark-swatch selected" data-mark-set="4" data-mark-actor="N4" aria-pressed="true"/, "the head was drawn again");
  assert.equal(env.app().roster().openId(), "N4", "the panel stays open");
  assert.match(card(env, "N4"), /has-mark mark-c4/);
  // the moderator's panel has no palette; neither has the panel of the seat that is played
  env.app().openPopover("moderator");
  env.flushRaf();
  assert.doesNotMatch(head(), /mark-palette|detail-marks/);
  env.app().openPopover("N3");
  env.flushRaf();
  assert.doesNotMatch(head(), /mark-palette|detail-marks/);
  // a mark made from the card's own palette repaints the open panel, too
  env.app().openPopover("N5");
  env.flushRaf();
  mark(env, "N5", 1);
  assert.match(head(), /class="mark-swatch selected" data-mark-set="1" data-mark-actor="N5"/);
  // after the seat is given back the panel shows the colour only
  env.sse("game.status", { status: "paused", reason: "user", winner: null });
  env.sse("takeover.updated", { takeover: { seat: null, reshuffle_available: false } });
  env.flushRaf();
  assert.match(head(), /<div class="detail-marks readonly" data-actor="N5"><span class="mark-dot mark-c1"/);
  assert.doesNotMatch(head(), /mark-palette/);
});

// =====================================================================================================
// the page: when they show (G20), where they live (G19)
// =====================================================================================================
test("after the seat is given back the same game still shows its marks - read-only; a new game is blank; the old one is back with the game", async () => {
  const { env } = await livePlayed("N3", { status: "paused" });
  mark(env, "N1", 2);
  mark(env, "N7", 5);
  env.sse("takeover.updated", { takeover: { seat: null, reshuffle_available: false } });
  env.flushRaf();
  // read-only: the stripe and the dot stay, but nothing is a button
  assert.match(card(env, "N1"), /has-mark mark-c2/);
  assert.match(card(env, "N1"), /<span class="pcard-mark readonly"><span class="mark-dot mark-c2"/);
  assert.doesNotMatch(html(env, "participants-grid"), /data-mark-toggle/);
  assert.doesNotMatch(card(env, "N5"), /pcard-mark/, "unmarked players have no ring any more");
  assert.match(msgOf(env, "N1"), /mark-dot mark-c2/, "the feed keeps them");
  assert.equal(env.app().marks().editable(), false);
  assert.deepEqual(marksOfGame(env), { N1: 2, N7: 5 }, "and they are still stored");
  // taking another seat over, in the same game: they are editable again; the new seat itself shows none
  env.sse("takeover.updated", { takeover: F.makeTakeover("N1") });
  env.flushRaf();
  assert.doesNotMatch(card(env, "N1"), /has-mark|pcard-mark/, "N1 is played now: it cannot be marked (and shows no mark of its own)");
  assert.match(card(env, "N7"), /has-mark mark-c5/);
  assert.match(card(env, "N7"), /data-mark-toggle="N7"/);
  env.sse("takeover.updated", { takeover: F.makeTakeover("N3") });
  assert.match(card(env, "N1"), /has-mark mark-c2/, "(its mark was only hidden)");

  // a new game starts blank - the page shows another game id
  const next = F.makeSession({ session_id: "s-next", event_id: 700, ...playedOver("N3", { status: "idle" }) });
  env.sse("snapshot", { session: next }, 700);
  assert.doesNotMatch(card(env, "N1"), /has-mark/);
  assert.doesNotMatch(card(env, "N7"), /has-mark/);
  assert.doesNotMatch(feedHtml(env), /mark-dot/);
  mark(env, "N2", 1);
  assert.deepEqual(Object.keys(stored(env)).sort(), ["s-next", "s-test"]);
  assert.deepEqual(marksOfGame(env, "s-next"), { N2: 1 });
  assert.deepEqual(marksOfGame(env, "s-test"), { N1: 2, N7: 5 });
  // the old game is loaded again (a "continue" from the history): its marks are there
  env.sse("snapshot", { session: F.makeSession({ session_id: "s-test", event_id: 710, ...playedOver("N3", { status: "paused" }) }) }, 710);
  assert.match(card(env, "N1"), /has-mark mark-c2/);
  assert.doesNotMatch(card(env, "N2"), /has-mark/);
});

test("a replay shows no marks - not even of the game that is replayed - and has no buttons; leaving it brings them back", async () => {
  const { env } = await livePlayed("N3");
  mark(env, "N1", 2);
  assert.match(card(env, "N1"), /has-mark/);
  const live_ = env.app().state.session;
  // the replay of the same game (a saved game on screen)
  env.app().state.replay = { live: live_, installing: false };
  env.app().renderAll(true);
  assert.equal(env.app().marks().editable(), false);
  assert.equal(Object.keys(env.app().marks().marks()).length, 0, "(an object of the page's own realm: only its keys can be compared)");
  assert.equal(env.app().marks().signature(), "");
  assert.doesNotMatch(html(env, "participants-grid"), /has-mark|pcard-mark|mark-dot|data-mark-toggle/);
  assert.doesNotMatch(feedHtml(env), /mark-dot/);
  assert.equal(env.app().marks().set("N2", 3), false, "nothing is written either");
  assert.equal(env.app().marks().headHtml("N1"), "");
  env.app().state.replay = null;
  env.app().renderAll(true);
  assert.match(card(env, "N1"), /has-mark mark-c2/);
  assert.match(msgOf(env, "N1"), /mark-dot mark-c2/);
});

test("marks that are already in this browser are there from the first paint (and junk in the storage does no harm)", async () => {
  const seeded = JSON.stringify({ "s-test": { updated_at: 1763480000, marks: { N4: 2, N7: 5, moderator: 3, N3: 1 } }, other: { updated_at: 5, marks: { N1: 0 } } });
  const { env } = await livePlayed("N3", {}, {}, {}, { storage: { [KEY]: seeded } });
  assert.match(card(env, "N4"), /has-mark mark-c2/);
  assert.match(card(env, "N7"), /has-mark mark-c5/);
  assert.doesNotMatch(card(env, "N1"), /has-mark/, "another game's");
  assert.doesNotMatch(card(env, "N3"), /has-mark/, "a mark on the played seat is not shown");
  const junk = await livePlayed("N3", {}, {}, {}, { storage: { [KEY]: "}{ not json" } });
  assert.doesNotMatch(html(junk.env, "participants-grid"), /has-mark/);
  mark(junk.env, "N4", 1); // and it is overwritten by a sound value
  assert.deepEqual(marksOfGame(junk.env), { N4: 1 });
  // a spectator who never played a seat of this game sees what it holds (read-only)
  const watcher = await live({ status: "running" }, {}, { storage: { [KEY]: seeded } });
  assert.match(card(watcher.env, "N4"), /has-mark mark-c2/);
  assert.doesNotMatch(html(watcher.env, "participants-grid"), /data-mark-toggle/);
});

test("another tab changes the marks: the storage event re-reads them and repaints; other keys are ignored; the key being cleared too", async () => {
  const { env } = await livePlayed("N3");
  mark(env, "N1", 2);
  // (the other tab marks N4 and unmarks N1)
  env.storage.set(KEY, JSON.stringify({ "s-test": { updated_at: 1763480100, marks: { N4: 6 } } }));
  env.sandbox.dispatchEvent("storage", { key: "some_other_key" });
  env.flushRaf();
  assert.match(card(env, "N1"), /has-mark mark-c2/, "an unrelated key: nothing re-read");
  env.sandbox.dispatchEvent("storage", { key: KEY });
  env.flushRaf();
  assert.doesNotMatch(card(env, "N1"), /has-mark/);
  assert.match(card(env, "N4"), /has-mark mark-c6/);
  assert.doesNotMatch(msgOf(env, "N1"), /mark-dot/, "the feed follows");
  // the user clears the site data: the event comes with key = null
  env.storage.delete(KEY);
  env.sandbox.dispatchEvent("storage", { key: null });
  env.flushRaf();
  assert.doesNotMatch(html(env, "participants-grid"), /has-mark mark-c/);
  // and the next mark here is written on top of whatever another tab wrote meanwhile (not on this page's old picture)
  env.storage.set(KEY, JSON.stringify({ "s-test": { updated_at: 1763480200, marks: { N8: 7 } } }));
  mark(env, "N2", 1);
  assert.deepEqual(marksOfGame(env), { N8: 7, N2: 1 });
});

test("at most 50 games are kept; a game that is deleted from the history takes its marks along", async () => {
  const many = {};
  for (let i = 0; i < 50; i++) many[`game-${i}`] = { updated_at: 1000 + i, marks: { N1: i % 8 } };
  many["old-1"] = { updated_at: 5, marks: { N1: 2 } }; // the oldest of all: the history lists it, too
  const { env, backend } = await livePlayed("N3", {}, {}, {}, { storage: { [KEY]: JSON.stringify(many) } });
  assert.equal(Object.keys(stored(env)).length, 51, "(nothing is written until a mark is)");
  mark(env, "N4", 1);
  assert.equal(Object.keys(stored(env)).length, 50, "52 games would be too many: the two oldest went");
  assert.ok(!("old-1" in stored(env)) && !("game-0" in stored(env)), "the oldest two");
  assert.ok("s-test" in stored(env) && "game-1" in stored(env) && "game-49" in stored(env));

  // the history: deleting a game
  const seeded = { "old-2": { updated_at: 4000000000, marks: { N5: 3 } }, "s-test": stored(env)["s-test"] };
  env.storage.set(KEY, JSON.stringify(seeded));
  env.sandbox.dispatchEvent("storage", { key: KEY });
  const asked = [];
  const roster = env.app().roster();
  roster.openDialog = async (o) => {
    asked.push(o);
    return { ok: true, values: {} };
  };
  env.el("btn-open-history-modal").dispatch("click");
  await flush(env);
  env.el("history-list").dispatch("click", { target: target({ "[data-action]": { "data-action": "delete" }, ".history-item": { "data-session-id": "old-2" } }) });
  await flush(env);
  assert.equal(asked.length, 1);
  assert.ok(backend.calls.some((c) => c.method === "DELETE" && c.url.endsWith("/api/games/old-2")), "the game was deleted on the server");
  assert.deepEqual(Object.keys(stored(env)), ["s-test"], "its marks went with it");
  assert.deepEqual(marksOfGame(env), { N4: 1 }, "the live game's marks stay");
  // a failed delete keeps them
  env.storage.set(KEY, JSON.stringify({ "old-1": { updated_at: 1, marks: { N5: 3 } }, "s-test": { updated_at: 2, marks: { N4: 1 } } }));
  env.sandbox.dispatchEvent("storage", { key: KEY });
  backend.failures["DELETE /api/games/old-1"] = [409, "删不掉"];
  env.el("history-list").dispatch("click", { target: target({ "[data-action]": { "data-action": "delete" }, ".history-item": { "data-session-id": "old-1" } }) });
  await flush(env);
  assert.ok("old-1" in stored(env), "the game is still there: so are its marks");
});

test("a browser that blocks localStorage: the marks live as long as the page, and nothing breaks", async () => {
  const backend = H.makeBackend({ session: F.makeSession({ event_id: 500, ...playedOver("N3") }) });
  const env = H.makeEnv({ fetchImpl: backend.fetch });
  env.sandbox.localStorage = {
    getItem: () => {
      throw new Error("blocked");
    },
    setItem: () => {
      throw new Error("blocked");
    },
    removeItem: () => {},
  };
  env.xhrHooks.auto = backend.uploadHandler;
  env.run();
  await settle();
  env.flushRaf();
  env.runNextTimer();
  env.sse("snapshot", { session: backend.session }, 500);
  mark(env, "N4", 3);
  assert.match(card(env, "N4"), /has-mark mark-c3/);
  env.sandbox.dispatchEvent("storage", { key: KEY }); // (it re-reads what the page remembers)
  env.flushRaf();
  assert.match(card(env, "N4"), /has-mark mark-c3/);
  wipe(env, "N4");
  assert.doesNotMatch(card(env, "N4"), /has-mark/);
});

// =====================================================================================================
// the phone and the markup of the page
// =====================================================================================================
test("the page has the palette's container, the module with the common cache-buster, and the merged mark styles", () => {
  const page = H.HTML_SRC;
  assert.match(page, /<div class="mark-pop" id="mark-pop" role="dialog" aria-label="颜色标记" hidden><\/div>/);
  // (the cache-buster is the page's version, whatever it is: the same as the main stylesheet's and the script's)
  const buster = (page.match(/href="static\/css\/style\.css\?v=([0-9][0-9.]*)"/) || [])[1];
  assert.ok(buster, "the main stylesheet has a cache-buster");
  assert.ok(page.includes(`<script src="static/js/marks.js?v=${buster}"></script>`), "and so has the module");
  assert.ok(page.indexOf('static/js/marks.js') > page.indexOf("static/js/archive.js") && page.indexOf("static/js/marks.js") < page.indexOf("static/js/takeover.js"));
  const css = fs.readFileSync(path.join(H.ROOT, "static", "css", "style.css"), "utf8");
  const play = css.indexOf("Arena-Agents v0.13.1 — playing a seat, part 2");
  assert.ok(play > css.indexOf(".pcard.out {"), "the mark overrides come after the base card rules");
  // a finger can press them: a touch screen gets bigger targets
  assert.match(css, /html\[data-input="touch"\] \.mark-swatch,\s*html\[data-input="touch"\] \.mark-clear \{\s*width: 40px;\s*height: 40px;/);
  assert.match(css, /html\[data-input="touch"\] \.pcard-mark\[data-mark-toggle\]::after \{\s*inset: -7px;/);
});

test("a marked player who is out keeps the colour of the mark: the grey-out of the card moves to its content (G20: the marks are read after the game)", async () => {
  const css = fs.readFileSync(path.join(H.ROOT, "static", "css", "style.css"), "utf8");
  const { env } = await livePlayed("N3", { participants: F.makeParticipants({ N3: { state: "human" }, N4: { out: true, tags: ["预言家", "出局"] } }) });
  mark(env, "N4", 5);
  assert.match(card(env, "N4"), /class="pcard out[^"]* has-mark mark-c5/, "the card is out AND marked: the rule below applies to exactly these");
  assert.doesNotMatch(card(env, "N5"), /class="pcard out/);
  // style.css greys an `.out` card as a whole (opacity + grayscale on the card): that would take the mark's colour with it
  const base = fs.readFileSync(path.join(H.ROOT, "static", "css", "style.css"), "utf8");
  assert.match(base, /\.pcard\.out \{\s*opacity: 0\.55;\s*filter: grayscale\(1\);/);
  assert.match(css, /\.pcard\.out\.has-mark \{\s*opacity: 1;\s*filter: none;\s*\}/);
  assert.match(css, /\.pcard\.out\.has-mark > :not\(\.pcard-mark\) \{\s*opacity: 0\.55;\s*filter: grayscale\(1\);\s*\}/);
  // ... and it is still so after the game, when the seat is given back (read-only)
  env.sse("game.status", { status: "paused", reason: "user", winner: null });
  env.sse("takeover.updated", { takeover: { seat: null, reshuffle_available: false } });
  env.flushRaf();
  assert.match(card(env, "N4"), /class="pcard out[^"]* has-mark mark-c5/);
  assert.match(card(env, "N4"), /<span class="pcard-mark readonly"><span class="mark-dot mark-c5"/);
});

test("every class the marks markup puts on the page has a rule in style.css (a mistyped class would draw nothing - the fake DOM cannot tell)", async () => {
  const css = fs.readFileSync(path.join(H.ROOT, "static", "css", "style.css"), "utf8");
  const request = F.makeRequest({ id: "r1", to: ["N3", "N6", "N10", "N12"], share_answers: true, seconds_left: 60, opened_seq: 6 });
  const { env } = await livePlayed("N3", { requests: [request] }, { requests: [F.seatRequest("r1", { channel_id: "wolves" })] });
  const seen = new Set();
  const collect = (markup) => {
    for (const m of String(markup).matchAll(/class="([^"]+)"/g)) for (const c of m[1].split(/\s+/)) if (c) seen.add(c);
  };
  for (let color = 0; color < 8; color++) mark(env, `N${color + 4}`, color); // N4 .. N11: all eight colours
  mark(env, "N1", 0); // a speaker of the public channel
  openPalette(env, "N12"); // an unmarked player: the ring and a palette with nothing selected
  collect(html(env, "participants-grid"));
  collect(html(env, "mark-pop"));
  env.app().marks().closePalette();
  collect(feedHtml(env)); // the public channel: Alice's messages
  channelClick(env, "wolves");
  env.flushRaf();
  collect(feedHtml(env)); // the request card's rows
  env.app().openPopover("N5");
  env.flushRaf();
  collect(html(env, "detail-head"));
  // the seat is given back: read-only dots, and the god view's list of private chats shows them
  env.sse("game.status", { status: "paused", reason: "user", winner: null });
  env.sse("takeover.updated", { takeover: { seat: null, reshuffle_available: false } });
  env.el("channels-list").dispatch("click", { target: target({ "[data-dm-toggle]": {} }) });
  env.flushRaf();
  collect(html(env, "channels-list"));
  collect(html(env, "participants-grid"));
  collect(html(env, "detail-head"));

  const ours = [...seen].filter((c) => /^(mark-|pcard-mark$|has-mark$|detail-marks$|ov-mark$)/.test(c)).sort();
  for (let i = 0; i < 8; i++) assert.ok(ours.includes(`mark-c${i}`), `colour ${i} was drawn somewhere`);
  for (const must of ["pcard-mark", "mark-slot", "has-mark", "mark-ring", "mark-dot", "mark-palette", "mark-swatch", "mark-clear", "detail-marks", "ov-mark"]) {
    assert.ok(ours.includes(must), `${must} is part of the page's markup`);
  }
  const unstyled = ours.filter((c) => !css.includes(`.${c}`));
  assert.deepEqual(unstyled, [], "classes without any rule in style.css");
  // (the container of the floating palette is the page's own element)
  assert.ok(css.includes(".mark-pop"));
});

// -------------------------------------------------------------------------------------------------
H.runTests(tests);
