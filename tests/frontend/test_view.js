"use strict";
// View model: spectator viewpoint, tiles, channel bar, requests, controls (static/js/view.js).
// Run: node tests/frontend/test_view.js
const assert = require("node:assert/strict");
const path = require("node:path");
const Store = require(path.join(__dirname, "..", "..", "static", "js", "store.js"));
const View = require(path.join(__dirname, "..", "..", "static", "js", "view.js"));
const F = require("./fixtures.js");

const tests = [];
const test = (name, fn) => tests.push([name, fn]);
const fresh = (over) => Store.normalizeSession(F.makeSession(over));
const GOD = View.GOD;

// ---- viewpoint: options -----------------------------------------------------------------
test("viewpoint options: god first, then every player; unknown viewpoints fall back to god", () => {
  const s = fresh();
  const opts = View.viewpointOptions(s);
  assert.equal(opts.length, 13);
  assert.deepEqual(opts[0], { id: "god", label: "上帝视角" });
  assert.deepEqual(opts[3], { id: "p03", label: "p03 · Carol 3" });
  assert.equal(View.normalizeViewpoint(s, "p03"), "p03");
  assert.equal(View.normalizeViewpoint(s, "ghost"), "god");
  assert.equal(View.normalizeViewpoint(s, ""), "god");
  assert.equal(View.normalizeViewpoint(s, undefined), "god");
  assert.equal(View.viewpointLabel(s, "p03"), "p03 · Carol 3");
  assert.equal(View.viewpointLabel(s, "ghost"), "上帝视角");
});

// ---- viewpoint: messages ----------------------------------------------------------------
test("god sees every message; a player sees only those whose visible_to_ids contain them", () => {
  const s = fresh();
  assert.equal(View.visibleMessages(s, GOD).length, 6);
  const p01 = View.visibleMessages(s, "p01");
  assert.deepEqual(p01.map((m) => m.seq), [3, 8, 12]); // not the wolves chat, not p03's DM
  const p03 = View.visibleMessages(s, "p03");
  assert.deepEqual(p03.map((m) => m.seq), [3, 5, 8, 9, 11, 12]);
  // a channel filter composes with the viewpoint
  assert.deepEqual(View.visibleMessages(s, "p03", "wolves").map((m) => m.seq), [5, 9]);
  assert.deepEqual(View.visibleMessages(s, "p01", "wolves"), []);
  assert.deepEqual(View.visibleMessages(s, GOD, "dm-p03").map((m) => m.seq), [11]);
  // empty visible_to_ids (a sealed entry) is god-only
  s.messages.push(Store.normalizeMessage(F.msg(20, "public", "p05", "密封内容", "speech", [])));
  assert.equal(View.visibleMessages(s, GOD).length, 7);
  assert.equal(View.visibleMessages(s, "p05").some((m) => m.seq === 20), false);
});

test("switching the viewpoint is pure: it never mutates the session", () => {
  const s = fresh();
  const before = JSON.stringify(s);
  View.visibleMessages(s, "p01");
  View.visibleChannels(s, "p01");
  View.boardTiles(s, "p01");
  View.channelGroups(s, "p07");
  assert.equal(JSON.stringify(s), before);
});

// ---- viewpoint: channels ----------------------------------------------------------------
test("a player only lists the channels they are a member of; DMs are grouped apart", () => {
  const s = fresh();
  assert.equal(View.visibleChannels(s, GOD).length, 14);
  assert.deepEqual(View.visibleChannels(s, "p01").map((c) => c.id), ["public", "dm-p01"]);
  assert.deepEqual(View.visibleChannels(s, "p03").map((c) => c.id), ["public", "wolves", "dm-p03"]);
  const g = View.channelGroups(s, GOD);
  assert.deepEqual(g.groups.map((c) => c.id), ["public", "wolves"]);
  assert.equal(g.dms.length, 12);
  const mine = View.channelGroups(s, "p03");
  assert.deepEqual(mine.dms.map((c) => c.id), ["dm-p03"]);
  assert.equal(View.normalizeSelectedChannel(s, "p01", "wolves"), "all"); // not visible -> all
  assert.equal(View.normalizeSelectedChannel(s, "p03", "wolves"), "wolves");
  assert.equal(View.normalizeSelectedChannel(s, GOD, "ghost"), "all");
  assert.equal(View.normalizeSelectedChannel(s, GOD, "all"), "all");
  assert.equal(View.normalizeSelectedChannel(s, GOD, ""), "all");
});

