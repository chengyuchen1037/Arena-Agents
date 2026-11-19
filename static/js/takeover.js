/* ==========================================================================
   Arena-Agents v0.13 — playing a seat: the viewpoint bar, the takeover bar, the reshuffle reminder
   Loaded after roster.js / budget.js and before app.js. Exposes window.ArenaTakeoverUI.create(ctx).

   Owns (docs/v0.13-plan.md F1-F5, contract §11.7)
     - the viewpoint bar  [👁 视角 ▾] [切换] [接管]: the drop-down only chooses what to look at, "切换" applies
       it, "接管" (only while the game has not started or is paused, never for "上帝视角") plays the chosen
       seat after a confirmation:                         POST api/takeover {seat}
     - the takeover bar   🎮 正在扮演 3 · Carol [⏸ 暂停 agent] [退出接管]: it replaces the viewpoint bar while a
       seat is played (the viewpoint is locked to it); leaving needs a paused / unstarted / finished game and a
       confirmation:                                       POST api/takeover/release
     - the soft pause (v0.13.1, docs/v0.13.1-plan.md G1-G7, contract §12.2), also part of the takeover bar:
         [⏸ 暂停 agent]   POST api/control/soft-pause   the calls in flight finish, then every agent holds, the
                          game clock stops - the person can still read, write and answer.  Only while the game runs.
         ⏳ 收尾中（还有 N 个回答） → ⏸ agent 已暂停      the two phases (session.soft_pause)
         [▶ 继续]         POST api/control/soft-resume  (also while it is still draining: that cancels it)
         立即暂停          POST api/control/pause         = the top bar's hard pause: cancels the calls in flight
     - the reminder of a seat taken over before the game started: "你已经知道自己的身份，建议点击「重新发牌」"
       + 「重新发牌」 (POST api/control/shuffle-identities). It disappears once the game started, and the
       click handler checks that again (a double click / a second tab must not re-deal a running game).

   Everything it decides comes from the pure functions of view.js (viewpointBar, takeoverBar (+ softPause),
   reshuffleReminder, takeoverAvailability); this file only wires them to the DOM and the API.

   ctx: { api, Core, View, showToast, getSession, getViewpoint, setViewpoint(id), openDialog(options),
          adoptSession(session, opts), refreshState() }
   ========================================================================== */

