"use strict";
// SSE v2 event application (static/js/store.js). Run: node tests/frontend/test_store.js
const assert = require("node:assert/strict");
const path = require("node:path");
const Store = require(path.join(__dirname, "..", "..", "static", "js", "store.js"));
const F = require("./fixtures.js");

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

const ev = (type, data = {}, id = 1) => ({ id, type, data });
const fresh = () => Store.normalizeSession(F.makeSession());
const apply = (s, type, data, id) => Store.applyEvent(s, ev(type, data, id));

// ---- normalization -------------------------------------------------------------
test("normalizeSession fills every field the renderers read (even from {})", () => {
  const s = Store.emptySession();
  for (const k of ["participants", "channels", "messages", "requests", "tickets", "binding_issues", "public_tags"]) {
    assert.ok(Array.isArray(s[k]), k);
  }
  assert.equal(s.status, "idle");
  assert.equal(s.winner, null);
  assert.deepEqual(s.dashboard, { factions: [], tag_stats: [] });
  assert.deepEqual(s.ui, { out_tags: [], status_tags: [], highlight_tags: [] });
  assert.deepEqual(s.usage_stats.by_actor, {});
  assert.equal(s.moderator.id, "moderator");
  assert.equal(s.moderator.state, "idle");
  assert.equal(s.event_id, 0);
  assert.deepEqual(s.model_bindings, {});
});

test("normalizeSession drops malformed list items, repairs types and sorts messages by seq", () => {
  const s = Store.normalizeSession({
    participants: [null, "x", { id: "p01", state: "bogus", tags: ["a", 1] }],
    channels: [{ id: "c1", members: ["p01"], floor: { queue: [null, { holder: "p01" }] } }, 7],
    messages: [{ seq: 9, content: "b" }, { seq: 3, content: "a" }, null],
    requests: [{ id: "r1", to: ["p01"], options: [{ id: "a" }, null] }],
    tickets: [{ id: "t1", player_id: "p01" }],
    usage_stats: { by_actor: "nope" },
  });
  assert.equal(s.participants.length, 1);
  assert.equal(s.participants[0].state, "idle");
  assert.deepEqual(s.participants[0].tags, ["a", "1"]);
  assert.equal(s.channels.length, 1);
  assert.equal(s.channels[0].name, "c1"); // name falls back to the id
  assert.equal(s.channels[0].floor.queue.length, 1);
  assert.deepEqual(s.messages.map((m) => m.seq), [3, 9]);
  assert.equal(s.requests[0].options[0].label, "a");
  assert.equal(s.requests[0].status, "open");
  assert.equal(s.tickets[0].status, "open");
  assert.deepEqual(s.usage_stats.by_actor, {});
});

// ---- snapshot ------------------------------------------------------------------
test("snapshot replaces the whole session and repaints everything", () => {
  const s = fresh();
  const next = F.makeSession({ session_id: "other", status: "paused", messages: [] });
  const r = apply(s, "snapshot", { session: next });
  assert.equal(r.replaced, true);
  assert.notEqual(r.session, s);
  assert.equal(r.session.session_id, "other");
  assert.equal(r.session.status, "paused");
  assert.equal(r.session.messages.length, 0);
  for (const k of Store.DIRTY_KEYS) assert.equal(r.dirty[k], true, k);
  // a snapshot without a session object still yields a usable (empty) session
  assert.equal(apply(s, "snapshot", {}).session.status, "idle");
});

// ---- messages -------------------------------------------------------------------
test("message.committed appends one whole message; thoughts / tool data are never kept", () => {
  const s = fresh();
  const before = s.messages.length;
  const r = Store.applyEvent(s, {
    id: 101,
    type: "message.committed",
    data: { ...F.msg(41, "public", "p04", "我是预言家。"), thought: "secret chain of thought", tool_calls: [{ name: "speak" }] },
  });
  assert.equal(s.messages.length, before + 1);
  assert.equal(s.messages[s.messages.length - 1].content, "我是预言家。");
  assert.equal(r.message.seq, 41);
  assert.equal(r.message.thought, undefined);
  assert.equal(r.message.tool_calls, undefined);
  assert.equal(r.dirty.feed, true);
  assert.equal(r.dirty.channels, true);
  assert.equal(r.dirty.board, false);
  assert.equal(s.seq, 41); // the ledger seq follows the newest message
  assert.equal(JSON.stringify(s).includes("secret chain of thought"), false);
});

