/* ==========================================================================
   Arena-Agents v0.12.1 — roster UI (E13–E16, E21, E24)
   Loaded after providers.js and before app.js. Exposes window.ArenaRosterUI.create(ctx).

   Owns
     - the player detail panel (a floating card on desktop / tablet, a bottom drawer on phones):
       name (editable while the game has not started), avatar, state, tags, metrics
       (incl. the four token kinds and the cache hit rate), the two expandable editors
       "性格设定" and "记忆" with "用主持人模型完善" (the draft is shown first, only a
       confirmation saves it), review of one player
     - the controls in the participant bar header: regenerate all avatars, the "名册" menu
       (export / import with a preview / clear all memory / "本局加载记忆"), "复盘全部"

   Server API (docs/v0.12-contracts.md §10.4):
     GET  api/roster                         PUT  api/roster/{id} {name?, avatar?, persona?, memory?}
     POST api/roster/{id}/avatar:regenerate  POST api/roster/avatars:regenerate
     POST api/roster/{id}/refine {kind, text} -> {draft}
     GET  api/roster/memory:export (zip)     POST api/roster/memory:import?dry_run=true|false (zip body)
     POST api/roster/memory:clear {include_persona}      PUT api/roster/memory-enabled {enabled}
     POST api/game/review {ids?}

   ctx: { api, Core, Avatar, View, showToast, getSession, getViewpoint, getClockNow,
          bindingText(id), openBindings(id), setViewpoint(id), patchParticipants(list), onChange() }
   ========================================================================== */

