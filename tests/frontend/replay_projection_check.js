"use strict";
// Helper for tests/test_replay_client.py (not a test itself).
// Applies the events of a REPLAY (app/game/replay.py: build_replay) to its initial session with the REAL client
// store (static/js/store.js) and checks that the result equals the session the server reported for the finished
// game: identities / tags, channels with their floors, every message, requests with their answers and results,
// tickets, the dashboard, the phase and the winner.
// Usage: node tests/frontend/replay_projection_check.js <replay-check.json>   (exit code 1 + a diff on mismatch)
//   the file: {"initial": ..., "events": [{"id","gt","type","data"}...], "final": <PublicSession of the game>}
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Store = require(path.join(__dirname, "..", "..", "static", "js", "store.js"));

const data = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const clone = (v) => JSON.parse(JSON.stringify(v));

let session = Store.normalizeSession(clone(data.initial));
const seen = {};
let lastId = 0;
for (const ev of data.events) {
  assert.ok(ev.id > lastId, "event ids must increase");
  lastId = ev.id;
  seen[ev.type] = (seen[ev.type] || 0) + 1;
  const res = Store.applyEvent(session, { id: ev.id, type: ev.type, data: ev.data }, { clockNow: ev.gt });
  assert.ok(!res.reload, `${ev.type} asked for a reload`);
  assert.equal(res.toast, "", `unexpected toast on ${ev.type}`);
  session = res.session;
}
const expected = Store.normalizeSession(clone(data.final));

// the clock-dependent parts of a floor (what is left of a turn, how long a discussion has run) are not compared
const floorShape = (f) => ({
  kind: f.kind,
  active: f.active,
  paused: f.paused,
  queue: f.queue,
  free: f.free
    ? {
        chars_used: f.free.chars_used,
        max_chars_total: f.free.max_chars_total,
        duration_s: f.free.duration_s,
        message_counts: f.free.message_counts,
        max_messages_per_member: f.free.max_messages_per_member,
        max_chars_per_message: f.free.max_chars_per_message,
        writers: f.free.writers,
      }
    : null,
});

const project = (s) => ({
  status: s.status,
  winner: s.winner,
  summary: s.summary,
  phase_text: s.phase_text,
  participants: Object.fromEntries(
    s.participants.map((p) => [p.id, { name: p.name, seat: p.seat, tags: p.tags, out: p.out }])
  ),
  channels: Object.fromEntries(
    s.channels.map((c) => [
      c.id,
      { name: c.name, type: c.type, mode: c.mode, frozen: c.frozen, members: c.members, readonly: c.readonly, system: c.system, owner: c.owner, floor: floorShape(c.floor) },
    ])
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
    answers: r.answers.map((a) => ({ player_id: a.player_id, value: a.value, label: a.label, defaulted: Boolean(a.defaulted) })),
    result: r.result,
  })),
  tickets: s.tickets.map((t) => ({ id: t.id, player_id: t.player_id, status: t.status, verdict: t.verdict, preview: t.preview })),
  dashboard: s.dashboard,
});

/** The paths where two JSON values differ (a readable alternative to the wall of text of a deepEqual failure). */
function diffPaths(a, b, where, out) {
  if (out.length >= 40 || a === b) return out;
  const objA = a && typeof a === "object";
  const objB = b && typeof b === "object";
  if (!objA || !objB || Array.isArray(a) !== Array.isArray(b)) {
    out.push(`${where}: replay=${JSON.stringify(a)} server=${JSON.stringify(b)}`);
    return out;
  }
  for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) diffPaths(a[k], b[k], `${where}.${k}`, out);
  return out;
}

const got = project(session);
const want = project(expected);
const differences = diffPaths(got, want, "session", []);
if (differences.length) {
  console.error(`the replay does not add up to the server's session (${differences.length}${differences.length >= 40 ? "+" : ""} differences):\n  ` + differences.join("\n  "));
  process.exit(1);
}
assert.deepEqual(got, want);

// the replay must really contain a game, not just agree on an empty one (the defaults fit the 12-player demo game;
// a shorter script says what it has in the file: {"minimums": {"messages": 20, "requests": 3, "channels": 14}})
const min = { messages: 21, requests: 4, channels: 13, ...(data.minimums || {}) };
assert.ok(got.messages.length >= min.messages, `messages (${got.messages.length} < ${min.messages})`);
assert.ok(got.requests.length >= min.requests && got.requests.every((r) => r.status !== "open"), `requests (${got.requests.length} < ${min.requests}, or one is still open)`);
assert.ok(Object.keys(got.channels).length >= min.channels, `channels (${Object.keys(got.channels).length} < ${min.channels})`);
assert.ok(Object.values(got.participants).some((p) => p.out), "somebody is out at the end");
console.log(
  `replay ok: ${data.events.length} events (${Object.keys(seen).length} types), ${got.messages.length} messages, ` +
    `${Object.keys(got.channels).length} channels, ${got.requests.length} requests, ${got.tickets.length} tickets`
);
