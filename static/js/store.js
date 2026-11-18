/* ==========================================================================
   Arena-Agents v0.12.1 — client store for SSE v2 (pure, no DOM access)
   Exposes window.ArenaStore and module.exports.

   The client keeps ONE object in the shape of PublicSession v3
   (docs/v0.12-contracts.md §8.1 + §10.6). Every SSE event updates the matching
   part of that object IN PLACE; `snapshot` replaces it as a whole and
   `session.updated` merges top-level fields.

     applyEvent(session, {id, type, data}, ctx) -> {
       session,        // the (possibly new) session object
       dirty,          // which UI sections must be repainted
       message,        // the committed message (message.committed only)
       flash,          // {actor_id, ability_id} (ability.used only)
       toast,          // text for the user (error events)
       budgetEvent,    // "warning" | "reached" (budget.warning / budget.reached)
       rosterChanged,  // true when names / avatars changed (roster.updated)
       reviewEvent,    // {id, state, message} (review.progress)
       archiveEvent,   // the normalized archive.progress payload (v0.13: export / import progress), else null
       replaced,       // true when the whole session was swapped (snapshot)
       takeoverChanged,// true when the played seat changed (takeover.updated): v0.13 locks the viewpoint to it
     }
     ctx: {clockNow}  the client's estimate of the game clock; request deadlines are kept as
                      absolute game-clock values so countdowns survive re-renders.

   Nothing here ever receives or stores chain-of-thought or tool-call details:
   the protocol does not carry them.
   ========================================================================== */

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.ArenaStore = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const MODERATOR_ID = "moderator";
  const MAX_MESSAGES = 2000; // same cap as the server snapshot
  const MAX_REQUESTS = 200;
  const MAX_TICKETS = 500;
  // v0.12.1 (E2): only three states. Older servers' "idle" / "stopped" both mean "not running" => sleeping.
  // v0.13 (F6): "human" = a person plays the seat instead of its agent.
  const AGENT_STATES = ["thinking", "acting", "sleeping", "human"];
  const DIRTY_KEYS = ["topbar", "board", "channels", "feed", "dashboard", "popover"];
  const METRIC_KEYS = [
    "activations",
    "calls",
    "prompt_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "completion_tokens",
    "cost_usd",
    "tool_calls",
    "thinking_rounds",
    "rejections",
    "cache_hit_rate",
  ];
  // session.updated never replaces the collections that have their own events.
  const MERGE_PROTECTED = new Set([
    "participants",
    "moderator",
    "channels",
    "messages",
    "requests",
    "tickets",
  ]);

  // -------------------------------------------------------------------------
  // small helpers
  // -------------------------------------------------------------------------

  const isObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const str = (v, d = "") => (v === undefined || v === null ? d : String(v));
  const num = (v, d = 0) => {
    const n = typeof v === "number" ? v : Number(v);
    return Number.isFinite(n) ? n : d;
  };
  const optNum = (v) => {
    if (v === undefined || v === null || v === "") return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };
  const strList = (v) => (Array.isArray(v) ? v.map((x) => String(x)) : []);

  function newDirty(value = false) {
    const d = {};
    for (const k of DIRTY_KEYS) d[k] = value;
    return d;
  }

  function mergeDirty(target, source) {
    const out = target || newDirty(false);
    if (source) for (const k of DIRTY_KEYS) out[k] = Boolean(out[k] || source[k]);
    return out;
  }

  function anyDirty(dirty) {
    return DIRTY_KEYS.some((k) => dirty && dirty[k]);
  }

  function findById(list, id) {
    const want = String(id);
    for (const item of list) if (item && item.id === want) return item;
    return null;
  }

  function upsertById(list, item, max) {
    const i = list.findIndex((x) => x && x.id === item.id);
    if (i >= 0) list[i] = item;
    else list.push(item);
    if (max && list.length > max) list.splice(0, list.length - max);
    return item;
  }

  function agentState(v) {
    return AGENT_STATES.includes(v) ? v : "sleeping";
  }

  /** Seat number of a participant: the explicit seat, else the number inside the id ("N3" -> 3). */
  function seatOf(p) {
    const s = p && p.seat !== "" && p.seat !== undefined && p.seat !== null ? Number(p.seat) : NaN;
    if (Number.isFinite(s) && s > 0) return s;
    const m = /(\d+)\s*$/.exec(str(p && p.id));
    return m ? Number(m[1]) : 0;
  }

  // -------------------------------------------------------------------------
  // normalizers (tolerate partial payloads: a missing field never breaks a render)
  // -------------------------------------------------------------------------

  function normalizeParticipant(raw) {
    const p = isObject(raw) ? raw : {};
    const out = {
      ...p,
      id: str(p.id),
      name: str(p.name, str(p.id)),
      avatar: isObject(p.avatar) ? p.avatar : null,
      tags: strList(p.tags),
      out: Boolean(p.out),
      state: agentState(p.state),
      sleep_until: optNum(p.sleep_until),
    };
    out.seat = seatOf(p);
    return out;
  }

  function normalizeModerator(raw) {
    const m = isObject(raw) ? raw : {};
    return {
      ...m,
      id: MODERATOR_ID,
      name: str(m.name, "主持人"),
      // v0.12.1: the moderator has a random pixel avatar like the players (null = the fixed top hat)
      avatar: isObject(m.avatar) ? m.avatar : null,
      state: agentState(m.state),
      sleep_until: optNum(m.sleep_until),
    };
  }

  function normalizeFloor(raw) {
    const f = isObject(raw) ? raw : {};
    const queue = Array.isArray(f.queue)
      ? f.queue.filter(isObject).map((q) => ({
          holder: str(q.holder),
          state: str(q.state, "pending"),
          messages_used: num(q.messages_used, 0),
          max_messages: num(q.max_messages, 0),
        }))
      : [];
    let free = null;
    if (isObject(f.free)) {
      free = { ...f.free, message_counts: isObject(f.free.message_counts) ? f.free.message_counts : {} };
    }
    return {
      kind: str(f.kind, "none"),
      active: f.active === undefined || f.active === null ? null : String(f.active),
      paused: Boolean(f.paused),
      seconds_left: f.seconds_left === undefined || f.seconds_left === null ? null : num(f.seconds_left, 0),
      queue,
      free,
    };
  }

  function normalizeChannel(raw) {
    const c = isObject(raw) ? raw : {};
    return {
      ...c,
      id: str(c.id),
      name: str(c.name, str(c.id)),
      type: str(c.type, "group"),
      mode: str(c.mode, "closed"),
      members: strList(c.members),
      readonly: strList(c.readonly),
      frozen: Boolean(c.frozen),
      system: Boolean(c.system),
      owner: c.owner === undefined || c.owner === null || c.owner === "" ? null : String(c.owner),
      floor: normalizeFloor(c.floor),
    };
  }

  function normalizeMessage(raw) {
    const m = isObject(raw) ? raw : {};
    const truncated = isObject(m.truncated) || m.truncated === true ? m.truncated : null;
    return {
      seq: num(m.seq, 0),
      ts: m.ts === undefined || m.ts === null ? "" : String(m.ts),
      channel_id: m.channel_id === undefined || m.channel_id === null ? null : String(m.channel_id),
      from: str(m.from),
      from_name: str(m.from_name, str(m.from)),
      content: str(m.content),
      kind: str(m.kind, "speech") || "speech",
      template_id: m.template_id === undefined || m.template_id === null || m.template_id === "" ? null : String(m.template_id),
      truncated,
      ticket_id: m.ticket_id === undefined || m.ticket_id === null || m.ticket_id === "" ? null : String(m.ticket_id),
      visible_to_ids: strList(m.visible_to_ids),
    };
  }

  function normalizeAnswer(raw) {
    const a = isObject(raw) ? raw : {};
    const out = {
      player_id: str(a.player_id),
      value: a.value === undefined || a.value === null ? "" : a.value,
      label: str(a.label, a.value === undefined || a.value === null ? "" : str(a.value)),
      reason: str(a.reason),
      defaulted: Boolean(a.defaulted),
    };
    if (Array.isArray(a.visible_to_ids)) out.visible_to_ids = strList(a.visible_to_ids);
    return out;
  }

  function normalizeResult(raw) {
    if (!isObject(raw)) return null;
    let tally = null;
    if (isObject(raw.tally)) {
      tally = {};
      for (const k of Object.keys(raw.tally)) tally[k] = num(raw.tally[k], 0);
    }
    return {
      reason: str(raw.reason),
      tally,
      leaders: Array.isArray(raw.leaders) ? strList(raw.leaders) : null,
      defaulted: strList(raw.defaulted),
      missing: strList(raw.missing),
    };
  }

  /**
   * RequestView (§10.6). `deadline_clock` is the absolute game-clock second the request closes at
   * (seconds_left + the clock when we learnt it); `answer_changes` counts re-answers seen live.
   */
  function normalizeRequest(raw, ctx) {
    const r = isObject(raw) ? raw : {};
    const secondsLeft = r.seconds_left === undefined || r.seconds_left === null ? null : num(r.seconds_left, 0);
    const now = ctx && Number.isFinite(Number(ctx.clockNow)) ? Number(ctx.clockNow) : 0;
    return {
      ...r,
      id: str(r.id),
      kind: str(r.kind, "choice"),
      question: str(r.question),
      options: Array.isArray(r.options)
        ? r.options.filter(isObject).map((o) => ({ id: str(o.id), label: str(o.label, str(o.id)) }))
        : [],
      to: strList(r.to),
      answered_by: strList(r.answered_by),
      status: str(r.status, "open"),
      sealed: Boolean(r.sealed),
      share_answers: Boolean(r.share_answers),
      auto_close: str(r.auto_close, "all_answered") || "all_answered",
      seconds_left: secondsLeft,
      deadline_clock: secondsLeft === null ? null : now + secondsLeft,
      opened_seq: num(r.opened_seq, 0),
      channel_id: r.channel_id === undefined || r.channel_id === null || r.channel_id === "" ? null : String(r.channel_id),
      answers: Array.isArray(r.answers) ? r.answers.filter(isObject).map(normalizeAnswer) : [],
      result: normalizeResult(r.result),
      answer_changes: isObject(r.answer_changes) ? r.answer_changes : {},
    };
  }

  function normalizeTicket(raw) {
    const t = isObject(raw) ? raw : {};
    return {
      ...t,
      id: str(t.id),
      player_id: str(t.player_id),
      status: str(t.status, "open"),
      verdict: t.verdict === undefined || t.verdict === null || t.verdict === "" ? null : String(t.verdict),
      preview: str(t.preview),
      opened_seq: num(t.opened_seq, 0),
    };
  }

  function normalizeDashboard(raw) {
    const d = isObject(raw) ? raw : {};
    return {
      ...d,
      factions: Array.isArray(d.factions) ? d.factions.filter(isObject) : [],
      tag_stats: Array.isArray(d.tag_stats) ? d.tag_stats.filter(isObject) : [],
    };
  }

  const BUDGET_STATES = ["ok", "warning", "reached"];

  function normalizeBudget(raw) {
    const b = isObject(raw) ? raw : {};
    const pos = (v) => {
      const n = optNum(v);
      return n !== null && n > 0 ? n : null;
    };
    const used = (v) => Math.max(0, num(v, 0));
    return {
      max_tokens: pos(b.max_tokens),
      max_cost_usd: pos(b.max_cost_usd),
      force_stop: Boolean(b.force_stop),
      used_tokens: used(b.used_tokens),
      used_cost_usd: used(b.used_cost_usd),
      state: BUDGET_STATES.includes(b.state) ? b.state : "ok",
    };
  }

  function normalizeReview(raw, previous) {
    const r = isObject(raw) ? raw : {};
    const prev = isObject(previous) ? previous : {};
    return {
      available: Boolean(r.available),
      done: strList(r.done),
      running: Boolean(r.running),
      // live per-player progress (review.progress events); a new snapshot starts clean
      progress: isObject(prev.progress) ? prev.progress : {},
    };
  }

  /** v0.13: one `archive.progress` payload (docs/v0.12-contracts.md §11.4) with every field present and typed. */
  function normalizeArchiveEvent(raw) {
    const d = isObject(raw) ? raw : {};
    return {
      job_id: str(d.job_id),
      kind: d.kind === "import" ? "import" : "export",
      phase: str(d.phase),
      done: Math.max(0, num(d.done, 0)),
      total: Math.max(0, num(d.total, 0)),
      status: ["running", "done", "error"].includes(d.status) ? d.status : "running",
      message: str(d.message),
      code: str(d.code),
      result: isObject(d.result) ? d.result : null,
    };
  }

  // -------------------------------------------------------------------------
  // v0.13: the seat a person plays (docs/v0.12-contracts.md §11.7)
  // -------------------------------------------------------------------------

  const optStr = (v) => (v === undefined || v === null || v === "" ? null : String(v));

  function normalizeTakeoverChannel(raw) {
    const c = isObject(raw) ? raw : {};
    let floor = null;
    if (isObject(c.floor)) {
      const f = c.floor;
      floor = {
        kind: str(f.kind, "none"),
        mine: Boolean(f.mine),
        paused: Boolean(f.paused),
        messages_left: optNum(f.messages_left),
        chars_left: optNum(f.chars_left),
        seconds_left: optNum(f.seconds_left),
        deadline_clock: optNum(f.deadline_clock),
      };
    }
    return {
      can_write: Boolean(c.can_write),
      why_not: optStr(c.why_not),
      code: optStr(c.code),
      max_chars: optNum(c.max_chars),
      floor,
      can_pass: Boolean(c.can_pass),
    };
  }

  function normalizeTakeoverRequest(raw) {
    const r = isObject(raw) ? raw : {};
    const mine = isObject(r.my_answer)
      ? { value: str(r.my_answer.value), label: str(r.my_answer.label, str(r.my_answer.value)), reason: str(r.my_answer.reason) }
      : null;
    return {
      id: str(r.id),
      kind: str(r.kind, "choice"),
      max_chars: optNum(r.max_chars),
      answered: Boolean(r.answered) || mine !== null,
      my_answer: mine,
      seconds_left: optNum(r.seconds_left),
      deadline_clock: optNum(r.deadline_clock),
      channel_id: optStr(r.channel_id),
    };
  }

  function normalizeTakeoverParam(raw) {
    const p = isObject(raw) ? raw : {};
    const out = { name: str(p.name), type: str(p.type, "text"), optional: Boolean(p.optional) };
    if (p.max_chars !== undefined) out.max_chars = optNum(p.max_chars);
    if (Array.isArray(p.values)) out.values = strList(p.values);
    if (p.min !== undefined) out.min = optNum(p.min);
    if (p.max !== undefined) out.max = optNum(p.max);
    if (Array.isArray(p.candidates)) out.candidates = strList(p.candidates);
    return out;
  }

  /**
   * `PublicSession.takeover` / the `takeover.updated` payload. Without a played seat only `seat` (null) and
   * `reshuffle_available` count: everything else is empty, whatever the payload says.
   */
  function normalizeTakeover(raw) {
    const t = isObject(raw) ? raw : {};
    const seat = optStr(t.seat);
    const out = {
      seat,
      reshuffle_available: Boolean(t.reshuffle_available),
      seq: 0,
      channels: {},
      requests: [],
      abilities: [],
      attention: [],
    };
    if (!seat) return out;
    out.seq = num(t.seq, 0);
    if (isObject(t.channels)) {
      for (const id of Object.keys(t.channels)) out.channels[id] = normalizeTakeoverChannel(t.channels[id]);
    }
    out.requests = (Array.isArray(t.requests) ? t.requests : []).filter(isObject).map(normalizeTakeoverRequest);
    out.abilities = (Array.isArray(t.abilities) ? t.abilities : []).filter(isObject).map((a) => ({
      id: str(a.id),
      description: str(a.description),
      quota_left: Math.max(0, num(a.quota_left, 0)),
      params: (Array.isArray(a.params) ? a.params : []).filter(isObject).map(normalizeTakeoverParam),
    }));
    out.attention = strList(t.attention);
    return out;
  }

  /** Coerce the scalar / small-object top-level fields (used after a merge too). */
  function normalizeScalars(s) {
    s.session_id = str(s.session_id);
    s.status = str(s.status, "idle") || "idle";
    s.pause_reason = str(s.pause_reason);
    s.hold_reason = str(s.hold_reason);
    s.winner = s.winner === undefined || s.winner === null || s.winner === "" ? null : s.winner;
    s.summary = str(s.summary);
    s.phase_text = str(s.phase_text);
    s.active_status_text = str(s.active_status_text);
    s.restore_notice = str(s.restore_notice);
    s.autosave_error = str(s.autosave_error);
    s.stall_notice = str(s.stall_notice);
    s.restored_from_save = Boolean(s.restored_from_save);
    s.demo = Boolean(s.demo);
    s.memory_enabled = s.memory_enabled !== false;
    s.seq = num(s.seq, 0);
    s.event_id = num(s.event_id, 0);
    s.clock = num(s.clock, 0);
    s.public_tags = strList(s.public_tags);
    s.binding_issues = Array.isArray(s.binding_issues) ? s.binding_issues : [];
    s.model_bindings = isObject(s.model_bindings) ? s.model_bindings : {};
    s.tag_styles = isObject(s.tag_styles) ? s.tag_styles : {};
    const ui = isObject(s.ui) ? s.ui : {};
    s.ui = {
      ...ui,
      out_tags: strList(ui.out_tags),
      status_tags: strList(ui.status_tags),
      highlight_tags: strList(ui.highlight_tags),
    };
    const u = isObject(s.usage_stats) ? s.usage_stats : {};
    s.usage_stats = {
      ...u,
      prompt_tokens: num(u.prompt_tokens, 0),
      cache_read_tokens: num(u.cache_read_tokens, 0),
      cache_write_tokens: num(u.cache_write_tokens, 0),
      completion_tokens: num(u.completion_tokens, 0),
      total_tokens: num(u.total_tokens, 0),
      total_cost_usd: num(u.total_cost_usd, 0),
      cache_hit_rate: num(u.cache_hit_rate, 0),
      by_model: isObject(u.by_model) ? u.by_model : {},
      by_actor: isObject(u.by_actor) ? u.by_actor : {},
    };
    s.budget = normalizeBudget(s.budget);
    s.review = normalizeReview(s.review, s.review);
    s.takeover = normalizeTakeover(s.takeover);
    return s;
  }

  /**
   * Fill every field the renderers read, tolerate partial payloads (older
   * backends, tests) and drop malformed list items. Mutates and returns `raw`.
   */
  function normalizeSession(raw, ctx) {
    const s = isObject(raw) ? raw : {};
    normalizeScalars(s);
    // a request's countdown is relative to the clock of the snapshot unless the caller knows better
    const reqCtx = ctx && Number.isFinite(Number(ctx.clockNow)) ? ctx : { clockNow: s.clock };
    s.participants = (Array.isArray(s.participants) ? s.participants : [])
      .filter(isObject)
      .map(normalizeParticipant);
    s.moderator = normalizeModerator(s.moderator);
    s.channels = (Array.isArray(s.channels) ? s.channels : []).filter(isObject).map(normalizeChannel);
    let messages = (Array.isArray(s.messages) ? s.messages : []).filter(isObject).map(normalizeMessage);
    for (let i = 1; i < messages.length; i++) {
      if (messages[i].seq < messages[i - 1].seq) {
        messages = messages.slice().sort((a, b) => a.seq - b.seq);
        break;
      }
    }
    if (messages.length > MAX_MESSAGES) messages = messages.slice(messages.length - MAX_MESSAGES);
    s.messages = messages;
    s.requests = (Array.isArray(s.requests) ? s.requests : [])
      .filter(isObject)
      .map((r) => normalizeRequest(r, reqCtx));
    s.tickets = (Array.isArray(s.tickets) ? s.tickets : []).filter(isObject).map(normalizeTicket);
    s.dashboard = normalizeDashboard(s.dashboard);
    return s;
  }

  function emptySession() {
    return normalizeSession({});
  }

  // -------------------------------------------------------------------------
  // messages
  // -------------------------------------------------------------------------

  /** Insert keeping ascending seq order. Returns the index, or -1 for a duplicate. */
  function insertMessage(list, msg) {
    const n = list.length;
    if (msg.seq <= 0 || n === 0 || msg.seq > list[n - 1].seq) {
      list.push(msg);
      return n;
    }
    let lo = 0;
    let hi = n;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (list[mid].seq < msg.seq) lo = mid + 1;
      else hi = mid;
    }
    if (lo < n && list[lo].seq === msg.seq) return -1;
    list.splice(lo, 0, msg);
    return lo;
  }

  /** A truncated message ends with "…" in the feed (D19). */
  function isTruncated(msg) {
    const t = msg && msg.truncated;
    if (!t) return false;
    if (typeof t === "object") return Object.keys(t).length > 0;
    return true;
  }

  // -------------------------------------------------------------------------
  // event handlers (one per SSE `type`)
  // -------------------------------------------------------------------------

  const clockOf = (res, ctx) =>
    ctx && Number.isFinite(Number(ctx.clockNow)) ? Number(ctx.clockNow) : num(res.session.clock, 0);

  /** Seat order of a player: the participant's seat number, else the number inside the id ("N10" -> 10), else last. */
  function seatRankOf(session, id) {
    const p = findById(session.participants, id);
    const seat = p ? Number(p.seat) : NaN;
    if (Number.isFinite(seat) && seat > 0) return seat;
    const m = /(\d+)\s*$/.exec(String(id));
    return m ? Number(m[1]) : Infinity;
  }

  const bySeat = (session) => (a, b) => seatRankOf(session, a) - seatRankOf(session, b) || String(a).localeCompare(String(b));

  /**
   * Put the answer of one player into a request (last answer wins) and track re-answers. The lists stay in
   * seat order, like the server's RequestView.answers (so events and snapshots reduce to the same thing).
   */
  function setAnswer(session, req, answer) {
    const i = req.answers.findIndex((a) => a.player_id === answer.player_id);
    if (i >= 0) {
      const before = req.answers[i];
      if (String(before.value) !== String(answer.value)) {
        req.answer_changes = { ...req.answer_changes, [answer.player_id]: num(req.answer_changes[answer.player_id], 0) + 1 };
      }
      req.answers[i] = answer;
    } else {
      req.answers.push(answer);
    }
    if (!req.answered_by.includes(answer.player_id)) req.answered_by.push(answer.player_id);
    const cmp = bySeat(session);
    req.answers.sort((x, y) => cmp(x.player_id, y.player_id));
    req.answered_by.sort(cmp);
  }

  const HANDLERS = {
    snapshot(res, d, ev, ctx) {
      const clock = d.session && Number.isFinite(Number(d.session.clock)) ? Number(d.session.clock) : undefined;
      res.session = normalizeSession(isObject(d.session) ? d.session : {}, clock === undefined ? ctx : { clockNow: clock });
      res.replaced = true;
      res.dirty = newDirty(true);
    },

    "message.committed"(res, d) {
      const s = res.session;
      const msg = normalizeMessage(d);
      if (insertMessage(s.messages, msg) < 0) return;
      if (s.messages.length > MAX_MESSAGES) s.messages.splice(0, s.messages.length - MAX_MESSAGES);
      if (msg.seq > s.seq) s.seq = msg.seq;
      res.message = msg;
      res.dirty.feed = true;
      res.dirty.channels = true;
    },

    "channel.created"(res, d) {
      if (!isObject(d.channel)) return;
      upsertById(res.session.channels, normalizeChannel(d.channel));
      res.dirty.channels = res.dirty.board = res.dirty.feed = true;
    },

    "floor.changed"(res, d) {
      const channel = findById(res.session.channels, d.channel_id);
      if (!channel) return;
      channel.floor = normalizeFloor(d.floor);
      res.dirty.channels = true;
    },

    "player.updated"(res, d) {
      const p = findById(res.session.participants, d.player_id);
      if (!p) return;
      if (Array.isArray(d.tags)) p.tags = strList(d.tags);
      if (d.out !== undefined) p.out = Boolean(d.out);
      res.dirty.board = res.dirty.popover = res.dirty.dashboard = true;
    },

    "request.opened"(res, d, ev, ctx) {
      if (!isObject(d.request)) return;
      const req = normalizeRequest(d.request, { clockNow: clockOf(res, ctx) });
      // a request that has no ledger position yet is placed right after what the client has seen
      if (!req.opened_seq) req.opened_seq = res.session.seq;
      upsertById(res.session.requests, req, MAX_REQUESTS);
      res.dirty.topbar = res.dirty.feed = true;
    },

    "request.progress"(res, d) {
      const r = findById(res.session.requests, d.request_id);
      if (!r) return;
      if (Array.isArray(d.answered_by)) r.answered_by = strList(d.answered_by);
      if (d.total !== undefined && Number.isFinite(Number(d.total))) r.total = Number(d.total);
      res.dirty.topbar = res.dirty.feed = true;
    },

    "request.answer"(res, d) {
      const r = findById(res.session.requests, d.request_id);
      if (!r || !d.player_id) return;
      setAnswer(res.session, r, normalizeAnswer({ ...d, player_id: d.player_id }));
      res.dirty.topbar = res.dirty.feed = true;
    },

    "request.closed"(res, d) {
      const r = findById(res.session.requests, d.request_id);
      if (!r) return;
      r.status = d.reason === "cancelled" ? "cancelled" : "closed";
      if (Array.isArray(d.answered_by)) r.answered_by = strList(d.answered_by);
      r.defaulted = strList(d.defaulted);
      r.seconds_left = null;
      r.deadline_clock = null;
      if (isObject(d.result)) r.result = normalizeResult({ reason: d.reason, ...d.result });
      else if (!r.result) r.result = normalizeResult({ reason: d.reason, defaulted: d.defaulted });
      res.dirty.topbar = res.dirty.feed = true;
    },

    "challenge.opened"(res, d) {
      if (!isObject(d.ticket)) return;
      upsertById(res.session.tickets, normalizeTicket(d.ticket), MAX_TICKETS);
      res.dirty.channels = true;
    },

    "challenge.resolved"(res, d) {
      const t = findById(res.session.tickets, d.ticket_id);
      if (!t) return;
      t.status = "resolved";
      t.verdict = d.verdict === undefined || d.verdict === null || d.verdict === "" ? null : String(d.verdict);
      res.dirty.channels = true;
    },

    "ability.used"(res, d) {
      res.flash = { actor_id: str(d.actor_id), ability_id: str(d.ability_id) };
      res.dirty.board = true;
    },

    "phase.changed"(res, d) {
      res.session.phase_text = str(d.text);
      res.dirty.topbar = true;
    },

    "dashboard.updated"(res, d) {
      res.session.dashboard = normalizeDashboard(d.dashboard);
      res.dirty.dashboard = true;
    },

    "game.status"(res, d) {
      const s = res.session;
      const status = str(d.status);
      if (!status) return;
      s.status = status;
      if (status === "finished") {
        if (d.winner !== undefined) s.winner = d.winner === null || d.winner === "" ? null : d.winner;
        if (d.summary !== undefined) s.summary = str(d.summary);
      } else if (status === "running") {
        // "continued" after a verdict withdraws the winner; a resumed game is no longer on hold
        s.winner = null;
        s.summary = "";
        s.pause_reason = "";
        s.hold_reason = "";
      }
      res.dirty.topbar = res.dirty.board = res.dirty.popover = res.dirty.feed = true;
    },

    "agent.status"(res, d) {
      const id = str(d.id);
      const state = agentState(d.state);
      const until = optNum(d.until);
      const s = res.session;
      const node = id === MODERATOR_ID ? s.moderator : findById(s.participants, id);
      if (!node) return;
      // v0.13 (F6): a seat a person plays has no agent - it stays "human" whatever an agent-state event says
      if (id !== MODERATOR_ID && s.takeover && s.takeover.seat === id) {
        node.state = "human";
        node.sleep_until = null;
        res.dirty.board = res.dirty.popover = true;
        return;
      }
      node.state = state;
      // only a sleeping agent can have a wake-up time
      node.sleep_until = state === "sleeping" ? until : null;
      res.dirty.board = res.dirty.popover = true;
      if (id === MODERATOR_ID) res.dirty.topbar = true;
    },

    "agent.metrics"(res, d) {
      const id = str(d.id);
      if (!id) return;
      const u = res.session.usage_stats;
      const prev = isObject(u.by_actor[id]) ? u.by_actor[id] : {};
      const next = { ...prev };
      for (const k of METRIC_KEYS) {
        if (d[k] !== undefined) next[k] = num(d[k], num(prev[k], 0));
      }
      // Keep the session totals (top-bar pill) live between snapshots: apply the delta.
      const delta = (k) => num(next[k], 0) - num(prev[k], 0);
      const dp = delta("prompt_tokens");
      const dcr = delta("cache_read_tokens");
      const dcw = delta("cache_write_tokens");
      const dc = delta("completion_tokens");
      const dcost = delta("cost_usd");
      u.prompt_tokens = Math.max(0, num(u.prompt_tokens, 0) + dp);
      u.cache_read_tokens = Math.max(0, num(u.cache_read_tokens, 0) + dcr);
      u.cache_write_tokens = Math.max(0, num(u.cache_write_tokens, 0) + dcw);
      u.completion_tokens = Math.max(0, num(u.completion_tokens, 0) + dc);
      u.total_tokens = Math.max(0, num(u.total_tokens, 0) + dp + dcr + dcw + dc);
      u.total_cost_usd = Math.max(0, Math.round((num(u.total_cost_usd, 0) + dcost) * 1e9) / 1e9);
      const inputSide = u.prompt_tokens + u.cache_read_tokens + u.cache_write_tokens;
      u.cache_hit_rate = inputSide > 0 ? u.cache_read_tokens / inputSide : 0;
      u.by_actor[id] = next;
      res.dirty.topbar = res.dirty.popover = true;
    },

    "request.updated"(res, d, ev, ctx) {
      // forward compatible: a whole RequestView replaces the one we have
      HANDLERS["request.opened"](res, d, ev, ctx);
    },

    "budget.updated"(res, d) {
      if (!isObject(d.budget)) return;
      res.session.budget = normalizeBudget(d.budget);
      res.dirty.topbar = true;
    },

    "budget.warning"(res, d) {
      HANDLERS["budget.updated"](res, d);
      res.budgetEvent = "warning";
    },

    "budget.reached"(res, d) {
      HANDLERS["budget.updated"](res, d);
      res.budgetEvent = "reached";
    },

    "roster.updated"(res, d) {
      const list = Array.isArray(d.participants) ? d.participants : [];
      let changed = false;
      // the moderator has an avatar of its own (a roster entry that is not a seat)
      if (isObject(d.moderator) && isObject(d.moderator.avatar)) {
        res.session.moderator.avatar = d.moderator.avatar;
        changed = true;
      }
      for (const item of list) {
        if (!isObject(item)) continue;
        const p = findById(res.session.participants, item.id);
        if (!p) continue;
        if (typeof item.name === "string" && item.name !== p.name) {
          p.name = item.name;
          changed = true;
        }
        if (isObject(item.avatar)) {
          p.avatar = item.avatar;
          changed = true;
        }
      }
      res.rosterChanged = true;
      if (changed || list.length) {
        res.dirty.board = res.dirty.popover = res.dirty.channels = res.dirty.feed = res.dirty.topbar = true;
      }
    },

    "review.progress"(res, d) {
      const id = str(d.id);
      if (!id) return;
      const r = res.session.review;
      const state = ["running", "done", "error"].includes(d.state) ? d.state : "running";
      r.progress[id] = { state, message: str(d.message) };
      if (state === "done" && !r.done.includes(id)) r.done.push(id);
      r.running = Object.values(r.progress).some((p) => p.state === "running");
      res.reviewEvent = { id, state, message: str(d.message) };
      // the 复盘全部 button (progress text) is part of the top-bar controls
      res.dirty.board = res.dirty.popover = res.dirty.topbar = true;
    },

    /**
     * v0.13: the played seat and its state (what it may do now). The seat's card says "human" while a person
     * plays it (the server's participants say so in a snapshot; this keeps the event path the same).
     */
    "takeover.updated"(res, d) {
      const s = res.session;
      const before = s.takeover ? s.takeover.seat : null;
      s.takeover = normalizeTakeover(d.takeover);
      const seat = s.takeover.seat;
      for (const p of s.participants) {
        if (seat && p.id === seat) {
          p.state = "human";
          p.sleep_until = null;
        } else if (p.state === "human") {
          p.state = "sleeping";
        }
      }
      res.takeoverChanged = before !== seat;
      res.dirty.topbar = res.dirty.board = res.dirty.channels = res.dirty.feed = res.dirty.popover = true;
    },

    "session.updated"(res, d) {
      const s = res.session;
      for (const key of Object.keys(d)) {
        if (MERGE_PROTECTED.has(key)) continue;
        if (key === "review") {
          s.review = normalizeReview(d.review, s.review);
        } else {
          s[key] = d[key];
        }
      }
      normalizeScalars(s);
      res.dirty.topbar = res.dirty.board = res.dirty.popover = res.dirty.feed = true;
    },

    /** v0.13: progress of an archive export / import (docs/archive.md); it is not part of the game's state. */
    "archive.progress"(res, d) {
      res.archiveEvent = normalizeArchiveEvent(d);
    },

    error(res, d) {
      res.toast = str(d.message, "服务器报告了一个错误");
    },
  };
  HANDLERS["channel.updated"] = HANDLERS["channel.created"];

  /**
   * Apply one SSE v2 event {id, type, data}. Unknown types are ignored (forward
   * compatible); the input session is mutated in place unless the event is a snapshot.
   */
  function applyEvent(session, ev, ctx) {
    const res = {
      session: session || emptySession(),
      dirty: newDirty(false),
      message: null,
      flash: null,
      toast: "",
      budgetEvent: null,
      rosterChanged: false,
      reviewEvent: null,
      archiveEvent: null,
      replaced: false,
      takeoverChanged: false,
    };
    if (!isObject(ev)) return res;
    const handler = HANDLERS[str(ev.type)];
    if (handler) handler(res, isObject(ev.data) ? ev.data : {}, ev, ctx || null);
    return res;
  }

  // -------------------------------------------------------------------------
  // event ids / ordering between SSE and HTTP responses
  // -------------------------------------------------------------------------

  /** Numeric id of an SSE payload (or `fallback`). */
  function eventIdOf(ev, fallback = 0) {
    const n = isObject(ev) ? Number(ev.id) : NaN;
    return Number.isFinite(n) && n >= 0 ? n : fallback;
  }

  /** An already-applied event (replayed after a reconnect) must be skipped. */
  function isStaleEvent(lastEventId, ev) {
    if (!isObject(ev) || ev.type === "snapshot") return false;
    const id = Number(ev.id);
    const last = Number(lastEventId);
    return Number.isFinite(id) && id > 0 && Number.isFinite(last) && last > 0 && id <= last;
  }

  /**
   * A control/state HTTP response carries a whole session. When the same game's
   * SSE stream is already ahead of it (event ids), applying it would roll the
   * store back, so it is ignored; a different game or an unknown age is adopted.
   */
  function shouldAdoptHttpSession(current, incoming, lastEventId) {
    if (!isObject(incoming)) return false;
    if (!isObject(current) || !current.session_id) return true;
    if (str(current.session_id) !== str(incoming.session_id)) return true;
    const inc = Number(incoming.event_id);
    const last = Number(lastEventId);
    if (!Number.isFinite(inc) || inc <= 0) return true;
    if (!Number.isFinite(last) || last <= 0) return true;
    return inc >= last;
  }

  return {
    MODERATOR_ID,
    MAX_MESSAGES,
    DIRTY_KEYS,
    METRIC_KEYS,
    AGENT_STATES,
    newDirty,
    mergeDirty,
    anyDirty,
    emptySession,
    normalizeSession,
    normalizeMessage,
    normalizeChannel,
    normalizeFloor,
    normalizeRequest,
    normalizeTicket,
    normalizeParticipant,
    normalizeBudget,
    normalizeReview,
    normalizeArchiveEvent,
    normalizeTakeover,
    seatOf,
    insertMessage,
    isTruncated,
    findById,
    applyEvent,
    eventIdOf,
    isStaleEvent,
    shouldAdoptHttpSession,
  };
});
