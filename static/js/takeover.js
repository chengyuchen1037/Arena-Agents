/* ==========================================================================
   Arena-Agents v0.14 — playing a seat: the viewpoint label and its menu, the takeover bar
   Loaded after roster.js / budget.js and before app.js. Exposes window.ArenaTakeoverUI.create(ctx).

   Owns (docs/v0.14-plan.md H27, H52; docs/v0.13-plan.md F1-F4, contract §11.7 / §13.10)
     - the viewpoint label (H27)    [👁 全局视角 ▾] · [👁 Alice视角 ▾] · [🎮 Alice视角（接管） ▾]: a pure front-end state, worked out from the
       viewpoint that was chosen and `takeover.seat`.  A click opens its menu:
           全局视角
           1 · Alice   [观看] [接管]         "以 X 的视角观看" / "接管 X" for every player
           …
           退出接管（3 · Carol）              only while a seat is played
       A choice is applied at once ("观看" only changes what the page shows; nothing is requested from the server).  "接管" (only while the
       game has not started or is paused, never twice) plays the seat after a confirmation:       POST api/takeover {seat}
       While a seat is played the viewpoint is locked to it: only "退出接管" works.
     - the takeover bar   [⏸ 暂停 agent] [退出接管]: next to the label while a seat is played; leaving needs a paused / unstarted / finished game
       and a confirmation:                                       POST api/takeover/release
     - the soft pause (v0.13.1, docs/v0.13.1-plan.md G1-G7, contract §12.2), also part of the takeover bar:
         [⏸ 暂停 agent]   POST api/control/soft-pause   the calls in flight finish, then every agent holds, the
                          game clock stops - the person can still read, write and answer.  Only while the game runs.
         ⏳ 收尾中（还有 N 个回答） → ⏸ agent 已暂停      the two phases (session.soft_pause)
         [▶ 继续]         POST api/control/soft-resume  (also while it is still draining: that cancels it)
         立即暂停          POST api/control/pause         = the top bar's hard pause: cancels the calls in flight
   (v0.14, H52: the identities are dealt when the game starts - a seat taken over before that has nothing to re-deal, the page asks for none.)

   Everything it decides comes from the pure functions of view.js (viewpointChip, viewpointMenu, takeoverBar (+ softPause),
   takeoverAvailability); this file only wires them to the DOM and the API.

   ctx: { api, Core, View, showToast, getSession, getViewpoint, setViewpoint(id), openDialog(options),
          adoptSession(session, opts), refreshState(), isReplaying() }
   ========================================================================== */