test("message.committed: duplicate seq ignored, late seq inserted in order, cap enforced", () => {
  const s = fresh();
  const n = s.messages.length;
  assert.equal(apply(s, "message.committed", F.msg(8, "public", "p01", "dup")).message, null);
  assert.equal(s.messages.length, n);
  apply(s, "message.committed", F.msg(10, "public", "p05", "late"));
  assert.deepEqual(s.messages.map((m) => m.seq), [3, 5, 8, 9, 10, 11, 12]);
  apply(s, "message.committed", F.msg(50, "public", "p05", "newest"));
  apply(s, "message.committed", F.msg(49, "public", "p05", "just before newest"));
  assert.equal(s.messages[s.messages.length - 1].seq, 50);
  assert.equal(s.seq, 50);

  const big = Store.normalizeSession({ messages: [] });
  for (let i = 1; i <= Store.MAX_MESSAGES + 25; i++) apply(big, "message.committed", F.msg(i, "public", "p01", "m" + i));
  assert.equal(big.messages.length, Store.MAX_MESSAGES);
  assert.equal(big.messages[0].seq, 26); // the oldest ones were dropped
});

test("truncated messages are flagged (the feed appends an ellipsis)", () => {
  const s = fresh();
  const r = apply(s, "message.committed", F.msg(60, "public", "p02", "…", "speech", F.EVERYONE, { truncated: { dropped_chars: 45, kept_chars: 300 } }));
  assert.deepEqual(r.message.truncated, { dropped_chars: 45, kept_chars: 300 });
  assert.equal(Store.isTruncated(r.message), true);
  assert.equal(Store.isTruncated({ truncated: null }), false);
  assert.equal(Store.isTruncated({ truncated: {} }), false);
  assert.equal(Store.isTruncated({ truncated: true }), true);
});

// ---- channels / floor -------------------------------------------------------------
test("channel.created / channel.updated upsert by id and keep the list order", () => {
  const s = fresh();
  const n = s.channels.length;
  const created = F.channel("gods", "神职频道", ["moderator", "p04", "p05"]);
  let r = apply(s, "channel.created", { channel: created });
  assert.equal(s.channels.length, n + 1);
  assert.equal(r.dirty.channels, true);
  assert.equal(r.dirty.board, true);
  r = apply(s, "channel.updated", { channel: { ...created, name: "神职", mode: "free", frozen: true } });
  assert.equal(s.channels.length, n + 1);
  const c = Store.findById(s.channels, "gods");
  assert.equal(c.name, "神职");
  assert.equal(c.frozen, true);
  assert.equal(s.channels[s.channels.length - 1].id, "gods");
  const idxPublic = s.channels.findIndex((x) => x.id === "public");
  apply(s, "channel.updated", { channel: { ...F.channel("public", "大厅", F.EVERYONE), mode: "closed" } });
  assert.equal(s.channels.findIndex((x) => x.id === "public"), idxPublic);
  assert.equal(Store.findById(s.channels, "public").name, "大厅");
});

test("floor.changed replaces that channel's FloorView; unknown channels are ignored", () => {
  const s = fresh();
  const floor = {
    kind: "queue",
    active: "p05",
    paused: false,
    seconds_left: 30,
    queue: [
      { holder: "p04", state: "done", messages_used: 1, max_messages: 2 },
      { holder: "p05", state: "active", messages_used: 0, max_messages: 2 },
      { holder: "p06", state: "pending", messages_used: 0, max_messages: 2 },
    ],
    free: null,
  };
  const r = apply(s, "floor.changed", { channel_id: "public", floor });
  assert.equal(r.dirty.channels, true);
  assert.equal(r.dirty.board, true);
  const ch = Store.findById(s.channels, "public");
  assert.equal(ch.floor.kind, "queue");
  assert.equal(ch.floor.active, "p05");
  assert.equal(ch.floor.queue.length, 3);
  const none = apply(s, "floor.changed", { channel_id: "ghost", floor });
  assert.equal(none.dirty.channels, false);
});

// ---- players / agents --------------------------------------------------------------
test("player.updated sets tags and out; agent.status sets the runtime state", () => {
  const s = fresh();
  let r = apply(s, "player.updated", { player_id: "p05", tags: ["女巫", "出局"], out: true });
  const p = Store.findById(s.participants, "p05");
  assert.deepEqual(p.tags, ["女巫", "出局"]);
  assert.equal(p.out, true);
  assert.equal(r.dirty.board, true);
  assert.equal(apply(s, "player.updated", { player_id: "ghost", tags: [], out: true }).dirty.board, false);

  r = apply(s, "agent.status", { id: "p05", state: "thinking" });
  assert.equal(p.state, "thinking");
  assert.equal(r.dirty.board, true);
  apply(s, "agent.status", { id: "moderator", state: "acting" });
  assert.equal(s.moderator.state, "acting");
  apply(s, "agent.status", { id: "p05", state: "bogus" });
  assert.equal(p.state, "idle");
  assert.equal(apply(s, "agent.status", { id: "ghost", state: "thinking" }).dirty.board, false);
});

