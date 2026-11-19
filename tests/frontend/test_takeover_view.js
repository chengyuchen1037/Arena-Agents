"use strict";
// Playing a seat (v0.13, F1-F19): the pure parts - the store's takeover state and `takeover.updated`, and the view
// model of the viewpoint bar, the takeover bar, the reshuffle reminder, the channel bar, the input box and the
// answer part of a request card (static/js/store.js, view.js).  The wiring to the page: test_takeover_ui.js.
// Run: node tests/frontend/test_takeover_view.js
const assert = require("node:assert/strict");
const path = require("node:path");
const Store = require(path.join(__dirname, "..", "..", "static", "js", "store.js"));
const View = require(path.join(__dirname, "..", "..", "static", "js", "view.js"));
const F = require("./fixtures.js");

const tests = [];
const test = (name, fn) => tests.push([name, fn]);
const GOD = View.GOD;

/** A normalized session in which N3 (Carol) is played. */
const played = (over = {}, takeoverOver = {}, seat = "N3") => Store.normalizeSession(F.makePlayedSession(seat, over, takeoverOver));
const plain = (over = {}) => Store.normalizeSession(F.makeSession(over));

// ---------------------------------------------------------------------------------------------- the store
test("the takeover of a session is normalized: nobody played -> only the seat and the re-deal flag count", () => {
  assert.deepEqual(Store.normalizeTakeover(undefined), {
    seat: null, reshuffle_available: false, seq: 0, channels: {}, requests: [], abilities: [], attention: [],
  });
  const junk = Store.normalizeTakeover({ seat: null, reshuffle_available: true, channels: { public: { can_write: true } }, attention: ["public"] });
  assert.equal(junk.seat, null);
  assert.equal(junk.reshuffle_available, true);
  assert.deepEqual([junk.channels, junk.attention], [{}, []], "state of a seat nobody plays is not kept");
  assert.equal(Store.emptySession().takeover.seat, null);
  assert.equal(plain().takeover.seat, null, "an older server without the field: nobody plays");
});

test("a played seat's state is read field by field (types, defaults, unknown fields dropped)", () => {
  const raw = F.makeTakeover("N3", {
    seq: "41",
    channels: {
      wolves: { can_write: true, why_not: null, code: null, max_chars: "300", floor: F.seatFloor({ kind: "queue", seconds_left: "12.5", deadline_clock: 130 }), can_pass: true },
    },
    requests: [F.seatRequest("r1", { answered: false, my_answer: { value: "N4", label: "4号 David", reason: "直觉", extra: 1 }, max_chars: 300 })],
    abilities: [F.explodeAbility()],
    attention: ["wolves", 7],
    bogus: true,
  });
  const t = Store.normalizeTakeover(raw);
  assert.equal(t.seq, 41);
  assert.deepEqual(Object.keys(t).sort(), ["abilities", "attention", "channels", "requests", "reshuffle_available", "seat", "seq"]);
  const wolves = t.channels.wolves;
  assert.deepEqual([wolves.can_write, wolves.max_chars, wolves.can_pass, wolves.code, wolves.why_not], [true, 300, true, null, null]);
  assert.deepEqual(wolves.floor, { kind: "queue", mine: true, paused: false, messages_left: 2, chars_left: 560, seconds_left: 12.5, deadline_clock: 130, untimed: false });
  assert.equal(t.channels["dm-N3"].floor, null);
  assert.equal(t.channels.public.floor.kind, "free");
  assert.deepEqual(t.requests[0], {
    id: "r1", kind: "choice", max_chars: 300, answered: true, my_answer: { value: "N4", label: "4号 David", reason: "直觉" },
    seconds_left: 90, deadline_clock: 102.5, channel_id: "public",
  });
  assert.deepEqual(t.abilities[0].params.map((p) => [p.name, p.type, p.max_chars || (p.candidates && p.candidates.length)]), [["target", "player_id", 12], ["declaration", "text", 120]]);
  assert.deepEqual(t.attention, ["wolves", "7"]);
});

