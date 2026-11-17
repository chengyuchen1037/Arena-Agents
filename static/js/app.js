/* ==========================================================================
   Arena-Agents v0.12 Frontend Controller
   (async agents · SSE v2 · minimal player tiles · speech-only feed)

   Pure logic lives in
     core.js    escaping / API client / formatting
     avatar.js  deterministic glyphs (shape x colour x texture)
     store.js   SSE v2 event application on a PublicSession v2 object
     view.js    viewpoint filtering, tiles, floor status, unread, banners
   This file wires them to the DOM.

   What is NOT here any more (v0.11 -> v0.12): streaming drafts, chain-of-thought
   and tool-call cards, expand/collapse controls, the moderator "engine stream".
   ========================================================================== */

(() => {
  const Core = window.ArenaCore;
  const Avatar = window.ArenaAvatar;
  const Store = window.ArenaStore;
  const View = window.ArenaView;
  if (!Core) {
    throw new Error("core.js 未加载：请确认 static/js/core.js 可访问");
  }
  if (!Avatar || !Store || !View) {
    throw new Error("avatar.js / store.js / view.js 未加载：请确认 static/js 下的脚本可访问");
  }
  const { escapeHtml, numText } = Core;

  // URL prefix the app is served under (supports sub-path reverse proxies).
  const SCRIPT_SRC = document.currentScript && document.currentScript.src;
  const URL_PREFIX = (() => {
    try {
      return Core.prefixFromScriptPath(new URL(SCRIPT_SRC, window.location.href).pathname);
    } catch {
      return "/";
    }
  })();

  const TOKEN_KEY = "arena_api_token";
  const VIEWPOINT_KEY = "arena_viewpoint_v1";
  const MESSAGE_HTML_CACHE_MAX = 5000;
  const FLASH_MS = 3000;
  let providerUI = null; // v0.10 provider/binding/session-list UI (providers.js)
  let layoutUI = null; // v0.11 collapse/resize UI (layout.js)
  let workspaceUI = null; // v0.11 workspace (工作文件夹) UI (workspace.js)

  const state = {
    session: Store.emptySession(), // PublicSession v2, updated in place by SSE events
    loaded: false, // a first session (HTTP or SSE) has arrived
    sessionKey: null,
    viewpointPref: View.GOD, // what the user chose (kept even while the game has no players yet)
    viewpoint: View.GOD, // effective viewpoint: "god" | player id
    selectedChannel: "all", // "all" | channel.id
    openActorId: null, // metrics popover (kept in state so re-renders never lose it)
    dmGroupOpen: false,
    autoScroll: true,
    autoScrollTop: null, // scrollTop of the feed right after our own scroll-to-bottom
    read: { baseline: 0, all: 0, seqs: {} }, // unread watermarks per channel
    flash: new Map(), // actor id -> expiry (ms): briefly highlights a tile (ability.used)
    // SSE
    sse: null,
    sseAttempt: 0,
    sseTimer: null,
    sseConnected: false,
    lastEventId: 0, // highest event id reflected in state.session
    sseResumeId: 0, // last id received over the stream (resume point for reconnects)
    appVersion: "",
    // render caches (skip DOM writes when nothing changed)
    lastBoardHtml: null,
    lastChannelsHtml: null,
    lastViewpointHtml: null,
    lastHintsHtml: null,
    lastBannerHtml: null,
    lastDashboardSig: null,
    lastPopoverHtml: null,
    feed: { key: "", count: 0, lastSeq: 0 },
    msgHtml: new Map(),
  };

  // ---------------------------------------------------------------------------
  // Auth token (ARENA_API_TOKEN) + API client
  // ---------------------------------------------------------------------------

  let memoryToken = "";
  function getToken() {
    try {
      return localStorage.getItem(TOKEN_KEY) || memoryToken;
    } catch {
      return memoryToken;
    }
  }
  function setToken(tok) {
    memoryToken = tok || "";
    try {
      if (tok) localStorage.setItem(TOKEN_KEY, tok);
      else localStorage.removeItem(TOKEN_KEY);
    } catch {
      /* storage unavailable: keep in memory only */
    }
  }

  let authPrompt = null; // shared pending promise so concurrent 401s ask once

  function requestAuthToken({ hadToken, usedToken }) {
    // Another request already obtained a newer token while this one was in flight.
    const current = getToken();
    if (current && current !== usedToken) return Promise.resolve(current);
    if (authPrompt) return authPrompt;
    authPrompt = showTokenModal(hadToken).finally(() => {
      authPrompt = null;
    });
    return authPrompt;
  }

  const api = Core.createApiClient({
    fetchImpl: (...args) => window.fetch(...args),
    getToken,
    onAuthRequired: requestAuthToken,
    prefix: URL_PREFIX,
  });

  async function apiPost(path, body = null) {
    return api.post(path, body);
  }

  // ---------------------------------------------------------------------------
  // DOM elements
  // ---------------------------------------------------------------------------

  const $ = (id) => document.getElementById(id);
  const elAppTitle = $("app-title");
  const elAppVersion = $("app-version");
  const elBtnTheme = $("btn-theme");
  const elStatusDot = $("active-status-dot");
  const elStatusText = $("active-status-text");
  const elPhasePill = $("phase-pill");
  const elRequestHints = $("request-hints");
  const elStatusBanner = $("status-banner");
  const elSelectViewpoint = $("select-viewpoint");
  const elFeedViewing = $("feed-viewing");

  const elSelectExecDepth = $("select-exec-depth");
  const elUsagePill = $("usage-telemetry-pill");
  const elUsageTokensText = $("usage-tokens-text");
  const elUsageCostText = $("usage-cost-text");

  const elBtnNew = $("btn-new");
  const elBtnSave = $("btn-save");
  const elBtnStart = $("btn-start");
  const elBtnPause = $("btn-pause");
  const elBtnResume = $("btn-resume");

  const elParticipantsGrid = $("participants-grid");
  const elChannelsList = $("channels-list");
  const elFeedChannelLabel = $("feed-channel-label");
  const elFeedList = $("feed-list");
  const elFeedRecords = $("feed-records-container");
  const elPopover = $("metrics-popover");

  const elFactionsList = $("factions-list");
  const elTagStatsList = $("tag-stats-list");
  const elToastContainer = $("toast-container");

  const elConnBanner = $("conn-banner");
  const elTokenModal = $("token-modal");
  const elTokenForm = $("token-form");
  const elTokenInput = $("input-api-token");
  const elTokenMessage = $("token-modal-message");
  const elBtnCancelToken = $("btn-cancel-token");

  function showToast(msg) {
    const toast = document.createElement("div");
    toast.className = "toast";
    // Never render "[object Object]": anything non-string goes through formatDetail.
    toast.textContent =
      typeof msg === "string"
        ? msg
        : Core.formatDetail(msg && msg.message ? msg.message : msg) || "操作失败";
    elToastContainer.appendChild(toast);
    setTimeout(() => {
      toast.remove();
    }, 3200);
  }

  const cssEscape = (value) =>
    typeof CSS !== "undefined" && CSS && typeof CSS.escape === "function"
      ? CSS.escape(String(value))
      : String(value).replace(/["\\]/g, "\\$&");

  /** event.target.closest that tolerates text nodes and the node-test stubs. */
  function closestFrom(target, selector) {
    let node = target;
    // A text node has no closest(): walk up to its element.
    if (node && typeof node.closest !== "function" && node.parentElement) node = node.parentElement;
    return node && typeof node.closest === "function" ? node.closest(selector) : null;
  }

  // ---------------------------------------------------------------------------
  // Per-game UI state (unread watermarks, selection)
  // ---------------------------------------------------------------------------

  function lastMessageSeq(session) {
    const m = session.messages;
    return m.length ? m[m.length - 1].seq : 0;
  }

  function resetReadState(session) {
    // History that exists when a game is loaded counts as read.
    state.read = { baseline: lastMessageSeq(session), all: 0, seqs: {} };
  }

  function markChannelRead(channelId) {
    const s = state.session;
    if (channelId === "all") {
      state.read.all = Math.max(state.read.all, View.latestSeq(s, "all"));
    } else {
      state.read.seqs[channelId] = Math.max(
        state.read.seqs[channelId] || 0,
        View.latestSeq(s, channelId)
      );
    }
  }

  function onMessageSeen(msg) {
    // A message that arrives in the channel being read (or while "all" is shown) is not unread.
    if (state.selectedChannel === "all") {
      state.read.all = Math.max(state.read.all, msg.seq);
    } else if (msg.channel_id === state.selectedChannel) {
      state.read.seqs[msg.channel_id] = Math.max(state.read.seqs[msg.channel_id] || 0, msg.seq);
    }
  }

  function flashActor(info) {
    if (!info || !info.actor_id) return;
    state.flash.set(info.actor_id, Date.now() + FLASH_MS);
    const s = state.session;
    showToast(
      `⚡ ${View.displayName(s, info.actor_id)}（${info.actor_id}）发动了能力${
        info.ability_id ? `：${info.ability_id}` : ""
      }`
    );
    setTimeout(() => {
      state.flash.delete(info.actor_id);
      scheduleRender({ board: true });
    }, FLASH_MS);
  }

  // ---------------------------------------------------------------------------
  // Rendering: top bar
  // ---------------------------------------------------------------------------

  function renderViewpointSelect(s) {
    if (!elSelectViewpoint) return;
    const options = View.viewpointOptions(s);
    const html = options
      .map((o) => `<option value="${escapeHtml(o.id)}">${escapeHtml(o.label)}</option>`)
      .join("");
    if (html !== state.lastViewpointHtml) {
      state.lastViewpointHtml = html;
      elSelectViewpoint.innerHTML = html;
    }
    if (elSelectViewpoint.value !== state.viewpoint) elSelectViewpoint.value = state.viewpoint;
  }

  function renderRequestHints(s) {
    if (!elRequestHints) return;
    const hints = View.requestHints(s, state.viewpoint);
    const shown = hints.slice(0, 3);
    let html = shown
      .map(
        (h) =>
          `<span class="req-pill" title="${escapeHtml(h.question)}${
            h.sealed ? "（密封：关闭前不公开答案）" : ""
          }"><span class="req-icon" aria-hidden="true">${h.icon}</span>${escapeHtml(
            h.label
          )} <strong>${numText(h.answered)}/${numText(h.total)}</strong></span>`
      )
      .join("");
    if (hints.length > shown.length) {
      html += `<span class="req-pill req-more">+${numText(hints.length - shown.length)}</span>`;
    }
    if (html !== state.lastHintsHtml) {
      state.lastHintsHtml = html;
      elRequestHints.innerHTML = html;
    }
  }

  function renderBanner(s) {
    if (!elStatusBanner) return;
    const lines = View.statusBanners(s);
    const html = lines
      .map((l) => `<span class="banner-line banner-${escapeHtml(l.kind)}">${escapeHtml(l.text)}</span>`)
      .join("");
    if (html !== state.lastBannerHtml) {
      state.lastBannerHtml = html;
      elStatusBanner.innerHTML = html;
      elStatusBanner.className = `status-banner${lines.length ? ` banner-${lines[0].kind}` : ""}`;
    }
    elStatusBanner.hidden = lines.length === 0;
  }

  function renderTopbar() {
    const s = state.session;
    elAppTitle.textContent = s.title || "Arena-Agents";
    const ver = state.appVersion || s.version || "";
    if (elAppVersion) elAppVersion.textContent = ver;

    elStatusText.textContent = View.statusText(s);
    if (elStatusText.setAttribute) elStatusText.setAttribute("title", View.statusText(s));
    elPhasePill.textContent = s.phase_text || "未开始";
    elStatusDot.className = `status-dot ${View.statusDotClass(s)}`;

    if (elSelectExecDepth && s.execution_depth && document.activeElement !== elSelectExecDepth) {
      elSelectExecDepth.value = s.execution_depth;
    }

    // Top-right token & cost telemetry (the cost is accumulated server-side per call).
    if (elUsageTokensText && elUsageCostText) {
      const u = s.usage_stats || {};
      const total = u.total_tokens > 0 ? u.total_tokens : 0;
      elUsageTokensText.textContent = Core.formatTokensCount(total);
      elUsageCostText.textContent = `$${Core.sessionCostUsd(u).toFixed(4)}`;
    }

    // Start only on a fresh game; Resume also works after the game finished (D8).
    const ctl = View.controlState(s);
    elBtnStart.disabled = !ctl.start;
    elBtnPause.disabled = !ctl.pause;
    elBtnResume.disabled = !ctl.resume;

    renderViewpointSelect(s);
    renderRequestHints(s);
    renderBanner(s);
    if (elFeedViewing) {
      elFeedViewing.textContent = `视角：${View.viewpointLabel(s, state.viewpoint)}`;
      elFeedViewing.classList.toggle("is-player", state.viewpoint !== View.GOD);
    }
    if (elSelectViewpoint) elSelectViewpoint.classList.toggle("is-player", state.viewpoint !== View.GOD);
  }

  // ---------------------------------------------------------------------------
  // Rendering: player tiles (≈64×44)
  // ---------------------------------------------------------------------------

  const BADGE_TEXT = { thinking: "💭", acting: "⚡", stopped: "■" };
  const FLOW_TEXT = { floor: "🎤", waiting: "✋" };

  function tileHtml(t) {
    const classes = ["tile"];
    if (t.isModerator) classes.push("tile-moderator");
    if (t.out) classes.push("out");
    if (t.viewing) classes.push("viewing");
    if (t.state) classes.push(`st-${t.state}`);
    if (t.flow) classes.push(`flow-${t.flow}`);
    if (state.flash.has(t.id)) classes.push("flash");
    const open = state.openActorId === t.id;
    if (open) classes.push("popover-open");

    const glyph = Avatar.glyphHtml(t.id, t.seat, { size: 20 });
    const idText = t.isModerator ? "MOD" : t.id;
    const tagText = t.mainTag || "—";
    const tagClass = t.mainTag ? `tag-${t.variant || "neutral"}` : "tag-empty";
    const stateBadge = t.state
      ? `<span class="tile-badge badge-${t.state}" title="${escapeHtml(
          View.STATE_LABELS[t.state] || ""
        )}">${BADGE_TEXT[t.state] || ""}</span>`
      : "";
    const flowBadge = t.flow
      ? `<span class="tile-badge badge-${t.flow}" title="${escapeHtml(
          View.FLOW_LABELS[t.flow] || ""
        )}">${FLOW_TEXT[t.flow] || ""}</span>`
      : "";

    // Layout (≈64×44): [glyph][id]
    //                  [main tag chip]
    // The state badge (thinking / acting / stopped) sits on the top-right corner and the
    // flow badge (holds the floor / owes an answer) on the bottom-right corner.
    return (
      `<div class="${classes.join(" ")}" data-actor="${escapeHtml(t.id)}" title="${escapeHtml(t.title)}">` +
      `<div class="tile-top">${glyph}<span class="tile-id">${escapeHtml(idText)}</span></div>` +
      `<div class="tile-bottom"><button type="button" class="tile-tag ${tagClass}" data-tag-actor="${escapeHtml(t.id)}"` +
      ` aria-haspopup="dialog" aria-expanded="${open ? "true" : "false"}"` +
      ` title="点击查看 ${escapeHtml(t.id)} 的指标">${escapeHtml(tagText)}</button></div>` +
      `${stateBadge}${flowBadge}</div>`
    );
  }

  function renderBoard() {
    const s = state.session;
    const tiles = View.boardTiles(s, state.viewpoint);
    const html = tiles.map(tileHtml).join("");
    // Skip the DOM write when nothing changed (keeps hover / focus, saves work).
    if (html !== state.lastBoardHtml) {
      state.lastBoardHtml = html;
      elParticipantsGrid.innerHTML = html;
    }
    if (state.openActorId) positionPopover();
  }

  // ---------------------------------------------------------------------------
  // Rendering: metrics popover (outside the grid; opened id lives in `state`)
  // ---------------------------------------------------------------------------

  function bindingText(s, actorId) {
    const b = providerUI ? providerUI.getBindingsFor(actorId) : (s.model_bindings || {})[actorId];
    if (!b) return "未绑定";
    const providers = providerUI ? providerUI.getProviders() : [];
    if (providers.length === 0) return `${b.model_id || "未绑定"} · ${b.reasoning_depth || "high"}`;
    return Core.bindingSummary(b, providers);
  }

  function popoverHtml() {
    const s = state.session;
    const id = state.openActorId;
    const isMod = id === View.MODERATOR_ID;
    const p = isMod ? null : View.participantOf(s, id);
    if (!isMod && !p) return null;

    const name = isMod ? s.moderator.name : p.name;
    const tags = isMod ? ["主持人"] : View.visibleTags(s, state.viewpoint, p);
    const m = View.actorMetrics(s, id);
    const seat = isMod ? undefined : p.seat;
    const agentState = isMod ? s.moderator.state : p.state;
    const showState = state.viewpoint === View.GOD || state.viewpoint === id;
    const stateLabel =
      showState && View.STATE_LABELS[agentState] ? View.STATE_LABELS[agentState] : "";

    const chips = tags.length
      ? tags
          .map(
            (t) =>
              `<span class="tag-badge ${escapeHtml(
                isMod ? "warning" : View.tagVariant(s, t)
              )}">${escapeHtml(t)}</span>`
          )
          .join("")
      : `<span class="tag-badge muted">无公开标签</span>`;

    const stat = (value, label, extra = "") =>
      `<div class="pop-stat${extra}"><span class="pop-stat-num">${value}</span><span class="pop-stat-label">${label}</span></div>`;

    return (
      `<div class="pop-head">${Avatar.glyphHtml(id, seat, { size: 26 })}` +
      `<div class="pop-title"><span class="pop-name">${escapeHtml(name)}</span>` +
      `<span class="pop-id">${escapeHtml(isMod ? "moderator" : id)}${
        stateLabel ? ` · ${escapeHtml(stateLabel)}` : ""
      }</span></div>` +
      `<button type="button" class="pop-close" data-pop-close aria-label="关闭">✕</button></div>` +
      `<div class="pop-tags">${chips}</div>` +
      `<div class="pop-model"><span class="pop-label">模型</span>` +
      `<span class="pop-model-text" title="${escapeHtml(bindingText(s, id))}">${escapeHtml(
        bindingText(s, id)
      )}</span>` +
      `<button type="button" class="btn btn-xs" data-pop-bind="${escapeHtml(id)}">配置</button></div>` +
      `<div class="pop-grid">` +
      stat(numText(m.toolCalls), "工具调用") +
      stat(numText(m.thinkingRounds), "思考次数") +
      stat(numText(m.calls), "LLM 调用") +
      stat(numText(m.rejections), "被拒绝") +
      stat(Core.formatCount(m.promptTokens), "输入 token") +
      stat(Core.formatCount(m.completionTokens), "输出 token") +
      stat(escapeHtml(Core.formatCost(m.costUsd)), "费用（累计）", " pop-stat-wide") +
      `</div>`
    );
  }

  function positionPopover() {
    if (!elPopover || !state.openActorId) return;
    const grid = elParticipantsGrid;
    const tile =
      grid && typeof grid.querySelector === "function"
        ? grid.querySelector(`[data-actor="${cssEscape(state.openActorId)}"]`)
        : null;
    if (!tile || typeof tile.getBoundingClientRect !== "function") return;
    const r = tile.getBoundingClientRect();
    const pw = elPopover.offsetWidth || 300;
    const ph = elPopover.offsetHeight || 230;
    const vw = Number(window.innerWidth) || 1280;
    const vh = Number(window.innerHeight) || 800;
    const left = Math.min(Math.max(8, r.left), Math.max(8, vw - pw - 8));
    let top = r.bottom + 6;
    if (top + ph > vh - 8) top = Math.max(8, r.top - ph - 6);
    elPopover.style.left = `${Math.round(left)}px`;
    elPopover.style.top = `${Math.round(top)}px`;
  }

  function closePopover() {
    if (state.openActorId === null) return;
    state.openActorId = null;
    state.lastPopoverHtml = null;
    renderPopover();
    renderBoard(); // the tag loses its "open" look
  }

  function openPopover(actorId) {
    state.openActorId = actorId;
    renderPopover();
    renderBoard();
  }

  function togglePopover(actorId) {
    if (state.openActorId === actorId) closePopover();
    else openPopover(actorId);
  }

  function renderPopover() {
    if (!elPopover) return;
    if (!state.openActorId) {
      elPopover.hidden = true;
      return;
    }
    const html = popoverHtml();
    if (html === null) {
      // the actor disappeared (new game): close quietly
      state.openActorId = null;
      elPopover.hidden = true;
      return;
    }
    if (html !== state.lastPopoverHtml) {
      state.lastPopoverHtml = html;
      elPopover.innerHTML = html;
    }
    elPopover.hidden = false;
    positionPopover();
  }

  // ---------------------------------------------------------------------------
  // Rendering: channel bar (floor status, unread, tickets)
  // ---------------------------------------------------------------------------

  function unreadBadge(n) {
    return n > 0 ? `<span class="channel-unread" title="${numText(n)} 条未读">${numText(n > 99 ? 99 : n)}${n > 99 ? "+" : ""}</span>` : "";
  }

  function channelItemHtml(ch, unread, compact) {
    const sum = View.floorSummary(state.session, ch);
    const active = state.selectedChannel === ch.id;
    const n = unread.byChannel[ch.id] || 0;
    const attrs =
      `data-channel="${escapeHtml(ch.id)}" title="${escapeHtml(ch.name)}（${escapeHtml(ch.id)}）"`;
    if (compact) {
      // System DMs: one line. The floor chip is only worth showing when the DM is not simply open
      // (closed as a punishment, frozen, ...): a free DM is the normal case.
      const chip =
        sum.icon && sum.kind !== "free"
          ? `<span class="floor-chip floor-${sum.kind} chip-icon" title="${escapeHtml(sum.text)}">${sum.icon}</span>`
          : "";
      return (
        `<div class="channel-item compact${active ? " active" : ""}${n ? " has-unread" : ""}" ${attrs}>` +
        `<div class="channel-row-top"><span class="channel-name">${escapeHtml(ch.name)}</span>` +
        `<span class="channel-flags">${chip}${unreadBadge(n)}</span></div></div>`
      );
    }
    const chip = sum.icon
      ? `<span class="floor-chip floor-${sum.kind}" title="${escapeHtml(sum.detail || sum.text)}">${sum.icon} ${escapeHtml(
          sum.text
        )}</span>`
      : "";
    const detail = sum.detail ? `<div class="channel-detail">${escapeHtml(sum.detail)}</div>` : "";
    return (
      `<div class="channel-item${active ? " active" : ""}${n ? " has-unread" : ""}" ${attrs}>` +
      `<div class="channel-row-top"><span class="channel-name">${escapeHtml(ch.name)}</span>${unreadBadge(n)}</div>` +
      `<div class="channel-meta">${chip}<span class="channel-members">${numText(ch.members.length)} 人</span></div>${detail}</div>`
    );
  }

  function ticketHtml(t) {
    const s = state.session;
    const st = View.ticketStatus(t);
    const preview = String(t.preview || "");
    return (
      `<div class="ticket-item ticket-${st.cls}" data-ticket="${escapeHtml(t.id)}" data-channel="${escapeHtml(
        View.ticketChannelId(s, t)
      )}" title="点击定位到该玩家与主持人的私聊">` +
      `<div class="ticket-row"><span class="ticket-id">${escapeHtml(t.id)}</span>` +
      `<span class="ticket-who">${escapeHtml(View.actorLabel(s, t.player_id))}</span>` +
      `<span class="ticket-status ts-${st.cls}">${escapeHtml(st.text)}</span></div>` +
      `<div class="ticket-preview">${escapeHtml(preview)}</div></div>`
    );
  }

  function renderChannels() {
    const s = state.session;
    const vp = state.viewpoint;
    const unread = View.unreadCounts(s, vp, state.read);
    const { groups, dms } = View.channelGroups(s, vp);
    const tickets = View.visibleTickets(s, vp);
    const total = View.visibleMessages(s, vp, "all").length;

    const html = [];
    html.push(
      `<div class="channel-item all-item${state.selectedChannel === "all" ? " active" : ""}${
        unread.total ? " has-unread" : ""
      }" data-channel="all">` +
        `<div class="channel-row-top"><span class="channel-name">全部</span>${unreadBadge(unread.total)}</div>` +
        `<div class="channel-meta"><span class="channel-members">${numText(total)} 条发言</span></div></div>`
    );

    if (groups.length === 0 && dms.length === 0) {
      html.push(
        `<div class="empty-placeholder">暂无频道<br/>对局启动后由主持人调用工具建立</div>`
      );
    }
    for (const ch of groups) html.push(channelItemHtml(ch, unread, false));

    // Challenge tickets are their own group, right below the channels (the system DMs follow).
    if (tickets.length) {
      const openCount = tickets.filter((t) => t.status !== "resolved").length;
      html.push(
        `<div class="channel-group-title tickets-title">质询工单 <span class="group-count">${numText(
          tickets.length
        )}</span>${openCount ? `<span class="ticket-open-count">${numText(openCount)} 待处理</span>` : ""}</div>`
      );
      for (const t of tickets) html.push(ticketHtml(t));
    }

    if (dms.length) {
      const selectedIsDm = dms.some((c) => c.id === state.selectedChannel);
      const open = state.dmGroupOpen || selectedIsDm;
      const dmUnread = dms.reduce((n, c) => n + (unread.byChannel[c.id] || 0), 0);
      html.push(
        `<div class="channel-group-title" data-dm-toggle role="button" tabindex="0" aria-expanded="${open}">` +
          `<span class="group-caret">${open ? "▼" : "▶"}</span>私聊 <span class="group-count">${numText(
            dms.length
          )}</span>${open ? "" : unreadBadge(dmUnread)}</div>`
      );
      if (open) for (const ch of dms) html.push(channelItemHtml(ch, unread, true));
    }

    const out = html.join("");
    if (out !== state.lastChannelsHtml) {
      state.lastChannelsHtml = out;
      elChannelsList.innerHTML = out;
    }
  }

  // ---------------------------------------------------------------------------
  // Rendering: feed (speech only, one block per committed message)
  // ---------------------------------------------------------------------------

  function appendEllipsis(html) {
    const mark = `<span class="msg-trunc" title="发言超出字数上限，已被截断">…</span>`;
    return /<\/p>$/.test(html) ? html.replace(/<\/p>$/, `${mark}</p>`) : html + mark;
  }

  function channelName(s, channelId) {
    if (channelId === null || channelId === undefined || channelId === "") return "";
    const ch = Store.findById(s.channels, channelId);
    return ch ? ch.name : String(channelId);
  }

  function messageHtml(m) {
    const s = state.session;
    const chName = channelName(s, m.channel_id);
    const cacheKey = `${m.seq}\u0001${chName}`;
    const cached = state.msgHtml.get(cacheKey);
    if (cached !== undefined) return cached;

    const info = View.messageKindInfo(m.kind);
    const sender = View.participantOf(s, m.from);
    const glyph = Avatar.glyphHtml(m.from, sender ? sender.seat : undefined, { size: 22 });
    const name = m.from_name || View.displayName(s, m.from);
    const time = Core.formatClockTime(m.ts);
    let body = Core.renderFormattedText(m.content) || `<p class="fmt-p"></p>`;
    if (Store.isTruncated(m)) body = appendEllipsis(body);

    const classes = ["msg", `msg-kind-${info.cls}`];
    if (m.from === View.MODERATOR_ID) classes.push("msg-moderator");
    if (m.from === View.ENGINE_ID) classes.push("msg-engine");
    const label = info.label
      ? `<span class="msg-kind-badge kind-${info.cls}"${
          m.template_id ? ` title="模板：${escapeHtml(m.template_id)}"` : ""
        }>${escapeHtml(info.label)}</span>`
      : "";
    const ticket = m.ticket_id
      ? `<span class="msg-ticket" title="质询工单">#${escapeHtml(m.ticket_id)}</span>`
      : "";
    const chip = chName
      ? `<span class="msg-chan" data-channel="${escapeHtml(m.channel_id)}" title="只看这个频道">${escapeHtml(
          chName
        )}</span>`
      : "";

    const html =
      `<article class="${classes.join(" ")}" data-seq="${numText(m.seq)}"` +
      ` data-from="${escapeHtml(m.from)}"${m.ticket_id ? ` data-ticket-msg="${escapeHtml(m.ticket_id)}"` : ""}>` +
      `<div class="msg-avatar">${glyph}</div>` +
      `<div class="msg-main"><div class="msg-head"><span class="msg-name">${escapeHtml(name)}</span>` +
      `${chip}${label}${ticket}<span class="msg-time">${escapeHtml(time)}</span></div>` +
      `<div class="msg-body formatted-text">${body}</div></div></article>`;
    Core.boundedSet(state.msgHtml, cacheKey, html, MESSAGE_HTML_CACHE_MAX);
    return html;
  }

  function appendMessageNode(html) {
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    const node = tpl.content && tpl.content.firstElementChild;
    if (node) elFeedRecords.appendChild(node);
  }

  function emptyFeedHtml(s) {
    if (state.viewpoint !== View.GOD) {
      return `<div class="empty-placeholder">${escapeHtml(
        View.viewpointLabel(s, state.viewpoint)
      )} 还没有看到任何发言</div>`;
    }
    return `<div class="empty-placeholder">当前暂无发言 — 点击右上角「开始」后，主持人会建立频道并推进对局；这里只显示发言，思维链与工具调用只写入日志</div>`;
  }

  function renderFeed(force = false) {
    const s = state.session;
    const sel = state.selectedChannel;
    elFeedChannelLabel.textContent =
      sel === "all" ? "频道: 全部记录" : `频道: ${channelName(s, sel)}`;

    const messages = View.visibleMessages(s, state.viewpoint, sel);
    // Channel names are part of every block (the channel chip): a rename / new channel repaints.
    const names = s.channels.map((c) => `${c.id}:${c.name}`).join(",");
    const key = `${s.session_id}|${state.viewpoint}|${sel}|${names}`;
    const last = state.feed;
    const lastSeq = messages.length ? messages[messages.length - 1].seq : 0;
    const sameKey = !force && last.key === key;

    if (
      sameKey &&
      last.count > 0 &&
      messages.length >= last.count &&
      messages[last.count - 1].seq === last.lastSeq
    ) {
      // Whole messages arrive one at a time: append only the new blocks (no streaming, no patching).
      for (let i = last.count; i < messages.length; i++) appendMessageNode(messageHtml(messages[i]));
    } else if (sameKey && last.count === messages.length && last.lastSeq === lastSeq) {
      // nothing changed for this view
    } else {
      elFeedRecords.innerHTML = messages.length
        ? messages.map(messageHtml).join("")
        : emptyFeedHtml(s);
    }
    state.feed = { key, count: messages.length, lastSeq };

    if (state.autoScroll) {
      elFeedList.scrollTop = elFeedList.scrollHeight;
      state.autoScrollTop = elFeedList.scrollTop; // the (clamped) position we scrolled to
    }
  }

  // ---------------------------------------------------------------------------
  // Rendering: dashboard (right column)
  // ---------------------------------------------------------------------------

  function renderDashboard() {
    // The panel is written by the moderator for the spectators: faction statistics are hidden
    // information, so a *player's* viewpoint must not show them (god view only).
    const hidden = state.viewpoint !== View.GOD;
    const dash = hidden ? {} : state.session.dashboard || {};
    const factions = dash.factions || [];
    const tagStats = dash.tag_stats || [];
    const emptyText = hidden
      ? "玩家视角不显示（阵营统计只属于主持人和上帝视角）"
      : "待主持人调用工具初始化";

    const factionsHtml =
      factions.length === 0
        ? `<div class="empty-placeholder">${emptyText}</div>`
        : factions
            .map(
              (f) => `
          <div class="stat-row">
            <span>${escapeHtml(f.name)}</span>
            <span class="stat-value ${escapeHtml(f.variant || "neutral")}">${numText(f.alive)} / ${numText(
                f.total
              )}</span>
          </div>`
            )
            .join("");

    const tagStatsHtml =
      tagStats.length === 0
        ? `<div class="empty-placeholder">${emptyText}</div>`
        : tagStats
            .map(
              (t) => `
          <div class="stat-row">
            <span>${escapeHtml(t.name)}</span>
            <span class="stat-value ${escapeHtml(t.variant || "neutral")}">${numText(t.count)}</span>
          </div>`
            )
            .join("");

    const sig = factionsHtml + "\u0001" + tagStatsHtml;
    if (sig === state.lastDashboardSig) return;
    state.lastDashboardSig = sig;
    elFactionsList.innerHTML = factionsHtml;
    elTagStatsList.innerHTML = tagStatsHtml;
  }

  // ---------------------------------------------------------------------------
  // Render scheduling (coalesce bursts of SSE events: at most one paint per frame)
  // ---------------------------------------------------------------------------

  const renderQueue = { pending: false, run: null, dirty: Store.newDirty(false), force: false };

  function renderNow(dirty, force = false) {
    const d = dirty || Store.newDirty(true);
    if (d.topbar) renderTopbar();
    if (d.board) renderBoard();
    if (d.channels) renderChannels();
    if (d.feed) renderFeed(force);
    if (d.dashboard) renderDashboard();
    if (d.popover) renderPopover();
  }

  function renderAll(force = false) {
    renderNow(Store.newDirty(true), force);
  }

  const pageHidden = () =>
    document.visibilityState === "hidden" || document.hidden === true;

  function scheduleRender(dirty, force = false) {
    Store.mergeDirty(renderQueue.dirty, dirty);
    renderQueue.force = renderQueue.force || force;
    if (renderQueue.pending) return;
    renderQueue.pending = true;
    const run = () => {
      // rAF and the timer fallback below race: whichever comes first paints, the other is a no-op.
      if (!renderQueue.pending || renderQueue.run !== run) return;
      renderQueue.pending = false;
      renderQueue.run = null;
      const d = renderQueue.dirty;
      const f = renderQueue.force;
      renderQueue.dirty = Store.newDirty(false);
      renderQueue.force = false;
      try {
        renderNow(d, f);
      } catch (e) {
        console.error("Render failed", e);
      }
    };
    renderQueue.run = run;
    // requestAnimationFrame never fires in a hidden / occluded tab, which would leave the DOM stale
    // (automation, background windows): use a plain timer there.
    if (!pageHidden() && typeof window.requestAnimationFrame === "function") {
      window.requestAnimationFrame(run);
    } else {
      window.setTimeout(run, pageHidden() ? 120 : 16);
    }
  }

  // A render that was queued on rAF when the page went hidden would wait until it is visible again.
  document.addEventListener("visibilitychange", () => {
    if (renderQueue.pending && renderQueue.run && pageHidden()) window.setTimeout(renderQueue.run, 120);
  });

  // ---------------------------------------------------------------------------
  // Session store glue
  // ---------------------------------------------------------------------------

  function loadViewpointPref() {
    try {
      const v = localStorage.getItem(VIEWPOINT_KEY);
      if (v) state.viewpointPref = v;
    } catch {
      /* storage unavailable: default to god */
    }
  }

  function saveViewpointPref(v) {
    try {
      localStorage.setItem(VIEWPOINT_KEY, v);
    } catch {
      /* ignore */
    }
  }

  /** Switch the spectator viewpoint. Pure front-end: nothing is requested from the server. */
  function setViewpoint(id, persist = true) {
    const vp = View.normalizeViewpoint(state.session, id);
    state.viewpointPref = vp;
    state.viewpoint = vp;
    if (persist) saveViewpointPref(vp);
    state.selectedChannel = View.normalizeSelectedChannel(state.session, vp, state.selectedChannel);
    state.autoScroll = true;
    state.feed.key = ""; // different message set: re-render the feed
    renderAll(true);
  }

  /** Replace the whole session (snapshot, state fetch, control / import / load response). */
  function replaceSession(next) {
    const prevKey = state.sessionKey;
    const key = next.session_id || null;
    state.session = next;
    state.sessionKey = key;
    state.loaded = true;
    // Whole-session replacement: never trust per-message HTML cached from the previous object.
    state.msgHtml.clear();
    if (key !== prevKey) {
      // A different game: forget selection and unread marks.
      state.selectedChannel = "all";
      resetReadState(next);
    } else if (!state.read.baseline) {
      resetReadState(next);
    }
    state.viewpoint = View.normalizeViewpoint(next, state.viewpointPref);
    state.selectedChannel = View.normalizeSelectedChannel(next, state.viewpoint, state.selectedChannel);
    if (state.openActorId && state.openActorId !== View.MODERATOR_ID && !View.participantOf(next, state.openActorId)) {
      state.openActorId = null;
    }
    if (providerUI) providerUI.onSession(next);
    if (workspaceUI) workspaceUI.onSession(next);
    // The feed compares what it already shows (view key + count + last seq), so an identical
    // snapshot after a reconnect does not rebuild the list or move the scroll position.
    scheduleRender(Store.newDirty(true));
  }

  /** A whole session from an HTTP response; ignored when the SSE stream is already ahead of it. */
  function adoptHttpSession(raw, opts = {}) {
    if (!raw || typeof raw !== "object") return false;
    const incoming = Store.normalizeSession(raw);
    if (!opts.force && !Store.shouldAdoptHttpSession(state.session, incoming, state.lastEventId)) {
      return false;
    }
    const differentGame = incoming.session_id !== state.session.session_id;
    if (differentGame) state.lastEventId = incoming.event_id > 0 ? incoming.event_id : 0;
    else if (incoming.event_id > state.lastEventId) state.lastEventId = incoming.event_id;
    replaceSession(incoming);
    return true;
  }

  const PROVIDER_RELEVANT = new Set(["session.updated", "game.status"]);

  /** Apply one SSE v2 event {id, type, data}. */
  function handleEvent(payload) {
    if (!payload || typeof payload !== "object" || !payload.type) return;
    if (Store.isStaleEvent(state.lastEventId, payload)) return; // replayed after a reconnect
    const id = Store.eventIdOf(payload, NaN);
    const result = Store.applyEvent(state.session, payload);

    if (result.replaced) {
      state.lastEventId = Number.isFinite(id) ? id : result.session.event_id;
      state.sseResumeId = state.lastEventId;
      replaceSession(result.session);
      return;
    }
    if (Number.isFinite(id) && id > 0) {
      if (id > state.lastEventId) state.lastEventId = id;
      if (id > state.sseResumeId) state.sseResumeId = id;
      if (id > state.session.event_id) state.session.event_id = id;
    }
    if (result.reload) {
      // A workspace switch changes providers, templates and sessions for the whole
      // server process; every tab must rebuild its UI instead of merging a session.
      if (typeof window.location.reload === "function") window.location.reload();
      return;
    }
    if (result.toast) showToast(result.toast);
    if (result.message) onMessageSeen(result.message);
    if (result.flash) flashActor(result.flash);
    if (PROVIDER_RELEVANT.has(payload.type)) {
      if (providerUI) providerUI.onSession(state.session);
      if (workspaceUI) workspaceUI.onSession(state.session);
    }
    scheduleRender(result.dirty);
  }

  // ---------------------------------------------------------------------------
  // Event delegation (immune to the periodic DOM refreshes)
  // ---------------------------------------------------------------------------

  /**
   * Activate on mousedown AND click. The board / channel list can be repainted
   * between a mousedown and its mouseup, which would swallow the click; the click
   * that does arrive right after a handled mousedown is skipped.
   */
  function bindPress(container, selector, keyAttr, handler) {
    if (!container) return;
    let recent = { key: null, at: 0 };
    container.addEventListener("mousedown", (evt) => {
      if (evt.button !== undefined && evt.button !== 0) return;
      const node = closestFrom(evt.target, selector);
      if (!node) return;
      recent = { key: node.getAttribute(keyAttr), at: Date.now() };
      handler(node, evt);
    });
    container.addEventListener("click", (evt) => {
      const node = closestFrom(evt.target, selector);
      if (!node) return;
      const key = node.getAttribute(keyAttr);
      if (recent.key !== null && recent.key === key && Date.now() - recent.at < 800) {
        recent = { key: null, at: 0 };
        return;
      }
      handler(node, evt);
    });
  }

  function selectChannel(id) {
    const next = View.normalizeSelectedChannel(state.session, state.viewpoint, id);
    state.selectedChannel = next;
    markChannelRead(next);
    state.autoScroll = true;
    renderNow({ ...Store.newDirty(false), channels: true, feed: true }, true);
  }

  function locateTicket(ticketId, channelId) {
    selectChannel(channelId);
    state.autoScroll = false;
    const node =
      elFeedRecords && typeof elFeedRecords.querySelector === "function"
        ? elFeedRecords.querySelector(`[data-ticket-msg="${cssEscape(ticketId)}"]`)
        : null;
    if (node) {
      if (typeof node.scrollIntoView === "function") node.scrollIntoView({ block: "center" });
      node.classList.add("msg-flash");
      setTimeout(() => node.classList.remove("msg-flash"), 1800);
    }
  }

  bindPress(elChannelsList, ".channel-item[data-channel]", "data-channel", (node) =>
    selectChannel(node.getAttribute("data-channel"))
  );
  bindPress(elChannelsList, ".ticket-item[data-ticket]", "data-ticket", (node) =>
    locateTicket(node.getAttribute("data-ticket"), node.getAttribute("data-channel"))
  );
  bindPress(elChannelsList, "[data-dm-toggle]", "data-dm-toggle", () => {
    state.dmGroupOpen = !state.dmGroupOpen;
    renderNow({ ...Store.newDirty(false), channels: true });
  });
  bindPress(elParticipantsGrid, "[data-tag-actor]", "data-tag-actor", (node) =>
    togglePopover(node.getAttribute("data-tag-actor"))
  );

  // The channel chip on a message filters the feed to that channel.
  elFeedRecords.addEventListener("click", (evt) => {
    if (window.getSelection && window.getSelection().toString().trim().length > 0) return;
    const chip = closestFrom(evt.target, ".msg-chan[data-channel]");
    if (chip) selectChannel(chip.getAttribute("data-channel"));
  });

  if (elPopover) {
    elPopover.addEventListener("click", (evt) => {
      if (closestFrom(evt.target, "[data-pop-close]")) {
        closePopover();
        return;
      }
      const bind = closestFrom(evt.target, "[data-pop-bind]");
      if (bind) {
        const actor = bind.getAttribute("data-pop-bind");
        closePopover();
        if (providerUI) providerUI.openBindings(actor);
      }
    });
  }

  // Click outside the popover (and not on a tag) or Esc closes it.
  if (typeof document.addEventListener === "function") {
    document.addEventListener("click", (evt) => {
      if (!state.openActorId) return;
      if (closestFrom(evt.target, "#metrics-popover") || closestFrom(evt.target, "[data-tag-actor]")) return;
      closePopover();
    });
    document.addEventListener("keydown", (evt) => {
      if (evt && evt.key === "Escape" && state.openActorId) closePopover();
    });
  }
  if (typeof window.addEventListener === "function") {
    window.addEventListener("resize", () => {
      if (state.openActorId) positionPopover();
    });
  }

  elFeedList.addEventListener("scroll", () => {
    // Scroll events arrive a frame late. If new messages were appended after our own scroll-to-bottom,
    // the position would look "not at the bottom" and silently switch following off: events that
    // merely report where WE scrolled to are ignored; only a real user scroll changes the mode.
    if (state.autoScrollTop !== null && Math.abs(elFeedList.scrollTop - state.autoScrollTop) <= 1) return;
    const nearBottom =
      elFeedList.scrollHeight - elFeedList.scrollTop - elFeedList.clientHeight < 60;
    state.autoScroll = nearBottom;
    state.autoScrollTop = null;
  });

  // ---------------------------------------------------------------------------
  // Controls
  // ---------------------------------------------------------------------------

  elBtnTheme.addEventListener("click", () => {
    const htmlEl = document.documentElement;
    const cur = htmlEl.getAttribute("data-theme") || "light";
    const next = cur === "light" ? "dark" : "light";
    htmlEl.setAttribute("data-theme", next);
  });

  if (elSelectViewpoint) {
    elSelectViewpoint.addEventListener("change", () => setViewpoint(elSelectViewpoint.value));
  }

  /** POST a control endpoint and adopt the session in its response. */
  async function control(path, okMessage) {
    try {
      const res = await apiPost(path);
      if (res && res.session) adoptHttpSession(res.session, { force: path === "api/control/new" });
      if (okMessage) showToast(okMessage);
      return true;
    } catch (err) {
      showToast(err.message);
      return false;
    }
  }

  elBtnNew.addEventListener("click", async () => {
    const ok = await control("api/control/new", "已重置为空白未开始对局，点击「开始」由主持人初始化并推进");
    if (ok) {
      state.selectedChannel = "all";
      renderAll(true);
    }
  });

  elBtnStart.addEventListener("click", () =>
    control("api/control/start", "对局已启动 — 主持人开始建立频道并推进对局")
  );

  elBtnResume.addEventListener("click", () => {
    const finished = state.session.status === "finished";
    return control(
      "api/control/start",
      finished ? "已在终局后继续：主持人和玩家重新开始行动" : "对局已继续运行"
    );
  });

  elBtnPause.addEventListener("click", () =>
    control("api/control/pause", "已暂停：所有 Agent 已停止，计时冻结")
  );

  elBtnSave.addEventListener("click", async () => {
    try {
      const res = await apiPost("api/session/save");
      const filename = (res && res.filename) || "arena_session.json";
      if (res && res.session) {
        const blob = new Blob([JSON.stringify(res.session, null, 2)], {
          type: "application/json",
        });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
      }
      showToast(`对局已保存: ${filename}`);
    } catch (err) {
      showToast(err.message);
    }
  });

  if (elSelectExecDepth) {
    elSelectExecDepth.addEventListener("change", async () => {
      const val = elSelectExecDepth.value;
      try {
        await apiPost("api/session/execution-depth", {
          execution_depth: val,
        });
        showToast(`已将执行深度切换为: ${val}`);
      } catch (err) {
        showToast(err.message);
      }
    });
  }

  // ---------------------------------------------------------------------------
  // Session import
  // ---------------------------------------------------------------------------
  const elBtnImportSession = document.getElementById("btn-import-session");
  const elFileImportSession = document.getElementById("file-import-session");
  if (elUsagePill) {
    // Cost breakdown lives with the model bindings.
    elUsagePill.addEventListener("click", () => {
      if (providerUI) providerUI.openBindings();
    });
  }

  function handleSessionLoaded(session) {
    adoptHttpSession(session, { force: true });
    state.selectedChannel = "all";
    renderAll(true);
  }

  if (elBtnImportSession && elFileImportSession) {
    elBtnImportSession.addEventListener("click", () => {
      elFileImportSession.click();
    });
    elFileImportSession.addEventListener("change", async (evt) => {
      const file = evt.target.files && evt.target.files[0];
      if (!file) return;
      try {
        const text = await file.text();
        let json;
        try {
          json = JSON.parse(text);
        } catch {
          throw new Error("文件不是有效的 JSON");
        }
        const res = await apiPost("api/session/import", json);
        handleSessionLoaded(res.session);
        showToast(`已成功导入存档: ${file.name}`);
      } catch (err) {
        showToast(`导入失败: ${err.message}`);
      } finally {
        elFileImportSession.value = "";
      }
    });
  }

  if (window.ArenaLayout) {
    // Layout is pure DOM/state: create it before the first render so the saved
    // collapsed panels are applied immediately.
    layoutUI = window.ArenaLayout.create({ Core });
  }

  if (window.ArenaProviderUI) {
    providerUI = window.ArenaProviderUI.create({
      api,
      Core,
      showToast,
      getSession: () => state.session,
      onSessionLoaded: handleSessionLoaded,
      // Providers/bindings changed: the metrics popover shows the model of an agent.
      onChange: () => scheduleRender({ ...Store.newDirty(false), popover: true }),
    });
  }

  if (window.ArenaWorkspaceUI) {
    workspaceUI = window.ArenaWorkspaceUI.create({
      api,
      Core,
      showToast,
      getSession: () => state.session,
      onSessionLoaded: handleSessionLoaded,
    });
  }

  // ---------------------------------------------------------------------------
  // Access-token modal (only ever shown after the server answers 401)
  // ---------------------------------------------------------------------------

  function showTokenModal(hadToken) {
    return new Promise((resolve) => {
      if (!elTokenModal || !elTokenForm || !elTokenInput) {
        resolve(null);
        return;
      }
      if (elTokenMessage) {
        elTokenMessage.textContent = hadToken
          ? "已保存的访问令牌无效或已更改，请重新输入 ARENA_API_TOKEN。"
          : "服务器要求访问令牌（ARENA_API_TOKEN），请输入后继续。";
      }
      elTokenInput.value = "";
      elTokenModal.style.display = "flex";
      elTokenInput.focus();

      const cleanup = () => {
        elTokenModal.style.display = "none";
        elTokenForm.removeEventListener("submit", onSubmit);
        if (elBtnCancelToken) elBtnCancelToken.removeEventListener("click", onCancel);
      };
      const onSubmit = (evt) => {
        evt.preventDefault();
        const v = elTokenInput.value.trim();
        if (!v) {
          elTokenInput.focus();
          return;
        }
        setToken(v);
        elTokenInput.value = "";
        cleanup();
        hideConnBanner();
        resolve(v);
        onTokenChanged();
      };
      const onCancel = () => {
        cleanup();
        showConnBanner("未授权：点击此处输入访问令牌", true);
        resolve(null);
      };
      elTokenForm.addEventListener("submit", onSubmit);
      if (elBtnCancelToken) elBtnCancelToken.addEventListener("click", onCancel);
    });
  }

  // ---------------------------------------------------------------------------
  // Connection status banner
  // ---------------------------------------------------------------------------

  function showConnBanner(text, clickable = false) {
    if (!elConnBanner) return;
    elConnBanner.textContent = text;
    elConnBanner.hidden = false;
    elConnBanner.classList.toggle("clickable", clickable);
  }

  function hideConnBanner() {
    if (!elConnBanner) return;
    elConnBanner.hidden = true;
    elConnBanner.textContent = "";
    elConnBanner.classList.remove("clickable");
  }

  if (elConnBanner) {
    elConnBanner.addEventListener("click", () => {
      if (!elConnBanner.classList.contains("clickable")) return;
      // Re-trigger an authenticated request; a 401 reopens the token modal.
      loadModelConfig().then(async (r) => {
        if (!r.ok && r.error && r.error.kind === "auth") return;
        await reloadState();
        if (!state.sseConnected) restartSSE();
      });
    });
  }

  // ---------------------------------------------------------------------------
  // SSE v2 (exponential backoff + jitter, single timer, resume with last_event_id)
  // ---------------------------------------------------------------------------

  const SSE_DISABLED = window.location.search.includes("nosse=1");

  function markSseConnected() {
    if (state.sseConnected && state.sseAttempt === 0) return;
    state.sseConnected = true;
    state.sseAttempt = 0;
    hideConnBanner();
  }

  function scheduleReconnect() {
    // Never stack timers: one pending reconnect at most.
    if (state.sseTimer !== null) return;
    const delay = Core.computeBackoff(state.sseAttempt);
    state.sseAttempt += 1;
    showConnBanner(
      `连接断开，正在重连…（第 ${state.sseAttempt} 次，约 ${Math.max(
        1,
        Math.round(delay / 1000)
      )} 秒后）`
    );
    state.sseTimer = window.setTimeout(() => {
      state.sseTimer = null;
      connectSSE();
    }, delay);
  }

  function closeSSE() {
    if (state.sseTimer !== null) {
      window.clearTimeout(state.sseTimer);
      state.sseTimer = null;
    }
    if (state.sse) {
      const old = state.sse;
      state.sse = null;
      old.onopen = old.onmessage = old.onerror = null;
      old.close();
    }
  }

  function restartSSE() {
    if (SSE_DISABLED) return;
    state.sseAttempt = 0;
    connectSSE();
  }

  // EventSource cannot expose HTTP status. When the stream fails, probe with a
  // normal request: a 401 pops the token modal (and resumes the stream once a
  // token is entered); anything else is just a transient outage.
  function probeAuthAfterSseError() {
    if (authPrompt) return;
    api.get("api/state").catch(() => {});
  }

  function connectSSE() {
    closeSSE();
    // A re-created EventSource does not send Last-Event-ID by itself: resume via the query string.
    const es = new EventSource(Core.buildEventsUrl(URL_PREFIX, getToken(), state.sseResumeId));
    state.sse = es;

    es.onopen = () => {
      if (state.sse === es) markSseConnected();
    };

    es.onmessage = (evt) => {
      if (state.sse !== es) return;
      markSseConnected();
      try {
        handleEvent(JSON.parse(evt.data));
      } catch (e) {
        console.error("Failed to handle SSE payload", e);
      }
    };

    es.onerror = () => {
      if (state.sse !== es) return;
      es.onopen = es.onmessage = es.onerror = null;
      es.close();
      state.sse = null;
      state.sseConnected = false;
      // Don't hammer the server while the user is typing a token.
      if (authPrompt) {
        showConnBanner("等待输入访问令牌…");
        return;
      }
      scheduleReconnect();
      if (state.sseAttempt === 1 || state.sseAttempt % 4 === 0) {
        probeAuthAfterSseError();
      }
    };
  }

  function onTokenChanged() {
    // A (new) token should resume the live stream right away.
    if (!state.sseConnected) restartSSE();
  }

  // Providers + bindings are loaded at startup; a 401 reuses the token modal.
  async function loadModelConfig() {
    if (!providerUI) return { ok: true };
    const res = await providerUI.load();
    if (!res.ok && res.error && res.error.kind !== "auth") {
      showToast(`读取模型配置失败：${res.error.message}`);
    }
    return res;
  }

  async function reloadState() {
    try {
      const data = await api.get("api/state");
      if (data && data.app_version) state.appVersion = String(data.app_version);
      if (data && data.session) {
        // /api/state also reports the restore flag at top level.
        if (data.restored && !data.session.restored_from_save) {
          data.session.restored_from_save = true;
        }
        adoptHttpSession(data.session, { force: true });
        // v0.11: /api/state carries the workspace summary (name / path withheld
        // for remote clients); it is the cheapest way to label the top bar.
        if (workspaceUI) {
          workspaceUI.onState(data.workspace || (data.session && data.session.workspace));
        }
        renderAll(true);
      }
      return true;
    } catch (err) {
      console.error("State fetch error", err);
      showToast(`加载对局状态失败：${err.message}`);
      return false;
    }
  }

  async function init() {
    loadViewpointPref();
    // First paint of the (empty) shell so the layout never looks broken while loading.
    renderAll(true);
    // Both calls check resp.ok through the shared client. A 401 here is the
    // first moment the token modal can appear; with no token configured the
    // user is never bothered.
    const llm = await loadModelConfig();
    if (!llm.ok && llm.error && llm.error.kind === "auth") {
      // The user dismissed the token prompt: don't ask again right away.
      // The banner lets them retry on demand.
      showConnBanner("未授权：点击此处输入访问令牌", true);
      return;
    }
    await reloadState();
    // v0.11: fetch the workspace summary (name, recent list, native picker
    // availability) without blocking the first render.
    if (workspaceUI) {
      workspaceUI.load().catch(() => {});
    }
    if (!SSE_DISABLED) {
      window.setTimeout(connectSSE, 300);
    }
  }

  // Small surface for tests and for poking at the UI from the console.
  window.ArenaApp = {
    state,
    handleEvent,
    adoptHttpSession,
    setViewpoint,
    selectChannel,
    openPopover,
    closePopover,
    togglePopover,
    renderAll,
    renderNow,
  };

  init();
})();