test("agent.metrics sets the per-agent numbers and keeps the session totals live (deltas)", () => {
  const s = fresh();
  const before = { ...s.usage_stats };
  const r = apply(s, "agent.metrics", {
    id: "p01",
    calls: 5,
    prompt_tokens: 1000, // was 600 -> +400
    completion_tokens: 250, // was 100 -> +150
    cost_usd: 0.01, // was 0.006 -> +0.004
    tool_calls: 8,
    thinking_rounds: 4,
    rejections: 2,
  });
  assert.equal(r.dirty.topbar, true);
  assert.equal(r.dirty.popover, true);
  assert.deepEqual(s.usage_stats.by_actor.p01, {
    calls: 5,
    prompt_tokens: 1000,
    completion_tokens: 250,
    cost_usd: 0.01,
    tool_calls: 8,
    thinking_rounds: 4,
    rejections: 2,
  });
  assert.equal(s.usage_stats.prompt_tokens, before.prompt_tokens + 400);
  assert.equal(s.usage_stats.completion_tokens, before.completion_tokens + 150);
  assert.equal(s.usage_stats.total_tokens, before.total_tokens + 550);
  assert.ok(Math.abs(s.usage_stats.total_cost_usd - (before.total_cost_usd + 0.004)) < 1e-9);

  // a first report for a new agent starts from zero; repeating it changes nothing
  apply(s, "agent.metrics", { id: "p09", calls: 1, prompt_tokens: 100, completion_tokens: 10, cost_usd: 0.001 });
  const total = s.usage_stats.total_tokens;
  apply(s, "agent.metrics", { id: "p09", calls: 1, prompt_tokens: 100, completion_tokens: 10, cost_usd: 0.001 });
  assert.equal(s.usage_stats.total_tokens, total);
  assert.equal(s.usage_stats.by_actor.p09.tool_calls, undefined); // only reported keys are stored
  // totals never go negative
  apply(s, "agent.metrics", { id: "p09", prompt_tokens: 0, completion_tokens: 0, cost_usd: 0 });
  assert.ok(s.usage_stats.total_tokens >= 0);
});

// ---- requests ------------------------------------------------------------------------
test("request.opened -> progress -> closed keeps one RequestView up to date", () => {
  const s = fresh();
  const request = {
    id: "r1",
    kind: "choice",
    question: "今天放逐谁？",
    options: [{ id: "p01", label: "p01" }, { id: "p02", label: "p02" }],
    to: F.PLAYER_IDS,
    answered_by: [],
    status: "open",
    sealed: true,
    seconds_left: 90,
  };
  let r = apply(s, "request.opened", { request });
  assert.equal(r.dirty.topbar, true);
  assert.equal(s.requests.length, 1);
  assert.equal(s.requests[0].sealed, true);

  r = apply(s, "request.progress", { request_id: "r1", answered_by: ["p01", "p02", "p03"], total: 12 });
  assert.deepEqual(s.requests[0].answered_by, ["p01", "p02", "p03"]);
  assert.equal(r.dirty.topbar, true);
  assert.equal(s.requests[0].status, "open");

  r = apply(s, "request.closed", { request_id: "r1", reason: "all_answered", answered_by: F.PLAYER_IDS, defaulted: [] });
  assert.equal(s.requests[0].status, "closed");
  assert.equal(s.requests[0].answered_by.length, 12);
  assert.equal(s.requests[0].seconds_left, null);

  apply(s, "request.opened", { request: { ...request, id: "r2" } });
  apply(s, "request.closed", { request_id: "r2", reason: "cancelled", answered_by: [], defaulted: [] });
  assert.equal(Store.findById(s.requests, "r2").status, "cancelled");
  apply(s, "request.closed", { request_id: "r3", reason: "deadline", answered_by: [], defaulted: ["p01"] }); // unknown: no crash
  assert.equal(s.requests.length, 2);
});