test("takeover.updated: the seat is locked in, its card says human, the old seat sleeps again; a repeat is no change", () => {
  const s = plain();
  const first = Store.applyEvent(s, { id: 5, type: "takeover.updated", data: { takeover: F.makeTakeover("N3") } });
  assert.equal(first.takeoverChanged, true);
  assert.equal(first.session.takeover.seat, "N3");
  assert.equal(View.participantOf(s, "N3").state, "human");
  assert.deepEqual([first.dirty.board, first.dirty.channels, first.dirty.feed, first.dirty.topbar], [true, true, true, true]);

  const again = Store.applyEvent(s, { id: 6, type: "takeover.updated", data: { takeover: F.makeTakeover("N3", { attention: ["public"] }) } });
  assert.equal(again.takeoverChanged, false, "the same seat: only the state moved");
  assert.deepEqual(s.takeover.attention, ["public"]);

  const other = Store.applyEvent(s, { id: 7, type: "takeover.updated", data: { takeover: F.makeTakeover("N5") } });
  assert.equal(other.takeoverChanged, true);
  assert.deepEqual([View.participantOf(s, "N3").state, View.participantOf(s, "N5").state], ["sleeping", "human"]);

  const given = Store.applyEvent(s, { id: 8, type: "takeover.updated", data: { takeover: { seat: null, reshuffle_available: false } } });
  assert.equal(given.takeoverChanged, true);
  assert.equal(s.takeover.seat, null);
  assert.ok(s.participants.every((p) => p.state !== "human"));
  // an event without a payload is "nobody" as well, never an exception
  assert.doesNotThrow(() => Store.applyEvent(s, { id: 9, type: "takeover.updated", data: {} }));
});

test("agent.status never takes 'human' away from the seat a person plays (it has no agent)", () => {
  const s = played();
  const res = Store.applyEvent(s, { id: 9, type: "agent.status", data: { id: "N3", state: "thinking", until: null } });
  assert.equal(View.participantOf(s, "N3").state, "human");
  assert.equal(res.dirty.board, true);
  Store.applyEvent(s, { id: 10, type: "agent.status", data: { id: "N4", state: "thinking" } });
  assert.equal(View.participantOf(s, "N4").state, "thinking");
  // after the seat is given back it is an ordinary agent again
  Store.applyEvent(s, { id: 11, type: "takeover.updated", data: { takeover: { seat: null, reshuffle_available: false } } });
  Store.applyEvent(s, { id: 12, type: "agent.status", data: { id: "N3", state: "thinking" } });
  assert.equal(View.participantOf(s, "N3").state, "thinking");
});

test("a snapshot carries the played seat; session.updated leaves it alone", () => {
  const snap = Store.applyEvent(plain(), { id: 1, type: "snapshot", data: { session: F.makePlayedSession("N6") } });
  assert.equal(snap.replaced, true);
  assert.equal(snap.session.takeover.seat, "N6");
  assert.equal(View.participantOf(snap.session, "N6").state, "human");
  Store.applyEvent(snap.session, { id: 2, type: "session.updated", data: { status: "running" } });
  assert.equal(snap.session.takeover.seat, "N6");
  assert.equal(snap.session.status, "running");
});

// ----------------------------------------------------------------------------------- the viewpoint bar
test("viewpointBar: the drop-down only chooses; 切换 needs a different choice; 接管 needs a player and a game that is not running", () => {
  const s = plain({ status: "paused" });
  let bar = View.viewpointBar(s, GOD, GOD);
  assert.deepEqual([bar.locked, bar.canSwitch, bar.canTakeover], [false, false, false]);
  assert.match(bar.takeoverTitle, /上帝视角不能接管/);

  bar = View.viewpointBar(s, "N3", GOD);
  assert.deepEqual([bar.draft, bar.canSwitch, bar.canTakeover], ["N3", true, true]);
  assert.match(bar.takeoverTitle, /接管 3 · Carol/);
  assert.match(bar.takeoverTitle, /暂停/);

  bar = View.viewpointBar(s, "N3", "N3");
  assert.deepEqual([bar.canSwitch, bar.canTakeover], [false, true], "already looking from there; still may take the seat over");
  // an unknown draft falls back to god
  assert.equal(View.viewpointBar(s, "N99", GOD).draft, GOD);
  // the draft defaults to the applied viewpoint
  assert.equal(View.viewpointBar(s, undefined, "N4").draft, "N4");
});

