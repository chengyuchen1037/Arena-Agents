"use strict";
// Helper for tests/frontend/test_mock_server.py (not a test itself).
// Applies a recorded SSE v2 event log to the initial PublicSession with the REAL client store
// (static/js/store.js) and checks that the result equals the server's final session.
// Usage: node tests/frontend/replay_check.js <replay.json>   (exit code 1 + diff on mismatch)
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Store = require(path.join(__dirname, "..", "..", "static", "js", "store.js"));

const data = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const clone = (v) => JSON.parse(JSON.stringify(v));

let session = Store.normalizeSession(clone(data.initial));
const seenTypes = new Set();
let lastId = 0;
for (const ev of data.events) {
  assert.ok(ev.id > lastId, `event ids must increase (${lastId} -> ${ev.id})`);
  lastId = ev.id;
  seenTypes.add(ev.type);
  const res = Store.applyEvent(session, ev);
  assert.equal(res.reload, false);
  assert.equal(res.toast, "", `unexpected toast on ${ev.type}`);
  session = res.session;
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
  seq: s.seq,
  execution_depth: s.execution_depth,
  participants: Object.fromEntries(s.participants.map((p) => [p.id, { tags: p.tags, out: p.out, state: p.state }])),
  moderator: s.moderator.state,
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
  requests: s.requests.map((r) => ({ id: r.id, kind: r.kind, status: r.status, answered_by: r.answered_by, to: r.to, sealed: r.sealed })),
  tickets: s.tickets.map((t) => ({ id: t.id, player_id: t.player_id, status: t.status, verdict: t.verdict, preview: t.preview })),
  dashboard: s.dashboard,
  by_actor: s.usage_stats.by_actor,
});

const got = project(session);
const want = project(expected);

// float noise in the running totals (the server rounds each addition)
const near = (a, b) => Math.abs(a - b) < 1e-5;
assert.ok(near(session.usage_stats.total_cost_usd, expected.usage_stats.total_cost_usd), `total cost ${session.usage_stats.total_cost_usd} vs ${expected.usage_stats.total_cost_usd}`);
assert.equal(session.usage_stats.prompt_tokens, expected.usage_stats.prompt_tokens);
assert.equal(session.usage_stats.completion_tokens, expected.usage_stats.completion_tokens);
assert.equal(session.usage_stats.total_tokens, expected.usage_stats.total_tokens);
for (const id of Object.keys(want.by_actor)) {
  assert.ok(near(got.by_actor[id].cost_usd, want.by_actor[id].cost_usd), `${id} cost`);
  got.by_actor[id].cost_usd = want.by_actor[id].cost_usd;
}

assert.deepEqual(got, want);
console.log(
  `replay ok: ${data.events.length} events (${seenTypes.size} types), ${got.messages.length} messages, ` +
    `${Object.keys(got.channels).length} channels, ${got.requests.length} requests, ${got.tickets.length} tickets`
);
