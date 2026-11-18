/* ==========================================================================
   Arena-Agents v0.13 — replaying a saved game (F36)
   Loaded after history.js and before app.js. Exposes window.ArenaReplayUI.create(ctx) (plus pure helpers).

   The server turns the ledger of a saved game into the spectator events of the live stream
   (app/game/replay.py, contract section 11.8): {game, initial, events: [{seq, gt, type, data}], duration_gt}.
   This module plays them back through the ordinary client store (store.js) - into a session of its OWN: while
   a replay runs, app.js shows that session instead of the live one and keeps the live game's events aside
   (state.replay.live), so leaving the replay shows the game exactly as it is now.

   Time is the game clock (`gt`): playback waits gt-difference / speed between two steps.  A step is all the
   events of one ledger entry (they are applied together).  Two things make a replay watchable:
     * "跳过空闲": a gap longer than IDLE_GAP_S game seconds takes IDLE_COMPRESSED_S instead;
     * MIN_GAP_S: two steps are never closer than this on screen (a scripted demo game has almost no game time).
   The seek bar counts steps; going back rebuilds the session from `initial` in one synchronous loop.

   ctx: { Core, Store, showToast,
          enter(session)      show this session instead of the live one (the first call stashes the live one),
          leave()             show the live session again,
          render(dirty), renderAll(), setClock(gt), onMessage(msg), onFlash(flash) }
   ========================================================================== */