(function (root) {
  "use strict";

  function create(ctx) {
    const opts = ctx || {};
    const Core = opts.Core || root.ArenaCore;
    const View = opts.View || root.ArenaView;
    const api = opts.api;
    const doc = opts.document || root.document;
    const win = opts.window || root;
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
      chip: $("btn-viewpoint"),
      chipText: $("viewpoint-text"),
      chipIcon: $("viewpoint-icon"),
      menu: $("viewpoint-menu"),
      takeoverBar: $("takeover-bar"),
      seat: $("takeover-seat"),
      btnRelease: $("btn-release-takeover"),
      softStatus: $("softpause-status"),
      btnSoft: $("btn-softpause"),
      btnSoftNow: $("btn-softpause-now"),
    };

    const st = {
      busy: false, // a takeover / release request is in flight (no double clicks)
      menuOpen: false,
      menuHtml: null, // what the menu shows now (it is only written when it changed)
    };

    const session = () => getSession() || {};

    function setHidden(node, hidden) {
      if (node) node.hidden = Boolean(hidden);
    }

    function setTitle(node, text) {
      if (node && typeof node.setAttribute === "function") node.setAttribute("title", text || "");
    }

    function setText(node, text) {
      if (node && node.textContent !== text) node.textContent = text;
    }

    function toggleClass(node, name, on) {
      if (node && node.classList && typeof node.classList.toggle === "function") node.classList.toggle(name, Boolean(on));
    }

    // -----------------------------------------------------------------------
    // the menu behind the label
    // -----------------------------------------------------------------------

    /** The markup of the menu (a pure function of the session and the viewpoint; `busy` greys everything while a request is on its way). */
    function menuHtml(model) {
      const dis = (enabled) => (enabled && !st.busy ? "" : " disabled");
      const rows = model.players
        .map((p) => {
          // (the check slot is always there: the names stay in line)
          const check = `<span class="vp-check"${p.current ? ` title="现在的视角"` : ""}>${p.current ? "✓" : ""}</span>`;
          const played = p.played ? `<span class="vp-played" title="你正在扮演这个座位">🎮</span>` : "";
          return (
            `<div class="vp-row${p.current ? " current" : ""}${p.played ? " played" : ""}" role="group" aria-label="${esc(p.name)}" data-seat="${esc(p.id)}">` +
            `<span class="vp-name">${check}<span class="vp-label">${esc(p.label)}</span>${played}</span>` +
            `<button type="button" class="btn btn-xs vp-view" role="menuitem" data-vp-view="${esc(p.id)}" title="${esc(p.view.title)}"` +
            ` aria-label="以 ${esc(p.name)} 的视角观看"${dis(p.view.enabled)}>观看</button>` +
            `<button type="button" class="btn btn-xs btn-takeover vp-take" role="menuitem" data-vp-take="${esc(p.id)}" title="${esc(p.takeover.title)}"` +
            ` aria-label="接管 ${esc(p.name)}"${dis(p.takeover.enabled)}>接管</button></div>`
          );
        })
        .join("");
      const god =
        `<button type="button" class="dropdown-item vp-god${model.god.current ? " current" : ""}" role="menuitem" data-vp-view="god"` +
        ` title="${esc(model.god.reason)}"${dis(model.god.enabled)}><span class="vp-check">${model.god.current ? "✓" : ""}</span>${esc(model.god.label)}</button>`;
      const head = `<div class="vp-head">以 X 的视角观看 · 接管 X</div>`;
      const note = model.takeoverNote ? `<div class="vp-note form-hint">${esc(model.takeoverNote)}</div>` : "";
      const release = model.release
        ? `<button type="button" class="dropdown-item danger vp-release" role="menuitem" data-vp-release title="${esc(model.release.title)}"${dis(model.release.enabled)}>` +
          `${esc(model.release.label)}</button>`
        : "";
      return `${god}${head}${rows}${note}${release}`;
    }

    /** Put the menu under the label, inside the screen (the menu is position: fixed, like the roster menu of the participant bar). */
    function placeMenu() {
      if (!el.menu || !el.chip || typeof el.chip.getBoundingClientRect !== "function") return;
      const r = el.chip.getBoundingClientRect();
      const vw = Number(win.innerWidth) || 1280;
      const mw = el.menu.offsetWidth || 300;
      if (el.menu.style) {
        el.menu.style.left = `${Math.round(Math.min(Math.max(8, r.left), Math.max(8, vw - mw - 8)))}px`;
        el.menu.style.top = `${Math.round(r.bottom + 5)}px`;
      }
    }

    function setMenu(open) {
      st.menuOpen = Boolean(open);
      if (el.menu) el.menu.hidden = !st.menuOpen;
      if (el.chip && typeof el.chip.setAttribute === "function") el.chip.setAttribute("aria-expanded", st.menuOpen ? "true" : "false");
      toggleClass(el.chip, "open", st.menuOpen);
      if (st.menuOpen) {
        renderMenu(true);
        placeMenu();
      }
    }

    function renderMenu(force) {
      if (!el.menu || (!st.menuOpen && !force)) return;
      const html = menuHtml(View.viewpointMenu(session(), getViewpoint()));
      if (force || html !== st.menuHtml) {
        st.menuHtml = html;
        el.menu.innerHTML = html;
      }
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
      // the label: 全局视角 / Alice视角 / 🎮 Alice视角（接管）
      const chip = View.viewpointChip(s, getViewpoint());
      setText(el.chipText, chip.text);
      setText(el.chipIcon, chip.icon);
      setTitle(el.chip, chip.title);
      if (el.chip && typeof el.chip.setAttribute === "function") el.chip.setAttribute("data-kind", chip.kind);
      toggleClass(el.chip, "is-player", chip.kind === "player");
      toggleClass(el.chip, "is-played", chip.kind === "played");
      renderMenu(false);

      const tb = View.takeoverBar(s);
      setHidden(el.takeoverBar, !tb);
      // (the stylesheet lets the phone's first row hold the label and the bar while a seat is played)
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
      toggleClass(el.takeoverBar, "is-soft-paused", Boolean(tb && tb.softPause.phase));
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

    /** 「观看」 / 全局视角: apply the viewpoint at once (never while a seat is played). */
    function viewAs(id) {
      const s = session();
      if (View.takeoverSeat(s)) {
        showToast("接管期间视角已锁定为你扮演的座位：请先退出接管");
        return false;
      }
      const target = View.normalizeViewpoint(s, id);
      setMenu(false);
      if (target === getViewpoint()) return false;
      setViewpoint(target);
      return true;
    }

    /** 「接管」: play `seatId`, after a confirmation. */
    function takeover(seatId) {
      setMenu(false);
      return exclusive(async () => {
        if (isReplaying()) {
          showToast("重放中不能接管：请先退出重放");
          return false;
        }
        const seat = seatId;
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
            `${session().status === "idle" ? "<li>对局还没有开始：身份在点击「开始」时才发牌，开始后你会在自己的座位上看到它。接管期间不能调整座位。</li>" : ""}` +
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
      setMenu(false);
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

    function closestFrom(target, selector) {
      let node = target;
      if (node && typeof node.closest !== "function" && node.parentElement) node = node.parentElement;
      return node && typeof node.closest === "function" ? node.closest(selector) : null;
    }

    on(el.chip, "click", () => {
      setMenu(!st.menuOpen);
    });
    on(el.menu, "click", (evt) => {
      const view = closestFrom(evt && evt.target, "[data-vp-view]");
      if (view) {
        if (!view.disabled) viewAs(view.getAttribute("data-vp-view"));
        return;
      }
      const take = closestFrom(evt && evt.target, "[data-vp-take]");
      if (take) {
        if (!take.disabled) takeover(take.getAttribute("data-vp-take"));
        return;
      }
      const rel = closestFrom(evt && evt.target, "[data-vp-release]");
      if (rel && !rel.disabled) release();
    });
    // a click outside the menu (and outside its label) closes it; so does Escape
    on(doc, "click", (evt) => {
      if (!st.menuOpen) return;
      const path = evt && typeof evt.composedPath === "function" ? evt.composedPath() : [];
      const inside = path.length
        ? path.some((n) => n === el.menu || n === el.chip)
        : Boolean(closestFrom(evt && evt.target, "#viewpoint-menu") || closestFrom(evt && evt.target, "#btn-viewpoint"));
      if (!inside) setMenu(false);
    });
    on(doc, "keydown", (evt) => {
      if (st.menuOpen && evt && evt.key === "Escape") {
        setMenu(false);
        if (el.chip && typeof el.chip.focus === "function") el.chip.focus();
      }
    });
    on(el.btnRelease, "click", () => {
      release();
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
      viewAs,
      takeover,
      release,
      softPause,
      softResume,
      hardPause,
      openMenu: () => setMenu(true),
      closeMenu: () => setMenu(false),
      menuOpen: () => st.menuOpen,
      _state: st,
    };
  }

  root.ArenaTakeoverUI = { create };
})(typeof window !== "undefined" ? window : this);
