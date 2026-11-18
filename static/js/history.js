/* ==========================================================================
   Arena-Agents v0.13 — the history of games (F34, F35)
   Loaded after providers.js and before app.js. Exposes window.ArenaHistoryUI.create(ctx) (plus pure helpers).

   The "📜 历史对局" button opens the list of every saved game: when it was played, how it ended, how long
   it took (game time), what it cost, whether a person played a seat.  A game can be
     replayed   ("重放":  GET api/games/{id}/replay -> replay.js plays the spectator view back),
     continued  ("继续":  POST api/games/{id}/load  -> it becomes the live game, paused),
     deleted    ("删除":  DELETE api/games/{id} -> the game and its logs only; the agents' memory lives in the
                          roster and is untouched - the confirmation says so).
   The live game is marked "当前" and can be neither loaded nor deleted.

   ctx: { api, Core, showToast, getSession, openDialog, isReplaying(), onLoaded(sessionRaw), onReplay(replay, game) }
   ========================================================================== */

(function (root) {
  "use strict";

  const enc = encodeURIComponent;

  /** "2026-11-18 01:20" in the viewer's local time ("" when unparsable). */
  function formatWhen(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    const p = (x) => String(x).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  /** Game seconds as "m:ss" / "h:mm:ss" ("" for nothing). */
  function formatGameTime(seconds) {
    const n = Number(seconds);
    if (!Number.isFinite(n) || n <= 0) return "";
    const s = Math.round(n);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = String(s % 60).padStart(2, "0");
    return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
  }

  /** GET api/games -> [{session_id, ...}] (tolerant: an array or {games: [...]}). */
  function normalizeGames(resp) {
    const list = Array.isArray(resp) ? resp : resp && Array.isArray(resp.games) ? resp.games : [];
    return list
      .filter((g) => g && typeof g === "object" && g.session_id)
      .map((g) => ({ ...g, session_id: String(g.session_id) }));
  }

  /**
   * What one row of the list shows and which of its buttons work.
   * `env`: {status, takeoverSeat, replaying} of the LIVE game (the buttons depend on it).
   */
  function rowModel(game, env, Core) {
    const e = env || {};
    const unreadable = game.status === "unreadable";
    const current = Boolean(game.current);
    const status = current && e.status ? e.status : game.status;
    const running = current && status === "running";
    let statusText;
    if (unreadable) statusText = "无法读取";
    else if (status === "finished") statusText = game.winner ? `已结束 · ${game.winner} 获胜` : "已结束";
    else if (status === "running") statusText = "进行中";
    else if (status === "paused") statusText = "已暂停";
    else statusText = "未开始";
    const meta = [
      formatWhen(game.created_at || game.updated_at),
      statusText,
      game.duration_gt > 0 ? `用时 ${formatGameTime(game.duration_gt)}` : "",
      game.players ? `${game.players} 名玩家` : "",
      game.tokens > 0 && Core ? `${Core.formatCount(game.tokens)} tokens` : "",
      game.cost_usd > 0 && Core ? Core.formatCost(game.cost_usd, 2) : "",
    ].filter(Boolean);

    let replayTitle = "按游戏时间重放这一局（只含观战能看到的内容，不含思考过程）";
    let canReplay = !unreadable;
    if (unreadable) replayTitle = "这局的文件无法读取";
    else if (status === "idle" && !(game.duration_gt > 0)) {
      canReplay = false; // a game that was never started has no ledger to play back
      replayTitle = "这一局还没有开始，没有可重放的内容";
    } else if (current && (running || e.takeoverSeat)) {
      canReplay = false;
      replayTitle = "对局进行中或正在接管一个座位，不能重放当前对局：请先暂停并退出接管";
    }

    // "继续" is for a game that was not finished: it comes back paused, like after a restart
    const canContinue = !unreadable && !current && status !== "finished";
    let loadTitle = "载入这一局（当前对局会先自动保存；载入后保持暂停）";
    let canLoad = canContinue;
    if (canContinue && e.status === "running") {
      canLoad = false;
      loadTitle = "对局进行中，不能载入其他对局：请先暂停";
    } else if (canContinue && e.replaying) {
      canLoad = false;
      loadTitle = "正在重放：请先退出重放";
    }

    return {
      id: game.session_id,
      title: game.scenario_title || game.title || game.session_id,
      current,
      human: Boolean(game.human_involved),
      unreadable,
      meta,
      showLoad: canContinue,
      canReplay,
      replayTitle,
      canLoad,
      loadTitle,
      canDelete: !current,
      deleteTitle: current ? "当前对局不能删除：请先新建或载入另一局" : "删除这一局（agent 的记忆不受影响）",
    };
  }

  function create(ctx) {
    const opts = ctx || {};
    const Core = opts.Core || root.ArenaCore;
    const api = opts.api;
    const doc = opts.document || root.document;
    if (!Core || !api || !doc) return null;
    const showToast = opts.showToast || (() => {});
    const getSession = opts.getSession || (() => null);
    const isReplaying = opts.isReplaying || (() => false);
    const esc = Core.escapeHtml;
    const $ = (id) => (typeof doc.getElementById === "function" ? doc.getElementById(id) : null);

    const el = {
      modal: $("history-modal"),
      list: $("history-list"),
      hint: $("history-hint"),
      btnOpen: $("btn-open-history-modal"),
      btnClose: $("btn-close-history-modal"),
      btnRefresh: $("btn-refresh-history"),
    };
    const st = { games: [], loading: false, error: "", busyId: "", lastHtml: null };

    const live = () => {
      const s = getSession() || {};
      return {
        status: s.status || "idle",
        takeoverSeat: s.takeover && s.takeover.seat ? String(s.takeover.seat) : null,
        replaying: Boolean(isReplaying()),
      };
    };

    function isOpen() {
      return Boolean(el.modal && el.modal.style && el.modal.style.display === "flex");
    }
    function show() {
      if (el.modal) el.modal.style.display = "flex";
    }
    function hide() {
      if (el.modal) el.modal.style.display = "none";
    }

    // -----------------------------------------------------------------------
    // rendering
    // -----------------------------------------------------------------------

    function rowHtml(row) {
      const busy = st.busyId === row.id;
      const badges =
        (row.current ? ' <span class="tag-badge muted">当前</span>' : "") +
        (row.human ? ' <span class="tag-badge purple" title="这局里有人接管过一个座位">🎮 有人类参与</span>' : "");
      const dis = (ok) => (ok && !busy ? "" : " disabled");
      return (
        `<div class="history-item${row.current ? " is-current" : ""}${row.unreadable ? " is-unreadable" : ""}" data-session-id="${esc(row.id)}">` +
        `<div class="history-main"><div class="history-title">${esc(row.title)}${badges}</div>` +
        `<div class="history-meta">${row.meta.map((m) => esc(m)).join(" &middot; ")}<span class="history-id"> &middot; ${esc(row.id)}</span></div></div>` +
        `<div class="history-actions">` +
        `<button type="button" class="btn btn-xs btn-primary-outline" data-action="replay"${dis(row.canReplay)} title="${esc(row.replayTitle)}">▶ 重放</button>` +
        (row.showLoad
          ? `<button type="button" class="btn btn-xs" data-action="load"${dis(row.canLoad)} title="${esc(row.loadTitle)}">继续</button>`
          : "") +
        `<button type="button" class="btn btn-xs btn-danger-outline" data-action="delete"${dis(row.canDelete)} title="${esc(row.deleteTitle)}">删除</button>` +
        `</div></div>`
      );
    }

    function render() {
      if (!el.list) return;
      let html;
      if (st.loading && st.games.length === 0) html = `<div class="empty-placeholder">正在读取…</div>`;
      else if (st.error) html = `<div class="empty-placeholder history-error">${esc(st.error)}</div>`;
      else if (st.games.length === 0) html = `<div class="empty-placeholder">还没有保存过的对局</div>`;
      else {
        const env = live();
        html = st.games.map((g) => rowHtml(rowModel(g, env, Core))).join("");
      }
      if (html !== st.lastHtml) {
        st.lastHtml = html;
        el.list.innerHTML = html;
      }
      if (el.hint) {
        const n = st.games.length;
        el.hint.textContent = n ? `共 ${n} 局，最近保存的在前。` : "";
      }
    }

    // -----------------------------------------------------------------------
    // actions
    // -----------------------------------------------------------------------

    async function refresh() {
      st.loading = true;
      st.error = "";
      render();
      try {
        st.games = normalizeGames(await api.get("api/games"));
      } catch (e) {
        st.games = [];
        st.error = `读取历史对局失败：${e.message}`;
      }
      st.loading = false;
      render();
    }

    const find = (id) => st.games.find((g) => g.session_id === id) || null;
    const labelOf = (g) => {
      const when = formatWhen(g.created_at || g.updated_at);
      return `${g.scenario_title || g.title || g.session_id}${when ? `（${when}）` : ""}`;
    };

    async function confirm(o) {
      if (typeof opts.openDialog === "function") return opts.openDialog(o);
      const ok = typeof root.confirm === "function" ? root.confirm(String(o.title || "确认")) : false;
      return { ok, values: {} };
    }

    /** The row of a game as it is NOW (the live game may have changed since the list was painted). */
    const modelOf = (game) => rowModel(game, live(), Core);

    async function replayGame(id) {
      const game = find(id);
      if (!game || st.busyId) return;
      const model = modelOf(game);
      if (!model.canReplay) {
        showToast(model.replayTitle);
        return;
      }
      st.busyId = id;
      render();
      try {
        const data = await api.get(`api/games/${enc(id)}/replay`);
        hide();
        if (typeof opts.onReplay === "function") opts.onReplay(data, game);
      } catch (e) {
        showToast(`无法重放：${e.message}`);
      } finally {
        st.busyId = "";
        render();
      }
    }

    async function loadGame(id) {
      const game = find(id);
      if (!game || st.busyId) return;
      const before = modelOf(game);
      if (!before.canLoad) {
        showToast(before.loadTitle);
        return;
      }
      const answer = await confirm({
        title: "继续这一局",
        okText: "载入",
        bodyHtml:
          `<p>载入 <strong>${esc(labelOf(game))}</strong>？</p>` +
          `<p class="form-hint">当前对局会先自动保存，然后被这一局替换。载入后对局保持<strong>暂停</strong>：检查模型绑定后点「继续」。</p>`,
      });
      if (!answer || !answer.ok) return;
      const after = modelOf(game); // the game may have been started while the question was open
      if (!after.canLoad) {
        showToast(after.loadTitle);
        return;
      }
      st.busyId = id;
      render();
      try {
        const res = await api.post(`api/games/${enc(id)}/load`);
        if (res && res.session && typeof opts.onLoaded === "function") opts.onLoaded(res.session);
        hide();
        showToast("对局已载入（保持暂停，请检查模型绑定后点击「继续」）");
      } catch (e) {
        showToast(e.message);
      } finally {
        st.busyId = "";
      }
      await refresh();
    }

    async function deleteGame(id) {
      const game = find(id);
      if (!game || st.busyId) return;
      const model = modelOf(game);
      if (!model.canDelete) {
        showToast(model.deleteTitle);
        return;
      }
      const answer = await confirm({
        title: "删除对局",
        okText: "删除",
        danger: true,
        bodyHtml:
          `<p>永久删除 <strong>${esc(labelOf(game))}</strong>？这一局的日志会一起删除，无法恢复。</p>` +
          `<p class="form-hint history-memory-note">删除对局不会影响当前 agent 的记忆：每个座位的性格与记忆保存在名册里，不属于某一局。</p>`,
      });
      if (!answer || !answer.ok) return;
      st.busyId = id;
      render();
      try {
        await api.del(`api/games/${enc(id)}`);
        showToast("对局已删除（agent 的记忆没有受到影响）");
      } catch (e) {
        showToast(e.message);
      } finally {
        st.busyId = "";
      }
      await refresh();
    }

    async function openDialogAndLoad() {
      show();
      await refresh();
    }

    /** The live game changed (status, a seat taken over, replay on / off): the buttons follow. */
    function onSession() {
      if (isOpen()) render();
    }

    function on(node, type, handler) {
      if (node && typeof node.addEventListener === "function") node.addEventListener(type, handler);
    }
    function closestAttr(target, selector, attr) {
      const node = target && target.closest ? target.closest(selector) : null;
      return node ? node.getAttribute(attr) : null;
    }

    on(el.btnOpen, "click", () => openDialogAndLoad());
    on(el.btnClose, "click", hide);
    on(el.btnRefresh, "click", () => refresh());
    on(el.list, "click", (evt) => {
      const action = closestAttr(evt && evt.target, "[data-action]", "data-action");
      const id = closestAttr(evt && evt.target, ".history-item[data-session-id]", "data-session-id");
      if (!action || !id) return;
      if (action === "replay") replayGame(id);
      else if (action === "load") loadGame(id);
      else if (action === "delete") deleteGame(id);
    });

    return { open: openDialogAndLoad, close: hide, refresh, onSession, isOpen, _state: st };
  }

  root.ArenaHistoryUI = { create, rowModel, normalizeGames, formatWhen, formatGameTime };
  if (typeof module !== "undefined" && module.exports) module.exports = root.ArenaHistoryUI;
})(typeof window !== "undefined" ? window : globalThis);