// ---- tags -----------------------------------------------------------------------------------
test("tags: god sees all; a player sees public tags of others and every tag of themself", () => {
  const s = fresh();
  const p03 = View.participantOf(s, "p03");
  const p04 = View.participantOf(s, "p04");
  assert.deepEqual(View.visibleTags(s, GOD, p03), ["狼人", "存活"]);
  assert.deepEqual(View.visibleTags(s, "p04", p03), ["存活"]); // role hidden from others
  assert.deepEqual(View.visibleTags(s, "p03", p03), ["狼人", "存活"]); // I know my own role
  assert.deepEqual(View.visibleTags(s, "p04", p04), ["预言家", "存活"]);
  p03.tags.push("警长");
  assert.deepEqual(View.visibleTags(s, "p04", p03), ["存活", "警长"]); // public_tags include 警长
  s.public_tags = [];
  assert.deepEqual(View.visibleTags(s, "p04", p03), []);
});

test("main tag: highlight_tags first, then a non-status tag, then anything", () => {
  const ui = { highlight_tags: ["警长", "白狼王"], status_tags: ["存活", "出局"], out_tags: ["出局"] };
  assert.equal(View.pickMainTag(["平民", "存活", "警长"], ui), "警长");
  assert.equal(View.pickMainTag(["白狼王", "存活"], ui), "白狼王");
  assert.equal(View.pickMainTag(["警长", "白狼王"], ui), "警长"); // earlier highlight wins
  assert.equal(View.pickMainTag(["存活", "狼人"], ui), "狼人");
  assert.equal(View.pickMainTag(["存活"], ui), "存活"); // only a status tag is left
  assert.equal(View.pickMainTag(["出局", "存活"], ui), "出局");
  assert.equal(View.pickMainTag([], ui), "");
  assert.equal(View.pickMainTag(["a", "b"], undefined), "a");
  assert.equal(View.pickMainTag(["a", "b"], {}), "a");
});

test("tag variants come from tag_styles; out tags are muted; the rest neutral", () => {
  const s = fresh();
  assert.equal(View.tagVariant(s, "狼人"), "danger");
  assert.equal(View.tagVariant(s, "预言家"), "purple");
  assert.equal(View.tagVariant(s, "出局"), "muted");
  assert.equal(View.tagVariant(s, "平民"), "neutral");
  s.tag_styles["怪异"] = { variant: "not-a-variant" };
  assert.equal(View.tagVariant(s, "怪异"), "neutral");
});

// ---- tiles ---------------------------------------------------------------------------------
test("board tiles: moderator first, 12 players, one main tag each, out players flagged", () => {
  const s = fresh();
  View.participantOf(s, "p05").tags = ["女巫", "出局"];
  View.participantOf(s, "p05").out = true;
  const tiles = View.boardTiles(s, GOD);
  assert.equal(tiles.length, 13);
  assert.equal(tiles[0].isModerator, true);
  assert.equal(tiles[0].mainTag, "主持人");
  assert.deepEqual(tiles.slice(1).map((t) => t.id), F.PLAYER_IDS);
  const byId = Object.fromEntries(tiles.map((t) => [t.id, t]));
  assert.equal(byId.p03.mainTag, "狼人");
  assert.equal(byId.p03.variant, "danger");
  assert.equal(byId.p05.mainTag, "女巫");
  assert.equal(byId.p05.out, true);
  assert.equal(byId.p01.out, false);
  assert.match(byId.p03.title, /p03 · Carol 3/);
  // the viewed player is marked
  assert.equal(View.boardTiles(s, "p03").find((t) => t.id === "p03").viewing, true);
  assert.equal(View.boardTiles(s, "p03").find((t) => t.id === "p04").viewing, false);
});