test("takeover is possible only while the game has not started or is paused (F2); the tooltip says why not", () => {
  for (const [status, ok, text] of [
    ["idle", true, /对局未开始/],
    ["paused", true, /对局已暂停/],
    ["running", false, /对局进行中不能接管.*暂停/],
    ["finished", false, /对局已结束/],
  ]) {
    const check = View.takeoverAvailability(plain({ status }), "N3");
    assert.equal(check.ok, ok, status);
    assert.match(check.reason, text, status);
  }
  const s = plain({ status: "paused" });
  assert.equal(View.takeoverAvailability(s, "moderator").ok, false);
  assert.equal(View.takeoverAvailability(s, "N99").ok, false);
  assert.equal(View.takeoverAvailability(s, GOD).ok, false);
  const busy = View.takeoverAvailability(played(), "N4");
  assert.equal(busy.ok, false);
  assert.match(busy.reason, /请先退出接管/);
});

test("while a seat is played the viewpoint bar is locked to it and the takeover bar replaces it (F3)", () => {
  const s = played({ status: "paused" });
  const bar = View.viewpointBar(s, GOD, GOD);
  assert.deepEqual([bar.locked, bar.seat, bar.draft, bar.canSwitch, bar.canTakeover], [true, "N3", "N3", false, false]);
  const tb = View.takeoverBar(s);
  assert.deepEqual([tb.seat, tb.label, tb.text, tb.canRelease], ["N3", "3 · Carol", "🎮 正在扮演 3 · Carol", true]);
  assert.equal(View.takeoverBar(plain()), null);
  for (const status of ["idle", "paused", "finished"]) assert.equal(View.takeoverBar(played({ status })).canRelease, true, status);
  const running = View.takeoverBar(played({ status: "running" }));
  assert.equal(running.canRelease, false);
  assert.match(running.releaseTitle, /先点「暂停」/);
  assert.equal(View.takeoverSeat(s), "N3");
  assert.equal(View.takeoverSeat(plain()), null);
});

test("the reshuffle reminder shows only for a played seat in a game that has not started, and never after the start (F5)", () => {
  assert.equal(View.reshuffleReminder(played({ status: "idle" }, { reshuffle_available: true })).visible, true);
  assert.match(View.reshuffleReminder(played({ status: "idle" }, { reshuffle_available: true })).text, /你已经知道自己的身份，建议点击「重新发牌」/);
  assert.equal(View.reshuffleReminder(played({ status: "idle" }, { reshuffle_available: false })).visible, false, "the server says a start was recorded");
  for (const status of ["running", "paused", "finished"]) {
    assert.equal(View.reshuffleReminder(played({ status }, { reshuffle_available: true })).visible, false, status);
  }
  assert.equal(View.reshuffleReminder(plain({ status: "idle" })).visible, false, "nobody plays: nothing to remind of");
});

// ---------------------------------------------------------------------------------------- the card
test("the played seat's card says 人类操作中 (🎮); in a locked view only its own state is shown", () => {
  const s = played();
  const god = View.boardTiles(s, GOD);
  const mine = god.find((t) => t.id === "N3");
  assert.deepEqual([mine.state, mine.stateIcon, mine.stateTitle], ["human", "🎮", "人类操作中"]);
  assert.match(mine.title, /🎮 人类操作中/);
  assert.equal(god.find((t) => t.id === "N4").state, "sleeping");
  const locked = View.boardTiles(s, "N3");
  assert.equal(locked.find((t) => t.id === "N3").state, "human");
  assert.equal(locked.find((t) => t.id === "N4").state, "", "the others' states stay hidden");
  assert.equal(View.stateTitle("human", null, 0), "人类操作中");
});