test("request.opened for an existing id replaces it (a re-open never duplicates)", () => {
  const s = fresh();
  const base = { id: "r1", kind: "choice", question: "q", options: [], to: ["p01"], answered_by: [], status: "open", sealed: false, seconds_left: null };
  apply(s, "request.opened", { request: base });
  apply(s, "request.opened", { request: { ...base, question: "q2" } });
  assert.equal(s.requests.length, 1);
  assert.equal(s.requests[0].question, "q2");
});

// ---- challenge tickets ------------------------------------------------------------------
test("challenge.opened / challenge.resolved keep the ticket list", () => {
  const s = fresh();
  let r = apply(s, "challenge.opened", { ticket: { id: "t2", player_id: "p08", status: "open", verdict: null, preview: "请复核", opened_seq: 50 } });
  assert.equal(r.dirty.channels, true);
  assert.equal(s.tickets.length, 2);
  r = apply(s, "challenge.resolved", { ticket_id: "t2", verdict: "upheld" });
  const t2 = Store.findById(s.tickets, "t2");
  assert.equal(t2.status, "resolved");
  assert.equal(t2.verdict, "upheld");
  assert.equal(r.dirty.channels, true);
  assert.equal(apply(s, "challenge.resolved", { ticket_id: "nope", verdict: "rejected" }).dirty.channels, false);
  assert.equal(Store.findById(s.tickets, "t1").status, "open");
});

// ---- misc UI events ----------------------------------------------------------------------
test("ability.used flashes the actor; phase.changed / dashboard.updated update the session", () => {
  const s = fresh();
  let r = apply(s, "ability.used", { ability_id: "white_wolf_explode", actor_id: "p12" });
  assert.deepEqual(r.flash, { actor_id: "p12", ability_id: "white_wolf_explode" });
  assert.equal(r.dirty.board, true);
  r = apply(s, "phase.changed", { text: "第 1 夜 · 黑夜" });
  assert.equal(s.phase_text, "第 1 夜 · 黑夜");
  assert.equal(r.dirty.topbar, true);
  r = apply(s, "dashboard.updated", { dashboard: { factions: [{ name: "狼人阵营", alive: 3, total: 4 }], tag_stats: [] } });
  assert.equal(s.dashboard.factions[0].alive, 3);
  assert.equal(r.dirty.dashboard, true);
});

test("game.status: finished records winner + summary; continuing withdraws them", () => {
  const s = fresh();
  let r = apply(s, "game.status", { status: "finished", reason: "end_game", winner: "好人阵营", summary: "狼人全部出局" });
  assert.equal(s.status, "finished");
  assert.equal(s.winner, "好人阵营");
  assert.equal(s.summary, "狼人全部出局");
  assert.equal(r.dirty.topbar, true);
  r = apply(s, "game.status", { status: "running", reason: "continued", winner: null });
  assert.equal(s.status, "running");
  assert.equal(s.winner, null);
  assert.equal(s.summary, "");
  // a finished event that omits the summary keeps what session.updated already set
  apply(s, "session.updated", { summary: "来自 session.updated" });
  apply(s, "game.status", { status: "finished", reason: "end_game", winner: "狼人阵营" });
  assert.equal(s.summary, "来自 session.updated");
  assert.equal(s.winner, "狼人阵营");
  // running clears the hold
  s.status = "paused";
  s.pause_reason = "hold";
  s.hold_reason = "请确认警长归属";
  apply(s, "game.status", { status: "running", reason: "resumed" });
  assert.equal(s.pause_reason, "");
  assert.equal(s.hold_reason, "");
});

test("session.updated merges top-level fields only; collections keep their own events", () => {
  const s = fresh();
  const participants = s.participants;
  const r = apply(s, "session.updated", {
    status: "paused",
    pause_reason: "hold",
    hold_reason: "请确认警长归属",
    active_status_text: "已暂停 · 主持人：请确认警长归属",
    model_bindings: { p01: { provider_id: "p1", model_id: "m", reasoning_depth: "low" } },
    binding_issues: [{ actor_id: "p02", message: "未绑定" }],
    restored_from_save: true,
    restore_notice: "已恢复",
    autosave_error: "",
    execution_depth: "high",
    title: "Arena X",
    participants: [], // protected
    messages: [], // protected
    channels: [], // protected
  });
  assert.equal(s.status, "paused");
  assert.equal(s.pause_reason, "hold");
  assert.equal(s.hold_reason, "请确认警长归属");
  assert.equal(s.execution_depth, "high");
  assert.equal(s.title, "Arena X");
  assert.equal(s.restored_from_save, true);
  assert.equal(s.binding_issues.length, 1);
  assert.equal(s.model_bindings.p01.model_id, "m");
  assert.equal(s.participants, participants);
  assert.equal(s.participants.length, 12);
  assert.ok(s.messages.length > 0);
  assert.ok(s.channels.length > 0);
  assert.equal(r.dirty.topbar, true);
  assert.equal(r.dirty.board, true);
  // untouched fields survive
  assert.equal(s.session_id, "s-test");
  assert.equal(s.phase_text, "第 1 天 · 白天发言");
  // type repair after a merge
  apply(s, "session.updated", { hold_reason: null, winner: "", model_bindings: null });
  assert.equal(s.hold_reason, "");
  assert.equal(s.winner, null);
  assert.deepEqual(s.model_bindings, {});
});