(function (root) {
  "use strict";

  const MOD = "moderator";
  const KINDS = ["persona", "memory"];
  const KIND_LABELS = { persona: "性格设定", memory: "记忆" };
  const KIND_HINTS = {
    persona: "说话风格、思维方式。默认为空。",
    memory: "对其他玩家的印象，以及自己玩狼人杀的经验（常输、常赢、胜率高…）。默认为空。",
  };
  const KIND_PLACEHOLDERS = {
    persona: "例如：说话简短直接，喜欢用反问；遇到不确定的情况先观察再表态。",
    memory: "例如：Bob 总是附和别人；我自己前几局常因过早跳身份被刀。",
  };

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
    const patchParticipants = opts.patchParticipants || (() => {});
    const onChange = opts.onChange || (() => {});
    if (!Core || !Avatar || !View || !doc) return null;

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
      btnRegenAll: $("btn-regen-all-avatars"),
      btnRosterMenu: $("btn-roster-menu"),
      rosterMenu: $("roster-menu"),
      btnExport: $("btn-roster-export"),
      btnImport: $("btn-roster-import"),
      btnClear: $("btn-roster-clear"),
      chkMemory: $("chk-memory-enabled"),
      btnReviewAll: $("btn-review-all"),
      fileMemory: $("file-import-memory"),
      dialog: $("dialog-modal"),
      dialogTitle: $("dialog-title"),
      dialogBody: $("dialog-body"),
      dialogOk: $("btn-dialog-ok"),
      dialogCancel: $("btn-dialog-cancel"),
      dialogClose: $("btn-close-dialog"),
    };

    const st = {
      loaded: false, // GET /api/roster succeeded at least once
      available: true, // false: the backend has no roster API (older server)
      editable: true,
      memoryEnabled: true,
      seats: new Map(), // id -> {seat, id, name, default_name, avatar, persona, memory}
      sessionKey: null,
      openId: null,
      drafts: {}, // "N3:persona" -> text typed in the editor (unsaved)
      refine: {}, // "N3:persona" -> {busy, draft}
      saving: {}, // "N3:persona" / "name:N3" / "regen:N3" -> true
      nameDraft: {}, // id -> typed name
      nameError: {},
      expanded: {}, // "N3:persona" -> true
      lastHead: null,
      lastLive: null,
      lastEditors: null,
      busyAll: false,
      busyReview: false,
    };

    const session = () => getSession() || {};
    const isIdle = () => session().status === "idle";
    const isPhone = () => {
      const html = doc.documentElement;
      return Boolean(html && typeof html.getAttribute === "function" && html.getAttribute("data-platform") === "phone");
    };

    function closestFrom(target, selector) {
      let node = target;
      if (node && typeof node.closest !== "function" && node.parentElement) node = node.parentElement;
      return node && typeof node.closest === "function" ? node.closest(selector) : null;
    }

    /**
     * Was the click inside `selector`? Uses the event path computed at dispatch time: a handler may
     * repaint (and so detach) the clicked button before this one runs, and `closest()` would then
     * wrongly say "outside".
     */
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

    // -----------------------------------------------------------------------
    // roster data
    // -----------------------------------------------------------------------

    function applyRoster(resp) {
      const r = Core.normalizeRoster(resp);
      st.seats = new Map(r.seats.map((s) => [s.id, s]));
      st.editable = r.editable;
      st.memoryEnabled = r.memory_enabled;
      st.loaded = true;
      st.available = true;
      return r;
    }

    /** GET /api/roster. A missing endpoint (older backend) just disables the editors. */
    async function load() {
      try {
        const res = await api.get("api/roster");
        applyRoster(res);
        render();
        renderControls();
        return { ok: true };
      } catch (e) {
        if (e && (e.status === 404 || e.status === 405)) st.available = false;
        render();
        renderControls();
        return { ok: false, error: e };
      }
    }

    /** One seat's entry out of a write response (the object itself, or {seat:{…}}, or {seats:[…]}). */
    function mergeSeatResponse(id, res) {
      if (!res || typeof res !== "object") return false;
      const one = res.seat && typeof res.seat === "object" ? res.seat : Array.isArray(res.seats) ? res.seats.find((x) => x && x.id === id) : res;
      if (!one || (one.id !== undefined && one.id !== id)) return false;
      // {ok: true} and friends carry nothing to merge: the caller reads the roster again
      if (!["name", "avatar", "persona", "memory"].some((k) => one[k] !== undefined && one[k] !== null)) return false;
      const cur = st.seats.get(id) || { seat: 0, id, name: id, default_name: "", avatar: null, persona: "", memory: "" };
      const next = { ...cur };
      if (typeof one.name === "string") next.name = one.name;
      if (one.avatar && typeof one.avatar === "object") next.avatar = one.avatar;
      if (typeof one.persona === "string") next.persona = one.persona;
      if (typeof one.memory === "string") next.memory = one.memory;
      st.seats.set(id, next);
      const patch = { id };
      if (typeof one.name === "string") patch.name = one.name;
      if (one.avatar && typeof one.avatar === "object") patch.avatar = one.avatar;
      if (patch.name !== undefined || patch.avatar !== undefined) patchParticipants([patch]);
      return true;
    }

    /** After a write: use what the server returned, otherwise read the roster again. */
    async function afterWrite(id, res) {
      if (!mergeSeatResponse(id, res)) await load();
      render();
      onChange();
    }

    const seatOf = (id) => st.seats.get(id) || null;

    // -----------------------------------------------------------------------
    // detail panel: head
    // -----------------------------------------------------------------------

    function nameValue(p) {
      return st.nameDraft[p.id] !== undefined ? st.nameDraft[p.id] : p.name;
    }

    function headHtml(id) {
      const s = session();
      if (id === MOD) {
        const name = (s.moderator && s.moderator.name) || "主持人";
        // the moderator has a random pixel avatar of its own (the fixed top hat only when the server sent none)
        const avatar = s.moderator && s.moderator.avatar ? s.moderator.avatar : null;
        // (a backend that sends no moderator avatar does not have the endpoint either: no button then)
        const canRegen = Boolean(avatar) && isIdle() && st.editable && st.available;
        const regenBusy = Boolean(st.saving[`regen:${MOD}`]);
        return (
          `<div class="detail-head-row">` +
          `<div class="detail-avatar">${Avatar.imgHtml(MOD, undefined, avatar, { size: 80 })}</div>` +
          `<div class="detail-title"><div class="detail-name">${esc(name)}</div>` +
          `<div class="detail-sub">主持人</div></div>` +
          `<button type="button" class="pop-close" data-detail-action="close" aria-label="关闭">✕</button></div>` +
          (canRegen
            ? `<div class="detail-actions"><button type="button" class="btn btn-xs" id="btn-detail-regen" data-detail-action="regen-avatar"${regenBusy ? " disabled" : ""}>🎲 重新生成头像</button></div>`
            : "")
        );
      }
      const p = View.participantOf(s, id);
      if (!p) return null;
      const editable = isIdle() && st.editable && st.available;
      const regenBusy = Boolean(st.saving[`regen:${id}`]);
      const nameBusy = Boolean(st.saving[`name:${id}`]);
      const err = st.nameError[id] || "";
      const nameBlock = editable
        ? `<div class="detail-name-row"><input type="text" class="form-input detail-name-input" id="input-detail-name"` +
          ` value="${esc(nameValue(p))}" aria-label="名字（1–20 个字符，不能和其他玩家重名）" autocomplete="off" spellcheck="false" />` +
          `<button type="button" class="btn btn-xs btn-primary-outline" id="btn-detail-save-name" data-detail-action="save-name"` +
          `${nameBusy ? " disabled" : ""}>保存名字</button></div>` +
          `<div class="form-error detail-error"${err ? "" : " hidden"}>${esc(err)}</div>`
        : `<div class="detail-name" title="对局未开始时才能改名">${esc(p.name)}</div>`;
      const actions = [];
      if (editable) {
        actions.push(
          `<button type="button" class="btn btn-xs" id="btn-detail-regen" data-detail-action="regen-avatar"${regenBusy ? " disabled" : ""}>🎲 重新生成头像</button>`
        );
      }
      if (getViewpoint() !== id) {
        actions.push(`<button type="button" class="btn btn-xs" data-detail-action="view-as" title="只看这名玩家能看到的内容">👁 以 ${esc(p.name)} 的视角观看</button>`);
      }
      return (
        `<div class="detail-head-row">` +
        `<div class="detail-avatar">${Avatar.imgHtml(id, p.seat, p.avatar, { size: 80 })}<span class="pcard-num detail-num">${Core.numText(p.seat)}</span></div>` +
        `<div class="detail-title">${nameBlock}<div class="detail-sub">${Core.numText(p.seat)} 号${p.out ? " · 已出局" : ""}</div></div>` +
        `<button type="button" class="pop-close" data-detail-action="close" aria-label="关闭">✕</button></div>` +
        (actions.length ? `<div class="detail-actions">${actions.join("")}</div>` : "")
      );
    }

    // -----------------------------------------------------------------------
    // detail panel: live part (state, tags, model, metrics, review)
    // -----------------------------------------------------------------------

    function statHtml(value, label, extra) {
      return `<div class="pop-stat${extra || ""}"><span class="pop-stat-num">${value}</span><span class="pop-stat-label">${esc(label)}</span></div>`;
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
      let review = "";
      if (!isMod) {
        const rv = View.reviewView(s);
        if (rv.visible) {
          const one = rv.of(id);
          if (one.state === "done") {
            review = `<div class="detail-review done"><span class="review-badge done">✓ 已复盘</span>` +
              `<button type="button" class="btn btn-xs" data-detail-action="show-memory">查看写入的记忆</button>` +
              `<span class="form-hint">战绩与复盘已写入该玩家的记忆。</span></div>`;
          } else if (one.state === "running") {
            review = `<div class="detail-review"><span class="review-badge running">⏳ 复盘中…</span>${one.message ? `<span class="form-hint">${esc(one.message)}</span>` : ""}</div>`;
          } else {
            review =
              `<div class="detail-review"><button type="button" class="btn btn-xs btn-primary-outline" data-detail-action="review-one"` +
              `${rv.available ? "" : " disabled"}>复盘此玩家</button>` +
              `${one.state === "error" ? `<span class="form-hint error">上次复盘失败：${esc(one.message)}</span>` : `<span class="form-hint">让该玩家用自己的模型复盘本局，写入记忆。</span>`}</div>`;
          }
        }
      }
      return (
        stateRow +
        `<div class="pop-tags">${chips}</div>` +
        `<div class="pop-model"><span class="pop-label">模型</span>` +
        `<span class="pop-model-text" title="${esc(bind)}">${esc(bind)}</span>` +
        `<button type="button" class="btn btn-xs" data-detail-action="bind">配置</button></div>` +
        `<div class="pop-grid">${grid}</div>` +
        review
      );
    }

    // -----------------------------------------------------------------------
    // detail panel: persona / memory editors
    // -----------------------------------------------------------------------

    const keyOf = (id, kind) => `${id}:${kind}`;

    function savedText(id, kind) {
      const seat = seatOf(id);
      return seat ? seat[kind] || "" : "";
    }

    function currentText(id, kind) {
      const k = keyOf(id, kind);
      return st.drafts[k] !== undefined ? st.drafts[k] : savedText(id, kind);
    }

    function editorHtml(id, kind) {
      const k = keyOf(id, kind);
      const open = Boolean(st.expanded[k]);
      const text = currentText(id, kind);
      const saved = savedText(id, kind);
      const dirty = text !== saved;
      const saving = Boolean(st.saving[k]);
      const rf = st.refine[k] || {};
      const usable = st.available && st.loaded;
      const meta = saved ? `已设置 · ${Core.charLength(saved)} 字` : "空";
      const draftBlock =
        rf.draft !== undefined && rf.draft !== null
          ? `<div class="detail-draft"><div class="detail-draft-title">主持人模型给出的草稿（尚未保存，请先看一遍）</div>` +
            `<div class="detail-draft-text">${esc(rf.draft)}</div>` +
            `<div class="detail-editor-actions">` +
            `<button type="button" class="btn btn-xs btn-primary-outline" data-detail-action="accept-draft" data-kind="${kind}"${saving ? " disabled" : ""}>采用并保存</button>` +
            `<button type="button" class="btn btn-xs" data-detail-action="discard-draft" data-kind="${kind}">放弃草稿</button></div></div>`
          : "";
      return (
        `<section class="detail-editor${open ? " open" : ""}" data-kind="${kind}">` +
        `<button type="button" class="detail-editor-toggle" data-detail-action="toggle-editor" data-kind="${kind}" aria-expanded="${open}">` +
        `<span class="caret" aria-hidden="true">${open ? "▼" : "▶"}</span><span class="detail-editor-title">${KIND_LABELS[kind]}</span>` +
        `<span class="detail-editor-meta">${esc(meta)}</span></button>` +
        `<div class="detail-editor-body">` +
        `<div class="form-hint">${esc(KIND_HINTS[kind])}${isIdle() ? "" : "　对局进行中修改的内容从下一局开始生效。"}</div>` +
        `<textarea class="form-input detail-textarea" id="ta-detail-${kind}" data-draft-key="${esc(k)}" rows="6"` +
        ` placeholder="${esc(KIND_PLACEHOLDERS[kind])}"${usable ? "" : " disabled"}>${esc(text)}</textarea>` +
        `<div class="detail-editor-actions">` +
        `<button type="button" class="btn btn-xs" id="btn-detail-refine-${kind}" data-detail-action="refine" data-kind="${kind}"` +
        `${usable && !rf.busy ? "" : " disabled"} title="把上面的要点交给主持人绑定的模型，扩写成完整设定；结果先给你确认再保存">${rf.busy ? "完善中…" : "✨ 用主持人模型完善"}</button>` +
        `<button type="button" class="btn btn-xs btn-primary-outline" id="btn-detail-save-${kind}" data-detail-action="save-text" data-kind="${kind}"` +
        `${usable && dirty && !saving ? "" : " disabled"}>${saving ? "保存中…" : "保存"}</button></div>` +
        `${usable ? "" : `<div class="form-hint error">名册接口不可用（后端可能尚未升级），暂时不能编辑。</div>`}` +
        `${draftBlock}</div></section>`
      );
    }

    function editorsHtml(id) {
      if (id === MOD) return "";
      return KINDS.map((kind) => editorHtml(id, kind)).join("");
    }

    // -----------------------------------------------------------------------
    // detail panel: open / close / render / position
    // -----------------------------------------------------------------------

    function resetCaches() {
      st.lastHead = null;
      st.lastLive = null;
      st.lastEditors = null;
    }

    /** Repaint the open panel. `only` = {head, live, editors}; default everything. */
    function render(only) {
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
      if (want.head) setHtmlIfChanged(el.head, head, "lastHead");
      if (want.live) setHtmlIfChanged(el.live, liveHtml(st.openId) || "", "lastLive");
      if (want.editors) setHtmlIfChanged(el.editors, editorsHtml(st.openId), "lastEditors");
      el.panel.hidden = false;
      if (el.backdrop) el.backdrop.hidden = !isPhone();
      position();
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

    // -----------------------------------------------------------------------
    // detail panel: actions
    // -----------------------------------------------------------------------

    const roster = () => Array.from(st.seats.values());

    async function saveName() {
      const id = st.openId;
      const p = id ? View.participantOf(session(), id) : null;
      if (!p || st.saving[`name:${id}`]) return;
      const input = $("input-detail-name");
      const raw = input ? input.value : nameValue(p);
      const others = (session().participants || []).filter((x) => x.id !== id).map((x) => x.name);
      const checked = Core.validateNickname(raw, others);
      if (!checked.ok) {
        st.nameError[id] = checked.error;
        st.nameDraft[id] = raw;
        render({ head: true });
        return;
      }
      if (checked.value === p.name) {
        st.nameError[id] = "";
        delete st.nameDraft[id];
        render({ head: true });
        return;
      }
      st.saving[`name:${id}`] = true;
      st.nameError[id] = "";
      render({ head: true });
      try {
        const res = await api.put(`api/roster/${enc(id)}`, { name: checked.value });
        delete st.nameDraft[id];
        // the response, else the SSE roster.updated, else a re-read keeps every view in step
        if (!mergeSeatResponse(id, res)) patchParticipants([{ id, name: checked.value }]);
        showToast(`名字已保存：${checked.value}`);
        onChange();
      } catch (e) {
        st.nameError[id] = e.message || "保存失败";
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
      render({ head: true });
      try {
        const res = await api.post(`api/roster/${enc(id)}/avatar:regenerate`);
        if (id === MOD) {
          // the moderator is not a seat: its new avatar goes straight to the session's moderator
          if (res && res.avatar && typeof res.avatar === "object") patchParticipants([{ id: MOD, avatar: res.avatar }]);
          else await load();
          render();
          onChange();
        } else {
          await afterWrite(id, res);
        }
        showToast(id === MOD ? "主持人头像已重新生成" : "头像已重新生成");
      } catch (e) {
        showToast(e.message);
      } finally {
        delete st.saving[`regen:${id}`];
        render({ head: true });
      }
    }

    async function saveText(kind, valueOverride) {
      const id = st.openId;
      if (!id || !KINDS.includes(kind)) return false;
      const k = keyOf(id, kind);
      if (st.saving[k]) return false;
      const ta = $(`ta-detail-${kind}`);
      const text = valueOverride !== undefined ? valueOverride : ta ? ta.value : currentText(id, kind);
      st.saving[k] = true;
      render({ editors: true });
      try {
        const res = await api.put(`api/roster/${enc(id)}`, { [kind]: text });
        if (!mergeSeatResponse(id, res) || typeof (res || {})[kind] !== "string") {
          const cur = st.seats.get(id);
          if (cur) st.seats.set(id, { ...cur, [kind]: text });
        }
        delete st.drafts[k];
        delete st.refine[k];
        showToast(`${KIND_LABELS[kind]}已保存`);
        return true;
      } catch (e) {
        showToast(e.message);
        return false;
      } finally {
        delete st.saving[k];
        render({ editors: true });
      }
    }

    async function refine(kind) {
      const id = st.openId;
      if (!id || !KINDS.includes(kind)) return;
      const k = keyOf(id, kind);
      if (st.refine[k] && st.refine[k].busy) return;
      const ta = $(`ta-detail-${kind}`);
      const text = ta ? ta.value : currentText(id, kind);
      st.drafts[k] = text;
      if (!String(text).trim()) {
        showToast(`请先在「${KIND_LABELS[kind]}」里写几个要点，再让模型完善`);
        return;
      }
      st.refine[k] = { busy: true };
      render({ editors: true });
      try {
        const res = await api.post(`api/roster/${enc(id)}/refine`, { kind, text });
        const draft = res && typeof res.draft === "string" ? res.draft : "";
        if (!draft.trim()) throw new Error("模型没有返回草稿，请稍后重试");
        st.refine[k] = { draft };
        st.expanded[k] = true;
      } catch (e) {
        delete st.refine[k];
        showToast(`完善失败：${e.message}`);
      } finally {
        render({ editors: true });
        // the draft (and its "采用并保存" button) appears below the text box: bring it into view
        const node = typeof doc.querySelector === "function" ? doc.querySelector(".detail-draft") : null;
        if (node && typeof node.scrollIntoView === "function") node.scrollIntoView({ block: "nearest" });
      }
    }

    async function acceptDraft(kind) {
      const id = st.openId;
      if (!id) return;
      const k = keyOf(id, kind);
      const rf = st.refine[k];
      if (!rf || typeof rf.draft !== "string") return;
      const ok = await saveText(kind, rf.draft);
      if (!ok) st.refine[k] = rf;
      render({ editors: true });
    }

    function discardDraft(kind) {
      const id = st.openId;
      if (!id) return;
      delete st.refine[keyOf(id, kind)];
      render({ editors: true });
    }

    async function review(ids) {
      if (st.busyReview) return;
      st.busyReview = true;
      try {
        await api.post("api/game/review", ids && ids.length ? { ids } : {});
        showToast(ids && ids.length ? "已开始复盘" : "已开始复盘全部玩家");
      } catch (e) {
        showToast(`复盘失败：${e.message}`);
      } finally {
        st.busyReview = false;
        renderControls();
        render({ live: true });
      }
    }

    function onPanelClick(evt) {
      const node = closestFrom(evt.target, "[data-detail-action]");
      if (!node) return;
      const action = node.getAttribute("data-detail-action");
      const kind = node.getAttribute("data-kind");
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
        case "view-as":
          if (st.openId && st.openId !== MOD) setViewpoint(st.openId);
          break;
        case "toggle-editor": {
          if (!st.openId || !KINDS.includes(kind)) break;
          const k = keyOf(st.openId, kind);
          // keep what was typed before the section is repainted
          const ta = $(`ta-detail-${kind}`);
          if (ta && typeof ta.value === "string") st.drafts[k] = ta.value;
          st.expanded[k] = !st.expanded[k];
          render({ editors: true });
          break;
        }
        case "refine":
          refine(kind);
          break;
        case "save-text":
          saveText(kind);
          break;
        case "accept-draft":
          acceptDraft(kind);
          break;
        case "discard-draft":
          discardDraft(kind);
          break;
        case "review-one":
          if (st.openId && st.openId !== MOD) review([st.openId]);
          break;
        case "show-memory": {
          // the review wrote into the memory file: open that editor so the user can read it
          if (!st.openId) break;
          const k = keyOf(st.openId, "memory");
          delete st.drafts[k];
          st.expanded[k] = true;
          render({ editors: true });
          const node = typeof doc.querySelector === "function" ? doc.querySelector(".detail-editor[data-kind='memory']") : null;
          if (node && typeof node.scrollIntoView === "function") node.scrollIntoView({ block: "nearest" });
          break;
        }
        case "bind": {
          const id = st.openId;
          close();
          if (id) openBindings(id);
          break;
        }
        default:
          break;
      }
    }

    function onPanelInput(evt) {
      const t = evt && evt.target;
      if (!t || typeof t.getAttribute !== "function") return;
      const key = t.getAttribute("data-draft-key");
      if (key) {
        st.drafts[key] = String(t.value === undefined ? "" : t.value);
        // the Save button follows the text without repainting (the cursor must stay put)
        const kind = key.split(":")[1];
        const id = key.split(":")[0];
        const btn = $(`btn-detail-save-${kind}`);
        if (btn) btn.disabled = st.drafts[key] === savedText(id, kind) || Boolean(st.saving[key]);
        return;
      }
      if (t.id === "input-detail-name" && st.openId) {
        st.nameDraft[st.openId] = String(t.value === undefined ? "" : t.value);
      }
    }

    function onPanelKeydown(evt) {
      if (evt && evt.key === "Enter" && evt.target && evt.target.id === "input-detail-name") {
        if (typeof evt.preventDefault === "function") evt.preventDefault();
        saveName();
      }
    }

    // -----------------------------------------------------------------------
    // dialogs
    // -----------------------------------------------------------------------

    /** A small modal: resolves {ok, values}. `collect()` reads the fields when the user confirms. */
    function openDialog(o) {
      return new Promise((resolve) => {
        if (!el.dialog || !el.dialogBody) {
          resolve({ ok: typeof win.confirm === "function" ? win.confirm(String(o.title || "确认")) : false, values: {} });
          return;
        }
        if (el.dialogTitle) el.dialogTitle.textContent = o.title || "确认";
        el.dialogBody.innerHTML = o.bodyHtml || "";
        if (el.dialogOk) {
          el.dialogOk.textContent = o.okText || "确定";
          el.dialogOk.className = `btn ${o.danger ? "btn-danger-outline" : "btn-primary-outline"}`;
          el.dialogOk.disabled = false;
        }
        if (el.dialogCancel) {
          el.dialogCancel.textContent = o.cancelText || "取消";
          el.dialogCancel.hidden = Boolean(o.hideCancel);
        }
        el.dialog.style.display = "flex";
        const finish = (ok) => {
          el.dialog.style.display = "none";
          if (el.dialogOk) el.dialogOk.removeEventListener("click", onOk);
          if (el.dialogCancel) el.dialogCancel.removeEventListener("click", onCancel);
          if (el.dialogClose) el.dialogClose.removeEventListener("click", onCancel);
          resolve({ ok, values: ok && typeof o.collect === "function" ? o.collect() : {} });
        };
        const onOk = () => finish(true);
        const onCancel = () => finish(false);
        if (el.dialogOk) el.dialogOk.addEventListener("click", onOk);
        if (el.dialogCancel) el.dialogCancel.addEventListener("click", onCancel);
        if (el.dialogClose) el.dialogClose.addEventListener("click", onCancel);
      });
    }

    // -----------------------------------------------------------------------
    // participant bar controls: roster menu, regenerate all, review all
    // -----------------------------------------------------------------------

    function menuOpen() {
      return Boolean(el.rosterMenu && !el.rosterMenu.hidden);
    }

    function setMenu(open) {
      if (!el.rosterMenu) return;
      el.rosterMenu.hidden = !open;
      if (el.btnRosterMenu && typeof el.btnRosterMenu.setAttribute === "function") {
        el.btnRosterMenu.setAttribute("aria-expanded", open ? "true" : "false");
      }
      if (open) placeMenu();
    }

    /** The menu is position: fixed (the bar clips its overflow): put it under its button, inside the screen. */
    function placeMenu() {
      if (!el.rosterMenu || !el.btnRosterMenu || typeof el.btnRosterMenu.getBoundingClientRect !== "function") return;
      const r = el.btnRosterMenu.getBoundingClientRect();
      const vw = Number(win.innerWidth) || 1280;
      const mw = el.rosterMenu.offsetWidth || 240;
      el.rosterMenu.style.left = `${Math.round(Math.min(Math.max(8, r.left), Math.max(8, vw - mw - 8)))}px`;
      el.rosterMenu.style.top = `${Math.round(r.bottom + 5)}px`;
    }

    function renderControls() {
      const s = session();
      const idle = s.status === "idle";
      if (el.btnRegenAll) {
        el.btnRegenAll.disabled = !idle || !st.editable || !st.available || st.busyAll;
        el.btnRegenAll.title = idle
          ? "重新生成全部玩家的头像（随机组合，可以反复点）"
          : "对局已开始：新建对局后才能重新生成头像";
      }
      const enabled = s.memory_enabled !== false;
      if (el.chkMemory) {
        el.chkMemory.checked = enabled;
        el.chkMemory.disabled = !idle || !st.available;
      }
      const lock = !idle || !st.available;
      if (el.btnImport) el.btnImport.disabled = lock;
      if (el.btnClear) el.btnClear.disabled = lock;
      if (el.btnExport) el.btnExport.disabled = !st.available;
      if (el.btnReviewAll) {
        const rv = View.reviewView(s);
        el.btnReviewAll.hidden = !rv.visible;
        if (rv.visible) {
          el.btnReviewAll.textContent = rv.running
            ? `复盘中 ${rv.doneCount}/${rv.total}…`
            : rv.allDone
              ? "✓ 已复盘"
              : "复盘全部";
          el.btnReviewAll.disabled = rv.running || rv.allDone || !rv.available || st.busyReview;
        }
      }
    }

    async function regenAll() {
      if (st.busyAll) return;
      st.busyAll = true;
      renderControls();
      try {
        const res = await api.post("api/roster/avatars:regenerate");
        if (res && Array.isArray(res.seats) && res.seats.length) {
          applyRoster({ ...res, memory_enabled: st.memoryEnabled, editable: st.editable });
          const patch = res.seats.filter((x) => x && x.id).map((x) => ({ id: x.id, name: x.name, avatar: x.avatar }));
          // "all" includes the moderator: it is re-rolled together with the twelve seats
          if (res.moderator && typeof res.moderator === "object" && res.moderator.avatar) patch.push({ id: MOD, avatar: res.moderator.avatar });
          patchParticipants(patch);
        } else {
          await load();
          patchParticipants(roster().map((x) => ({ id: x.id, name: x.name, avatar: x.avatar })));
        }
        showToast("已重新生成全部头像");
        onChange();
      } catch (e) {
        showToast(e.message);
      } finally {
        st.busyAll = false;
        renderControls();
        render();
      }
    }

    function saveBlob(blob, filename) {
      try {
        const url = win.URL && win.URL.createObjectURL ? win.URL.createObjectURL(blob) : URL.createObjectURL(blob);
        const a = doc.createElement("a");
        a.href = url;
        a.download = filename;
        if (doc.body && typeof doc.body.appendChild === "function") doc.body.appendChild(a);
        a.click();
        if (typeof a.remove === "function") a.remove();
        (win.URL || URL).revokeObjectURL(url);
        return true;
      } catch {
        return false;
      }
    }

    async function exportMemory() {
      try {
        const res = await api.download("api/roster/memory:export");
        const name = (res && res.filename) || "arena-memory.zip";
        saveBlob(res.blob, name);
        showToast(`已导出全部记忆：${name}`);
      } catch (e) {
        showToast(`导出失败：${e.message}`);
      }
    }

    const nameOfId = (id) => {
      const p = View.participantOf(session(), id);
      return p ? p.name : id;
    };

    async function importMemory(file) {
      if (!file) return;
      let buf;
      try {
        buf = typeof file.arrayBuffer === "function" ? await file.arrayBuffer() : file;
      } catch {
        showToast("无法读取所选文件");
        return;
      }
      let preview;
      try {
        preview = await api.upload("api/roster/memory:import?dry_run=true", buf, "application/zip");
      } catch (e) {
        showToast(`记忆包无法导入：${e.message}`);
        return;
      }
      const changes = Array.isArray(preview && preview.changes) ? preview.changes : [];
      if (!changes.length) {
        showToast("记忆包里没有可导入的玩家内容");
        return;
      }
      const rows = changes
        .map((c) => {
          const what = [c.persona ? "性格设定" : "", c.memory ? "记忆" : ""].filter(Boolean).join("、") || "（无变化）";
          return `<li><strong>${esc(nameOfId(c.id))}</strong>：<span class="muted">${esc(what)}</span> 将被覆盖</li>`;
        })
        .join("");
      const choice = await openDialog({
        title: "导入记忆包",
        bodyHtml:
          `<div>导入后，下面这些玩家的内容会被记忆包里的版本<strong>覆盖</strong>（没有列出的玩家不受影响）：</div>` +
          `<ul class="dialog-list">${rows}</ul>`,
        okText: `确认导入 ${changes.length} 名玩家`,
      });
      if (!choice.ok) return;
      try {
        await api.upload("api/roster/memory:import?dry_run=false", buf, "application/zip");
        await load();
        render();
        showToast(`已导入 ${changes.length} 名玩家的记忆`);
        onChange();
      } catch (e) {
        showToast(`导入失败：${e.message}`);
      }
    }

    async function clearMemory() {
      const choice = await openDialog({
        title: "清除全部记忆",
        bodyHtml:
          `<div>将清空<strong>全部 12 名玩家</strong>的记忆文件（战绩、对局记忆、经验与印象）。此操作不能撤销，建议先「导出全部记忆」。</div>` +
          `<label class="checkbox-label dialog-check"><input type="checkbox" id="chk-dialog-include-persona" />` +
          `<span>同时清除性格设定</span></label>`,
        okText: "确认清除",
        danger: true,
        collect: () => {
          const box = $("chk-dialog-include-persona");
          return { include_persona: Boolean(box && box.checked) };
        },
      });
      if (!choice.ok) return;
      const include = Boolean(choice.values && choice.values.include_persona);
      try {
        await api.post("api/roster/memory:clear", { include_persona: include });
        // drop unsaved editor text so the cleared files show as empty
        st.drafts = {};
        st.refine = {};
        await load();
        showToast(include ? "已清除全部记忆和性格设定" : "已清除全部记忆");
        onChange();
      } catch (e) {
        showToast(`清除失败：${e.message}`);
      }
    }

    async function setMemoryEnabled(enabled) {
      try {
        const res = await api.put("api/roster/memory-enabled", { enabled });
        st.memoryEnabled = res && typeof res.enabled === "boolean" ? res.enabled : enabled;
        showToast(enabled ? "本局将加载记忆" : "本局不加载记忆（记忆文件不受影响）");
        onChange();
      } catch (e) {
        if (el.chkMemory) el.chkMemory.checked = !enabled;
        showToast(e.message);
      }
    }

    function onMenuClick(evt) {
      const node = closestFrom(evt.target, "[data-roster-action]");
      if (!node) return;
      const action = node.getAttribute("data-roster-action");
      if (action === "export") {
        setMenu(false);
        exportMemory();
      } else if (action === "import") {
        setMenu(false);
        if (el.fileMemory && typeof el.fileMemory.click === "function") el.fileMemory.click();
      } else if (action === "clear") {
        setMenu(false);
        clearMemory();
      }
    }

    // -----------------------------------------------------------------------
    // session hooks
    // -----------------------------------------------------------------------

    /** The session changed (snapshot / events). Refresh the controls; reload the roster for a new game. */
    function onSession(s) {
      const key = s && s.session_id ? s.session_id : null;
      if (key !== st.sessionKey) {
        const first = st.sessionKey === null;
        st.sessionKey = key;
        if (!first) {
          st.drafts = {};
          st.refine = {};
          st.nameDraft = {};
          st.nameError = {};
          load();
        }
      }
      renderControls();
    }

    /** review.progress finished for someone: their memory file changed. */
    function onReviewDone() {
      load();
    }

    // -----------------------------------------------------------------------
    // wiring
    // -----------------------------------------------------------------------

    function on(node, type, handler) {
      if (node && typeof node.addEventListener === "function") node.addEventListener(type, handler);
    }

    on(el.panel, "click", onPanelClick);
    on(el.panel, "input", onPanelInput);
    on(el.panel, "keydown", onPanelKeydown);

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
    on(el.btnRegenAll, "click", () => regenAll());
    on(el.btnReviewAll, "click", () => review([]));
    on(el.btnRosterMenu, "click", () => setMenu(!menuOpen()));
    on(el.rosterMenu, "click", onMenuClick);
    on(el.chkMemory, "change", () => setMemoryEnabled(Boolean(el.chkMemory.checked)));
    on(el.fileMemory, "change", (evt) => {
      const file = evt && evt.target && evt.target.files && evt.target.files[0];
      if (file) importMemory(file);
      if (el.fileMemory) el.fileMemory.value = "";
    });
    if (typeof doc.addEventListener === "function") {
      doc.addEventListener("click", (evt) => {
        if (menuOpen() && !inPath(evt, "#roster-menu") && !inPath(evt, "#btn-roster-menu")) {
          setMenu(false);
        }
        if (!st.openId) return;
        if (inPath(evt, "#detail-panel") || inPath(evt, "[data-actor]") || inPath(evt, "#dialog-modal")) {
          return;
        }
        close();
      });
      doc.addEventListener("keydown", (evt) => {
        if (!evt || evt.key !== "Escape") return;
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

    return {
      load,
      onSession,
      onReviewDone,
      render,
      renderControls,
      open,
      close,
      toggle,
      position,
      openId: () => st.openId,
      isOpen: () => st.openId !== null,
      seat: seatOf,
      closeMenu: () => setMenu(false),
      openDialog,
      importMemory,
      exportMemory,
      clearMemory,
      regenAll,
      _state: st,
    };
  }

  root.ArenaRosterUI = { create };
})(typeof window !== "undefined" ? window : globalThis);