// ---------------------------------------------------------------------------------------- channels
test("channel bar: the seat's own 禁言中 / 轮到你发言 come from the server's seat state, and the attention mark too (F12, F18)", () => {
  const s = played({ status: "running" }, {
    channels: {
      wolves: { can_write: false, code: "CHANNEL_CLOSED", why_not: "频道「狼人阵营」当前已关闭，只有主持人可以发言。", max_chars: null },
      public: { can_write: true, floor: F.seatFloor({ kind: "queue", messages_left: 1 }), max_chars: 300, can_pass: true },
    },
    attention: ["public"],
  });
  const by = (id, vp = "N3") => View.channelModel(s, vp, s.channels.find((c) => c.id === id));
  assert.deepEqual([by("public").summary.kind, by("public").summary.text, by("public").summary.detail], ["mine", "轮到你发言", "还可发 1 条"]);
  assert.equal(by("public").attention, true);
  assert.deepEqual([by("wolves").summary.kind, by("wolves").summary.icon, by("wolves").summary.text], ["muted", "🔇", "禁言中"]);
  assert.equal(by("wolves").attention, false);
  assert.equal(by("dm-N3").attention, false);
  // the free chat where the seat may write keeps the channel's own description
  const free = played({ status: "running" }, { channels: { public: { can_write: true, floor: F.seatFree() } } });
  assert.equal(View.channelModel(free, "N3", free.channels.find((c) => c.id === "public")).summary.kind, "free");
  // a muted channel keeps what it is: the queue progress follows "禁言中"
  s.channels.find((c) => c.id === "public").mode = "queue";
  s.takeover.channels.public.can_write = false;
  s.takeover.channels.public.floor.mine = false;
  const waiting = by("public");
  assert.equal(waiting.summary.kind, "muted");
  assert.match(waiting.summary.detail, /队列/);
  // a viewer that is not the played seat (the page is locked, but the function is pure) sees the channel as it is
  assert.equal(View.channelModel(s, GOD, s.channels.find((c) => c.id === "public")).attention, false);
  assert.equal(View.channelModel(s, GOD, s.channels.find((c) => c.id === "wolves")).summary.kind, "closed");
});

// ---------------------------------------------------------------------------------------- the input box
const model = (over, takeoverOver, channelId = "public", text = "") => View.composerModel(played(over, takeoverOver), channelId, text, { clockNow: 90 });

test("composer: nothing without a played seat or a channel", () => {
  assert.deepEqual(View.composerModel(plain(), "public", ""), { visible: false });
  assert.deepEqual(View.composerModel(played(), null, ""), { visible: false });
});

test("composer: the send button's five states (F14) - 发送 / 禁言中 / 已暂停 / 未开始 / 已结束 - and typing is always possible", () => {
  const speakable = { channels: { public: { can_write: true, max_chars: 300, floor: F.seatFree() } } };
  const send = model({ status: "running" }, speakable, "public", "大家好");
  assert.deepEqual([send.mode, send.label, send.sendEnabled], ["send", "发送", true]);
  assert.equal(model({ status: "running" }, speakable, "public", "").sendEnabled, false, "nothing to send");
  assert.equal(model({ status: "running" }, speakable, "public", "   ").sendEnabled, false, "only blanks");

  const muted = model({ status: "running" }, { channels: { public: { can_write: false, code: "NO_FLOOR", why_not: "还没轮到你发言，当前可以发言的是：Alice。", max_chars: null } } }, "public", "我想说");
  assert.deepEqual([muted.mode, muted.label, muted.sendEnabled], ["muted", "禁言中", false]);
  assert.equal(muted.reason, "还没轮到你发言，当前可以发言的是：Alice。", "the hover text is the server's reason");
  assert.equal(muted.hint, muted.reason, "and it is written under the box as well (a phone has no hover)");
  assert.equal(muted.count, 3, "the draft counts all the same");

  assert.deepEqual([model({ status: "paused" }, speakable, "public", "x").mode, model({ status: "paused" }, speakable, "public", "x").label], ["paused", "已暂停"]);
  assert.deepEqual([model({ status: "idle" }, speakable, "public", "x").mode, model({ status: "idle" }, speakable, "public", "x").label], ["idle", "未开始"]);
  assert.deepEqual([model({ status: "finished" }, speakable, "public", "x").mode, model({ status: "finished" }, speakable, "public", "x").label], ["finished", "已结束"]);
  for (const status of ["paused", "idle", "finished"]) assert.equal(model({ status }, speakable, "public", "x").sendEnabled, false, status);
  // a channel the server has not described yet (it was created a moment ago)
  const young = played({ status: "running" });
  delete young.takeover.channels.public;
  const unknown = View.composerModel(young, "public", "x", { clockNow: 0 });
  assert.deepEqual([unknown.mode, unknown.label, unknown.sendEnabled], ["muted", "禁言中", false]);
  assert.match(unknown.hint, /同步中/);
});