test("player viewpoint: other tiles only show public tags (the role never leaks)", () => {
  const s = fresh();
  const tiles = View.boardTiles(s, "p04"); // the seer's point of view
  const byId = Object.fromEntries(tiles.map((t) => [t.id, t]));
  assert.equal(byId.p04.mainTag, "预言家"); // own role is known
  assert.equal(byId.p03.mainTag, "存活"); // a wolf shows only "存活"
  assert.deepEqual(byId.p03.tags, ["存活"]);
  for (const id of F.PLAYER_IDS.filter((x) => x !== "p04")) {
    assert.ok(!byId[id].tags.some((t) => ["狼人", "平民", "女巫", "猎人", "守卫", "白狼王", "预言家"].includes(t)), id);
  }
  // god view of the same game shows the roles
  assert.equal(View.boardTiles(s, GOD).find((t) => t.id === "p03").mainTag, "狼人");
});

test("state badges: thinking / acting / stopped for god; others stay hidden in a player's view", () => {
  const s = fresh();
  View.participantOf(s, "p01").state = "thinking";
  View.participantOf(s, "p02").state = "acting";
  View.participantOf(s, "p03").state = "stopped";
  s.moderator.state = "thinking";
  const god = Object.fromEntries(View.boardTiles(s, GOD).map((t) => [t.id, t]));
  assert.equal(god.p01.state, "thinking");
  assert.equal(god.p02.state, "acting");
  assert.equal(god.p03.state, "stopped");
  assert.equal(god.p04.state, "");
  assert.equal(god.moderator.state, "thinking");
  const p02 = Object.fromEntries(View.boardTiles(s, "p02").map((t) => [t.id, t]));
  assert.equal(p02.p02.state, "acting"); // my own state is shown
  assert.equal(p02.p01.state, ""); // thinking of others would leak who is active
  assert.equal(p02.moderator.state, "");
  assert.match(god.p01.title, /思考中/);
});

test("flow badges: floor holder from the viewpoint's channels; waiting for an answer", () => {
  const s = fresh();
  const pub = Store.findById(s.channels, "public");
  pub.mode = "queue";
  pub.floor = Store.normalizeFloor({
    kind: "queue",
    active: "p05",
    queue: [
      { holder: "p04", state: "done" },
      { holder: "p05", state: "active" },
      { holder: "p06", state: "pending" },
    ],
  });
  const wolves = Store.findById(s.channels, "wolves");
  wolves.mode = "turn";
  wolves.floor = Store.normalizeFloor({ kind: "turn", active: "p03", queue: [{ holder: "p03", state: "active" }, { holder: "p06", state: "active" }] });
  s.requests = [
    Store.normalizeRequest({ id: "r1", kind: "choice", to: ["p01", "p02", "p05"], answered_by: ["p02"], status: "open" }),
    Store.normalizeRequest({ id: "r0", kind: "choice", to: ["p07"], answered_by: [], status: "closed" }),
  ];
  const god = Object.fromEntries(View.boardTiles(s, GOD).map((t) => [t.id, t]));
  assert.equal(god.p05.flow, "floor"); // holds the public queue token
  assert.equal(god.p03.flow, "floor"); // turn group: several holders
  assert.equal(god.p06.flow, "floor");
  assert.equal(god.p04.flow, ""); // already done
  assert.equal(god.p01.flow, "waiting"); // unanswered open request
  assert.equal(god.p02.flow, ""); // answered
  assert.equal(god.p07.flow, ""); // closed request: nothing to wait for
  // p01 only sees the public channel and its own request
  const p01 = Object.fromEntries(View.boardTiles(s, "p01").map((t) => [t.id, t]));
  assert.equal(p01.p05.flow, "floor");
  assert.equal(p01.p03.flow, ""); // the wolves channel is invisible to p01
  assert.equal(p01.p01.flow, "waiting");
  assert.equal(p01.p05.flow === "waiting", false);
  // a frozen or paused floor holds nobody
  pub.frozen = true;
  assert.equal(View.flowState(s, GOD).holders.has("p05"), false);
  pub.frozen = false;
  pub.floor.paused = true;
  assert.equal(View.flowState(s, GOD).holders.has("p05"), false);
});

