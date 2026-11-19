/* ==========================================================================
   Arena-Agents v0.13.1 — the error log (G32–G36)
   Loaded after roster.js and before app.js. Exposes window.ArenaCallLogUI.create(ctx) (plus pure helpers).

   The "📋 日志" button of the top bar opens a window with every LLM call of the current game, one line each (when,
   who, which model, how it ended, how long, how many tokens), and the runtime errors that are no model call (a tool
   that raised, a timer pass that failed, an agent that gave its action up).  A line that went wrong opens to the error
   type, the HTTP status, the attempt and the scrubbed message.  Filters: all / errors only, by role.  The red badge on
   the button counts the UNREAD errors: timeout / error lines above the last id the user marked as read (kept per game
   in localStorage "arena_calllog_read_v1", the last 50 games; on a phone the badge sits on the ⋯ button too).

   An id belongs to ONE log.  After a restart, a load or an import the server rebuilds the log of a game from its trace and
   numbers the lines from 1 again (the rebuilt lines also differ a little from the live ones), so "read up to id 50" says
   nothing about the new log: a new error with id 3 would never light the badge.  Therefore a mark carries a witness, the
   `ts` of the line it ended at, kept beside it under "arena_calllog_anchor_v1" (the contract's map stays as it is).  A mark
   is trusted while the log still has THAT line at THAT id; otherwise it is re-based by time (rebaseMark): what is not later
   than the mark stays read, everything else is unread.

   The page never completes what it holds with "the lines after my last id": those lines say nothing about whether the lines
   BEFORE them are still the ones it holds (a rebuilt log may be longer, and its new errors may have lower ids).  When
   something may be off - an id that was skipped (llm.call is droppable), a line that does not fit what the page holds, the
   snapshot after a reconnect, the window opening - it reads the log AGAIN, the whole of what it keeps (the failed lines for
   the badge; every line while the window is open), and the answer REPLACES what it holds.  Reads are numbered: only the answer
   of the newest one counts.  Such recoveries are bounded (see recover): one at once, the others folded in or spaced.

   Server API (docs/v0.12-contracts.md §12.3; only metadata: never a prompt, an answer, a header or a key):
     GET api/llm/calls?after=&limit=&errors_only=  -> {calls: [CallEntry], last_id, total, errors, retries}
     SSE llm.call {call, errors}                   -> one more line (an ordinary event: it may be dropped - a gap in the ids
                                                      makes the page read the log again; it never asks for `after` itself)

   ctx: { api, Core, Store, View, showToast, getSession (the LIVE game: also while a replay is on screen),
          windows? (Core.createWindowStack(), shared with the thinking record), storage?, document?, window? }
   ========================================================================== */

