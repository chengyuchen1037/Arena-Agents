/* ==========================================================================
   Arena-Agents v0.14 — the bots UI (H19–H23, H30, H33; replaces roster.js of v0.12.1 / v0.13)
   Loaded after botlib.js / botedit.js and before app.js. Exposes window.ArenaBotsUI.create(ctx) and, once created, window.Bots.

   Owns
     - the player detail panel (a floating card on desktop / tablet, a bottom drawer on phones): the bot's name (editable before the game starts,
       🔒 once it is initialized), its avatar and avatar code, the initialization row (✔ ⚠ ✖ ⏳ + the button), state, tags, model, metrics, the review
       of one player and what it changed; below: the editors of botedit.js (底层设定, 记忆, 待确认, 导入 / 导出)
     - the controls in the participant bar header: "⚙ 全局初始化" (+ the counts), the "名册 ▾" menu (本局加载记忆, 清除全部学到的记忆, 重新生成全部头像,
       the learning use), "复盘全部"
     - the shared modal (`openDialog`: confirmations and forms)
     - window.Bots.focus(botId, fix): the start check's "去初始化 / 去导入" jump (docs/v0.12-contracts.md §13.10)

   Server API (§13.7):
     GET api/bots (limits, defaults)        GET api/bots/{id}                  PUT api/bots/{id} {name?, avatar?, confirm_reset?}
     POST api/bots/{id}/avatar:regenerate   POST api/bots/avatars:regenerate   PUT api/moderator {avatar}   POST api/moderator/avatar:regenerate
     POST api/bots/init {ids?, rebuild?}    POST api/bots/init:cancel          POST api/bots/memory:clear   PUT api/bots/memory-enabled {enabled}
     POST api/game/review {ids?, learn_style?}
   SSE (store.js): bots.updated -> onBots, bot.init.progress -> onInitEvent, review.progress -> onReviewEvent

   ctx: { api, Core, Avatar, View, showToast, getSession, getViewpoint, getClockNow, bindingText(id), openBindings(id), setViewpoint(id), lockedSeat(),
          markHeadHtml(id), openThoughts(id), thoughtsBlocked(id), isWindowOpen(), patchParticipants(list), applyBots(payload), onChange(),
          download(path) (fetch + save), buildUrl(path, {token}), openSettings(column), copyText?(text), document?, window? }
   ========================================================================== */

