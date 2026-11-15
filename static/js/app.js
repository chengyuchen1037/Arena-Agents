/* ==========================================================================
   Arena-Agents v0.11 Frontend Controller
   (Executable Markdown Scenarios · Game-Agnostic Runtime)
   Pure helpers live in core.js (window.ArenaCore).
   ========================================================================== */

(() => {
  const Core = window.ArenaCore;
  if (!Core) {
    throw new Error("core.js 未加载：请确认 static/js/core.js 可访问");
  }
  const {
    escapeHtml,
    numText,
    safeNum,
    stripMarkdownForPreview,
    renderFormattedText,
    renderFormattedBlocks,
    boundedSet,
  } = Core;

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
  let providerUI = null; // v0.10 provider/binding/session-list UI (providers.js)
  let layoutUI = null; // v0.11 collapse/resize/card-size UI (layout.js)
  let workspaceUI = null; // v0.11 workspace (工作文件夹) UI (workspace.js)
  const MAX_EXPAND_ENTRIES = 5000;

  const state = {
    session: null,
    selectedChannel: "all", // "engine" | "all" | channel.id
    showToolCalls: true,
    level1Expanded: new Map(), // action_id -> boolean (Level 1 action card, default true)
    level2Expanded: new Map(), // record_id -> boolean (Level 2 subitem: message default true, thought/tool default false)
    autoScroll: true,
    sse: null,
    sseAttempt: 0,
    sseTimer: null,
    sseConnected: false,
    lastChannelsSig: "",
    lastRecordsSig: "",
    lastBoardHtml: null,
    appVersion: "", // application version from /api/state (not the restored session's version)
    lastDashboardSig: null,
    sessionKey: null,
    draftNodes: new Map(), // draft key -> { el, sig }
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

  // DOM Elements
  const elAppTitle = document.getElementById("app-title");
  const elAppVersion = document.getElementById("app-version");
  const elScenarioName = document.getElementById("scenario-name");
  const elBtnTheme = document.getElementById("btn-theme");
  const elStatusDot = document.getElementById("active-status-dot");
  const elStatusText = document.getElementById("active-status-text");
  const elPhasePill = document.getElementById("phase-pill");

  const elSelectExecDepth = document.getElementById("select-exec-depth");
  const elUsagePill = document.getElementById("usage-telemetry-pill");
  const elUsageTokensText = document.getElementById("usage-tokens-text");
  const elUsageCostText = document.getElementById("usage-cost-text");

  const elModeratorStatusPill = document.getElementById("moderator-status-pill");
  const elModeratorStatusText = document.getElementById("moderator-status-text");

  const elBtnNew = document.getElementById("btn-new");
  const elBtnSave = document.getElementById("btn-save");
  const elBtnStart = document.getElementById("btn-start");
  const elBtnPause = document.getElementById("btn-pause");
  const elBtnResume = document.getElementById("btn-resume");
  const elBtnForcePause = document.getElementById("btn-force-pause");
  const elBtnShuffle = document.getElementById("btn-shuffle-identities");

  const elParticipantsGrid = document.getElementById("participants-grid");
  const elChannelsList = document.getElementById("channels-list");
  const elFeedChannelLabel = document.getElementById("feed-channel-label");
  const elChkShowTools = document.getElementById("chk-show-tools");
  const elBtnResetDefault = document.getElementById("btn-reset-default");
  const elBtnExpandAll = document.getElementById("btn-expand-all");
  const elBtnCollapseAll = document.getElementById("btn-collapse-all");
  const elFeedList = document.getElementById("feed-list");
  const elFeedRecordsContainer = document.getElementById("feed-records-container");
  const elFeedStreamingContainer = document.getElementById("feed-streaming-container");

  const elFactionsList = document.getElementById("factions-list");
  const elTagStatsList = document.getElementById("tag-stats-list");
  const elToastContainer = document.getElementById("toast-container");

  const elConnBanner = document.getElementById("conn-banner");
  const elTokenModal = document.getElementById("token-modal");
  const elTokenForm = document.getElementById("token-form");
  const elTokenInput = document.getElementById("input-api-token");
  const elTokenMessage = document.getElementById("token-modal-message");
  const elBtnCancelToken = document.getElementById("btn-cancel-token");

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

  function renderToolCallContent(rec) {
    if (!rec || rec.kind !== "tool_call" || !rec.tool_name) {
      return renderFormattedText(rec ? rec.content : "");
    }
    const argsStr = JSON.stringify(rec.tool_args || {}, null, 2);
    const resRaw = (rec.tool_result || "").trim();
    let resHtml = "";
    if (resRaw.startsWith("{") || resRaw.startsWith("[")) {
      try {
        const parsed = JSON.parse(resRaw);
        resHtml = `<pre class="fmt-pre"><code>${escapeHtml(
          JSON.stringify(parsed, null, 2)
        )}</code></pre>`;
      } catch {
        resHtml = renderFormattedText(resRaw);
      }
    } else {
      resHtml = renderFormattedText(resRaw);
    }
    return `
      <div class="tool-detail-grid">
        <div class="tool-detail-row">
          <span class="tool-detail-label">调用参数</span>
          <pre class="fmt-pre tool-args-pre"><code>${escapeHtml(argsStr)}</code></pre>
        </div>
        ${
          resRaw
            ? `<div class="tool-detail-row">
                <span class="tool-detail-label">返回结果</span>
                <div class="tool-detail-result">${resHtml}</div>
              </div>`
            : ""
        }
      </div>
    `;
  }

  function getBadgeVariantClass(badge) {
    if (!badge) return "";
    const b = String(badge);
    if (b.includes("战略") || b.includes("规划") || b.includes("复盘")) {
      return "badge-strategic";
    }
    if (b.includes("问讯") || b.includes("私聊")) {
      return "badge-inquiry";
    }
    if (b.includes("自爆") || b.includes("插话")) {
      return "badge-interrupt";
    }
    if (b.includes("投票")) {
      return "badge-vote";
    }
    return "";
  }

  // Cost is accumulated server-side from each call's price snapshot (v0.10):
  // changing a price later never rewrites history.
  function computeSessionUsage(s) {
    const u = s.usage_stats || {};
    if (typeof u.total_tokens === "number" && u.total_tokens > 0) {
      return { totalTokens: u.total_tokens, costUsd: Core.sessionCostUsd(u) };
    }
    return { totalTokens: 0, costUsd: Core.sessionCostUsd(u) };
  }

  // ---------------------------------------------------------------------------
  // Rendering Functions
  // ---------------------------------------------------------------------------

  function ensureValidSelectedChannel(s) {
    if (state.selectedChannel === "all" || state.selectedChannel === "engine") {
      return;
    }
    const exists = (s.channels || []).some((ch) => ch.id === state.selectedChannel);
    if (!exists) {
      state.selectedChannel = "all";
    }
  }

  function renderAll(force = false) {
    const s = state.session;
    if (!s) return;
    ensureValidSelectedChannel(s);
    renderTopbar(s);
    renderParticipantsBoard(s);
    renderChannelsSidebar(s, force);
    renderFeed(s, force);
    renderDashboard(s);
  }

  function renderStreamChunkOnly(s) {
    if (!s) return;
    renderTopbar(s);
    updateParticipantHighlightsInPlace(s);
    updateEngineChannelWorkingState(s);
    renderStreamingDraft(s);
    if (state.autoScroll) {
      elFeedList.scrollTop = elFeedList.scrollHeight;
    }
  }

  // Coalesce bursts of SSE events into at most one render per animation frame.
  // A pending full render always wins over a stream-chunk-only render.
  const renderQueue = { handle: null, full: false, force: false, stream: false };

  function scheduleRender(kind, force = false) {
    if (kind === "full") {
      renderQueue.full = true;
      renderQueue.force = renderQueue.force || force;
    } else {
      renderQueue.stream = true;
    }
    if (renderQueue.handle !== null) return;
    const run = () => {
      renderQueue.handle = null;
      const full = renderQueue.full;
      const force = renderQueue.force;
      const stream = renderQueue.stream;
      renderQueue.full = renderQueue.force = renderQueue.stream = false;
      if (!state.session) return;
      try {
        if (full) renderAll(force);
        else if (stream) renderStreamChunkOnly(state.session);
      } catch (e) {
        console.error("Render failed", e);
      }
    };
    renderQueue.handle =
      typeof window.requestAnimationFrame === "function"
        ? window.requestAnimationFrame(run)
        : window.setTimeout(run, 16);
  }

  // Single entry point for every new session snapshot (SSE or API response).
  // Resets per-game UI state when a different game is loaded.
  function setSession(s) {
    if (!s) return;
    const key = s.session_id || null;
    const prevCount = state.session && state.session.records ? state.session.records.length : 0;
    const newCount = s.records ? s.records.length : 0;
    if (
      state.session &&
      (key !== state.sessionKey || newCount < prevCount)
    ) {
      resetExpandState();
    }
    state.sessionKey = key;
    state.session = s;
    if (providerUI) providerUI.onSession(s);
    if (workspaceUI) workspaceUI.onSession(s);
  }

  function resetExpandState() {
    state.level1Expanded.clear();
    state.level2Expanded.clear();
    state.lastRecordsSig = "";
  }

  function formatTokensCount(num) {
    if (typeof num !== "number" || isNaN(num)) return "0 Tokens";
    if (num >= 1_000_000_000) {
      return `${(num / 1_000_000_000).toFixed(2)}B Tokens`;
    }
    if (num >= 1_000_000) {
      return `${(num / 1_000_000).toFixed(2)}M Tokens`;
    }
    if (num >= 1_000) {
      return `${(num / 1_000).toFixed(1)}K Tokens`;
    }
    return `${num.toLocaleString()} Tokens`;
  }

  function renderTopbar(s) {
    elAppTitle.textContent = s.title || "Arena-Agents";
    const ver = state.appVersion || s.engine_version || s.version || "";
    if (elAppVersion) {
      elAppVersion.textContent = ver;
    }
    if (elScenarioName) {
      elScenarioName.textContent = s.scenario_name || ver;
    }
    elStatusText.textContent = s.active_status_text || "等待开局";
    elPhasePill.textContent = s.phase_text || "未开始";

    if (
      elSelectExecDepth &&
      s.execution_depth &&
      document.activeElement !== elSelectExecDepth
    ) {
      elSelectExecDepth.value = s.execution_depth;
    }

    // Update top-right Token & Cost ($) telemetry (auto convert to M and B units)
    if (elUsageTokensText && elUsageCostText) {
      const usage = computeSessionUsage(s);
      elUsageTokensText.textContent = formatTokensCount(usage.totalTokens);
      elUsageCostText.textContent = `$${usage.costUsd.toFixed(4)}`;
    }

    elStatusDot.className = "status-dot";
    if (s.status === "running" || s.active_actor_id || s.moderator_active) {
      elStatusDot.classList.add("active");
    } else if (s.status === "paused") {
      elStatusDot.classList.add("paused");
    } else {
      elStatusDot.classList.add("idle");
    }

    // Moderator status pill in the top-right of the board
    if (elModeratorStatusPill && elModeratorStatusText) {
      const rawStatus = s.moderator_status || "待命 · 等待开局";
      const cleanStatus = rawStatus.replace(/^主持人[:：]\s*/, "");
      elModeratorStatusText.textContent = cleanStatus;
      if (s.moderator_active) {
        elModeratorStatusPill.classList.add("active");
      } else {
        elModeratorStatusPill.classList.remove("active");
      }
    }

    const isRunning = s.status === "running";
    const isPaused = s.status === "paused";

    elBtnStart.disabled = isRunning || (isPaused && s.records && s.records.length > 0);
    elBtnPause.disabled = !isRunning;
    elBtnResume.disabled = !isPaused;
    elBtnForcePause.disabled = !isRunning && !s.active_actor_id && !s.moderator_active;
    if (elBtnShuffle) elBtnShuffle.disabled = isRunning;
  }

  // Short "Provider / model · depth" label for a seat card (v0.10 bindings).
  function bindingLabelFor(s, actorId) {
    const b = (s.model_bindings && s.model_bindings[actorId]) || null;
    const providers = providerUI ? providerUI.getProviders() : [];
    if (b && providers.length === 0) return `${b.model_id || "未绑定"} · ${b.reasoning_depth || "high"}`;
    return Core.bindingSummary(b, providers);
  }

  function renderParticipantsBoard(s) {
    const tagStyles = s.tag_styles || {};
    const html = (s.participants || [])
      .map((p) => {
        const leftTags = [];
        const rightTags = [];
        let grayOut = false;

        const tags = p.tags || [];
        if (tags.length === 0) {
          leftTags.push(`<span class="tag-badge muted">待主持人分配</span>`);
        } else {
          tags.forEach((tag) => {
            const st = tagStyles[tag] || { slot: "right", variant: "neutral" };
            if (st.gray_out_card) grayOut = true;
            const badgeHtml = `<span class="tag-badge ${escapeHtml(
              st.variant || "neutral"
            )}">${escapeHtml(tag)}</span>`;
            if (st.slot === "left") {
              leftTags.push(badgeHtml);
            } else {
              rightTags.push(badgeHtml);
            }
          });
        }

        const isWorking = Boolean(p.generating || p.active_stage);
        const stageText = p.active_stage || (p.generating ? "思考中" : "");
        const cardClasses = ["player-card"];
        if (isWorking) cardClasses.push("generating");
        if (grayOut) cardClasses.push("grayed-out");

        return `
          <div class="${cardClasses.join(" ")}" data-player-id="${escapeHtml(p.id)}">
            <div class="card-top-row">
              <span>${escapeHtml(p.seat)} &middot; ${escapeHtml(p.id)}</span>
              <span class="card-generating-label">${
                isWorking ? escapeHtml(stageText) : ""
              }</span>
            </div>
            <div class="card-name">${escapeHtml(p.name)}</div>
            <div class="card-binding" data-bind-actor="${escapeHtml(p.id)}" title="点击设置此玩家的模型与思考深度">${escapeHtml(
              bindingLabelFor(s, p.id)
            )}</div>
            <div class="card-tags-row">
              <div class="tags-slot-left">${leftTags.join("")}</div>
              <div class="tags-slot-right">${rightTags.join("")}</div>
            </div>
          </div>
        `;
      })
      .join("");

    // Skip the DOM write when nothing changed (keeps hover/selection, saves work).
    if (html !== state.lastBoardHtml) {
      state.lastBoardHtml = html;
      elParticipantsGrid.innerHTML = html;
    }
  }

  elParticipantsGrid.addEventListener("click", (evt) => {
    const node = evt.target && evt.target.closest ? evt.target.closest(".card-binding[data-bind-actor]") : null;
    if (node && providerUI) providerUI.openBindings(node.getAttribute("data-bind-actor"));
  });

  function updateParticipantHighlightsInPlace(s) {
    (s.participants || []).forEach((p) => {
      const card = elParticipantsGrid.querySelector(
        `.player-card[data-player-id="${CSS.escape(p.id)}"]`
      );
      if (!card) return;
      const isWorking = Boolean(p.generating || p.active_stage);
      const stageText = p.active_stage || (p.generating ? "思考中" : "");
      const labelEl = card.querySelector(".card-generating-label");
      const labelText = isWorking ? stageText : "";
      if (
        card.classList.contains("generating") !== isWorking ||
        (labelEl && labelEl.textContent !== labelText)
      ) {
        // DOM now diverges from the last full render; force the next one to rewrite it.
        state.lastBoardHtml = null;
      }
      card.classList.toggle("generating", isWorking);
      if (labelEl && labelEl.textContent !== labelText) {
        labelEl.textContent = labelText;
      }
    });
  }

  function updateEngineChannelWorkingState(s) {
    const engineItem = elChannelsList.querySelector('.channel-item[data-channel="engine"]');
    if (engineItem) {
      engineItem.classList.toggle("working", Boolean(s.moderator_active));
    }
  }

  function renderChannelsSidebar(s, force = false) {
    const records = s.records || [];
    const engineCount = records.filter((r) => r.stream === "engine").length;
    const totalCount = records.length;

    const sigParts = [
      state.selectedChannel,
      state.showToolCalls ? "tools1" : "tools0",
      s.moderator_active ? "mod1" : "mod0",
      `eng:${engineCount}`,
      `tot:${totalCount}`,
    ];

    (s.channels || []).forEach((ch) => {
      const chCount = records.filter(
        (r) =>
          r.channel_id === ch.id &&
          (r.stream === "channel" || (state.showToolCalls && r.kind === "tool_call"))
      ).length;
      const qSig = Object.values(ch.interrupt_quotas || {})
        .map((q) => `${q.player_id}:${q.used}/${q.max_count}`)
        .join(",");
      sigParts.push(`${ch.id}:${ch.status}:${(ch.members || []).length}:${chCount}:${qSig}`);
    });

    const sig = sigParts.join("|");
    if (!force && sig === state.lastChannelsSig) {
      return;
    }
    state.lastChannelsSig = sig;

    const itemsHtml = [];

    // 1. 主持人处理流 (Engine Stream)
    const engineClasses = ["channel-item", "engine-stream"];
    if (state.selectedChannel === "engine") engineClasses.push("active");
    if (s.moderator_active) engineClasses.push("working");

    itemsHtml.push(`
      <div class="${engineClasses.join(" ")}" data-channel="engine">
        <div class="channel-row-top">
          <span class="channel-name">主持人处理流</span>
          <span class="channel-badge engine">引擎</span>
        </div>
        <div class="channel-meta">分析与工具调用 &middot; ${engineCount} 条</div>
        <div class="channel-members-preview">非频道 &middot; 无成员 &middot; 玩家永不可见</div>
      </div>
    `);

    // 2. 全部 (All Records)
    itemsHtml.push(`
      <div class="channel-item ${
        state.selectedChannel === "all" ? "active" : ""
      }" data-channel="all">
        <div class="channel-row-top">
          <span class="channel-name">全部</span>
        </div>
        <div class="channel-meta">${totalCount} 条记录</div>
      </div>
    `);

    // 3. Dynamic Channels created by Moderator
    const channels = s.channels || [];
    if (channels.length === 0) {
      itemsHtml.push(`
        <div class="empty-placeholder">
          暂无会话频道<br/>对局启动后由主持人调用工具动态建立
        </div>
      `);
    } else {
      channels.forEach((ch) => {
        const chRecords = records.filter(
          (r) =>
            r.channel_id === ch.id &&
            (r.stream === "channel" || (state.showToolCalls && r.kind === "tool_call"))
        );
        const memberNames = (ch.members || []).map((m) =>
          getActorDisplayName(s, m)
        );
        const statusLabel = ch.status === "open" ? "开放" : "关闭";

        const quotas = Object.values(ch.interrupt_quotas || {});
        const quotaHtml = quotas
          .map(
            (q) =>
              `<div class="channel-interrupt-pill">插话权: ${escapeHtml(
                q.player_name
              )}(${numText(q.used)}/${numText(q.max_count)})</div>`
          )
          .join("");

        itemsHtml.push(`
          <div class="channel-item ${
            state.selectedChannel === ch.id ? "active" : ""
          }" data-channel="${escapeHtml(ch.id)}">
            <div class="channel-row-top">
              <span class="channel-name">${escapeHtml(ch.name)}</span>
              <span class="channel-badge ${escapeHtml(ch.status)}">${statusLabel}</span>
            </div>
            <div class="channel-meta">${escapeHtml(ch.type)} &middot; ${numText(
          memberNames.length
        )} 人 &middot; ${numText(chRecords.length)} 条</div>
            <div class="channel-members-preview">${escapeHtml(
              memberNames.join("、")
            )}</div>
            ${quotaHtml}
          </div>
        `);
      });
    }

    elChannelsList.innerHTML = itemsHtml.join("");
  }

  function getActorDisplayName(s, actorId) {
    if (actorId === "moderator") return "主持人";
    const p = (s.participants || []).find((x) => x.id === actorId);
    return p ? p.name : actorId;
  }

  function groupRecordsIntoActionBundles(records) {
    const bundles = [];
    let curBundle = null;

    for (let i = 0; i < records.length; i++) {
      const r = records[i];
      const aid = r.action_id || `legacy_${r.actor_id}_${r.id}`;

      if (curBundle && curBundle.action_id === aid) {
        curBundle.records.push(r);
        curBundle.end_time = r.timestamp || curBundle.end_time;
      } else {
        if (curBundle) {
          bundles.push(curBundle);
        }
        curBundle = {
          action_id: aid,
          action_type:
            r.action_type || (r.actor_id === "moderator" ? "moderator" : "speak"),
          actor_id: r.actor_id,
          actor_name: r.actor_name,
          channel_id: r.channel_id,
          stream: r.stream,
          start_time: r.timestamp,
          end_time: r.timestamp,
          records: [r],
        };
      }
    }
    if (curBundle) {
      bundles.push(curBundle);
    }
    return bundles;
  }

  function isLevel1Expanded(actionId) {
    if (state.level1Expanded.has(actionId)) {
      return state.level1Expanded.get(actionId);
    }
    return true; // Level 1 default: expanded
  }

  function isLevel2Expanded(r) {
    if (r && state.level2Expanded.has(r.id)) {
      return state.level2Expanded.get(r.id);
    }
    // Level 2 default: only speech message is expanded; thoughts, tool calls, and notes are collapsed
    return r && r.kind === "message";
  }

  function renderSubitem(r, isMod) {
    const isSubExpanded = isLevel2Expanded(r);
    const isMsg = r.kind === "message";
    const isThought = r.kind === "thought";
    const isTool = r.kind === "tool_call";
    const isNote = r.kind === "note";

    // Subitem header badge
    let badgeHtml = "";
    if (isMsg) {
      badgeHtml = `<span class="subitem-tag-speech">发言</span>`;
    } else if (isThought) {
      const cls = isMod ? "badge-mod-thought" : "badge-player-thought";
      badgeHtml = `<span class="${cls}">思维链</span>`;
    } else if (isTool) {
      const cls = isMod ? "badge-mod-tool" : "badge-player-tool";
      badgeHtml = `<span class="${cls}">工具调用</span>${
        r.tool_name ? ` <strong class="tool-name">${escapeHtml(r.tool_name)}</strong>` : ""
      }`;
    } else if (isNote) {
      const cls = isMod ? "badge-mod-thought" : "badge-player-thought";
      badgeHtml = `<span class="${cls}">内部记录</span>`;
    } else {
      badgeHtml = `<span class="tag-badge neutral">${escapeHtml(r.kind || "记录")}</span>`;
    }

    // Subitem preview when collapsed
    let subPreview = "";
    if (!isSubExpanded) {
      if (isTool) {
        const argsStr = r.tool_args ? JSON.stringify(r.tool_args) : "";
        subPreview = argsStr.length > 70 ? argsStr.slice(0, 70) + "..." : argsStr;
      } else {
        subPreview = stripMarkdownForPreview(r.content || "");
        if (subPreview.length > 70) {
          subPreview = subPreview.slice(0, 70) + "...";
        }
      }
    }

    // Subitem body when expanded
    let bodyHtml = "";
    if (isSubExpanded) {
      if (isTool) {
        bodyHtml = renderToolCallContent(r);
      } else {
        bodyHtml = renderFormattedText(r.content);
      }
    }

    const subitemCls = [
      "action-subitem",
      isMsg ? "subitem-message" : isTool ? "subitem-tool" : isThought ? "subitem-thought" : "subitem-note",
      isSubExpanded ? "expanded" : "collapsed",
    ].join(" ");

    return `
      <div class="${subitemCls}" data-record-id="${escapeHtml(r.id)}">
        <div class="subitem-header">
          <span class="subitem-caret">${isSubExpanded ? "▼" : "▶"}</span>
          ${badgeHtml}
          ${!isSubExpanded ? `<span class="subitem-preview">${escapeHtml(subPreview)}</span>` : ""}
          <span class="subitem-time">${escapeHtml(r.timestamp || "")}</span>
        </div>
        ${isSubExpanded ? `<div class="subitem-body formatted-text">${bodyHtml}</div>` : ""}
      </div>
    `;
  }

  function renderActionCard(b, isStreaming = false) {
    const isMod = b.actor_id === "moderator";
    const isCardExpanded = isLevel1Expanded(b.action_id);

    // First preview text when Level 1 is collapsed
    let previewText = "";
    if (!isCardExpanded) {
      const firstMsg = b.records.find((r) => r.kind === "message");
      if (firstMsg) {
        previewText = stripMarkdownForPreview(firstMsg.content);
      } else if (b.records.length > 0) {
        const firstRec = b.records[0];
        if (firstRec.kind === "tool_call") {
          previewText = `[工具调用: ${firstRec.tool_name || ""}]`;
        } else {
          previewText = stripMarkdownForPreview(firstRec.content || "");
        }
      }
      if (previewText.length > 70) {
        previewText = previewText.slice(0, 70) + "...";
      }
    }

    const cardClass = [
      "action-card",
      isMod ? "moderator" : "player",
      isCardExpanded ? "expanded" : "collapsed",
      isStreaming ? "streaming" : "",
    ].join(" ");

    return `
      <div class="${cardClass}" data-action-id="${escapeHtml(b.action_id)}">
        <div class="action-card-header" data-action-id="${escapeHtml(b.action_id)}">
          <span class="action-caret">${isCardExpanded ? "▼" : "▶"}</span>
          <span class="action-actor-name">${escapeHtml(
            b.actor_name || b.actor_id
          )}</span>
          ${
            b.channel_id
              ? `<span class="action-channel-pill">${escapeHtml(
                  b.channel_id
                )}</span>`
              : ""
          }
          ${
            !isCardExpanded
              ? `<span class="action-preview">${escapeHtml(previewText)}</span>`
              : ""
          }
          <span class="action-time">${escapeHtml(
            b.end_time || b.start_time || ""
          )}</span>
        </div>

        ${
          isCardExpanded
            ? `
          <div class="action-card-body">
            ${b.records.map((r) => renderSubitem(r, isMod)).join("")}
          </div>
        `
            : ""
        }
      </div>
    `;
  }

  function filterFeedRecords(s) {
    const records = s.records || [];
    const sel = state.selectedChannel;

    return records.filter((rec) => {
      if (rec.kind === "tool_call" && !state.showToolCalls && sel !== "engine") {
        return false;
      }
      if (sel === "engine") {
        return rec.stream === "engine";
      }
      if (sel === "all") {
        return true;
      }
      return rec.channel_id === sel;
    });
  }

  // Make `container`'s element children match `htmlList` (one root element per
  // entry), touching only entries whose HTML differs from the previous call.
  function patchChildren(container, htmlList) {
    const prev = container.__patchHtml || [];
    const tpl = document.createElement("template");
    for (let i = 0; i < htmlList.length; i++) {
      const existing = container.children[i];
      if (existing && prev[i] === htmlList[i]) continue;
      tpl.innerHTML = htmlList[i];
      // Every entry must yield exactly one element so indexes stay aligned.
      const node = tpl.content.firstElementChild || document.createElement("span");
      if (existing) container.replaceChild(node, existing);
      else container.appendChild(node);
    }
    while (container.children.length > htmlList.length) {
      container.lastElementChild.remove();
    }
    container.__patchHtml = htmlList.slice();
  }

  function renderFeed(s, force = false) {
    const sel = state.selectedChannel;
    if (sel === "engine") {
      elFeedChannelLabel.textContent = "频道: 主持人处理流 (内部引擎)";
    } else if (sel === "all") {
      elFeedChannelLabel.textContent = "频道: 全部记录";
    } else {
      elFeedChannelLabel.textContent = `频道: ${sel}`;
    }

    const filtered = filterFeedRecords(s);
    const bundles = groupRecordsIntoActionBundles(filtered);
    const expandedSig = bundles
      .map(
        (b) =>
          `${b.action_id}:${isLevel1Expanded(b.action_id) ? 1 : 0}:[${b.records
            .map((r) => `${r.id}:${isLevel2Expanded(r) ? 1 : 0}`)
            .join(",")}]`
      )
      .join(";");
    const recordsSig = `${sel}|${state.showToolCalls ? 1 : 0}|${filtered.length}|${expandedSig}`;

    if (force || recordsSig !== state.lastRecordsSig) {
      state.lastRecordsSig = recordsSig;
      const blocksHtml = [];

      if (bundles.length === 0 && !s.streaming_draft) {
        blocksHtml.push(`
          <div class="empty-placeholder">
            当前暂无发言或思考记录 — 点击右上角「开始」后，主持人将深度学习游戏规则并调用工具创建频道、分配身份、推进对局
          </div>
        `);
      } else {
        bundles.forEach((bundle) => {
          blocksHtml.push(renderActionCard(bundle));
        });
      }

      // Patch only the cards that changed instead of rewriting the whole feed.
      patchChildren(elFeedRecordsContainer, blocksHtml);
    }

    renderStreamingDraft(s);

    if (state.autoScroll) {
      elFeedList.scrollTop = elFeedList.scrollHeight;
    }
  }

  // Replace `container`'s children with the given blocks, rewriting only the
  // blocks whose HTML changed (streaming text grows by appending, so usually
  // just the last block is touched).
  function patchBlocks(container, blocks) {
    patchChildren(container, blocks);
  }

  function draftStructureSig(d, aid, expanded) {
    return [
      expanded ? 1 : 0,
      d.actor_id === "moderator" ? "m" : "p",
      d.actor_name || d.actor_id || "",
      d.channel_id || "",
      d.thought_text ? 1 : 0,
      d.content_text ? 1 : 0,
      aid,
    ].join("\u0001");
  }

  function buildDraftCardHtml(d, aid, expanded) {
    const isMod = d.actor_id === "moderator";
    const subitems = [];
    if (expanded && d.thought_text) {
      subitems.push(`
        <div class="action-subitem subitem-thought expanded" data-record-id="${escapeHtml(aid)}_thought">
          <div class="subitem-header">
            <span class="subitem-caret">▼</span>
            <span class="${isMod ? "badge-mod-thought" : "badge-player-thought"}">思维链</span>
            <span class="subitem-time">思考中...</span>
          </div>
          <div class="subitem-body formatted-text" data-draft-part="thought"></div>
        </div>`);
    }
    if (expanded && d.content_text) {
      subitems.push(`
        <div class="action-subitem subitem-message expanded" data-record-id="${escapeHtml(aid)}_content">
          <div class="subitem-header">
            <span class="subitem-caret">▼</span>
            <span class="subitem-tag-speech">发言</span>
            <span class="subitem-time">发言生成中...</span>
          </div>
          <div class="subitem-body formatted-text" data-draft-part="content"></div>
        </div>`);
    }
    return `
      <div class="action-card ${isMod ? "moderator" : "player"} streaming ${
      expanded ? "expanded" : "collapsed"
    }" data-action-id="${escapeHtml(aid)}">
        <div class="action-card-header" data-action-id="${escapeHtml(aid)}">
          <span class="action-caret">${expanded ? "▼" : "▶"}</span>
          <span class="action-actor-name">${escapeHtml(d.actor_name || d.actor_id)}</span>
          ${
            d.channel_id
              ? `<span class="action-channel-pill">${escapeHtml(d.channel_id)}</span>`
              : ""
          }
          <span class="action-time">行动中...</span>
        </div>
        ${expanded ? `<div class="action-card-body">${subitems.join("")}</div>` : ""}
      </div>`;
  }

  // Streaming drafts are updated in place: a card is only rebuilt when its
  // structure changes; per-chunk updates patch the changed text blocks only.
  function renderStreamingDraft(s) {
    const sel = state.selectedChannel;
    const drafts = [];
    if (s.streaming_draft) {
      drafts.push(s.streaming_draft);
    }
    if (
      s.concurrent_streaming_drafts &&
      typeof s.concurrent_streaming_drafts === "object"
    ) {
      Object.values(s.concurrent_streaming_drafts).forEach((d) => {
        if (d && !drafts.some((x) => x.actor_id === d.actor_id)) {
          drafts.push(d);
        }
      });
    }

    const visibleDrafts = drafts.filter((d) => {
      if (sel === "all") return true;
      if (sel === "engine") return d.stream === "engine";
      return d.channel_id === sel;
    });

    const container = elFeedStreamingContainer;
    if (visibleDrafts.length === 0) {
      if (state.draftNodes.size > 0 || container.firstChild) {
        while (container.lastElementChild) container.lastElementChild.remove();
        state.draftNodes.clear();
      }
      return;
    }

    const seen = new Set();
    visibleDrafts.forEach((d, idx) => {
      const aid = d.action_id || `draft_${d.actor_id}`;
      let key = aid;
      while (seen.has(key)) key += "#";
      seen.add(key);

      const expanded = isLevel1Expanded(aid);
      const sig = draftStructureSig(d, aid, expanded);
      let entry = state.draftNodes.get(key);

      if (!entry || entry.sig !== sig || !entry.el.isConnected) {
        const tpl = document.createElement("template");
        tpl.innerHTML = buildDraftCardHtml(d, aid, expanded);
        const el = tpl.content.firstElementChild;
        if (entry && entry.el.parentNode === container) {
          container.replaceChild(el, entry.el);
        } else {
          container.appendChild(el);
        }
        entry = { el, sig };
        state.draftNodes.set(key, entry);
      }

      // Keep DOM order equal to draft order.
      if (container.children[idx] !== entry.el) {
        container.insertBefore(entry.el, container.children[idx] || null);
      }

      if (expanded) {
        const thoughtEl = entry.el.querySelector('[data-draft-part="thought"]');
        if (thoughtEl) patchBlocks(thoughtEl, renderFormattedBlocks(d.thought_text));
        const contentEl = entry.el.querySelector('[data-draft-part="content"]');
        if (contentEl) patchBlocks(contentEl, renderFormattedBlocks(d.content_text));
      }
    });

    // Drop cards for drafts that finished or are no longer visible.
    for (const [key, entry] of Array.from(state.draftNodes.entries())) {
      if (!seen.has(key)) {
        entry.el.remove();
        state.draftNodes.delete(key);
      }
    }
  }

  function renderDashboard(s) {
    const dash = s.dashboard || {};
    const factions = dash.factions || [];
    const tagStats = dash.tag_stats || [];

    const factionsHtml =
      factions.length === 0
        ? `<div class="empty-placeholder">待主持人调用工具初始化</div>`
        : factions
            .map(
              (f) => `
          <div class="stat-row">
            <span>${escapeHtml(f.name)}</span>
            <span class="stat-value ${escapeHtml(f.variant || "neutral")}">${numText(
                f.alive
              )} / ${numText(f.total)}</span>
          </div>
        `
            )
            .join("");

    const tagStatsHtml =
      tagStats.length === 0
        ? `<div class="empty-placeholder">待主持人调用工具初始化</div>`
        : tagStats
            .map(
              (t) => `
          <div class="stat-row">
            <span>${escapeHtml(t.name)}</span>
            <span class="stat-value ${escapeHtml(t.variant || "neutral")}">${numText(
                t.count
              )}</span>
          </div>
        `
            )
            .join("");

    const sig = factionsHtml + "\u0001" + tagStatsHtml;
    if (sig === state.lastDashboardSig) return;
    state.lastDashboardSig = sig;
    elFactionsList.innerHTML = factionsHtml;
    elTagStatsList.innerHTML = tagStatsHtml;
  }

  // ---------------------------------------------------------------------------
  // Event Delegation (Immune to SSE DOM Refreshes)
  // ---------------------------------------------------------------------------

  function handleChannelSelectEvent(evt) {
    if (evt.type === "mousedown" && evt.button !== 0) return;
    const item = evt.target.closest(".channel-item[data-channel]");
    if (!item) return;
    const chId = item.getAttribute("data-channel");
    if (chId && state.selectedChannel !== chId) {
      state.selectedChannel = chId;
      if (state.session) {
        renderChannelsSidebar(state.session, true);
        renderFeed(state.session, true);
      }
    }
  }

  elChannelsList.addEventListener("mousedown", handleChannelSelectEvent);
  elChannelsList.addEventListener("click", handleChannelSelectEvent);

  function handleFeedClick(evt) {
    if (
      window.getSelection &&
      window.getSelection().toString().trim().length > 0
    ) {
      return;
    }

    // Level 2 toggle: clicking a subitem header inside an action
    const subHeader = evt.target.closest(".subitem-header");
    if (subHeader) {
      const subitem = subHeader.closest(".action-subitem");
      const rid = subitem ? subitem.getAttribute("data-record-id") : null;
      if (rid && state.session) {
        const rec = (state.session.records || []).find((x) => x.id === rid);
        const curExpanded = isLevel2Expanded(rec || { id: rid, kind: "message" });
        boundedSet(state.level2Expanded, rid, !curExpanded, MAX_EXPAND_ENTRIES);
        renderFeed(state.session, true);
        return;
      }
    }

    // Level 1 toggle: clicking action card header
    const cardHeader = evt.target.closest(".action-card-header");
    if (cardHeader) {
      const aid = cardHeader.getAttribute("data-action-id");
      if (aid && state.session) {
        boundedSet(state.level1Expanded, aid, !isLevel1Expanded(aid), MAX_EXPAND_ENTRIES);
        renderFeed(state.session, true);
        return;
      }
    }
  }

  elFeedRecordsContainer.addEventListener("click", handleFeedClick);
  elFeedStreamingContainer.addEventListener("click", handleFeedClick);

  // ---------------------------------------------------------------------------
  // Event Handlers & API Calls
  // ---------------------------------------------------------------------------

  elBtnTheme.addEventListener("click", () => {
    const htmlEl = document.documentElement;
    const cur = htmlEl.getAttribute("data-theme") || "light";
    const next = cur === "light" ? "dark" : "light";
    htmlEl.setAttribute("data-theme", next);
  });

  elBtnNew.addEventListener("click", async () => {
    try {
      const res = await apiPost("api/control/new");
      setSession(res.session);
      resetExpandState();
      state.selectedChannel = "all";
      renderAll(true);
      showToast("已重置为空白未开始对局，点击「开始」由主持人初始化并推进");
    } catch (err) {
      showToast(err.message);
    }
  });

  if (elBtnShuffle) {
    elBtnShuffle.addEventListener("click", async () => {
      try {
        const res = await apiPost("api/control/shuffle-identities");
        setSession(res.session);
        renderAll(true);
        showToast("已刷新初始身份洗牌种子");
      } catch (err) {
        showToast(err.message);
      }
    });
  }

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

  elBtnStart.addEventListener("click", async () => {
    try {
      const res = await apiPost("api/control/start");
      setSession(res.session);
      renderAll(true);
      showToast("对局已启动 — 主持人正在深度学习规则并初始化全场");
    } catch (err) {
      showToast(err.message);
    }
  });

  elBtnResume.addEventListener("click", async () => {
    try {
      const res = await apiPost("api/control/start");
      setSession(res.session);
      renderAll(true);
      showToast("对局已继续运行");
    } catch (err) {
      showToast(err.message);
    }
  });

  elBtnPause.addEventListener("click", async () => {
    try {
      const res = await apiPost("api/control/pause");
      setSession(res.session);
      renderAll(true);
      showToast("已请求在当前步骤完成后暂停");
    } catch (err) {
      showToast(err.message);
    }
  });

  elBtnForcePause.addEventListener("click", async () => {
    try {
      const res = await apiPost("api/control/force-pause");
      setSession(res.session);
      renderAll(true);
      showToast("已强制中断当前生成并暂停");
    } catch (err) {
      showToast(err.message);
    }
  });

  elChkShowTools.addEventListener("change", () => {
    state.showToolCalls = elChkShowTools.checked;
    if (state.session) {
      renderChannelsSidebar(state.session, true);
      renderFeed(state.session, true);
    }
  });

  if (elBtnResetDefault) {
    elBtnResetDefault.addEventListener("click", () => {
      resetExpandState();
      if (state.session) {
        renderFeed(state.session, true);
      }
      showToast("已恢复默认折叠：一级行动展开，二级仅发言展开，思维链与工具折叠");
    });
  }

  if (elBtnExpandAll) {
    elBtnExpandAll.addEventListener("click", () => {
      if (!state.session) return;
      const bundles = groupRecordsIntoActionBundles(filterFeedRecords(state.session));
      bundles.forEach((b) => {
        boundedSet(state.level1Expanded, b.action_id, true, MAX_EXPAND_ENTRIES);
        b.records.forEach((r) => {
          boundedSet(state.level2Expanded, r.id, true, MAX_EXPAND_ENTRIES);
        });
      });
      renderFeed(state.session, true);
      showToast("已全展开（一级行动与二级思维链/工具/发言全部展开）");
    });
  }

  if (elBtnCollapseAll) {
    elBtnCollapseAll.addEventListener("click", () => {
      if (!state.session) return;
      const bundles = groupRecordsIntoActionBundles(filterFeedRecords(state.session));
      bundles.forEach((b) => {
        boundedSet(state.level1Expanded, b.action_id, false, MAX_EXPAND_ENTRIES);
      });
      renderFeed(state.session, true);
      showToast("已全折叠（一级行动卡片全部折叠）");
    });
  }

  if (elSelectExecDepth) {
    elSelectExecDepth.addEventListener("change", async () => {
      const val = elSelectExecDepth.value;
      try {
        await apiPost("api/session/execution-depth", {
          execution_depth: val,
        });
        showToast(`已将执行链深度切换为: ${val}`);
      } catch (err) {
        showToast(err.message);
      }
    });
  }

  elFeedList.addEventListener("scroll", () => {
    const nearBottom =
      elFeedList.scrollHeight - elFeedList.scrollTop - elFeedList.clientHeight < 60;
    state.autoScroll = nearBottom;
  });

  // ---------------------------------------------------------------------------
  // Session Import
  // ---------------------------------------------------------------------------
  const elBtnImportSession = document.getElementById("btn-import-session");
  const elFileImportSession = document.getElementById("file-import-session");
  if (elUsagePill) {
    // Cost breakdown lives with the model bindings.
    elUsagePill.addEventListener("click", () => {
      if (providerUI) providerUI.openBindings();
    });
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
        setSession(res.session);
        // A different game was loaded: forget every folded/expanded card.
        resetExpandState();
        state.selectedChannel = "all";
        renderAll(true);
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
    // card size / collapsed panels are applied immediately.
    layoutUI = window.ArenaLayout.create({ Core });
  }

  function handleSessionLoaded(session) {
    setSession(session);
    resetExpandState();
    state.selectedChannel = "all";
    renderAll(true);
  }

  if (window.ArenaProviderUI) {
    providerUI = window.ArenaProviderUI.create({
      api,
      Core,
      showToast,
      getSession: () => state.session,
      onSessionLoaded: handleSessionLoaded,
      // Providers/bindings changed: repaint the seat cards that show their model.
      onChange: () => {
        state.lastBoardHtml = null;
        if (state.session) renderParticipantsBoard(state.session);
      },
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
  // SSE Real-time Connection (exponential backoff + jitter, single timer)
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
    const es = new EventSource(Core.buildEventsUrl(URL_PREFIX, getToken()));
    state.sse = es;

    es.onopen = () => {
      if (state.sse === es) markSseConnected();
    };

    es.onmessage = (evt) => {
      if (state.sse !== es) return;
      markSseConnected();
      try {
        const payload = JSON.parse(evt.data);
        // A workspace switch changes providers, templates and sessions for the whole
        // server process; other tabs must rebuild their entire UI, not merge a session.
        if (payload.event === "workspace_changed") {
          window.location.reload();
          return;
        }
        if (payload.session) {
          setSession(payload.session);
          if (payload.event === "stream_chunk") {
            scheduleRender("stream");
          } else {
            scheduleRender("full", payload.event === "initial");
          }
        }
      } catch (e) {
        console.error("Failed to parse SSE payload", e);
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
        setSession(data.session);
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
    if (elChkShowTools) {
      state.showToolCalls = elChkShowTools.checked;
    }
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

  init();
})();