(function (root) {
  "use strict";

  const SPEEDS = [0.5, 1, 2, 4, 8];
  const IDLE_GAP_S = 5;
  const IDLE_COMPRESSED_S = 0.5;
  const MIN_GAP_S = 0.12;

  const clone = (v) => JSON.parse(JSON.stringify(v));

  /** Group the events by ledger entry: [{seq, gt, from, to}] (events [from, to) belong to one step). */
  function buildSteps(events) {
    const steps = [];
    let current = null;
    (Array.isArray(events) ? events : []).forEach((ev, i) => {
      if (current && current.seq === ev.seq) {
        current.to = i + 1;
      } else {
        current = { seq: ev.seq, gt: Number(ev.gt) || 0, from: i, to: i + 1 };
        steps.push(current);
      }
    });
    return steps;
  }

  /** How long (ms) to wait between two steps at `prevGt` and `nextGt` (game seconds). */
  function stepDelayMs(prevGt, nextGt, speed, skipIdle) {
    const gap = Math.max(0, Number(nextGt) - Number(prevGt) || 0);
    let wall = gap;
    if (skipIdle && gap > IDLE_GAP_S) wall = IDLE_COMPRESSED_S;
    wall = Math.max(wall, MIN_GAP_S);
    const s = Number(speed) > 0 ? Number(speed) : 1;
    return Math.round((wall / s) * 1000);
  }

  function create(ctx) {
    const opts = ctx || {};
    const Core = opts.Core || root.ArenaCore;
    const Store = opts.Store || root.ArenaStore;
    const doc = opts.document || root.document;
    if (!Core || !Store || !doc) return null;
    const showToast = opts.showToast || (() => {});
    const $ = (id) => (typeof doc.getElementById === "function" ? doc.getElementById(id) : null);

    const el = {
      bar: $("replay-bar"),
      title: $("replay-title"),
      restart: $("btn-replay-restart"),
      play: $("btn-replay-play"),
      speed: $("select-replay-speed"),
      skip: $("chk-replay-skip"),
      seek: $("replay-seek"),
      time: $("replay-time"),
      exit: $("btn-exit-replay"),
    };
    const st = {
      active: false,
      data: null,
      events: [],
      steps: [],
      duration: 0,
      title: "",
      index: 0, // steps applied
      playing: false,
      speed: 1,
      skipIdle: true,
      timer: null,
      session: null,
      pendingSeek: null,
      seekQueued: false,
    };

    const setTimer = (fn, ms) => (opts.setTimeout || root.setTimeout).call(root, fn, ms);
    const clearTimer = () => {
      if (st.timer !== null) {
        (opts.clearTimeout || root.clearTimeout).call(root, st.timer);
        st.timer = null;
      }
    };
    const clockText = (gt) => Core.formatCountdown(gt);
    const currentGt = () => (st.index > 0 ? st.steps[st.index - 1].gt : 0);

    // -----------------------------------------------------------------------
    // the replayed session
    // -----------------------------------------------------------------------

    /** What marks a session as a replay (the top bar, the takeover button and the controls read it). */
    function decorate(session, gt) {
      session.replay = { title: st.title, session_id: String(st.data.game ? st.data.game.session_id : ""), duration_gt: st.duration };
      session.active_status_text = `重放中 · ${clockText(gt)} / ${clockText(st.duration)}`;
      session.clock = gt;
    }

    function freshSession() {
      const session = Store.normalizeSession(clone(st.data.initial));
      decorate(session, 0);
      return session;
    }

    /** Apply the events of steps [from, to) to `session`; `live` also reports messages / flashes (playback only). */
    function applyRange(session, from, to, dirty, live) {
      for (let i = from; i < to; i++) {
        const step = st.steps[i];
        for (let k = step.from; k < step.to; k++) {
          const ev = st.events[k];
          const res = Store.applyEvent(session, { id: k + 1, type: ev.type, data: ev.data }, { clockNow: ev.gt });
          if (dirty && res.dirty) Store.mergeDirty(dirty, res.dirty);
          if (live) {
            if (res.message && opts.onMessage) opts.onMessage(res.message);
            if (res.flash && opts.onFlash) opts.onFlash(res.flash);
          }
        }
        session.seq = Math.max(Number(session.seq) || 0, Number(step.seq) || 0);
      }
      decorate(session, to > 0 ? st.steps[to - 1].gt : 0);
    }

    // -----------------------------------------------------------------------
    // the bar
    // -----------------------------------------------------------------------

    function syncControls() {
      if (el.bar) el.bar.hidden = !st.active;
      if (el.title) el.title.textContent = st.title ? `「${st.title}」` : "";
      if (el.play) {
        el.play.textContent = st.playing ? "⏸" : "▶";
        if (typeof el.play.setAttribute === "function") {
          el.play.setAttribute("aria-label", st.playing ? "暂停" : "播放");
          el.play.setAttribute("title", st.playing ? "暂停" : st.index >= st.steps.length && st.steps.length ? "从头再播" : "播放");
        }
        el.play.disabled = !st.active || st.steps.length === 0;
      }
      if (el.restart) el.restart.disabled = !st.active || st.index === 0;
      if (el.seek) {
        el.seek.max = String(st.steps.length);
        el.seek.value = String(st.index);
        el.seek.disabled = !st.active || st.steps.length === 0;
      }
      if (el.time) el.time.textContent = `${clockText(currentGt())} / ${clockText(st.duration)}`;
    }

    // -----------------------------------------------------------------------
    // playback
    // -----------------------------------------------------------------------

    function scheduleNext() {
      clearTimer();
      if (!st.active || !st.playing || st.index >= st.steps.length) return;
      const prev = st.index > 0 ? st.steps[st.index - 1].gt : 0;
      const ms = stepDelayMs(prev, st.steps[st.index].gt, st.speed, st.skipIdle);
      st.timer = setTimer(() => {
        st.timer = null;
        advance();
      }, ms);
    }

    function advance() {
      if (!st.active || st.index >= st.steps.length) return;
      const dirty = Store.newDirty(false);
      applyRange(st.session, st.index, st.index + 1, dirty, true);
      st.index += 1;
      if (st.index >= st.steps.length) st.playing = false;
      afterChange(dirty);
      scheduleNext();
    }

    function afterChange(dirty) {
      if (opts.setClock) opts.setClock(currentGt());
      syncControls();
      if (opts.render) opts.render({ ...dirty, topbar: true });
    }

    /** Jump to "`target` steps are applied" (a back-jump rebuilds the session from the start). */
    function seekTo(target) {
      if (!st.active) return;
      const to = Math.max(0, Math.min(st.steps.length, Math.round(Number(target) || 0)));
      if (to === st.index) {
        syncControls();
        return;
      }
      if (to < st.index) {
        const session = freshSession();
        applyRange(session, 0, to, null, false);
        st.session = session;
        st.index = to;
        if (opts.setClock) opts.setClock(currentGt());
        syncControls();
        if (opts.enter) opts.enter(session);
        return;
      }
      applyRange(st.session, st.index, to, null, false);
      st.index = to;
      if (opts.setClock) opts.setClock(currentGt());
      syncControls();
      if (opts.renderAll) opts.renderAll();
    }

    function setPlaying(on) {
      if (!st.active) return;
      if (on && st.index >= st.steps.length && st.steps.length) seekTo(0); // "play" at the end plays again
      st.playing = Boolean(on) && st.steps.length > 0;
      syncControls();
      scheduleNext();
    }

    // -----------------------------------------------------------------------
    // start / leave
    // -----------------------------------------------------------------------

    /** Start replaying `data` (the answer of GET api/games/{id}/replay); `game` is its row of the history list. */
    function start(data, game) {
      if (!data || typeof data !== "object" || !data.initial || !Array.isArray(data.events)) {
        showToast("重放数据无效");
        return false;
      }
      clearTimer();
      st.data = data;
      st.events = data.events;
      st.steps = buildSteps(st.events);
      const lastGt = st.steps.length ? st.steps[st.steps.length - 1].gt : 0;
      st.duration = Math.max(Number(data.duration_gt) || 0, lastGt);
      const g = data.game || {};
      st.title = String((game && (game.scenario_title || game.title)) || g.scenario_title || g.title || g.session_id || "");
      st.index = 0;
      st.speed = el.speed ? Number(el.speed.value) || 1 : st.speed;
      st.skipIdle = el.skip ? Boolean(el.skip.checked) : st.skipIdle;
      st.session = freshSession();
      st.active = true;
      st.playing = st.steps.length > 0;
      if (opts.setClock) opts.setClock(0);
      syncControls();
      if (opts.enter) opts.enter(st.session);
      scheduleNext();
      return true;
    }

    function exit() {
      if (!st.active) return;
      clearTimer();
      st.active = false;
      st.playing = false;
      st.session = null;
      st.data = null;
      st.events = [];
      st.steps = [];
      st.index = 0;
      syncControls();
      if (opts.leave) opts.leave();
    }

    // -----------------------------------------------------------------------
    // wiring
    // -----------------------------------------------------------------------

    function on(node, type, handler) {
      if (node && typeof node.addEventListener === "function") node.addEventListener(type, handler);
    }

    on(el.play, "click", () => setPlaying(!st.playing));
    on(el.restart, "click", () => {
      clearTimer();
      st.playing = false;
      seekTo(0);
      syncControls();
    });
    on(el.speed, "change", () => {
      st.speed = Number(el.speed.value) || 1;
      scheduleNext(); // the wait that is running is replaced by one at the new speed
    });
    on(el.skip, "change", () => {
      st.skipIdle = Boolean(el.skip.checked);
      scheduleNext();
    });
    on(el.seek, "input", () => {
      st.pendingSeek = Number(el.seek.value);
      const raf = opts.requestAnimationFrame || root.requestAnimationFrame;
      const apply = () => {
        st.seekQueued = false;
        const value = st.pendingSeek;
        st.pendingSeek = null;
        if (value === null) return;
        seekTo(value);
        scheduleNext(); // keep playing from the new position
      };
      if (typeof raf === "function") {
        if (!st.seekQueued) {
          st.seekQueued = true;
          raf.call(root, apply);
        }
      } else {
        apply();
      }
    });
    on(el.exit, "click", exit);

    return {
      start,
      exit,
      seekTo,
      setPlaying,
      isActive: () => st.active,
      isPlaying: () => st.playing,
      _state: st,
    };
  }

  root.ArenaReplayUI = { create, buildSteps, stepDelayMs, SPEEDS, IDLE_GAP_S, IDLE_COMPRESSED_S, MIN_GAP_S };
  if (typeof module !== "undefined" && module.exports) module.exports = root.ArenaReplayUI;
})(typeof window !== "undefined" ? window : globalThis);
