"use strict";
// View model: spectator viewpoint, player cards, channel bar, request cards, controls (static/js/view.js).
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
const req = (over, clockNow = 0) => Store.normalizeRequest(F.makeRequest(over), { clockNow });

// ---- names -------------------------------------------------------------------------------
test("names: the nickname everywhere, never the internal id; refName is what the models see", () => {
  const s = fresh();
  assert.equal(View.displayName(s, "moderator"), "主持人");
  assert.equal(View.displayName(s, "engine"), "系统");
  assert.equal(View.displayName(s, "N3"), "Carol");
  assert.equal(View.displayName(s, "N99"), "N99");
  assert.equal(View.actorLabel(s, "N3"), "Carol"); // v0.12.0 printed "p03 · Carol 3"
  assert.equal(View.actorLabel(s, "moderator"), "主持人");
  assert.equal(View.refName(s, "N3"), "N3 Carol");
  assert.equal(View.refName(s, "N99"), "N99");
  View.participantOf(s, "N3").name = "Caroline"; // a renamed player shows the new name at once
  assert.equal(View.displayName(s, "N3"), "Caroline");
});

test("actorAvatar: players give their own description, the moderator gives its random one (null = top hat)", () => {
  const s = fresh();
  const mine = F.avatarOf(3);
  View.participantOf(s, "N3").avatar = mine;
  assert.deepEqual(View.actorAvatar(s, "N3"), { id: "N3", seat: 3, desc: mine });
  assert.deepEqual(View.actorAvatar(s, "N99"), { id: "N99", seat: undefined, desc: null });
  assert.deepEqual(View.actorAvatar(s, "moderator"), { id: "moderator", seat: undefined, desc: null }, "an older backend sends none");
  const host = F.avatarOf(8);
  s.moderator.avatar = host;
  assert.deepEqual(View.actorAvatar(s, "moderator"), { id: "moderator", seat: undefined, desc: host });
  assert.deepEqual(View.actorAvatar(null, "moderator"), { id: "moderator", seat: undefined, desc: null }, "no session yet");
});

test("annotateIdRefs: 'N3' keeps its characters and gains the hover text 'N3 Carol'; markup and N1 vs N10 are safe", () => {
  const s = fresh();
  const out = View.annotateIdRefs("<p>狼人队友为 N3、N10。N1 说：N12 是狼，<b>N3</b></p>", s);
  assert.ok(out.includes('<span class="id-ref" title="N3 Carol">N3</span>'));
  assert.ok(out.includes('<span class="id-ref" title="N10 Jack">N10</span>'));
  assert.ok(out.includes('<span class="id-ref" title="N1 Alice">N1</span>'));
  assert.ok(out.includes('<span class="id-ref" title="N12 Leo">N12</span>'));
  assert.ok(!out.includes('title="N1 Alice">N1</span>0'), "N10 is not split into N1 + 0");
  // the visible text is unchanged
  assert.equal(out.replace(/<[^>]*>/g, ""), "狼人队友为 N3、N10。N1 说：N12 是狼，N3");
  // ids inside tags / attributes are left alone; words that merely contain an id are left alone
  assert.equal(View.annotateIdRefs('<a href="/N3">x</a>', s), '<a href="/N3">x</a>');
  assert.equal(View.annotateIdRefs("<p>ZN3 N33 N3x</p>", s), "<p>ZN3 N33 N3x</p>");
  // a renamed player: the hover text follows
  View.participantOf(s, "N3").name = "Caroline";
  assert.ok(View.annotateIdRefs("N3", s).includes('title="N3 Caroline"'));
  // nothing to annotate without players
  assert.equal(View.annotateIdRefs("N3", Store.emptySession()), "N3");
  assert.equal(View.annotateIdRefs("", s), "");
});

// ---- viewpoint: options / messages / channels --------------------------------------------------
test("viewpoint options: god first, then every player by seat; unknown viewpoints fall back to god", () => {
  const s = fresh();
  const opts = View.viewpointOptions(s);
  assert.equal(opts.length, 13);
  assert.deepEqual(opts[0], { id: "god", label: "上帝视角" });
  assert.deepEqual(opts[3], { id: "N3", label: "3 · Carol" });
  assert.equal(View.normalizeViewpoint(s, "N3"), "N3");
  assert.equal(View.normalizeViewpoint(s, "ghost"), "god");
  assert.equal(View.normalizeViewpoint(s, ""), "god");
  assert.equal(View.normalizeViewpoint(s, undefined), "god");
  assert.equal(View.viewpointLabel(s, "N3"), "Carol");
  assert.equal(View.viewpointLabel(s, "ghost"), "上帝视角");
});

test("god sees every message; a player sees only those whose visible_to_ids contain them", () => {
  const s = fresh();
  assert.equal(View.visibleMessages(s, GOD).length, 6);
  assert.deepEqual(View.visibleMessages(s, "N1").map((m) => m.seq), [3, 8, 12]); // not the wolves chat, not N3's DM
  assert.deepEqual(View.visibleMessages(s, "N3").map((m) => m.seq), [3, 5, 8, 9, 11, 12]);
  assert.deepEqual(View.visibleMessages(s, "N3", "wolves").map((m) => m.seq), [5, 9]);
  assert.deepEqual(View.visibleMessages(s, "N1", "wolves"), []);
  assert.deepEqual(View.visibleMessages(s, GOD, "dm-N3").map((m) => m.seq), [11]);
  // empty visible_to_ids (a sealed entry) is god-only
  s.messages.push(Store.normalizeMessage(F.msg(20, "public", "N5", "密封内容", "speech", [])));
  assert.equal(View.visibleMessages(s, GOD).length, 7);
  assert.equal(View.visibleMessages(s, "N5").some((m) => m.seq === 20), false);
});

test("switching the viewpoint is pure: it never mutates the session", () => {
  const s = fresh({ requests: [F.makeRequest()] });
  const before = JSON.stringify(s);
  View.visibleMessages(s, "N1");
  View.visibleChannels(s, "N1");
  View.boardTiles(s, "N1");
  View.channelGroups(s, "N7");
  View.feedEntries(s, "N3", "wolves");
  View.boardSummary(s, "N1");
  View.requestHints(s, "N3");
  assert.equal(JSON.stringify(s), before);
});

