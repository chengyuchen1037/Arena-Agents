/* ==========================================================================
   Arena-Agents v0.12 — client store for SSE v2 (pure, no DOM access)
   Exposes window.ArenaStore and module.exports.

   The client keeps ONE object in the shape of PublicSession v2
   (docs/v0.12-contracts.md §8.1). Every SSE event of §8.2 updates the matching
   part of that object IN PLACE; `snapshot` replaces it as a whole and
   `session.updated` merges top-level fields.

     applyEvent(session, {id, type, data}) -> {
       session,        // the (possibly new) session object
       dirty,          // which UI sections must be repainted
       message,        // the committed message (message.committed only)
       flash,          // {actor_id, ability_id} (ability.used only)
       toast,          // text for the user (error events)
       reload,         // true for workspace.changed
       replaced,       // true when the whole session was swapped (snapshot)
     }

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
  const AGENT_STATES = ["idle", "thinking", "acting", "stopped"];
  const DIRTY_KEYS = ["topbar", "board", "channels", "feed", "dashboard", "popover"];
  const METRIC_KEYS = [
    "calls",
    "prompt_tokens",
    "completion_tokens",
    "cost_usd",
    "tool_calls",
    "thinking_rounds",
    "rejections",
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

  // -------------------------------------------------------------------------
  // normalizers (tolerate partial payloads: a missing field never breaks a render)
  // -------------------------------------------------------------------------

  function normalizeParticipant(raw) {
    const p = isObject(raw) ? raw : {};
    const state = AGENT_STATES.includes(p.state) ? p.state : "idle";
    return {
      ...p,
      id: str(p.id),
      seat: p.seat === undefined || p.seat === null ? "" : p.seat,
      name: str(p.name, str(p.id)),
      tags: strList(p.tags),
      out: Boolean(p.out),
      state,
    };
  }

  function normalizeModerator(raw) {
    const m = isObject(raw) ? raw : {};
    return {
      ...m,
      id: MODERATOR_ID,
      name: str(m.name, "主持人"),
      state: AGENT_STATES.includes(m.state) ? m.state : "idle",
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

  function normalizeRequest(raw) {
    const r = isObject(raw) ? raw : {};
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
      seconds_left: r.seconds_left === undefined || r.seconds_left === null ? null : num(r.seconds_left, 0),
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
    s.restored_from_save = Boolean(s.restored_from_save);
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
      completion_tokens: num(u.completion_tokens, 0),
      total_tokens: num(u.total_tokens, 0),
      total_cost_usd: num(u.total_cost_usd, 0),
      by_model: isObject(u.by_model) ? u.by_model : {},
      by_actor: isObject(u.by_actor) ? u.by_actor : {},
    };
    return s;
  }

  /**
   * Fill every field the renderers read, tolerate partial payloads (older
   * backends, tests) and drop malformed list items. Mutates and returns `raw`.
   */
  function normalizeSession(raw) {
    const s = isObject(raw) ? raw : {};
    normalizeScalars(s);
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
    s.requests = (Array.isArray(s.requests) ? s.requests : []).filter(isObject).map(normalizeRequest);
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
  // event handlers (one per SSE v2 `type`)
  // -------------------------------------------------------------------------

  const HANDLERS = {
    snapshot(res, d) {
      res.session = normalizeSession(isObject(d.session) ? d.session : {});
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
      res.dirty.channels = res.dirty.board = true;
    },

    "player.updated"(res, d) {
      const p = findById(res.session.participants, d.player_id);
      if (!p) return;
      if (Array.isArray(d.tags)) p.tags = strList(d.tags);
      if (d.out !== undefined) p.out = Boolean(d.out);
      res.dirty.board = res.dirty.popover = true;
    },

    "request.opened"(res, d) {
      if (!isObject(d.request)) return;
      upsertById(res.session.requests, normalizeRequest(d.request), MAX_REQUESTS);
      res.dirty.topbar = res.dirty.board = true;
    },

    "request.progress"(res, d) {
      const r = findById(res.session.requests, d.request_id);
      if (!r) return;
      if (Array.isArray(d.answered_by)) r.answered_by = strList(d.answered_by);
      if (d.total !== undefined && Number.isFinite(Number(d.total))) r.total = Number(d.total);
      res.dirty.topbar = res.dirty.board = true;
    },

    "request.closed"(res, d) {
      const r = findById(res.session.requests, d.request_id);
      if (!r) return;
      r.status = d.reason === "cancelled" ? "cancelled" : "closed";
      if (Array.isArray(d.answered_by)) r.answered_by = strList(d.answered_by);
      r.defaulted = strList(d.defaulted);
      r.seconds_left = null;
      res.dirty.topbar = res.dirty.board = true;
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
      res.dirty.topbar = res.dirty.board = res.dirty.popover = true;
    },

    "agent.status"(res, d) {
      const id = str(d.id);
      const state = AGENT_STATES.includes(d.state) ? d.state : "idle";
      const s = res.session;
      if (id === MODERATOR_ID) {
        s.moderator.state = state;
      } else {
        const p = findById(s.participants, id);
        if (!p) return;
        p.state = state;
      }
      res.dirty.board = res.dirty.popover = true;
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
      const dp = num(next.prompt_tokens, 0) - num(prev.prompt_tokens, 0);
      const dc = num(next.completion_tokens, 0) - num(prev.completion_tokens, 0);
      const dcost = num(next.cost_usd, 0) - num(prev.cost_usd, 0);
      u.prompt_tokens = Math.max(0, num(u.prompt_tokens, 0) + dp);
      u.completion_tokens = Math.max(0, num(u.completion_tokens, 0) + dc);
      u.total_tokens = Math.max(0, num(u.total_tokens, 0) + dp + dc);
      u.total_cost_usd = Math.max(0, Math.round((num(u.total_cost_usd, 0) + dcost) * 1e9) / 1e9);
      u.by_actor[id] = next;
      res.dirty.topbar = res.dirty.popover = true;
    },

    "session.updated"(res, d) {
      const s = res.session;
      for (const key of Object.keys(d)) {
        if (!MERGE_PROTECTED.has(key)) s[key] = d[key];
      }
      normalizeScalars(s);
      res.dirty.topbar = res.dirty.board = res.dirty.popover = true;
    },

    "workspace.changed"(res) {
      res.reload = true;
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
  function applyEvent(session, ev) {
    const res = {
      session: session || emptySession(),
      dirty: newDirty(false),
      message: null,
      flash: null,
      toast: "",
      reload: false,
      replaced: false,
    };
    if (!isObject(ev)) return res;
    const handler = HANDLERS[str(ev.type)];
    if (handler) handler(res, isObject(ev.data) ? ev.data : {}, ev);
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
    insertMessage,
    isTruncated,
    findById,
    applyEvent,
    eventIdOf,
    isStaleEvent,
    shouldAdoptHttpSession,
  };
});
