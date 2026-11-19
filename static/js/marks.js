/* ==========================================================================
   Arena-Agents v0.13.1 — colour marks on the players (G16-G20, contract §12.7)
   Loaded before takeover.js / composer.js and app.js.  Exposes window.ArenaMarks (also module.exports):
   pure helpers (the 8 colours, the localStorage format) and create(ctx), the page's part.

   While a seat is played the person can mark every OTHER player with one of 8 colours (no moderator, no text, no
   meaning prescribed: the person decides what a colour stands for, G17).  A mark is shown
     - on the player card: a coloured stripe along its top edge and a dot in its top-right corner (the dot is the
       button that opens the palette: 8 colours + 清除),
     - in the feed before the speaker's name, in the private-chat list, on the request card's recipient rows and in
       the detail panel (its palette sits in the head; roster.js asks for it through ctx.markHeadHtml).
   The 8 colours also differ in the SHAPE of their dot (circle, square, triangle ...), for colour-blind people.

   Where they live (G19): the browser only - localStorage["arena_seat_marks_v1"] =
       {"<session_id>": {"updated_at": <seconds>, "marks": {"N4": 2, "N7": 5}}}        (colour = 0-7)
   at most the 50 most recently marked games; a deleted game takes its marks along (forget()); other tabs follow
   through the `storage` event.  They are not part of the saved game, of an archive, of a replay, nor of
   anything an agent is told.  When they show (G20): editable while a seat is played; after giving the seat back the
   same game still shows them on this browser (read-only); a new game starts blank; a replay shows none.

   ctx (create): { View, getSession, getViewpoint, isReplaying(), onChange(), storage?, window?, document?, now()? }
   ========================================================================== */

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.ArenaMarks = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const STORAGE_KEY = "arena_seat_marks_v1";
  const MAX_GAMES = 50;
  const MODERATOR_ID = "moderator";

  /**
   * The palette.  `shape` is the form of the dot (CSS: .mark-dot.mark-cN); colour and form always go together, so
   * the 8 marks stay apart for a person who cannot tell two of the colours apart.  `name` is only a tooltip.
   */
  const COLORS = Object.freeze([
    Object.freeze({ id: 0, name: "红", shape: "circle" }),
    Object.freeze({ id: 1, name: "橙", shape: "square" }),
    Object.freeze({ id: 2, name: "黄", shape: "triangle" }),
    Object.freeze({ id: 3, name: "绿", shape: "diamond" }),
    Object.freeze({ id: 4, name: "青", shape: "inverted-triangle" }),
    Object.freeze({ id: 5, name: "蓝", shape: "hexagon" }),
    Object.freeze({ id: 6, name: "紫", shape: "star" }),
    Object.freeze({ id: 7, name: "灰", shape: "cross" }),
  ]);

  // -------------------------------------------------------------------------
  // the stored form (pure)
  // -------------------------------------------------------------------------

  const isObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const isColor = (v) => typeof v === "number" && Number.isInteger(v) && v >= 0 && v < COLORS.length;
  // (a plain pattern also keeps "__proto__" and friends out of the objects below)
  const isSessionId = (v) => typeof v === "string" && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(v);
  const isPlayerId = (v) => typeof v === "string" && /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(v) && v !== MODERATOR_ID && v !== "engine";

  /** The text of the storage key -> {sessionId: {updated_at, marks}}; whatever is not that shape is dropped. */
  function parseStore(text) {
    let raw;
    try {
      raw = JSON.parse(text);
    } catch {
      return {};
    }
    if (!isObject(raw)) return {};
    const out = {};
    for (const id of Object.keys(raw)) {
      const entry = raw[id];
      if (!isSessionId(id) || !isObject(entry) || !isObject(entry.marks)) continue;
      const marks = {};
      for (const pid of Object.keys(entry.marks)) {
        if (isPlayerId(pid) && isColor(entry.marks[pid])) marks[pid] = entry.marks[pid];
      }
      const at = Number(entry.updated_at);
      out[id] = { updated_at: Number.isFinite(at) ? at : 0, marks };
    }
    return out;
  }

  /** Keep the `max` most recently marked games (a tie goes to the one stored later). */
  function pruneStore(store, max = MAX_GAMES) {
    const ids = Object.keys(store);
    if (ids.length <= max) return store;
    const keep = new Set(
      ids
        .map((id, i) => ({ id, at: store[id].updated_at, i }))
        .sort((a, b) => b.at - a.at || b.i - a.i)
        .slice(0, max)
        .map((x) => x.id)
    );
    const out = {};
    for (const id of ids) if (keep.has(id)) out[id] = store[id];
    return out;
  }

  /** The marks of one game ({} for an unknown one): a copy. */
  function marksOf(store, sessionId) {
    const entry = isSessionId(sessionId) && Object.prototype.hasOwnProperty.call(store, sessionId) ? store[sessionId] : null;
    return entry ? { ...entry.marks } : {};
  }

  /** A new store with `playerId` marked `color` (null / undefined: cleared) in game `sessionId`; `nowSec` = its time. */
  function withMark(store, sessionId, playerId, color, nowSec) {
    if (!isSessionId(sessionId) || !isPlayerId(playerId)) return store;
    const clearing = color === null || color === undefined;
    if (!clearing && !isColor(color)) return store;
    const marks = marksOf(store, sessionId);
    if (clearing) delete marks[playerId];
    else marks[playerId] = color;
    const out = { ...store };
    if (Object.keys(marks).length === 0) delete out[sessionId];
    else out[sessionId] = { updated_at: Math.floor(Number.isFinite(Number(nowSec)) ? Number(nowSec) : 0), marks };
    return pruneStore(out);
  }

  /** A new store without the marks of game `sessionId` (a deleted game takes them along). */
  function withoutGame(store, sessionId) {
    if (!isSessionId(sessionId) || !Object.prototype.hasOwnProperty.call(store, sessionId)) return store;
    const out = { ...store };
    delete out[sessionId];
    return out;
  }

  // -------------------------------------------------------------------------
  // markup (pure)
  // -------------------------------------------------------------------------

  /** The little coloured form ("" for no / an unknown colour).  Its tooltip is the colour's name - nothing else. */
  function dotHtml(color, extraClass) {
    if (!isColor(color)) return "";
    const c = COLORS[color];
    return `<span class="mark-dot mark-c${c.id}${extraClass ? ` ${extraClass}` : ""}" role="img" aria-label="${c.name}色标记" title="${c.name}"></span>`;
  }

  /** An id as it goes into an attribute: the ids of the game are plain (N4), anything odd loses its odd characters. */
  const safeId = (id) => String(id === undefined || id === null ? "" : id).replace(/[^A-Za-z0-9._-]/g, "");

  /** The palette of one player: the 8 colours (the current one pressed) and 清除.  No text but the colour names. */
  function paletteHtml(actorId, current) {
    const id = safeId(actorId);
    const swatches = COLORS.map(
      (c) =>
        `<button type="button" class="mark-swatch${current === c.id ? " selected" : ""}" data-mark-set="${c.id}" data-mark-actor="${id}"` +
        ` aria-pressed="${current === c.id ? "true" : "false"}" aria-label="${c.name}" title="${c.name}">` +
        `<span class="mark-dot mark-c${c.id}" aria-hidden="true"></span></button>`
    ).join("");
    const clear =
      `<button type="button" class="mark-clear" data-mark-clear data-mark-actor="${id}" aria-label="清除" title="清除"` +
      `${isColor(current) ? "" : " disabled"}>✕</button>`;
    return `<div class="mark-palette" role="group" aria-label="颜色标记" data-actor="${id}">${swatches}${clear}</div>`;
  }

  // -------------------------------------------------------------------------
  // the page's part
  // -------------------------------------------------------------------------

  function create(ctx) {
    const opts = ctx || {};
    const View = opts.View || (typeof globalThis !== "undefined" && globalThis.ArenaView) || null;
    const doc = opts.document || (typeof document !== "undefined" ? document : null);
    const win = opts.window || (typeof window !== "undefined" ? window : null);
    if (!View) return null;
    const getSession = opts.getSession || (() => null);
    const getViewpoint = opts.getViewpoint || (() => View.GOD);
    const isReplaying = opts.isReplaying || (() => false);
    const onChange = opts.onChange || (() => {});
    const now = opts.now || (() => Date.now() / 1000);
    const $ = (id) => (doc && typeof doc.getElementById === "function" ? doc.getElementById(id) : null);

    let storage = opts.storage;
    if (storage === undefined) {
      try {
        storage = win ? win.localStorage : null;
      } catch {
        storage = null; // (blocked storage: the marks live as long as the page)
      }
    }

    const el = { pop: $("mark-pop"), detail: $("detail-panel") };
    const st = {
      memory: null, // the stored text when there is no usable localStorage
      store: {},
      popFor: null, // the player whose palette is open (the floating one)
    };

    function readText() {
      if (storage) {
        try {
          return storage.getItem(STORAGE_KEY); // (null: nothing stored - someone cleared it - and that is the truth)
        } catch {
          /* blocked: fall back to what this page remembers */
        }
      }
      return st.memory;
    }

    function writeText(text) {
      st.memory = text;
      try {
        if (storage) storage.setItem(STORAGE_KEY, text);
      } catch {
        /* storage full / blocked: the marks stay in memory */
      }
    }

    const reload = () => {
      st.store = parseStore(readText());
    };
    reload();

    const session = () => getSession() || {};
    const sid = () => String(session().session_id || "");
    const seat = () => View.takeoverSeat(session());

    /** The person may change the marks: a seat is played, the viewpoint is its own, no replay is on screen (G20). */
    function editable() {
      if (isReplaying()) return false;
      const s = seat();
      return Boolean(s) && getViewpoint() === s && sid() !== "";
    }

    /** Every mark of this game as shown: none in a replay, none on the played seat itself (it cannot be marked). */
    function marks() {
      if (isReplaying()) return {};
      const all = marksOf(st.store, sid());
      const s = seat();
      if (s) delete all[s];
      return all;
    }

    function colorOf(id) {
      const m = marks();
      return Object.prototype.hasOwnProperty.call(m, id) && isColor(m[id]) ? m[id] : null;
    }

    /** Can this player be marked?  Players only: not the moderator, not the seat that is played. */
    function canMark(id) {
      return Boolean(id) && id !== MODERATOR_ID && Boolean(View.participantOf(session(), id)) && id !== seat();
    }

    /**
     * What the cached HTML that shows marks must be keyed by (app.js: computeRosterSig): the marks, and whether the
     * cards offer them as buttons.  Constant (nearly empty) for a game nobody ever marked.
     */
    function signature() {
      if (isReplaying()) return "";
      const m = marks();
      return `${editable() ? "e" : "r"}${Object.keys(m)
        .sort()
        .map((k) => `${k}=${m[k]}`)
        .join(",")}`;
    }

    // ---- writing --------------------------------------------------------------------------------
    function commit(next) {
      st.store = next;
      writeText(JSON.stringify(next));
      onChange();
    }

    /** Mark `id` with `color` (0-7); null clears.  Only while a seat is played, never the seat itself. */
    function set(id, color) {
      if (!editable() || !canMark(id)) return false;
      if (color !== null && !isColor(color)) return false;
      reload(); // (another tab may have written since: change what is stored now, not what this page remembers)
      commit(withMark(st.store, sid(), id, color, now()));
      return true;
    }

    const clear = (id) => set(id, null);

    /** A deleted game takes its marks along (G19). */
    function forget(sessionId) {
      reload();
      const next = withoutGame(st.store, String(sessionId));
      if (next === st.store) return false;
      commit(next);
      return true;
    }

    // ---- what the renderers put into their HTML ------------------------------------------------------
    /** The dot before a name (feed, private-chat list, request rows): "" for an unmarked player. */
    function nameDotHtml(id) {
      return dotHtml(colorOf(id));
    }

    /**
     * The corner of a player card: the button that opens the palette while a seat is played (an empty ring when the
     * player has no mark yet), a plain dot afterwards (read-only), nothing for an unmarked player nobody may mark.
     */
    function cardMarkHtml(id) {
      const color = colorOf(id);
      if (editable() && canMark(id)) {
        return (
          `<span class="pcard-mark${color === null ? " is-empty" : ""}" data-mark-toggle="${safeId(id)}"` +
          ` role="button" tabindex="0" aria-haspopup="true" aria-label="颜色标记" title="颜色标记">` +
          `${color === null ? `<span class="mark-ring" aria-hidden="true"></span>` : dotHtml(color)}</span>`
        );
      }
      return color === null ? "" : `<span class="pcard-mark readonly">${dotHtml(color)}</span>`;
    }

    /** The top stripe of a marked card: the class that gives the card its colour ("" without a mark). */
    function cardClass(id) {
      const color = colorOf(id);
      return color === null ? "" : `has-mark mark-c${color}`;
    }

    /** The row of the detail panel's head (roster.js: ctx.markHeadHtml): the palette, or the dot once it is read-only. */
    function headHtml(id) {
      if (isReplaying() || id === MODERATOR_ID || !View.participantOf(session(), id)) return "";
      const color = colorOf(id);
      if (editable() && canMark(id)) return `<div class="detail-marks" data-actor="${safeId(id)}">${paletteHtml(id, color)}</div>`;
      return color === null ? "" : `<div class="detail-marks readonly" data-actor="${safeId(id)}">${dotHtml(color)}</div>`;
    }

    // ---- the floating palette (a card's dot) --------------------------------------------------------
    function place(anchor) {
      if (!el.pop || !anchor || typeof anchor.getBoundingClientRect !== "function") return;
      const r = anchor.getBoundingClientRect();
      const vw = Number(win && win.innerWidth) || 1280;
      const vh = Number(win && win.innerHeight) || 800;
      const pw = Number(el.pop.offsetWidth) || 220;
      const ph = Number(el.pop.offsetHeight) || 96;
      const left = Math.min(Math.max(8, Math.round(r.right - pw)), Math.max(8, vw - pw - 8));
      let top = Math.round(r.bottom + 6);
      if (top + ph > vh - 8) top = Math.max(8, Math.round(r.top - ph - 6)); // no room below: above the dot
      el.pop.style.left = `${left}px`;
      el.pop.style.top = `${top}px`;
    }

    function openPalette(id, anchor) {
      if (!el.pop || !editable() || !canMark(id)) return false;
      st.popFor = id;
      el.pop.innerHTML = paletteHtml(id, colorOf(id));
      if (typeof el.pop.setAttribute === "function") el.pop.setAttribute("data-actor", id); // (roster.js: a click in here is not "outside")
      el.pop.hidden = false;
      place(anchor);
      return true;
    }

    function closePalette() {
      st.popFor = null;
      if (el.pop && !el.pop.hidden) {
        el.pop.hidden = true;
        el.pop.innerHTML = "";
      }
    }

    function togglePalette(id, anchor) {
      if (st.popFor === id && el.pop && !el.pop.hidden) {
        closePalette();
        return false;
      }
      return openPalette(id, anchor);
    }

    /** After every render: an open palette that is no longer allowed (the seat was given back ...) closes, the rest follows the marks. */
    function sync() {
      if (st.popFor === null) return;
      if (!editable() || !canMark(st.popFor)) closePalette();
      else if (el.pop) el.pop.innerHTML = paletteHtml(st.popFor, colorOf(st.popFor));
    }

    // ---- events -------------------------------------------------------------------------------------
    function on(node, type, handler) {
      if (node && typeof node.addEventListener === "function") node.addEventListener(type, handler);
    }

    function closestFrom(target, selector) {
      let node = target;
      if (node && typeof node.closest !== "function" && node.parentElement) node = node.parentElement;
      return node && typeof node.closest === "function" ? node.closest(selector) : null;
    }

    /** A click in a palette (the floating one, or the detail panel's): colour -> mark, 清除 -> unmark. */
    function paletteClick(evt) {
      const target = evt && evt.target;
      const swatch = closestFrom(target, "[data-mark-set]");
      if (swatch) {
        const done = set(swatch.getAttribute("data-mark-actor"), Number(swatch.getAttribute("data-mark-set")));
        if (done) closePalette();
        return done;
      }
      const wipe = closestFrom(target, "[data-mark-clear]");
      if (wipe) {
        const done = clear(wipe.getAttribute("data-mark-actor"));
        if (done) closePalette();
        return done;
      }
      return false;
    }

    on(el.pop, "click", paletteClick);
    on(el.detail, "click", paletteClick);

    // another tab changed the marks (the `storage` event never fires in the tab that wrote)
    on(win, "storage", (evt) => {
      if (evt && evt.key !== null && evt.key !== undefined && evt.key !== STORAGE_KEY) return;
      reload();
      onChange();
    });

    if (doc && typeof doc.addEventListener === "function") {
      doc.addEventListener("click", (evt) => {
        if (st.popFor === null) return;
        const path = evt && typeof evt.composedPath === "function" ? evt.composedPath() : [];
        const inside = path.length
          ? path.some((n) => n && typeof n.matches === "function" && (n.matches("#mark-pop") || n.matches("[data-mark-toggle]")))
          : Boolean(closestFrom(evt && evt.target, "#mark-pop") || closestFrom(evt && evt.target, "[data-mark-toggle]"));
        if (!inside) closePalette();
      });
      doc.addEventListener("keydown", (evt) => {
        if (!evt || evt.key !== "Escape" || st.popFor === null) return;
        closePalette();
        // (the palette answers Escape first: the detail panel behind it stays)
        if (typeof evt.stopImmediatePropagation === "function") evt.stopImmediatePropagation();
      });
    }

    return {
      editable,
      marks,
      colorOf,
      canMark,
      signature,
      set,
      clear,
      forget,
      reload,
      nameDotHtml,
      cardMarkHtml,
      cardClass,
      headHtml,
      openPalette,
      closePalette,
      togglePalette,
      sync,
      isPaletteOpen: () => st.popFor !== null,
      _state: st,
    };
  }

  return {
    STORAGE_KEY,
    MAX_GAMES,
    COLORS,
    isColor,
    parseStore,
    pruneStore,
    marksOf,
    withMark,
    withoutGame,
    dotHtml,
    paletteHtml,
    safeId,
    create,
  };
});