// ---- channel bar: floor status ---------------------------------------------------------------
test("floorSummary: 🔒 closed · 🎤 turn · 📋 queue · 💬 free · 👁 listen · ❄ frozen", () => {
  const s = fresh();
  const ch = (mode, floor, extra = {}) => ({ ...Store.normalizeChannel({ id: "c", mode, floor, ...extra }) });
  assert.deepEqual(View.floorSummary(s, ch("closed")), { kind: "closed", icon: "🔒", text: "已关闭", detail: "" });
  assert.equal(View.floorSummary(s, ch("listen")).icon, "👁");
  const free = View.floorSummary(s, ch("free", { kind: "free", free: { chars_used: 120, max_chars_total: 2000 } }));
  assert.equal(free.icon, "💬");
  assert.equal(free.detail, "120/2000 字");
  assert.equal(View.floorSummary(s, ch("free")).detail, "");

  const turn = View.floorSummary(s, ch("turn", { kind: "turn", queue: [{ holder: "p05", state: "active" }, { holder: "p06", state: "active" }] }));
  assert.equal(turn.icon, "🎤");
  assert.match(turn.detail, /p05 · Eve 5、p06 · Frank 6/);
  const many = View.floorSummary(s, ch("turn", { kind: "turn", queue: ["p01", "p02", "p03"].map((h) => ({ holder: h, state: "active" })) }));
  assert.match(many.detail, /等3人/);
  assert.match(View.floorSummary(s, ch("turn", { kind: "turn", paused: true })).text, /暂停/);

  const queue = View.floorSummary(
    s,
    ch("queue", {
      kind: "queue",
      queue: [
        { holder: "p01", state: "done" },
        { holder: "p02", state: "done" },
        { holder: "p03", state: "active" },
        { holder: "p04", state: "pending" },
      ],
    })
  );
  assert.equal(queue.icon, "📋");
  assert.equal(queue.text, "队列 2/4");
  assert.match(queue.detail, /p03 · Carol 3/);

  const frozen = View.floorSummary(s, ch("queue", { kind: "queue" }, { frozen: true }));
  assert.equal(frozen.icon, "❄"); // frozen wins over the mode
  assert.equal(View.floorSummary(s, ch("weird")).kind, "none");
  assert.equal(View.floorSummary(s, null).kind, "none");
});

// ---- unread -----------------------------------------------------------------------------------
test("unread counts: above baseline, above the all-watermark and the channel watermark", () => {
  const s = fresh();
  const read = { baseline: 9, all: 0, seqs: {} }; // history up to seq 9 is read
  let u = View.unreadCounts(s, GOD, read);
  assert.deepEqual(u.byChannel, { "dm-p03": 1, public: 1 }); // seq 11 (dm-p03) and 12 (public)
  assert.equal(u.total, 2);
  u = View.unreadCounts(s, GOD, { baseline: 9, all: 11, seqs: {} });
  assert.deepEqual(u.byChannel, { public: 1 });
  u = View.unreadCounts(s, GOD, { baseline: 9, all: 0, seqs: { public: 12 } });
  assert.deepEqual(u.byChannel, { "dm-p03": 1 });
  // the viewpoint filters first: p01 cannot see dm-p03
  u = View.unreadCounts(s, "p01", read);
  assert.deepEqual(u.byChannel, { public: 1 });
  u = View.unreadCounts(s, GOD, { baseline: 100 });
  assert.equal(u.total, 0);
  assert.equal(View.unreadCounts(s, GOD, undefined).total, 6); // nothing read yet
  assert.equal(View.latestSeq(s, "public"), 12);
  assert.equal(View.latestSeq(s, "wolves"), 9);
  assert.equal(View.latestSeq(s, "all"), 12);
  assert.equal(View.latestSeq(s, "nothing"), 0);
});

