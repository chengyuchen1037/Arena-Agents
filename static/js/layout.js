/* ==========================================================================
   Arena-Agents v0.11 — Layout: board / side-panel collapsing, drag resizers and
   the four player-card size tiers. All persistence goes through localStorage
   (pure normalization helpers live in core.js). Loaded after core.js; app.js
   calls window.ArenaLayout.create() once the DOM is available.

   Nothing here re-renders the session: it only toggles classes / inline sizes,
   so existing event delegation and streaming scroll are untouched.
   ========================================================================== */

(function (root) {
  "use strict";

  function create(ctx) {
    const opts = ctx || {};
    const Core = opts.Core || root.ArenaCore;
    const doc = opts.document || root.document;
    const win = opts.window || root;
    const storage =
      opts.storage !== undefined ? opts.storage : root.localStorage || null;
    if (!Core || !doc) return null;

    const $ = (id) => (typeof doc.getElementById === "function" ? doc.getElementById(id) : null);

    const el = {
      main: $("main-container"),
      boardSection: $("board-section"),
      boardResizer: $("board-resizer"),
      workspaceGrid: $("workspace-grid"),
      channelsPanel: $("channels-panel"),
      dashboardPanel: $("dashboard-panel"),
      channelsResizer: $("channels-resizer"),
      dashboardResizer: $("dashboard-resizer"),
      btnToggleBoard: $("btn-toggle-board"),
      btnToggleBoardLabel: $("btn-toggle-board-label"),
      btnToggleChannels: $("btn-toggle-channels"),
      btnToggleDashboard: $("btn-toggle-dashboard"),
      selectCardSize: $("select-card-size"),
      participantsGrid: $("participants-grid"),
    };

    const state = Core.loadLayoutState(storage);
    state.cardSize = Core.loadCardSize(storage);

    const active = { kind: null, startPos: 0, startSize: 0 };

    // -----------------------------------------------------------------------
    // Small helpers (the fake DOM used by node tests only implements a subset)
    // -----------------------------------------------------------------------
    function call(node, method, ...args) {
      if (node && typeof node[method] === "function") {
        try {
          node[method](...args);
        } catch {
          /* ignore: layout must never break the game UI */
        }
      }
    }

    function setText(node, text) {
      if (node) node.textContent = String(text);
    }

    function viewportWidth() {
      return Math.max(320, Math.round(Number(win.innerWidth) || 1280));
    }

    function viewportHeight() {
      return Math.max(320, Math.round(Number(win.innerHeight) || 800));
    }

    function measureHeight(node) {
      if (node && typeof node.getBoundingClientRect === "function") {
        const rect = node.getBoundingClientRect();
        if (rect && Number.isFinite(rect.height) && rect.height > 0) {
          return Math.round(rect.height);
        }
      }
      return 0;
    }

    function measureWidth(node) {
      if (node && typeof node.getBoundingClientRect === "function") {
        const rect = node.getBoundingClientRect();
        if (rect && Number.isFinite(rect.width) && rect.width > 0) {
          return Math.round(rect.width);
        }
      }
      return 0;
    }

    function maxBoardHeight() {
      const usable = Math.max(
        Core.PANEL_LIMITS.board.min + 24,
        (viewportHeight() - 54) * Core.PANEL_LIMITS.board.maxRatio
      );
      return Math.round(usable);
    }

    function clampBoardHeight(value) {
      return Math.max(
        Core.PANEL_LIMITS.board.min,
        Math.min(maxBoardHeight(), Math.round(Number(value) || 0))
      );
    }

    function clampPanelWidth(side, value) {
      const limits = Core.PANEL_LIMITS[side] || Core.PANEL_LIMITS.left;
      const other = side === "left" ? state.rightWidth : state.leftWidth;
      // Keep a usable centre column even on a small viewport.
      const usable = viewportWidth() - other - 360;
      const max = Math.max(limits.min, Math.min(limits.max, usable));
      return Math.max(limits.min, Math.min(max, Math.round(Number(value) || 0)));
    }

    // -----------------------------------------------------------------------
    // Apply state to the DOM
    // -----------------------------------------------------------------------
    function applyCardSize() {
      const size = Core.normalizeCardSize(state.cardSize);
      state.cardSize = size;
      if (el.selectCardSize && el.selectCardSize.value !== size) {
        el.selectCardSize.value = size;
      }
      const grid = el.participantsGrid;
      if (grid) {
        for (const tier of Core.CARD_SIZES) call(grid.classList, "remove", `card-size-${tier.id}`);
        call(grid.classList, "add", `card-size-${size}`);
        if (typeof grid.setAttribute === "function") grid.setAttribute("data-card-size", size);
      }
    }

    function applyResponsive() {
      if (!el.workspaceGrid) return;
      const narrow = viewportWidth() <= 980;
      call(el.workspaceGrid.classList, "toggle", "narrow-layout", narrow);
      return narrow;
    }

    function updateToggleUi() {
      const bc = Boolean(state.boardCollapsed);
      const lc = Boolean(state.leftCollapsed);
      const rc = Boolean(state.rightCollapsed);

      const boardBtn = el.btnToggleBoard;
      if (boardBtn) {
        if (typeof boardBtn.setAttribute === "function") {
          boardBtn.setAttribute("aria-pressed", bc ? "true" : "false");
          boardBtn.setAttribute("title", bc ? "展开参与者看板" : "折叠参与者看板");
        }
        setText(el.btnToggleBoardLabel, bc ? "展开看板" : "折叠看板");
      }

      const leftBtn = el.btnToggleChannels;
      if (leftBtn) {
        if (typeof leftBtn.setAttribute === "function") {
          leftBtn.setAttribute("aria-pressed", lc ? "true" : "false");
          leftBtn.setAttribute("title", lc ? "展开左侧频道栏" : "折叠左侧频道栏");
        }
        setText(leftBtn, lc ? "▶" : "◀");
      }

      const rightBtn = el.btnToggleDashboard;
      if (rightBtn) {
        if (typeof rightBtn.setAttribute === "function") {
          rightBtn.setAttribute("aria-pressed", rc ? "true" : "false");
          rightBtn.setAttribute("title", rc ? "展开右侧状态栏" : "折叠右侧状态栏");
        }
        setText(rightBtn, rc ? "◀" : "▶");
      }
    }

    function apply() {
      const bc = Boolean(state.boardCollapsed);
      const lc = Boolean(state.leftCollapsed);
      const rc = Boolean(state.rightCollapsed);

      if (el.main) call(el.main.classList, "toggle", "board-collapsed", bc);
      if (el.boardSection) {
        // null => natural (auto) height
        el.boardSection.style.height =
          !bc && state.boardHeight != null ? `${state.boardHeight}px` : "";
      }
      if (el.boardResizer) el.boardResizer.hidden = bc;

      if (el.workspaceGrid) {
        call(el.workspaceGrid.classList, "toggle", "left-collapsed", lc);
        call(el.workspaceGrid.classList, "toggle", "right-collapsed", rc);
      }
      if (el.channelsPanel) {
        el.channelsPanel.style.width = lc ? "" : `${state.leftWidth}px`;
      }
      if (el.dashboardPanel) {
        el.dashboardPanel.style.width = rc ? "" : `${state.rightWidth}px`;
      }
      if (el.channelsResizer) el.channelsResizer.hidden = lc;
      if (el.dashboardResizer) el.dashboardResizer.hidden = rc;

      updateToggleUi();
      applyCardSize();
      applyResponsive();
      return publicState();
    }

    function persist() {
      const saved = Core.saveLayoutState(storage, state);
      state.boardCollapsed = saved.boardCollapsed;
      state.leftCollapsed = saved.leftCollapsed;
      state.rightCollapsed = saved.rightCollapsed;
      state.boardHeight = saved.boardHeight;
      state.leftWidth = saved.leftWidth;
      state.rightWidth = saved.rightWidth;
      Core.saveCardSize(storage, state.cardSize);
    }

    function publicState() {
      return {
        boardCollapsed: Boolean(state.boardCollapsed),
        leftCollapsed: Boolean(state.leftCollapsed),
        rightCollapsed: Boolean(state.rightCollapsed),
        boardHeight: state.boardHeight,
        leftWidth: state.leftWidth,
        rightWidth: state.rightWidth,
        cardSize: state.cardSize,
      };
    }

    // -----------------------------------------------------------------------
    // Collapse / expand
    // -----------------------------------------------------------------------
    function toggleBoard() {
      state.boardCollapsed = !state.boardCollapsed;
      persist();
      apply();
      return state.boardCollapsed;
    }

    function toggleChannels() {
      state.leftCollapsed = !state.leftCollapsed;
      persist();
      apply();
      return state.leftCollapsed;
    }

    function toggleDashboard() {
      state.rightCollapsed = !state.rightCollapsed;
      persist();
      apply();
      return state.rightCollapsed;
    }

    function setCardSize(id) {
      state.cardSize = Core.normalizeCardSize(id);
      Core.saveCardSize(storage, state.cardSize);
      applyCardSize();
      return state.cardSize;
    }

    // -----------------------------------------------------------------------
    // Drag resizing (the drag starts on a separator, continues on the document)
    // -----------------------------------------------------------------------
    function currentSizeOf(kind) {
      if (kind === "board") {
        if (state.boardHeight != null) return state.boardHeight;
        return measureHeight(el.boardSection) || 150;
      }
      if (kind === "left") {
        return state.leftCollapsed ? 0 : state.leftWidth;
      }
      if (kind === "right") {
        return state.rightCollapsed ? 0 : state.rightWidth;
      }
      return 0;
    }

    function beginResize(kind, pos) {
      if (kind !== "board" && kind !== "left" && kind !== "right") return false;
      if (kind === "board" && state.boardCollapsed) return false;
      if (kind === "left" && state.leftCollapsed) return false;
      if (kind === "right" && state.rightCollapsed) return false;
      active.kind = kind;
      active.startPos = Number(pos) || 0;
      active.startSize = currentSizeOf(kind);
      setDraggingCursor(kind);
      return true;
    }

    function moveResize(pos) {
      if (!active.kind) return false;
      const delta = (Number(pos) || 0) - active.startPos;
      if (active.kind === "board") {
        state.boardHeight = clampBoardHeight(active.startSize + delta);
      } else if (active.kind === "left") {
        state.leftWidth = clampPanelWidth("left", active.startSize + delta);
      } else {
        // right panel grows when the separator is dragged left
        state.rightWidth = clampPanelWidth("right", active.startSize - delta);
      }
      apply();
      return true;
    }

    function endResize() {
      if (!active.kind) return false;
      active.kind = null;
      clearDraggingCursor();
      persist();
      return true;
    }

    function setDraggingCursor(kind) {
      const body = doc.body;
      if (!body) return;
      call(body.classList, "add", "arena-resizing");
      if (body.style) body.style.cursor = kind === "board" ? "row-resize" : "col-resize";
    }

    function clearDraggingCursor() {
      const body = doc.body;
      if (!body) return;
      call(body.classList, "remove", "arena-resizing");
      if (body.style) body.style.cursor = "";
    }

    function resetBoardHeight() {
      state.boardHeight = null;
      persist();
      apply();
      return state.boardHeight;
    }

    function resetLeftWidth() {
      state.leftWidth = Core.LAYOUT_DEFAULTS.leftWidth;
      persist();
      apply();
      return state.leftWidth;
    }

    function resetRightWidth() {
      state.rightWidth = Core.LAYOUT_DEFAULTS.rightWidth;
      persist();
      apply();
      return state.rightWidth;
    }

    function reflowLimits() {
      state.leftWidth = clampPanelWidth("left", state.leftWidth);
      state.rightWidth = clampPanelWidth("right", state.rightWidth);
      if (state.boardHeight != null) state.boardHeight = clampBoardHeight(state.boardHeight);
      apply();
    }

    // -----------------------------------------------------------------------
    // Event wiring
    // -----------------------------------------------------------------------
    function on(node, type, handler) {
      if (node && typeof node.addEventListener === "function") {
        node.addEventListener(type, handler);
      }
    }

    function safePreventDefault(evt) {
      if (evt && typeof evt.preventDefault === "function") evt.preventDefault();
    }

    function positionOf(kind, evt) {
      if (!evt) return 0;
      if (kind === "board") {
        return Number.isFinite(evt.clientY) ? evt.clientY : 0;
      }
      return Number.isFinite(evt.clientX) ? evt.clientX : 0;
    }

    function wireResizer(node, kind, onReset) {
      on(node, "pointerdown", (evt) => {
        if (evt && evt.button !== undefined && evt.button !== 0) return;
        safePreventDefault(evt);
        beginResize(kind, positionOf(kind, evt));
      });
      on(node, "dblclick", (evt) => {
        safePreventDefault(evt);
        onReset();
      });
    }

    wireResizer(el.boardResizer, "board", resetBoardHeight);
    wireResizer(el.channelsResizer, "left", resetLeftWidth);
    wireResizer(el.dashboardResizer, "right", resetRightWidth);

    on(el.btnToggleBoard, "click", () => toggleBoard());
    on(el.btnToggleChannels, "click", () => toggleChannels());
    on(el.btnToggleDashboard, "click", () => toggleDashboard());
    on(el.selectCardSize, "change", () => setCardSize(el.selectCardSize.value));

    if (typeof doc.addEventListener === "function") {
      doc.addEventListener("pointermove", (evt) => {
        if (!active.kind) return;
        safePreventDefault(evt);
        moveResize(positionOf(active.kind, evt));
      });
      doc.addEventListener("pointerup", () => endResize());
      doc.addEventListener("pointercancel", () => endResize());
    }
    if (typeof win.addEventListener === "function") {
      win.addEventListener("resize", () => reflowLimits());
    }

    apply();

    return {
      state: publicState,
      els: el,
      apply,
      persist,
      toggleBoard,
      toggleChannels,
      toggleDashboard,
      setCardSize,
      beginResize,
      moveResize,
      endResize,
      resetBoardHeight,
      resetLeftWidth,
      resetRightWidth,
      reflowLimits,
      isNarrow: () => Boolean(el.workspaceGrid && el.workspaceGrid.classList.contains("narrow-layout")),
      measureHeight: () => measureHeight(el.boardSection),
      measureWidth: (which) =>
        measureWidth(which === "left" ? el.channelsPanel : el.dashboardPanel),
    };
  }

  root.ArenaLayout = { create };
})(typeof window !== "undefined" ? window : globalThis);