(function (root) {
  "use strict";

  function create(ctx) {
    const opts = ctx || {};
    const Core = opts.Core || root.ArenaCore;
    const View = opts.View || root.ArenaView;
    const api = opts.api;
    const doc = opts.document || root.document;
    if (!Core || !View || !api || !doc) return null;

    const esc = Core.escapeHtml;
    const showToast = opts.showToast || (() => {});
    const getSession = opts.getSession || (() => null);
    const getViewpoint = opts.getViewpoint || (() => View.GOD);
    const setViewpoint = opts.setViewpoint || (() => {});
    const openDialog = opts.openDialog || (async () => ({ ok: true, values: {} }));
    const adoptSession = opts.adoptSession || (() => {});
    const refreshState = opts.refreshState || (async () => {});
    const isReplaying = opts.isReplaying || (() => false); // a saved game is on screen: nobody can take a seat over
    const $ = (id) => (typeof doc.getElementById === "function" ? doc.getElementById(id) : null);

    const el = {
      bar: $("viewpoint-bar"),
      select: $("select-viewpoint"),
      btnSwitch: $("btn-viewpoint-switch"),
      btnTakeover: $("btn-takeover"),
      takeoverBar: $("takeover-bar"),
      seat: $("takeover-seat"),
      btnRelease: $("btn-release-takeover"),
      softStatus: $("softpause-status"),
      btnSoft: $("btn-softpause"),
      btnSoftNow: $("btn-softpause-now"),
      reminder: $("takeover-reminder"),
      reminderText: $("takeover-reminder-text"),
      btnReshuffle: $("btn-shuffle-identities"),
    };

    const st = {
      draft: null, // what the drop-down shows (chosen, not applied yet)
      applied: null, // the viewpoint the draft was last synchronised with
      optionsHtml: null,
      busy: false, // a takeover / release / reshuffle request is in flight (no double clicks)
    };

    const session = () => getSession() || {};

    function setHidden(node, hidden) {
      if (node) node.hidden = Boolean(hidden);
    }

    function setTitle(node, text) {
      if (node && typeof node.setAttribute === "function") node.setAttribute("title", text || "");
    }

    // -----------------------------------------------------------------------
    // rendering
    // -----------------------------------------------------------------------

    /** The soft-pause part of the takeover bar: the state text, the one button (pause / resume), 立即暂停. */
    function renderSoftPause(sp) {
      if (el.softStatus) {
        setHidden(el.softStatus, !sp.text);
        if (el.softStatus.textContent !== sp.text) el.softStatus.textContent = sp.text;
        setTitle(el.softStatus, sp.statusTitle);
        if (typeof el.softStatus.setAttribute === "function") el.softStatus.setAttribute("data-phase", sp.phase || "");
      }
      if (el.btnSoft) {
        if (el.btnSoft.textContent !== sp.label) el.btnSoft.textContent = sp.label;
        el.btnSoft.disabled = !sp.enabled || st.busy;
        setTitle(el.btnSoft, sp.title);
        if (typeof el.btnSoft.setAttribute === "function") el.btnSoft.setAttribute("data-action", sp.action);
      }
      if (el.btnSoftNow) {
        setHidden(el.btnSoftNow, !sp.showNow);
        el.btnSoftNow.disabled = st.busy;
        setTitle(el.btnSoftNow, sp.nowTitle);
      }
    }

    function render() {
      const s = session();
      const applied = getViewpoint();
      if (el.select) {
        const html = View.viewpointOptions(s)
          .map((o) => `<option value="${esc(o.id)}">${esc(o.label)}</option>`)
          .join("");
        if (html !== st.optionsHtml) {
          st.optionsHtml = html;
          el.select.innerHTML = html;
        }
      }
      // the drop-down keeps a pending choice until the applied viewpoint moves under it (a card's "view as",
      // a new game, a lock) - then it follows
      if (st.draft === null || st.applied !== applied) {
        st.draft = applied;
        st.applied = applied;
      }
      st.draft = View.normalizeViewpoint(s, st.draft);
      const bar = View.viewpointBar(s, st.draft, applied);
      if (el.select && el.select.value !== bar.draft) el.select.value = bar.draft;
      if (el.select && el.select.classList) el.select.classList.toggle("is-player", bar.draft !== View.GOD);
      setHidden(el.bar, bar.locked);
      if (el.btnSwitch) {
        el.btnSwitch.disabled = !bar.canSwitch || st.busy;
        setTitle(el.btnSwitch, bar.switchTitle);
      }
      if (el.btnTakeover) {
        const replaying = Boolean(isReplaying());
        el.btnTakeover.disabled = replaying || !bar.canTakeover || st.busy;
        setTitle(el.btnTakeover, replaying ? "重放中不能接管：请先退出重放" : bar.takeoverTitle);
      }

      const tb = View.takeoverBar(s);
      setHidden(el.takeoverBar, !tb);
      // (the stylesheet frees the phone's first row while a seat is played: no version badge)
      const page = doc.documentElement;
      if (page && typeof page.setAttribute === "function") page.setAttribute("data-takeover", tb ? "1" : "0");
      if (tb) {
        if (el.seat) el.seat.textContent = tb.label;
        if (el.btnRelease) {
          el.btnRelease.disabled = !tb.canRelease || st.busy;
          setTitle(el.btnRelease, tb.releaseTitle);
        }
        renderSoftPause(tb.softPause);
      }
      if (el.takeoverBar && el.takeoverBar.classList && typeof el.takeoverBar.classList.toggle === "function") {
        el.takeoverBar.classList.toggle("is-soft-paused", Boolean(tb && tb.softPause.phase));
      }

      const reminder = View.reshuffleReminder(s);
      setHidden(el.reminder, !reminder.visible);
      if (reminder.visible && el.reminderText) el.reminderText.textContent = reminder.text;
      if (el.btnReshuffle) el.btnReshuffle.disabled = !reminder.visible || st.busy;
    }

    // -----------------------------------------------------------------------
    // actions
    // -----------------------------------------------------------------------

    /** Run one request at a time; the buttons stay disabled while it is in flight. */
    async function exclusive(work) {
      if (st.busy) return false;
      st.busy = true;
      render();
      try {
        return await work();
      } finally {
        st.busy = false;
        render();
      }
    }

    /** "切换": apply the drop-down's choice as the spectator viewpoint (never while a seat is played). */
    function switchViewpoint() {
      const s = session();
      if (View.takeoverSeat(s)) return false;
      const target = View.normalizeViewpoint(s, st.draft);
      if (target === getViewpoint()) return false;
      setViewpoint(target);
      return true;
    }

    /** "接管": play the seat chosen in the drop-down, after a confirmation. */
    function takeover(seatId) {
      return exclusive(async () => {
        if (isReplaying()) {
          showToast("重放中不能接管：请先退出重放");
          return false;
        }
        const seat = seatId || st.draft;
        const check = View.takeoverAvailability(session(), seat);
        if (!check.ok) {
          showToast(check.reason);
          return false;
        }
        const label = View.seatLabel(session(), seat);
        const ask = await openDialog({
          title: `接管 ${label}？`,
          bodyHtml:
            `<p class="dialog-p">接管后，你将以 <strong>${esc(label)}</strong> 的身份参与对局：发言、投票、使用技能；` +
            `这个座位的 agent 会暂停（不再调用模型）。</p>` +
            `<ul class="dialog-list">` +
            `<li>你会看到该座位的视角，视角被锁定；退出接管后恢复。</li>` +
            `<li>主持人和其他玩家都不知道有人类参与。</li>` +
            `<li>发言权时长、请求截止时间仍由主持人决定，超时按规则处理。</li>` +
            `${session().status === "idle" ? "<li>对局还没有开始：接管后你会先看到自己的身份，可以重新发牌。</li>" : ""}` +
            `</ul>`,
          okText: "接管",
          cancelText: "取消",
        });
        if (!ask.ok) return false;
        // the game may have started while the dialog was open
        const again = View.takeoverAvailability(session(), seat);
        if (!again.ok) {
          showToast(again.reason);
          return false;
        }
        try {
          const res = await api.post("api/takeover", { seat });
          if (res && res.session) adoptSession(res.session, { force: false });
          showToast(`已接管 ${label}：视角已锁定为这个座位`);
          return true;
        } catch (err) {
          showToast(err.message);
          await refreshState();
          return false;
        }
      });
    }

    /** "退出接管": give the seat back to its agent (only while the game does not run), after a confirmation. */
    function release() {
      return exclusive(async () => {
        const s = session();
        const bar = View.takeoverBar(s);
        if (!bar) return false;
        if (!bar.canRelease) {
          showToast(bar.releaseTitle);
          return false;
        }
        const ask = await openDialog({
          title: `退出接管 ${bar.label}？`,
          bodyHtml:
            `<p class="dialog-p">退出后，<strong>${esc(bar.label)}</strong> 交还给它的 agent。</p>` +
            `<ul class="dialog-list">` +
            `<li>继续对局后，这个 agent 会重新读到完整的对局记录——包括你以它的名义说过的话。</li>` +
            `<li>它不会被告知有人代打过。</li>` +
            `<li>视角恢复成你接管之前选的视角。</li>` +
            `</ul>`,
          okText: "退出接管",
          cancelText: "继续扮演",
          danger: true,
        });
        if (!ask.ok) return false;
        if (session().status === "running") {
          showToast("对局进行中不能退出接管：请先点「暂停」");
          return false;
        }
        try {
          const res = await api.post("api/takeover/release");
          if (res && res.session) adoptSession(res.session, { force: false });
          showToast(`已退出接管：${bar.label} 交还给 agent`);
          return true;
        } catch (err) {
          showToast(err.message);
          await refreshState();
          return false;
        }
      });
    }

    /**
     * 「重新发牌」: re-deal every seat (POST api/control/shuffle-identities) - only before the game starts.
     * The state is checked again right here, not only when the button was drawn: a double click, a second
     * tab or a slow render must never re-deal a game that has started (the server refuses it as well).
     */
    function reshuffle() {
      return exclusive(async () => {
        if (!View.reshuffleReminder(session()).visible) {
          showToast("对局已经开始，不能再重新发牌");
          return false;
        }
        try {
          const res = await api.post("api/control/shuffle-identities");
          if (res && res.session) adoptSession(res.session, { force: true });
          showToast("已重新发牌：请看看你新的身份");
          return true;
        } catch (err) {
          showToast(err.message);
          await refreshState(); // (a refused re-deal means the game has started: the reminder must go)
          return false;
        }
      });
    }

    // -----------------------------------------------------------------------
    // the soft pause (v0.13.1)
    // -----------------------------------------------------------------------

    /** The soft-pause endpoints answer with the whole session: the page adopts it (the stream may be ahead: it decides). */
    function adopt(res) {
      if (res && res.session) adoptSession(res.session, { force: false });
    }

    /**
     * 「⏸ 暂停 agent」: let the model calls in flight finish, then hold every agent (the moderator too); the game clock
     * stops now.  Only while a seat is played and the game runs - checked again here, not only when the button was drawn.
     * The server's refusal (409, a sentence with its full stop) is shown as it is.
     */
    function softPause() {
      return exclusive(async () => {
        const s = session();
        if (!View.takeoverSeat(s)) return false;
        const sp = View.softPauseControl(s);
        if (sp.action !== "pause") return false; // (it is soft-paused already)
        if (!sp.enabled) {
          showToast(sp.title);
          return false;
        }
        try {
          adopt(await api.post("api/control/soft-pause"));
          showToast("已发起软暂停：已发出的回答会照常做完，计时已冻结，你仍可阅读、发言和作答");
          return true;
        } catch (err) {
          showToast(err.message);
          await refreshState();
          return false;
        }
      });
    }

    /** 「▶ 继续」: the agents go on (they first merge what arrived meanwhile into one message); also cancels a draining pause. */
    function softResume() {
      return exclusive(async () => {
        const s = session();
        if (!View.takeoverSeat(s)) return false;
        if (!View.softPauseOf(s).active) {
          showToast("现在没有处于软暂停");
          return false;
        }
        try {
          adopt(await api.post("api/control/soft-resume"));
          showToast("agent 已继续：先合并你这段时间的发言和作答，再接着行动");
          return true;
        } catch (err) {
          showToast(err.message);
          await refreshState();
          return false;
        }
      });
    }

    /** 「立即暂停」: the top bar's hard pause (it cancels the calls that are still in flight, their tokens are wasted). */
    function hardPause() {
      return exclusive(async () => {
        const s = session();
        if (s.status !== "running") {
          showToast(`${View.notRunningText(s.status)}，不需要再暂停`);
          return false;
        }
        try {
          adopt(await api.post("api/control/pause"));
          showToast("已立即暂停：在途的模型调用已取消，所有 agent 休眠，计时冻结");
          return true;
        } catch (err) {
          showToast(err.message);
          await refreshState();
          return false;
        }
      });
    }

    // -----------------------------------------------------------------------
    // events
    // -----------------------------------------------------------------------

    function on(node, type, handler) {
      if (node && typeof node.addEventListener === "function") node.addEventListener(type, handler);
    }

    on(el.select, "change", () => {
      st.draft = el.select.value;
      render();
    });
    on(el.btnSwitch, "click", () => {
      switchViewpoint();
      render();
    });
    on(el.btnTakeover, "click", () => {
      takeover();
    });
    on(el.btnRelease, "click", () => {
      release();
    });
    on(el.btnReshuffle, "click", () => {
      reshuffle();
    });
    on(el.btnSoft, "click", () => {
      // one button, two meanings: it says 「⏸ 暂停 agent」 until a soft pause exists, then 「▶ 继续」
      if (View.softPauseOf(session()).active) softResume();
      else softPause();
    });
    on(el.btnSoftNow, "click", () => {
      hardPause();
    });

    return {
      render,
      switchViewpoint,
      takeover,
      release,
      reshuffle,
      softPause,
      softResume,
      hardPause,
      draft: () => st.draft,
      _state: st,
    };
  }

  root.ArenaTakeoverUI = { create };
})(typeof window !== "undefined" ? window : this);