// ---- tickets ------------------------------------------------------------------------------------
test("tickets: all for god, own for a player; click target is the player's system DM", () => {
  const s = fresh();
  s.tickets.push(Store.normalizeTicket({ id: "t2", player_id: "p08", status: "resolved", verdict: "upheld", preview: "x" }));
  assert.equal(View.visibleTickets(s, GOD).length, 2);
  assert.deepEqual(View.visibleTickets(s, "p08").map((t) => t.id), ["t2"]);
  assert.deepEqual(View.visibleTickets(s, "p01"), []);
  assert.equal(View.ticketChannelId(s, s.tickets[0]), "dm-p03");
  // a differently named DM is found through its owner
  Store.findById(s.channels, "dm-p03").id = "private-3";
  assert.equal(View.ticketChannelId(s, s.tickets[0]), "private-3");
  assert.equal(View.ticketChannelId(s, { player_id: "p99" }), "dm-p99");
  assert.deepEqual(View.ticketStatus(s.tickets[0]), { text: "待处理", cls: "open" });
  assert.deepEqual(View.ticketStatus(s.tickets[1]), { text: "成立", cls: "upheld" });
  assert.deepEqual(View.ticketStatus({ status: "resolved", verdict: "rejected" }), { text: "驳回", cls: "rejected" });
  assert.deepEqual(View.ticketStatus({ status: "resolved", verdict: null }), { text: "已处理", cls: "resolved" });
});

// ---- messages -----------------------------------------------------------------------------------
test("message kinds map to a label + a safe css class", () => {
  assert.equal(View.messageKindInfo("announce").label, "公告");
  assert.equal(View.messageKindInfo("template").cls, "template");
  assert.equal(View.messageKindInfo("system").cls, "system");
  assert.equal(View.messageKindInfo("reply").label, "质询回复");
  assert.equal(View.messageKindInfo("challenge").label, "质询");
  assert.equal(View.messageKindInfo("ability").label, "能力");
  assert.equal(View.messageKindInfo("speech").label, "");
  assert.equal(View.messageKindInfo('x"><b>').cls, "xb");
  assert.equal(View.messageKindInfo(undefined).cls, "speech");
});

// ---- requests ------------------------------------------------------------------------------------
test("request hints: 投票中 7/12 for choices, 作答中 for text; closed ones vanish; viewpoint-aware", () => {
  const s = fresh();
  s.requests = [
    Store.normalizeRequest({ id: "r1", kind: "choice", question: "今天放逐谁？", to: F.PLAYER_IDS, answered_by: F.PLAYER_IDS.slice(0, 7), sealed: true }),
    Store.normalizeRequest({ id: "r2", kind: "text", question: "遗言？", to: ["p03"], answered_by: [] }),
    Store.normalizeRequest({ id: "r3", kind: "choice", to: ["p01"], status: "closed" }),
  ];
  const god = View.requestHints(s, GOD);
  assert.equal(god.length, 2);
  assert.equal(god[0].label, "投票中");
  assert.equal(god[0].answered, 7);
  assert.equal(god[0].total, 12);
  assert.equal(god[0].sealed, true);
  assert.equal(god[1].label, "作答中");
  assert.equal(god[1].total, 1);
  assert.deepEqual(View.requestHints(s, "p03").map((h) => h.id), ["r1", "r2"]);
  assert.deepEqual(View.requestHints(s, "p01").map((h) => h.id), ["r1"]); // r2 is not addressed to p01
  // total falls back to the progress event's total when `to` is unknown
  s.requests = [Store.normalizeRequest({ id: "r9", kind: "choice", to: [], total: 5, answered_by: ["p01"] })];
  assert.equal(View.requestHints(s, GOD)[0].total, 5);
});

