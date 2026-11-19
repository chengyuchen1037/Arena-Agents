/* ==========================================================================
   Arena-Agents v0.13.1 — the thinking record (G21–G26)
   Loaded after roster.js and before app.js. Exposes window.ArenaThoughtsUI.create(ctx) (plus pure helpers).

   The "🧠 思考记录" button of a player's (or the moderator's) detail panel opens a big window of its own - inside the
   page, not a browser pop-up - that reads like a chat log: from top to bottom in time, a divider for every time the
   agent was woken up (game time), under it the bubbles of that wake-up (thinking, and the self-talk that came with no
   tool call; "第 N 轮" says which round of the wake-up).  Tool calls and their results are never shown (so neither
   is the speech itself: that is in the feed).  The newest is at the bottom (the window opens there and follows it as
   long as you are at the bottom), "加载更早的记录" goes back, and while the window is open it asks for what is new
   every 3 seconds (there is no event for it: D5 stands, the thoughts are read on demand).  When more than a page came in
   between two polls (a throttled tab), the stretch in between is a HOLE the page remembers and fills, MAX_GAP_PAGES per poll.

   Server API (docs/v0.12-contracts.md §12.4):
     GET api/agents/{id}/thoughts?before=<cursor>&limit=<n>
       -> {agent_id, name, items: [ThoughtItem oldest -> newest], next_before, has_more}
     ThoughtItem {id (stable: the DOM key), ts, gt, gen, round, kind: "thought" | "content", text, model}
     There is no `after`: the poll reads the newest page again and keeps the items whose id it has not seen.
     409 while another seat is played and the game is not over (G25), 404 an unknown id, 400 a bad cursor:
     the server's own `detail` is shown as it is.

   ctx: { api, Core, View, Avatar, showToast, getSession (the session on screen), isReplaying?, windows? (Core.createWindowStack(),
          shared with the error log: the newest window is on top, Escape closes it alone), document?, window? }
   ========================================================================== */