(function (root) {
  "use strict";

  const Lib = root.ArenaBotLib || (typeof require === "function" ? require("./botlib.js") : null);
  const Editors = root.ArenaBotEditors || (typeof require === "function" ? require("./botedit.js") : null);

  const MOD = "moderator";
  /** The fixed sentences of docs/v0.12-contracts.md §13.10 (the tests assert on them). */
  const INIT_CONFIRM = "bot 会根据自己的底层设定阅读官方契约和规则，写出操作笔记、规则要点和打法。每个最多 120 秒，期间不能开局。";
  const RESET_CONFIRM = "修改名字或头像会清空这个玩家的初始化结果和全部记忆（操作笔记、规则要点、打法、待解问题、印象、战绩），底层设定保留。修改后需要重新初始化才能开局。是否确认？";
  const CLEAR_ALL_CONFIRM = "将清空全部 12 名玩家学到的记忆（操作笔记、规则要点、打法、待解问题、印象、战绩），初始化结果一并清空（变为未初始化），底层设定保留。此操作不能撤销，建议先在「设置」里导出总存档。";
  const MEMORY_LABELS = { arch_notes: "操作笔记", digest: "规则要点", playbook: "打法", questions: "待解问题", people: "印象", record: "战绩" };
  const FINISHED_INIT = ["done", "failed", "timeout", "cancelled"];

  const errText = (e) => (e && (e.detail || e.message)) || "操作失败";

  function create(ctx) {
    const opts = ctx || {};
    const Core = opts.Core || root.ArenaCore;
    const Avatar = opts.Avatar || root.ArenaAvatar;
    const View = opts.View || root.ArenaView;
    const api = opts.api;
    const doc = opts.document || root.document;
    const win = opts.window || root;
    const showToast = opts.showToast || (() => {});
    const getSession = opts.getSession || (() => null);
    const getViewpoint = opts.getViewpoint || (() => View.GOD);
    const getClockNow = opts.getClockNow || (() => 0);
    const bindingText = opts.bindingText || (() => "未绑定");
    const openBindings = opts.openBindings || (() => {});
    const setViewpoint = opts.setViewpoint || (() => {});
    const lockedSeat = opts.lockedSeat || (() => null);
    const patchParticipants = opts.patchParticipants || (() => {});
    const applyBots = opts.applyBots || (() => {});
    const onChange = opts.onChange || (() => {});
    const markHeadHtml = opts.markHeadHtml || (() => "");
    const openThoughts = opts.openThoughts || (() => {});
    const thoughtsBlocked = opts.thoughtsBlocked || (() => "");
    const isWindowOpen = opts.isWindowOpen || (() => false);
    const openSettings = opts.openSettings || (() => false);
    const buildUrl = opts.buildUrl || ((path) => path);
    if (!Core || !Avatar || !View || !doc || !Lib || !Editors) return null;

    const esc = Core.escapeHtml;
    const enc = encodeURIComponent;
    const $ = (id) => (typeof doc.getElementById === "function" ? doc.getElementById(id) : null);

    const el = {
      panel: $("detail-panel"),
      backdrop: $("detail-backdrop"),
      head: $("detail-head"),
      live: $("detail-live"),
      editors: $("detail-editors"),
      grid: $("participants-grid"),
      modChip: $("mod-chip"),
      btnInitAll: $("btn-init-all"),
      initSummary: $("init-summary"),
      btnRosterMenu: $("btn-roster-menu"),
      rosterMenu: $("roster-menu"),
      btnClear: $("btn-roster-clear"),
      btnRegen: $("btn-roster-regen"),
      chkMemory: $("chk-memory-enabled"),
      usage: $("roster-usage"),
      btnReviewAll: $("btn-review-all"),
      fileBot: $("file-bot-import"),
      fileCore: $("file-core-import"),
      dialog: $("dialog-modal"),
      dialogTitle: $("dialog-title"),
      dialogBody: $("dialog-body"),
      dialogOk: $("btn-dialog-ok"),
      dialogCancel: $("btn-dialog-cancel"),
      dialogClose: $("btn-close-dialog"),
    };

    const st = {
      available: true, // false: the backend has no bots API (an older server): the editors are off
      loaded: false, // GET /api/bots succeeded at least once
      limits: Lib.normalizeLimits(null),
      defaults: [], // the default library: [{name, avatar, identity}]
      detail: new Map(), // bot id -> BotDetail (the newest we have)
      detailRev: {},
      detailFp: {}, // bot id -> the fingerprint of its summary when the detail was read
      detailErr: {},
      reading: {},
      sessionKey: null,
      openId: null, // the actor of the open panel: a seat ("N3") or "moderator"
      saving: {}, // "name:N3" / "avatar:N3" / "regen:N3" / "moderator" -> true
      nameDraft: {},
      nameError: {},
      avatarOpen: {},
      codeDraft: {},
      codeNote: {}, // id -> {kind: "error" | "hint" | "ok", text}
      changesOpen: {},
      initNote: {}, // bot id -> why the last start of an initialization was refused
      lastHead: null,
      lastLive: null,
      lastEditors: null,
      busy: { init: false, review: false, all: false },
      deferred: false, // a repaint of the head / editors waits for the user to leave the text box
      rev: 0,
      botsRev: 0,
      jobId: "", // the initialization job we have seen running (the summary is shown when it ends)
      jobResults: {}, // bot id -> the last bot.init.progress of that job
    };

    const session = () => getSession() || {};
    const isReplay = () => Boolean(session().replay);
    // (a replayed game - session.replay - is never editable: the bots belong to the live game)
    const isIdle = () => session().status === "idle" && !isReplay();
    const isPhone = () => {
      const html = doc.documentElement;
      return Boolean(html && typeof html.getAttribute === "function" && html.getAttribute("data-platform") === "phone");
    };

    function on(node, type, handler) {
      if (node && typeof node.addEventListener === "function") node.addEventListener(type, handler);
    }

    function closestFrom(target, selector) {
      let node = target;
      if (node && typeof node.closest !== "function" && node.parentElement) node = node.parentElement;
      return node && typeof node.closest === "function" ? node.closest(selector) : null;
    }

    /** Was the click inside `selector`?  (The path computed at dispatch time: a handler may repaint - and detach - the clicked button first.) */
    function inPath(evt, selector) {
      const path = evt && typeof evt.composedPath === "function" ? evt.composedPath() : [];
      if (path.length) return path.some((n) => n && typeof n.matches === "function" && n.matches(selector));
      return Boolean(closestFrom(evt && evt.target, selector));
    }

    function setHtmlIfChanged(node, html, key) {
      if (!node) return;
      if (st[key] === html) return;
      st[key] = html;
      node.innerHTML = html;
    }

    /** Is the keyboard focus inside `node` (a text box of it)?  (False under the test DOM, which has no contains().) */
    function hasFocus(node) {
      const active = doc.activeElement;
      return Boolean(active && node && typeof node.contains === "function" && node.contains(active));
    }

    /** ... and is it in a text box (a repaint would drop the caret; a focused button does not mind)? */
    function typingIn(node) {
      const active = doc.activeElement;
      return Boolean(active && hasFocus(node) && /^(TEXTAREA|INPUT)$/i.test(String(active.tagName || "")));
    }

    // -----------------------------------------------------------------------------------------------------------------
    // the bots: who sits where, what we know of each
    // -----------------------------------------------------------------------------------------------------------------

    const botList = () => (session().bots && Array.isArray(session().bots.bots) ? session().bots.bots : []);
    const participantOf = (id) => (id && id !== MOD ? View.participantOf(session(), id) : null);
    const botIdOf = (actorId) => {
      const p = participantOf(actorId);
      return p && p.bot_id ? p.bot_id : "";
    };
    const summaryOf = (botId) => (botId ? botList().find((b) => b.id === botId) || null : null);
    const detailOf = (botId) => st.detail.get(botId) || null;
    const openBotId = () => (st.openId && st.openId !== MOD ? botIdOf(st.openId) : "");
    /** The seat ("N3") a bot sits in (its participant, else the seat its summary names), "" when it is not at the table. */
    function seatOfBot(botId) {
      const p = (session().participants || []).find((x) => x.bot_id === botId);
      if (p) return p.id;
      const sum = summaryOf(botId);
      return sum && sum.seat ? sum.seat : "";
    }
    const jobOf = () => (session().bots && session().bots.init_job) || null;
    const initProgressOf = (botId) => (session().init_progress && session().init_progress[botId]) || null;

    /** GET api/bots/{id}: the BotDetail of a bot.  `force`: also when its summary says nothing changed. */
    async function readDetail(botId, force = false) {
      if (!botId || !st.available) return;
      const sum = summaryOf(botId);
      if (!sum) return;
      const fp = Lib.fingerprint(sum);
      // (one read per state of the summary: it is under way, done - or failed, and then a repaint must not ask again and again)
      if (!force && st.detailFp[botId] === fp) return;
      st.reading[botId] = true;
      st.detailFp[botId] = fp;
      const ticket = ++st.rev;
      try {
        const d = await api.get(`api/bots/${enc(botId)}`);
        if (d && typeof d === "object" && ticket >= (st.detailRev[botId] || 0)) {
          st.detail.set(botId, d);
          st.detailRev[botId] = ticket;
          delete st.detailErr[botId];
        }
      } catch (e) {
        st.detailErr[botId] = `读取失败：${errText(e)}`;
      } finally {
        delete st.reading[botId];
        if (openBotId() === botId) render({ head: true, live: true, editors: true }, true);
      }
    }

    /** GET api/bots: the limits, the default library and the live part (the line-up, every bot's state) - the store keeps the live part. */
    async function refreshBots() {
      const ticket = ++st.botsRev;
      try {
        const res = await api.get("api/bots");
        if (ticket < st.botsRev) return null; // a newer read is on its way: this answer is older than that one
        if (res && typeof res === "object") {
          st.limits = Lib.normalizeLimits(res.limits);
          if (Array.isArray(res.defaults)) st.defaults = res.defaults.filter((d) => d && typeof d === "object" && d.name);
          applyBots(res);
          st.loaded = true;
          st.available = true;
        }
        return res;
      } catch (e) {
        if (e && e.status === 404) st.available = false;
        return null;
      }
    }

    /** The answer of a write: a BotDetail (its `id` may be a new one - a bot that was reset by a rename). */
    function setDetail(botId, detail) {
      if (!detail || typeof detail !== "object") return;
      const id = detail.id || botId;
      if (id !== botId) {
        st.detail.delete(botId);
        delete st.detailFp[botId];
        editors.resetBot(botId);
      }
      st.detail.set(id, detail);
      st.detailRev[id] = ++st.rev;
      delete st.detailErr[id];
      render({ head: true, live: true, editors: true }, true);
      refreshBots().then(() => {
        const sum = summaryOf(id);
        if (sum) st.detailFp[id] = Lib.fingerprint(sum);
        render({ head: true, live: true, editors: true }, true);
      });
    }

    // -----------------------------------------------------------------------------------------------------------------
    // the editors (botedit.js)
    // -----------------------------------------------------------------------------------------------------------------

    async function downloadFile(path) {
      if (typeof opts.download === "function") {
        // (these GETs need the token as a HEADER, which a link cannot send: the page fetches the bytes and saves them)
        try {
          await opts.download(path);
        } catch (e) {
          showToast(`下载失败：${errText(e)}`);
        }
        return;
      }
      const url = buildUrl(path, { token: true });
      try {
        const a = doc.createElement("a");
        a.href = url;
        a.download = "";
        if (doc.body && typeof doc.body.appendChild === "function") doc.body.appendChild(a);
        a.click();
        if (typeof a.remove === "function") a.remove();
      } catch {
        showToast("无法开始下载");
      }
    }

    function openFilePicker(kind) {
      const input = kind === "core" ? el.fileCore : el.fileBot;
      if (!input) return;
      input.value = "";
      if (typeof input.click === "function") input.click();
    }

    const editors = Editors.create({
      api,
      Core,
      Lib,
      doc,
      win,
      showToast,
      openDialog: (o) => openDialog(o),
      limits: () => st.limits,
      defaults: () => st.defaults,
      session,
      isIdle,
      isReplay,
      openBotId,
      summaryOf,
      detailOf,
      detailError: (botId) => st.detailErr[botId] || "",
      setDetail,
      adopt: adoptBot,
      // `soft`: the answer of a request, not a click - it must not take the caret out of the box the user is typing in
      repaint: (soft) => render({ editors: true, head: true }, Boolean(soft)),
      refreshBots,
      download: downloadFile,
      openFilePicker,
    });

    // -----------------------------------------------------------------------------------------------------------------
    // detail panel: head
    // -----------------------------------------------------------------------------------------------------------------

    function nameValue(p) {
      return st.nameDraft[p.id] !== undefined ? st.nameDraft[p.id] : p.name;
    }

    /** The description of the avatar the page shows for `id` (null: the server sent none). */
    function avatarOf(id) {
      const s = session();
      if (id === MOD) return s.moderator && s.moderator.avatar ? s.moderator.avatar : null;
      const p = View.participantOf(s, id);
      return p && p.avatar ? p.avatar : null;
    }

    /** The other seats (and the moderator) whose avatar looks exactly like `desc`: ["2 号 Bob", "主持人"]. */
    function twinsOf(id, desc) {
      const s = session();
      const out = [];
      for (const p of s.participants || []) if (p.id !== id && Avatar.sameLook(desc, p.avatar)) out.push(`${Core.numText(p.seat)} 号 ${p.name}`);
      if (id !== MOD && s.moderator && Avatar.sameLook(desc, s.moderator.avatar)) out.push("主持人");
      return out;
    }

    /** What typing / pressing "按编号生成" makes of a code: {ok, description, twins} or {ok: false, reason, field?, part?, message}. */
    function codeCheck(id, text) {
      // (no `trim()` of our own: the decoder says "empty" itself, by the rules the backend shares - tests/avatar_code_cases.json)
      const r = Avatar.decode(String(text === undefined || text === null ? "" : text), Avatar.getParts());
      return r.ok ? { ...r, twins: twinsOf(id, r.description) } : r;
    }

    function avatarPopHtml(id) {
      const busy = Boolean(st.saving[`avatar:${id}`] || st.saving[`regen:${id}`]);
      const note = st.codeNote[id];
      return (
        `<div class="avatar-pop" id="avatar-pop">` +
        `<div class="avatar-pop-row"><button type="button" class="btn btn-xs" id="btn-detail-regen" data-detail-action="regen-avatar"${busy ? " disabled" : ""}>🎲 随机生成</button>` +
        `<span class="form-hint">随机组合，保证和其他头像至少 3 项不同</span></div>` +
        `<div class="avatar-pop-row"><input type="text" class="form-input avatar-code-input" id="input-avatar-code" value="${esc(st.codeDraft[id] || "")}"` +
        ` placeholder="输入头像编号，如 f03s05h12c07e04m02a09k03" aria-label="头像编号（不分大小写，可以用空格或 - 分隔）" autocomplete="off" spellcheck="false" />` +
        `<button type="button" class="btn btn-xs btn-primary-outline" id="btn-detail-apply-code" data-detail-action="apply-code"${busy ? " disabled" : ""}>按编号生成</button></div>` +
        `<div class="avatar-pop-note${note ? ` ${esc(note.kind)}` : ""}" id="avatar-pop-note" role="status">${note ? esc(note.text) : ""}</div></div>`
      );
    }

    /** The row under the avatar: its code, 复制, and (before the game starts) the 🎲 that opens the panel. */
    function avatarRowHtml(id, desc, canEdit, frozen) {
      const code = Avatar.codeOf(desc);
      if (!code && !canEdit) return "";
      const open = canEdit && Boolean(st.avatarOpen[id]);
      return (
        `<div class="detail-code-row"><span class="avatar-code-label">头像编号</span>` +
        (code
          ? `<code class="avatar-code" id="avatar-code" title="点击选中，可以复制给别人">${esc(code)}</code>` +
            `<button type="button" class="btn btn-xs" id="btn-detail-copy-code" data-detail-action="copy-code" title="复制头像编号">复制</button>`
          : `<span class="form-hint">（这个头像没有编号）</span>`) +
        (canEdit
          ? `<button type="button" class="btn btn-xs avatar-dice${open ? " active" : ""}" id="btn-detail-avatar-pop" data-detail-action="toggle-avatar-pop"` +
            ` aria-expanded="${open}" title="${frozen ? "换头像会清空初始化结果和记忆（要先确认）" : "重新生成头像：随机，或输入编号（对局开始后不能改）"}">🎲</button>`
          : "") +
        `</div>${open ? avatarPopHtml(id) : ""}`
      );
    }

    function thoughtsActionHtml(id) {
      const why = thoughtsBlocked(id);
      const who = id === MOD ? "主持人" : "这名玩家";
      return why
        ? `<button type="button" class="btn btn-xs" id="btn-detail-thoughts" data-detail-action="open-thoughts" disabled title="${esc(why)}">🧠 思考记录</button>`
        : `<button type="button" class="btn btn-xs" id="btn-detail-thoughts" data-detail-action="open-thoughts" title="像聊天记录一样看${who}的思考（不含工具调用）">🧠 思考记录</button>`;
    }

    /** The reason, in words under the buttons: a disabled button alone does not say why (and a phone has no tooltips). */
    function thoughtsNoteHtml(id) {
      const why = thoughtsBlocked(id);
      return why ? `<div class="form-hint detail-thoughts-note">🔒 ${esc(why)}</div>` : "";
    }

    /** Why a bot cannot be initialized right now ("" = it can): the server refuses in the same cases (409 game_active / init_busy). */
    function initBlockedReason() {
      if (!st.available) return "后端没有 bots 接口（版本太旧）。";
      if (isReplay()) return "正在回放一局存档：回到当前对局再初始化。";
      const status = session().status;
      if (status === "running" || status === "paused") return "对局进行中（或已暂停），不能初始化：请先新建对局。";
      if (jobOf()) return "已经有一个初始化任务在运行：请等它结束，或者先取消。";
      return "";
    }

    function initRowHtml(sum) {
      const v = Lib.initView(sum);
      const progress = initProgressOf(sum.id);
      const job = jobOf();
      const mine = Boolean(job && job.ids.includes(sum.id));
      const pv = v.status === "initializing" ? Lib.initProgressView(progress, job ? job.deadline_s : 120) : null;
      const blocked = initBlockedReason();
      const label = v.status === "ready" || v.status === "stale" ? "重新初始化" : v.status === "failed" ? "重试初始化" : "初始化";
      const noCore = !sum.core.complete;
      const reason = blocked || (noCore ? "底层设定还不完整：先补全三个必填分区。" : "");
      const bar =
        pv && pv.running
          ? `<div class="init-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pv.percent}"><div class="init-bar-fill" id="init-bar-fill" style="width:${pv.percent}%"></div></div>`
          : "";
      const note = st.initNote[sum.id];
      return (
        `<div class="detail-init init-${v.tone}" id="detail-init" data-init="${v.status}">` +
        `<div class="detail-init-line"><span class="init-symbol" title="${esc(v.label)}">${v.symbol}</span><span class="init-label">${esc(v.label)}</span>` +
        (v.status === "initializing" && mine
          ? `<button type="button" class="btn btn-xs" id="btn-detail-init-cancel" data-detail-action="init-cancel">取消初始化</button>`
          : `<button type="button" class="btn btn-xs btn-primary-outline" id="btn-detail-init" data-detail-action="init-one"${reason || v.status === "initializing" ? " disabled" : ""}` +
            ` title="${esc(reason || "让它读契约和规则，写出自己的笔记和打法")}">${label}</button>`) +
        `</div>` +
        `<div class="form-hint init-detail" id="detail-init-text">${esc(pv ? pv.line : v.detail)}</div>${bar}` +
        (reason && v.status !== "initializing" ? `<div class="form-hint init-reason">${esc(reason)}</div>` : "") +
        (note ? `<div class="form-error init-note" id="detail-init-note">${esc(note)}</div>` : "") +
        (sum.binding && !sum.binding.ok ? `<div class="form-hint error init-nobind">还没有给它绑定模型：点下面的「配置」。</div>` : "") +
        `</div>`
      );
    }

    function headHtml(id) {
      const s = session();
      if (id === MOD) {
        const name = (s.moderator && s.moderator.name) || "主持人";
        // the moderator has a random pixel avatar of its own (the fixed top hat only when the server sent none)
        const avatar = s.moderator && s.moderator.avatar ? s.moderator.avatar : null;
        // (a backend that sends no moderator avatar does not have the endpoint either: no 🎲 then)
        const canRegen = Boolean(avatar) && isIdle() && st.available;
        return (
          `<div class="detail-head-row">` +
          `<div class="detail-avatar">${Avatar.imgHtml(MOD, undefined, avatar, { size: 80 })}</div>` +
          `<div class="detail-title"><div class="detail-name">${esc(name)}</div>` +
          `<div class="detail-sub">主持人</div></div>` +
          `<button type="button" class="pop-close" data-detail-action="close" aria-label="关闭">✕</button></div>` +
          avatarRowHtml(MOD, avatar, canRegen, false) +
          `<div class="detail-actions">${thoughtsActionHtml(MOD)}</div>` +
          thoughtsNoteHtml(MOD)
        );
      }
      const p = View.participantOf(s, id);
      if (!p) return null;
      const sum = summaryOf(p.bot_id);
      const frozen = Boolean(sum && sum.frozen);
      const editable = isIdle() && st.available && Boolean(sum);
      const nameBusy = Boolean(st.saving[`name:${id}`]);
      const err = st.nameError[id] || "";
      const lock = frozen
        ? `<span class="lock-badge" title="已初始化（或学到了记忆）：改名或换头像会清空它的初始化结果和全部记忆，底层设定保留；改之前要先确认。">🔒</span>`
        : "";
      const nameBlock = editable
        ? `<div class="detail-name-row">${lock}<input type="text" class="form-input detail-name-input" id="input-detail-name"` +
          ` value="${esc(nameValue(p))}" aria-label="名字（1–20 个字符，不能和其他玩家重名）" autocomplete="off" spellcheck="false" />` +
          `<button type="button" class="btn btn-xs btn-primary-outline" id="btn-detail-save-name" data-detail-action="save-name"` +
          `${nameBusy ? " disabled" : ""}>保存名字</button></div>` +
          `<div class="form-error detail-error"${err ? "" : " hidden"}>${esc(err)}</div>` +
          (frozen ? `<div class="form-hint detail-frozen" id="detail-frozen">🔒 已初始化：改名或换头像会清空初始化结果和记忆（要先确认）。</div>` : "")
        : `<div class="detail-name" title="${isIdle() ? "" : "对局未开始时才能改名"}">${esc(p.name)}${lock}</div>`;
      const actions = [thoughtsActionHtml(id)];
      if (lockedSeat()) {
        // (F3) a played seat is the viewpoint until it is given back: looking from the others is not possible
        if (lockedSeat() !== id) {
          actions.push(
            `<button type="button" class="btn btn-xs" data-detail-action="view-as" disabled title="接管期间视角已锁定为你扮演的座位；退出接管后才能切换">👁 以 ${esc(p.name)} 的视角观看</button>`
          );
        }
      } else if (getViewpoint() !== id) {
        actions.push(`<button type="button" class="btn btn-xs" data-detail-action="view-as" title="只看这名玩家能看到的内容">👁 以 ${esc(p.name)} 的视角观看</button>`);
      }
      return (
        `<div class="detail-head-row">` +
        `<div class="detail-avatar">${Avatar.imgHtml(id, p.seat, p.avatar, { size: 80 })}<span class="pcard-num detail-num">${Core.numText(p.seat)}</span></div>` +
        `<div class="detail-title">${nameBlock}<div class="detail-sub">${Core.numText(p.seat)} 号${p.out ? " · 已出局" : ""}</div></div>` +
        `<button type="button" class="pop-close" data-detail-action="close" aria-label="关闭">✕</button></div>` +
        avatarRowHtml(id, p.avatar, editable, frozen) +
        (sum ? initRowHtml(sum) : p.bot_id ? "" : `<div class="form-hint detail-nobot">这局是旧版存档：没有 bot 信息，不能初始化或编辑底层设定。</div>`) +
        `<div class="detail-actions">${actions.join("")}</div>` +
        thoughtsNoteHtml(id) +
        markHeadHtml(id) // v0.13.1 (G16 / G18): the colour palette under the actions (marks.js)
      );
    }

    // -----------------------------------------------------------------------------------------------------------------
    // detail panel: live part (state, tags, model, metrics, review)
    // -----------------------------------------------------------------------------------------------------------------

    function statHtml(value, label, extra) {
      return `<div class="pop-stat${extra || ""}"><span class="pop-stat-num">${value}</span><span class="pop-stat-label">${esc(label)}</span></div>`;
    }

    /** "3 号 Carol" (the live nickname), escaped. */
    function seatLabelOf(id) {
      const p = View.participantOf(session(), id);
      return p ? `${Core.numText(p.seat)} 号 ${esc(p.name)}` : esc(id);
    }

    /**
     * What a finished review changed (H33): per memory file the old text and the new one, and how many proposals for the L0 now wait to be
     * confirmed.  It comes with the `review.progress` event (`changes`); the client keeps it while the page lives.
     */
    function changesHtml(id) {
      const s = session();
      const progress = s.review && s.review.progress ? s.review.progress[id] : null;
      const ch = progress && progress.changes;
      if (!ch) return "";
      const files = Array.isArray(ch.files) ? ch.files : [];
      const proposals = Number(ch.proposals) || 0;
      if (!files.length && !proposals) {
        return `<div class="review-changes"><div class="review-changes-head">本次复盘没有改动记忆（战绩已计入）。</div></div>`;
      }
      const open = st.changesOpen[id] !== false;
      const names = files.map((f) => MEMORY_LABELS[f.file] || f.file);
      const summary = `本次改动：${names.length ? names.join("、") : "记忆没有变化"}${proposals ? `；${Core.numText(proposals)} 条底层设定提案待确认` : ""}（旧 → 新）`;
      const items = files.map(
        (f) =>
          `<li class="rc-item"><div class="rc-who">${esc(MEMORY_LABELS[f.file] || f.file)}</div>` +
          `<div class="stage-diff">${Lib.lineDiff(f.before, f.after)
            .map((l) => `<div class="diff-line ${l.kind}"><span class="diff-mark">${l.kind === "add" ? "+" : l.kind === "del" ? "−" : " "}</span><span class="diff-text">${esc(l.text) || "&nbsp;"}</span></div>`)
            .join("")}</div></li>`
      );
      if (proposals) {
        items.push(
          `<li class="rc-item"><div class="rc-who">底层设定提案</div><div class="rc-line">${Core.numText(proposals)} 条等你确认：` +
            `<button type="button" class="btn btn-xs" data-detail-action="show-staging">去看提案</button></div></li>`
        );
      }
      return (
        `<div class="review-changes"><button type="button" class="review-changes-toggle" data-detail-action="toggle-changes" aria-expanded="${open}">` +
        `<span class="caret" aria-hidden="true">${open ? "▼" : "▶"}</span>${esc(summary)}</button>` +
        (open ? `<ul class="review-changes-list">${items.join("")}</ul>` : "") +
        `</div>`
      );
    }

    function reviewHtml(id) {
      const rv = View.reviewView(session());
      if (!rv.visible) return "";
      const one = rv.of(id);
      if (one.state === "done") {
        return (
          `<div class="detail-review done"><span class="review-badge done">✓ 已复盘</span>` +
          `<button type="button" class="btn btn-xs" data-detail-action="show-memory">查看写入的记忆</button>` +
          `<span class="form-hint">战绩已计入；记忆里的改动直接生效，没有回滚。</span></div>` +
          changesHtml(id)
        );
      }
      if (one.state === "running") {
        const left = one.secondsLeft === null || one.secondsLeft === undefined ? "" : `（还剩 ${Core.numText(one.secondsLeft)} 秒）`;
        return `<div class="detail-review"><span class="review-badge running">⏳ 复盘中…${esc(left)}</span>${one.message ? `<span class="form-hint">${esc(one.message)}</span>` : ""}</div>`;
      }
      const why = !rv.supported ? "这局是旧版存档，不能复盘。" : !rv.available ? "对局还没有结束，或者现在不能复盘。" : "";
      return (
        `<div class="detail-review"><button type="button" class="btn btn-xs btn-primary-outline" data-detail-action="review-one"` +
        `${rv.available && !st.busy.review ? "" : " disabled"}>复盘此玩家</button>` +
        `${one.state === "error" ? `<span class="form-hint error">上次复盘失败：${esc(one.message)}</span>` : `<span class="form-hint">${esc(why || "让该玩家用自己的模型复盘本局（最多 60 秒），更新自己的记忆。")}</span>`}</div>`
      );
    }

    function liveHtml(id) {
      const s = session();
      const vp = getViewpoint();
      const isMod = id === MOD;
      const p = isMod ? null : View.participantOf(s, id);
      if (!isMod && !p) return null;
      const node = isMod ? s.moderator : p;
      const state = View.stateBadge(vp, id, node.state);
      const stateRow = state
        ? `<div class="detail-state st-${state}"><span class="detail-state-icon">${View.STATE_ICONS[state]}</span>` +
          `<span>${esc(View.stateTitle(state, node.sleep_until, getClockNow()))}</span></div>`
        : "";
      let chips;
      if (isMod) {
        chips = `<span class="tag-badge warning">主持人</span>`;
      } else if (s.dealt === false) {
        chips = ""; // (H52) the identities are dealt when the game starts: before that nothing is shown - not even a "?"
      } else {
        const tags = View.visibleTags(s, vp, p);
        const main = View.mainTagInfo(s, vp, p);
        const parts = [];
        if (!main.known) parts.push(`<span class="tag-badge muted" title="当前视角看不到这名玩家的身份">身份：?</span>`);
        for (const t of tags) parts.push(`<span class="tag-badge ${esc(View.tagVariant(s, t))}">${esc(t)}</span>`);
        chips = parts.join("");
      }
      const m = View.actorMetrics(s, id);
      const bind = bindingText(id);
      // activity (4 columns), then the four token kinds (4 columns), then the cost
      const grid =
        statHtml(Core.numText(m.activations), "激活次数") +
        statHtml(Core.numText(m.calls), "LLM 调用") +
        statHtml(Core.numText(m.thinkingRounds), "思考次数") +
        statHtml(Core.numText(m.toolCalls), "工具调用") +
        statHtml(Core.numText(m.rejections), "被拒绝") +
        statHtml(esc(Core.formatPercent(m.cacheHitRate)), "缓存命中率") +
        statHtml(esc(Core.formatCost(m.costUsd)), "费用（累计）", " pop-stat-span2 pop-stat-cost") +
        `<div class="pop-caption">token 用量</div>` +
        statHtml(esc(Core.formatCount(m.promptTokens)), "未命中输入", " pop-tok pop-tok-miss") +
        statHtml(esc(Core.formatCount(m.cacheReadTokens)), "缓存读取", " pop-tok pop-tok-read") +
        statHtml(esc(Core.formatCount(m.cacheWriteTokens)), "缓存写入", " pop-tok pop-tok-write") +
        statHtml(esc(Core.formatCount(m.completionTokens)), "输出", " pop-tok pop-tok-out");
      return (
        stateRow +
        `<div class="pop-tags">${chips}</div>` +
        `<div class="pop-model"><span class="pop-label">模型</span>` +
        `<span class="pop-model-text" title="${esc(bind)}">${esc(bind)}</span>` +
        `<button type="button" class="btn btn-xs" data-detail-action="bind">配置</button></div>` +
        `<div class="pop-grid">${grid}</div>` +
        (isMod ? "" : reviewHtml(id))
      );
    }

    function editorsHtml(id) {
      if (id === MOD) return "";
      if (isReplay()) return `<div class="form-hint editors-replay">正在回放一局存档：这里不能编辑当前对局的 bot。</div>`;
      const botId = botIdOf(id);
      if (!botId) return "";
      if (!st.available) return `<div class="form-hint error">bots 接口不可用（后端可能尚未升级），暂时不能编辑。</div>`;
      return editors.html(botId);
    }

    /**
     * Repaint the open panel.  `only` = {head, live, editors}; default everything.  `soft`: a repaint that nobody asked for with a click (the answer
     * of a request, an event): a region in which the user is typing waits - it is drawn when the focus leaves it (see `deferred`).
     */
    function render(only, soft) {
      if (!el.panel) return;
      if (!st.openId) {
        el.panel.hidden = true;
        if (el.backdrop) el.backdrop.hidden = true;
        return;
      }
      const want = only || { head: true, live: true, editors: true };
      const head = headHtml(st.openId);
      if (head === null) {
        // the agent disappeared (a new game): close quietly
        close();
        return;
      }
      const botId = openBotId();
      if (botId && !isReplay()) readDetail(botId); // (a no-op while the summary of the bot says nothing changed since it was read)
      // (v0.13.1) a repaint that nobody asked for - an event, the 1 s ticker - must not replace the text box the user is
      // typing in (its caret and focus would be lost): such a region waits until the focus leaves it
      const asked = (part) => !soft && Boolean(only && only[part]);
      const may = (part, node) => (soft ? !typingIn(node) : asked(part) || !hasFocus(node));
      if (want.head) {
        if (may("head", el.head)) setHtmlIfChanged(el.head, head, "lastHead");
        else st.deferred = true;
      }
      if (want.live) setHtmlIfChanged(el.live, liveHtml(st.openId) || "", "lastLive");
      if (want.editors) {
        if (may("editors", el.editors)) setHtmlIfChanged(el.editors, editorsHtml(st.openId), "lastEditors");
        else st.deferred = true;
      }
      el.panel.hidden = false;
      if (el.backdrop) el.backdrop.hidden = !isPhone();
      position();
    }

    function resetCaches() {
      st.lastHead = null;
      st.lastLive = null;
      st.lastEditors = null;
    }

    function position() {
      if (!el.panel || !st.openId) return;
      const style = el.panel.style;
      if (isPhone()) {
        style.left = "";
        style.top = "";
        style.maxHeight = "";
        return;
      }
      let anchor = null;
      if (st.openId === MOD) anchor = el.modChip;
      else if (el.grid && typeof el.grid.querySelector === "function") {
        const sel = `[data-actor="${typeof CSS !== "undefined" && CSS && CSS.escape ? CSS.escape(st.openId) : st.openId}"]`;
        anchor = el.grid.querySelector(sel);
      }
      if (!anchor || typeof anchor.getBoundingClientRect !== "function") return;
      const r = anchor.getBoundingClientRect();
      const vw = Number(win.innerWidth) || 1280;
      const vh = Number(win.innerHeight) || 800;
      const pw = Math.min(el.panel.offsetWidth || 400, vw - 16);
      // the card is partly lower than the panel is tall: shift the panel up so that all of it is on
      // screen (it may then cover the card row; the card it belongs to stays highlighted)
      const ph = Math.min(el.panel.scrollHeight || el.panel.offsetHeight || 480, vh - 16);
      let left = Math.min(Math.max(8, r.left), Math.max(8, vw - pw - 8));
      if (st.openId === MOD) left = Math.min(Math.max(8, r.right - pw), Math.max(8, vw - pw - 8));
      const top = Math.max(8, Math.min(r.bottom + 6, vh - ph - 8));
      style.left = `${Math.round(left)}px`;
      style.top = `${Math.round(top)}px`;
      style.maxHeight = `${Math.max(220, Math.round(vh - top - 8))}px`;
    }

    function open(id) {
      if (st.openId !== id) {
        st.openId = id;
        resetCaches();
        const botId = botIdOf(id);
        if (botId && !isReplay()) readDetail(botId, true);
      }
      render();
      onChange();
    }

    function close() {
      if (st.openId === null) return;
      st.openId = null;
      resetCaches();
      render();
      onChange();
    }

    function toggle(id) {
      if (st.openId === id) close();
      else open(id);
    }

    // -----------------------------------------------------------------------------------------------------------------
    // dialogs
    // -----------------------------------------------------------------------------------------------------------------

    // v0.13: `finish` of the dialog that is open right now (Esc answers it "no"; a new dialog answers the old one "no")
    let activeFinish = null;

    /** A small modal: resolves {ok, values}. `collect()` reads the fields when the user confirms; Esc = cancel. */
    function openDialog(o) {
      return new Promise((resolve) => {
        if (!el.dialog || !el.dialogBody) {
          resolve({ ok: typeof win.confirm === "function" ? win.confirm(String(o.title || "确认")) : false, values: {} });
          return;
        }
        if (activeFinish) activeFinish(false); // never leave the promise of a replaced dialog hanging
        if (el.dialogTitle) el.dialogTitle.textContent = o.title || "确认";
        el.dialogBody.innerHTML = o.bodyHtml || "";
        if (el.dialogOk) {
          el.dialogOk.textContent = o.okText || "确定";
          el.dialogOk.className = `btn ${o.danger ? "btn-danger-outline" : "btn-primary-outline"}`;
          el.dialogOk.disabled = Boolean(o.okDisabled);
        }
        if (el.dialogCancel) {
          el.dialogCancel.textContent = o.cancelText || "取消";
          el.dialogCancel.hidden = Boolean(o.hideCancel);
        }
        el.dialog.style.display = "flex";
        let done = false;
        const finish = (ok) => {
          if (done) return;
          done = true;
          if (activeFinish === finish) activeFinish = null;
          el.dialog.style.display = "none";
          if (el.dialogOk) el.dialogOk.removeEventListener("click", onOk);
          if (el.dialogCancel) el.dialogCancel.removeEventListener("click", onCancel);
          if (el.dialogClose) el.dialogClose.removeEventListener("click", onCancel);
          if (typeof doc.removeEventListener === "function") doc.removeEventListener("keydown", onKey);
          resolve({ ok, values: ok && typeof o.collect === "function" ? o.collect() : {} });
        };
        const onOk = () => finish(true);
        const onCancel = () => finish(false);
        const onKey = (evt) => {
          if (evt && evt.key === "Escape") finish(false);
        };
        if (el.dialogOk) el.dialogOk.addEventListener("click", onOk);
        if (el.dialogCancel) el.dialogCancel.addEventListener("click", onCancel);
        if (el.dialogClose) el.dialogClose.addEventListener("click", onCancel);
        if (typeof doc.addEventListener === "function") doc.addEventListener("keydown", onKey);
        activeFinish = finish;
      });
    }

    // -----------------------------------------------------------------------------------------------------------------
    // detail panel: name and avatar (H23: a bot that has learned something is frozen - changing it resets the bot)
    // -----------------------------------------------------------------------------------------------------------------

    /** The confirmation of H23.  `err`: the answer of the server (409 needs_confirm) whose own sentence is shown when it has one. */
    async function confirmReset(sum, err) {
      const sentence = (err && err.detail) || RESET_CONFIRM;
      const choice = await openDialog({
        title: "修改名字或头像",
        bodyHtml: `<div class="dialog-reset" id="dialog-reset-text">${esc(sentence)}</div>` + (sum ? `<div class="form-hint">玩家：${esc(sum.name)}</div>` : ""),
        okText: "确认修改",
        danger: true,
      });
      return Boolean(choice && choice.ok);
    }

    /**
     * A change of the name or the avatar of the bot at the open seat.  `call(confirmReset)` makes the request (the first time without
     * `confirm_reset` unless the bot is frozen: then the user is asked first).  Resolves the BotDetail answer, or null (cancelled / failed).
     */
    async function changeBot(id, call) {
      const botId = botIdOf(id);
      const sum = summaryOf(botId);
      if (!sum) return null;
      let confirmed = false;
      if (sum.frozen) {
        if (!(await confirmReset(sum))) return null;
        confirmed = true;
      }
      try {
        return await call(confirmed);
      } catch (e) {
        if (e && e.code === "needs_confirm" && !confirmed) {
          // (the bot was frozen by something we did not know yet: a review, another tab)
          if (!(await confirmReset(sum, e))) return null;
          return call(true);
        }
        throw e;
      }
    }

    /**
     * The BotDetail that answers a change of a bot's name / avatar / everything (a rename, an import): the panel follows the (maybe new) id, the card
     * and the other views follow the seat.
     */
    function adoptBot(botId, detail) {
      if (!detail || typeof detail !== "object") return;
      const seat = detail.seat || seatOfBot(botId);
      setDetail(botId, detail);
      if (seat) patchParticipants([{ id: seat, name: detail.name, avatar: detail.avatar }]);
    }

    const adoptChanged = (id, detail) => adoptBot(botIdOf(id), detail);

    async function saveName() {
      const id = st.openId;
      const p = id ? View.participantOf(session(), id) : null;
      if (!p || st.saving[`name:${id}`]) return;
      const input = $("input-detail-name");
      const raw = input ? input.value : nameValue(p);
      // An unchanged name is nothing to save - whatever it is.  Only a NEW nickname is held to the rules.
      if (String(raw === undefined || raw === null ? "" : raw).trim() === p.name) {
        st.nameError[id] = "";
        delete st.nameDraft[id];
        render({ head: true });
        return;
      }
      const others = (session().participants || []).filter((x) => x.id !== id).map((x) => x.name);
      const bad = Lib.botNameProblem(raw, others, st.limits);
      if (bad) {
        st.nameError[id] = bad;
        st.nameDraft[id] = raw;
        render({ head: true });
        return;
      }
      const name = String(raw).trim();
      const typed = st.nameDraft[id]; // what the box held when Save was pressed
      st.saving[`name:${id}`] = true;
      st.nameError[id] = "";
      render({ head: true });
      try {
        const botId = botIdOf(id);
        const detail = await changeBot(id, (confirm) => api.put(`api/bots/${enc(botId)}`, confirm ? { name, confirm_reset: true } : { name }));
        if (detail) {
          // what was typed while the request was on its way is a LATER edit: it stays (an unsaved difference to the name saved now)
          if (st.nameDraft[id] === typed) delete st.nameDraft[id];
          adoptChanged(id, detail);
          showToast(`名字已保存：${detail.name || name}`);
          onChange();
        }
      } catch (e) {
        st.nameError[id] = errText(e);
        showToast(st.nameError[id]);
      } finally {
        delete st.saving[`name:${id}`];
        render({ head: true });
      }
    }

    async function regenAvatar() {
      const id = st.openId;
      if (!id || st.saving[`regen:${id}`]) return;
      st.saving[`regen:${id}`] = true;
      st.codeNote[id] = null;
      render({ head: true });
      try {
        if (id === MOD) {
          // the moderator is not a bot: its new avatar goes straight to the session's moderator
          const res = await api.post("api/moderator/avatar:regenerate");
          if (res && res.avatar && typeof res.avatar === "object") patchParticipants([{ id: MOD, avatar: res.avatar }]);
          render();
          onChange();
        } else {
          const botId = botIdOf(id);
          const detail = await changeBot(id, (confirm) => api.post(`api/bots/${enc(botId)}/avatar:regenerate`, confirm ? { confirm_reset: true } : {}));
          if (!detail) return;
          adoptChanged(id, detail);
        }
        st.codeNote[id] = { kind: "ok", text: "已随机生成新头像。" };
        showToast(id === MOD ? "主持人头像已重新生成" : "头像已重新生成");
      } catch (e) {
        st.codeNote[id] = { kind: "error", text: errText(e) };
        showToast(errText(e));
      } finally {
        delete st.saving[`regen:${id}`];
        render({ head: true });
      }
    }

    // ---- the avatar code: copy it, make an avatar from a code ----------------------------------------------------------

    function toggleAvatarPop() {
      const id = st.openId;
      if (!id) return;
      st.avatarOpen[id] = !st.avatarOpen[id];
      render({ head: true });
    }

    /** navigator.clipboard where the page may use it; else the old hidden-text-box way. Resolves whether it worked. */
    async function copyText(text) {
      try {
        const nav = win.navigator;
        if (nav && nav.clipboard && typeof nav.clipboard.writeText === "function") {
          await nav.clipboard.writeText(text);
          return true;
        }
      } catch {
        /* not allowed (an insecure page, a denied permission): try the fallback */
      }
      try {
        if (doc.body && typeof doc.createElement === "function" && typeof doc.execCommand === "function") {
          const box = doc.createElement("textarea");
          box.value = text;
          if (typeof box.setAttribute === "function") box.setAttribute("readonly", "");
          box.style.position = "fixed";
          box.style.opacity = "0";
          doc.body.appendChild(box);
          if (typeof box.select === "function") box.select();
          const ok = doc.execCommand("copy");
          if (typeof box.remove === "function") box.remove();
          return Boolean(ok);
        }
      } catch {
        /* nothing else to try */
      }
      return false;
    }

    async function copyCode() {
      const id = st.openId;
      if (!id) return;
      const code = Avatar.codeOf(avatarOf(id));
      if (!code) return;
      const ok = await (opts.copyText || copyText)(code);
      showToast(ok ? `头像编号已复制：${code}` : "复制失败：请手动选中编号再复制");
    }

    /** Typing in the code box: a valid code says so (and warns about a twin) at once; a half-typed one says nothing yet. */
    function onCodeInput(id, text) {
      st.codeDraft[id] = text;
      const check = codeCheck(id, text);
      if (check.ok) {
        st.codeNote[id] = check.twins.length
          ? { kind: "hint", text: `和 ${check.twins.join("、")} 的头像完全相同：可以生成，但看起来会是同款。` }
          : { kind: "ok", text: "编号有效。" };
      } else {
        st.codeNote[id] = null;
      }
      const note = $("avatar-pop-note");
      if (note) {
        note.textContent = st.codeNote[id] ? st.codeNote[id].text : "";
        note.className = `avatar-pop-note${st.codeNote[id] ? ` ${st.codeNote[id].kind}` : ""}`;
      }
    }

    /** "按编号生成": the format and every part are checked here first (the server checks again); a twin is allowed, with a hint. */
    async function applyCode() {
      const id = st.openId;
      if (!id || st.saving[`avatar:${id}`]) return;
      const box = $("input-avatar-code");
      const text = box && typeof box.value === "string" ? box.value : st.codeDraft[id] || "";
      st.codeDraft[id] = text;
      const check = codeCheck(id, text);
      if (!check.ok) {
        st.codeNote[id] = { kind: "error", text: check.message };
        render({ head: true });
        return;
      }
      const current = avatarOf(id);
      if (current && Avatar.sameLook(check.description, current)) {
        st.codeNote[id] = { kind: "hint", text: "这已经是当前的头像。" };
        render({ head: true });
        return;
      }
      st.saving[`avatar:${id}`] = true;
      st.codeNote[id] = null;
      render({ head: true });
      try {
        if (id === MOD) {
          const res = await api.put("api/moderator", { avatar: check.description });
          patchParticipants([{ id: MOD, avatar: res && res.avatar && typeof res.avatar === "object" ? res.avatar : check.description }]);
          render();
          onChange();
        } else {
          const botId = botIdOf(id);
          const detail = await changeBot(id, (confirm) => api.put(`api/bots/${enc(botId)}`, confirm ? { avatar: check.description, confirm_reset: true } : { avatar: check.description }));
          if (!detail) return;
          adoptChanged(id, detail);
        }
        const code = Avatar.codeOf(check.description);
        st.codeDraft[id] = "";
        st.codeNote[id] = check.twins.length
          ? { kind: "hint", text: `头像已按编号生成。注意：和 ${check.twins.join("、")} 的头像完全相同（同款）。` }
          : { kind: "ok", text: "头像已按编号生成。" };
        showToast(check.twins.length ? `头像已按编号生成：${code}（和 ${check.twins.join("、")} 同款）` : `头像已按编号生成：${code}`);
      } catch (e) {
        st.codeNote[id] = { kind: "error", text: errText(e) || "生成失败" };
      } finally {
        delete st.saving[`avatar:${id}`];
        render({ head: true });
      }
    }

    // -----------------------------------------------------------------------------------------------------------------
    // initialization (H19–H22)
    // -----------------------------------------------------------------------------------------------------------------

    const labelOfBot = (sum) => `${sum.seat ? `${String(sum.seat).replace(/^N/, "")} 号 ` : ""}${sum.name}`;

    /** The list in the dialog of the global initialization: who is initialized, and why (the state of each). */
    function targetsHtml(all) {
      const list = botList();
      const ids = Lib.initTargets(list, all);
      if (!ids.length) return `<div class="form-hint init-none">现在没有需要初始化的玩家：全部都是 ✔。要重做的话，勾选下面的「全部重新初始化」。</div>`;
      return (
        `<div class="init-target-head">将初始化 ${Core.numText(ids.length)} 个玩家：</div><ul class="init-targets">` +
        ids
          .map((id) => {
            const sum = list.find((b) => b.id === id);
            const v = Lib.initView(sum);
            const warn = sum.binding && !sum.binding.ok ? `<span class="form-error"> 没有绑定模型</span>` : "";
            return `<li data-bot="${esc(id)}"><span class="init-symbol">${v.symbol}</span> ${esc(labelOfBot(sum))} <span class="form-hint">${esc(v.label)}</span>${warn}</li>`;
          })
          .join("") +
        `</ul>`
      );
    }

    /** POST api/bots/init {ids?, rebuild?}: the progress comes as bot.init.progress events.  Resolves whether the job started. */
    async function postInit(body) {
      st.busy.init = true;
      for (const k of Object.keys(st.initNote)) delete st.initNote[k];
      renderControls();
      try {
        const res = await api.post("api/bots/init", body);
        const n = res && Array.isArray(res.ids) ? res.ids.length : (body.ids || []).length;
        showToast(`已开始初始化 ${Core.numText(n)} 个玩家，每个最多 ${Core.numText((res && res.deadline_s) || 120)} 秒`);
        refreshBots();
        return true;
      } catch (e) {
        await showInitRefused(e, body);
        return false;
      } finally {
        st.busy.init = false;
        renderControls();
        render({ head: true });
      }
    }

    /** The server said no (409 game_active / init_busy / no_binding ...): say why where the user is looking. */
    async function showInitRefused(e, body) {
      const text = errText(e);
      const ids = (body && body.ids) || [];
      if (ids.length === 1) st.initNote[ids[0]] = text;
      showToast(text);
      const problems = Array.isArray(e && e.problems) ? e.problems : [];
      if (e && e.code === "no_binding" && problems.length) {
        const rows = problems
          .map((p) => `<li>${esc(p.name || p.seat || "")}：${esc(((p.items || [])[0] || {}).message || "没有绑定模型")}</li>`)
          .join("");
        const choice = await openDialog({
          title: "还不能初始化",
          bodyHtml: `<div>${esc(text)}</div><ul class="init-targets">${rows}</ul><div class="form-hint">初始化要用每个玩家自己绑定的模型。</div>`,
          okText: "去配置模型绑定",
          cancelText: "关闭",
        });
        if (choice && choice.ok) {
          const first = problems.find((p) => p.seat) || null;
          openBindings(first ? first.seat : MOD);
        }
      }
    }

    async function initOne(botId) {
      const sum = summaryOf(botId);
      if (!sum) return false;
      const blocked = initBlockedReason();
      if (blocked) {
        showToast(blocked);
        return false;
      }
      const choice = await openDialog({
        title: `初始化 ${sum.name}`,
        bodyHtml: `<div class="dialog-init" id="dialog-init-text">${esc(INIT_CONFIRM)}</div><div class="form-hint">将初始化：${esc(labelOfBot(sum))}（${esc(Lib.initView(sum).label)}）</div>`,
        okText: "开始初始化",
      });
      if (!choice || !choice.ok) return false;
      return postInit({ ids: [botId] });
    }

    /** "⚙ 全局初始化": the dialog lists who is taken (not ✔ yet; "全部重新初始化" takes everybody), then one job starts. */
    async function initAll() {
      const job = jobOf();
      if (job) return cancelInit();
      const blocked = initBlockedReason();
      if (blocked) {
        showToast(blocked);
        return false;
      }
      const list = botList();
      if (!list.length) return false;
      const pending = openDialog({
        title: "全局初始化",
        bodyHtml:
          `<div class="dialog-init" id="dialog-init-text">${esc(INIT_CONFIRM)}</div>` +
          `<div id="init-targets">${targetsHtml(false)}</div>` +
          `<label class="checkbox-label dialog-check"><input type="checkbox" id="chk-init-all" /><span>全部重新初始化（包括已经 ✔ 的：它们的记忆文件会被修订，不会清空）</span></label>`,
        okText: "开始初始化",
        okDisabled: Lib.initTargets(list, false).length === 0,
        collect: () => {
          const box = $("chk-init-all");
          return { all: Boolean(box && box.checked) };
        },
      });
      const box = $("chk-init-all");
      if (box) {
        box.checked = false;
        on(box, "change", () => {
          const targets = $("init-targets");
          if (targets) targets.innerHTML = targetsHtml(Boolean(box.checked));
          if (el.dialogOk) el.dialogOk.disabled = Lib.initTargets(list, Boolean(box.checked)).length === 0;
        });
      }
      const choice = await pending;
      if (!choice || !choice.ok) return false;
      const ids = Lib.initTargets(list, Boolean(choice.values && choice.values.all));
      if (!ids.length) return false;
      return postInit({ ids });
    }

    async function cancelInit() {
      try {
        await api.post("api/bots/init:cancel", {});
        showToast("已取消初始化：没写完的文件不会保留，原来的文件不变。");
        refreshBots();
        return true;
      } catch (e) {
        showToast(`取消失败：${errText(e)}`);
        return false;
      }
    }

    /** Counts of the states, "✔ 9 · ⚠ 1 · ✖ 1 · ○ 1" (the states that are not there are left out). */
    function initCountsText() {
      const counts = {};
      for (const b of botList()) counts[b.init.status] = (counts[b.init.status] || 0) + 1;
      return ["ready", "initializing", "stale", "failed", "uninitialized"]
        .filter((k) => counts[k])
        .map((k) => `${Lib.INIT_SYMBOLS[k]} ${counts[k]}`)
        .join(" · ");
    }

    /** What the running job is doing: {total, finished, leftMax} (the largest `seconds_left` of the bots that still run). */
    function jobProgress() {
      const job = jobOf();
      if (!job) return null;
      let finished = 0;
      let leftMax = null;
      for (const id of job.ids) {
        const ev = initProgressOf(id);
        if (ev && FINISHED_INIT.includes(ev.state)) finished++;
        else if (ev && ev.seconds_left !== null && ev.seconds_left !== undefined) leftMax = Math.max(leftMax === null ? 0 : leftMax, ev.seconds_left);
      }
      return { total: job.ids.length, finished, leftMax };
    }

    /**
     * The end of a job (`init_job` went null): one summary for the whole table instead of twelve toasts.  A bot whose last event is not an end
     * (the job was cancelled, or the stream missed it) was interrupted: it is no failure.
     */
    function finishJob() {
      const results = st.jobResults;
      st.jobId = "";
      st.jobResults = {};
      const ids = Object.keys(results);
      if (!ids.length) return;
      const ok = ids.filter((id) => results[id].state === "done");
      const bad = ids.filter((id) => results[id].state === "failed" || results[id].state === "timeout");
      const cut = ids.length - ok.length - bad.length;
      for (const id of ok) editors.memoryChanged(id);
      if (!bad.length && !ok.length) return; // (everything was cancelled: the toast of the cancel said it)
      if (!bad.length) {
        showToast(`初始化完成：${Core.numText(ok.length)} 个玩家都写好了自己的笔记 ✔${cut ? `（${Core.numText(cut)} 个被取消）` : ""}`);
        return;
      }
      const why = bad
        .slice(0, 3)
        .map((id) => {
          const ev = results[id];
          const sum = summaryOf(id);
          // (the server's reason ends with its own full stop: inside the bracket, and before the next reason, it must not)
          return `${sum ? sum.name : ev.name || id}：${Core.stripEndPunct(ev.error) || (ev.state === "timeout" ? "超时" : "失败")}`;
        })
        .join("；");
      showToast(`初始化结束：${Core.numText(ok.length)} 个成功，${Core.numText(bad.length)} 个没成功（${why}${bad.length > 3 ? "……" : ""}）${cut ? `，${Core.numText(cut)} 个被取消` : ""}`);
    }

    /** A `bot.init.progress` event (the store has already kept it in `session.init_progress`): the card, the panel and the bar follow. */
    function onInitEvent(ev) {
      if (!ev) return;
      if (st.jobId !== ev.job_id) {
        st.jobId = ev.job_id;
        st.jobResults = {};
      }
      st.jobResults[ev.bot_id] = ev;
      renderControls();
      if (st.openId && st.openId !== MOD && botIdOf(st.openId) === ev.bot_id) render({ head: true }, true);
    }

    /** `bots.updated` (or a snapshot): the line-up / states / memory statistics may have changed. */
    function onBots() {
      const job = jobOf();
      if (job && st.jobId !== job.job_id) {
        st.jobId = job.job_id;
        st.jobResults = {};
      } else if (!job && st.jobId) finishJob();
      renderControls();
      if (st.openId) render({ head: true, live: true, editors: true }, true);
    }

    // -----------------------------------------------------------------------------------------------------------------
    // review (H31-H33): the buttons of the participant bar and of the panel; the end-of-game dialog is web-play's
    // -----------------------------------------------------------------------------------------------------------------

    /** POST api/game/review {ids?, learn_style?}.  `ids` empty = every bot; `learn_style`: also learn the way a person played (the seats they played). */
    async function review(ids, options) {
      if (st.busy.review) return false;
      st.busy.review = true;
      try {
        const body = ids && ids.length ? { ids } : {};
        if (options && typeof options.learn_style === "boolean") body.learn_style = options.learn_style;
        const res = await api.post("api/game/review", body);
        const secs = (res && res.deadline_s) || 60;
        showToast(ids && ids.length ? `已开始复盘（最多 ${secs} 秒）` : `已开始复盘全部玩家（最多 ${secs} 秒）`);
        return true;
      } catch (e) {
        showToast(`复盘失败：${errText(e)}`);
        return false;
      } finally {
        st.busy.review = false;
        renderControls();
        render({ live: true });
      }
    }

    /** `review.progress` finished for a bot: its memory files were written - what was typed over the old text is moot, the detail is read again. */
    function onReviewEvent(ev) {
      if (!ev || (ev.state !== "done" && ev.state !== "timeout")) {
        renderControls();
        return;
      }
      const botId = ev.bot_id || botIdOf(ev.id);
      if (botId) {
        editors.memoryChanged(botId);
        readDetail(botId, true);
      }
      refreshBots();
    }

    // -----------------------------------------------------------------------------------------------------------------
    // participant bar controls: 全局初始化, the roster menu, 复盘全部
    // -----------------------------------------------------------------------------------------------------------------

    function menuOpen() {
      return Boolean(el.rosterMenu && !el.rosterMenu.hidden);
    }

    function setMenu(openIt) {
      if (!el.rosterMenu) return;
      el.rosterMenu.hidden = !openIt;
      if (el.btnRosterMenu && typeof el.btnRosterMenu.setAttribute === "function") {
        el.btnRosterMenu.setAttribute("aria-expanded", openIt ? "true" : "false");
      }
      if (openIt) placeMenu();
    }

    /** The menu is position: fixed (the bar clips its overflow): put it under its button, inside the screen. */
    function placeMenu() {
      if (!el.rosterMenu || !el.btnRosterMenu || typeof el.btnRosterMenu.getBoundingClientRect !== "function") return;
      const r = el.btnRosterMenu.getBoundingClientRect();
      const vw = Number(win.innerWidth) || 1280;
      const mw = el.rosterMenu.offsetWidth || 260;
      el.rosterMenu.style.left = `${Math.round(Math.min(Math.max(8, r.left), Math.max(8, vw - mw - 8)))}px`;
      el.rosterMenu.style.top = `${Math.round(r.bottom + 5)}px`;
    }

    function usageText() {
      const u = (session().bots && session().bots.learning_usage) || { calls: 0, tokens: 0, cost_usd: 0 };
      if (!u.calls && !u.tokens) return "学习用量：还没有（初始化和复盘的调用记在这里）";
      return `学习用量：${Core.numText(u.calls)} 次调用 · ${Core.formatTokensShort(u.tokens)} tokens · ${Core.formatCost(u.cost_usd, 4)}`;
    }

    function renderControls() {
      const s = session();
      const idle = isIdle();
      const job = jobOf();
      const prog = jobProgress();
      const blocked = initBlockedReason();
      if (el.btnInitAll) {
        if (job) {
          el.btnInitAll.textContent = `■ 取消初始化 ${prog.finished}/${prog.total}`;
          el.btnInitAll.disabled = false;
          el.btnInitAll.title = "取消正在进行的初始化：没写完的文件不会保留，原来的文件不变";
        } else {
          el.btnInitAll.textContent = "⚙ 全局初始化";
          el.btnInitAll.disabled = Boolean(blocked) || st.busy.init;
          el.btnInitAll.title = blocked || "让还没有初始化（或需要重新初始化）的玩家读契约和规则，写出自己的笔记和打法；也可以全部重做";
        }
      }
      if (el.initSummary) {
        const counts = initCountsText();
        const text = job && prog ? `初始化中 ${prog.finished}/${prog.total}${prog.leftMax === null ? "" : ` · 还剩 ${Core.numText(prog.leftMax)} 秒`}` : counts;
        el.initSummary.textContent = text;
        el.initSummary.hidden = !text;
        if (typeof el.initSummary.setAttribute === "function") {
          el.initSummary.setAttribute("title", "✔ 已初始化 · ⚠ 需重新初始化 · ✖ 失败 · ⏳ 初始化中 · ○ 未初始化");
        }
      }
      const enabled = s.memory_enabled !== false;
      if (el.chkMemory) {
        el.chkMemory.checked = enabled;
        el.chkMemory.disabled = !idle || !st.available;
      }
      const lock = !idle || !st.available || Boolean(job);
      if (el.btnClear) el.btnClear.disabled = lock;
      if (el.btnRegen) {
        el.btnRegen.disabled = lock || st.busy.all;
        el.btnRegen.title = idle ? "重新生成全部玩家的头像（随机组合，可以反复点；已经初始化的玩家保持不变）" : "对局已开始：新建对局后才能重新生成头像";
      }
      if (el.usage) el.usage.textContent = usageText();
      if (el.btnReviewAll) {
        const rv = View.reviewView(s);
        el.btnReviewAll.hidden = !rv.visible;
        if (rv.visible) {
          el.btnReviewAll.textContent = rv.running ? `复盘中 ${rv.doneCount}/${rv.total}…` : rv.allDone ? "✓ 已复盘" : "复盘全部";
          el.btnReviewAll.disabled = rv.running || rv.allDone || !rv.available || st.busy.review;
          if (!rv.supported && typeof el.btnReviewAll.setAttribute === "function") el.btnReviewAll.setAttribute("title", "这局是旧版存档，不能复盘");
        }
      }
    }

    async function regenAll() {
      if (st.busy.all) return;
      st.busy.all = true;
      renderControls();
      try {
        const res = await api.post("api/bots/avatars:regenerate", {});
        const live = res && res.bots && typeof res.bots === "object" ? res.bots : null;
        if (live) {
          applyBots(live);
          const patch = (live.bots || []).filter((b) => b && b.seat && b.avatar).map((b) => ({ id: b.seat, name: b.name, avatar: b.avatar }));
          if (patch.length) patchParticipants(patch);
        } else {
          await refreshBots();
        }
        const skipped = res && Array.isArray(res.skipped) ? res.skipped.length : 0;
        showToast(skipped ? `已重新生成头像（${Core.numText(skipped)} 个已经初始化的玩家保持不变）` : "已重新生成全部头像");
        onChange();
      } catch (e) {
        showToast(errText(e));
      } finally {
        st.busy.all = false;
        renderControls();
        render();
      }
    }

    async function clearMemory() {
      const choice = await openDialog({
        title: "清除全部学到的记忆",
        bodyHtml: `<div id="dialog-clear-text">${esc(CLEAR_ALL_CONFIRM)}</div>`,
        okText: "确认清除",
        danger: true,
      });
      if (!choice.ok) return false;
      try {
        await api.post("api/bots/memory:clear", {});
        editors.reset();
        for (const b of botList()) editors.memoryChanged(b.id);
        st.detail.clear();
        st.detailFp = {};
        await refreshBots();
        const botId = openBotId();
        if (botId) readDetail(botId, true);
        showToast("已清除全部学到的记忆（底层设定保留，需要重新初始化）");
        onChange();
        return true;
      } catch (e) {
        showToast(`清除失败：${errText(e)}`);
        return false;
      }
    }

    async function setMemoryEnabled(enabled) {
      try {
        const res = await api.put("api/bots/memory-enabled", { enabled });
        if (res && typeof res === "object" && res.bots) applyBots(res);
        else await refreshBots();
        showToast(enabled ? "本局将加载记忆" : "本局不加载记忆（记忆文件不受影响）");
        onChange();
      } catch (e) {
        if (el.chkMemory) el.chkMemory.checked = !enabled;
        showToast(errText(e));
      }
    }

    function onMenuClick(evt) {
      const node = closestFrom(evt.target, "[data-roster-action]");
      if (!node) return;
      const action = node.getAttribute("data-roster-action");
      if (action === "clear") {
        setMenu(false);
        clearMemory();
      } else if (action === "regen") {
        setMenu(false);
        regenAll();
      }
    }

    // -----------------------------------------------------------------------------------------------------------------
    // window.Bots.focus (the start check's jump), session hooks
    // -----------------------------------------------------------------------------------------------------------------

    /**
     * Take the user to the place that fixes a problem of the start check.  `fix`: edit_core / import_core (the panel of the bot, the L0
     * editor open) · init (the panel, at the initialization row) · bind_model (the bindings dialog of that seat) · import_contract /
     * import_rules (the settings dialog, at that column; `botId` is ignored).  Resolves whether it knew what to do.
     */
    function focus(botId, fix) {
      if (fix === "import_contract") return Boolean(openSettings("contract"));
      if (fix === "import_rules") return Boolean(openSettings("rules"));
      const seat = botId === MOD ? MOD : seatOfBot(botId);
      if (!seat) return false;
      if (fix === "bind_model") {
        openBindings(seat);
        return true;
      }
      if (seat === MOD) return false;
      open(seat);
      let target = "";
      if (fix === "init") target = "detail-init";
      else target = editors.focusFix(botId, fix);
      const node = target ? $(target) : null;
      if (node) {
        if (typeof node.scrollIntoView === "function") node.scrollIntoView({ block: "center" });
        if (typeof node.focus === "function") node.focus();
        if (node.classList && typeof node.classList.add === "function") {
          node.classList.add("flash");
          if (typeof win.setTimeout === "function") win.setTimeout(() => node.classList.remove("flash"), 1800);
        }
      }
      return true;
    }

    /** GET api/bots: the limits and the default library (the live part is the session's).  A missing endpoint (an older backend) turns the editors off. */
    async function load() {
      await refreshBots();
      const botId = openBotId();
      if (botId) readDetail(botId, true);
      render();
      renderControls();
    }

    /** The session changed (snapshot / events). Refresh the controls; reload the bots for a new game. */
    function onSession(s) {
      const key = s && s.session_id ? s.session_id : null;
      if (key !== st.sessionKey) {
        const first = st.sessionKey === null;
        st.sessionKey = key;
        if (!first) {
          editors.reset(); // (a request that is still on its way belongs to the game before)
          st.detail.clear();
          st.detailFp = {};
          st.nameDraft = {};
          st.nameError = {};
          st.avatarOpen = {};
          st.codeDraft = {};
          st.codeNote = {};
          st.changesOpen = {};
          st.initNote = {};
          load();
        }
      }
      onBots();
    }

    // -----------------------------------------------------------------------------------------------------------------
    // panel events
    // -----------------------------------------------------------------------------------------------------------------

    function onPanelClick(evt) {
      const node = closestFrom(evt.target, "[data-detail-action]");
      if (!node) return;
      const action = node.getAttribute("data-detail-action");
      switch (action) {
        case "close":
          close();
          break;
        case "save-name":
          saveName();
          break;
        case "regen-avatar":
          regenAvatar();
          break;
        case "toggle-avatar-pop":
          toggleAvatarPop();
          break;
        case "copy-code":
          copyCode();
          break;
        case "apply-code":
          applyCode();
          break;
        case "open-thoughts":
          // (the button is disabled while the record is closed: this guards a stale click)
          if (st.openId && !thoughtsBlocked(st.openId)) openThoughts(st.openId);
          break;
        case "view-as":
          if (st.openId && st.openId !== MOD && !lockedSeat()) setViewpoint(st.openId);
          break;
        case "toggle-changes":
          if (st.openId) {
            st.changesOpen[st.openId] = st.changesOpen[st.openId] === false;
            render({ live: true });
          }
          break;
        case "review-one":
          if (st.openId && st.openId !== MOD) review([st.openId]);
          break;
        case "init-one":
          if (openBotId()) initOne(openBotId());
          break;
        case "init-cancel":
          cancelInit();
          break;
        case "show-memory":
        case "show-staging": {
          // the review wrote into the memory / proposed changes: open that editor so the user can read it
          const botId = openBotId();
          if (!botId) break;
          const kind = action === "show-memory" ? "memory" : "staging";
          if (action === "show-memory") editors.memoryChanged(botId);
          editors.expand(botId, kind, true);
          readDetail(botId, true);
          render({ editors: true });
          const target = typeof doc.querySelector === "function" ? doc.querySelector(`.detail-editor[data-kind='${kind}']`) : null;
          if (target && typeof target.scrollIntoView === "function") target.scrollIntoView({ block: "nearest" });
          break;
        }
        case "bind": {
          const id = st.openId;
          close();
          if (id) openBindings(id);
          break;
        }
        default:
          editors.onAction(action, node, evt);
          break;
      }
    }

    function onPanelInput(evt) {
      const t = evt && evt.target;
      if (!t || typeof t.getAttribute !== "function") return;
      const id = st.openId;
      if (!id) return;
      if (t.id === "input-avatar-code") {
        onCodeInput(id, String(t.value === undefined || t.value === null ? "" : t.value));
      } else if (t.id === "input-detail-name") {
        st.nameDraft[id] = String(t.value === undefined || t.value === null ? "" : t.value);
      } else {
        editors.onInput(evt);
      }
    }

    function onPanelKeydown(evt) {
      if (!evt || evt.key !== "Enter" || !evt.target) return;
      if (evt.target.id === "input-detail-name") {
        if (typeof evt.preventDefault === "function") evt.preventDefault();
        saveName();
      } else if (evt.target.id === "input-avatar-code") {
        if (typeof evt.preventDefault === "function") evt.preventDefault();
        applyCode();
      }
    }

    /** The shared file inputs (index.html): a file was chosen for the click that opened the picker. */
    function onFileChosen(kind, evt) {
      const file = evt && evt.target && evt.target.files && evt.target.files[0];
      if (!file) return;
      editors.onFile(kind, file);
      if (evt.target) evt.target.value = "";
    }

    on(el.panel, "click", onPanelClick);
    on(el.panel, "focusout", () => {
      // the user left the text box a repaint waited for
      if (st.deferred) {
        st.deferred = false;
        render();
      }
    });
    on(el.panel, "input", onPanelInput);
    on(el.panel, "change", onPanelInput);
    on(el.panel, "keydown", onPanelKeydown);
    on(el.fileBot, "change", (evt) => onFileChosen("arenabot", evt));
    on(el.fileCore, "change", (evt) => onFileChosen("core", evt));

    // the phone drawer: drag the grab handle down to close it
    const grab = el.panel && typeof el.panel.querySelector === "function" ? el.panel.querySelector(".detail-grab") : null;
    if (grab) {
      let startY = null;
      const dragged = (evt) => Math.max(0, Number(evt && evt.clientY) - startY || 0);
      on(grab, "pointerdown", (evt) => {
        startY = Number(evt.clientY);
        if (typeof grab.setPointerCapture === "function" && evt.pointerId !== undefined) {
          try {
            grab.setPointerCapture(evt.pointerId);
          } catch {
            /* the pointer is already gone */
          }
        }
      });
      on(grab, "pointermove", (evt) => {
        if (startY === null) return;
        el.panel.style.transition = "none";
        el.panel.style.transform = `translateY(${dragged(evt)}px)`;
      });
      const end = (evt) => {
        if (startY === null) return;
        const dy = dragged(evt);
        startY = null;
        el.panel.style.transition = "";
        el.panel.style.transform = "";
        if (dy > 90) close();
      };
      on(grab, "pointerup", end);
      on(grab, "pointercancel", end);
    }
    on(el.backdrop, "click", () => close());
    on(el.btnInitAll, "click", () => initAll());
    on(el.btnReviewAll, "click", () => review([]));
    on(el.btnRosterMenu, "click", () => setMenu(!menuOpen()));
    on(el.rosterMenu, "click", onMenuClick);
    on(el.chkMemory, "change", () => setMemoryEnabled(Boolean(el.chkMemory.checked)));
    if (typeof doc.addEventListener === "function") {
      doc.addEventListener("click", (evt) => {
        if (menuOpen() && !inPath(evt, "#roster-menu") && !inPath(evt, "#btn-roster-menu")) {
          setMenu(false);
        }
        if (!st.openId) return;
        // (v0.13.1: the thinking record and the error log are windows of their own that may stand next to the panel)
        if (inPath(evt, "#detail-panel") || inPath(evt, "[data-actor]") || inPath(evt, "#dialog-modal") || inPath(evt, ".vw-window")) {
          return;
        }
        close();
      });
      doc.addEventListener("keydown", (evt) => {
        if (!evt || evt.key !== "Escape") return;
        if (activeFinish) return; // a dialog is open: Esc answers it (see openDialog) and nothing behind it
        if (isWindowOpen()) return; // a window of the page is open: Esc closes that one (thoughts.js / calllog.js)
        if (menuOpen()) setMenu(false);
        else if (st.openId) close();
      });
    }
    if (typeof win.addEventListener === "function") {
      win.addEventListener("resize", () => {
        if (st.openId) position();
        if (menuOpen()) placeMenu();
      });
    }

    const instance = {
      load,
      onSession,
      onBots,
      onInitEvent,
      onReviewEvent,
      render,
      renderControls,
      open,
      close,
      toggle,
      position,
      openId: () => st.openId,
      isOpen: () => st.openId !== null,
      bot: (actorId) => summaryOf(botIdOf(actorId)),
      closeMenu: () => setMenu(false),
      openDialog,
      clearMemory,
      regenAll,
      initAll,
      initOne,
      cancelInit,
      review,
      focus,
      refreshBots,
      editors,
      limits: () => st.limits,
      defaults: () => st.defaults,
      _state: st,
    };
    // the one global of the page (the start check of web-play calls `Bots.focus`)
    root.Bots = {
      focus: (botId, fix) => instance.focus(botId, fix),
      initAll: () => instance.initAll(),
      initOne: (botId) => instance.initOne(botId),
      review: (ids, options) => instance.review(ids, options),
      open: (actorId) => instance.open(actorId),
    };
    return instance;
  }

  root.ArenaBotsUI = {
    create,
    INIT_CONFIRM,
    RESET_CONFIRM,
    CLEAR_ALL_CONFIRM,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = root.ArenaBotsUI;
})(typeof window !== "undefined" ? window : globalThis);