(function (root) {
  "use strict";

  const READ_KEY = "arena_calllog_read_v1"; // {"<session_id>": <last read id>}   (docs/v0.12-contracts.md §12.7)
  const ANCHOR_KEY = "arena_calllog_anchor_v1"; // {"<session_id>": "<ts of the line that id is>"}: the witness of the mark
  const READ_KEEP = 50; // games whose "read up to" id is remembered
  // A rebuilt line carries the time of its trace record, a live line the time it was added: a few milliseconds apart.  A line
  // that is not later than the mark by more than this counts as read (the rebuild takes seconds: a NEW error never is that close).
  const REBASE_SLACK_MS = 50;
  const FETCH_LIMIT = 2000; // the server keeps (and answers with) at most this many lines
  const PAGE_ROWS = 300; // rows drawn at a time ("显示更多" adds as many again)
  const MIN_CATCHUP_GAP_MS = 1500; // two "read the log again to make sure" recoveries are at least this far apart (it doubles while they find nothing wrong)
  const RECOVER_MAX_GAP_MS = 12000; // ... up to this

  const STATUS_INFO = {
    ok: { label: "成功", cls: "ok" },
    retry: { label: "重试", cls: "retry" },
    timeout: { label: "超时", cls: "timeout" },
    error: { label: "失败", cls: "error" },
    cancelled: { label: "取消", cls: "cancelled" },
  };
  // a line that is no model request (a tool that raised, an agent that retries / gave its action up) says it in its own words
  const EVENT_STATUS_INFO = {
    retry: { label: "将重试", cls: "retry" },
    error: { label: "运行错误", cls: "error" },
  };
  const PURPOSE_LABELS = { turn: "对局", review: "复盘", refine: "完善", runtime: "运行" };

  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const pad2 = (n) => String(n).padStart(2, "0");

  // -------------------------------------------------------------------------
  // pure helpers (tested without a page)
  // -------------------------------------------------------------------------

  /** timeout / error: what the red badge counts */
  const isError = (c) => Boolean(c) && (c.status === "timeout" || c.status === "error");
  /** retry / timeout / error: what "只看错误" shows (the server's errors_only) */
  const isFailure = (c) => Boolean(c) && (c.status === "retry" || c.status === "timeout" || c.status === "error");

  /** How many error lines are newer than the last id the user has read. */
  function unreadErrors(calls, readId) {
    const above = isNum(readId) ? readId : 0;
    let n = 0;
    for (const c of calls || []) if (c.id > above && isError(c)) n++;
    return n;
  }

  /** Counts of what the list holds: {total, errors, retries}. */
  function countCalls(calls) {
    const out = { total: 0, errors: 0, retries: 0 };
    for (const c of calls || []) {
      out.total++;
      if (isError(c)) out.errors++;
      else if (c.status === "retry") out.retries++;
    }
    return out;
  }

  /** f: {errorsOnly, actor ("all" or an actor id)} -> the lines to show, in the order of `calls`. */
  function filterCalls(calls, f) {
    const o = f || {};
    return (calls || []).filter((c) => (!o.errorsOnly || isFailure(c)) && (!o.actor || o.actor === "all" || c.actor_id === o.actor));
  }

  /** "850 ms" / "1.2 s" / "34 s" / "2 分 00 秒" ("" when not known). */
  function formatElapsed(ms) {
    if (!isNum(ms) || ms < 0) return "";
    if (ms < 1000) return `${Math.round(ms)} ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
    const s = Math.round(ms / 1000);
    return `${Math.floor(s / 60)} 分 ${pad2(s % 60)} 秒`;
  }

  /** Game seconds as "m:ss" / "h:mm:ss" ("" when not known). */
  function formatGameTime(seconds) {
    if (!isNum(seconds) || seconds < 0) return "";
    const s = Math.floor(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return h > 0 ? `${h}:${pad2(m)}:${pad2(s % 60)}` : `${m}:${pad2(s % 60)}`;
  }

  /** "2026-11-18 10:03:12" in the viewer's local time ("" when unparsable). */
  function formatFullTime(ts) {
    if (!ts) return "";
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return "";
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }

  /** "N3 Carol" (the live nickname wins), "主持人", "名册" (the moderator's model completing a persona / memory), "系统". */
  function actorLabel(entry, session, View) {
    const id = entry.actor_id;
    if (id === "moderator") return (session && session.moderator && session.moderator.name) || entry.actor_name || "主持人";
    if (id === "roster") return "名册";
    if (id === "engine") return "系统";
    const p = View && session ? View.participantOf(session, id) : null;
    const name = p ? p.name : entry.actor_name;
    return name && name !== id ? `${id} ${name}` : id;
  }

  /** The "角色" column: who made the call and what for ("复盘 · N3 Carol", "完善"). */
  function roleLabel(entry, session, View) {
    if (entry.purpose === "review") return `复盘 · ${actorLabel(entry, session, View)}`;
    if (entry.purpose === "refine") return "完善";
    return actorLabel(entry, session, View);
  }

  /** The result chip: {label, cls}. */
  function statusInfo(entry) {
    if (entry.kind === "event" && EVENT_STATUS_INFO[entry.status]) return EVENT_STATUS_INFO[entry.status];
    return STATUS_INFO[entry.status] || STATUS_INFO.error;
  }

  /** The actors that have lines, for the role filter: [{id, label}] - the moderator, the players by seat, then the rest. */
  function actorOptions(calls, session, View) {
    const seen = new Map();
    for (const c of calls || []) if (c.actor_id && !seen.has(c.actor_id)) seen.set(c.actor_id, c);
    const rank = (id) => {
      if (id === "moderator") return 0;
      const m = /^N(\d+)$/.exec(id);
      if (m) return 1 + Number(m[1]);
      return id === "roster" ? 100 : id === "engine" ? 101 : 102;
    };
    return Array.from(seen.entries())
      .sort((a, b) => rank(a[0]) - rank(b[0]) || String(a[0]).localeCompare(String(b[0])))
      .map(([id, c]) => ({ id, label: actorLabel({ ...c, actor_id: id }, session, View) }));
  }

  /** Input side of a call: every token that was read, cached or written ("" when the line has no numbers). */
  function inputTokens(c) {
    const parts = [c.prompt_tokens, c.cache_read_tokens, c.cache_write_tokens].filter(isNum);
    return parts.length ? parts.reduce((a, b) => a + b, 0) : null;
  }

  /** The "model" column: "deepseek / deepseek-chat" (a line without a model: ""). */
  function modelText(c) {
    if (c.provider_id && c.model_id) return `${c.provider_id} / ${c.model_id}`;
    return c.model_id || c.provider_id || "";
  }

  /** What the closed row says about how it ended: the finish reason, or for a failure the HTTP status / error type. */
  function reasonText(c) {
    if (isFailure(c) || c.status === "cancelled") {
      if (c.http_status !== null && c.http_status !== undefined) return `HTTP ${c.http_status}`;
      return c.error_class || (c.status === "cancelled" ? "已取消" : "");
    }
    return c.finish_reason || "";
  }

  /** The key / value pairs of an opened row (only what is known). Values are plain text: the caller escapes them. */
  function detailPairs(c, session, View, Core) {
    const count = (n) => (Core && Core.formatCount ? Core.formatCount(n) : String(n));
    const out = [];
    const when = formatFullTime(c.ts);
    if (when) out.push(["时间", when]);
    const gt = formatGameTime(c.gt);
    if (gt) out.push(["游戏时间", gt]);
    out.push(["类型", c.kind === "event" ? "运行错误（不是模型请求）" : "模型请求"]);
    out.push(["角色", roleLabel(c, session, View)]);
    out.push(["用途", PURPOSE_LABELS[c.purpose] || c.purpose]);
    const model = modelText(c);
    if (model) out.push(["Provider / 模型", model]);
    if (c.attempt !== null && c.attempt !== undefined) out.push(["第几次尝试", `第 ${c.attempt} 次${c.status === "retry" ? "（失败，随后重试）" : ""}`]);
    const elapsed = formatElapsed(c.elapsed_ms);
    if (elapsed) out.push(["耗时", elapsed]);
    const tokens = [];
    if (isNum(c.prompt_tokens)) tokens.push(`未命中输入 ${count(c.prompt_tokens)}`);
    if (isNum(c.cache_read_tokens)) tokens.push(`缓存读取 ${count(c.cache_read_tokens)}`);
    if (isNum(c.cache_write_tokens)) tokens.push(`缓存写入 ${count(c.cache_write_tokens)}`);
    if (isNum(c.completion_tokens)) tokens.push(`输出 ${count(c.completion_tokens)}`);
    if (tokens.length) out.push(["token", tokens.join(" · ")]);
    if (c.finish_reason) out.push(["结束原因", c.finish_reason]);
    if (c.http_status !== null && c.http_status !== undefined) out.push(["HTTP 状态", String(c.http_status)]);
    if (c.error_class) out.push(["错误类型", c.error_class]);
    if (c.message) out.push(["错误信息", c.message]);
    return out;
  }

  /** One row of the list (a string of HTML; every value is escaped). o: {Core, View, session, expanded, unread} */
  function rowHtml(c, o) {
    const esc = o.Core.escapeHtml;
    const st = statusInfo(c);
    const role = roleLabel(c, o.session, o.View);
    const time = o.Core.formatClockTime(c.ts);
    const gt = formatGameTime(c.gt);
    const input = inputTokens(c);
    const tokens =
      input === null && !isNum(c.completion_tokens)
        ? ""
        : `${input === null ? "–" : o.Core.formatCount(input)} / ${isNum(c.completion_tokens) ? o.Core.formatCount(c.completion_tokens) : "–"}`;
    const reason = reasonText(c);
    const model = modelText(c);
    const classes = ["cl-row", `cl-${st.cls}`, c.kind === "event" ? "cl-event" : "", o.unread ? "unread" : "", o.expanded ? "open" : ""];
    const detail = o.expanded
      ? `<dl class="cl-detail">${detailPairs(c, o.session, o.View, o.Core)
          .map(([k, v]) => `<div${k === "错误信息" ? ' class="cl-wide"' : ""}><dt>${esc(k)}</dt><dd${k === "错误信息" ? ' class="cl-message"' : ""}>${esc(v)}</dd></div>`)
          .join("")}</dl>`
      : "";
    return (
      `<div class="${classes.filter(Boolean).join(" ")}" data-call-id="${o.Core.numText(c.id)}">` +
      `<button type="button" class="cl-main" data-calllog-action="toggle" data-call-id="${o.Core.numText(c.id)}" aria-expanded="${Boolean(o.expanded)}">` +
      `<span class="cl-caret" aria-hidden="true">${o.expanded ? "▼" : "▶"}</span>` +
      `<span class="cl-time" title="${esc(formatFullTime(c.ts))}${gt ? ` · 游戏时间 ${esc(gt)}` : ""}">${esc(time)}${gt ? `<small>游戏 ${esc(gt)}</small>` : ""}</span>` +
      `<span class="cl-role" title="${esc(PURPOSE_LABELS[c.purpose] || "")}">${esc(role)}</span>` +
      `<span class="cl-model" title="Provider / 模型">${esc(model)}</span>` +
      `<span class="cl-status st-${st.cls}">${esc(st.label)}</span>` +
      `<span class="cl-num cl-elapsed" title="耗时">${esc(formatElapsed(c.elapsed_ms))}</span>` +
      `<span class="cl-num cl-tokens" title="token：输入（含缓存）/ 输出">${esc(tokens)}</span>` +
      `<span class="cl-reason" title="结束原因 / 状态">${esc(reason)}</span>` +
      `</button>${detail}</div>`
    );
  }

  // ---- "read up to" per game: a mark {id, ts} (the last READ_KEEP games) -------------------------------------------------
  // The mark is "everything up to the line `id` was read"; `ts` is the time of that line, the witness that `id` still means
  // that line (see the header).  {id: 0, ts: ""} = nothing was read.

  const timeOf = (c) => (c && typeof c.ts === "string" && c.ts ? Date.parse(c.ts) : NaN);

  /** The very same line?  A line of one log never changes: another line behind the same id means another log. */
  function sameLine(a, b) {
    return Boolean(a) && Boolean(b) && a.ts === b.ts && a.status === b.status && a.actor_id === b.actor_id && a.kind === b.kind;
  }

  /** The newest retry / timeout / error line (what the badge and "只看错误" are about), or null. */
  function lastFailure(calls) {
    const list = calls || [];
    for (let i = list.length - 1; i >= 0; i--) if (isFailure(list[i])) return list[i];
    return null;
  }

  /** What "全部标为已读" marks: the newest failed line (every error we hold is at or before it) as {id, ts}; no failure: {id: 0, ts: ""}. */
  function markOf(calls) {
    const line = lastFailure(calls);
    return line ? { id: line.id, ts: line.ts || "" } : { id: 0, ts: "" };
  }

  /**
   * Is `mark` still about THIS log?  calls: the lines we hold (ascending ids); meta: {last_id, total} of the answer they come from.
   * Yes when the log has that very line at that id (same ts), or when the line is gone only because the ring dropped it (the log
   * starts above the mark: everything it holds is newer).  A mark without a witness (kept by an older page) can only be checked
   * for being no longer than the log.
   */
  function markIsValid(mark, calls, meta) {
    const m = mark || { id: 0, ts: "" };
    if (!(m.id > 0)) return true; // nothing was read: nothing to protect
    const last = meta && Number.isFinite(meta.last_id) ? meta.last_id : 0;
    if (!m.ts) return m.id <= last;
    const line = (calls || []).find((c) => c.id === m.id);
    if (line) return line.ts === m.ts;
    const total = meta && Number.isFinite(meta.total) ? meta.total : 0;
    return total > 0 && m.id < last - total + 1;
  }

  /**
   * The mark of ANOTHER log carried over to this one by time: the failed lines that are not later than the mark (a few ms of slack,
   * REBASE_SLACK_MS) were read, up to the first line that is later - or whose time we do not know: from there on everything is unread.
   * Never hides what is newer than the mark; the price is that a line the user did read may show again if its time is not known.
   * calls: ascending ids.  Returns the new mark {id, ts} ({id: 0, ts: ""}: nothing is known to be read).
   */
  function rebaseMark(mark, calls) {
    const limit = Date.parse(mark && mark.ts) + REBASE_SLACK_MS;
    let anchor = null;
    if (Number.isFinite(limit)) {
      for (const c of calls || []) {
        const t = timeOf(c);
        if (!Number.isFinite(t) || t > limit) break;
        if (isFailure(c)) anchor = c;
      }
    }
    return anchor ? { id: anchor.id, ts: anchor.ts } : { id: 0, ts: "" };
  }

  function readMap(storage, key) {
    try {
      const raw = storage ? storage.getItem(key) : null;
      const data = raw ? JSON.parse(raw) : {};
      return data && typeof data === "object" && !Array.isArray(data) ? data : {};
    } catch {
      return {};
    }
  }

  /** The stored mark of a game ({id: 0, ts: ""} when there is none); a mark of an older page has an id and no witness. */
  function readMarkOf(storage, sessionId) {
    const n = Number(readMap(storage, READ_KEY)[sessionId]);
    if (!Number.isFinite(n) || n <= 0) return { id: 0, ts: "" };
    const ts = readMap(storage, ANCHOR_KEY)[sessionId];
    return { id: Math.floor(n), ts: typeof ts === "string" ? ts : "" };
  }

  /** Remember the mark of this game; the games last used stay (the oldest beyond READ_KEEP go, from both maps). */
  function saveReadMark(storage, sessionId, mark) {
    if (!storage || !sessionId) return;
    try {
      const ids = readMap(storage, READ_KEY);
      const witnesses = readMap(storage, ANCHOR_KEY);
      delete ids[sessionId]; // (re-inserted: it is now the newest key)
      delete witnesses[sessionId];
      ids[sessionId] = Math.max(0, Math.floor(Number(mark && mark.id) || 0));
      if (mark && typeof mark.ts === "string" && mark.ts) witnesses[sessionId] = mark.ts;
      const keys = Object.keys(ids);
      for (const k of keys.slice(0, Math.max(0, keys.length - READ_KEEP))) delete ids[k];
      for (const k of Object.keys(witnesses)) if (!(k in ids)) delete witnesses[k];
      storage.setItem(READ_KEY, JSON.stringify(ids));
      storage.setItem(ANCHOR_KEY, JSON.stringify(witnesses));
    } catch {
      /* storage unavailable: the badge simply comes back after a reload */
    }
  }

  // -------------------------------------------------------------------------
  // the window and the badge
  // -------------------------------------------------------------------------

  function create(ctx) {
    const opts = ctx || {};
    const Core = opts.Core || root.ArenaCore;
    const Store = opts.Store || root.ArenaStore;
    const View = opts.View || root.ArenaView;
    const api = opts.api;
    const doc = opts.document || root.document;
    const win = opts.window || root;
    const showToast = opts.showToast || (() => {});
    const getSession = opts.getSession || (() => null);
    if (!Core || !Store || !doc || !api) return null;
    let storage = opts.storage;
    if (storage === undefined) {
      try {
        storage = win.localStorage || null;
      } catch {
        storage = null;
      }
    }

    const $ = (id) => (typeof doc.getElementById === "function" ? doc.getElementById(id) : null);
    const el = {
      win: $("calllog-window"),
      sub: $("calllog-sub"),
      toolbar: $("calllog-toolbar"),
      list: $("calllog-list"),
      foot: $("calllog-foot"),
      close: $("btn-close-calllog"),
      btn: $("btn-open-calllog"),
      badge: $("calllog-badge"),
      moreBadge: $("more-badge"),
    };

    // The floating windows of the page in the order they were opened (ctx.windows, shared with the thinking record): the newest is on top -
    // on the screen, and for Escape, which closes that one only.  A window created on its own keeps a stack of its own.
    const windows = opts.windows || (typeof Core.createWindowStack === "function" ? Core.createWindowStack() : null);
    const setZ = (z) => {
      if (el.win && el.win.style) el.win.style.zIndex = String(z);
    };
    const bringToFront = () => {
      if (windows) windows.opened("calllog", setZ);
    };
    const sendBack = () => {
      if (windows) windows.closed("calllog");
      else setZ("");
    };

    const st = {
      sessionId: null,
      calls: [], // CallEntry, ascending by id: the failed ones first (for the badge), every line once the window was opened
      full: false, // `calls` holds the whole log (the window loaded it)
      lastId: 0, // the highest id of the log we know of (the server's last_id, or a line that came by event)
      readId: 0, // everything up to this id was read (marked by the user) ...
      readTs: "", // ... and this is the `ts` of that line: the witness that the id still means it (see the header)
      synced: false, // `calls` is the picture of a read of the log (not only lines that came by event): the floor of the ring is known
      seq: 0, // the number of the newest read: an answer that carries another number is stale (a newer read began, or the game changed)
      reading: null, // the newest read while it is on its way: {seq, full}
      again: false, // a recovery was asked for while a read was on its way: one more follows it
      arrived: [], // the lines that came by event since the newest read began (its answer may not know them yet)
      recoverTimer: null, // a recovery that waits for its turn (see recover)
      lastRecoverAt: 0, // when the last recovery read began
      recoverStreak: 0, // recoveries in a row that found nothing wrong (each one doubles the wait for the next)
      readEventId: null, // the event id of the game when the newest read began (a snapshot that carries the same one has nothing new)
      open: false,
      mode: "all", // "all" | "errors"
      actor: "all",
      expanded: new Set(),
      rows: PAGE_ROWS,
      loading: false,
      error: "",
      lastFetchAt: 0,
      lastToolbar: null,
      lastList: null,
      lastFoot: null,
    };

    const session = () => getSession() || {};
    const errorText = (e) => (e && (e.detail || e.message)) || "读取日志失败";

    function closestFrom(target, selector) {
      let node = target;
      if (node && typeof node.closest !== "function" && node.parentElement) node = node.parentElement;
      return node && typeof node.closest === "function" ? node.closest(selector) : null;
    }

    function setHtmlIfChanged(node, html, key) {
      if (!node || st[key] === html) return false;
      st[key] = html;
      node.innerHTML = html;
      return true;
    }

    // ---- data ---------------------------------------------------------------------------------------------

    function normalizeBody(body) {
      const b = body && typeof body === "object" ? body : {};
      const num = (v) => (Number.isFinite(Number(v)) && Number(v) > 0 ? Math.floor(Number(v)) : 0);
      return {
        calls: (Array.isArray(b.calls) ? b.calls : []).map(Store.normalizeCall).filter(Boolean),
        last_id: num(b.last_id),
        total: num(b.total),
        errors: num(b.errors),
        retries: num(b.retries),
      };
    }

    /** Lines that came by event, in order (a line we have is ignored); the newest FETCH_LIMIT stay.  Returns whether something was added. */
    function merge(list) {
      const have = new Set(st.calls.map((c) => c.id));
      const fresh = list.filter((c) => !have.has(c.id));
      if (fresh.length) {
        st.calls = st.calls.concat(fresh).sort((a, b) => a.id - b.id);
        if (st.calls.length > FETCH_LIMIT) st.calls = st.calls.slice(st.calls.length - FETCH_LIMIT);
        if (st.reading) st.arrived.push(...fresh); // (the read on its way was asked before these came: its answer may not hold them)
      }
      for (const c of list) if (c.id > st.lastId) st.lastId = c.id;
      return fresh.length > 0;
    }

    /** After a read of the log: the mark must be about the log we now hold; if it is not (rebuilt: the ids start over), re-base it by time. */
    function reconcileMark(meta) {
      const mark = { id: st.readId, ts: st.readTs };
      if (markIsValid(mark, st.calls, meta)) return;
      const next = rebaseMark(mark, st.calls);
      st.readId = next.id;
      st.readTs = next.ts;
      saveReadMark(storage, st.sessionId, next);
    }

    /**
     * Take the answer of a read as THE picture of the log.  What we hold is replaced, never completed: an id means one line inside one
     * log only, and a rebuilt log numbers its lines from 1 again (see the header) - mixing an answer into what we hold could put two
     * numberings into one list, and the lines after our last id say nothing about whether the lines before it are still the ones we
     * hold.  Lines that came by event while the answer was on its way and are newer than it stay (a hole between them and the answer
     * asks for one more read).
     */
    function adopt(body, knownBefore, full) {
      const held = new Map(st.calls.map((c) => [c.id, c]));
      const rebuilt = body.last_id < knownBefore || body.calls.some((c) => held.has(c.id) && !sameLine(held.get(c.id), c));
      const newer = st.arrived.filter((c) => c.id > body.last_id).sort((a, b) => a.id - b.id);
      st.arrived = [];
      st.calls = body.calls.concat(newer);
      st.full = full;
      st.lastId = Math.max(body.last_id, ...newer.map((c) => c.id));
      st.synced = true;
      if (rebuilt) st.expanded = new Set(); // (the ids they were kept by meant other lines)
      reconcileMark({ last_id: body.last_id, total: body.total });
      if (newer.length && newer[0].id !== body.last_id + 1) st.again = true;
    }

    async function request(params) {
      const query = Object.entries(params)
        .map(([k, v]) => `${k}=${encodeURIComponent(v)}`)
        .join("&");
      st.lastFetchAt = Date.now();
      return normalizeBody(await api.get(`api/llm/calls?${query}`));
    }

    /** What the picture looks like (to see whether a read changed anything). */
    const pictureOf = () => `${st.lastId}:${st.calls.length}:${st.calls.length ? st.calls[0].id : 0}:${st.calls.length ? st.calls[st.calls.length - 1].id : 0}`;

    function stopRecoverTimer() {
      if (st.recoverTimer !== null && typeof win.clearTimeout === "function") win.clearTimeout(st.recoverTimer);
      st.recoverTimer = null;
    }

    /**
     * Read what we keep of the log - the failed lines (the badge needs nothing else), or every line while the window is open - and make
     * it the picture of the log.  Reads are numbered: only the answer of the NEWEST one counts, so a slow older answer (the badge's,
     * when the window has opened since) can neither replace a newer one nor undo its `full`, and the answer of a game we have left
     * is never taken at all.  `recovering`: this read is a recovery (see recover).
     */
    async function read(full, recovering) {
      const sid = st.sessionId;
      if (!sid) return;
      stopRecoverTimer(); // (this read is what a waiting recovery was waiting for)
      const my = ++st.seq;
      st.reading = { seq: my, full };
      st.arrived = [];
      st.loading = full;
      if (recovering) st.lastRecoverAt = Date.now();
      const game = getSession();
      st.readEventId = game && game.event_id !== undefined ? game.event_id : null;
      if (full) render();
      const knownBefore = st.lastId;
      const pictureBefore = pictureOf();
      try {
        const body = await request(full ? { limit: FETCH_LIMIT } : { errors_only: "true", limit: FETCH_LIMIT });
        if (my !== st.seq) return; // stale: a newer read began, or the game changed
        st.error = "";
        adopt(body, knownBefore, full);
      } catch (e) {
        if (my !== st.seq) return;
        st.error = errorText(e);
      }
      st.reading = null;
      st.loading = false;
      if (recovering) st.recoverStreak = pictureOf() === pictureBefore ? st.recoverStreak + 1 : 0;
      render();
      if (st.again) {
        st.again = false;
        recover();
      }
    }

    /**
     * "Something may be off" - a snapshot of this game (after a reconnect, a load or an import the log may be another one), a gap in
     * the ids, a line that does not fit what we hold, the window opening: read the log again.  The whole of what we keep, never "the lines
     * after our last id" - those say nothing about whether the lines before it are still the same ones.  A recovery is bounded: while a
     * read is on its way one more is folded in behind it, and two recoveries are at least MIN_CATCHUP_GAP_MS apart (twice as far for each
     * one in a row that found nothing wrong, up to RECOVER_MAX_GAP_MS) - the first one after a quiet time is not delayed.
     */
    function recover() {
      if (!st.sessionId || st.recoverTimer !== null) return; // (one is already waiting: it reads the newest state)
      if (st.reading) {
        st.again = true;
        return;
      }
      const spacing = Math.min(RECOVER_MAX_GAP_MS, MIN_CATCHUP_GAP_MS * 2 ** st.recoverStreak);
      const wait = st.lastRecoverAt + spacing - Date.now();
      if (wait > 0 && typeof win.setTimeout === "function") {
        st.recoverTimer = win.setTimeout(() => {
          st.recoverTimer = null;
          read(st.open, true);
        }, wait);
        return;
      }
      read(st.open, true);
    }

    // ---- the badge ------------------------------------------------------------------------------------------

    function unreadCount() {
      return unreadErrors(st.calls, st.readId);
    }

    function renderBadge() {
      const n = unreadCount();
      const text = n > 99 ? "99+" : String(n);
      for (const node of [el.badge, el.moreBadge]) {
        if (!node) continue;
        node.hidden = n === 0;
        node.textContent = n === 0 ? "" : text;
        if (typeof node.setAttribute === "function") node.setAttribute("title", n === 0 ? "" : `${n} 条未读错误`);
      }
      if (el.btn && typeof el.btn.setAttribute === "function") {
        el.btn.setAttribute(
          "title",
          n === 0 ? "错误日志：全部 LLM 调用与报错" : `错误日志：全部 LLM 调用与报错（${n} 条未读错误）`
        );
      }
    }

    // ---- the window -----------------------------------------------------------------------------------------

    function toolbarHtml(s) {
      const options = actorOptions(st.calls, s, View);
      const unread = unreadCount();
      const failures = st.calls.filter(isFailure).length;
      const seg = (mode, label) =>
        `<button type="button" class="btn btn-xs${st.mode === mode ? " active" : ""}" data-calllog-action="mode" data-mode="${mode}" aria-pressed="${st.mode === mode}">${label}</button>`;
      const actorValid = st.actor === "all" || options.some((o) => o.id === st.actor);
      return (
        `<div class="cl-seg" role="group" aria-label="显示范围">${seg("all", "全部")}${seg("errors", `只看错误${failures ? ` <span class="cl-seg-count">${Core.numText(failures)}</span>` : ""}`)}</div>` +
        `<select class="form-input cl-actor" id="select-calllog-actor" aria-label="按角色筛选">` +
        `<option value="all"${actorValid && st.actor === "all" ? " selected" : ""}>全部角色</option>` +
        options.map((o) => `<option value="${Core.escapeHtml(o.id)}"${st.actor === o.id ? " selected" : ""}>${Core.escapeHtml(o.label)}</option>`).join("") +
        `</select>` +
        `<span class="cl-spacer"></span>` +
        `<button type="button" class="btn btn-xs" data-calllog-action="mark-read"${unread ? "" : " disabled"} title="把现有的错误都标为已读（红色徽章清零）">全部标为已读</button>` +
        `<button type="button" class="btn btn-xs" data-calllog-action="refresh"${st.loading ? " disabled" : ""} title="重新读取日志">${st.loading ? "读取中…" : "刷新"}</button>`
      );
    }

    function listHtml(s) {
      if (st.error && st.calls.length === 0) {
        return `<div class="empty-placeholder cl-empty cl-error">${Core.escapeHtml(st.error)}</div>`;
      }
      if (st.loading && st.calls.length === 0) return `<div class="empty-placeholder cl-empty">读取中…</div>`;
      const rows = filterCalls(st.calls, { errorsOnly: st.mode === "errors", actor: st.actor }).slice().reverse(); // newest first
      if (!rows.length) {
        return (
          `<div class="empty-placeholder cl-empty">` +
          (st.mode === "errors"
            ? "没有报错，也没有重试 ✓"
            : st.calls.length
              ? "这个角色还没有调用记录"
              : "还没有调用记录。对局开始后，每次模型调用都会记在这里。") +
          `</div>`
        );
      }
      const shown = rows.slice(0, st.rows);
      let html = shown
        .map((c) => rowHtml(c, { Core, View, session: s, expanded: st.expanded.has(c.id), unread: c.id > st.readId && isError(c) }))
        .join("");
      if (rows.length > shown.length) {
        html += `<button type="button" class="btn btn-xs cl-more" data-calllog-action="more">显示更多（还有 ${Core.numText(rows.length - shown.length)} 条）</button>`;
      }
      return (st.error ? `<div class="form-hint error cl-error">${Core.escapeHtml(st.error)}</div>` : "") + html;
    }

    function footHtml() {
      const n = countCalls(st.calls);
      const bits = [`共 ${Core.numText(n.total)} 条`, `报错 ${Core.numText(n.errors)}`, `重试 ${Core.numText(n.retries)}`];
      if (!st.full) bits.push("只加载了报错与重试，正在读取全部…");
      return `${bits.join(" · ")}　只显示最近 ${Core.numText(FETCH_LIMIT)} 条；不含提示词、模型输出和密钥。`;
    }

    function render() {
      renderBadge();
      if (!el.win) return;
      if (!st.open) {
        el.win.hidden = true;
        return;
      }
      el.win.hidden = false;
      const s = session();
      setHtmlIfChanged(el.toolbar, toolbarHtml(s), "lastToolbar");
      if (el.list) {
        const before = el.list.scrollHeight || 0;
        const top = el.list.scrollTop || 0;
        const changed = setHtmlIfChanged(el.list, listHtml(s), "lastList");
        // the newest line is on top: a line that arrives while the user reads further down must not push the page away
        if (changed && top > 4 && before) el.list.scrollTop = top + Math.max(0, (el.list.scrollHeight || 0) - before);
      }
      setHtmlIfChanged(el.foot, footHtml(), "lastFoot");
      if (el.sub) {
        const n = countCalls(st.calls);
        el.sub.textContent = `当前对局 · ${n.total} 条 · 报错 ${n.errors}`;
      }
    }

    function open() {
      if (st.open) return;
      st.open = true;
      bringToFront();
      st.rows = PAGE_ROWS;
      st.lastToolbar = st.lastList = st.lastFoot = null;
      render();
      // the whole log is wanted: we hold it only if the last read said so - and no read of the failed lines alone is on its way (its answer
      // would replace the whole log)
      if (!st.full || (st.reading && !st.reading.full)) {
        if (!(st.reading && st.reading.full)) read(true); // (a read of the whole log may be on its way already; one of the failed lines is made stale by this one)
      } else if (Date.now() - st.lastFetchAt > MIN_CATCHUP_GAP_MS) {
        recover(); // (the window was closed for a while: make sure nothing was lost - the last line of all would show no gap)
      }
    }

    function close() {
      if (!st.open) return;
      st.open = false;
      sendBack();
      render();
    }

    /** Everything we hold was read: the mark is the newest failed line (every error is at or before it), with its time as the witness. */
    function markAllRead() {
      const mark = markOf(st.calls);
      if (mark.id > 0) {
        st.readId = mark.id;
        st.readTs = mark.ts;
        saveReadMark(storage, st.sessionId, mark);
      } // (no failed line: nothing to mark, and a mark that is still good stays)
      render();
    }

    // ---- events from the page ------------------------------------------------------------------------------

    /** The session changed (snapshot / HTTP answer): a different game starts a new log; the same game may be another log than we hold. */
    function onSession(s) {
      const id = s && s.session_id ? String(s.session_id) : null;
      if (id !== st.sessionId) {
        st.sessionId = id;
        st.seq++; // (the answers of reads of the game we leave are stale from now on)
        st.reading = null;
        st.again = false;
        st.arrived = [];
        st.recoverStreak = 0;
        st.lastRecoverAt = 0;
        stopRecoverTimer();
        st.calls = [];
        st.lastId = 0;
        st.full = false;
        st.error = "";
        st.loading = false;
        st.actor = "all";
        st.rows = PAGE_ROWS;
        st.expanded = new Set();
        const mark = id ? readMarkOf(storage, id) : { id: 0, ts: "" };
        st.readId = mark.id;
        st.readTs = mark.ts;
        st.synced = false;
        st.lastToolbar = st.lastList = st.lastFoot = null;
        render();
        if (id) read(st.open);
      } else if (id) {
        // A snapshot of the same game (after a reconnect; or any HTTP answer): llm.call is droppable, and after a restart, a load or an
        // import the log may be ANOTHER one with the same game id - read it again.  Not when this very read of ours is the one the
        // snapshot belongs to (nothing happened since: the first snapshot of a page that has just read the log).
        const sameView = s.event_id !== undefined && s.event_id === st.readEventId;
        if (!(sameView && Date.now() - st.lastFetchAt <= MIN_CATCHUP_GAP_MS)) recover();
      }
    }

    /** One `llm.call` event ({call, errors}, already normalized by the store). */
    function onEvent(info) {
      if (!info || !info.call || !st.sessionId) return;
      const c = info.call;
      const held = st.calls.find((x) => x.id === c.id);
      if (held) {
        // a line we have; or another line behind an id we hold - the log was rebuilt, its ids start over: never a "duplicate"
        if (!sameLine(held, c)) recover();
        return;
      }
      if (st.lastId > 0 && c.id <= st.lastId) {
        // an id we do not hold, and not a newer one than we know
        // - below the ring: the server keeps the newest FETCH_LIMIT lines, we keep the same ones; a late copy of a line that has fallen out
        //   of it is nothing to read again (a rebuilt log's first new line has a low id as well - but a rebuilt log comes with a snapshot,
        //   and every snapshot of this game reads the log again, see onSession)
        if (st.synced && c.id <= st.lastId - FETCH_LIMIT) return;
        // - inside the ring, and it should be in what we hold (every failed line up to the last id; every line once the whole log was
        //   read): it came out of order, or it is a line of another log - read the log again
        if (isFailure(c) || st.full) {
          recover();
          return;
        }
      } else if (st.lastId > 0 && c.id > st.lastId + 1) {
        recover(); // lines in between were lost: the read brings them, and this line
        return;
      }
      merge([c]);
      render();
    }

    function onClick(evt) {
      const node = closestFrom(evt.target, "[data-calllog-action]");
      if (!node) return;
      const action = node.getAttribute("data-calllog-action");
      if (action === "mode") {
        st.mode = node.getAttribute("data-mode") === "errors" ? "errors" : "all";
        st.rows = PAGE_ROWS;
        render();
      } else if (action === "mark-read") {
        markAllRead();
      } else if (action === "refresh") {
        if (!st.loading) read(true);
      } else if (action === "more") {
        st.rows += PAGE_ROWS;
        render();
      } else if (action === "toggle") {
        const id = Number(node.getAttribute("data-call-id"));
        if (!Number.isFinite(id)) return;
        if (st.expanded.has(id)) st.expanded.delete(id);
        else st.expanded.add(id);
        render();
      }
    }

    function on(node, type, handler) {
      if (node && typeof node.addEventListener === "function") node.addEventListener(type, handler);
    }

    on(el.btn, "click", () => (st.open ? close() : open()));
    on(el.close, "click", () => close());
    on(el.toolbar, "click", onClick);
    on(el.list, "click", onClick);
    on(el.toolbar, "change", (evt) => {
      const t = evt && evt.target;
      if (t && t.id === "select-calllog-actor") {
        st.actor = String(t.value || "all");
        st.rows = PAGE_ROWS;
        render();
      }
    });
    if (typeof doc.addEventListener === "function") {
      doc.addEventListener("keydown", (evt) => {
        // (only the window that is on top answers: one Escape closes one thing)
        if (st.open && (windows ? windows.escape(evt, "calllog") : Boolean(evt && evt.key === "Escape"))) close();
      });
    }

    renderBadge();

    return {
      onSession,
      onEvent,
      open,
      close,
      toggle: () => (st.open ? close() : open()),
      isOpen: () => st.open,
      markAllRead,
      unread: unreadCount,
      render,
      refresh: () => read(st.open),
      calls: () => st.calls.slice(),
      _state: st,
    };
  }

  root.ArenaCallLogUI = {
    create,
    READ_KEY,
    ANCHOR_KEY,
    READ_KEEP,
    REBASE_SLACK_MS,
    FETCH_LIMIT,
    PAGE_ROWS,
    STATUS_INFO,
    isError,
    isFailure,
    unreadErrors,
    countCalls,
    filterCalls,
    formatElapsed,
    formatGameTime,
    formatFullTime,
    actorLabel,
    roleLabel,
    statusInfo,
    actorOptions,
    inputTokens,
    modelText,
    reasonText,
    detailPairs,
    rowHtml,
    sameLine,
    lastFailure,
    markOf,
    markIsValid,
    rebaseMark,
    readMarkOf,
    saveReadMark,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = root.ArenaCallLogUI;
})(typeof window !== "undefined" ? window : globalThis);