(function (root) {
  "use strict";

  const MOD = "moderator";
  const POLL_MS = 3000; // while the window is open: ask for what is new this often
  const PAGE = 50; // items per request (the server's default)
  const MAX_GAP_PAGES = 20; // a poll fills a hole (more than a page of new items since the last poll) this many pages at most - the rest is the next poll's
  const FOLD_CHARS = 700; // a bubble longer than this is folded ("展开全文")
  const FOLLOW_PX = 60; // "at the bottom" = within this many pixels of it

  const KIND_LABELS = { thought: "🧠 思考", content: "💬 自言自语" };

  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const pad2 = (n) => String(n).padStart(2, "0");

  // -------------------------------------------------------------------------
  // pure helpers (tested without a page)
  // -------------------------------------------------------------------------

  /**
   * Why this agent's record cannot be opened now ("" = it can): while a seat is played the OTHER agents' thoughts are
   * closed until the game is over (G25; the server refuses too - this is the same rule, so the button can say it first),
   * and a replay shows another game than the one the server keeps the record of.
   */
  function blockedReason(session, agentId, opts) {
    const s = session || {};
    if (opts && opts.replaying) return "重放的是历史对局：思考记录只针对当前对局，退出重放后再看。";
    const seat = s.takeover && s.takeover.seat ? String(s.takeover.seat) : null;
    if (seat && s.status !== "finished" && String(agentId) !== seat) {
      return "接管期间不能查看其他玩家的思考记录，对局结束后开放。";
    }
    return "";
  }

  /** One ThoughtItem with every field present and typed (anything that is no item: null). */
  function normalizeItem(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    const id = raw.id === undefined || raw.id === null ? "" : String(raw.id);
    const text = raw.text === undefined || raw.text === null ? "" : String(raw.text);
    if (!id) return null;
    const optNum = (v) => (v === undefined || v === null || v === "" || !Number.isFinite(Number(v)) ? null : Number(v));
    return {
      id,
      ts: raw.ts === undefined || raw.ts === null ? "" : String(raw.ts),
      gt: optNum(raw.gt),
      gen: optNum(raw.gen),
      round: optNum(raw.round),
      kind: raw.kind === "content" ? "content" : "thought",
      text,
      model: raw.model === undefined || raw.model === null || raw.model === "" ? null : String(raw.model),
    };
  }

  /** A response of the endpoint, tolerant: {agent_id, name, items, next_before, has_more}. */
  function normalizePage(body) {
    const b = body && typeof body === "object" ? body : {};
    const items = (Array.isArray(b.items) ? b.items : []).map(normalizeItem).filter(Boolean);
    const nextBefore = b.next_before === undefined || b.next_before === null || b.next_before === "" ? null : String(b.next_before);
    return {
      agent_id: b.agent_id === undefined || b.agent_id === null ? "" : String(b.agent_id),
      name: b.name === undefined || b.name === null ? "" : String(b.name),
      items,
      next_before: nextBefore,
      has_more: Boolean(b.has_more) && nextBefore !== null,
    };
  }

  /** Game seconds as "m:ss" / "h:mm:ss" ("" when not known). */
  function formatGameTime(seconds) {
    if (!isNum(seconds) || seconds < 0) return "";
    const s = Math.floor(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return h > 0 ? `${h}:${pad2(m)}:${pad2(s % 60)}` : `${m}:${pad2(s % 60)}`;
  }

  /** "10:03:12" in the viewer's local time ("" when unparsable). */
  function formatWallTime(ts) {
    if (!ts) return "";
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return "";
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }

  /**
   * The rows of the window, oldest first: a divider where the wake-up (`gen`) changes - and before the first item - then the
   * bubbles of that wake-up.  [{type: "divider", key, gen, gt, ts} | {type: "bubble", key, item}]
   */
  function buildRows(items) {
    const rows = [];
    let prev;
    items.forEach((item, i) => {
      if (i === 0 || item.gen !== prev) {
        rows.push({ type: "divider", key: `d:${item.id}`, gen: item.gen, gt: item.gt, ts: item.ts });
      }
      rows.push({ type: "bubble", key: item.id, item });
      prev = item.gen;
    });
    return rows;
  }

  /** The new items of `incoming` (ids not in `seen`), in their order. */
  function freshItems(incoming, seen) {
    return incoming.filter((item) => !seen.has(item.id));
  }

  function dividerHtml(row, Core) {
    const gt = formatGameTime(row.gt);
    const when = gt ? `游戏时间 ${gt}` : formatWallTime(row.ts) ? `时间 ${formatWallTime(row.ts)}` : "";
    return `<div class="th-divider" data-gen="${Core.numText(row.gen === null ? 0 : row.gen)}"><span>⏰ 被唤醒${when ? ` · ${Core.escapeHtml(when)}` : ""}</span></div>`;
  }

  /** One bubble (HTML; the text is escaped, line breaks stay: the CSS keeps them). o: {Core, expanded} */
  function bubbleHtml(item, o) {
    const esc = o.Core.escapeHtml;
    const long = item.text.length > FOLD_CHARS;
    const folded = long && !o.expanded;
    const text = folded ? `${item.text.slice(0, FOLD_CHARS).trimEnd()}…` : item.text;
    const bits = [`<span class="th-kind">${esc(KIND_LABELS[item.kind])}</span>`];
    if (item.round !== null) bits.push(`<span class="th-round">第 ${o.Core.numText(item.round)} 轮</span>`);
    if (item.model) bits.push(`<span class="th-model" title="模型">${esc(item.model)}</span>`);
    const time = formatWallTime(item.ts);
    if (time) bits.push(`<span class="th-time" title="本地时间">${esc(time)}</span>`);
    return (
      `<article class="th-bubble th-${item.kind}${folded ? " th-folded" : ""}" data-thought-id="${esc(item.id)}">` +
      `<div class="th-meta">${bits.join("")}</div>` +
      `<div class="th-text">${esc(text)}</div>` +
      (long
        ? `<button type="button" class="btn btn-xs th-fold" data-thoughts-action="fold" data-thought-id="${esc(item.id)}">${folded ? `展开全文（${o.Core.numText(item.text.length)} 字）` : "收起"}</button>`
        : "") +
      `</article>`
    );
  }

  // -------------------------------------------------------------------------
  // the window
  // -------------------------------------------------------------------------

  function create(ctx) {
    const opts = ctx || {};
    const Core = opts.Core || root.ArenaCore;
    const View = opts.View || root.ArenaView;
    const Avatar = opts.Avatar || root.ArenaAvatar;
    const api = opts.api;
    const doc = opts.document || root.document;
    const win = opts.window || root;
    const getSession = opts.getSession || (() => null);
    const isReplaying = opts.isReplaying || (() => false); // a saved game is on screen: the server's record is another game's
    if (!Core || !View || !doc || !api) return null;

    const enc = encodeURIComponent;
    const $ = (id) => (typeof doc.getElementById === "function" ? doc.getElementById(id) : null);
    const el = {
      win: $("thoughts-window"),
      head: $("thoughts-head"),
      list: $("thoughts-list"),
      foot: $("thoughts-foot"),
      latest: $("btn-thoughts-latest"),
      close: $("btn-close-thoughts"),
      refresh: $("btn-thoughts-refresh"),
    };

    // The floating windows of the page in the order they were opened (ctx.windows, shared with the error log): the newest is on top - on
    // the screen, and for Escape, which closes that one only.  A window created on its own keeps a stack of its own.
    const windows = opts.windows || (typeof Core.createWindowStack === "function" ? Core.createWindowStack() : null);
    const setZ = (z) => {
      if (el.win && el.win.style) el.win.style.zIndex = String(z);
    };
    const bringToFront = () => {
      if (windows) windows.opened("thoughts", setZ);
    };
    const sendBack = () => {
      if (windows) windows.closed("thoughts");
      else setZ("");
    };

    const st = {
      agentId: null,
      open: false,
      items: [], // ThoughtItem, oldest -> newest
      seen: new Set(), // their ids
      nextBefore: null, // the cursor that goes further back
      hasMore: false,
      loaded: false, // the first page has arrived (an empty record is then really empty)
      loading: false, // the first page is on its way
      loadingMore: false,
      fetching: false, // a poll / the first page is on its way (one at a time)
      error: "",
      follow: true, // the view sticks to the newest while the user is at the bottom
      stick: false, // go to the bottom after the next repaint that changed the list (the first page, new bubbles while following)
      wasBlocked: false, // the last paint was the "closed" notice: when it ends the list is drawn afresh
      unseen: 0, // new items that came in while the user read further up
      // holes in the list: more than a page of items came in between two polls (the window was throttled - a background tab - or the agent
      // thought a lot), so the newest page does not reach what we hold.  [{at: id of the item right above the hole, cursor: `before` that
      // goes on into it}], the oldest hole first.  They are filled a few pages per poll until the list is whole again.
      gaps: [],
      expanded: new Set(), // folded bubbles that were opened
      timer: null,
      token: 0, // bumps with every opening: an answer that belongs to an older one is dropped
      sessionId: null,
      lastHead: null,
      lastList: null,
      lastFoot: null,
    };

    const session = () => getSession() || {};
    const errorText = (e) => (e && (e.detail || e.message)) || "读取思考记录失败";
    const blocked = () => (st.agentId ? blockedReason(session(), st.agentId, { replaying: isReplaying() }) : "");

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

    function reset() {
      st.items = [];
      st.seen = new Set();
      st.nextBefore = null;
      st.hasMore = false;
      st.loaded = false;
      st.loading = false;
      st.loadingMore = false;
      st.fetching = false;
      st.error = "";
      st.follow = true;
      st.stick = false;
      st.wasBlocked = false;
      st.unseen = 0;
      st.gaps = [];
      st.expanded = new Set();
      st.lastHead = st.lastList = st.lastFoot = null;
    }

    const url = (id, params) => `api/agents/${enc(id)}/thoughts?${params}`;

    /** The newest page: the first read of the window, and then every poll (the items whose id is new are added at the end). */
    async function fetchLatest(initial) {
      if (!st.open || !st.agentId || st.fetching) return;
      const id = st.agentId;
      const token = st.token;
      st.fetching = true;
      if (initial) st.loading = true;
      render();
      let grew = false; // something to read was added at the end: a reader who follows is taken down to it
      try {
        const page = normalizePage(await api.get(url(id, `limit=${PAGE}`)));
        if (token !== st.token) return;
        st.error = "";
        if (initial || !st.loaded) {
          st.items = page.items;
          st.seen = new Set(page.items.map((i) => i.id));
          st.nextBefore = page.next_before;
          st.hasMore = page.has_more;
          st.loaded = true;
          st.gaps = [];
          grew = page.items.length > 0;
        } else {
          const fresh = freshItems(page.items, st.seen);
          if (fresh.length) {
            if (!st.items.length) {
              // the record was empty and now has more than we ever saw: this page is its newest end, "加载更早的记录" goes on from it
              st.nextBefore = page.next_before;
              st.hasMore = page.has_more;
            } else if (fresh.length === page.items.length && page.has_more) {
              // none of the page is known although we hold something, and there is more behind it: more than a page came in since the last
              // poll.  What is between what we hold and this page is a HOLE: remember where it is (it is filled below, a few pages per poll,
              // until it is closed - never given up, and never fetched twice)
              st.gaps.push({ at: fresh[0].id, cursor: page.next_before });
            }
            st.items = st.items.concat(fresh);
            for (const item of fresh) st.seen.add(item.id);
            if (!st.follow) st.unseen += fresh.length;
            grew = true;
          }
          if (st.gaps.length && (await fillGaps(id, token)) > 0) grew = true;
          if (token !== st.token) return;
        }
      } catch (e) {
        if (token === st.token) st.error = errorText(e);
      } finally {
        if (token === st.token) {
          st.fetching = false;
          st.loading = false;
        }
      }
      if (token !== st.token) return;
      if (grew && st.follow) st.stick = true;
      render();
    }

    /**
     * Fill the holes of the list (see `st.gaps`), the NEWEST one first, at most MAX_GAP_PAGES pages per call.  Each page goes in right in
     * front of the part of the list that is above the hole, so the list stays in the order of the log; a hole is closed when a page reaches
     * what we hold (some of its items are known) or there is nothing older.  What is left after the pages of this call stays in `st.gaps`
     * for the next poll.  Returns how many items were added.
     */
    async function fillGaps(id, token) {
      let added = 0;
      for (let pages = 0; st.gaps.length && pages < MAX_GAP_PAGES; pages++) {
        const gap = st.gaps[st.gaps.length - 1];
        const page = normalizePage(await api.get(url(id, `before=${enc(gap.cursor)}&limit=${PAGE}`)));
        if (token !== st.token) return added;
        const at = st.items.findIndex((item) => item.id === gap.at);
        if (at < 0) {
          st.gaps.pop(); // (the item the hole was in front of is not in the list any more: nothing to fill it in front of)
          continue;
        }
        const fresh = freshItems(page.items, st.seen);
        if (fresh.length) {
          st.items.splice(at, 0, ...fresh);
          for (const item of fresh) st.seen.add(item.id);
          if (!st.follow) st.unseen += fresh.length;
          added += fresh.length;
          gap.at = fresh[0].id;
        }
        gap.cursor = page.next_before;
        if (fresh.length < page.items.length || !page.has_more || !page.next_before) st.gaps.pop();
      }
      return added;
    }

    /** Further back: the page before the oldest item we hold (put in front; the view stays where it was). */
    async function loadMore() {
      if (!st.open || !st.agentId || !st.hasMore || st.loadingMore || !st.nextBefore) return;
      const id = st.agentId;
      const token = st.token;
      st.loadingMore = true;
      render();
      let anchor = null; // where the user was when the older page arrived (they may have scrolled while we waited)
      try {
        const page = normalizePage(await api.get(url(id, `before=${enc(st.nextBefore)}&limit=${PAGE}`)));
        if (token !== st.token) return;
        st.error = "";
        anchor = el.list ? { height: el.list.scrollHeight || 0, top: el.list.scrollTop || 0 } : null;
        const fresh = freshItems(page.items, st.seen);
        st.items = fresh.concat(st.items);
        for (const item of fresh) st.seen.add(item.id);
        st.nextBefore = page.next_before;
        st.hasMore = page.has_more;
      } catch (e) {
        if (token === st.token) st.error = errorText(e);
      } finally {
        if (token === st.token) st.loadingMore = false;
      }
      if (token !== st.token) return;
      render();
      // what was added is above the viewport: keep the bubble the user was reading where it is
      if (anchor && anchor.height && el.list) el.list.scrollTop = anchor.top + Math.max(0, (el.list.scrollHeight || 0) - anchor.height);
    }

    // ---- polling ------------------------------------------------------------------------------------------------

    function stopPolling() {
      if (st.timer !== null && typeof win.clearTimeout === "function") win.clearTimeout(st.timer);
      st.timer = null;
    }

    function schedulePoll() {
      if (st.timer !== null || !st.open || typeof win.setTimeout !== "function") return;
      st.timer = win.setTimeout(async () => {
        st.timer = null;
        if (!st.open) return;
        if (!blocked()) await fetchLatest(false);
        if (st.open) schedulePoll();
      }, POLL_MS);
    }

    // ---- rendering ----------------------------------------------------------------------------------------------

    function nameOf(id) {
      return View.displayName(session(), id);
    }

    function headHtml() {
      if (!st.agentId) return "";
      const s = session();
      const a = View.actorAvatar(s, st.agentId);
      const avatar = Avatar ? Avatar.imgHtml(st.agentId, a.seat, a.desc, { size: 36, className: "vw-avatar-img" }) : "";
      const isMod = st.agentId === MOD;
      const seat = !isMod ? View.participantOf(s, st.agentId) : null;
      const sub = `${isMod ? "主持人" : seat ? `${Core.numText(seat.seat)} 号` : Core.escapeHtml(st.agentId)} · 只显示思考与自言自语，不显示工具调用`;
      return (
        `<span class="vw-avatar">${avatar}</span>` +
        `<span class="vw-heading"><span class="vw-title">🧠 ${Core.escapeHtml(nameOf(st.agentId))} 的思考记录</span>` +
        `<span class="vw-sub">${sub}</span></span>`
      );
    }

    function emptyText() {
      const m = View.actorMetrics(session(), st.agentId);
      return m.calls > 0
        ? "该模型没有返回思考内容（或这局的日志不含思考记录：例如导入时选择了“不含日志”）。"
        : "还没有思考记录：这名玩家还没有开始行动。对局开始后，每次被唤醒的思考都会按时间出现在这里。";
    }

    function listHtml() {
      const why = blocked();
      if (why) return `<div class="th-state th-blocked" role="status">🔒 ${Core.escapeHtml(why)}</div>`;
      if (st.error && !st.items.length) return `<div class="th-state th-error" role="alert">${Core.escapeHtml(st.error)}</div>`;
      if (!st.loaded) return `<div class="th-state">读取中…</div>`;
      if (!st.items.length) {
        if (st.hasMore) {
          // a huge log is searched one stretch per request (§12.4): the newest stretch may hold no thought at all although
          // older ones do - the server then answers with an empty page and the cursor to go on with
          return (
            `<div class="th-state th-empty">最近的日志里还没有找到思考记录（日志很大，每次只查一段）。` +
            `<div><button type="button" class="btn btn-xs th-more" data-thoughts-action="more"${st.loadingMore ? " disabled" : ""}>${st.loadingMore ? "读取中…" : "继续往前找"}</button></div></div>`
          );
        }
        return `<div class="th-state th-empty">${Core.escapeHtml(emptyText())}</div>`;
      }
      const top = st.hasMore
        ? `<button type="button" class="btn btn-xs th-more" data-thoughts-action="more"${st.loadingMore ? " disabled" : ""}>${st.loadingMore ? "读取中…" : "↑ 加载更早的记录"}</button>`
        : `<div class="th-start">— 这是最早的记录 —</div>`;
      const rows = buildRows(st.items)
        .map((r) => (r.type === "divider" ? dividerHtml(r, Core) : bubbleHtml(r.item, { Core, expanded: st.expanded.has(r.key) })))
        .join("");
      return top + rows + (st.error ? `<div class="th-state th-error" role="alert">${Core.escapeHtml(st.error)}</div>` : "");
    }

    function footHtml() {
      if (blocked()) return "";
      const n = st.items.length;
      const filling = st.gaps.length ? " · 中间有一段新内容正在补读…" : "";
      return n
        ? `已显示 ${Core.numText(n)} 条${st.hasMore ? "（还有更早的）" : ""}${filling} · 窗口打开期间每 3 秒读取一次新内容`
        : "窗口打开期间每 3 秒读取一次新内容";
    }

    function atBottom() {
      if (!el.list) return true;
      return (el.list.scrollHeight || 0) - (el.list.scrollTop || 0) - (el.list.clientHeight || 0) < FOLLOW_PX;
    }

    function scrollToBottom() {
      if (!el.list) return;
      el.list.scrollTop = el.list.scrollHeight || 0;
      st.follow = true;
      st.unseen = 0;
    }

    function render() {
      if (!el.win) return;
      if (!st.open) {
        el.win.hidden = true;
        return;
      }
      el.win.hidden = false;
      // when the "closed" notice goes away the list is drawn afresh: a reader who was following is taken to the newest again
      const lock = blocked();
      if (!lock && st.wasBlocked && st.follow) st.stick = true;
      st.wasBlocked = Boolean(lock);
      setHtmlIfChanged(el.head, headHtml(), "lastHead");
      const changed = setHtmlIfChanged(el.list, listHtml(), "lastList");
      setHtmlIfChanged(el.foot, footHtml(), "lastFoot");
      if (el.latest) {
        el.latest.hidden = !(st.unseen > 0 && !st.follow);
        el.latest.textContent = `↓ 有 ${st.unseen} 条新内容`;
      }
      // (only what asked for it moves the view: a repaint for any other reason - "读取中…", a fold, the older page - leaves it alone)
      if (st.stick) {
        st.stick = false;
        if (changed) scrollToBottom();
      }
    }

    // ---- open / close -----------------------------------------------------------------------------------------

    /** Open the window for one agent ("N3" / "moderator"); the same agent again just brings it up to date. */
    function open(agentId) {
      const id = String(agentId || "");
      if (!id) return;
      const same = st.open && st.agentId === id;
      st.agentId = id;
      st.open = true;
      bringToFront();
      st.sessionId = session().session_id || null;
      if (!same) {
        st.token += 1;
        stopPolling();
        reset();
      }
      render();
      if (!blocked()) fetchLatest(!same || !st.loaded);
      schedulePoll();
    }

    function close() {
      if (!st.open) return;
      st.open = false;
      sendBack();
      st.token += 1; // answers on their way are of no interest any more
      stopPolling();
      st.fetching = false;
      render();
    }

    /** The session changed (a snapshot, a takeover began or ended, a new game): re-check the rule, start over for a new game. */
    function onSession(s) {
      if (!st.open) return;
      const id = s && s.session_id ? String(s.session_id) : null;
      if (id !== st.sessionId) {
        // another game: its record is another one (the agent id is the same, the thoughts are not)
        st.sessionId = id;
        st.token += 1;
        stopPolling();
        reset();
        render();
        if (!blocked()) fetchLatest(true);
        schedulePoll();
        return;
      }
      const why = blocked();
      render();
      if (!why && st.loaded === false && !st.fetching) fetchLatest(true); // (it was closed a moment ago: ask now)
    }

    function onClick(evt) {
      const node = closestFrom(evt.target, "[data-thoughts-action]");
      if (!node) return;
      const action = node.getAttribute("data-thoughts-action");
      if (action === "more") loadMore();
      else if (action === "fold") {
        const id = node.getAttribute("data-thought-id");
        if (st.expanded.has(id)) st.expanded.delete(id);
        else st.expanded.add(id);
        const top = el.list ? el.list.scrollTop : 0;
        render();
        if (el.list) el.list.scrollTop = top; // (opening a bubble must not throw the view to the bottom)
      }
    }

    function on(node, type, handler) {
      if (node && typeof node.addEventListener === "function") node.addEventListener(type, handler);
    }

    on(el.close, "click", () => close());
    on(el.refresh, "click", () => {
      if (st.open && !blocked()) fetchLatest(false);
    });
    on(el.latest, "click", () => {
      scrollToBottom();
      render();
    });
    on(el.list, "click", onClick);
    on(el.list, "scroll", () => {
      if (!el.list) return;
      st.follow = atBottom();
      if (st.follow && st.unseen) {
        st.unseen = 0;
        if (el.latest) el.latest.hidden = true;
      }
      // the top: the user wants to go further back
      if ((el.list.scrollTop || 0) < 30 && st.hasMore && !st.loadingMore) loadMore();
    });
    if (typeof doc.addEventListener === "function") {
      doc.addEventListener("keydown", (evt) => {
        // (only the window that is on top answers: one Escape closes one thing)
        if (st.open && (windows ? windows.escape(evt, "thoughts") : Boolean(evt && evt.key === "Escape"))) close();
      });
    }

    return {
      open,
      close,
      onSession,
      isOpen: () => st.open,
      agentId: () => (st.open ? st.agentId : null),
      refresh: () => fetchLatest(false),
      loadMore,
      render,
      items: () => st.items.slice(),
      _state: st,
    };
  }

  root.ArenaThoughtsUI = {
    create,
    POLL_MS,
    PAGE,
    MAX_GAP_PAGES,
    FOLD_CHARS,
    blockedReason,
    normalizeItem,
    normalizePage,
    formatGameTime,
    formatWallTime,
    buildRows,
    freshItems,
    dividerHtml,
    bubbleHtml,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = root.ArenaThoughtsUI;
})(typeof window !== "undefined" ? window : globalThis);