test("composer: the counter is 'n/max' with the server's limit; over the limit the tail is cut (not refused)", () => {
  const limited = { channels: { public: { can_write: true, max_chars: 10, floor: F.seatFree() } } };
  const m = model({ status: "running" }, limited, "public", "一二三四五六七八九十十一");
  assert.deepEqual([m.count, m.max, m.over, m.sendEnabled], [12, 10, 2, true]);
  assert.equal(View.countChars("😀😀 "), 2, "characters, not UTF-16 units; the ends are trimmed like the server does");
  assert.equal(View.countChars(null), 0);
  const free = model({ status: "running" }, { channels: { public: { can_write: true, max_chars: null } } }, "public", "abc");
  assert.deepEqual([free.max, free.over], [null, 0]);
});

test("composer: 结束发言 shows only while the seat holds that channel's floor; 技能 is grey without an enabled ability (F15, F16)", () => {
  const mine = { channels: { public: { can_write: true, max_chars: 300, can_pass: true, floor: F.seatFloor() } } };
  let m = model({ status: "running" }, mine);
  assert.deepEqual([m.showPass, m.canPass, m.abilityEnabled], [true, true, false]);
  assert.match(m.abilityReason, /没有可用的技能/);
  m = model({ status: "running" }, { ...mine, abilities: [F.explodeAbility()] });
  assert.deepEqual([m.abilityEnabled, m.abilityReason], [true, "技能：释放前会让你确认"]);
  // not my floor: no button; free discussion has none either
  m = model({ status: "running" }, { channels: { public: { can_write: true, max_chars: 300, floor: F.seatFloor({ mine: false }) } } });
  assert.equal(m.showPass, false);
  m = model({ status: "running" }, { channels: { public: { can_write: true, max_chars: 300, floor: F.seatFree() } } });
  assert.equal(m.showPass, false);
  // my floor, but paused / frozen: the button is there and disabled, the title says why
  m = model({ status: "running" }, { channels: { public: { can_write: false, can_pass: false, max_chars: null, code: "NO_FLOOR", why_not: "发言权已暂停，请等待主持人恢复。", floor: F.seatFloor({ paused: true }) } } });
  assert.deepEqual([m.showPass, m.canPass], [true, false]);
  assert.match(m.passTitle, /发言权已暂停或频道被冻结/);
  // a game that is not running: no skill, no pass
  m = model({ status: "paused" }, { ...mine, abilities: [F.explodeAbility()] });
  assert.deepEqual([m.canPass, m.abilityEnabled], [false, false]);
  assert.match(m.abilityReason, /对局已暂停/);
  assert.match(m.passTitle, /对局已暂停/);
});

test("composer hint: what is left of the floor, with a countdown on the client's game clock", () => {
  const mine = { channels: { public: { can_write: true, max_chars: 300, can_pass: true, floor: F.seatFloor({ messages_left: 2, deadline_clock: 155 }) } } };
  const m = View.composerModel(played({ status: "running" }, mine), "public", "x", { clockNow: 90 });
  assert.equal(m.hint, "轮到你发言 · 还可发 2 条 · 单条 ≤ 300 字 · 剩余 1:05");
  assert.equal(View.composerModel(played({ status: "running" }, mine), "public", "x", { clockNow: 400 }).hint.endsWith("剩余 0:00"), true, "never negative");
  // a paused floor has no deadline: the frozen seconds are shown
  const frozen = { channels: { public: { can_write: true, max_chars: 300, floor: F.seatFloor({ deadline_clock: null, seconds_left: 42.2, paused: false }) } } };
  assert.match(View.composerModel(played({ status: "running" }, frozen), "public", "x", { clockNow: 90 }).hint, /剩余 0:43/);
  assert.equal(View.floorSecondsLeft(F.seatFloor({ mine: false }), 0), null);
  assert.equal(View.floorSecondsLeft(null, 0), null);
  assert.equal(View.clockText(65), "1:05");
  const free = View.composerModel(played({ status: "running" }, { channels: { public: { can_write: true, max_chars: 300, floor: F.seatFree({ messages_left: 3 }) } } }), "public", "", { clockNow: 0 });
  assert.equal(free.hint, "这次讨论你还可发 3 条");
  assert.match(View.composerModel(played({ status: "idle" }), "public", "").hint, /对局还没有开始/);
  assert.match(View.composerModel(played({ status: "paused" }), "public", "").hint, /对局已暂停/);
  assert.match(View.composerModel(played({ status: "finished" }), "public", "").hint, /对局已结束/);
});