test("workspace.changed asks for a reload; error carries a toast; unknown / broken events are no-ops", () => {
  const s = fresh();
  assert.equal(apply(s, "workspace.changed", {}).reload, true);
  const e = apply(s, "error", { message: "boom" });
  assert.equal(e.toast, "boom");
  assert.equal(apply(s, "error", {}).toast.length > 0, true);
  // v0.11 streaming / thinking / tool events no longer exist: they are ignored, not rendered
  const snapshotBefore = JSON.stringify(s);
  for (const bad of [
    null,
    undefined,
    "x",
    5,
    {},
    { type: "thought.delta", data: { text: "x" } },
    { type: "stream_chunk", data: { text: "x" } },
    { type: "tool_call", data: { name: "speak" } },
  ]) {
    const r = Store.applyEvent(s, bad);
    assert.equal(r.reload, false);
    assert.equal(r.session, s);
    assert.equal(Store.anyDirty(r.dirty), false);
  }
  assert.equal(JSON.stringify(s), snapshotBefore);
});

// ---- ids / ordering ------------------------------------------------------------------------
test("event ids: stale replays are skipped, snapshots never are", () => {
  assert.equal(Store.eventIdOf({ id: 7 }), 7);
  assert.equal(Store.eventIdOf({ id: "8" }), 8);
  assert.equal(Store.eventIdOf({}, 3), 3);
  assert.equal(Store.eventIdOf(null, 4), 4);
  assert.equal(Store.isStaleEvent(10, { id: 10, type: "phase.changed" }), true);
  assert.equal(Store.isStaleEvent(10, { id: 9, type: "phase.changed" }), true);
  assert.equal(Store.isStaleEvent(10, { id: 11, type: "phase.changed" }), false);
  assert.equal(Store.isStaleEvent(10, { id: 3, type: "snapshot" }), false); // a restarted server may send a lower id
  assert.equal(Store.isStaleEvent(0, { id: 1, type: "phase.changed" }), false);
  assert.equal(Store.isStaleEvent(10, { type: "phase.changed" }), false); // no id: apply
});

test("an HTTP session older than the SSE stream is not adopted; a new game / unknown age is", () => {
  const cur = F.makeSession({ event_id: 100 });
  assert.equal(Store.shouldAdoptHttpSession(cur, F.makeSession({ event_id: 90 }), 100), false);
  assert.equal(Store.shouldAdoptHttpSession(cur, F.makeSession({ event_id: 100 }), 100), true);
  assert.equal(Store.shouldAdoptHttpSession(cur, F.makeSession({ event_id: 130 }), 100), true);
  assert.equal(Store.shouldAdoptHttpSession(cur, F.makeSession({ session_id: "new", event_id: 1 }), 100), true);
  assert.equal(Store.shouldAdoptHttpSession(cur, F.makeSession({ event_id: 0 }), 100), true);
  assert.equal(Store.shouldAdoptHttpSession(cur, F.makeSession({ event_id: 5 }), 0), true);
  assert.equal(Store.shouldAdoptHttpSession(null, F.makeSession(), 0), true);
  assert.equal(Store.shouldAdoptHttpSession(cur, null, 0), false);
});

test("mergeDirty / anyDirty combine repaint flags", () => {
  const a = Store.newDirty();
  assert.equal(Store.anyDirty(a), false);
  Store.mergeDirty(a, { feed: true });
  Store.mergeDirty(a, { board: true });
  assert.equal(a.feed && a.board, true);
  assert.equal(a.channels, false);
  assert.equal(Store.anyDirty(a), true);
  assert.equal(Store.anyDirty(Store.newDirty(true)), true);
});

// ---- runner -----------------------------------------------------------------------------------
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