// ---- top bar -------------------------------------------------------------------------------------
test("control buttons: start only when idle; resume when paused OR finished; pause only when running", () => {
  const ctl = (status) => View.controlState({ status });
  assert.deepEqual(ctl("idle"), { start: true, pause: false, resume: false });
  assert.deepEqual(ctl("running"), { start: false, pause: true, resume: false });
  assert.deepEqual(ctl("paused"), { start: false, pause: false, resume: true });
  assert.deepEqual(ctl("finished"), { start: false, pause: false, resume: true }); // continue after the verdict (D8)
  assert.deepEqual(View.controlState(null), { start: true, pause: false, resume: false });
});

test("status dot / text follow the game status; active_status_text wins", () => {
  assert.equal(View.statusDotClass({ status: "running" }), "active");
  assert.equal(View.statusDotClass({ status: "paused" }), "paused");
  assert.equal(View.statusDotClass({ status: "finished" }), "finished");
  assert.equal(View.statusDotClass({ status: "idle" }), "idle");
  assert.equal(View.statusText({ status: "running", active_status_text: "公共频道发言中" }), "公共频道发言中");
  assert.match(View.statusText({ status: "idle" }), /点击「开始」/);
  assert.match(View.statusText({ status: "paused" }), /暂停/);
  assert.match(View.statusText({ status: "finished" }), /结束/);
});

test("banners: the moderator's hold reason, errors, stall, game over and autosave errors", () => {
  const hold = View.statusBanners({ status: "paused", pause_reason: "hold", hold_reason: "请确认警长归属" });
  assert.equal(hold.length, 1);
  assert.equal(hold[0].kind, "hold");
  assert.match(hold[0].text, /请确认警长归属/);
  assert.match(hold[0].text, /继续/);
  assert.equal(View.statusBanners({ status: "paused", pause_reason: "hold", hold_reason: "" })[0].kind, "hold");
  assert.equal(View.statusBanners({ status: "paused", pause_reason: "user" }).length, 0); // a plain user pause needs no banner
  assert.equal(View.statusBanners({ status: "paused", pause_reason: "error" })[0].kind, "error");
  assert.match(View.statusBanners({ status: "paused", pause_reason: "stall" })[0].text, /没有进展/);
  const done = View.statusBanners({ status: "finished", winner: "好人阵营", summary: "狼人全部出局" });
  assert.equal(done[0].kind, "finished");
  assert.match(done[0].text, /好人阵营 获胜/);
  assert.match(done[0].text, /狼人全部出局/);
  assert.match(done[0].text, /继续/); // the user is told that continuing is possible
  assert.equal(View.statusBanners({ status: "running", pause_reason: "hold" }).length, 0);
  const err = View.statusBanners({ status: "running", autosave_error: "磁盘已满" });
  assert.match(err[0].text, /磁盘已满/);
  assert.deepEqual(View.statusBanners(null), []);
});

// ---- metrics ---------------------------------------------------------------------------------------
test("actorMetrics reads usage_stats.by_actor with zero defaults", () => {
  const s = fresh();
  assert.deepEqual(View.actorMetrics(s, "p01"), {
    calls: 3,
    promptTokens: 600,
    completionTokens: 100,
    toolCalls: 5,
    thinkingRounds: 2,
    rejections: 1,
    costUsd: 0.006,
  });
  assert.equal(View.actorMetrics(s, "p09").toolCalls, 0);
  assert.equal(View.actorMetrics(s, "moderator").toolCalls, 9);
  assert.equal(View.actorMetrics(null, "p01").calls, 0);
});

test("names and labels", () => {
  const s = fresh();
  assert.equal(View.displayName(s, "moderator"), "主持人");
  assert.equal(View.displayName(s, "engine"), "系统");
  assert.equal(View.displayName(s, "p03"), "Carol 3");
  assert.equal(View.displayName(s, "p99"), "p99");
  assert.equal(View.actorLabel(s, "p03"), "p03 · Carol 3");
  assert.equal(View.actorLabel(s, "moderator"), "主持人");
  assert.equal(View.actorLabel(s, "p99"), "p99");
});

// ---- runner ----------------------------------------------------------------------------------------
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