test("a player only lists the channels they are a member of; private chats are grouped apart", () => {
  const s = fresh();
  assert.equal(View.visibleChannels(s, GOD).length, 14);
  assert.deepEqual(View.visibleChannels(s, "N1").map((c) => c.id), ["public", "dm-N1"]);
  assert.deepEqual(View.visibleChannels(s, "N3").map((c) => c.id), ["public", "wolves", "dm-N3"]);
  const g = View.channelGroups(s, GOD);
  assert.deepEqual(g.groups.map((c) => c.id), ["public", "wolves"]);
  assert.equal(g.dms.length, 12);
  assert.deepEqual(g.dms.map((c) => c.owner), F.PLAYER_IDS, "private chats are in seat order (N2 before N10)");
  assert.deepEqual(View.channelGroups(s, "N3").dms.map((c) => c.id), ["dm-N3"]);
});

// ---- there is no "all" channel any more -----------------------------------------------------------------
test("default channel: the public channel; without one a group channel everybody is in, else the first group channel; never a private chat", () => {
  const s = fresh();
  assert.equal(View.defaultChannelId(s, GOD), "public");
  assert.equal(View.defaultChannelId(s, "N3"), "public");
  // a renamed / differently-id'ed public channel is still found (the channel every participant is in)
  const t = fresh();
  Store.findById(t.channels, "public").id = "lobby";
  assert.equal(View.defaultChannelId(t, GOD), "lobby");
  // no public channel at all: the first group channel the viewpoint can see
  const u = fresh();
  u.channels = u.channels.filter((c) => c.id !== "public");
  assert.equal(View.defaultChannelId(u, GOD), "wolves");
  assert.equal(View.defaultChannelId(u, "N3"), "wolves");
  // ... and when there is none (the game has not started: only the 12 private chats exist) nothing is
  // selected, instead of opening the feed on "Alice"'s private chat
  assert.equal(View.defaultChannelId(u, "N1"), null);
  const v = fresh();
  v.channels = v.channels.filter((c) => c.system);
  assert.equal(View.defaultChannelId(v, GOD), null);
  assert.equal(View.defaultChannelId(Store.emptySession(), GOD), null);
  // a private chat can still be chosen on purpose
  assert.equal(View.normalizeSelectedChannel(v, GOD, "dm-N3"), "dm-N3");
});

test("normalizeSelectedChannel: a visible choice is kept; everything else (incl. the old \"all\") falls back to the default", () => {
  const s = fresh();
  assert.equal(View.normalizeSelectedChannel(s, "N3", "wolves"), "wolves");
  assert.equal(View.normalizeSelectedChannel(s, "N1", "wolves"), "public"); // not visible
  assert.equal(View.normalizeSelectedChannel(s, GOD, "ghost"), "public");
  assert.equal(View.normalizeSelectedChannel(s, GOD, "all"), "public");
  assert.equal(View.normalizeSelectedChannel(s, GOD, ""), "public");
  assert.equal(View.normalizeSelectedChannel(s, GOD, null), "public");
  assert.equal(View.normalizeSelectedChannel(Store.emptySession(), GOD, "x"), null);
});

// ---- the channel list: DM titles, status chips ---------------------------------------------------------------
test("a private chat is titled with the other party's nickname only (no \"私聊\") and wears that party's avatar", () => {
  const s = fresh();
  const dm = Store.findById(s.channels, "dm-N3");
  const god = View.channelModel(s, GOD, dm);
  assert.equal(god.kind, "dm");
  assert.equal(god.title, "Carol");
  assert.equal(god.avatarId, "N3");
  assert.ok(!god.title.includes("私聊"));
  // from the player's own point of view the other party is the moderator
  const own = View.channelModel(s, "N3", dm);
  assert.equal(own.title, "主持人");
  assert.equal(own.avatarId, "moderator");
  // a renamed player renames the chat
  View.participantOf(s, "N3").name = "Caroline";
  assert.equal(View.channelModel(s, GOD, dm).title, "Caroline");
  // group channels keep their name and get a one-letter glyph
  const pub = View.channelModel(s, GOD, Store.findById(s.channels, "public"));
  assert.deepEqual([pub.kind, pub.title, pub.glyph, pub.memberCount], ["group", "公共频道", "公", 13]);
  assert.equal(View.channelTitle(s, GOD, "dm-N5"), "Eve");
  assert.equal(View.channelTitle(s, GOD, "nope"), "");
});

test("floorSummary: 禁言中 (was 已关闭) · 🎤 turn · 📋 queue · 💬 free · 👁 listen · ❄ frozen — names, not ids", () => {
  const s = fresh();
  const ch = (mode, floor, extra = {}) => ({ ...Store.normalizeChannel({ id: "c", mode, floor, ...extra }) });
  const closed = View.floorSummary(s, ch("closed"));
  assert.deepEqual(closed, { kind: "closed", icon: "🔇", text: "禁言中", detail: "" });
  assert.ok(!JSON.stringify(closed).includes("已关闭"));
  assert.equal(View.floorSummary(s, ch("listen")).icon, "👁");
  const free = View.floorSummary(s, ch("free", { kind: "free", free: { chars_used: 120, max_chars_total: 2000 } }));
  assert.equal(free.icon, "💬");
  assert.equal(free.detail, "120/2000 字");
  assert.equal(View.floorSummary(s, ch("free")).detail, "");

  const turn = View.floorSummary(s, ch("turn", { kind: "turn", queue: [{ holder: "N5", state: "active" }, { holder: "N6", state: "active" }] }));
  assert.equal(turn.icon, "🎤");
  assert.equal(turn.detail, "持麦：Eve、Frank");
  const many = View.floorSummary(s, ch("turn", { kind: "turn", queue: ["N1", "N2", "N3"].map((h) => ({ holder: h, state: "active" })) }));
  assert.match(many.detail, /等3人/);
  assert.match(View.floorSummary(s, ch("turn", { kind: "turn", paused: true })).text, /暂停/);

  const queue = View.floorSummary(
    s,
    ch("queue", {
      kind: "queue",
      queue: [
        { holder: "N1", state: "done" },
        { holder: "N2", state: "done" },
        { holder: "N3", state: "active" },
        { holder: "N4", state: "pending" },
      ],
    })
  );
  assert.equal(queue.icon, "📋");
  assert.equal(queue.text, "队列 2/4");
  assert.equal(queue.detail, "持麦：Carol");

  const frozen = View.floorSummary(s, ch("queue", { kind: "queue" }, { frozen: true }));
  assert.equal(frozen.icon, "❄"); // frozen wins over the mode
  assert.equal(frozen.text, "已冻结");
  assert.equal(View.floorSummary(s, ch("weird")).kind, "none");
  assert.equal(View.floorSummary(s, null).kind, "none");
});