// ---------------------------------------------------------------------------------- request cards (F17)
const withRequests = (status, reqs, takeoverRequests, seat = "N3") => {
  const s = played({ status }, { requests: takeoverRequests });
  s.requests = reqs.map((r) => Store.normalizeRequest(F.makeRequest(r), { clockNow: 90 }));
  return s;
};
const card = (s, id = "r1", viewpoint = "N3") => View.requestCard(s, viewpoint, s.requests.find((r) => r.id === id), { clockNow: 100 });

test("request card action: only a recipient, only an open request, only for the played seat; clickable only while the game runs", () => {
  const rows = [F.seatRequest("r1")];
  const running = withRequests("running", [{ id: "r1", to: ["N3", "N6"] }], rows);
  const action = card(running).action;
  assert.deepEqual([action.kind, action.enabled, action.answered, action.disabledReason], ["choice", true, false, ""]);
  assert.deepEqual(action.options.map((o) => [o.id, o.label, o.selected]), [["N1", "1号 Alice", false], ["N4", "4号 David", false], ["N5", "5号 Eve", false]]);

  const paused = card(withRequests("paused", [{ id: "r1", to: ["N3", "N6"] }], rows)).action;
  assert.equal(paused.enabled, false);
  assert.match(paused.disabledReason, /对局已暂停.*计时也已暂停/);
  assert.match(card(withRequests("idle", [{ id: "r1", to: ["N3"] }], rows)).action.disabledReason, /对局还没有开始/);
  assert.match(card(withRequests("finished", [{ id: "r1", to: ["N3"] }], rows)).action.disabledReason, /对局已结束/);

  // not a recipient / a closed request / another seat's view / nobody plays: no action at all
  assert.equal(card(withRequests("running", [{ id: "r1", to: ["N6", "N10"] }], rows), "r1", GOD).action, null);
  assert.equal(card(withRequests("running", [{ id: "r1", to: ["N6", "N10"] }], rows), "r1", "N3").action, null);
  assert.equal(card(withRequests("running", [{ id: "r1", to: ["N3"], status: "closed", seconds_left: null }], rows)).action, null);
  assert.equal(card(withRequests("running", [{ id: "r1", to: ["N3"] }], rows), "r1", GOD).action, null, "the god view has nothing to answer");
  const spectator = plain({ status: "running", requests: [F.makeRequest({ id: "r1", to: ["N3"] })] });
  assert.equal(View.requestCard(spectator, GOD, spectator.requests[0], { clockNow: 0 }).action, null);
});

test("request card action: my answer is marked and may be changed; a text request has an input with the server's limit", () => {
  const mine = F.seatRequest("r1", { answered: true, my_answer: { value: "N4", label: "4号 David", reason: "" } });
  const s = withRequests("running", [{ id: "r1", to: ["N3", "N6"], answers: [F.answer("N3", "N4", "4号 David")] }], [mine]);
  const a = card(s).action;
  assert.equal(a.answered, true);
  assert.deepEqual(a.myAnswer, { value: "N4", label: "4号 David", reason: "" });
  assert.deepEqual(a.options.filter((o) => o.selected).map((o) => o.id), ["N4"]);
  // the answer is known from the request itself too (a session adopted before the first push)
  const bare = withRequests("running", [{ id: "r1", to: ["N3", "N6"], answers: [F.answer("N3", "N5", "5号 Eve")] }], []);
  assert.deepEqual(card(bare).action.options.filter((o) => o.selected).map((o) => o.id), ["N5"]);
  assert.equal(card(bare).action.answered, true);

  const text = withRequests("running", [{ id: "r2", kind: "text", options: [], question: "留下遗言", to: ["N3"], share_answers: false, auto_close: "all_answered" }], [F.seatRequest("r2", { kind: "text", max_chars: 300 })]);
  const t = card(text, "r2").action;
  assert.deepEqual([t.kind, t.maxChars, t.options, t.enabled], ["text", 300, [], true]);
});

