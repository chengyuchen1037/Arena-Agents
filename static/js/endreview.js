/* ==========================================================================
   Arena-Agents v0.14 — the question at the end of a game: should the bots review it?
   (docs/v0.14-plan.md H31-H33, docs/v0.12-contracts.md §13.10)
   Loaded after takeover.js / composer.js and before app.js.  Exposes window.ArenaEndReviewUI.create(ctx).

   When the LIVE game becomes finished and `review.supported && review.available && !review.running` and nobody was reviewed yet (`review.done` is empty),
   a window asks - once:  「这局结束了，是否让 bot 复盘？」
     - every bot of the table is listed in seat order, all of them selected;
     - a seat a person played in this game says 「有人类代打」 and has the switch 「学习接管者的风格」 (on by default): the seats come from
       GET api/games/{session_id}/detail `human_spans` (and from what the page itself knows: the seat that is played right now);
     - 「复盘」:  POST api/game/review {ids, learn_style: {seat: bool}} -> {started, deadline_s: 60}.  The same window then shows the progress of every bot:
       its own 60 second countdown (`review.progress` -> `seconds_left`, counted down here between two events), and when it is done what it changed in
       its memory and how many proposals for its L0 now wait to be confirmed (`changes`);
     - 「暂不」 (also ✕ and Esc) is remembered in localStorage["arena_review_dismissed_v1"] = {"<session_id>": true}: the question does not come again
       for that game.  The participant bar keeps 「复盘全部」 / 「复盘此玩家」 (bots.js) for later.
   Every decision (when to ask, the rows, the counts) comes from the pure functions of view.js (reviewPrompt, reviewRows); this file wires them to the DOM
   and the API.

   ctx: { api, Core, View, showToast, getSession() (the LIVE game), isReplaying(), openBot(seat), storage }
   ========================================================================== */

