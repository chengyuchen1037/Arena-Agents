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

  // ==========================================================================
  // v0.14 (H29): moving the player cards while the game has not started
  // ==========================================================================

  const SEAT_DRAG_PX = 6; // a mouse / pen press that moves this far is a drag, not a click
  const SEAT_LONG_PRESS_MS = 380; // a touch has to be held this long before the card can be moved (a swipe scrolls instead)
  const SEAT_TOUCH_SLOP_PX = 10; // ... and must not wander further than this meanwhile
  const SEAT_CLICK_GUARD_MS = 350; // the click that follows a drag is not a press on a card

  /**
   * The cards of the participant bar can be moved while the game has not started (the seating is the line-up of the bots): a mouse / pen press that moves,
   * a touch that is held and then moved, or Alt+← / Alt+→ with the focus on a card.  This only works out WHAT was moved - `commit(from, to, {keyboard})`
   * (indexes in seat order, `to` = the seat the card was dropped on) is the caller's: it changes the line-up and asks the server (app.js).
   *
   * ctx: { document, window, grid (the card container), View, armed() -> may the cards be moved now?, commit(from, to, info), onBusy(busy) }
   * `busy()` is true from the press on a card until it is let go: the caller does not repaint the cards meanwhile (the card in the hand would be replaced
   * under the finger, and a repaint between the press and the release loses the click).  `clickSuppressed()`: the click that ends a drag must not open the card.
   */
  function createSeatDrag(ctx) {
    const opts = ctx || {};
    const doc = opts.document || root.document;
    const win = opts.window || root;
    const View = opts.View || root.ArenaView;
    const grid = opts.grid;
    if (!doc || !grid || !View || typeof grid.addEventListener !== "function") return null;
    const armed = opts.armed || (() => false);
    const commit = opts.commit || (() => {});
    const onBusy = opts.onBusy || (() => {});
    const setTimer = (fn, ms) => win.setTimeout(fn, ms);
    const clearTimer = (id) => win.clearTimeout(id);

    const st = { press: null, drag: null, suppressUntil: 0 };

    function closest(target, selector) {
      let node = target;
      if (node && typeof node.closest !== "function" && node.parentElement) node = node.parentElement;
      return node && typeof node.closest === "function" ? node.closest(selector) : null;
    }

    const cardsOf = () => (typeof grid.querySelectorAll === "function" ? Array.from(grid.querySelectorAll(".pcard[data-actor]")) : []);

    function classOf(node, method, name) {
      if (node && node.classList && typeof node.classList[method] === "function") node.classList[method](name);
    }

    function prevent(evt) {
      if (evt && typeof evt.preventDefault === "function" && evt.cancelable !== false) evt.preventDefault();
    }

    // ---- the press -------------------------------------------------------------------------------------------------
    function listen(on) {
      const method = on ? "addEventListener" : "removeEventListener";
      if (typeof doc[method] !== "function") return;
      doc[method]("pointermove", onMove);
      doc[method]("pointerup", onUp);
      doc[method]("pointercancel", onCancel);
      doc[method]("keydown", onEscape);
      doc[method]("contextmenu", onContextMenu);
    }

    function endPress() {
      const p = st.press;
      if (!p) return;
      if (p.timer !== null) clearTimer(p.timer);
      listen(false);
      const d = st.drag;
      if (d) {
        if (d.card && d.card.style) d.card.style.transform = "";
        classOf(d.card, "remove", "seat-dragging");
        if (d.to >= 0) classOf(d.cards[d.to], "remove", "seat-target");
        classOf(grid, "remove", "seats-dragging");
      }
      st.press = null;
      st.drag = null;
      onBusy(false);
    }

    function beginDrag() {
      const p = st.press;
      if (!p || st.drag) return;
      const cards = cardsOf();
      const rects = cards.map((c) => (typeof c.getBoundingClientRect === "function" ? c.getBoundingClientRect() : null));
      st.drag = { from: p.index, card: p.card, cards, rects, to: -1 };
      classOf(grid, "add", "seats-dragging");
      classOf(p.card, "add", "seat-dragging");
    }

    function moveDrag(x, y) {
      const d = st.drag;
      const p = st.press;
      if (!d || !p) return;
      if (d.card && d.card.style) d.card.style.transform = `translate(${Math.round(x - p.x)}px, ${Math.round(y - p.y)}px)`;
      const hit = View.seatIndexAt(d.rects, x, y);
      const to = hit >= 0 && hit !== d.from ? hit : -1;
      if (to === d.to) return;
      if (d.to >= 0) classOf(d.cards[d.to], "remove", "seat-target");
      if (to >= 0) classOf(d.cards[to], "add", "seat-target");
      d.to = to;
    }

    function onMove(evt) {
      const p = st.press;
      if (!p || !evt) return;
      if (p.pointerId !== undefined && evt.pointerId !== undefined && evt.pointerId !== p.pointerId) return;
      if (evt.pointerType === "mouse" && evt.buttons === 0) {
        endPress(); // (the button was let go outside the window: nobody told us)
        return;
      }
      const x = Number(evt.clientX) || 0;
      const y = Number(evt.clientY) || 0;
      if (!st.drag) {
        const distance = Math.hypot(x - p.x, y - p.y);
        if (p.touch) {
          if (distance > SEAT_TOUCH_SLOP_PX) endPress(); // a swipe: the page scrolls, nothing is moved
          return;
        }
        if (distance < SEAT_DRAG_PX) return;
        beginDrag();
      }
      moveDrag(x, y);
      prevent(evt);
    }

    function onUp(evt) {
      const p = st.press;
      if (!p) return;
      if (p.pointerId !== undefined && evt && evt.pointerId !== undefined && evt.pointerId !== p.pointerId) return;
      const d = st.drag;
      const from = d ? d.from : -1;
      const to = d ? d.to : -1;
      endPress();
      if (d) {
        st.suppressUntil = Date.now() + SEAT_CLICK_GUARD_MS;
        if (to >= 0 && to !== from) commit(from, to, { keyboard: false });
      }
    }

    function onCancel() {
      if (!st.press) return;
      if (st.drag) st.suppressUntil = Date.now() + SEAT_CLICK_GUARD_MS;
      endPress();
    }

    function onEscape(evt) {
      if (evt && evt.key === "Escape") onCancel();
    }

    function onContextMenu(evt) {
      if (st.press && st.press.touch) prevent(evt); // (the long press of a touch is not the menu of the image under the finger)
    }

    function onTouchMove(evt) {
      if (st.drag) prevent(evt);
    }

    function onDown(evt) {
      if (!evt || (evt.button !== undefined && evt.button !== 0) || evt.isPrimary === false) return;
      const card = closest(evt.target, ".pcard[data-actor]");
      if (!card || closest(evt.target, "[data-mark-toggle]")) return;
      if (!armed()) return; // the game has started / a seat is played / a replay is on screen: the cards are plain buttons
      endPress();
      const index = cardsOf().indexOf(card);
      if (index < 0) return;
      const touch = evt.pointerType === "touch";
      st.press = {
        card,
        index,
        x: Number(evt.clientX) || 0,
        y: Number(evt.clientY) || 0,
        pointerId: evt.pointerId,
        touch,
        timer: null,
      };
      if (touch) {
        st.press.timer = setTimer(() => {
          if (st.press) st.press.timer = null;
          if (st.press && !st.drag) beginDrag();
        }, SEAT_LONG_PRESS_MS);
      }
      listen(true);
      onBusy(true);
    }

    // ---- the keyboard: Alt+← / Alt+→ with the focus on a card ---------------------------------------------------------
    function onKey(evt) {
      if (!evt || !evt.altKey || (evt.key !== "ArrowLeft" && evt.key !== "ArrowRight")) return;
      const card = closest(evt.target, ".pcard[data-actor]");
      if (!card || !armed()) return; // (Alt+← is "back" of the browser: nothing is taken from it while the cards cannot be moved)
      const cards = cardsOf();
      const from = cards.indexOf(card);
      if (from < 0) return;
      prevent(evt);
      const to = View.seatKeyTarget(from, evt.key, cards.length);
      if (to !== null) commit(from, to, { keyboard: true });
    }

    // the browser's own drag of an image (the avatar of a card is an <img>) would take the pointer away: while the cards can be moved it does not start
    grid.addEventListener("dragstart", (evt) => {
      if (armed()) prevent(evt);
    });
    grid.addEventListener("pointerdown", onDown);
    grid.addEventListener("keydown", onKey);
    // a held touch that is being moved must not scroll the page: that needs a listener that may cancel the event - and one that exists BEFORE the finger goes
    // down (a browser decides at touchstart whether a touchmove can be cancelled), so it is registered once, here, and only cancels while a card is carried
    grid.addEventListener("touchmove", onTouchMove, { passive: false });
    // a press that is never let go (the window lost the focus, an alert, the tab went to the background) must not keep the cards frozen
    if (typeof win.addEventListener === "function") win.addEventListener("blur", onCancel);
    if (typeof doc.addEventListener === "function") {
      doc.addEventListener("visibilitychange", () => {
        if (doc.visibilityState === "hidden") onCancel();
      });
    }

    return {
      busy: () => st.press !== null,
      dragging: () => st.drag !== null,
      clickSuppressed: () => Date.now() < st.suppressUntil,
      armed: () => Boolean(armed()),
      cancel: onCancel,
      /** The cards were drawn again (a key move): the focus goes to the card now at `index`, so Alt+← / → can be pressed again. */
      focusIndex(index) {
        const node = cardsOf()[index];
        if (node && typeof node.focus === "function") node.focus();
      },
    };
  }

  root.ArenaLayout = { create, createSeatDrag, NARROW_MAX };
})(typeof window !== "undefined" ? window : globalThis);