test("requestPick (G45): a pick is not an answer; what the card says about it, and whether 「提交」 can be pressed", () => {
  const fresh = card(withRequests("running", [{ id: "r1", to: ["N3", "N6"] }], [F.seatRequest("r1")])).action;
  const sum = (p) => [p.picked, p.pickedLabel, p.currentLabel, p.dirty, p.canSubmit, p.submitLabel, p.kind];
  // nothing picked (or something that is not an option): nothing to say, the button is grey
  assert.deepEqual(sum(View.requestPick(fresh, null)), [null, "", "", false, false, "提交", "none"]);
  assert.deepEqual(sum(View.requestPick(fresh, undefined)), [null, "", "", false, false, "提交", "none"]);
  assert.deepEqual(sum(View.requestPick(fresh, "N99")), [null, "", "", false, false, "提交", "none"]);
  // picked and not answered yet: a new answer
  assert.deepEqual(sum(View.requestPick(fresh, "N4")), ["N4", "4号 David", "", true, true, "提交", "new"]);
  assert.equal(View.requestPick(fresh, 4).picked, null, "ids are compared as strings: 4 is not N4");

  // answered: the button reads 修改回答; picking the submitted choice again is no change; another one is a change
  const mine = F.seatRequest("r1", { answered: true, my_answer: { value: "N4", label: "4号 David", reason: "" } });
  const answered = card(withRequests("running", [{ id: "r1", to: ["N3", "N6"], answers: [F.answer("N3", "N4", "4号 David")] }], [mine])).action;
  assert.deepEqual(sum(View.requestPick(answered, null)), [null, "", "", false, false, "修改回答", "none"]);
  assert.deepEqual(sum(View.requestPick(answered, "N4")), ["N4", "4号 David", "4号 David", false, false, "修改回答", "none"]);
  assert.deepEqual(sum(View.requestPick(answered, "N5")), ["N5", "5号 Eve", "4号 David", true, true, "修改回答", "change"]);

  // a frozen card (paused) can hold a pick but never be pressed; a text request and no action have no picks at all
  const paused = card(withRequests("paused", [{ id: "r1", to: ["N3", "N6"] }], [F.seatRequest("r1")])).action;
  const frozen = View.requestPick(paused, "N4");
  assert.deepEqual([frozen.picked, frozen.dirty, frozen.canSubmit], ["N4", true, false]);
  const text = card(withRequests("running", [{ id: "r2", kind: "text", options: [], question: "留下遗言", to: ["N3"], share_answers: false, auto_close: "all_answered" }], [F.seatRequest("r2", { kind: "text", max_chars: 300 })]), "r2").action;
  assert.deepEqual(sum(View.requestPick(text, "N4")), [null, "", "", false, false, "提交", "none"]);
  assert.deepEqual(sum(View.requestPick(null, "N4")), [null, "", "", false, false, "提交", "none"]);
});

test("request card: the countdown follows the client's game clock; a request the seat answered is no longer 'waiting' for it", () => {
  const s = withRequests("running", [{ id: "r1", to: ["N3", "N6"], seconds_left: 30 }], [F.seatRequest("r1")]);
  assert.equal(card(s).countdown, 20, "deadline_clock 120 (90 + 30) at the clock 100");
  assert.equal(View.requestCard(s, "N3", s.requests[0], { clockNow: 500 }).countdown, 0);
});

// ---------------------------------------------------------------------------------------------- runner
(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log(`ok   - ${name}`);
    } catch (e) {
      failed++;
      console.log(`FAIL - ${name}\n${e && e.stack ? e.stack : e}`);
    }
  }
  console.log(`\n${tests.length - failed}/${tests.length} passed`);
  process.exit(failed ? 1 : 0);
})();
