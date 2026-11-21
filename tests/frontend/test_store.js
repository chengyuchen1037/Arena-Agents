"use strict";
// SSE event application on a PublicSession v3 (static/js/store.js). Run: node tests/frontend/test_store.js
const assert = require("node:assert/strict");
const path = require("node:path");
const Store = require(path.join(__dirname, "..", "..", "static", "js", "store.js"));
const F = require("./fixtures.js");

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

const ev = (type, data = {}, id = 1) => ({ id, type, data });
const fresh = () => Store.normalizeSession(F.makeSession());
const apply = (s, type, data, id, ctx) => Store.applyEvent(s, ev(type, data, id), ctx);

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
  assert.equal(s.usage_stats.cache_read_tokens, 0);
  assert.equal(s.usage_stats.cache_write_tokens, 0);
  assert.equal(s.usage_stats.cache_hit_rate, 0);
  assert.equal(s.moderator.id, "moderator");
  assert.equal(s.moderator.state, "sleeping");
  assert.equal(s.moderator.sleep_until, null);
  assert.equal(s.event_id, 0);
  assert.deepEqual(s.model_bindings, {});
  // v0.12.1 fields
  assert.deepEqual(s.budget, { max_tokens: null, max_cost_usd: null, force_stop: false, used_tokens: 0, used_cost_usd: 0, state: "ok" });
  assert.equal(s.memory_enabled, true);
  // v0.14 (§13.8): supported (a v5 game cannot be reviewed), the 60 seconds, what a running review has left
  assert.deepEqual(s.review, { available: false, done: [], running: false, supported: true, deadline_s: 60, seconds_left: null, progress: {} });
  assert.equal(s.dealt, true, "a server that does not say has dealt: its games always show the identities");
  assert.deepEqual(s.system_status, { code: "", text: "", reason: "" });
  assert.deepEqual(s.bots, { lineup: [], bots: [], memory_enabled: true, init_job: null, ready: { ok: true, problems: [] }, learning_usage: { calls: 0, tokens: 0, cost_usd: 0 } });
  assert.equal(s.stall_notice, "");
  assert.equal(s.demo, false);
  assert.equal("execution_depth" in s, false, "the execution depth is gone");
});

test("normalizeSession drops malformed list items, repairs types and sorts messages by seq", () => {
  const s = Store.normalizeSession({
    participants: [null, "x", { id: "N1", state: "bogus", tags: ["a", 1], seat: "7", sleep_until: "12.5" }],
    channels: [{ id: "c1", members: ["N1"], floor: { queue: [null, { holder: "N1" }] } }, 7],
    messages: [{ seq: 9, content: "b" }, { seq: 3, content: "a" }, null],
    requests: [{ id: "r1", to: ["N1"], options: [{ id: "a" }, null] }],
    tickets: [{ id: "t1", player_id: "N1" }],
    usage_stats: { by_actor: "nope" },
  });
  assert.equal(s.participants.length, 1);
  assert.equal(s.participants[0].state, "sleeping"); // unknown states are "not running"
  assert.deepEqual(s.participants[0].tags, ["a", "1"]);
  assert.equal(s.participants[0].seat, 7); // seats arrive as strings
  assert.equal(s.participants[0].sleep_until, 12.5);
  assert.equal(s.participants[0].avatar, null);
  assert.equal(s.channels.length, 1);
  assert.equal(s.channels[0].name, "c1"); // name falls back to the id
  assert.equal(s.channels[0].floor.queue.length, 1);
  assert.deepEqual(s.messages.map((m) => m.seq), [3, 9]);
  assert.equal(s.requests[0].options[0].label, "a");
  assert.equal(s.requests[0].status, "open");
  assert.deepEqual(s.requests[0].answers, []);
  assert.equal(s.requests[0].result, null);
  assert.equal(s.tickets[0].status, "open");
  assert.deepEqual(s.usage_stats.by_actor, {});
});

test("legacy agent states (idle / stopped) are not special any more: they count as sleeping", () => {
  const s = Store.normalizeSession({ participants: [{ id: "N1", state: "idle" }, { id: "N2", state: "stopped" }, { id: "N3", state: "thinking" }, { id: "N4", state: "acting" }] });
  assert.deepEqual(s.participants.map((p) => p.state), ["sleeping", "sleeping", "thinking", "acting"]);
  // (v0.13: a seat a person plays; v0.13.1: an agent held in front of the soft-pause gate)
  assert.deepEqual(Store.AGENT_STATES, ["thinking", "acting", "sleeping", "human", "held"]);
});

