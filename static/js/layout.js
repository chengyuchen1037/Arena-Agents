/* ==========================================================================
   Arena-Agents v0.12.1 — Layout: participant bar collapsing, channel column rail and drag resizers.
   All persistence goes through localStorage (pure normalization helpers live in
   core.js). Loaded after core.js; app.js calls window.ArenaLayout.create() once
   the DOM is available.

   v0.12.1: the right-hand "对局状态" column is gone (its numbers are the summary strip of the
   participant bar).  "Collapsing" the channel column now shrinks it to an avatar rail (names
   hidden) instead of hiding it; below 1024 px the rail is forced by the stylesheet / the
   `narrow-layout` class.

   Nothing here re-renders the session: it only toggles classes / inline sizes,
   so existing event delegation and the feed's scroll position are untouched.
   ========================================================================== */

(function (root) {
  "use strict";

  const NARROW_MAX = 1023; // below 1024 px the channel column is always an avatar rail

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
      channelsResizer: $("channels-resizer"),
      btnToggleBoard: $("btn-toggle-board"),
      btnToggleBoardLabel: $("btn-toggle-board-label"),
      btnToggleChannels: $("btn-toggle-channels"),
    };

    const state = Core.loadLayoutState(storage);

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

    function clampPanelWidth(value) {
      const limits = Core.PANEL_LIMITS.left;
      // Keep a usable feed column even on a small viewport.
      const usable = viewportWidth() - 420;
      const max = Math.max(limits.min, Math.min(limits.max, usable));
      return Math.max(limits.min, Math.min(max, Math.round(Number(value) || 0)));
    }

    // -----------------------------------------------------------------------
    // Apply state to the DOM
    // -----------------------------------------------------------------------
    /** The avatar rail is forced in the "compact" layout (platform.js sets <html data-layout>). */
    function applyResponsive() {
      if (!el.workspaceGrid) return false;
      const html = doc.documentElement;
      const layout = html && typeof html.getAttribute === "function" ? html.getAttribute("data-layout") : null;
      const narrow = layout ? layout === "compact" : viewportWidth() <= NARROW_MAX;
      call(el.workspaceGrid.classList, "toggle", "narrow-layout", narrow);
      return narrow;
    }

    function updateToggleUi() {
      const bc = Boolean(state.boardCollapsed);
      const lc = Boolean(state.leftCollapsed);

      const boardBtn = el.btnToggleBoard;
      if (boardBtn) {
        if (typeof boardBtn.setAttribute === "function") {
          boardBtn.setAttribute("aria-pressed", bc ? "true" : "false");
          boardBtn.setAttribute("title", bc ? "展开参与者栏" : "折叠参与者栏");
        }
        setText(el.btnToggleBoardLabel, bc ? "展开看板" : "折叠看板");
      }

      const leftBtn = el.btnToggleChannels;
      if (leftBtn) {
        if (typeof leftBtn.setAttribute === "function") {
          leftBtn.setAttribute("aria-pressed", lc ? "true" : "false");
          leftBtn.setAttribute("title", lc ? "展开频道栏（显示名字）" : "收起为头像条");
        }
        setText(leftBtn, lc ? "▶" : "◀");
      }
    }

    function apply() {
      const bc = Boolean(state.boardCollapsed);
      const lc = Boolean(state.leftCollapsed);

      if (el.main) call(el.main.classList, "toggle", "board-collapsed", bc);
      if (el.boardSection) {
        // null => natural (auto) height
        el.boardSection.style.height =
          !bc && state.boardHeight != null ? `${state.boardHeight}px` : "";
      }
      if (el.boardResizer) el.boardResizer.hidden = bc;

      if (el.workspaceGrid) {
        call(el.workspaceGrid.classList, "toggle", "left-collapsed", lc);
      }
      if (el.channelsPanel) {
        el.channelsPanel.style.width = lc ? "" : `${state.leftWidth}px`;
      }
      if (el.channelsResizer) el.channelsResizer.hidden = lc;

      updateToggleUi();
      applyResponsive();
      return publicState();
    }

    function persist() {
      const saved = Core.saveLayoutState(storage, state);
      state.boardCollapsed = saved.boardCollapsed;
      state.leftCollapsed = saved.leftCollapsed;
      state.boardHeight = saved.boardHeight;
      state.leftWidth = saved.leftWidth;
    }

    function publicState() {
      return {
        boardCollapsed: Boolean(state.boardCollapsed),
        leftCollapsed: Boolean(state.leftCollapsed),
        boardHeight: state.boardHeight,
        leftWidth: state.leftWidth,
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
      return 0;
    }

    function beginResize(kind, pos) {
      if (kind !== "board" && kind !== "left") return false;
      if (kind === "board" && state.boardCollapsed) return false;
      if (kind === "left" && state.leftCollapsed) return false;
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
      } else {
        state.leftWidth = clampPanelWidth(active.startSize + delta);
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

    function reflowLimits() {
      state.leftWidth = clampPanelWidth(state.leftWidth);
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

    on(el.btnToggleBoard, "click", () => toggleBoard());
    on(el.btnToggleChannels, "click", () => toggleChannels());

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
      beginResize,
      moveResize,
      endResize,
      resetBoardHeight,
      resetLeftWidth,
      reflowLimits,
      isNarrow: () => Boolean(el.workspaceGrid && el.workspaceGrid.classList.contains("narrow-layout")),
      measureHeight: () => measureHeight(el.boardSection),
      measureWidth: () => measureWidth(el.channelsPanel),
    };
  }

  root.ArenaLayout = { create, NARROW_MAX };
})(typeof window !== "undefined" ? window : globalThis);
