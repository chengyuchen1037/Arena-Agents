"use strict";
// Helper for tests/frontend/test_mock_server.py (not a test itself).
// Applies a recorded SSE v2 event log to the initial PublicSession v3 with the REAL client store
// (static/js/store.js) and checks that the result equals the server's final session: names / avatars,
// the three agent states + wake-up times, requests with their (shared / sealed) answers and results,
// the budget, the review, token counters incl. the cache kinds.
// Usage: node tests/frontend/replay_check.js <replay.json> [--live]   (exit code 1 + diff on mismatch)
//   --live: the log was recorded from the REAL backend by tools/live_replay.py: the comparison with the server's
//           final session is the same, the assertions about what the mock's script contains are skipped.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Store = require(path.join(__dirname, "..", "..", "static", "js", "store.js"));

const data = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const LIVE = process.argv.includes("--live");
const clone = (v) => JSON.parse(JSON.stringify(v));

let session = Store.normalizeSession(clone(data.initial));
const seenTypes = new Set();
const typeCount = {};
const sleepers = new Set(); // agents that were ever sent to sleep with a wake-up time
const states = new Set();
let lastId = 0;
for (const ev of data.events) {
  assert.ok(ev.id > lastId, `event ids must increase (${lastId} -> ${ev.id})`);
  lastId = ev.id;
  seenTypes.add(ev.type);
  typeCount[ev.type] = (typeCount[ev.type] || 0) + 1;
  const res = Store.applyEvent(session, ev, { clockNow: 0 });
  assert.equal(res.reload, false);
  assert.equal(res.toast, "", `unexpected toast on ${ev.type}`);
  session = res.session;
  if (ev.type === "agent.status") {
    states.add(ev.data.state);
    if (ev.data.until !== null && ev.data.until !== undefined) sleepers.add(ev.data.id);
  }
}
const expected = Store.normalizeSession(clone(data.final));

const project = (s) => ({
  status: s.status,
  pause_reason: s.pause_reason,
  hold_reason: s.hold_reason,
  winner: s.winner,
  summary: s.summary,
  phase_text: s.phase_text,
  active_status_text: s.active_status_text,
  // (the real backend's session.seq is the ledger's last entry; the stream only carries the seq of messages)
  seq: LIVE ? 0 : s.seq,
  memory_enabled: s.memory_enabled,
  stall_notice: s.stall_notice,
  budget: s.budget,
  review: { available: s.review.available, done: s.review.done, running: s.review.running },
  participants: Object.fromEntries(
    s.participants.map((p) => [p.id, { name: p.name, seat: p.seat, avatar: p.avatar, tags: p.tags, out: p.out, state: p.state, sleep_until: p.sleep_until }])
  ),
  moderator: { state: s.moderator.state, sleep_until: s.moderator.sleep_until },
  channels: Object.fromEntries(
    s.channels.map((c) => [c.id, { mode: c.mode, frozen: c.frozen, members: c.members, system: c.system, owner: c.owner, floor: c.floor }])
  ),
  messages: s.messages.map((m) => ({
    seq: m.seq,
    channel_id: m.channel_id,
    from: m.from,
    from_name: m.from_name,
    content: m.content,
    kind: m.kind,
    template_id: m.template_id,
    truncated: m.truncated,
    ticket_id: m.ticket_id,
    visible_to_ids: m.visible_to_ids,
  })),
  requests: s.requests.map((r) => ({
    id: r.id,
    kind: r.kind,
    question: r.question,
    status: r.status,
    answered_by: r.answered_by,
    to: r.to,
    sealed: r.sealed,
    share_answers: r.share_answers,
    auto_close: r.auto_close,
    opened_seq: r.opened_seq,
    options: r.options,
    answers: r.answers.map((a) => ({ player_id: a.player_id, value: a.value, label: a.label })),
    result: r.result,
  })),
  tickets: s.tickets.map((t) => ({ id: t.id, player_id: t.player_id, status: t.status, verdict: t.verdict, preview: t.preview })),
  dashboard: s.dashboard,
  by_actor: s.usage_stats.by_actor,
});