test("normalizeRequest keeps the fields of RequestView and turns seconds_left into an absolute game-clock deadline", () => {
  const r = Store.normalizeRequest(F.makeRequest({ seconds_left: 30, answers: [F.answer("N3", "N5", "5号 Eve")] }), { clockNow: 100 });
  assert.equal(r.share_answers, true);
  assert.equal(r.auto_close, "unanimous");
  assert.equal(r.opened_seq, 6);
  assert.equal(r.deadline_clock, 130);
  assert.equal(r.answers[0].label, "5号 Eve");
  assert.equal(Store.normalizeRequest({ id: "x" }).deadline_clock, null);
  assert.equal(Store.normalizeRequest({ id: "x", seconds_left: 5 }).deadline_clock, 5); // no clock known: relative to 0
  const closed = Store.normalizeRequest({ id: "x", status: "closed", result: { reason: "unanimous", tally: { N5: 4 }, leaders: ["N5"], defaulted: [], missing: [] } });
  assert.deepEqual(closed.result.tally, { N5: 4 });
  assert.deepEqual(closed.result.leaders, ["N5"]);
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

test("a snapshot's open requests get their deadline from the snapshot's own clock", () => {
  const s = fresh();
  const r = apply(s, "snapshot", { session: F.makeSession({ clock: 200, requests: [F.makeRequest({ seconds_left: 45 })] }) });
  assert.equal(r.session.requests[0].deadline_clock, 245);
});

// ---- messages -------------------------------------------------------------------
test("message.committed appends one whole message; thoughts / tool data are never kept", () => {
  const s = fresh();
  const before = s.messages.length;
  const r = Store.applyEvent(s, {
    id: 101,
    type: "message.committed",
    data: { ...F.msg(41, "public", "N4", "我是预言家。"), thought: "secret chain of thought", tool_calls: [{ name: "speak" }] },
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
  assert.equal(apply(s, "message.committed", F.msg(8, "public", "N1", "dup")).message, null);
  assert.equal(s.messages.length, n);
  apply(s, "message.committed", F.msg(10, "public", "N5", "late"));
  assert.deepEqual(s.messages.map((m) => m.seq), [3, 5, 8, 9, 10, 11, 12]);
  apply(s, "message.committed", F.msg(50, "public", "N5", "newest"));
  apply(s, "message.committed", F.msg(49, "public", "N5", "just before newest"));
  assert.equal(s.messages[s.messages.length - 1].seq, 50);
  assert.equal(s.seq, 50);

  const big = Store.normalizeSession({ messages: [] });
  for (let i = 1; i <= Store.MAX_MESSAGES + 25; i++) apply(big, "message.committed", F.msg(i, "public", "N1", "m" + i));
  assert.equal(big.messages.length, Store.MAX_MESSAGES);
  assert.equal(big.messages[0].seq, 26); // the oldest ones were dropped
});

test("truncated messages are flagged (the feed appends an ellipsis)", () => {
  const s = fresh();
  const r = apply(s, "message.committed", F.msg(60, "public", "N2", "…", "speech", F.EVERYONE, { truncated: { dropped_chars: 45, kept_chars: 300 } }));
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
  const created = F.channel("gods", "神职频道", ["moderator", "N4", "N5"]);
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
    active: "N5",
    paused: false,
    seconds_left: 30,
    queue: [
      { holder: "N4", state: "done", messages_used: 1, max_messages: 2 },
      { holder: "N5", state: "active", messages_used: 0, max_messages: 2 },
      { holder: "N6", state: "pending", messages_used: 0, max_messages: 2 },
    ],
    free: null,
  };
  const r = apply(s, "floor.changed", { channel_id: "public", floor });
  assert.equal(r.dirty.channels, true);
  const ch = Store.findById(s.channels, "public");
  assert.equal(ch.floor.kind, "queue");
  assert.equal(ch.floor.active, "N5");
  assert.equal(ch.floor.queue.length, 3);
  const none = apply(s, "floor.changed", { channel_id: "ghost", floor });
  assert.equal(none.dirty.channels, false);
});

// ---- players / agents --------------------------------------------------------------
test("player.updated sets tags and out", () => {
  const s = fresh();
  const r = apply(s, "player.updated", { player_id: "N5", tags: ["女巫", "出局"], out: true });
  const p = Store.findById(s.participants, "N5");
  assert.deepEqual(p.tags, ["女巫", "出局"]);
  assert.equal(p.out, true);
  assert.equal(r.dirty.board, true);
  assert.equal(apply(s, "player.updated", { player_id: "ghost", tags: [], out: true }).dirty.board, false);
});

test("agent.status: three states; `until` only belongs to a sleeping agent", () => {
  const s = fresh();
  const p = Store.findById(s.participants, "N5");
  let r = apply(s, "agent.status", { id: "N5", state: "thinking", until: null });
  assert.equal(p.state, "thinking");
  assert.equal(p.sleep_until, null);
  assert.equal(r.dirty.board, true);
  apply(s, "agent.status", { id: "N5", state: "acting" });
  assert.equal(p.state, "acting");
  apply(s, "agent.status", { id: "N5", state: "sleeping", until: 42.5 });
  assert.equal(p.state, "sleeping");
  assert.equal(p.sleep_until, 42.5);
  // waking up clears the alarm even if the server sent one
  apply(s, "agent.status", { id: "N5", state: "thinking", until: 99 });
  assert.equal(p.sleep_until, null);
  apply(s, "agent.status", { id: "N5", state: "sleeping" }); // an untimed sleep
  assert.equal(p.sleep_until, null);
  // the moderator has the same states
  r = apply(s, "agent.status", { id: "moderator", state: "acting" });
  assert.equal(s.moderator.state, "acting");
  assert.equal(r.dirty.topbar, true);
  apply(s, "agent.status", { id: "moderator", state: "sleeping", until: 20 });
  assert.equal(s.moderator.sleep_until, 20);
  // unknown states count as sleeping; unknown agents are ignored
  apply(s, "agent.status", { id: "N5", state: "bogus" });
  assert.equal(p.state, "sleeping");
  assert.equal(apply(s, "agent.status", { id: "ghost", state: "thinking" }).dirty.board, false);
});

test("agent.metrics: per-agent numbers incl. cache tokens; the session totals follow by deltas", () => {
  const s = fresh();
  const before = { ...s.usage_stats };
  const r = apply(s, "agent.metrics", {
    id: "N1",
    activations: 6,
    calls: 5,
    prompt_tokens: 1000, // was 600 -> +400
    cache_read_tokens: 500, // was 300 -> +200
    cache_write_tokens: 50, // was 50 -> +0
    completion_tokens: 250, // was 100 -> +150
    cost_usd: 0.01, // was 0.006 -> +0.004
    tool_calls: 8,
    thinking_rounds: 4,
    rejections: 2,
    cache_hit_rate: 0.3226,
  });
  assert.equal(r.dirty.topbar, true);
  assert.equal(r.dirty.popover, true);
  assert.deepEqual(s.usage_stats.by_actor.N1, {
    activations: 6,
    calls: 5,
    prompt_tokens: 1000,
    cache_read_tokens: 500,
    cache_write_tokens: 50,
    completion_tokens: 250,
    cost_usd: 0.01,
    tool_calls: 8,
    thinking_rounds: 4,
    rejections: 2,
    cache_hit_rate: 0.3226,
  });
  assert.equal(s.usage_stats.prompt_tokens, before.prompt_tokens + 400);
  assert.equal(s.usage_stats.cache_read_tokens, before.cache_read_tokens + 200);
  assert.equal(s.usage_stats.cache_write_tokens, before.cache_write_tokens);
  assert.equal(s.usage_stats.completion_tokens, before.completion_tokens + 150);
  assert.equal(s.usage_stats.total_tokens, before.total_tokens + 400 + 200 + 150); // all four kinds
  assert.ok(Math.abs(s.usage_stats.total_cost_usd - (before.total_cost_usd + 0.004)) < 1e-9);
  const u = s.usage_stats;
  assert.ok(Math.abs(u.cache_hit_rate - u.cache_read_tokens / (u.prompt_tokens + u.cache_read_tokens + u.cache_write_tokens)) < 1e-9);

  // a first report for a new agent starts from zero; repeating it changes nothing
  apply(s, "agent.metrics", { id: "N9", calls: 1, prompt_tokens: 100, cache_read_tokens: 40, completion_tokens: 10, cost_usd: 0.001 });
  const total = s.usage_stats.total_tokens;
  apply(s, "agent.metrics", { id: "N9", calls: 1, prompt_tokens: 100, cache_read_tokens: 40, completion_tokens: 10, cost_usd: 0.001 });
  assert.equal(s.usage_stats.total_tokens, total);
  assert.equal(s.usage_stats.by_actor.N9.tool_calls, undefined); // only reported keys are stored
  // totals never go negative
  apply(s, "agent.metrics", { id: "N9", prompt_tokens: 0, completion_tokens: 0, cost_usd: 0 });
  assert.ok(s.usage_stats.total_tokens >= 0);
});

// ---- requests ------------------------------------------------------------------------
test("request.opened -> progress -> closed keeps one RequestView up to date", () => {
  const s = fresh();
  const request = F.makeRequest({ id: "r1", to: F.PLAYER_IDS, sealed: true, share_answers: false, auto_close: "all_answered", seconds_left: 90, opened_seq: 0 });
  let r = apply(s, "request.opened", { request }, 1, { clockNow: 50 });
  assert.equal(r.dirty.topbar, true);
  assert.equal(r.dirty.feed, true);
  assert.equal(s.requests.length, 1);
  assert.equal(s.requests[0].sealed, true);
  assert.equal(s.requests[0].deadline_clock, 140);
  assert.equal(s.requests[0].opened_seq, s.seq, "a request without a ledger position is placed after what we have seen");

  r = apply(s, "request.progress", { request_id: "r1", answered_by: ["N1", "N2", "N3"], total: 12 });
  assert.deepEqual(s.requests[0].answered_by, ["N1", "N2", "N3"]);
  assert.equal(s.requests[0].total, 12);
  assert.equal(r.dirty.topbar, true);
  assert.equal(s.requests[0].status, "open");

  r = apply(s, "request.closed", { request_id: "r1", reason: "all_answered", answered_by: F.PLAYER_IDS, defaulted: [], result: { reason: "all_answered", tally: { N1: 7, N2: 5 }, leaders: ["N1"], defaulted: [], missing: [] } });
  assert.equal(s.requests[0].status, "closed");
  assert.equal(s.requests[0].answered_by.length, 12);
  assert.equal(s.requests[0].seconds_left, null);
  assert.equal(s.requests[0].deadline_clock, null);
  assert.deepEqual(s.requests[0].result.tally, { N1: 7, N2: 5 });
  assert.deepEqual(s.requests[0].result.leaders, ["N1"]);

  apply(s, "request.opened", { request: { ...request, id: "r2" } });
  apply(s, "request.closed", { request_id: "r2", reason: "cancelled", answered_by: [], defaulted: [] });
  assert.equal(Store.findById(s.requests, "r2").status, "cancelled");
  assert.equal(Store.findById(s.requests, "r2").result.reason, "cancelled");
  apply(s, "request.closed", { request_id: "r3", reason: "deadline", answered_by: [], defaulted: ["N1"] }); // unknown: no crash
  assert.equal(s.requests.length, 2);
});

test("request.answer: answers land in the card; a changed answer replaces the old one and is counted", () => {
  const s = fresh();
  apply(s, "request.opened", { request: F.makeRequest() });
  const answer = (pid, value, label) => ({ request_id: "r1", player_id: pid, value, label, reason: "", visible_to_ids: ["moderator", ...F.WOLVES] });
  let r = apply(s, "request.answer", answer("N3", "N5", "5号 Eve"));
  assert.equal(r.dirty.feed, true);
  assert.equal(r.dirty.topbar, true);
  const req = s.requests[0];
  assert.deepEqual(req.answers.map((a) => [a.player_id, a.value, a.label]), [["N3", "N5", "5号 Eve"]]);
  assert.deepEqual(req.answered_by, ["N3"]);
  apply(s, "request.answer", answer("N6", "N4", "4号 David"));
  assert.equal(req.answers.length, 2);
  // N6 changes their mind: replaced in place, and the change is counted
  apply(s, "request.answer", answer("N6", "N5", "5号 Eve"));
  assert.equal(req.answers.length, 2);
  assert.equal(req.answers.find((a) => a.player_id === "N6").value, "N5");
  assert.deepEqual(req.answer_changes, { N6: 1 });
  assert.deepEqual(req.answered_by, ["N3", "N6"]);
  // the same answer again is not a change
  apply(s, "request.answer", answer("N6", "N5", "5号 Eve"));
  assert.deepEqual(req.answer_changes, { N6: 1 });
  // answers for unknown requests / without a player are ignored
  assert.equal(apply(s, "request.answer", { request_id: "ghost", player_id: "N3", value: "x", label: "x" }).dirty.feed, false);
  assert.equal(apply(s, "request.answer", { request_id: "r1", value: "x" }).dirty.feed, false);
});

test("request.answer keeps answers and answered_by in SEAT order (N10 after N9), whatever the arrival order", () => {
  const s = fresh();
  apply(s, "request.opened", { request: F.makeRequest({ to: F.PLAYER_IDS, share_answers: false, sealed: true }) });
  const answer = (pid) => apply(s, "request.answer", { request_id: "r1", player_id: pid, value: "N5", label: "5号 Eve", reason: "", visible_to_ids: ["moderator"] });
  for (const pid of ["N10", "N2", "N12", "N1", "N9"]) answer(pid);
  const req = s.requests[0];
  assert.deepEqual(req.answers.map((a) => a.player_id), ["N1", "N2", "N9", "N10", "N12"]);
  assert.deepEqual(req.answered_by, ["N1", "N2", "N9", "N10", "N12"]);
  // a player the session does not know is ordered by the number in the id, then last
  answer("X7");
  answer("guest");
  assert.deepEqual(req.answers.map((a) => a.player_id), ["N1", "N2", "X7", "N9", "N10", "N12", "guest"]);
});

test("request.opened for an existing id replaces it (a re-open never duplicates)", () => {
  const s = fresh();
  const base = F.makeRequest({ id: "r1", question: "q" });
  apply(s, "request.opened", { request: base });
  apply(s, "request.opened", { request: { ...base, question: "q2" } });
  assert.equal(s.requests.length, 1);
  assert.equal(s.requests[0].question, "q2");
});

// ---- challenge tickets ------------------------------------------------------------------
test("challenge.opened / challenge.resolved keep the ticket list", () => {
  const s = fresh();
  let r = apply(s, "challenge.opened", { ticket: { id: "t2", player_id: "N8", status: "open", verdict: null, preview: "请复核", opened_seq: 50 } });
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
  let r = apply(s, "ability.used", { ability_id: "white_wolf_explode", actor_id: "N12" });
  assert.deepEqual(r.flash, { actor_id: "N12", ability_id: "white_wolf_explode" });
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
    model_bindings: { N1: { provider_id: "p1", model_id: "m", reasoning_depth: "low" } },
    binding_issues: [{ actor_id: "N2", message: "未绑定" }],
    restored_from_save: true,
    restore_notice: "已恢复",
    autosave_error: "",
    title: "Arena X",
    participants: [], // protected
    messages: [], // protected
    channels: [], // protected
  });
  assert.equal(s.status, "paused");
  assert.equal(s.pause_reason, "hold");
  assert.equal(s.hold_reason, "请确认警长归属");
  assert.equal(s.title, "Arena X");
  assert.equal(s.restored_from_save, true);
  assert.equal(s.binding_issues.length, 1);
  assert.equal(s.model_bindings.N1.model_id, "m");
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
  apply(s, "session.updated", { hold_reason: null, winner: "", model_bindings: null, stall_notice: null });
  assert.equal(s.hold_reason, "");
  assert.equal(s.winner, null);
  assert.deepEqual(s.model_bindings, {});
  assert.equal(s.stall_notice, "");
});

test("session.updated carries the v0.12.1 fields: budget, memory_enabled, review, stall_notice (and the pause reason 'budget')", () => {
  const s = fresh();
  apply(s, "session.updated", {
    status: "paused",
    pause_reason: "budget",
    budget: { max_tokens: 1000, max_cost_usd: 2.5, force_stop: true, used_tokens: 1000, used_cost_usd: 0.4, state: "reached" },
    memory_enabled: false,
    review: { available: true, done: ["N1"], running: false },
    stall_notice: "对局似乎停滞了",
  });
  assert.equal(s.pause_reason, "budget");
  assert.deepEqual(s.budget, { max_tokens: 1000, max_cost_usd: 2.5, force_stop: true, used_tokens: 1000, used_cost_usd: 0.4, state: "reached" });
  assert.equal(s.memory_enabled, false);
  assert.deepEqual([s.review.available, s.review.done, s.review.running], [true, ["N1"], false]);
  assert.equal(s.stall_notice, "对局似乎停滞了");
  // the stall notice goes away when progress resumes
  apply(s, "session.updated", { stall_notice: "" });
  assert.equal(s.stall_notice, "");
  // live review progress survives a session.updated that carries the review summary
  apply(s, "review.progress", { id: "N2", state: "running", message: "…" });
  apply(s, "session.updated", { review: { available: true, done: ["N1"], running: true } });
  assert.equal(s.review.progress.N2.state, "running");
  assert.equal(s.review.running, true);
});

// ---- budget / roster / review events ------------------------------------------------------------
test("budget.updated / budget.warning / budget.reached replace the budget and report the severity", () => {
  const s = fresh();
  const b = (used, state) => ({ budget: { max_tokens: 1000, max_cost_usd: null, force_stop: true, used_tokens: used, used_cost_usd: 0.1, state } });
  let r = apply(s, "budget.updated", b(500, "ok"));
  assert.equal(s.budget.used_tokens, 500);
  assert.equal(r.budgetEvent, null);
  assert.equal(r.dirty.topbar, true);
  r = apply(s, "budget.warning", b(800, "warning"));
  assert.equal(r.budgetEvent, "warning");
  assert.equal(s.budget.state, "warning");
  r = apply(s, "budget.reached", b(1000, "reached"));
  assert.equal(r.budgetEvent, "reached");
  assert.equal(s.budget.state, "reached");
  assert.equal(s.budget.force_stop, true);
  // a malformed budget changes nothing
  apply(s, "budget.updated", { budget: "x" });
  assert.equal(s.budget.state, "reached");
  // negative / garbage numbers are repaired
  const n = Store.normalizeBudget({ max_tokens: -5, max_cost_usd: "abc", used_tokens: -1, used_cost_usd: "x", state: "weird" });
  assert.deepEqual(n, { max_tokens: null, max_cost_usd: null, force_stop: false, used_tokens: 0, used_cost_usd: 0, state: "ok" });
});

test("roster.updated renames and re-draws the listed players only", () => {
  const s = fresh();
  const avatar = F.avatarOf(99);
  const r = apply(s, "roster.updated", { participants: [{ id: "N3", name: "Caroline", avatar }, { id: "N4", name: "David" }, { id: "ghost", name: "X" }] });
  assert.equal(r.rosterChanged, true);
  assert.equal(r.dirty.board, true);
  assert.equal(r.dirty.channels, true);
  assert.equal(r.dirty.feed, true);
  assert.equal(Store.findById(s.participants, "N3").name, "Caroline");
  assert.deepEqual(Store.findById(s.participants, "N3").avatar, avatar);
  assert.equal(Store.findById(s.participants, "N4").name, "David");
  assert.equal(Store.findById(s.participants, "N1").name, "Alice");
  assert.equal(s.participants.length, 12);
});

test("the moderator has a random avatar of its own: a snapshot keeps it, roster.updated changes it", () => {
  const s = fresh();
  assert.equal(s.moderator.avatar, null, "the fixture session has none: the renderer then draws the fixed top hat");
  const av = F.avatarOf(7);
  s.moderator = Store.normalizeSession({ ...s, moderator: { id: "moderator", name: "主持人", avatar: av, state: "sleeping" } }).moderator;
  assert.deepEqual(s.moderator.avatar, av);
  // garbage is repaired to "no avatar", never an exception
  assert.equal(Store.normalizeSession({ ...s, moderator: { id: "moderator", avatar: "x" } }).moderator.avatar, null);
  assert.equal(Store.normalizeSession({ ...s, moderator: { id: "moderator", avatar: [1] } }).moderator.avatar, null);

  // the moderator's entry travels as ``moderator`` (it is not a participant) and repaints the same views
  const next = F.avatarOf(8);
  const r = apply(s, "roster.updated", { participants: [], moderator: { id: "moderator", avatar: next } });
  assert.deepEqual(s.moderator.avatar, next);
  assert.equal(r.rosterChanged, true);
  assert.equal(r.dirty.topbar, true);
  assert.equal(r.dirty.feed, true, "the moderator's messages in the feed carry its avatar");
  assert.equal(s.participants.length, 12, "the moderator is not added to the players");
  // an event without a usable moderator leaves it alone
  apply(s, "roster.updated", { participants: [{ id: "N1", name: "Alicia" }], moderator: { id: "moderator", avatar: "nope" } });
  assert.deepEqual(s.moderator.avatar, next);
  apply(s, "roster.updated", { participants: [] });
  assert.deepEqual(s.moderator.avatar, next);
});

test("review.progress tracks running / done / error per player; done players are remembered", () => {
  const s = fresh();
  let r = apply(s, "review.progress", { id: "N1", state: "running", message: "正在复盘…" });
  assert.deepEqual(r.reviewEvent, { id: "N1", state: "running", message: "正在复盘…", bot_id: "", name: "", seconds_left: null, deadline_s: 60 });
  assert.equal(r.dirty.topbar, true, "the 复盘全部 button shows the progress");
  assert.equal(r.dirty.board, true);
  assert.equal(s.review.running, true);
  assert.equal(s.review.progress.N1.state, "running");
  r = apply(s, "review.progress", { id: "N1", state: "done", message: "已写入" });
  assert.equal(r.reviewEvent.state, "done");
  assert.deepEqual(s.review.done, ["N1"]);
  assert.equal(s.review.running, false);
  r = apply(s, "review.progress", { id: "N2", state: "error", message: "模型不可用" });
  assert.equal(s.review.progress.N2.state, "error");
  assert.deepEqual(s.review.done, ["N1"]);
  apply(s, "review.progress", { id: "N1", state: "done" }); // idempotent
  assert.deepEqual(s.review.done, ["N1"]);
  assert.equal(apply(s, "review.progress", {}).reviewEvent, null);
  // an unknown state counts as running
  apply(s, "review.progress", { id: "N3", state: "weird" });
  assert.equal(s.review.progress.N3.state, "running");
});

test("archive.progress is normalized and leaves the game alone; error carries a toast; unknown / broken events are no-ops", () => {
  const s = fresh();
  const before = JSON.stringify(s);
  const r0 = apply(s, "archive.progress", { job_id: "j1", kind: "import", phase: "replace", done: "3", total: 7, status: "running" });
  assert.deepEqual(r0.archiveEvent, { job_id: "j1", kind: "import", phase: "replace", done: 3, total: 7, status: "running", message: "", code: "", result: null });
  assert.equal(JSON.stringify(s), before, "the session is not touched");
  assert.equal(Store.anyDirty(r0.dirty), false, "nothing to repaint");
  // a finished job carries its result; an error its message and code; garbage is made harmless
  const done = apply(s, "archive.progress", { job_id: "j1", kind: "export", status: "done", phase: "finalize", done: 1, total: 1, result: { name: "arena-x.arenaagents" } }).archiveEvent;
  assert.deepEqual([done.status, done.result.name], ["done", "arena-x.arenaagents"]);
  const bad = apply(s, "archive.progress", { job_id: "j2", kind: "weird", status: "weird", done: -4, total: "x", result: "nope" }).archiveEvent;
  assert.deepEqual([bad.kind, bad.status, bad.done, bad.total, bad.result], ["export", "running", 0, 0, null]);
  assert.equal(apply(s, "phase.changed", { text: "x" }).archiveEvent, null);
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
    assert.equal(r.archiveEvent, null);
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

// ---- v0.14: the bots at the table, the deal, the status label, the init / review progress (contract §13.7 / §13.8) -------------
test("a session carries `dealt`, `system_status` and `bots`; every participant knows its bot and the bot's initialization status", () => {
  const s = Store.normalizeSession(F.makeSession({ status: "idle", dealt: false, system_status: { code: "before_game", text: "对局前", reason: "" } }));
  assert.equal(s.dealt, false);
  assert.deepEqual(s.system_status, { code: "before_game", text: "对局前", reason: "" });
  assert.equal(s.bots.bots.length, 12);
  assert.deepEqual(s.bots.lineup, F.BOT_IDS);
  assert.deepEqual(s.participants.map((p) => p.bot_id), F.BOT_IDS);
  assert.ok(s.participants.every((p) => p.init_status === "ready"));
  // a v5 game has no bots: "" and null; an unknown status is "not known" (null), never a made-up ✔
  const v5 = Store.normalizeSession({ participants: [{ id: "N1" }, { id: "N2", bot_id: "b-1", init_status: "weird" }] });
  assert.deepEqual([v5.participants[0].bot_id, v5.participants[0].init_status], ["", null]);
  assert.deepEqual([v5.participants[1].bot_id, v5.participants[1].init_status], ["b-1", null]);
  // an unknown system status code is shown as none (the page then falls back to the old status text)
  assert.deepEqual(Store.normalizeSystemStatus({ code: "weird", text: "x", reason: "y" }), { code: "", text: "x", reason: "y" });
});

test("normalizeBots: every field typed, problems grouped, `limits` / `defaults` kept only when they are there", () => {
  const raw = F.makeBotsFull({
    init_job: { job_id: "j1", state: "running", ids: [F.botId(1)], started_at: 1763800000, deadline_s: 120 },
    ready: { ok: false, problems: [F.makeProblem("bot", [{ code: "stale", message: "初始化之后契约变了。", fix: "init" }], { bot_id: F.botId(2), seat: "N2", name: "Bob" })] },
  });
  const b = Store.normalizeBots(raw);
  assert.equal(b.bots.length, 12);
  assert.deepEqual(b.bots[0].core.chars, raw.bots[0].core.chars);
  assert.equal(b.bots[0].init.status, "ready");
  assert.equal(b.bots[0].frozen, true);
  assert.deepEqual(b.init_job, { job_id: "j1", state: "running", ids: [F.botId(1)], started_at: 1763800000, deadline_s: 120 });
  assert.deepEqual(b.ready.problems[0], { scope: "bot", bot_id: F.botId(2), seat: "N2", name: "Bob", items: [{ code: "stale", message: "初始化之后契约变了。", fix: "init" }] });
  assert.equal(b.ready.ok, false);
  assert.equal(b.limits.core_limits.voice, 600);
  assert.equal(b.defaults.length, 12);
  assert.equal("limits" in Store.normalizeBots(F.makeBots()), false, "the live part has no limits");
  // garbage becomes harmless defaults
  const bad = Store.normalizeBots({ bots: [null, { id: "b-1", init: { status: "weird" }, memory_stats: "x", staging: "many" }], ready: { problems: [{ items: [null, { code: 5 }] }] } });
  assert.equal(bad.bots.length, 1);
  assert.equal(bad.bots[0].init.status, "uninitialized");
  assert.equal(bad.bots[0].staging, 0);
  assert.deepEqual(bad.bots[0].memory_stats.people, 0);
  assert.equal(bad.ready.ok, false, "a list of problems says not ready, whatever `ok` says");
});

test("bots.updated replaces the live BotsPayload and the seats' bot / status follow it; session.updated cannot overwrite it", () => {
  const s = fresh();
  const stale = F.makeBots();
  stale.bots[2] = F.makeBotSummary(3, { init: { status: "stale", stale: ["contract"] } });
  const r = apply(s, "bots.updated", { bots: stale });
  assert.equal(r.botsEvent, s.bots);
  assert.equal(s.bots.bots[2].init.status, "stale");
  assert.equal(s.participants[2].init_status, "stale", "the card of seat 3 shows ⚠ now");
  assert.equal(s.participants[0].init_status, "ready");
  assert.deepEqual([r.dirty.board, r.dirty.topbar, r.dirty.popover], [true, true, true]);
  // a new line-up: the bot of seat 1 is the one that was in seat 2
  const order = F.makeBots();
  const [a, b2] = order.bots.slice(0, 2);
  order.bots[0] = { ...b2, seat: "N1" };
  order.bots[1] = { ...a, seat: "N2" };
  order.lineup = order.bots.map((x) => x.id);
  apply(s, "bots.updated", { bots: order });
  assert.equal(s.participants[0].bot_id, F.botId(2));
  assert.equal(s.participants[1].bot_id, F.botId(1));
  // session.updated never replaces the collection that has an event of its own
  apply(s, "session.updated", { bots: { lineup: [], bots: [] }, memory_enabled: false });
  assert.equal(s.bots.bots.length, 12);
  assert.equal(s.memory_enabled, false);
  // session.updated carries the status label, the deal and the review of the new shapes
  apply(s, "session.updated", { system_status: { code: "reviewing", text: "复盘中", reason: "" }, dealt: true, review: { available: true, done: [], running: true, supported: true, deadline_s: 60, seconds_left: 41 } });
  assert.deepEqual(s.system_status, { code: "reviewing", text: "复盘中", reason: "" });
  assert.equal(s.review.seconds_left, 41);
  assert.equal(s.review.running, true);
});

test("bot.init.progress: the newest event of every bot of the running job is kept; a new job starts a clean list", () => {
  const s = fresh();
  const ev1 = { job_id: "j1", bot_id: F.botId(1), name: "Alice", state: "running", seconds_left: 100, deadline_s: 120, files_written: ["arch_notes"], error: null };
  let r = apply(s, "bot.init.progress", ev1);
  assert.deepEqual(r.initEvent, ev1);
  assert.deepEqual(s.init_progress[F.botId(1)], ev1);
  assert.equal(r.dirty.board, true);
  apply(s, "bot.init.progress", { ...ev1, bot_id: F.botId(2), name: "Bob", state: "queued", seconds_left: null, files_written: [] });
  apply(s, "bot.init.progress", { ...ev1, seconds_left: 99, files_written: ["arch_notes", "digest"] });
  assert.equal(Object.keys(s.init_progress).length, 2);
  assert.deepEqual(s.init_progress[F.botId(1)].files_written, ["arch_notes", "digest"]);
  apply(s, "bot.init.progress", { ...ev1, job_id: "j2", seconds_left: 120 });
  assert.deepEqual(Object.keys(s.init_progress), [F.botId(1)], "the entries of the older job are gone");
  // an unknown state counts as running; a failure keeps its reason; garbage without a bot is a no-op
  const failed = apply(s, "bot.init.progress", { job_id: "j2", bot_id: F.botId(3), state: "failed", error: "模型调用失败", deadline_s: 120 }).initEvent;
  assert.deepEqual([failed.state, failed.error, failed.seconds_left, failed.files_written], ["failed", "模型调用失败", null, []]);
  assert.equal(apply(s, "bot.init.progress", { job_id: "j2", bot_id: F.botId(4), state: "weird" }).initEvent.state, "running");
  assert.equal(apply(s, "bot.init.progress", {}).initEvent, null);
  assert.equal("init_progress" in Store.normalizeSession(F.makeSession()), false, "it is the client's own state: not part of a snapshot");
});

test("review.progress (v0.14): queued -> running with a countdown -> done / timeout with `changes`; a timed-out review counts as done", () => {
  const s = fresh();
  let r = apply(s, "review.progress", { id: "N3", bot_id: F.botId(3), name: "Carol", state: "queued", message: "排队中…", seconds_left: null, deadline_s: 60, changes: null });
  assert.equal(s.review.running, true, "queued counts: the review is under way");
  assert.deepEqual([r.reviewEvent.bot_id, r.reviewEvent.name, r.reviewEvent.seconds_left], [F.botId(3), "Carol", null]);
  r = apply(s, "review.progress", { id: "N3", bot_id: F.botId(3), name: "Carol", state: "running", message: "正在复盘…", seconds_left: 44, deadline_s: 60, changes: null });
  assert.equal(s.review.progress.N3.seconds_left, 44);
  assert.equal("changes" in r.reviewEvent, false, "only a finished review has changes");
  const changes = { files: [{ file: "people", before: "", after: "- Bob：话少" }, { file: "playbook", before: "a", after: "b" }], proposals: 1 };
  r = apply(s, "review.progress", { id: "N3", bot_id: F.botId(3), name: "Carol", state: "timeout", message: "到了 60 秒", seconds_left: 0, deadline_s: 60, changes });
  assert.deepEqual(r.reviewEvent.changes, changes);
  assert.deepEqual(s.review.progress.N3.changes, changes);
  assert.deepEqual(s.review.done, ["N3"]);
  assert.equal(s.review.running, false);
  r = apply(s, "review.progress", { id: "N4", state: "error", message: "模型不可用", changes: null });
  assert.deepEqual(s.review.done, ["N3"], "an error is not done: it can be asked for again");
  assert.deepEqual(Store.normalizeReviewChanges({ files: [{ file: "people" }, null, { before: "x" }], proposals: "2" }), { files: [{ file: "people", before: "", after: "" }], proposals: 2 });
  assert.equal(Store.normalizeReviewChanges(null), null);
});

test("a snapshot and a call line of purpose `init` (H24): the error log knows the new purpose", () => {
  assert.equal(Store.normalizeCall(F.makeCall(1, { purpose: "init", actor_id: F.botId(1), actor_name: "Alice" })).purpose, "init");
  assert.equal(Store.normalizeCall(F.makeCall(2, { purpose: "weird" })).purpose, "turn");
  const r = apply(fresh(), "snapshot", { session: F.makeSession({ status: "idle", dealt: false }) });
  assert.equal(r.replaced, true);
  assert.equal(r.session.dealt, false);
  assert.equal(r.session.bots.bots.length, 12);
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
