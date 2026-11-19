/* ==========================================================================
   Arena-Agents v0.13.1 Frontend Controller
   (async agents · SSE v2 · player cards with pixel avatars · request cards · responsive)

   Pure logic lives in
     core.js      escaping / API client / formatting / budget + price helpers
     avatar.js    pixel avatars (sprite library static/avatar/parts.json, canvas -> data URL)
     store.js     SSE event application on a PublicSession v3 object
     view.js      viewpoint filtering, cards, channels, request cards, banners
     platform.js  desktop / tablet / phone detection (<html data-platform>)
   Panels with their own state: roster.js (detail panel, roster menu), budget.js (limit dialog),
   providers.js (Provider / price source / bindings), layout.js, archive.js (settings: export / import an archive).
   This file wires them to the DOM.

   What is NOT here (v0.12 -> v0.12.1): the 🎤 / ✋ / ■ card states and their legend, the execution
   depth select, the "全部" channel, the right-hand "对局状态" column (it became the summary strip of
   the participant bar). Still not here: streaming drafts, chain-of-thought, tool-call cards.
   ========================================================================== */

(() => {
  const Core = window.ArenaCore;
  const Avatar = window.ArenaAvatar;
  const Store = window.ArenaStore;
  const View = window.ArenaView;
  const Platform = window.ArenaPlatform || null;
  if (!Core) {
    throw new Error("core.js 未加载：请确认 static/js/core.js 可访问");
  }
  if (!Avatar || !Store || !View) {
    throw new Error("avatar.js / store.js / view.js 未加载：请确认 static/js 下的脚本可访问");
  }
  const { escapeHtml, numText } = Core;
  const MOD = View.MODERATOR_ID;

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
  const LONG_PRESS_MS = 450;
  let providerUI = null; // v0.10 provider / binding UI (providers.js)
  let layoutUI = null; // v0.11 collapse/resize UI (layout.js)
  let archiveUI = null; // v0.13 settings dialog (export / import an archive) + the "old data found" notice (archive.js)
  let rosterUI = null; // v0.12.1 detail panel + roster menu (roster.js)
  let budgetUI = null; // v0.12.1 token / cost limit dialog (budget.js)
  let historyUI = null; // v0.13 history of games: list, delete, continue, replay (history.js)
  let replayUI = null; // v0.13 replay player: plays a saved game back in a session of its own (replay.js)
  let takeoverUI = null; // v0.13 viewpoint bar / takeover bar / reshuffle reminder (takeover.js)
  let composerUI = null; // v0.13 input box, abilities, request answers of the played seat (composer.js)
  let marksUI = null; // v0.13.1 colour marks on the players: dots, palette, localStorage (marks.js)
  let thoughtsUI = null; // v0.13.1 the thinking record window of an agent (thoughts.js)
  let calllogUI = null; // v0.13.1 the error log window + the unread badge of the top bar (calllog.js)
  let platformCtl = null; // platform.js controller

  const state = {
    session: Store.emptySession(), // PublicSession v3, updated in place by SSE events
    loaded: false, // a first session (HTTP or SSE) has arrived
    sessionKey: null,
    viewpointPref: View.GOD, // what the user chose (kept even while the game has no players yet)
    viewpoint: View.GOD, // effective viewpoint: "god" | player id
    selectedChannel: null, // channel id; null while there is no channel (the default is picked automatically)
    channelChosen: false, // false: the selection is automatic (public channel, else the first one) and follows new channels
    dmGroupOpen: false,
    ticketPopOpen: false,
    phoneTab: "feed", // phone layout: "feed" | "channels" | "players"
    platform: null, // platform.js result
    autoScroll: true,
    autoScrollTop: null, // scrollTop of the feed right after our own scroll-to-bottom
    read: { baseline: 0, seqs: {} }, // unread watermarks per channel
    flash: new Map(), // actor id -> expiry (ms): briefly highlights a card (ability.used)
    clockSync: { clock: 0, at: 0, running: false }, // estimate of the game clock (sleep timers, request deadlines)
    replay: null, // v0.13: while a saved game is replayed: {live} - the live session is kept aside (replay.js)
    // SSE
    sse: null,
    sseAttempt: 0,
    sseTimer: null,
    sseConnected: false,
    lastEventId: 0, // highest event id reflected in state.session
    sseResumeId: 0, // last id received over the stream (resume point for reconnects)
    appVersion: "",
    avatarEpoch: 0, // bumps when the sprite library arrives (avatars change from circles to pixel art)
    rosterSig: "", // names + avatars: part of every cache key that contains them
    // render caches (skip DOM writes when nothing changed)
    lastBoardHtml: null,
    lastSummaryHtml: null,
    lastChannelsHtml: null,
    lastHintsHtml: null,
    lastBannerHtml: null,
    lastModHtml: null,
    lastTabbarHtml: null,
    lastTicketPopHtml: null,
    feed: { key: "", rows: [] },
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
  const elModChip = $("mod-chip");
  const elTopbarStatus = $("topbar-status");

  const elUsagePill = $("usage-telemetry-pill");
  const elUsageTokensText = $("usage-tokens-text");
  const elUsageCostText = $("usage-cost-text");

  const elBtnNew = $("btn-new");
  const elBtnStart = $("btn-start");
  const elBtnPause = $("btn-pause");
  const elBtnResume = $("btn-resume");
  const elBtnMore = $("btn-more");
  const elMoreMenu = $("more-menu");
  const elMoreMenuList = $("more-menu-list");

  const elMain = $("main-container");
  const elTabbar = $("tabbar");
  const elParticipantsGrid = $("participants-grid");
  const elBoardSummary = $("board-summary");
  const elChannelsList = $("channels-list");
  const elFeedChannelLabel = $("feed-channel-label");
  const elFeedList = $("feed-list");
  const elFeedRecords = $("feed-records-container");
  const elTicketPop = $("ticket-pop");
  const elTouchTip = $("touch-tip");
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

  /** event.target.closest that tolerates text nodes and the node-test stubs. */
  function closestFrom(target, selector) {
    let node = target;
    // A text node has no closest(): walk up to its element.
    if (node && typeof node.closest !== "function" && node.parentElement) node = node.parentElement;
    return node && typeof node.closest === "function" ? node.closest(selector) : null;
  }

  const isPhone = () => Boolean(state.platform && state.platform.platform === "phone");

  /** Set an element's innerHTML only when it changed (keeps hover / focus, saves work). */
  function setHtml(node, html, cacheKey) {
    if (!node) return;
    if (state[cacheKey] === html) return;
    state[cacheKey] = html;
    node.innerHTML = html;
  }

  // ---------------------------------------------------------------------------
  // Game clock estimate (sleep timers, request deadlines)
  // ---------------------------------------------------------------------------

  function clockNow() {
    return Core.gameClockNow(state.clockSync, Date.now());
  }

  /**
   * The server told us the clock (snapshot / session.updated): trust it and run from now on - but only while the clock
   * runs.  v0.13.1 (G2, §12.2): `status` stays "running" during a soft pause while the clock stands still, so the
   * session's `clock_running` decides (judged by `status` when a server does not send it).
   */
  function syncClock(session) {
    state.clockSync = { clock: Number(session.clock) || 0, at: Date.now(), running: View.clockRunning(session) };
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
    state.read = { baseline: lastMessageSeq(session), seqs: {} };
  }

  function markChannelRead(channelId) {
    if (!channelId) return;
    state.read.seqs[channelId] = Math.max(
      state.read.seqs[channelId] || 0,
      View.latestSeq(state.session, channelId)
    );
  }

  function onMessageSeen(msg) {
    // A message that arrives in the channel being read is not unread.
    if (msg.channel_id && msg.channel_id === state.selectedChannel) {
      state.read.seqs[msg.channel_id] = Math.max(state.read.seqs[msg.channel_id] || 0, msg.seq);
    }
  }

  /**
   * The selected channel is always a visible one (there is no "all" any more): keep the user's choice
   * while it is valid, otherwise fall back to the public channel (or the first one).
   */
  function ensureSelectedChannel() {
    // an automatic selection re-evaluates the default every time (the public channel appears after
    // the game starts: it replaces the first private chat that was selected before)
    const next = state.channelChosen
      ? View.normalizeSelectedChannel(state.session, state.viewpoint, state.selectedChannel)
      : View.defaultChannelId(state.session, state.viewpoint);
    if (next !== state.selectedChannel) {
      state.selectedChannel = next;
      if (next) markChannelRead(next);
      state.autoScroll = true;
    }
    return next;
  }

  function flashActor(info) {
    if (!info || !info.actor_id) return;
    state.flash.set(info.actor_id, Date.now() + FLASH_MS);
    const s = state.session;
    showToast(
      `⚡ ${View.displayName(s, info.actor_id)} 发动了能力${info.ability_id ? `：${info.ability_id}` : ""}`
    );
    setTimeout(() => {
      state.flash.delete(info.actor_id);
      scheduleRender({ board: true });
    }, FLASH_MS);
  }

  /**
   * Names + avatars (+ the colour marks, v0.13.1): every cached HTML that contains them is keyed by this - a mark
   * that changes must repaint the messages that were drawn with the old one.
   */
  function computeRosterSig(s) {
    return (
      `${state.avatarEpoch}|` +
      s.participants.map((p) => `${p.id}:${p.name}:${Avatar.descriptionKey(p.avatar)}`).join("|") +
      `|${s.moderator.name}:${Avatar.descriptionKey(s.moderator.avatar)}` +
      `|${marksUI ? marksUI.signature() : ""}`
    );
  }

  /** The coloured dot of a marked player (before a name); "" for an unmarked one, for the moderator and without marks. */
  const markDot = (id) => (marksUI ? marksUI.nameDotHtml(id) : "");

  /** An <img> for any actor (moderator / engine / player). */
  function avatarImg(id, size, className) {
    const a = View.actorAvatar(state.session, id);
    return Avatar.imgHtml(id, a.seat, a.desc, { size, className });
  }

  // ---------------------------------------------------------------------------
  // Rendering: top bar
  // ---------------------------------------------------------------------------

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
    setHtml(elRequestHints, html, "lastHintsHtml");
  }

  function renderBanner(s) {
    if (!elStatusBanner) return;
    const lines = View.statusBanners(s);
    const html = lines
      .map((l) => {
        // a phone shows the short sentence (CSS switches by <html data-layout>)
        const body = l.short
          ? `<span class="banner-full">${escapeHtml(l.text)}</span><span class="banner-short">${escapeHtml(l.short)}</span>`
          : escapeHtml(l.text);
        return `<span class="banner-line banner-${escapeHtml(l.kind)}">${body}</span>`;
      })
      .join("");
    if (html !== state.lastBannerHtml) {
      state.lastBannerHtml = html;
      elStatusBanner.innerHTML = html;
      elStatusBanner.className = `status-banner${lines.length ? ` banner-${lines[0].kind}` : ""}`;
    }
    elStatusBanner.hidden = lines.length === 0;
  }

  /** The moderator is a small chip at the end of the status row (not a card). */
  function renderModeratorChip(s) {
    if (!elModChip) return;
    const t = View.moderatorTile(s, state.viewpoint, { clockNow: clockNow() });
    const html =
      `${avatarImg(MOD, 20, "mod-chip-avatar")}` +
      `<span class="mod-chip-name">${escapeHtml(t.name)}</span>` +
      (t.state
        ? `<span class="mod-chip-state st-${t.state}" title="${escapeHtml(t.stateTitle)}">${t.stateIcon}</span>`
        : "");
    setHtml(elModChip, html, "lastModHtml");
    if (elModChip.setAttribute) elModChip.setAttribute("title", t.title);
    elModChip.classList.toggle("popover-open", Boolean(rosterUI && rosterUI.openId() === MOD));
  }

  function usageTitle(budget) {
    if (!budget.hasLimit) return "点击打开模型绑定（费用按每次调用的价格快照累加）。点「上限」可设置 token / 费用上限。";
    const parts = [];
    if (budget.tokens.max) parts.push(`token ${Core.formatPercent(budget.tokens.ratio)}`);
    if (budget.cost.max) parts.push(`费用 ${Core.formatPercent(budget.cost.ratio)}`);
    const word = budget.state === "reached" ? "已达到上限" : budget.state === "warning" ? "已超过 80%" : "预算";
    return `${word}：${parts.join("，")}${budget.forceStop ? "（达到上限后强制停止）" : ""}。点击打开模型绑定。`;
  }

  function renderTopbar() {
    const s = state.session;
    elAppTitle.textContent = s.title || "Arena-Agents";
    const ver = state.appVersion || s.version || "";
    if (elAppVersion) elAppVersion.textContent = s.demo ? `${ver} · 演示模式` : ver;

    elStatusText.textContent = View.statusText(s);
    if (elStatusText.setAttribute) elStatusText.setAttribute("title", View.statusText(s));
    elPhasePill.textContent = s.phase_text || "未开始";
    // before the game starts the phase repeats the status ("未开始 …"): a phone has no room for both
    if (elPhasePill.classList) elPhasePill.classList.toggle("is-idle", s.status === "idle");
    elStatusDot.className = `status-dot ${View.statusDotClass(s)}`;

    // Top-right token & cost telemetry (the cost is accumulated server-side per call);
    // yellow from 80 % of the limit, red when it is reached.
    if (elUsageTokensText && elUsageCostText) {
      const u = s.usage_stats || {};
      const total = u.total_tokens > 0 ? u.total_tokens : 0;
      // the number only: the unit " Tokens" is its own element (hidden when the top bar is tight)
      elUsageTokensText.textContent = Core.formatTokensShort(total);
      elUsageCostText.textContent = `$${Core.sessionCostUsd(u).toFixed(4)}`;
    }
    if (elUsagePill) {
      const budget = View.budgetView(s);
      if (elUsagePill.setAttribute) {
        elUsagePill.setAttribute("data-budget", budget.state);
        elUsagePill.setAttribute("title", usageTitle(budget));
      }
    }

    // Start only on a fresh game; Resume also works after the game finished (D8).
    const ctl = View.controlState(s);
    elBtnStart.disabled = !ctl.start;
    elBtnPause.disabled = !ctl.pause;
    elBtnResume.disabled = !ctl.resume;
    // v0.13.1 (G7): during a soft pause the top bar's 暂停 is the hard pause (it ends the soft pause), and the soft
    // pause is continued from the takeover bar - the top bar's 继续 stays grey
    const softPaused = View.softPauseOf(s).active;
    if (elBtnPause.setAttribute) {
      elBtnPause.setAttribute("title", softPaused ? "硬暂停：立即停下所有 agent，取消仍在途的模型调用（同时结束软暂停）" : "");
    }
    if (elBtnResume.setAttribute) {
      elBtnResume.setAttribute("title", softPaused ? "现在是软暂停：请用接管栏里的「▶ 继续」" : "继续对局；对局结束后也可以继续");
    }
    elBtnNew.disabled = Boolean(s.replay); // a replay is not the live game: nothing of the live controls applies
    if (historyUI) historyUI.onSession();

    // v0.13: the viewpoint bar [视角▾][切换][接管] / the takeover bar / the reshuffle reminder (takeover.js)
    if (takeoverUI) takeoverUI.render();
    renderRequestHints(s);
    renderBanner(s);
    renderModeratorChip(s);
    if (elFeedViewing) {
      elFeedViewing.textContent = `视角：${View.viewpointLabel(s, state.viewpoint)}`;
      elFeedViewing.classList.toggle("is-player", state.viewpoint !== View.GOD);
    }
    // (the drop-down's own colour follows its pending choice: takeover.js)
    if (budgetUI) budgetUI.onSession(s);
    if (rosterUI) rosterUI.renderControls();
  }

  // ---------------------------------------------------------------------------
  // Rendering: player cards (E17: identity is the main tag; badges; three agent states)
  // ---------------------------------------------------------------------------

  function tileHtml(t) {
    const classes = ["pcard"];
    if (t.out) classes.push("out");
    if (t.viewing) classes.push("viewing");
    if (t.state) classes.push(`st-${t.state}`);
    if (state.flash.has(t.id)) classes.push("flash");
    if (rosterUI && rosterUI.openId() === t.id) classes.push("popover-open");
    // v0.13.1 (G16, G18): the colour mark - a stripe along the card's top edge and a dot in the top-right corner (the
    // button of the palette while a seat is played); the sheriff's badge makes room for it
    const markEl = marksUI ? marksUI.cardMarkHtml(t.id) : "";
    const markClass = marksUI ? marksUI.cardClass(t.id) : "";
    if (markClass) classes.push(markClass);
    if (markEl) classes.push("mark-slot");

    const badges = t.badges
      .map(
        (b) =>
          `<span class="pbadge pbadge-${escapeHtml(b.variant)}" title="${escapeHtml(b.tag)}">${escapeHtml(b.text)}</span>`
      )
      .join("");
    const tag =
      `<span class="pcard-tag tag-${escapeHtml(t.main.variant)}${t.main.known ? "" : " unknown"}"` +
      ` title="${t.main.known ? escapeHtml(t.main.text) : "当前视角看不到这名玩家的身份"}">${escapeHtml(t.main.text)}</span>`;
    const stateEl = t.state
      ? `<span class="pcard-state st-${t.state}" title="${escapeHtml(t.stateTitle)}">${t.stateIcon}</span>`
      : "";
    const until = t.sleepUntil === null || t.sleepUntil === undefined ? "" : numText(t.sleepUntil);
    // Layout: [number]            [badges]
    //                 [avatar]
    //                 [name]
    //                 [identity tag]      [state]
    return (
      `<div class="${classes.join(" ")}" data-actor="${escapeHtml(t.id)}" data-state="${escapeHtml(t.state)}"` +
      ` data-until="${until}" role="button" tabindex="0" title="${escapeHtml(t.title)}"` +
      ` aria-label="${escapeHtml(t.name)}，${numText(t.seat)} 号">` +
      `<span class="pcard-num">${numText(t.seat)}</span>` +
      `<span class="pcard-badges">${badges}</span>${markEl}` +
      `<span class="pcard-avatar">${avatarImg(t.id, 40)}</span>` +
      `<span class="pcard-text"><span class="pcard-name">${escapeHtml(t.name)}</span>${tag}</span>` +
      `${stateEl}</div>`
    );
  }

  function renderBoard() {
    const s = state.session;
    const tiles = View.boardTiles(s, state.viewpoint, { clockNow: clockNow() });
    const html = tiles.map(tileHtml).join("");
    // Skip the DOM write when nothing changed (keeps hover / focus, saves work).
    setHtml(elParticipantsGrid, html, "lastBoardHtml");
    if (rosterUI && rosterUI.isOpen()) rosterUI.position();
  }

  /** "狼 2/4 · 神 4/4 · 民 3/4 · 警长 Grace": the god view's compact summary at the end of the bar. */
  function renderSummary() {
    if (!elBoardSummary) return;
    const sum = View.boardSummary(state.session, state.viewpoint);
    const html = sum
      ? `<span class="sum-label">对局状态</span>` +
        sum.items
          .map(
            (i) =>
              `<span class="sum-chip sum-${escapeHtml(i.variant)}" title="${escapeHtml(i.title)}">${escapeHtml(i.text)}</span>`
          )
          .join("")
      : "";
    setHtml(elBoardSummary, html, "lastSummaryHtml");
    elBoardSummary.hidden = !sum;
  }

  // Hover: the sleeping timer in the tooltip must be fresh (the board is not repainted every second).
  function refreshCardTitle(card) {
    if (!card || typeof card.getAttribute !== "function") return;
    if (card.getAttribute("data-state") !== "sleeping") return;
    const id = card.getAttribute("data-actor");
    const p = View.participantOf(state.session, id);
    if (!p) return;
    const tip = View.stateTitle("sleeping", p.sleep_until, clockNow());
    const t = View.tileModel(state.session, state.viewpoint, p, { clockNow: clockNow() });
    card.setAttribute("title", t.title);
    const icon = card.querySelector ? card.querySelector(".pcard-state") : null;
    if (icon && icon.setAttribute) icon.setAttribute("title", tip);
  }

  // ---------------------------------------------------------------------------
  // Rendering: channel bar (floor status, unread, tickets) — no "all" channel
  // ---------------------------------------------------------------------------

  function unreadBadge(n, cls = "channel-unread") {
    return n > 0
      ? `<span class="${cls}" title="${numText(n)} 条未读">${numText(n > 99 ? 99 : n)}${n > 99 ? "+" : ""}</span>`
      : "";
  }

  function channelItemHtml(ch, unread) {
    const s = state.session;
    const m = View.channelModel(s, state.viewpoint, ch);
    const sum = m.summary;
    const active = state.selectedChannel === ch.id;
    const n = unread.byChannel[ch.id] || 0;
    const isDm = m.kind === "dm";
    // A private chat is normally "free": the chip is only worth showing when it is muted / frozen.
    const showChip = Boolean(sum.icon) && !(isDm && sum.kind === "free");
    // v0.13 (F18): the channels that wait for the person who plays a seat (it holds the floor / has an open request)
    const attention = Boolean(m.attention);
    const tipText =
      `${m.title}${showChip ? `｜${sum.icon} ${sum.text}${sum.detail ? `（${sum.detail}）` : ""}` : ""}` +
      `${attention ? "｜✋ 待你操作" : ""}`;
    const hue = Avatar.HUES[Avatar.hashString(ch.id) % Avatar.HUES.length];
    const avatar = isDm
      ? avatarImg(m.avatarId, 40, "chan-avatar-img")
      : `<span class="chan-glyph" style="--gh:${hue}" aria-hidden="true">${escapeHtml(m.glyph)}</span>`;
    const chip = showChip
      ? `<span class="floor-chip floor-${escapeHtml(sum.kind)}" title="${escapeHtml(sum.detail || sum.text)}">` +
        `<span class="chip-icon">${sum.icon}</span><span class="chip-text">${escapeHtml(sum.text)}</span></span>`
      : "";
    const detail = sum.detail && showChip ? `<span class="channel-detail">${escapeHtml(sum.detail)}</span>` : "";
    const members = isDm ? "" : `<span class="channel-members">${numText(m.memberCount)} 人</span>`;
    const attn = attention
      ? `<span class="attn-chip" title="轮到你发言，或有一个请求等你作答">待你操作</span>`
      : "";
    // v0.13.1 (G18): the colour mark of the player a private chat is with - before its name (and, on the avatar rail
    // where names are hidden, on the avatar's corner)
    const mark = isDm ? markDot(m.avatarId) : "";
    return (
      `<div class="channel-item${isDm ? " is-dm" : " is-group"}${active ? " active" : ""}${n ? " has-unread" : ""}${
        attention ? " needs-action" : ""
      }"` +
      ` data-channel="${escapeHtml(ch.id)}" data-title="${escapeHtml(tipText)}" title="${escapeHtml(tipText)}" role="button" tabindex="0">` +
      `<span class="chan-avatar">${avatar}` +
      `${showChip ? `<span class="chan-ov ov-state">${sum.icon}</span>` : ""}${unreadBadge(n, "chan-ov ov-unread")}` +
      `${attention ? `<span class="chan-ov ov-attn" title="待你操作">✋</span>` : ""}` +
      `${mark ? `<span class="chan-ov ov-mark">${mark}</span>` : ""}</span>` +
      `<span class="chan-body"><span class="chan-row"><span class="channel-name">${mark}${escapeHtml(m.title)}</span>${unreadBadge(n)}</span>` +
      `<span class="channel-meta">${chip}${attn}${members}</span>${detail}</span></div>`
    );
  }

  function ticketHtml(t) {
    const s = state.session;
    const st = View.ticketStatus(t);
    const preview = String(t.preview || "");
    return (
      `<div class="ticket-item ticket-${st.cls}" data-ticket="${escapeHtml(t.id)}" data-channel="${escapeHtml(
        View.ticketChannelId(s, t)
      )}" title="点击定位到该玩家与主持人的对话">` +
      `<div class="ticket-row"><span class="ticket-id">${escapeHtml(t.id)}</span>` +
      `<span class="ticket-who">${escapeHtml(View.displayName(s, t.player_id))}</span>` +
      `<span class="ticket-status ts-${st.cls}">${escapeHtml(st.text)}</span></div>` +
      `<div class="ticket-preview">${escapeHtml(preview)}</div></div>`
    );
  }

  function renderChannels() {
    const s = state.session;
    const vp = state.viewpoint;
    ensureSelectedChannel();
    const unread = View.unreadCounts(s, vp, state.read);
    const { groups, dms } = View.channelGroups(s, vp);
    const tickets = View.visibleTickets(s, vp);

    const html = [];
    if (groups.length === 0 && dms.length === 0) {
      html.push(`<div class="empty-placeholder channels-empty">暂无频道<br/>对局启动后由主持人建立</div>`);
    }
    for (const ch of groups) html.push(channelItemHtml(ch, unread));

    // Challenge tickets are their own group, right below the channels (the private chats follow).
    // Wide: a titled list. Avatar rail: one icon with the number (opens the list as a popover).
    if (tickets.length) {
      const openCount = tickets.filter((t) => t.status !== "resolved").length;
      html.push(
        `<div class="channel-group-title tickets-title">质询工单 <span class="group-count">${numText(
          tickets.length
        )}</span>${openCount ? `<span class="ticket-open-count">${numText(openCount)} 待处理</span>` : ""}</div>`
      );
      for (const t of tickets) html.push(ticketHtml(t));
      html.push(
        `<button type="button" class="rail-tickets${openCount ? " has-open" : ""}" data-tickets-toggle` +
          ` title="质询工单：共 ${numText(tickets.length)} 条，${numText(openCount)} 条待处理">` +
          `<span class="rail-icon" aria-hidden="true">❓</span><span class="rail-count">${numText(tickets.length)}</span></button>`
      );
    }

    if (dms.length) {
      const selectedIsDm = dms.some((c) => c.id === state.selectedChannel);
      const open = state.dmGroupOpen || selectedIsDm;
      const dmUnread = dms.reduce((n, c) => n + (unread.byChannel[c.id] || 0), 0);
      html.push(
        `<div class="channel-group-title dm-title" data-dm-toggle role="button" tabindex="0" aria-expanded="${open}"` +
          ` title="${open ? "收起" : "展开"}私聊频道（${numText(dms.length)} 个）">` +
          `<span class="group-caret">${open ? "▼" : "▶"}</span><span class="group-icon" aria-hidden="true">✉</span>` +
          `<span class="group-label">私聊</span> <span class="group-count">${numText(dms.length)}</span>${
            open ? "" : unreadBadge(dmUnread)
          }</div>`
      );
      if (open) for (const ch of dms) html.push(channelItemHtml(ch, unread));
    }

    const out = html.join("");
    setHtml(elChannelsList, `${out}`, "lastChannelsHtml");
    renderTabbar(unread.total);
    renderTicketPop(tickets);
  }

  /** The ticket list as a popover (avatar-rail mode), kept in sync while it is open. */
  function renderTicketPop(tickets) {
    if (!elTicketPop) return;
    const list = tickets || View.visibleTickets(state.session, state.viewpoint);
    if (!state.ticketPopOpen || !list.length) {
      elTicketPop.hidden = true;
      return;
    }
    setHtml(
      elTicketPop,
      `<div class="ticket-pop-title">质询工单 <span class="group-count">${numText(list.length)}</span></div>` +
        list.map(ticketHtml).join(""),
      "lastTicketPopHtml"
    );
    elTicketPop.hidden = false;
  }

  function positionTicketPop(anchor) {
    if (!elTicketPop || !anchor || typeof anchor.getBoundingClientRect !== "function") return;
    const r = anchor.getBoundingClientRect();
    const vw = Number(window.innerWidth) || 1280;
    const vh = Number(window.innerHeight) || 800;
    const pw = Math.min(elTicketPop.offsetWidth || 280, vw - 16);
    elTicketPop.style.left = `${Math.round(Math.min(Math.max(8, r.right + 8), Math.max(8, vw - pw - 8)))}px`;
    elTicketPop.style.top = `${Math.round(Math.min(Math.max(8, r.top), Math.max(8, vh - 260)))}px`;
  }

  // ---------------------------------------------------------------------------
  // Rendering: phone tab bar
  // ---------------------------------------------------------------------------

  const TABS = [
    { id: "feed", icon: "💬", label: "信息流" },
    { id: "channels", icon: "#", label: "频道" },
    { id: "players", icon: "👥", label: "玩家" },
  ];

  function renderTabbar(unreadTotal) {
    if (!elTabbar) return;
    const total = unreadTotal === undefined ? View.unreadCounts(state.session, state.viewpoint, state.read).total : unreadTotal;
    const html = TABS.map((t) => {
      const on = state.phoneTab === t.id;
      const badge = t.id === "channels" && total > 0 ? `<span class="tab-badge">${numText(total > 99 ? 99 : total)}</span>` : "";
      return (
        `<button type="button" class="tab-btn${on ? " active" : ""}" role="tab" aria-selected="${on}" data-tab="${t.id}">` +
        `<span class="tab-icon" aria-hidden="true">${t.icon}</span><span class="tab-label">${t.label}</span>${badge}</button>`
      );
    }).join("");
    setHtml(elTabbar, html, "lastTabbarHtml");
  }

  function setPhoneTab(tab) {
    if (!TABS.some((t) => t.id === tab)) return;
    state.phoneTab = tab;
    if (elMain && elMain.setAttribute) elMain.setAttribute("data-phone-tab", tab);
    state.lastTabbarHtml = null;
    renderTabbar();
    if (tab === "feed") {
      state.autoScroll = true;
      scheduleRender({ ...Store.newDirty(false), feed: true });
    }
    if (rosterUI) rosterUI.close();
  }

  // ---------------------------------------------------------------------------
  // Rendering: feed (speech + request cards, one block per committed message)
  // ---------------------------------------------------------------------------

  function appendEllipsis(html) {
    const mark = `<span class="msg-trunc" title="发言超出字数上限，已被截断">…</span>`;
    return /<\/p>$/.test(html) ? html.replace(/<\/p>$/, `${mark}</p>`) : html + mark;
  }

  function messageHtml(m) {
    const s = state.session;
    const cacheKey = `${m.seq}\u0001${state.rosterSig}`;
    const cached = state.msgHtml.get(cacheKey);
    if (cached !== undefined) return cached;

    const info = View.messageKindInfo(m.kind);
    const sender = View.participantOf(s, m.from);
    // the live nickname (roster) wins over the name frozen into the message
    const name = sender ? sender.name : m.from === MOD || m.from === View.ENGINE_ID ? View.displayName(s, m.from) : m.from_name || m.from;
    const time = Core.formatClockTime(m.ts);
    let body = Core.renderFormattedText(m.content) || `<p class="fmt-p"></p>`;
    if (Store.isTruncated(m)) body = appendEllipsis(body);
    // "N3" in the text keeps its characters and gets the hover text "N3 Carol"
    body = View.annotateIdRefs(body, s);

    const classes = ["msg", `msg-kind-${info.cls}`];
    if (m.from === MOD) classes.push("msg-moderator");
    if (m.from === View.ENGINE_ID) classes.push("msg-engine");
    const label = info.label
      ? `<span class="msg-kind-badge kind-${info.cls}"${
          m.template_id ? ` title="模板：${escapeHtml(m.template_id)}"` : ""
        }>${escapeHtml(info.label)}</span>`
      : "";
    const ticket = m.ticket_id
      ? `<span class="msg-ticket" title="质询工单">#${escapeHtml(m.ticket_id)}</span>`
      : "";

    const html =
      `<article class="${classes.join(" ")}" data-seq="${numText(m.seq)}"` +
      ` data-from="${escapeHtml(m.from)}"${m.ticket_id ? ` data-ticket-msg="${escapeHtml(m.ticket_id)}"` : ""}>` +
      `<div class="msg-avatar">${avatarImg(m.from, 40, "msg-avatar-img")}</div>` +
      // (v0.13.1, G18: the speaker's colour mark sits before the name; it is part of rosterSig, so a changed mark repaints)
      `<div class="msg-main"><div class="msg-head">${markDot(m.from)}<span class="msg-name">${escapeHtml(name)}</span>` +
      `${label}${ticket}<span class="msg-time">${escapeHtml(time)}</span></div>` +
      `<div class="msg-body formatted-text">${body}</div></div></article>`;
    Core.boundedSet(state.msgHtml, cacheKey, html, MESSAGE_HTML_CACHE_MAX);
    return html;
  }

  const AUTO_CLOSE_LABELS = { unanimous: "全员一致才结束", deadline: "到时才结束" };

  /** Request card (E18 / E25): question, recipients with their (visible) answers, countdown, result. */
  function requestHtml(c) {
    const s = state.session;
    const kindLabel = c.shareAnswers ? "商议" : c.kind === "choice" ? "投票" : "问答";
    const icon = c.kind === "choice" ? "🗳" : "✍";
    const chips = [];
    if (c.sealed) chips.push(`<span class="req-chip sealed" title="密封：关闭前其他玩家看不到答案">🔒 密封</span>`);
    if (c.shareAnswers) chips.push(`<span class="req-chip share" title="收件人实时看到彼此的选择，关闭前可以改答">👥 组内可见</span>`);
    if (AUTO_CLOSE_LABELS[c.autoClose]) chips.push(`<span class="req-chip auto">${AUTO_CLOSE_LABELS[c.autoClose]}</span>`);
    const timer =
      c.deadlineClock !== null && c.deadlineClock !== undefined
        ? `<span class="req-timer" title="剩余时间（游戏时钟，暂停时冻结）">⏱ <span class="req-count" data-deadline="${numText(c.deadlineClock)}"></span></span>`
        : "";
    const pct = c.total > 0 ? Math.min(100, Math.round((c.answeredCount / c.total) * 100)) : 0;
    const rows = c.recipients
      .map((r) => {
        let ans;
        if (r.answer) {
          const revised = r.answer.changes > 0 ? `<span class="req-revised" title="改答 ${numText(r.answer.changes)} 次">↻${numText(r.answer.changes)}</span>` : "";
          const lock = c.sealed ? `<span class="req-lock" title="密封请求：只有上帝视角能看到答案">🔒</span>` : "";
          const text = escapeHtml(r.answer.label);
          ans = `<span class="req-ans answered${r.answer.defaulted ? " defaulted" : ""}"><span class="req-ans-label" title="${escapeHtml(
            r.answer.reason || r.answer.label
          )}">${r.answer.defaulted ? "（默认）" : ""}${text}</span>${revised}${lock}</span>`;
        } else if (r.hidden) {
          ans = `<span class="req-ans submitted" title="已作答（答案不公开）">✓ 已提交</span>`;
        } else {
          ans = `<span class="req-ans pending">…</span>`;
        }
        return (
          // (v0.13.1, G18: a recipient's colour mark sits before its name)
          `<li class="req-row${r.answered ? " answered" : ""}"><span class="req-who">${avatarImg(r.id, 20, "req-avatar")}${markDot(r.id)}` +
          `<span class="req-name">${escapeHtml(r.name)}</span></span>${ans}</li>`
        );
      })
      .join("");
    let result = "";
    if (c.status !== "open") {
      if (c.result) {
        const max = Math.max(1, ...c.result.tally.map((t) => t.count));
        const tally = c.result.tally
          .map(
            (t) =>
              `<li class="${t.leader ? "leader" : ""}"><span class="tally-label">${escapeHtml(t.label)}</span>` +
              `<span class="tally-bar"><span style="width:${Math.round((t.count / max) * 100)}%"></span></span>` +
              `<span class="tally-n">${numText(t.count)} 票</span></li>`
          )
          .join("");
        const notes = [];
        if (c.result.zeroCount > 0) notes.push(`其余 ${numText(c.result.zeroCount)} 项 0 票`);
        if (c.result.leaders.length) notes.push(`最高：${c.result.leaders.map(escapeHtml).join("、")}`);
        if (c.result.defaulted.length) notes.push(`默认答案：${c.result.defaulted.map(escapeHtml).join("、")}`);
        if (c.result.missing.length) notes.push(`未作答：${c.result.missing.map(escapeHtml).join("、")}`);
        result =
          `<div class="req-result"><div class="req-result-title">结果</div>` +
          `${tally ? `<ul class="req-tally">${tally}</ul>` : ""}` +
          `${notes.length ? `<div class="req-result-note">${notes.join(" · ")}</div>` : ""}</div>`;
      } else if (c.status === "closed") {
        result = `<div class="req-result"><div class="req-result-note">已关闭，共 ${numText(c.answeredCount)} 人作答${
          c.answersHidden ? "（密封请求的结果由主持人公布）" : ""
        }</div></div>`;
      }
    }
    const question = View.annotateIdRefs(Core.renderInlineFormatting(c.question), s);
    // v0.13 (F17): the person who plays a recipient answers right on the card
    const action = composerUI && c.action ? composerUI.requestActionHtml(c) : "";
    return (
      `<article class="req-card req-${escapeHtml(c.status)}${c.sealed ? " is-sealed" : ""}${c.shareAnswers ? " is-share" : ""}${
        action ? " has-action" : ""
      }" data-req="${escapeHtml(c.id)}">` +
      `<div class="req-head"><span class="req-icon" aria-hidden="true">${icon}</span><span class="req-kind">${kindLabel}请求</span>` +
      `<span class="req-status st-${escapeHtml(c.status)}">${escapeHtml(c.statusLabel)}</span>${chips.join("")}${timer}</div>` +
      `<div class="req-question">${question || "（没有问题文本）"}</div>` +
      `<div class="req-progress"><span class="req-progress-text">${numText(c.answeredCount)}/${numText(c.total)} 已作答</span>` +
      `<span class="req-bar"><span class="req-bar-fill" style="width:${pct}%"></span></span></div>` +
      `<ul class="req-rows">${rows}</ul>${action}${result}</article>`
    );
  }

  function appendMessageNode(html) {
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    const node = tpl.content && tpl.content.firstElementChild;
    if (node) {
      elFeedRecords.appendChild(node);
      if (composerUI) composerUI.restoreRequestDrafts(node);
    }
  }

  /**
   * A text the person typed into a request card (data-draft-key): when the card is drawn again while the
   * input has the focus, the caret must stay where it was.
   */
  function focusedDraft(node) {
    const a = document.activeElement;
    if (!a || !node || typeof node.contains !== "function" || !node.contains(a)) return null;
    const key = typeof a.getAttribute === "function" ? a.getAttribute("data-draft-key") : null;
    return key ? { key, start: a.selectionStart, end: a.selectionEnd } : null;
  }

  function refocusDraft(fresh, keep) {
    if (!keep || typeof fresh.querySelector !== "function") return;
    const box = fresh.querySelector(`[data-draft-key="${cssEscape(keep.key)}"]`);
    if (!box) return;
    if (typeof box.focus === "function") box.focus();
    if (typeof box.setSelectionRange === "function" && keep.start !== undefined && keep.start !== null) {
      box.setSelectionRange(keep.start, keep.end);
    }
  }

  /** Replace the i-th block in place (a request card changed); false when the DOM cannot do it. */
  function replaceRow(i, html) {
    const node = elFeedRecords.children && elFeedRecords.children[i];
    if (!node || typeof elFeedRecords.replaceChild !== "function") return false;
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    const fresh = tpl.content && tpl.content.firstElementChild;
    if (!fresh) return false;
    const keep = focusedDraft(node);
    elFeedRecords.replaceChild(fresh, node);
    if (composerUI) composerUI.restoreRequestDrafts(fresh);
    refocusDraft(fresh, keep);
    return true;
  }

  function emptyFeedHtml(s) {
    if (!state.selectedChannel) {
      return `<div class="empty-placeholder">当前暂无频道 — 点击「开始」后，主持人会建立频道并推进对局</div>`;
    }
    if (state.viewpoint !== View.GOD) {
      return `<div class="empty-placeholder">${escapeHtml(
        View.viewpointLabel(s, state.viewpoint)
      )} 在这个频道里还没有看到任何内容</div>`;
    }
    // (v0.13.1, G26: the thoughts are no longer "only in the log": the detail panel of a player opens them)
    return `<div class="empty-placeholder">这个频道里还没有发言 — 这里显示发言与请求卡片；玩家的思考内容在详情面板的「🧠 思考记录」里查看，工具调用只写入日志</div>`;
  }

  function tickCountdowns() {
    if (elFeedRecords && typeof elFeedRecords.querySelectorAll === "function") {
      const now = clockNow();
      for (const n of Array.from(elFeedRecords.querySelectorAll("[data-deadline]"))) {
        const d = Number(n.getAttribute("data-deadline"));
        n.textContent = Core.formatCountdown(d - now);
      }
      // v0.13.1 (G45): a vote that was picked but not submitted is flagged when the deadline is close
      for (const n of Array.from(elFeedRecords.querySelectorAll("[data-warn-at]"))) {
        const at = Number(n.getAttribute("data-warn-at"));
        if (n.classList && typeof n.classList.toggle === "function") n.classList.toggle("urgent", Number.isFinite(at) && now >= at);
      }
    }
  }

  function renderFeed(force = false) {
    const s = state.session;
    const vp = state.viewpoint;
    const sel = ensureSelectedChannel();
    if (elFeedChannelLabel) {
      elFeedChannelLabel.textContent = sel ? View.channelTitle(s, vp, sel) : "";
    }

    const clock = clockNow();
    const entries = sel ? View.feedEntries(s, vp, sel) : [];
    const rows = entries.map((e) => ({
      key: e.key,
      html: e.kind === "msg" ? messageHtml(e.ref) : requestHtml(View.requestCard(s, vp, e.ref, { clockNow: clock })),
    }));
    const key = `${s.session_id}|${vp}|${sel}|${state.rosterSig}`;
    const prev = state.feed.rows;

    const rebuild = () => {
      const keep = focusedDraft(elFeedRecords);
      elFeedRecords.innerHTML = rows.length ? rows.map((r) => r.html).join("") : emptyFeedHtml(s);
      if (composerUI) composerUI.restoreRequestDrafts(elFeedRecords);
      refocusDraft(elFeedRecords, keep);
    };

    if (force || state.feed.key !== key) {
      rebuild();
    } else {
      // Whole blocks arrive one at a time: append the new ones; a request card is repainted in place.
      let i = 0;
      let ok = true;
      for (; i < prev.length && i < rows.length; i++) {
        if (prev[i].key !== rows[i].key) break;
        if (prev[i].html !== rows[i].html && !replaceRow(i, rows[i].html)) {
          ok = false;
          break;
        }
      }
      if (!ok || i < prev.length) rebuild();
      else for (let j = i; j < rows.length; j++) appendMessageNode(rows[j].html);
      if (prev.length === 0 && rows.length > 0 && ok && i === 0) {
        // the empty-feed placeholder must go when the first block arrives
        rebuild();
      }
    }
    state.feed = { key, rows: rows.map((r) => ({ key: r.key, html: r.html })) };
    tickCountdowns();

    if (state.autoScroll) {
      elFeedList.scrollTop = elFeedList.scrollHeight;
      state.autoScrollTop = elFeedList.scrollTop; // the (clamped) position we scrolled to
    }
  }

  // ---------------------------------------------------------------------------
  // Detail panel (roster.js owns it; this only forwards)
  // ---------------------------------------------------------------------------

  function renderPopover() {
    if (rosterUI) rosterUI.render();
  }

  function openPopover(actorId) {
    if (rosterUI) rosterUI.open(actorId);
    renderBoard();
    renderModeratorChip(state.session);
  }

  function closePopover() {
    if (!rosterUI || !rosterUI.isOpen()) return;
    rosterUI.close();
    renderBoard();
    renderModeratorChip(state.session);
  }

  function togglePopover(actorId) {
    if (rosterUI && rosterUI.openId() === actorId) closePopover();
    else openPopover(actorId);
  }

  // ---------------------------------------------------------------------------
  // Render scheduling (coalesce bursts of SSE events: at most one paint per frame)
  // ---------------------------------------------------------------------------

  const renderQueue = { pending: false, run: null, dirty: Store.newDirty(false), force: false };

  function renderNow(dirty, force = false) {
    const d = dirty || Store.newDirty(true);
    state.rosterSig = computeRosterSig(state.session);
    if (d.topbar) renderTopbar();
    if (d.board) renderBoard();
    if (d.board || d.dashboard) renderSummary();
    if (d.channels || d.feed) {
      if (d.channels) renderChannels();
      else ensureSelectedChannel();
    }
    if (d.feed) renderFeed(force);
    if (d.popover) renderPopover();
    // v0.13: the input box follows the selected channel and the seat's state (it only touches what changed)
    if (composerUI) composerUI.render();
    // v0.13.1: an open colour palette follows the marks - and goes when the seat was given back / a replay began
    if (marksUI) marksUI.sync();
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

  /**
   * The viewpoint a session asks for: the seat a person plays is locked as the viewpoint (v0.13, F3); otherwise
   * what the user chose.  (Front end only: the server still sends the god view's data, a known limit.)
   */
  function effectiveViewpoint(session) {
    return View.normalizeViewpoint(session, View.takeoverSeat(session) || state.viewpointPref);
  }

  /** Switch the spectator viewpoint. Pure front-end: nothing is requested from the server. */
  function setViewpoint(id, persist = true) {
    if (View.takeoverSeat(state.session)) return; // locked while a seat is played
    const vp = View.normalizeViewpoint(state.session, id);
    state.viewpointPref = vp;
    state.viewpoint = vp;
    if (persist) saveViewpointPref(vp);
    state.selectedChannel = state.channelChosen
      ? View.normalizeSelectedChannel(state.session, vp, state.selectedChannel)
      : View.defaultChannelId(state.session, vp);
    markChannelRead(state.selectedChannel);
    state.autoScroll = true;
    state.feed.key = ""; // different message set: re-render the feed
    renderAll(true);
  }

  /**
   * The played seat changed (takeover.updated): the viewpoint becomes that seat, or - after leaving it - what
   * the user had chosen before.  A different message set: the feed is drawn again.
   */
  function syncViewpointLock() {
    const next = effectiveViewpoint(state.session);
    if (next === state.viewpoint) return;
    state.viewpoint = next;
    state.selectedChannel = state.channelChosen
      ? View.normalizeSelectedChannel(state.session, next, state.selectedChannel)
      : View.defaultChannelId(state.session, next);
    markChannelRead(state.selectedChannel);
    state.autoScroll = true;
    state.feed.key = "";
    if (rosterUI && rosterUI.isOpen()) rosterUI.render();
  }

  // ---------------------------------------------------------------------------
  // v0.13 (F36): replaying a saved game (replay.js).  While a replay runs, `state.session` IS the replayed session
  // (so every renderer shows it); the live game is kept aside in `state.replay.live` and still follows the SSE
  // stream, so leaving the replay shows the game exactly as it is now.
  // ---------------------------------------------------------------------------

  /** The live game's session (while a replay runs `state.session` is the replayed one). */
  const liveSession = () => (state.replay ? state.replay.live : state.session);

  /** Show `session` (a replay) instead of the live one; called again for every rebuild after a seek. */
  function enterReplay(session) {
    if (!state.replay) state.replay = { live: state.session, installing: false };
    state.replay.installing = true;
    try {
      state.sessionKey = null; // a different "game": the selected channel and the unread marks start afresh
      replaceSession(session);
    } finally {
      state.replay.installing = false;
    }
    state.clockSync = { clock: Number(session.clock) || 0, at: Date.now(), running: false }; // the replay sets the clock itself
  }

  /** Back to the live game. */
  function leaveReplay() {
    if (!state.replay) return;
    const live = state.replay.live;
    state.replay = null;
    state.sessionKey = null;
    replaceSession(live);
    syncClock(live);
    renderAll(true);
  }

  /** An event of the live stream while a replay is shown: it goes into the session kept aside, nothing is painted. */
  function handleLiveEventDuringReplay(payload) {
    const live = state.replay.live;
    const id = Store.eventIdOf(payload, NaN);
    const result = Store.applyEvent(live, payload, { clockNow: Number(live.clock) || 0 });
    if (result.replaced) {
      state.lastEventId = Number.isFinite(id) ? id : result.session.event_id;
      state.sseResumeId = state.lastEventId;
      state.replay.live = result.session;
    } else if (Number.isFinite(id) && id > 0) {
      if (id > state.lastEventId) state.lastEventId = id;
      if (id > state.sseResumeId) state.sseResumeId = id;
      if (id > live.event_id) live.event_id = id;
    }
    if (historyUI) historyUI.onSession();
    // the settings dialog: archive import needs the LIVE game paused, whatever replay is on screen
    if (archiveUI && (result.replaced || payload.type === "game.status" || payload.type === "session.updated")) archiveUI.onSession(state.replay.live);
    // v0.13.1: the error log belongs to the LIVE game: its lines keep coming (and its badge keeps counting) during a replay
    if (calllogUI) {
      if (result.llmCall) calllogUI.onEvent(result.llmCall);
      if (result.replaced) calllogUI.onSession(state.replay.live);
    }
  }

  /** Replace the whole session (snapshot, state fetch, control / import / load response). */
  function replaceSession(next) {
    if (state.replay && !state.replay.installing) {
      state.replay.live = next; // a snapshot / HTTP answer of the LIVE game during a replay: kept aside, not shown
      return;
    }
    const prevKey = state.sessionKey;
    const key = next.session_id || null;
    state.session = next;
    state.sessionKey = key;
    state.loaded = true;
    syncClock(next);
    // Whole-session replacement: never trust per-message HTML cached from the previous object.
    state.msgHtml.clear();
    if (key !== prevKey) {
      // A different game: forget selection and unread marks.
      state.selectedChannel = null;
      state.channelChosen = false;
      resetReadState(next);
    } else if (!state.read.baseline) {
      resetReadState(next);
    }
    state.viewpoint = effectiveViewpoint(next);
    state.selectedChannel = state.channelChosen
      ? View.normalizeSelectedChannel(next, state.viewpoint, state.selectedChannel)
      : View.defaultChannelId(next, state.viewpoint);
    if (state.selectedChannel && key !== prevKey) markChannelRead(state.selectedChannel);
    if (rosterUI && rosterUI.openId() && rosterUI.openId() !== MOD && !View.participantOf(next, rosterUI.openId())) {
      rosterUI.close();
    }
    // providers, bindings, the notice bar and the archive import concern the LIVE game (during a replay, `next` is the
    // replayed one); the roster shows what is on screen
    if (providerUI) providerUI.onSession(liveSession());
    if (archiveUI) archiveUI.onSession(liveSession());
    if (calllogUI) calllogUI.onSession(liveSession()); // v0.13.1: the error log is the live game's, also while a replay is on screen
    if (rosterUI) rosterUI.onSession(next);
    if (thoughtsUI) thoughtsUI.onSession(next); // v0.13.1: the open thinking record re-checks "may I look" (a seat taken over, a replay) and follows a new game
    // The feed compares what it already shows (view key + blocks), so an identical
    // snapshot after a reconnect does not rebuild the list or move the scroll position.
    scheduleRender(Store.newDirty(true));
  }

  /** A whole session from an HTTP response; ignored when the SSE stream is already ahead of it. */
  function adoptHttpSession(raw, opts = {}) {
    if (!raw || typeof raw !== "object") return false;
    const incoming = Store.normalizeSession(raw);
    const current = liveSession(); // (during a replay the live game is the one kept aside)
    if (!opts.force && !Store.shouldAdoptHttpSession(current, incoming, state.lastEventId)) {
      return false;
    }
    const differentGame = incoming.session_id !== current.session_id;
    if (differentGame) state.lastEventId = incoming.event_id > 0 ? incoming.event_id : 0;
    else if (incoming.event_id > state.lastEventId) state.lastEventId = incoming.event_id;
    replaceSession(incoming);
    return true;
  }

  const PROVIDER_RELEVANT = new Set(["session.updated", "game.status"]);

  /** roster.js / the store change names and avatars through the same code path as roster.updated. */
  function patchParticipants(list) {
    // the moderator is not a participant: its entry travels as ``data.moderator`` (exactly like the SSE event)
    const moderator = list.find((x) => x && x.id === MOD);
    const data = { participants: list.filter((x) => !(x && x.id === MOD)) };
    if (moderator) data.moderator = moderator;
    const res = Store.applyEvent(state.session, { type: "roster.updated", data });
    scheduleRender(res.dirty);
  }

  /** Apply one SSE v2 event {id, type, data}. */
  function handleEvent(payload) {
    if (!payload || typeof payload !== "object" || !payload.type) return;
    if (Store.isStaleEvent(state.lastEventId, payload)) return; // replayed after a reconnect
    if (payload.type === "archive.progress") {
      // v0.13: an export / import job is not part of any game: handled the same with or without a replay on screen
      const archiveResult = Store.applyEvent(state.session, payload, null);
      const archiveEventId = Store.eventIdOf(payload, NaN);
      if (Number.isFinite(archiveEventId) && archiveEventId > 0) {
        if (archiveEventId > state.lastEventId) state.lastEventId = archiveEventId;
        if (archiveEventId > state.sseResumeId) state.sseResumeId = archiveEventId;
      }
      if (archiveUI && archiveResult.archiveEvent) archiveUI.onEvent(archiveResult.archiveEvent);
      return;
    }
    if (state.replay) {
      handleLiveEventDuringReplay(payload); // a saved game is on screen: the live game follows in the background
      return;
    }
    const id = Store.eventIdOf(payload, NaN);
    const before = clockNow();
    const result = Store.applyEvent(state.session, payload, { clockNow: before });

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
    // the game clock stops while the game is not running (D18), and - v0.13.1 (G2) - while a soft pause holds it
    // although the status stays "running": freeze / resume our estimate (`clock_running`, judged by the status
    // when a server does not send it)
    const s = state.session;
    if (payload.type === "session.updated" && payload.data && payload.data.clock !== undefined) {
      syncClock(s);
    } else if (View.clockRunning(s) !== state.clockSync.running) {
      state.clockSync = { clock: before, at: Date.now(), running: View.clockRunning(s) };
    }
    if (result.takeoverChanged) syncViewpointLock();
    // v0.13.1: whether the thoughts of another agent may be read depends on the played seat and on the game being over
    if (thoughtsUI && (result.takeoverChanged || payload.type === "game.status")) thoughtsUI.onSession(state.session);
    if (result.llmCall && calllogUI) calllogUI.onEvent(result.llmCall); // one more line of the error log (and maybe a red badge)
    if (result.toast) showToast(result.toast);
    if (result.message) onMessageSeen(result.message);
    if (result.flash) flashActor(result.flash);
    if (result.budgetEvent === "warning") showToast("⚠ 预算已用到上限的 80%");
    if (result.budgetEvent === "reached") showToast("⛔ 已达到预算上限");
    if (result.reviewEvent) {
      const ev = result.reviewEvent;
      if (ev.state === "done" && rosterUI) rosterUI.onReviewDone(ev.id);
      if (ev.state === "error") showToast(`复盘失败（${View.displayName(s, ev.id)}）：${ev.message || "未知错误"}`);
    }
    if (PROVIDER_RELEVANT.has(payload.type)) {
      if (providerUI) providerUI.onSession(state.session);
      if (archiveUI) archiveUI.onSession(state.session);
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
    state.channelChosen = true;
    markChannelRead(next);
    state.autoScroll = true;
    state.ticketPopOpen = false;
    renderNow({ ...Store.newDirty(false), channels: true, feed: true }, true);
    if (isPhone() && state.phoneTab !== "feed") setPhoneTab("feed");
  }

  function locateTicket(ticketId, channelId) {
    state.ticketPopOpen = false;
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

  const cssEscape = (value) =>
    typeof CSS !== "undefined" && CSS && typeof CSS.escape === "function"
      ? CSS.escape(String(value))
      : String(value).replace(/["\\]/g, "\\$&");

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
  bindPress(elChannelsList, "[data-tickets-toggle]", "data-tickets-toggle", (node) => {
    state.ticketPopOpen = !state.ticketPopOpen;
    state.lastTicketPopHtml = null;
    renderTicketPop();
    if (state.ticketPopOpen) positionTicketPop(node);
  });
  if (elTicketPop) {
    elTicketPop.addEventListener("click", (evt) => {
      const node = closestFrom(evt.target, ".ticket-item[data-ticket]");
      if (node) locateTicket(node.getAttribute("data-ticket"), node.getAttribute("data-channel"));
    });
  }

  // v0.13.1 (G16): the colour dot in a card's corner (only while a seat is played) opens the palette - never the panel
  bindPress(elParticipantsGrid, "[data-mark-toggle]", "data-mark-toggle", (node) => {
    if (marksUI) marksUI.togglePalette(node.getAttribute("data-mark-toggle"), node);
  });
  // a player card (or the moderator chip) opens the detail panel / drawer
  bindPress(elParticipantsGrid, ".pcard[data-actor]", "data-actor", (node, evt) => {
    if (closestFrom(evt && evt.target, "[data-mark-toggle]")) return; // (the dot is handled above)
    togglePopover(node.getAttribute("data-actor"));
  });
  bindPress(elTopbarStatus, "#mod-chip", "data-actor", (node) =>
    togglePopover(node.getAttribute("data-actor") || MOD)
  );
  if (elParticipantsGrid) {
    elParticipantsGrid.addEventListener("keydown", (evt) => {
      if (!evt || (evt.key !== "Enter" && evt.key !== " ")) return;
      const dot = closestFrom(evt.target, "[data-mark-toggle]");
      if (dot) {
        if (typeof evt.preventDefault === "function") evt.preventDefault();
        if (marksUI) marksUI.togglePalette(dot.getAttribute("data-mark-toggle"), dot);
        return;
      }
      const card = closestFrom(evt.target, ".pcard[data-actor]");
      if (!card) return;
      if (typeof evt.preventDefault === "function") evt.preventDefault();
      togglePopover(card.getAttribute("data-actor"));
    });
    elParticipantsGrid.addEventListener("mouseover", (evt) => {
      refreshCardTitle(closestFrom(evt.target, ".pcard[data-actor]"));
    });
  }

  // phone: the bottom tab bar
  if (elTabbar) {
    elTabbar.addEventListener("click", (evt) => {
      const node = closestFrom(evt.target, "[data-tab]");
      if (node) setPhoneTab(node.getAttribute("data-tab"));
    });
  }

  // touch: a long press on a channel (or a rail avatar) shows its name
  let tipTimer = null;
  let tipHideTimer = null;
  function hideTip() {
    if (tipTimer !== null) window.clearTimeout(tipTimer);
    tipTimer = null;
    if (elTouchTip) elTouchTip.hidden = true;
  }
  if (elChannelsList && elTouchTip) {
    elChannelsList.addEventListener("pointerdown", (evt) => {
      if (evt.pointerType === "mouse") return;
      const item = closestFrom(evt.target, ".channel-item[data-title]");
      if (!item) return;
      hideTip();
      tipTimer = window.setTimeout(() => {
        tipTimer = null;
        elTouchTip.textContent = item.getAttribute("data-title") || "";
        elTouchTip.hidden = false;
        if (typeof item.getBoundingClientRect === "function") {
          const r = item.getBoundingClientRect();
          const vw = Number(window.innerWidth) || 390;
          elTouchTip.style.left = `${Math.round(Math.min(r.right + 8, Math.max(8, vw - (elTouchTip.offsetWidth || 160) - 8)))}px`;
          elTouchTip.style.top = `${Math.round(Math.max(8, r.top + 4))}px`;
        }
        if (tipHideTimer !== null) window.clearTimeout(tipHideTimer);
        tipHideTimer = window.setTimeout(hideTip, 2200);
      }, LONG_PRESS_MS);
    });
    for (const type of ["pointerup", "pointercancel", "pointerleave"]) {
      elChannelsList.addEventListener(type, () => {
        if (tipTimer !== null) {
          window.clearTimeout(tipTimer);
          tipTimer = null;
        }
      });
    }
  }

  // Click outside the ticket popover closes it; the detail panel / roster menu close themselves (roster.js).
  if (typeof document.addEventListener === "function") {
    // the event path is computed at dispatch time: a handler that repaints (detaches) the clicked
    // button must not turn an inside click into an "outside" one
    const inPath = (evt, selector) => {
      const path = evt && typeof evt.composedPath === "function" ? evt.composedPath() : [];
      if (path.length) return path.some((n) => n && typeof n.matches === "function" && n.matches(selector));
      return Boolean(closestFrom(evt && evt.target, selector));
    };
    document.addEventListener("click", (evt) => {
      if (state.ticketPopOpen && !inPath(evt, "#ticket-pop") && !inPath(evt, "[data-tickets-toggle]")) {
        state.ticketPopOpen = false;
        renderTicketPop();
      }
      if (elMoreMenu && !elMoreMenu.hidden && !inPath(evt, "#more-menu") && !inPath(evt, "#btn-more")) {
        setMoreMenu(false);
      }
    });
    document.addEventListener("keydown", (evt) => {
      if (evt && evt.key === "Escape") {
        if (elMoreMenu && !elMoreMenu.hidden) setMoreMenu(false);
        if (state.ticketPopOpen) {
          state.ticketPopOpen = false;
          renderTicketPop();
        }
      }
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
  // Phone: the "⋯" menu takes the secondary buttons of the top bar
  // ---------------------------------------------------------------------------

  // Real elements are moved (so their listeners keep working) and put back when the screen widens.
  const RELOCATIONS = [
    { id: "btn-open-settings-modal", slot: "menu" },
    { id: "btn-theme", slot: "menu" },
    // v0.13: the whole bar [视角▾][切换][接管] moves; the takeover bar (#takeover-bar) stays in the first row
    { id: "viewpoint-bar", slot: "menu" },
    { id: "btn-open-budget-modal", slot: "menu" },
    // v0.13.1 (G32): the error log button moves too; its unread badge then shows on the ⋯ button (calllog.js)
    { id: "btn-open-calllog", slot: "menu" },
    { id: "btn-new", slot: "menu" },
    { id: "btn-open-history-modal", slot: "menu" },
    { id: "btn-open-bindings-modal", slot: "menu" },
    { id: "btn-open-provider-modal", slot: "menu" },
    { id: "usage-telemetry-pill", slot: "status" },
  ];
  let relocated = false;

  function relocateTopbar(phone) {
    if (phone === relocated) return;
    for (const r of RELOCATIONS) {
      const node = $(r.id);
      if (!node || !node.parentNode) continue;
      if (phone) {
        if (!r.marker) {
          r.marker = typeof document.createComment === "function" ? document.createComment(`home:${r.id}`) : document.createElement("span");
          node.parentNode.insertBefore(r.marker, node);
        }
        const target = r.slot === "menu" ? elMoreMenuList : elTopbarStatus;
        if (!target) continue;
        if (r.slot === "status" && elModChip && elModChip.parentNode === target) target.insertBefore(node, elModChip);
        else target.appendChild(node);
      } else if (r.marker && r.marker.parentNode) {
        r.marker.parentNode.insertBefore(node, r.marker);
      }
    }
    relocated = phone;
    if (!phone) setMoreMenu(false);
  }

  function setMoreMenu(open) {
    if (!elMoreMenu) return;
    elMoreMenu.hidden = !open;
    if (elBtnMore && elBtnMore.setAttribute) elBtnMore.setAttribute("aria-expanded", open ? "true" : "false");
  }

  if (elBtnMore) elBtnMore.addEventListener("click", () => setMoreMenu(Boolean(elMoreMenu && elMoreMenu.hidden)));
  if (elMoreMenu) {
    elMoreMenu.addEventListener("click", (evt) => {
      // an action closes the menu; the select / checkbox-like rows keep it open until the user is done
      const btn = closestFrom(evt.target, "button");
      if (btn && btn.id !== "btn-theme") setMoreMenu(false);
    });
  }

  function onPlatformChange(info) {
    state.platform = info;
    relocateTopbar(info.platform === "phone");
    if (layoutUI) layoutUI.apply(); // the avatar rail follows the layout
    if (rosterUI) rosterUI.render();
    renderTabbar();
    hideTip();
    state.ticketPopOpen = false;
    renderTicketPop();
  }

  // ---------------------------------------------------------------------------
  // Controls
  // ---------------------------------------------------------------------------

  elBtnTheme.addEventListener("click", () => {
    const htmlEl = document.documentElement;
    const cur = htmlEl.getAttribute("data-theme") || "light";
    const next = cur === "light" ? "dark" : "light";
    htmlEl.setAttribute("data-theme", next);
  });

  // (v0.13: the drop-down only chooses; "切换" / "接管" are wired in takeover.js)

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
      state.selectedChannel = null;
      state.channelChosen = false;
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
    control("api/control/pause", "已暂停：所有 Agent 已休眠，计时冻结")
  );

  // ---------------------------------------------------------------------------
  // Usage pill + adopting a session that an HTTP answer carries (v0.13: the single-game JSON import is gone)
  // ---------------------------------------------------------------------------
  if (elUsagePill) {
    // Cost breakdown lives with the model bindings.
    elUsagePill.addEventListener("click", () => {
      if (providerUI) providerUI.openBindings();
    });
  }

  function handleSessionLoaded(session) {
    adoptHttpSession(session, { force: true });
    state.selectedChannel = null;
    state.channelChosen = false;
    renderAll(true);
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
      // (v0.13: the providers, the bindings and the "未绑定模型" light concern the LIVE game, also while a replay is on screen)
      getSession: () => liveSession(),
      // Providers/bindings changed: the detail panel shows the model of an agent.
      onChange: () => scheduleRender({ ...Store.newDirty(false), popover: true }),
    });
  }

  function bindingText(actorId) {
    const b = providerUI ? providerUI.getBindingsFor(actorId) : (state.session.model_bindings || {})[actorId];
    if (!b) return "未绑定";
    const providers = providerUI ? providerUI.getProviders() : [];
    if (providers.length === 0) return `${b.model_id || "未绑定"} · ${b.reasoning_depth || "high"}`;
    return Core.bindingSummary(b, providers);
  }

  // v0.13.1 (G16-G20): the colour marks on the players (marks.js).  Created before the detail panel: the palette of
  // the panel's head comes from here (ctx.markHeadHtml below), and this module's document listeners (Escape, a click
  // outside the palette) must answer first.
  if (window.ArenaMarks) {
    marksUI = window.ArenaMarks.create({
      View,
      getSession: () => state.session,
      getViewpoint: () => state.viewpoint,
      isReplaying: () => Boolean(state.replay),
      // a mark was set / cleared here, or in another tab: every place that shows one is drawn again (the feed's cache
      // key holds the marks, see computeRosterSig)
      onChange: () => scheduleRender({ ...Store.newDirty(false), board: true, channels: true, feed: true, popover: true }),
    });
  }

  if (window.ArenaRosterUI) {
    rosterUI = window.ArenaRosterUI.create({
      api,
      Core,
      Avatar,
      View,
      showToast,
      getSession: () => state.session,
      getViewpoint: () => state.viewpoint,
      getClockNow: clockNow,
      bindingText,
      openBindings: (id) => {
        if (providerUI) providerUI.openBindings(id);
      },
      setViewpoint: (id) => setViewpoint(id),
      // v0.13 (F3): while a seat is played the viewpoint is locked: "以 X 的视角观看" is disabled
      lockedSeat: () => View.takeoverSeat(state.session),
      // v0.13.1 (G16, G18): the colour palette (or the read-only dot) in the head of the detail panel
      markHeadHtml: (id) => (marksUI ? marksUI.headHtml(id) : ""),
      // v0.13.1 (G21, G25): "🧠 思考记录" opens the window of an agent; while a seat is played the others' records are closed
      // (the reason is the button's tooltip and a line of text)
      openThoughts: (id) => {
        if (thoughtsUI) thoughtsUI.open(id);
      },
      thoughtsBlocked: (id) => (window.ArenaThoughtsUI ? window.ArenaThoughtsUI.blockedReason(state.session, id, { replaying: Boolean(state.replay) }) : ""),
      // Escape closes a window that is open before it closes the panel behind it
      isWindowOpen: () => Boolean((thoughtsUI && thoughtsUI.isOpen()) || (calllogUI && calllogUI.isOpen())),
      patchParticipants,
      onChange: () => scheduleRender({ ...Store.newDirty(false), board: true, topbar: true, channels: true, feed: true }),
    });
  }

  // v0.13.1: the two floating windows below share one stack: the one opened last is on top (on the screen, and for the Escape key, which
  // closes that one only - the other, and the detail panel behind them, stay)
  const windowStack = typeof Core.createWindowStack === "function" ? Core.createWindowStack() : null; // (null: each window keeps a stack of its own)

  // v0.13.1 (G21-G26): the thinking record window (thoughts.js) - read on demand from GET /api/agents/{id}/thoughts
  if (window.ArenaThoughtsUI) {
    thoughtsUI = window.ArenaThoughtsUI.create({
      api,
      Core,
      Avatar,
      View,
      showToast,
      windows: windowStack,
      getSession: () => state.session,
      isReplaying: () => Boolean(state.replay), // (the record is the live game's: while a replay is shown it is closed)
    });
  }

  // v0.13.1 (G32-G36): the error log window and the unread badge of the top bar (calllog.js).  The log is the LIVE game's.
  if (window.ArenaCallLogUI) {
    calllogUI = window.ArenaCallLogUI.create({
      api,
      Core,
      Store,
      View,
      showToast,
      windows: windowStack,
      getSession: () => liveSession(),
    });
  }

  /** The shared modal of roster.js (confirmations, forms); the browser's confirm() when it is missing. */
  function openDialog(options) {
    if (rosterUI && typeof rosterUI.openDialog === "function") return rosterUI.openDialog(options);
    const ok = typeof window.confirm === "function" ? window.confirm(String((options && options.title) || "确认")) : false;
    return Promise.resolve({ ok, values: {} });
  }

  // v0.13 (F1-F5): the viewpoint bar [视角▾][切换][接管], the takeover bar, the reshuffle reminder
  if (window.ArenaTakeoverUI) {
    takeoverUI = window.ArenaTakeoverUI.create({
      api,
      Core,
      View,
      showToast,
      getSession: () => state.session,
      getViewpoint: () => state.viewpoint,
      setViewpoint: (id) => setViewpoint(id),
      openDialog,
      adoptSession: (raw, opts) => adoptHttpSession(raw, opts),
      refreshState: () => reloadState(),
      isReplaying: () => Boolean(state.replay),
    });
  }

  // v0.13 (F11-F17): the input box, the floor, abilities and the answers of request cards
  if (window.ArenaComposerUI) {
    composerUI = window.ArenaComposerUI.create({
      api,
      Core,
      View,
      showToast,
      getSession: () => state.session,
      getViewpoint: () => state.viewpoint,
      getSelectedChannel: () => state.selectedChannel,
      getClockNow: clockNow,
      getFeedNode: () => elFeedRecords,
      openDialog,
      onSent: () => {
        state.autoScroll = true; // the player's own message scrolls the feed to the bottom
      },
      onChange: () => scheduleRender({ ...Store.newDirty(false), topbar: true, channels: true, feed: true }),
    });
  }

  // v0.13 (F34, F35): the history of games - list, delete, continue, replay
  if (window.ArenaHistoryUI) {
    historyUI = window.ArenaHistoryUI.create({
      // v0.13.1 (G19): a deleted game takes the colour marks (they live in this browser) along
      api: marksUI
        ? {
            ...api,
            del: async (path, ...rest) => {
              const out = await api.del(path, ...rest);
              const gone = /^\/?(?:[^?#]*\/)?api\/games\/([^/?#]+)$/.exec(String(path));
              if (gone) marksUI.forget(decodeURIComponent(gone[1]));
              return out;
            },
          }
        : api,
      Core,
      showToast,
      getSession: () => liveSession(),
      openDialog,
      isReplaying: () => Boolean(state.replay),
      onLoaded: (session) => handleSessionLoaded(session),
      onReplay: (data, game) => {
        if (replayUI) replayUI.start(data, game);
      },
    });
  }

  // v0.13 (F36): the replay player (it works on a session of its own, see enterReplay / leaveReplay)
  if (window.ArenaReplayUI) {
    replayUI = window.ArenaReplayUI.create({
      Core,
      Store,
      showToast,
      enter: enterReplay,
      leave: () => leaveReplay(),
      render: (dirty) => scheduleRender(dirty),
      renderAll: () => renderAll(true),
      setClock: (gt) => {
        state.clockSync = { clock: Number(gt) || 0, at: Date.now(), running: false };
      },
      onMessage: onMessageSeen,
      onFlash: flashActor,
    });
  }

  if (window.ArenaBudgetUI) {
    budgetUI = window.ArenaBudgetUI.create({
      api,
      Core,
      View,
      showToast,
      getSession: () => liveSession(), // the limit is the live game's, also while a replay is on screen
      onBudget: (budget) => {
        liveSession().budget = budget;
        scheduleRender({ ...Store.newDirty(false), topbar: true });
      },
    });
  }

  if (window.ArenaArchiveUI) {
    archiveUI = window.ArenaArchiveUI.create({
      api,
      Core,
      showToast,
      getSession: () => liveSession(), // (a replay on screen must not hide that the live game runs: import needs it paused)
      getToken,
      // XHR uploads send the token as a header; a download link cannot, so it carries ?token= (the server accepts it there)
      buildUrl: (path, options) => {
        const url = Core.joinUrl(URL_PREFIX, path);
        const token = options && options.token ? getToken() : "";
        return token ? `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}` : url;
      },
      requestToken: requestAuthToken,
      reload: () => {
        if (typeof window.location.reload === "function") window.location.reload();
      },
      openDialog,
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
        // v0.13 (F24): "old v0.12.1 data found" (the path is only sent to a client on the server's own machine)
        if (archiveUI) archiveUI.onState(data.legacy_data);
        renderAll(true);
      }
      return true;
    } catch (err) {
      console.error("State fetch error", err);
      showToast(`加载对局状态失败：${err.message}`);
      return false;
    }
  }

  // 1 Hz: request countdowns and the sleep timers in the open detail panel (the game clock is an estimate
  // between events; it stops while the game is paused).
  function startTicker() {
    if (typeof window.setInterval !== "function") return;
    window.setInterval(() => {
      tickCountdowns();
      if (rosterUI && rosterUI.isOpen()) rosterUI.render({ live: true });
      // the seconds left of the played seat's floor (the hint under the input box)
      if (composerUI && View.takeoverSeat(state.session)) composerUI.render();
    }, 1000);
  }

  async function init() {
    loadViewpointPref();
    // First paint of the (empty) shell so the layout never looks broken while loading.
    renderAll(true);
    // The sprite library is optional: without it every avatar is a coloured circle with the seat number.
    Avatar.onChange(() => {
      state.avatarEpoch += 1;
      state.lastBoardHtml = null;
      state.lastModHtml = null;
      state.lastChannelsHtml = null;
      state.feed.key = "";
      state.msgHtml.clear();
      scheduleRender(Store.newDirty(true), true);
    });
    Avatar.loadParts((...args) => window.fetch(...args), Core.joinUrl(URL_PREFIX, "static/avatar/parts.json"));
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
    if (rosterUI) rosterUI.load().catch(() => {});
    if (!SSE_DISABLED) {
      window.setTimeout(connectSSE, 300);
    }
    startTicker();
  }

  // Platform detection: <html data-platform> is already set by index.html's head script; start tracking
  // rotations / resizes and move the phone-only pieces.
  if (Platform) {
    platformCtl = Platform.start({ window, document, onChange: onPlatformChange });
    if (platformCtl && platformCtl.info()) onPlatformChange(platformCtl.info());
  }

  // Small surface for tests and for poking at the UI from the console.
  window.ArenaApp = {
    state,
    handleEvent,
    adoptHttpSession,
    setViewpoint,
    selectChannel,
    setPhoneTab,
    openPopover,
    closePopover,
    togglePopover,
    renderAll,
    renderNow,
    patchParticipants,
    platform: () => (platformCtl ? platformCtl.info() : null),
    roster: () => rosterUI,
    thoughts: () => thoughtsUI,
    calllog: () => calllogUI,
    budget: () => budgetUI,
    archive: () => archiveUI,
    takeover: () => takeoverUI,
    composer: () => composerUI,
    marks: () => marksUI,
    history: () => historyUI,
    replay: () => replayUI,
    liveSession,
  };

  init();
})();