// ---- unread -----------------------------------------------------------------------------------------------------
test("unread counts: above the baseline and above the channel's own watermark; the viewpoint filters first", () => {
  const s = fresh();
  const read = { baseline: 9, seqs: {} }; // history up to seq 9 is read
  let u = View.unreadCounts(s, GOD, read);
  assert.deepEqual(u.byChannel, { "dm-N3": 1, public: 1 }); // seq 11 (dm-N3) and 12 (public)
  assert.equal(u.total, 2);
  u = View.unreadCounts(s, GOD, { baseline: 9, seqs: { public: 12 } });
  assert.deepEqual(u.byChannel, { "dm-N3": 1 });
  u = View.unreadCounts(s, "N1", read);
  assert.deepEqual(u.byChannel, { public: 1 });
  assert.equal(View.unreadCounts(s, GOD, { baseline: 100 }).total, 0);
  assert.equal(View.unreadCounts(s, GOD, undefined).total, 6); // nothing read yet
  assert.equal(View.latestSeq(s, "public"), 12);
  assert.equal(View.latestSeq(s, "wolves"), 9);
  assert.equal(View.latestSeq(s, "nothing"), 0);
});

// ---- tickets -------------------------------------------------------------------------------------------------------
test("tickets: all for god, own for a player; click target is the player's system DM", () => {
  const s = fresh();
  s.tickets.push(Store.normalizeTicket({ id: "t2", player_id: "N8", status: "resolved", verdict: "upheld", preview: "x" }));
  assert.equal(View.visibleTickets(s, GOD).length, 2);
  assert.deepEqual(View.visibleTickets(s, "N8").map((t) => t.id), ["t2"]);
  assert.deepEqual(View.visibleTickets(s, "N1"), []);
  assert.equal(View.ticketChannelId(s, s.tickets[0]), "dm-N3");
  Store.findById(s.channels, "dm-N3").id = "private-3"; // a differently named DM is found through its owner
  assert.equal(View.ticketChannelId(s, s.tickets[0]), "private-3");
  assert.equal(View.ticketChannelId(s, { player_id: "N99" }), "dm-N99");
  assert.deepEqual(View.ticketStatus(s.tickets[0]), { text: "待处理", cls: "open" });
  assert.deepEqual(View.ticketStatus(s.tickets[1]), { text: "成立", cls: "upheld" });
  assert.deepEqual(View.ticketStatus({ status: "resolved", verdict: "rejected" }), { text: "驳回", cls: "rejected" });
  assert.deepEqual(View.ticketStatus({ status: "resolved", verdict: null }), { text: "已处理", cls: "resolved" });
});

// ---- tags: identity is always the main tag -----------------------------------------------------------------
test("tags: god sees all; a player sees public tags of others and every tag of themself", () => {
  const s = fresh();
  const p03 = View.participantOf(s, "N3");
  const p04 = View.participantOf(s, "N4");
  assert.deepEqual(View.visibleTags(s, GOD, p03), ["狼人", "存活"]);
  assert.deepEqual(View.visibleTags(s, "N4", p03), ["存活"]); // role hidden from others
  assert.deepEqual(View.visibleTags(s, "N3", p03), ["狼人", "存活"]); // I know my own role
  assert.deepEqual(View.visibleTags(s, "N4", p04), ["预言家", "存活"]);
  p03.tags.push("警长");
  assert.deepEqual(View.visibleTags(s, "N4", p03), ["存活", "警长"]); // public_tags include 警长
  s.public_tags = [];
  assert.deepEqual(View.visibleTags(s, "N4", p03), []);
});

test("main tag is ALWAYS the identity: the sheriff (a highlight tag) never takes its place (E17)", () => {
  const s = fresh();
  const p04 = View.participantOf(s, "N4");
  p04.tags = ["警长", "预言家", "存活"]; // the sheriff tag even comes first
  const m = View.mainTagInfo(s, GOD, p04);
  assert.deepEqual([m.text, m.known, m.variant], ["预言家", true, "purple"]);
  // ... and shows up as a badge instead
  assert.deepEqual(View.badgeTags(s, GOD, p04).map((b) => [b.tag, b.text]), [["警长", "警"]]);
  // the old helper agrees
  assert.equal(View.pickMainTag(["警长", "预言家", "存活"], s.ui, s.public_tags), "预言家");
  assert.equal(View.pickMainTag(["存活", "出局", "警长"], s.ui, s.public_tags), "", "no identity visible");
  assert.equal(View.pickMainTag(["a", "b"], undefined), "a");
  assert.equal(View.pickMainTag([], s.ui, s.public_tags), "");
  // the tile model carries both
  const tile = View.tileModel(s, GOD, p04);
  assert.equal(tile.main.text, "预言家");
  assert.equal(tile.badges.length, 1);
});