const got = project(session);
const want = project(expected);

// float noise in the running totals (the server rounds each addition)
const near = (a, b, eps = 1e-5) => Math.abs(a - b) < eps;
const u = session.usage_stats;
const e = expected.usage_stats;
assert.ok(near(u.total_cost_usd, e.total_cost_usd), `total cost ${u.total_cost_usd} vs ${e.total_cost_usd}`);
for (const k of ["prompt_tokens", "cache_read_tokens", "cache_write_tokens", "completion_tokens", "total_tokens"]) {
  assert.equal(u[k], e[k], `usage_stats.${k}`);
}
assert.ok(near(u.cache_hit_rate, e.cache_hit_rate, 2e-3), `cache hit rate ${u.cache_hit_rate} vs ${e.cache_hit_rate}`);
for (const id of Object.keys(want.by_actor)) {
  assert.ok(got.by_actor[id], `by_actor.${id} missing`);
  assert.ok(near(got.by_actor[id].cost_usd, want.by_actor[id].cost_usd), `${id} cost`);
  got.by_actor[id].cost_usd = want.by_actor[id].cost_usd;
}
// the budget's used numbers follow the same totals
assert.equal(got.budget.used_tokens, u.total_tokens);
assert.ok(near(got.budget.used_cost_usd, u.total_cost_usd, 1e-4));
got.budget.used_cost_usd = want.budget.used_cost_usd;

/** The paths where two JSON values differ (a readable alternative to the wall of text of a deepEqual failure). */
function diffPaths(a, b, where, out) {
  if (out.length >= 40) return out;
  if (a === b) return out;
  const objA = a && typeof a === "object";
  const objB = b && typeof b === "object";
  if (!objA || !objB || Array.isArray(a) !== Array.isArray(b)) {
    out.push(`${where}: events=${JSON.stringify(a)} server=${JSON.stringify(b)}`);
    return out;
  }
  for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) diffPaths(a[k], b[k], `${where}.${k}`, out);
  return out;
}
const differences = diffPaths(got, want, "session", []);
if (differences.length) {
  console.error(`the events do not add up to the server's session (${differences.length}${differences.length >= 40 ? "+" : ""} differences):\n  ` + differences.join("\n  "));
  process.exit(1);
}
assert.deepEqual(got, want);

if (LIVE) {
  assert.ok(states.size >= 1 && [...states].every((s) => ["acting", "sleeping", "thinking"].includes(s)), `agent states: ${[...states]}`);
  console.log(
    `live replay ok: ${data.events.length} events (${seenTypes.size} types), ${got.messages.length} messages, ` +
      `${Object.keys(got.channels).length} channels, ${got.requests.length} requests, ${got.tickets.length} tickets, ` +
      `states ${[...states].sort().join("/")}, ${session.review.done.length} reviews, ` +
      `budget ${session.budget.state}`
  );
  process.exit(0);
}

// ---- what the script is supposed to show (so a silent regression of the mock is caught here too)
assert.deepEqual([...states].sort(), ["acting", "sleeping", "thinking"], "all three agent states are used, and nothing else");
assert.ok(sleepers.size >= 1, "at least one agent sleeps with a wake-up time");
const shared = session.requests.find((r) => r.share_answers);
assert.ok(shared, "a share_answers request exists (the wolves' kill)");
assert.ok(Object.keys(shared.answer_changes || {}).length >= 1, "...and somebody changed their answer on it");
assert.ok(session.requests.some((r) => r.sealed && r.answers.length > 1), "a sealed vote exists");
assert.ok(session.requests.every((r) => r.status !== "open"), "every request is closed at the end");
assert.deepEqual(session.review.done.length, 12, "everybody was reviewed");
assert.ok((typeCount["budget.warning"] || 0) >= 1 && (typeCount["budget.reached"] || 0) >= 1, "the budget warned and was reached");

console.log(
  `replay ok: ${data.events.length} events (${seenTypes.size} types), ${got.messages.length} messages, ` +
    `${Object.keys(got.channels).length} channels, ${got.requests.length} requests, ${got.tickets.length} tickets`
);