(function (root) {
  "use strict";

  const DISMISS_KEY = "arena_review_dismissed_v1";
  const DISMISS_KEEP = 100; // the games remembered as "暂不" (the oldest go first)
  const MEMORY_LABELS = { arch_notes: "操作笔记", digest: "规则要点", playbook: "打法", questions: "待解问题", people: "印象", record: "战绩" };
  const FINAL_STATES = ["done", "timeout", "error"];

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
    const isReplaying = opts.isReplaying || (() => false);
    const openBot = opts.openBot || (() => {});
    const storage = opts.storage !== undefined ? opts.storage : root.localStorage || null;
    const $ = (id) => (typeof doc.getElementById === "function" ? doc.getElementById(id) : null);

    const el = {
      modal: $("review-modal"),
      title: $("review-title"),
      body: $("review-body"),
      count: $("review-count"),
      btnStart: $("btn-review-start"),
      btnLater: $("btn-review-later"),
      btnClose: $("btn-close-review"),
    };

    const st = {
      phase: "closed", // "closed" | "ask" (the question) | "progress" (the reviews run / ran)
      sessionId: "", // the game the window is about
      shown: new Set(), // the games the question was shown for (once per game and page)
      humans: [], // the seats a person played in this game
      choices: {}, // seat id -> {checked, learnStyle}: what the user ticked
      started: [], // the seats the review was started for
      deadline: 60,
      stamps: {}, // seat id -> Date.now() when its last review.progress arrived (the countdown counts down from there)
      busy: false, // POST api/game/review is on its way
      html: null, // what the list shows now ...
      sig: null, // ... and its structure without the moving countdowns (see paint)
      timer: null,
    };

    const session = () => getSession() || {};

    // -----------------------------------------------------------------------
    // "暂不" is remembered per game
    // -----------------------------------------------------------------------
    function readDismissed() {
      try {
        const raw = storage ? JSON.parse(storage.getItem(DISMISS_KEY) || "{}") : {};
        return raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
      } catch {
        return {};
      }
    }

    function dismiss(sessionId) {
      if (!sessionId || !storage) return;
      try {
        const all = { ...readDismissed(), [sessionId]: true };
        const keys = Object.keys(all);
        for (const k of keys.slice(0, Math.max(0, keys.length - DISMISS_KEEP))) delete all[k];
        storage.setItem(DISMISS_KEY, JSON.stringify(all));
      } catch {
        /* storage unavailable: the question may come once more after a reload */
      }
    }

    // -----------------------------------------------------------------------
    // rendering
    // -----------------------------------------------------------------------
    function setText(node, text) {
      if (node && node.textContent !== text) node.textContent = text;
    }

    function humanSeatsNow(s) {
      const seats = new Set();
      const played = View.takeoverSeat(s);
      if (played) seats.add(played);
      for (const p of s.participants || []) if (p && p.state === "human") seats.add(p.id);
      return Array.from(seats);
    }

    function askHtml(s) {
      const rows = View.reviewRows(s, st.humans, st.choices);
      const list = rows
        .map((r) => {
          const style = r.human
            ? `<label class="rv-style" title="打开后，这个 bot 复盘时也会学习接管者在这个座位上的说话方式和打法（可能产生对它底层设定的修改建议，要你确认）">` +
              `<input type="checkbox" data-rv-style="${esc(r.id)}"${r.learnStyle ? " checked" : ""}${r.checked ? "" : " disabled"} />` +
              `<span>学习接管者的风格</span></label>`
            : "";
          return (
            `<div class="rv-row${r.human ? " is-human" : ""}${r.done ? " is-done" : ""}" data-seat="${esc(r.id)}">` +
            `<label class="rv-pick"><input type="checkbox" data-rv-check="${esc(r.id)}"${r.checked ? " checked" : ""}${r.done ? " disabled" : ""} />` +
            `<span class="rv-seat">${esc(Core.numText(r.seat))}</span><span class="rv-name">${esc(r.name)}</span></label>` +
            `${r.human ? `<span class="rv-human" title="这局这个座位由人类接管过">有人类代打</span>` : ""}` +
            `${r.done ? `<span class="rv-done">已复盘</span>` : ""}${style}</div>`
          );
        })
        .join("");
      const humanNote = rows.some((r) => r.human)
        ? `<p class="form-hint rv-note">「有人类代打」：这局里这个座位有人接管过。bot 复盘时可以学习接管者的风格；不想学就关掉右边的开关。</p>`
        : "";
      return (
        `<p class="dialog-p">每个 bot 用自己的模型复盘本局（最多 ${esc(Core.numText(s.review ? s.review.deadline_s : 60))} 秒）：更新自己的记忆，也可能提出对底层设定的修改（要你确认才生效）。` +
        `每局每个 bot 只复盘一次。选「暂不」之后，参与者栏里的「复盘全部」「复盘此玩家」还可以补做。</p>` +
        `<div class="rv-actions"><button type="button" class="btn btn-xs" data-rv-all="1">全选</button>` +
        `<button type="button" class="btn btn-xs" data-rv-all="0">全不选</button></div>` +
        `<div class="rv-list">${list}</div>${humanNote}`
      );
    }

    /** What the countdown of a running review shows now: the seconds of the last event minus what has passed since it arrived. */
    function secondsLeftOf(id, progress) {
      if (progress.seconds_left === null || progress.seconds_left === undefined) return null;
      const stamp = st.stamps[id];
      const passed = stamp ? Math.max(0, Math.floor((Date.now() - stamp) / 1000)) : 0;
      return Math.max(0, Math.ceil(progress.seconds_left) - passed);
    }

    /** How much of the 60 seconds of a review has gone (the width of its bar, 0-100). */
    function barPercent(left, progress) {
      const total = Math.max(1, Number(progress && progress.deadline_s) || st.deadline || 60);
      return Math.max(0, Math.min(100, Math.round(((total - left) / total) * 100)));
    }

    /**
     * The countdowns move once a second while the rest of the list stays as it is: they are written into their elements, the list is not drawn again (a click
     * on a button of a row must not be lost to a repaint every second).  false when an element is missing - the caller then draws the whole list.
     */
    function patchCountdowns(s) {
      if (!el.body || typeof el.body.querySelector !== "function") return false;
      const progress = (s.review && s.review.progress) || {};
      try {
        for (const id of st.started) {
          const p = progress[id];
          if (!p || p.state !== "running" || p.seconds_left === null || p.seconds_left === undefined) continue;
          const left = secondsLeftOf(id, p);
          const num = el.body.querySelector(`[data-rv-left="${id}"]`);
          const bar = el.body.querySelector(`[data-rv-bar="${id}"]`);
          if (!num || !bar) return false;
          setText(num, Core.numText(left));
          if (bar.style) bar.style.width = `${barPercent(left, p)}%`;
        }
        return true;
      } catch {
        return false; // (an id the selector cannot take: the list is drawn whole instead)
      }
    }

    function changesHtml(id, ch) {
      if (!ch) return "";
      const names = (ch.files || []).map((f) => MEMORY_LABELS[f.file] || f.file);
      const parts = [];
      parts.push(
        names.length
          ? `改动了 ${esc(Core.numText(names.length))} 个文件：${names.map(esc).join("、")}`
          : "这次没有改动记忆"
      );
      let button = "";
      if (ch.proposals) {
        parts.push(`<span class="rv-proposals">有 ${esc(Core.numText(ch.proposals))} 条对底层设定的修改建议，等你确认</span>`);
        button = `<button type="button" class="btn btn-xs" data-rv-open="${esc(id)}" title="打开这个玩家的面板，在里面确认或拒绝建议、查看记忆的改动">去看看</button>`;
      } else if (names.length) {
        button = `<button type="button" class="btn btn-xs" data-rv-open="${esc(id)}" title="打开这个玩家的面板，查看记忆的改动">去看看</button>`;
      }
      // (the button follows the sentence, it is not one more part of it: no "·" in front of it)
      return `<div class="rv-result">${parts.join(" · ")}${button ? " " + button : ""}</div>`;
    }

    function progressHtml(s) {
      const progress = (s.review && s.review.progress) || {};
      const rows = View.reviewRows(s, st.humans, st.choices).filter((r) => st.started.includes(r.id));
      return (
        `<div class="rv-list rv-progress">` +
        rows
          .map((r) => {
            const p = progress[r.id] || null;
            const state = p ? p.state : "queued";
            let status;
            let bar = "";
            if (state === "done") status = `<span class="rv-state ok">✔ 已复盘</span>`;
            else if (state === "timeout") status = `<span class="rv-state warn">⏱ 时间到，已保存写好的部分</span>`;
            else if (state === "error") status = `<span class="rv-state bad">✖ 复盘失败${p && p.message ? `：${esc(p.message)}` : ""}</span>`;
            else if (state === "running") {
              const left = secondsLeftOf(r.id, p);
              status = `<span class="rv-state busy">⏳ 复盘中${left === null ? "" : ` · 还剩 <span class="rv-left" data-rv-left="${esc(r.id)}">${esc(Core.numText(left))}</span> 秒`}</span>`;
              if (left !== null) bar = `<span class="rv-bar" aria-hidden="true"><span data-rv-bar="${esc(r.id)}" style="width:${barPercent(left, p)}%"></span></span>`;
            } else status = `<span class="rv-state">排队中…</span>`;
            const note = p && p.message && (state === "running" || state === "queued") ? `<span class="form-hint rv-msg">${esc(p.message)}</span>` : "";
            const result = state === "done" || state === "timeout" ? changesHtml(r.id, p && p.changes) : "";
            return (
              `<div class="rv-row rv-${esc(state)}${r.human ? " is-human" : ""}" data-seat="${esc(r.id)}" data-state="${esc(state)}">` +
              `<span class="rv-seat">${esc(Core.numText(r.seat))}</span><span class="rv-name">${esc(r.name)}</span>` +
              `${r.human ? `<span class="rv-human" title="这局这个座位由人类接管过">有人类代打</span>` : ""}${status}${bar}${note}${result}</div>`
            );
          })
          .join("") +
        `</div>`
      );
    }

    /** How many of the started reviews have not finished (the window says so and keeps its countdown going). */
    function runningCount(s) {
      const progress = (s.review && s.review.progress) || {};
      return st.started.filter((id) => !progress[id] || !FINAL_STATES.includes(progress[id].state)).length;
    }

    /** The structure of a list without its moving parts (the countdown numbers and the widths of the bars). */
    const structureOf = (html) => html.replace(/(data-rv-left="[^"]*">)\d+/g, "$1#").replace(/style="width:\d+%"/g, 'style="width:#"');

    /** Write the list: all of it when its structure changed, else only the countdowns (see patchCountdowns). */
    function paint(html, s) {
      const sig = structureOf(html);
      if (sig !== st.sig) {
        st.sig = sig;
        st.html = html;
        el.body.innerHTML = html;
        return;
      }
      if (html === st.html) return;
      st.html = html;
      if (!patchCountdowns(s)) el.body.innerHTML = html;
    }

    /**
     * The user ticked something: the boxes of the page already show it, so what the list would be written as now IS what is on the page.  It is remembered as
     * such - the next draw finds nothing to change and leaves the list alone (a rebuild would take the keyboard focus off the box that was just ticked).
     */
    function rememberPainted() {
      st.html = askHtml(session());
      st.sig = structureOf(st.html);
    }

    /** The footer of the question: how many are ticked, and the buttons (the list itself is not touched: a tick must not rebuild it under the user's hand). */
    function renderAsk(s, redraw) {
      const rows = View.reviewRows(s, st.humans, st.choices);
      const chosen = rows.filter((r) => r.checked && !r.done).length;
      if (redraw) paint(askHtml(s), s);
      setText(el.title, "这局结束了，是否让 bot 复盘？");
      setText(el.count, `已选 ${Core.numText(chosen)} / ${Core.numText(rows.length)}`);
      if (el.btnStart) {
        el.btnStart.hidden = false;
        el.btnStart.disabled = chosen === 0 || st.busy;
        setText(el.btnStart, st.busy ? "正在开始…" : "复盘");
      }
      if (el.btnLater) {
        el.btnLater.hidden = false;
        setText(el.btnLater, "暂不");
      }
    }

    function render() {
      if (st.phase === "closed" || !el.body) return;
      const s = session();
      if (st.phase === "ask") {
        renderAsk(s, true);
      } else {
        paint(progressHtml(s), s);
        const running = runningCount(s);
        setText(el.title, running ? "bot 正在复盘" : "复盘完成");
        setText(el.count, running ? `还有 ${Core.numText(running)} 个在复盘` : "全部完成：改动已经写进各自的记忆");
        if (el.btnStart) el.btnStart.hidden = true;
        if (el.btnLater) {
          el.btnLater.hidden = false;
          setText(el.btnLater, running ? "关闭（后台继续）" : "关闭");
        }
        syncTimer(running > 0);
      }
    }

    // -----------------------------------------------------------------------
    // the countdown: the server tells the seconds left at most once a second, the window counts down between two events
    // -----------------------------------------------------------------------
    function syncTimer(on) {
      if (!on || typeof win.setInterval !== "function") {
        if (st.timer !== null && typeof win.clearInterval === "function") win.clearInterval(st.timer);
        st.timer = null;
        return;
      }
      if (st.timer === null) st.timer = win.setInterval(() => render(), 1000);
    }

    // -----------------------------------------------------------------------
    // open / close
    // -----------------------------------------------------------------------
    function show(on) {
      if (el.modal && el.modal.style) el.modal.style.display = on ? "flex" : "none";
    }

    /** Close the window.  `remember`: it was answered with 「暂不」 - the question does not come again for this game. */
    function close(remember) {
      if (st.phase === "closed") return;
      if (remember && st.phase === "ask") dismiss(st.sessionId);
      st.phase = "closed";
      st.html = null;
      st.sig = null;
      syncTimer(false);
      show(false);
    }

    /** The seats a person played in this game: the page's own knowledge first, then the game's `human_spans` (GET api/games/{id}/detail). */
    async function readHumans(sessionId) {
      try {
        const detail = await api.get(`api/games/${encodeURIComponent(sessionId)}/detail`);
        if (st.phase !== "ask" || st.sessionId !== sessionId) return;
        const spans = detail && Array.isArray(detail.human_spans) ? detail.human_spans : [];
        const seats = new Set(st.humans);
        for (const span of spans) if (span && typeof span.seat === "string" && span.seat) seats.add(span.seat);
        if (seats.size !== st.humans.length) {
          st.humans = Array.from(seats);
          render();
        }
      } catch {
        /* an older server / a game that cannot be read: what the page knows stays */
      }
    }

    function open(s) {
      const game = s || session();
      st.phase = "ask";
      st.sessionId = game.session_id || "";
      st.choices = {};
      st.started = [];
      st.stamps = {};
      st.busy = false;
      st.html = null;
      st.sig = null;
      st.humans = humanSeatsNow(game);
      show(true);
      render();
      readHumans(st.sessionId);
    }

    /** The live session changed: is it time to ask?  (Cheap: it is called after every event.) */
    function onSession(s) {
      if (!s) return;
      if (st.phase !== "closed") {
        if (s.session_id !== st.sessionId) close(false); // another game: the question is moot
        else render();
        return;
      }
      if (s.status !== "finished" || isReplaying() || st.shown.has(s.session_id)) return; // (called after every event: the cheap checks first)
      if (!View.reviewPrompt(s, readDismissed()).show) return;
      st.shown.add(s.session_id);
      open(s);
    }

    /** One `review.progress` event (the store has put it in `session.review.progress` already). */
    function onReviewEvent(ev) {
      if (!ev || !ev.id) return;
      st.stamps[ev.id] = Date.now();
      if (st.phase === "progress") render();
    }

    // -----------------------------------------------------------------------
    // the answer
    // -----------------------------------------------------------------------
    async function start() {
      if (st.phase !== "ask" || st.busy) return false;
      const s = session();
      const rows = View.reviewRows(s, st.humans, st.choices).filter((r) => r.checked && !r.done);
      if (!rows.length) {
        showToast("至少选一个 bot 再点「复盘」");
        return false;
      }
      const body = { ids: rows.map((r) => r.id) };
      const learn = {};
      for (const r of rows) if (r.human) learn[r.id] = Boolean(r.learnStyle);
      if (Object.keys(learn).length) body.learn_style = learn;
      st.busy = true;
      render();
      try {
        const res = await api.post("api/game/review", body);
        st.started = (res && Array.isArray(res.started) && res.started.length ? res.started : body.ids).slice();
        st.deadline = (res && Number(res.deadline_s)) || 60;
        for (const id of st.started) st.stamps[id] = Date.now();
        st.phase = "progress";
        st.html = null;
        st.sig = null;
        return true;
      } catch (err) {
        showToast(`复盘没有开始：${err && err.message ? err.message : "请求失败"}`); // (409: nothing to review any more / a review runs already ...)
        return false;
      } finally {
        st.busy = false;
        render();
      }
    }

    function later() {
      close(true);
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

    function choiceOf(id) {
      if (!st.choices[id]) st.choices[id] = {};
      return st.choices[id];
    }

    on(el.btnStart, "click", () => {
      start();
    });
    on(el.btnLater, "click", () => later());
    on(el.btnClose, "click", () => later());
    on(el.body, "change", (evt) => {
      if (st.phase !== "ask") return;
      const target = evt && evt.target;
      const check = closestFrom(target, "[data-rv-check]");
      if (check) {
        const id = check.getAttribute("data-rv-check");
        choiceOf(id).checked = Boolean(check.checked);
        // the switch of a seat that is not reviewed is greyed (in place: the list is not drawn again, the focus stays where it is)
        const sw = el.body && typeof el.body.querySelector === "function" ? el.body.querySelector(`[data-rv-style="${id}"]`) : null;
        if (sw) sw.disabled = !check.checked;
        rememberPainted();
        renderAsk(session(), false);
        return;
      }
      const style = closestFrom(target, "[data-rv-style]");
      if (style) {
        choiceOf(style.getAttribute("data-rv-style")).learnStyle = Boolean(style.checked);
        rememberPainted();
      }
    });
    on(el.body, "click", (evt) => {
      const target = evt && evt.target;
      const all = closestFrom(target, "[data-rv-all]");
      if (all) {
        const checked = all.getAttribute("data-rv-all") === "1";
        for (const r of View.reviewRows(session(), st.humans, st.choices)) if (!r.done) choiceOf(r.id).checked = checked;
        render();
        return;
      }
      const open1 = closestFrom(target, "[data-rv-open]");
      if (open1) {
        const seat = open1.getAttribute("data-rv-open");
        close(false); // (the panel of the player opens behind a modal: the window goes first - the reviews go on in the background)
        openBot(seat);
      }
    });
    on(doc, "keydown", (evt) => {
      if (st.phase === "closed" || !evt || evt.key !== "Escape") return;
      const dialog = $("dialog-modal");
      if (dialog && dialog.style && dialog.style.display === "flex") return; // a confirmation is open above this window: Esc closes that one
      later();
    });

    return {
      onSession,
      onReviewEvent,
      render,
      open,
      close,
      start,
      later,
      tick: render, // (the countdown; the page calls it once a second itself while a review runs)
      isOpen: () => st.phase !== "closed",
      phase: () => st.phase,
      dismissed: readDismissed,
      _state: st,
    };
  }

  root.ArenaEndReviewUI = { create, DISMISS_KEY };
})(typeof window !== "undefined" ? window : globalThis);