test("a hidden identity is a \"?\" (never the status tag, never the sheriff badge replacing it)", () => {
  const s = fresh();
  const p03 = View.participantOf(s, "N3");
  p03.tags = ["狼人", "存活", "警长"];
  // seen by another player: only the public tags are visible
  const other = View.mainTagInfo(s, "N4", p03);
  assert.deepEqual([other.text, other.known, other.variant], ["?", false, "muted"]);
  assert.deepEqual(View.badgeTags(s, "N4", p03).map((b) => b.tag), ["警长"], "the sheriff is public: a badge next to the ?");
  // seen by the player themself / by god: the identity
  assert.equal(View.mainTagInfo(s, "N3", p03).text, "狼人");
  assert.equal(View.mainTagInfo(s, GOD, p03).text, "狼人");
  // a participant without any identity tag at all is not a mystery
  const plain = Store.normalizeParticipant({ id: "N1", seat: 1, name: "A", tags: ["存活"] });
  assert.equal(View.mainTagInfo(s, "N2", plain).text, "?"); // nothing to show: the card still needs a main tag
});

test("a scenario whose team tags are highlight tags (not public) still gets them as the main tag", () => {
  const s = fresh({
    public_tags: ["存活", "出局"],
    ui: { out_tags: ["出局"], status_tags: [], highlight_tags: ["红队", "蓝队"] },
    tag_styles: {},
  });
  const p = View.participantOf(s, "N1");
  p.tags = ["红队", "存活"];
  assert.equal(View.mainTagInfo(s, GOD, p).text, "红队");
  assert.equal(View.mainTagInfo(s, "N2", p).text, "?");
  assert.deepEqual(View.badgeTags(s, GOD, p), [], "a team is not a badge");
  assert.ok(!View.boardSummary(s, GOD).text.includes("红队"), "...and not a summary item either (the teams are already on the cards)");
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

// ---- player cards -----------------------------------------------------------------------------------------------------
test("cards: 12 players in seat order (\"10\" after \"9\"), seat number, nickname, one main tag, out flag; the moderator is not a card", () => {
  const s = fresh();
  View.participantOf(s, "N5").tags = ["女巫", "出局"];
  View.participantOf(s, "N5").out = true;
  const tiles = View.boardTiles(s, GOD);
  assert.equal(tiles.length, 12);
  assert.deepEqual(tiles.map((t) => t.id), F.PLAYER_IDS);
  assert.deepEqual(tiles.map((t) => t.seat), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
  const byId = Object.fromEntries(tiles.map((t) => [t.id, t]));
  assert.equal(byId.N3.name, "Carol");
  assert.equal(byId.N3.main.text, "狼人");
  assert.equal(byId.N3.main.variant, "danger");
  assert.equal(byId.N5.main.text, "女巫");
  assert.equal(byId.N5.out, true);
  assert.equal(byId.N1.out, false);
  assert.match(byId.N3.title, /3号 Carol/);
  assert.ok(!byId.N3.title.includes("N3"), "the internal id is not shown");
  assert.equal(View.boardTiles(s, "N3").find((t) => t.id === "N3").viewing, true);
  assert.equal(View.boardTiles(s, "N3").find((t) => t.id === "N4").viewing, false);
  // shuffled participants come out sorted by seat
  const shuffled = fresh({ participants: F.makeParticipants().reverse() });
  assert.deepEqual(View.boardTiles(shuffled, GOD).map((t) => t.id), F.PLAYER_IDS);
});

test("player viewpoint: other cards show \"?\" instead of the role (the role never leaks)", () => {
  const s = fresh();
  const byId = Object.fromEntries(View.boardTiles(s, "N4").map((t) => [t.id, t])); // the seer's point of view
  assert.equal(byId.N4.main.text, "预言家"); // own role is known
  for (const id of F.PLAYER_IDS.filter((x) => x !== "N4")) {
    assert.equal(byId[id].main.text, "?", id);
    assert.equal(byId[id].main.known, false);
  }
  assert.equal(View.boardTiles(s, GOD).find((t) => t.id === "N3").main.text, "狼人");
  // the hover text of a card never contains a hidden role
  assert.ok(!byId.N3.title.includes("狼人"));
});

test("three agent states only: thinking / acting / sleeping; god sees all, a player only their own", () => {
  const s = fresh();
  View.participantOf(s, "N1").state = "thinking";
  View.participantOf(s, "N2").state = "acting";
  View.participantOf(s, "N3").state = "sleeping";
  s.moderator.state = "thinking";
  const god = Object.fromEntries(View.boardTiles(s, GOD).map((t) => [t.id, t]));
  assert.deepEqual([god.N1.state, god.N1.stateIcon], ["thinking", "💭"]);
  assert.deepEqual([god.N2.state, god.N2.stateIcon], ["acting", "⚡"]);
  assert.deepEqual([god.N3.state, god.N3.stateIcon], ["sleeping", "💤"]);
  assert.equal(View.moderatorTile(s, GOD).state, "thinking");
  const p02 = Object.fromEntries(View.boardTiles(s, "N2").map((t) => [t.id, t]));
  assert.equal(p02.N2.state, "acting"); // my own state is shown
  assert.equal(p02.N1.state, ""); // others' states would leak who is active
  assert.equal(View.moderatorTile(s, "N2").state, "");
  // (v0.13 adds "human": a seat a person plays; see the takeover tests)
  assert.deepEqual(Object.keys(View.STATE_ICONS), ["thinking", "acting", "sleeping", "human"]);
  // the removed card states have no icon any more
  for (const gone of ["🎤", "✋", "▪", "stopped", "idle"]) assert.ok(!JSON.stringify(View.STATE_ICONS).includes(gone));
  assert.equal(View.stateBadge(GOD, "N1", "stopped"), "");
  assert.equal(View.stateBadge(GOD, "N1", "idle"), "");
});

test("the sleeping hover text: waiting for new messages, or the time left of a timed wake-up (game clock)", () => {
  assert.equal(View.stateTitle("sleeping", null, 10), "休眠：等待新消息");
  assert.equal(View.stateTitle("sleeping", undefined, 10), "休眠：等待新消息");
  assert.equal(View.stateTitle("sleeping", 40, 10), "休眠：定时醒来：剩余 30 秒");
  assert.equal(View.stateTitle("sleeping", 40.2, 10), "休眠：定时醒来：剩余 31 秒"); // rounds up
  assert.equal(View.stateTitle("sleeping", 130, 10), "休眠：定时醒来：剩余 2 分 00 秒");
  assert.equal(View.stateTitle("sleeping", 10, 12), "休眠：即将醒来");
  assert.equal(View.stateTitle("thinking", null, 0), "思考中");
  assert.equal(View.stateTitle("acting", 99, 0), "执行中");
  assert.equal(View.stateTitle("bogus", null, 0), "");
  const s = fresh();
  View.participantOf(s, "N6").sleep_until = 75;
  const t = View.tileModel(s, GOD, View.participantOf(s, "N6"), { clockNow: 60 });
  assert.equal(t.stateTitle, "休眠：定时醒来：剩余 15 秒");
  assert.match(t.title, /💤 休眠：定时醒来：剩余 15 秒/);
  assert.equal(View.tileModel(s, GOD, View.participantOf(s, "N7"), { clockNow: 60 }).stateTitle, "休眠：等待新消息");
});

// ---- summary strip ----------------------------------------------------------------------------------------------------------
test("the summary strip (end of the participant bar) is god-only: 狼 2/4 · 神 4/4 · 民 3/4 · 警长 Grace", () => {
  const s = fresh();
  s.dashboard.factions[0].alive = 2;
  s.dashboard.factions[2].alive = 3;
  View.participantOf(s, "N7").tags = ["猎人", "存活", "警长"];
  const sum = View.boardSummary(s, GOD);
  assert.deepEqual(sum.items.map((i) => i.text), ["狼 2/4", "神 4/4", "民 3/4", "警长 Grace"]);
  assert.equal(sum.text, "狼 2/4 · 神 4/4 · 民 3/4 · 警长 Grace");
  assert.equal(sum.items[0].variant, "danger");
  assert.match(sum.items[0].title, /狼人阵营：存活 2 \/ 4/);
  assert.equal(View.boardSummary(s, "N1"), null, "a player's view must not show the factions");
  assert.equal(View.boardSummary(s, "N7"), null);
  // several sheriffs are abbreviated; no data -> no strip
  for (const id of ["N1", "N2", "N3", "N4"]) View.participantOf(s, id).tags.push("警长");
  assert.match(View.boardSummary(s, GOD).items.pop().text, /等5人$/);
  assert.equal(View.boardSummary(Store.emptySession(), GOD), null);
  assert.deepEqual(["狼人阵营", "神职", "平民阵营", "好人阵营", "第三方", "X"].map(View.shortFactionName), ["狼", "神", "民", "好", "三", "X"]);
});

// ---- messages -----------------------------------------------------------------------------------------------------------------
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

// ---- requests: who sees what (E18 / E25) ---------------------------------------------------------------------------
const wolfRequest = (over = {}) =>
  req(
    {
      answers: [F.answer("N3", "N5", "5号 Eve"), F.answer("N6", "N4", "4号 David"), F.answer("N10", "N5", "5号 Eve")],
      answered_by: ["N3", "N6", "N10"],
      ...over,
    },
    100
  );

test("god sees every request, incl. sealed ones and their answers; a player only requests addressed to them", () => {
  const s = fresh();
  s.requests = [wolfRequest(), req({ id: "r2", to: ["N10"], kind: "text", options: [], share_answers: false, sealed: false }), req({ id: "r3", to: F.PLAYER_IDS, sealed: true, share_answers: false })];
  assert.deepEqual(s.requests.filter((r) => View.requestVisible(r, GOD)).map((r) => r.id), ["r1", "r2", "r3"]);
  assert.deepEqual(s.requests.filter((r) => View.requestVisible(r, "N3")).map((r) => r.id), ["r1", "r3"]);
  assert.deepEqual(s.requests.filter((r) => View.requestVisible(r, "N10")).map((r) => r.id), ["r1", "r2", "r3"]);
  assert.deepEqual(s.requests.filter((r) => View.requestVisible(r, "N1")).map((r) => r.id), ["r3"]);
  assert.deepEqual(View.requestHints(s, "N1").map((h) => h.id), ["r3"]);
});

test("answers: god all; a player their own, plus everybody's in a share_answers request they belong to (never in a sealed one)", () => {
  const shared = wolfRequest();
  assert.deepEqual(View.visibleAnswers(shared, GOD).map((a) => a.player_id), ["N3", "N6", "N10"]);
  assert.deepEqual(View.visibleAnswers(shared, "N12").map((a) => a.player_id), ["N3", "N6", "N10"], "a wolf who has not answered yet still sees the others' choices");
  assert.deepEqual(View.visibleAnswers(shared, "N3").map((a) => a.player_id), ["N3", "N6", "N10"]);
  assert.deepEqual(View.visibleAnswers(shared, "N1"), [], "not a recipient: nothing");
  const sealed = wolfRequest({ share_answers: false, sealed: true });
  assert.deepEqual(View.visibleAnswers(sealed, GOD).map((a) => a.player_id), ["N3", "N6", "N10"], "god reads sealed answers");
  assert.deepEqual(View.visibleAnswers(sealed, "N6").map((a) => a.player_id), ["N6"], "a sealed request shows each player only their own answer");
  assert.deepEqual(View.visibleAnswers(sealed, "N12"), []);
  const unshared = wolfRequest({ share_answers: false, sealed: false });
  assert.deepEqual(View.visibleAnswers(unshared, "N3").map((a) => a.player_id), ["N3"]);
  // an answer that the server did not mark visible to a viewer stays hidden
  const partial = wolfRequest();
  partial.answers[1].visible_to_ids = ["moderator", "N6"];
  assert.deepEqual(View.visibleAnswers(partial, "N3").map((a) => a.player_id), ["N3", "N10"]);
});

test("request card: recipients in seat order with their current choice, changes counted, progress, countdown", () => {
  const s = fresh();
  const r = wolfRequest({ answer_changes: { N6: 1 } });
  const card = View.requestCard(s, GOD, r, { clockNow: 110 });
  assert.deepEqual(card.recipients.map((x) => [x.id, x.name]), [["N3", "Carol"], ["N6", "Frank"], ["N10", "Jack"], ["N12", "Leo"]]);
  const byId = Object.fromEntries(card.recipients.map((x) => [x.id, x]));
  assert.equal(byId.N3.answer.label, "5号 Eve");
  assert.equal(byId.N6.answer.changes, 1);
  assert.equal(byId.N12.answered, false);
  assert.equal(byId.N12.answer, null);
  assert.equal(byId.N12.hidden, false);
  assert.deepEqual([card.answeredCount, card.total], [3, 4]);
  assert.equal(card.shareAnswers, true);
  assert.equal(card.autoClose, "unanimous");
  assert.equal(card.status, "open");
  assert.equal(card.countdown, 80); // deadline 190, clock 110
  assert.equal(card.deadlineClock, 190);
  assert.equal(card.result, null);
  assert.equal(card.answersHidden, false);
  // the countdown never goes negative and a closed request has none
  assert.equal(View.requestCard(s, GOD, r, { clockNow: 500 }).countdown, 0);
  assert.equal(View.requestCountdown({ ...r, status: "closed" }, 110), null);
  assert.equal(View.requestCountdown(req({ seconds_left: null }), 0), null);
});

test("a sealed vote: god reads the answers, a player sees \"submitted\" for others but not what they chose", () => {
  const s = fresh();
  const sealed = wolfRequest({ share_answers: false, sealed: true });
  const god = View.requestCard(s, GOD, sealed, { clockNow: 100 });
  assert.equal(god.recipients.find((x) => x.id === "N3").answer.label, "5号 Eve");
  assert.equal(god.answersHidden, false);
  const me = View.requestCard(s, "N12", sealed, { clockNow: 100 });
  assert.equal(me.answersHidden, true);
  const n3 = me.recipients.find((x) => x.id === "N3");
  assert.equal(n3.answered, true);
  assert.equal(n3.hidden, true);
  assert.equal(n3.answer, null);
  assert.equal(me.recipients.find((x) => x.id === "N12").hidden, false);
});

test("the closed card shows the result: tally of the voted options (empty ones folded), leaders, defaulted and missing", () => {
  const s = fresh();
  const closed = wolfRequest({
    status: "closed",
    seconds_left: null,
    result: { reason: "unanimous", tally: { N1: 0, N4: 1, N5: 3 }, leaders: ["N5"], defaulted: ["N12"], missing: [] },
  });
  const god = View.requestCard(s, GOD, closed, {});
  assert.deepEqual(god.result.tally.map((t) => [t.id, t.count, t.leader]), [["N5", 3, true], ["N4", 1, false]]);
  assert.equal(god.result.zeroCount, 1);
  assert.deepEqual(god.result.leaders, ["5号 Eve"]);
  assert.deepEqual(god.result.defaulted, ["Leo"]);
  assert.equal(god.result.tally[0].label, "5号 Eve");
  // a recipient of a shared request sees the result too; a stranger or a sealed-vote participant does not
  assert.ok(View.requestCard(s, "N3", closed, {}).result);
  const sealedClosed = { ...closed, share_answers: false, sealed: true };
  assert.equal(View.requestCard(s, "N3", sealedClosed, {}).result, null, "the count of a sealed vote is for the moderator to announce");
  assert.ok(View.requestCard(s, GOD, sealedClosed, {}).result);
  // a tally without any votes keeps all rows (nothing to fold)
  const empty = wolfRequest({ status: "closed", result: { reason: "deadline", tally: { N1: 0, N4: 0 }, leaders: [], defaulted: [], missing: ["N3"] } });
  assert.equal(View.requestCard(s, GOD, empty, {}).result.tally.length, 2);
  assert.deepEqual(View.requestCard(s, GOD, empty, {}).result.missing, ["Carol"]);
});

test("a request lives in the tightest channel that holds every recipient (wolves' target -> wolves; a vote of all -> public; last words -> that player's chat)", () => {
  const s = fresh();
  assert.equal(View.requestChannelId(s, GOD, wolfRequest()), "wolves");
  assert.equal(View.requestChannelId(s, "N3", wolfRequest()), "wolves");
  assert.equal(View.requestChannelId(s, GOD, req({ to: F.PLAYER_IDS })), "public");
  assert.equal(View.requestChannelId(s, GOD, req({ to: ["N10"] })), "dm-N10");
  // the server may name the channel itself (preferred when the viewer can see it)
  assert.equal(View.requestChannelId(s, GOD, req({ to: ["N10"], channel_id: "public" })), "public");
  assert.equal(View.requestChannelId(s, "N1", req({ to: ["N1"], channel_id: "wolves" })), "dm-N1", "an invisible channel is ignored");
  // no channel fits: the default one
  assert.equal(View.requestChannelId(s, GOD, req({ to: ["N1", "ghost"] })), "public");
});

test("the feed of a channel: messages and request cards in ledger order; the card sits at its opened_seq", () => {
  const s = fresh();
  s.requests = [wolfRequest({ opened_seq: 6 }), req({ id: "r2", to: F.PLAYER_IDS, opened_seq: 10 }), req({ id: "r3", to: F.WOLVES, opened_seq: 0 })];
  const wolves = View.feedEntries(s, GOD, "wolves");
  assert.deepEqual(wolves.map((e) => e.key), ["m5", "rr1", "m9", "rr3"], "r1 at 6 (between seq 5 and 9); r3 has no position: after everything");
  const pub = View.feedEntries(s, GOD, "public");
  assert.deepEqual(pub.map((e) => e.key), ["m3", "m8", "rr2", "m12"]);
  // a request opened at the same seq as a message comes after it
  s.requests = [req({ id: "r9", to: F.PLAYER_IDS, opened_seq: 8 })];
  assert.deepEqual(View.feedEntries(s, GOD, "public").map((e) => e.key), ["m3", "m8", "rr9", "m12"]);
  // a player's feed: only requests addressed to them, only in channels they can see
  s.requests = [wolfRequest({ opened_seq: 6 }), req({ id: "r2", to: F.PLAYER_IDS, opened_seq: 10 })];
  assert.deepEqual(View.feedEntries(s, "N1", "public").map((e) => e.key), ["m3", "m8", "rr2", "m12"]);
  assert.deepEqual(View.feedEntries(s, "N1", "wolves"), []);
  assert.deepEqual(View.feedEntries(s, "N3", "wolves").map((e) => e.key), ["m5", "rr1", "m9"]);
  assert.deepEqual(View.feedEntries(s, GOD, "no-such-channel"), []);
});

test("request hints (top bar): 投票中 / 商议中 / 作答中 n/m; closed ones vanish; viewpoint-aware", () => {
  const s = fresh();
  s.requests = [
    req({ id: "r1", kind: "choice", question: "今天放逐谁？", to: F.PLAYER_IDS, answered_by: F.PLAYER_IDS.slice(0, 7), sealed: true, share_answers: false }),
    req({ id: "r2", kind: "text", question: "遗言？", to: ["N3"], answered_by: [], options: [], share_answers: false }),
    wolfRequest({ id: "r4" }),
    req({ id: "r5", kind: "choice", to: ["N1"], status: "closed" }),
  ];
  const god = View.requestHints(s, GOD);
  assert.equal(god.length, 3);
  assert.deepEqual([god[0].label, god[0].answered, god[0].total, god[0].sealed], ["投票中", 7, 12, true]);
  assert.deepEqual([god[1].label, god[1].total], ["作答中", 1]);
  assert.deepEqual([god[2].label, god[2].answered, god[2].total], ["商议中", 3, 4]);
  assert.deepEqual(View.requestHints(s, "N3").map((h) => h.id), ["r1", "r2", "r4"]);
  assert.deepEqual(View.requestHints(s, "N1").map((h) => h.id), ["r1"]);
  // total falls back to the progress event's total when `to` is unknown
  s.requests = [Store.normalizeRequest({ id: "r9", kind: "choice", to: [], total: 5, answered_by: ["N1"] })];
  assert.equal(View.requestHints(s, GOD)[0].total, 5);
});

// ---- top bar --------------------------------------------------------------------------------------------------------------
test("control buttons: start only when idle; resume when paused OR finished; pause only when running", () => {
  const ctl = (status) => View.controlState({ status });
  assert.deepEqual(ctl("idle"), { start: true, pause: false, resume: false });
  assert.deepEqual(ctl("running"), { start: false, pause: true, resume: false });
  assert.deepEqual(ctl("paused"), { start: false, pause: false, resume: true });
  assert.deepEqual(ctl("finished"), { start: false, pause: false, resume: true }); // continue after the verdict (D8)
  assert.deepEqual(View.controlState(null), { start: true, pause: false, resume: false });
});

test("status dot / text follow the game status; a budget pause says so", () => {
  assert.equal(View.statusDotClass({ status: "running" }), "active");
  assert.equal(View.statusDotClass({ status: "paused" }), "paused");
  assert.equal(View.statusDotClass({ status: "finished" }), "finished");
  assert.equal(View.statusDotClass({ status: "idle" }), "idle");
  assert.equal(View.statusText({ status: "running", active_status_text: "公共频道发言中" }), "公共频道发言中");
  assert.match(View.statusText({ status: "idle" }), /点击「开始」/);
  assert.match(View.statusText({ status: "paused" }), /暂停/);
  assert.match(View.statusText({ status: "finished" }), /结束/);
  assert.equal(View.statusText({ status: "paused", pause_reason: "budget", active_status_text: "已暂停" }), "因预算上限暂停");
});

test("budget view: ratios of the limits that are set; the server's state wins, the ratio is the fallback", () => {
  const view = (b) => View.budgetView({ budget: b });
  assert.deepEqual(view(undefined).hasLimit, false);
  const none = view({ max_tokens: null, max_cost_usd: null, used_tokens: 5, used_cost_usd: 1, state: "ok" });
  assert.deepEqual([none.hasLimit, none.ratio, none.state], [false, null, "ok"]);
  const half = view({ max_tokens: 1000, max_cost_usd: 10, used_tokens: 500, used_cost_usd: 1, state: "ok" });
  assert.equal(half.tokens.ratio, 0.5);
  assert.equal(half.cost.ratio, 0.1);
  assert.equal(half.ratio, 0.5);
  assert.equal(half.state, "ok");
  assert.equal(view({ max_tokens: 1000, used_tokens: 800, state: "ok" }).state, "warning", "80 % by the numbers");
  assert.equal(view({ max_tokens: 1000, used_tokens: 1000, state: "ok" }).state, "reached", "100 % by the numbers");
  assert.equal(view({ max_cost_usd: 2, used_cost_usd: 1.7, state: "ok" }).state, "warning", "the cost limit counts too");
  assert.equal(view({ max_tokens: 1000, used_tokens: 10, state: "reached" }).state, "reached", "the server decides");
  assert.equal(view({ max_tokens: 1000, used_tokens: 10, force_stop: true, state: "warning" }).forceStop, true);
});

test("banners: budget (yellow/red), stall, hold, errors, game over, autosave; phones get short sentences", () => {
  const hold = View.statusBanners({ status: "paused", pause_reason: "hold", hold_reason: "请确认警长归属" });
  assert.equal(hold.length, 1);
  assert.equal(hold[0].kind, "hold");
  assert.match(hold[0].text, /请确认警长归属/);
  assert.match(hold[0].text, /继续/);
  assert.ok(hold[0].short.includes("请确认警长归属") && !hold[0].short.includes("确认后"));
  assert.equal(View.statusBanners({ status: "paused", pause_reason: "hold", hold_reason: "" })[0].kind, "hold");
  assert.equal(View.statusBanners({ status: "paused", pause_reason: "user" }).length, 0); // a plain user pause needs no banner
  assert.equal(View.statusBanners({ status: "paused", pause_reason: "error" })[0].kind, "error");
  assert.match(View.statusBanners({ status: "paused", pause_reason: "stall" })[0].text, /没有进展/);
  const done = View.statusBanners({ status: "finished", winner: "好人阵营", summary: "狼人全部出局" });
  assert.equal(done[0].kind, "finished");
  assert.match(done[0].text, /好人阵营 获胜/);
  assert.match(done[0].text, /狼人全部出局/);
  assert.match(done[0].text, /继续/); // the user is told that continuing is possible
  assert.match(done[0].text, /复盘全部/);
  assert.equal(done[0].short, "🏁 对局已结束：好人阵营 获胜");
  assert.equal(View.statusBanners({ status: "running", pause_reason: "hold" }).length, 0);
  const err = View.statusBanners({ status: "running", autosave_error: "磁盘已满" });
  assert.match(err[0].text, /磁盘已满/);
  assert.deepEqual(View.statusBanners(null), []);

  // budget: paused because of it / reached but still running / limit raised while paused
  const reachedBudget = { max_tokens: 100, used_tokens: 100, state: "reached", force_stop: true };
  const paused = View.statusBanners({ status: "paused", pause_reason: "budget", budget: reachedBudget });
  assert.equal(paused[0].kind, "budget");
  assert.match(paused[0].text, /因预算上限暂停/);
  assert.match(paused[0].text, /提高上限/);
  assert.match(paused[0].short, /因预算上限暂停/);
  const raised = View.statusBanners({ status: "paused", pause_reason: "budget", budget: { max_tokens: 500, used_tokens: 100, state: "ok", force_stop: true } });
  assert.match(raised[0].text, /上限已调整/);
  const noStop = View.statusBanners({ status: "running", budget: { ...reachedBudget, force_stop: false } });
  assert.equal(noStop[0].kind, "budget");
  assert.match(noStop[0].text, /未勾选「强制停止」/);
  assert.match(View.statusBanners({ status: "running", budget: reachedBudget })[0].text, /正在强制停止/);
  assert.equal(View.statusBanners({ status: "running", budget: { max_tokens: 100, used_tokens: 85, state: "warning" } }).length, 0, "80 % only colours the stats bar");

  // the stall warning (E9): shown while the notice is non-empty, never pauses
  const stall = View.statusBanners({ status: "running", stall_notice: "对局似乎停滞了" });
  assert.equal(stall.length, 1);
  assert.equal(stall[0].kind, "stall");
  assert.match(stall[0].text, /对局似乎停滞了/);
  assert.match(View.statusBanners({ status: "running", stall_notice: "已 120 秒没有进展" })[0].text, /已 120 秒没有进展/);
  assert.equal(View.statusBanners({ status: "running", stall_notice: "" }).length, 0);
  assert.equal(stall[0].short, "⚠ 对局似乎停滞了（游戏没有暂停）", "a phone shows one short line");
  // the real watchdog (app/runtime/supervisor.py) sends a whole sentence: it is shown as it is, not under a second heading
  const watchdog = "对局静止：已连续 3 次唤醒主持人，账本仍无进展（约 120 秒）。";
  assert.equal(View.statusBanners({ status: "running", stall_notice: watchdog })[0].text, `⚠ ${watchdog}`);
  assert.equal(View.statusBanners({ status: "running", stall_notice: "对局似乎停滞了" })[0].text, "⚠ 对局似乎停滞了");
  // a bare reason that does not say it gets the heading
  assert.equal(View.statusBanners({ status: "running", stall_notice: "主持人 3 次没有回应" })[0].text, "⚠ 对局似乎停滞了：主持人 3 次没有回应");
  // most severe first
  const both = View.statusBanners({ status: "paused", pause_reason: "budget", budget: reachedBudget, stall_notice: "x", autosave_error: "y" });
  assert.deepEqual(both.map((b) => b.kind), ["budget", "stall", "error"]);
});

// ---- metrics / review ---------------------------------------------------------------------------------------------------------
test("actorMetrics reads usage_stats.by_actor (incl. the four token kinds and the cache hit rate) with zero defaults", () => {
  const s = fresh();
  assert.deepEqual(View.actorMetrics(s, "N1"), {
    activations: 4,
    calls: 3,
    promptTokens: 600,
    cacheReadTokens: 300,
    cacheWriteTokens: 50,
    completionTokens: 100,
    cacheHitRate: 0.3158,
    toolCalls: 5,
    thinkingRounds: 2,
    rejections: 1,
    costUsd: 0.006,
  });
  assert.equal(View.actorMetrics(s, "N9").toolCalls, 0);
  assert.equal(View.actorMetrics(s, "N9").cacheHitRate, 0);
  assert.equal(View.actorMetrics(s, "moderator").toolCalls, 9);
  assert.equal(View.actorMetrics(null, "N1").calls, 0);
  // without a reported rate it is derived: cache_read / (miss + read + write)
  delete s.usage_stats.by_actor.N1.cache_hit_rate;
  assert.ok(Math.abs(View.actorMetrics(s, "N1").cacheHitRate - 300 / 950) < 1e-9);
});

test("review: the buttons only exist after the game is finished; per-player running / done / error", () => {
  const s = fresh({ status: "running" });
  assert.equal(View.reviewView(s).visible, false);
  s.status = "finished";
  s.review = { available: true, done: ["N1"], running: false, progress: {} };
  let rv = View.reviewView(s);
  assert.equal(rv.visible, true);
  assert.equal(rv.available, true);
  assert.equal(rv.of("N1").state, "done");
  assert.equal(rv.of("N2").state, "none");
  assert.deepEqual([rv.doneCount, rv.total, rv.allDone, rv.running], [1, 12, false, false]);
  s.review.progress = { N2: { state: "running", message: "正在复盘…" }, N3: { state: "error", message: "模型不可用" } };
  rv = View.reviewView(s);
  assert.equal(rv.running, true);
  assert.deepEqual(rv.of("N2"), { state: "running", message: "正在复盘…" });
  assert.deepEqual(rv.of("N3"), { state: "error", message: "模型不可用" });
  s.review.done = F.PLAYER_IDS;
  assert.equal(View.reviewView(s).allDone, true);
  assert.equal(View.reviewView(fresh({ status: "finished", review: { available: false, done: [], running: false } })).available, false);
});

// ---- runner ----------------------------------------------------------------------------------------------------------------------
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
